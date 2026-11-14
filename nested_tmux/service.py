"""Private, bounded JSON RPC. The tmux server itself remains the session store."""

import fcntl
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

from .config import Error
from .runtime import Tree

MAX_REQUEST = 4096


def state_path(name, base=None):
    base = (
        Path(base)
        if base
        else Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/nested-tmux-{os.getuid()}"))
    )
    key = hashlib.sha256(name.encode()).hexdigest()[:16]
    return base.absolute() / f"nt-{key}"


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise Error(f"{path} must be a private directory owned by this user (mode 0700)")
    if len(os.fsencode(path / "rpc.sock")) > 100:
        raise Error(
            "runtime directory is too long for a portable Unix socket path; use --runtime-dir /tmp/..."
        )


def request(state, op, **fields):
    payload = json.dumps({"op": op, **fields}).encode() + b"\n"
    if len(payload) > MAX_REQUEST:
        raise Error("RPC request too large")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(120 if op == "reconcile" else 30)
        client.connect(str(Path(state) / "rpc.sock"))
        client.sendall(payload)
        with client.makefile("rb") as stream:
            raw = stream.readline(1024 * 1024)
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise Error("controller returned an invalid response") from exc
        if "error" in result:
            raise Error(result["error"])
        return result["result"]


def serve(config, state, ssh_config=None):
    private_directory(state.parent)
    private_directory(state)
    lock = (state / "controller.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise Error("a controller is already starting or running for this workspace") from exc
    tree = Tree(config, state, ssh_config)
    server = socket.socket(socket.AF_UNIX)
    path = state / "rpc.sock"
    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        tree.prepare()
        path.unlink(missing_ok=True)  # Exclusive lock proves no live controller owns it.
        server.bind(str(path))
        path.chmod(0o600)
        server.listen(16)
        server.settimeout(0.5)
        next_reap = time.monotonic() + 0.5
        while running and not tree.closed:
            if time.monotonic() >= next_reap:
                tree.reap_exited()
                next_reap = time.monotonic() + 0.5
                if tree.closed:
                    break
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            with client:
                client.settimeout(2)
                try:
                    with client.makefile("rb") as stream:
                        line = stream.readline(MAX_REQUEST + 1)
                    if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                        raise Error("invalid or oversized RPC request")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise Error("RPC request must be an object")
                    op = message.get("op")
                    if op == "status":
                        result = tree.status()
                    elif op == "nav":
                        result = tree.navigate(message.get("action"))
                    elif op == "reconcile":
                        tree.prepare()
                        result = tree.status()
                    elif op == "add":
                        result = tree.add(message.get("parent"), message.get("node"))
                    elif op == "remove":
                        result = tree.remove(message.get("path"))
                    elif op == "rename":
                        result = tree.rename(message.get("path"), message.get("title"))
                    elif op == "dump":
                        result = tree.dump()
                    elif op == "stop":
                        running, result = False, {"stopped": True}
                    elif op == "down":
                        tree.down()
                        running, result = False, {"stopped": True}
                    else:
                        raise Error("unknown RPC operation")
                    response = {"result": result}
                except (Error, ValueError, OSError, TypeError, subprocess.TimeoutExpired) as exc:
                    response = {"error": str(exc)}
                try:
                    client.sendall(json.dumps(response).encode() + b"\n")
                except OSError:
                    pass
    finally:
        server.close()
        path.unlink(missing_ok=True)
        tree.close()
        lock.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
