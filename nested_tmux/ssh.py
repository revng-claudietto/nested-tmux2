"""Private OpenSSH masters and channels. Remote data never selects local routes."""

import errno
import fcntl
import hashlib
import os
import select
import shlex
import socket
import stat
import subprocess
import time
from collections import deque
from pathlib import Path

from .config import Error


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE", "TMUX_PARENT"}}


class Transport:
    def __init__(self, host="local", ssh_config=None, master=None):
        self.host = host
        self.ssh_config = ssh_config
        self.master = master

    def ssh(self, terminal=False):
        args = ["ssh", "-tt" if terminal else "-T", "-a", "-x", "-e", "none"]
        for option in (
            "ForwardAgent=no",
            "ForwardX11=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "BatchMode=yes",
            "ConnectTimeout=10",
            "ServerAliveInterval=10",
            "ServerAliveCountMax=3",
        ):
            args += ["-o", option]
        if self.ssh_config:
            args += ["-F", str(self.ssh_config)]
        return args

    def wrap(self, args, terminal=False):
        if self.host == "local":
            return args
        ssh = self.ssh(terminal)
        ssh += ["-o", "ControlMaster=no", "-o", "ControlPersist=no"]
        if self.master:
            # A missing/refused mux channel must fail, never create a second
            # independent connection. Only the controller creates masters.
            ssh += ["-S", str(self.master.path), "-o", "ProxyCommand=false"]
        else:
            ssh += ["-o", "ControlPath=none"]
        return ssh + ["--", self.host, "exec " + shlex.join(args)]


class SSHMaster:
    def __init__(self, state, host, ssh_config=None, persist=300):
        self.transport = Transport(host, ssh_config)
        self.persist = persist
        identity = f"mux-v1:{host}:{ssh_config or ''}"
        self.path = Path(state).absolute() / (
            "s-" + hashlib.sha256(identity.encode()).hexdigest()[:16]
        )
        # ssh expands ControlPath tokens, and adds a random temporary suffix
        # when binding. Never let expansions redirect this private socket.
        if any(c in str(self.path) for c in "%$~") or len(os.fsencode(self.path)) > 88:
            raise Error(
                "SSH runtime path is too long or contains %, $ or ~; use a short --runtime-dir"
            )
        self.retry_after = 0
        self.last_error = ""

    def _private(self):
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Error(f"{parent} must be a private directory owned by this user (mode 0700)")
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Error("unsafe SSH master socket")

    @staticmethod
    def _run(args, timeout=12):
        # Bound diagnostics even if the remote SSH banner/stderr is hostile.
        chunks = deque(maxlen=16)
        process = subprocess.Popen(
            args,
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + timeout
        os.set_blocking(process.stderr.fileno(), False)
        try:
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Error("SSH master operation timed out")
                if select.select([process.stderr], [], [], min(0.05, remaining))[0]:
                    block = os.read(process.stderr.fileno(), 4096)
                    if block:
                        chunks.append(block)
                    else:
                        process.wait(timeout=remaining)
            for _ in range(16):
                try:
                    block = os.read(process.stderr.fileno(), 4096)
                except BlockingIOError:
                    break
                if not block:
                    break
                chunks.append(block)
            return process.returncode, b"".join(chunks).decode(errors="replace").strip()
        except subprocess.TimeoutExpired as exc:
            raise Error("SSH master operation timed out") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stderr.close()

    def command(self, operation):
        return self.transport.ssh() + [
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPersist=no",
            "-S",
            str(self.path),
            "-O",
            operation,
            "--",
            self.transport.host,
        ]

    def ensure(self):
        self._private()
        lock = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            # The file lock also serializes a controller restart against any
            # still-finishing recovery from its predecessor.
            fcntl.flock(lock, fcntl.LOCK_EX)
            if time.monotonic() < self.retry_after:
                raise Error(self.last_error)
            try:
                if self.path.exists():
                    code, _ = self._run(self.command("check"), timeout=2)
                    if code == 0:
                        return
                    # Remove only a dead socket. A live but unresponsive master
                    # is not permission to start a second TCP connection.
                    with socket.socket(socket.AF_UNIX) as probe:
                        probe.settimeout(1)
                        try:
                            probe.connect(str(self.path))
                        except OSError as exc:
                            if exc.errno not in {errno.ENOENT, errno.ECONNREFUSED}:
                                raise
                        else:
                            raise Error("SSH master is not responding")
                    self._private()
                    self.path.unlink(missing_ok=True)
                args = self.transport.ssh() + [
                    "-N",
                    "-f",
                    "-o",
                    "ControlMaster=yes",
                    "-o",
                    f"ControlPersist={self.persist}",
                    "-S",
                    str(self.path),
                    "--",
                    self.transport.host,
                ]
                code, detail = self._run(args)
                if code:
                    raise Error(f"SSH master connection failed: {detail}")
                self._private()
                code, detail = self._run(self.command("check"), timeout=2)
                if code:
                    raise Error(f"SSH master did not become ready: {detail}")
            except (Error, OSError) as exc:
                self.retry_after = time.monotonic() + 1
                self.last_error = str(exc)
                raise
        finally:
            os.close(lock)

    def stop(self):
        """Close a workspace-owned local master without making an SSH connection."""
        self._private()
        if self.path.exists():
            code, detail = self._run(self.command("exit"), timeout=2)
            if code and not any(text in detail for text in ("No such file", "Connection refused")):
                raise Error(f"could not close SSH master: {detail}")
