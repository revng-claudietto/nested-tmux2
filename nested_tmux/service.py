"""Private, bounded JSON RPC. The tmux server itself remains the session store."""

import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

from .config import Error
from .inventory import Inventory, prefix, run, socket_directory, terminate
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
        client.settimeout(120 if op in {"reconcile", "kill"} else 30)
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


def owner_state(name, fallback):
    """A live local root may be owned through a different runtime directory."""
    rows, _ = Inventory(name, fallback).scan({"local"})
    for row in rows:
        if not row.root:
            continue
        code, value, _ = run(
            [
                "tmux",
                "-N",
                "-L",
                row.socket,
                "-f",
                "/dev/null",
                "show-options",
                "-gqv",
                "@nt2-state",
            ]
        )
        if code == 0 and value.strip():
            candidate = Path(value.strip())
            if not candidate.is_absolute():
                continue
            try:
                result = request(candidate, "status")
                if result.get("name") == name:
                    return candidate
            except (OSError, Error):
                pass
    return fallback


def serve(config, state, ssh_config=None, size=None):
    private_directory(state.parent)
    private_directory(state)
    directory = socket_directory()
    private_directory(directory)
    lock_path = directory / (prefix(config.name) + "controller.lock")
    lock = os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+")
    # stop removes its RPC listener before finishing control-channel cleanup.
    # An immediate up must wait for that owner to release the lock.
    lock_deadline = time.monotonic() + 15
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if time.monotonic() >= lock_deadline:
                lock.close()
                raise Error(
                    "a controller is already starting or running for this workspace"
                ) from exc
            time.sleep(0.05)
    tree = Tree(config, state, ssh_config)
    server = socket.socket(socket.AF_UNIX)
    path = state / "rpc.sock"
    running = True

    def stop(signum, frame):
        nonlocal running
        running = False

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        tree.prepare(size=size)
        tree.root.control.call("set-option", "-g", "@nt2-state", str(state))
        path.unlink(missing_ok=True)  # Exclusive lock proves no live controller owns it.
        server.bind(str(path))
        path.chmod(0o600)
        server.listen(16)
        server.settimeout(0.5)
        next_reap = time.monotonic() + 0.5
        while running and not tree.closed:
            if time.monotonic() >= next_reap:
                tree.reconnect()
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
                keyboard = False
                try:
                    with client.makefile("rb") as stream:
                        line = stream.readline(MAX_REQUEST + 1)
                    if len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                        raise Error("invalid or oversized RPC request")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise Error("RPC request must be an object")
                    op = message.get("op")
                    keyboard = op == "key"
                    if op == "status":
                        result = tree.status()
                    elif op in {"nav", "key"}:
                        result = tree.navigate(message.get("action"))
                    elif op == "reconcile":
                        tree.prepare(size=message.get("size"))
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
                    elif op == "kill":
                        hosts = message.get("hosts", ["local"])
                        if not isinstance(hosts, list) or any(
                            not isinstance(h, str)
                            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", h)
                            for h in hosts
                        ):
                            raise Error("invalid discovery hosts")
                        result = terminate(
                            Inventory(config.name, state, ssh_config, tree.masters),
                            hosts,
                            path=message.get("path"),
                            host=message.get("host"),
                            all_rows=message.get("all", False),
                            recursive=message.get("recursive", True),
                            local=message.get("local", False),
                            tree=tree,
                        )
                    else:
                        raise Error("unknown RPC operation")
                    response = (
                        b"OK\n" if keyboard else json.dumps({"result": result}).encode() + b"\n"
                    )
                except (Error, ValueError, OSError, TypeError, subprocess.TimeoutExpired) as exc:
                    if keyboard:
                        # Small ASCII frames keep the native client free of JSON parsing.
                        # Remote diagnostics must never become terminal escape sequences.
                        detail = "".join(c if 32 <= ord(c) < 127 else "?" for c in str(exc))[:1024]
                        response = ("ERR " + (detail or "operation failed") + "\n").encode()
                    else:
                        response = json.dumps({"error": str(exc)}).encode() + b"\n"
                try:
                    client.sendall(response)
                except OSError:
                    pass
    finally:
        server.close()
        path.unlink(missing_ok=True)
        tree.close()
        lock.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
