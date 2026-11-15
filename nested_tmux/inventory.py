"""Discover managed servers without starting them or trusting remote routes."""

import hashlib
import os
import re
import select
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Error, workspace_name
from .ssh import SSHMaster, Transport, clean_env

PATH_RE = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,32}")
LEGACY_RE = re.compile(r"nt-[0-9a-f]{16}-(?:root|[0-9a-f]{16})")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def prefix(workspace):
    workspace_name(workspace)
    return f"nested-tmux2-{workspace[:12]}-{digest(workspace)}-"


def socket_name(workspace, path):
    if not isinstance(path, str) or not PATH_RE.fullmatch(path):
        raise Error("invalid row path")
    return prefix(workspace) + digest(path)


def socket_directory():
    return Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"


def run(args, timeout=15):
    """Collect bounded output from local commands or an untrusted SSH peer."""
    process = subprocess.Popen(
        args,
        env=clean_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    streams = list(buffers)
    deadline = time.monotonic() + timeout
    try:
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Error("row operation timed out")
            ready, _, _ = select.select(streams, [], [], min(remaining, 0.1))
            for stream in ready:
                block = os.read(stream.fileno(), 8192)
                if not block:
                    streams.remove(stream)
                else:
                    buffers[stream].extend(block)
                    if sum(map(len, buffers.values())) > 1024 * 1024:
                        raise Error("row operation output exceeds size limit")
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return (process.returncode, *(buffers[s].decode(errors="replace") for s in buffers))
    except BaseException:
        # The shell's tmux query may still be alive after the shell exits.
        # This process group contains only this operation's owned children.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in buffers:
            stream.close()


# The prefix is an argv value, never interpolated shell code. No command here
# creates a server. Legacy sockets are accepted only with explicit v2 metadata.
SCAN = r"""
command -v tmux >/dev/null 2>&1 || { echo 'tmux is not installed' >&2; exit 127; }
directory="${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)"
for socket in "$directory"/"$1"* "$directory"/nt-*; do
    [ -S "$socket" ] || continue
    name=${socket##*/}
    metadata=$(tmux -N -L "$name" -f /dev/null display-message -p -t tree: \
        '#{@nt2-version}|#{@nt2-workspace}|#{@nt2-row}|#{@nt2-root}' 2>/dev/null) || continue
    printf '%s|%s\n' "$name" "$metadata"
done
"""


@dataclass(frozen=True)
class Record:
    host: str
    path: str
    socket: str
    root: bool = False


class Inventory:
    def __init__(self, workspace, state, ssh_config=None, masters=None):
        self.workspace = workspace_name(workspace)
        self.state = state
        self.ssh_config = ssh_config
        self.masters = masters if masters is not None else {}

    def transport(self, host):
        master = None
        if host != "local":
            if host not in self.masters:
                self.masters[host] = SSHMaster(self.state, host, self.ssh_config)
            master = self.masters[host]
            master.ensure()
        return Transport(host, self.ssh_config, master)

    def decode(self, host, output):
        records = []
        for line in output.splitlines():
            fields = line.split("|")
            if len(fields) != 5:
                continue
            name, version, workspace, path, root = fields
            if version != "1" or workspace != self.workspace:
                continue
            if not PATH_RE.fullmatch(path) or root not in {"0", "1"}:
                raise Error(f"invalid row metadata on {host}")
            modern = name == socket_name(workspace, path)
            legacy = LEGACY_RE.fullmatch(name) and name.rsplit("-", 1)[-1] == (
                "root" if root == "1" else digest(path)
            )
            if not (modern or legacy) or (host != "local" and root == "1"):
                raise Error(f"row identity does not match socket on {host}")
            records.append(Record(host, path, name, root == "1"))
        return sorted(set(records), key=lambda row: (row.host, row.path, row.socket))

    def scan(self, hosts):
        rows, errors = [], []
        for host in sorted(set(hosts), key=lambda h: (h != "local", h)):
            try:
                code, output, detail = run(
                    self.transport(host).wrap(
                        ["sh", "-c", SCAN, "nested-tmux2-discover", prefix(self.workspace)]
                    )
                )
                if code:
                    raise Error(detail.strip() or f"discovery exited with status {code}")
                rows.extend(self.decode(host, output))
            except (Error, OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{host}: {exc}")
        return rows, errors

    def kill(self, row):
        # Records are always scoped to the queried host. Never use a peer's
        # socket path, host field, or executable as a local command argument.
        code, _, detail = run(
            self.transport(row.host).wrap(
                ["tmux", "-N", "-L", row.socket, "-f", "/dev/null", "kill-server"]
            )
        )
        if code and not any(s in detail for s in ("no server running", "No such file")):
            raise Error(detail.strip() or f"kill exited with status {code}")


def hosts_for(config, local=False):
    return {"local"} if local else {"local", *(n.host for n in config.root.walk())}


def select_rows(rows, path=None, host=None, all_rows=False, recursive=True):
    if all_rows:
        return rows
    if not isinstance(path, str) or not PATH_RE.fullmatch(path):
        raise Error("specify a full row ID path, or --all")
    matches = [r for r in rows if r.path == path and (host is None or r.host == host)]
    if not matches:
        raise Error(f"no running row named {path}")
    if len(matches) != 1:
        raise Error(f"row {path} is ambiguous; select its host with --host")
    anchor = matches[0]
    # A remote subtree cannot return to local or change host. In particular,
    # a forged remote ancestor must never broaden a kill into local rows.
    return [
        r
        for r in rows
        if r == anchor
        or (
            recursive
            and r.path.startswith(path + "/")
            and (anchor.host == "local" or r.host == anchor.host)
        )
    ]


def stop_masters(state, masters):
    """Close only local mux sockets owned by this workspace, without connecting."""
    errors = []
    try:
        info = Path(state).lstat()
    except FileNotFoundError:
        return errors
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return [f"unsafe SSH master directory: {state}"]
    for path in Path(state).glob("s-*"):
        if not re.fullmatch(r"s-[0-9a-f]{16}", path.name):
            continue
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode):
            continue
        master = next((m for m in masters.values() if m.path == path), None)
        master = master or SSHMaster(state, "unused-host")
        master.path = path
        try:
            master.stop()
        except (Error, OSError) as exc:
            errors.append(str(exc))
    return errors


def terminate(
    inventory,
    hosts,
    *,
    path=None,
    host=None,
    all_rows=False,
    recursive=True,
    tree=None,
    local=False,
):
    rows, errors = inventory.scan({"local"} if local else hosts)
    cleanup_states = {inventory.state}
    if all_rows:
        # A stopped controller may have left masters in another runtime
        # directory. Only a locally queried root can supply this cleanup path.
        for row in rows:
            if row.host == "local" and row.root:
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
                candidate = Path(value.strip())
                if code == 0 and candidate.is_absolute() and candidate.name == inventory.state.name:
                    cleanup_states.add(candidate)
    try:
        selected = select_rows(rows, path, host, all_rows, recursive)
    except Error as exc:
        raise Error("; ".join([str(exc), *errors])) from exc
    killed = []
    for row in sorted(selected, key=lambda r: (r.path.count("/"), r.host, r.path)):
        try:
            inventory.kill(row)
            killed.append(row)
        except (Error, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{row.host}:{row.path}: {exc}")
    if tree is not None:
        try:
            tree.forget(killed, local=local)
        except (Error, OSError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
    if all_rows:
        if tree is not None:
            tree.closed = True
            tree.close()
        for state in cleanup_states:
            errors.extend(stop_masters(state, inventory.masters))
    return {"killed": [{"host": r.host, "path": r.path} for r in killed], "errors": errors}
