"""Documented tmux control mode, over pipes or SSH (never the binary socket protocol)."""

import os
import queue
import shlex
import subprocess
import threading
from collections import deque

from .config import Error

MAX_LINE = 65536
MAX_RESPONSE = 1024 * 1024
MAX_QUEUED_LINES = 128


class TransportError(Error):
    """The stream cannot safely be reused; the last command may have executed."""


def quote(value):
    """Quote one tmux parser token. Shell quoting is a separate operation."""
    value = str(value)
    if "\0" in value:
        raise Error("tmux command arguments cannot contain NUL")
    # Single quotes suppress tmux variable expansion, including a leading '$'.
    # Concatenate an escaped quote/newline between literal segments as needed.
    return (
        "'"
        + (value.replace("'", "'\\''").replace("\n", "'\"\\n\"'").replace("\r", "'\"\\r\"'"))
        + "'"
    )


def command(*args):
    return " ".join(quote(arg) for arg in args)


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE", "TMUX_PARENT"}}


class Transport:
    def __init__(self, host="local", ssh_config=None):
        self.host = host
        self.ssh_config = ssh_config

    def wrap(self, args, terminal=False):
        if self.host == "local":
            return args
        ssh = [
            "ssh",
            "-tt" if terminal else "-T",
            "-a",
            "-x",
            "-e",
            "none",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "ControlPersist=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.ssh_config:
            ssh += ["-F", str(self.ssh_config)]
        # ssh joins arguments with spaces; transmit exactly one shell-quoted command.
        return ssh + ["--", self.host, "exec " + shlex.join(args)]


class Control:
    def __init__(self, transport, socket_name, timeout=15):
        self.transport = transport
        self.socket_name = socket_name
        self.timeout = timeout
        self.process = None
        self.lines = queue.Queue(maxsize=MAX_QUEUED_LINES)
        self.errors = deque(maxlen=20)
        self.threads = []

    def connect(self, create=True):
        self.close()
        self.lines = queue.Queue(maxsize=MAX_QUEUED_LINES)
        self.errors = deque(maxlen=20)
        args = ["tmux", "-u", "-L", self.socket_name, "-f", "/dev/null", "-C"]
        args += (
            ["new-session", "-A", "-s", "tree", "-n", "__bootstrap__", "sleep 2147483647"]
            if create
            else ["attach-session", "-t", "tree"]
        )
        self.process = subprocess.Popen(
            self.transport.wrap(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        process, lines, errors = self.process, self.lines, self.errors

        def read_stdout():
            try:
                while line := process.stdout.readline(MAX_LINE + 1):
                    if len(line) > MAX_LINE:
                        errors.append("remote control line exceeds size limit")
                        process.terminate()
                        break
                    try:
                        lines.put_nowait(line.rstrip("\r\n"))
                    except queue.Full:
                        errors.append("remote control notification queue exceeds size limit")
                        process.terminate()
                        break
            finally:
                try:
                    lines.put_nowait(None)
                except queue.Full:
                    lines.get_nowait()
                    lines.put_nowait(None)

        def read_stderr():
            while line := process.stderr.readline(4096):
                errors.append(line.rstrip())

        self.threads = [threading.Thread(target=f, daemon=True) for f in (read_stdout, read_stderr)]
        for thread in self.threads:
            thread.start()
        try:
            self._response()
            self.call("refresh-client", "-f", "no-output")
        except Exception:
            self.close()
            raise

    def _response(self, timeout=None):
        import time

        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        block = None
        output = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(
                    f"tmux control timeout on {self.transport.host}; command outcome is unknown"
                )
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TransportError(
                    f"tmux control timeout on {self.transport.host}; command outcome is unknown"
                ) from exc
            if line is None:
                detail = "; ".join(self.errors)
                raise TransportError(
                    f"tmux control disconnected on {self.transport.host}: {detail}"
                )
            if block is None:
                if line.startswith("%begin "):
                    block = line.split()[1:3]
                elif line.startswith("%exit"):
                    raise TransportError(f"tmux control exited on {self.transport.host}: {line}")
                continue
            fields = line.split()
            if fields and fields[0] in {"%end", "%error"} and fields[1:3] == block:
                if fields[0] == "%error":
                    raise Error("tmux: " + "\n".join(output))
                return output
            # Notifications occur outside response blocks. Command output may itself start with %.
            size += len(line)
            if size > MAX_RESPONSE:
                raise TransportError("remote control response exceeds size limit")
            output.append(line)

    def call(self, *args, timeout=None):
        if self.process is None or self.process.poll() is not None:
            raise Error(f"tmux control is not connected on {self.transport.host}")
        try:
            self.process.stdin.write(command(*args) + "\n")
            self.process.stdin.flush()
            return self._response(timeout)
        except TransportError:
            self.close()
            raise
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise Error(f"tmux control write failed on {self.transport.host}: {exc}") from exc

    def close(self):
        process = self.process
        if process is None:
            return
        self.process = None
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for thread in self.threads:
            thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
