"""A tree of tmux rows. Only the outer row interprets navigation keys."""

import base64
import hashlib
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, Error, Node, as_data, parse
from .control import Control, Transport, TransportError, clean_env
from .ssh import SSHMaster


def launcher(*args):
    # Nix's entry-point wrapper adds dependencies to sys.path, not PYTHONPATH.
    # Carry those paths into the controller and tmux's key-binding helpers.
    paths = [str(Path(path).resolve()) for path in sys.path if path]
    return [
        sys.executable,
        str(Path(__file__).with_name("launcher.py")),
        json.dumps(paths),
        *map(str, args),
    ]


@dataclass
class Row:
    node: Node
    control: Control
    windows: dict[str, str] = field(default_factory=dict)


class Tree:
    def __init__(self, config: Config, state: Path, ssh_config=None):
        self.config = config
        self.state = state
        self.ssh_config = ssh_config
        try:
            machine = Path("/etc/machine-id").read_text().strip()
        except OSError:
            machine = socket.gethostname()
        self.namespace = hashlib.sha256(f"{machine}:{state}".encode()).hexdigest()[:16]
        self.rows = {}
        self.masters = {}
        self._recoveries = {}
        self._retry = {}
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="nt-reconnect")
        self._install_config(config)
        self.active = config.root.path
        self.errors = {}
        self.closed = False

    def _install_config(self, config):
        self._metadata(config)  # Reject oversized edits before changing live state.
        previous = self.rows
        self.config = config
        self.rows = {}
        self.parents = {}
        for node in config.root.walk():
            for child in node.children:
                self.parents[child.path] = node.path
            if node.branch:
                ident = (
                    "root"
                    if node is config.root
                    else hashlib.sha256(node.path.encode()).hexdigest()[:16]
                )
                master = None
                if node.host != "local":
                    if node.host not in self.masters:
                        self.masters[node.host] = SSHMaster(self.state, node.host, self.ssh_config)
                    master = self.masters[node.host]
                control = Control(
                    Transport(node.host, self.ssh_config, master), f"nt-{self.namespace}-{ident}"
                )
                old = previous.get(node.path)
                if old:
                    old.node = node
                    self.rows[node.path] = old
                else:
                    self.rows[node.path] = Row(node, control)

    @property
    def root(self):
        return self.rows[self.config.root.path]

    def recover(self, create=True):
        # The root server owns the live tree. YAML is used only for a fresh root.
        self.root.control.connect(create=create)
        saved = self.root.control.call("show-options", "-gqv", "@nt-tree")
        if saved:
            try:
                config = parse(json.loads(base64.b64decode(saved[0], validate=True)))
            except (ValueError, Error) as exc:
                raise Error(f"invalid live tree metadata: {exc}") from exc
            old_root = self.root
            self._install_config(config)
            if self.root is not old_root:
                self.root.control.close()
                self.root.control = old_root.control

    def prepare(self, size=None):
        if size is not None and (
            not isinstance(size, (tuple, list))
            or len(size) != 2
            or any(type(d) is not int or not 1 <= d <= 10000 for d in size)
        ):
            raise Error("size must contain two integers between 1 and 10000")
        self.recover()
        self._save_metadata()
        if size is not None:
            clients = self.root.control.call("list-clients", "-F", "#{client_control_mode}")
            # An attached terminal remains authoritative. Before first attach,
            # seed all root windows from the launching tty, less its status row.
            if "0" not in clients:
                self._seed_size(self.root, (size[0], max(1, size[1] - 1)), all_windows=True)
        root_size = self._root_size()
        for row in self.rows.values():
            if row is not self.root:
                row.control.connect()
            self._seed_size(row, root_size)
        for row in reversed(list(self.rows.values())):
            self._prepare_row(row)
        self._bindings()
        self.active = self.config.root.path
        self._styles()

    def _root_size(self):
        value = self.root.control.call(
            "display-message", "-p", "-t", "tree:", "#{window_width} #{window_height}"
        )[0]
        return tuple(map(int, value.split()))

    def _seed_size(self, row, root_size, all_windows=False):
        width, height = root_size
        height = max(1, height - row.node.path.count("/"))
        ctl = row.control
        ctl.call("set-option", "-t", "tree", "default-size", f"{width}x{height}")
        for line in ctl.call("list-windows", "-F", "#{window_id} #{@nt-ready}"):
            wid, _, ready = line.partition(" ")
            if ready == "1" and not all_windows:
                continue
            # Only placeholder panes are normally resized here. Restore tmux's
            # automatic sizing so real tty changes still propagate after attach.
            ctl.call("resize-window", "-t", wid, "-x", str(width), "-y", str(height))
            ctl.call("set-option", "-w", "-t", wid, "window-size", "latest")

    @staticmethod
    def _metadata(config):
        encoded = base64.b64encode(
            json.dumps(as_data(config), separators=(",", ":")).encode()
        ).decode()
        if len(encoded) > 60000:
            raise Error("live tree metadata exceeds 60 KB; use smaller workspace trees")
        return encoded

    def _save_metadata(self):
        self.root.control.call("set-option", "-g", "@nt-tree", self._metadata(self.config))

    def _prepare_row(self, row):
        ctl = row.control
        for name, value in {
            # Keep an exited pane only until the controller removes it from the
            # live tree. This also makes very short startup commands observable.
            "remain-on-exit": "on",
            "remain-on-exit-format": "",
            "base-index": "0",
            "renumber-windows": "on",
            "automatic-rename": "off",
            "allow-rename": "off",
            "allow-passthrough": "off",
            "set-titles": "off",
            "prefix": "None",
            "prefix2": "None",
            "mouse": "off",
            "status-left": (
                f"[{self.config.name}] "
                if row is self.root and self.config.name != "default"
                else ""
            ),
            "status-left-length": "40",
            "status-right": "",
            "status-style": "bg=colour237,fg=colour255",
            "window-status-format": " #I #W ",
            "window-status-current-format": " #I #W ",
        }.items():
            ctl.call("set-option", "-g", name, value)
        ctl.call("set-option", "-s", "escape-time", "10")
        ctl.call("set-option", "-s", "set-clipboard", "off")
        # Older supported tmux versions do not offer clipboard reads.
        server_options = ctl.call("show-options", "-s")
        if any(line.startswith("get-clipboard ") for line in server_options):
            ctl.call("set-option", "-s", "get-clipboard", "off")
        for table in ("root", "prefix"):
            # tmux removes empty tables; unbinding an absent table is an error.
            ctl.call("bind-key", "-T", table, "F12", "display-message", "")
            ctl.call("unbind-key", "-a", "-T", table)
        existing = {}
        bootstrap = []
        for line in ctl.call("list-windows", "-F", "#{window_id}\t#{@nt-node}\t#{window_name}"):
            if line.count("\t") < 2:
                raise Error(f"unexpected list-windows response on {row.node.host}: {line!r}")
            wid, path, title = line.split("\t", 2)
            if path:
                if path in existing:
                    raise Error(f"duplicate managed window {path}; resolve it before starting")
                existing[path] = wid
            elif title == "__bootstrap__":
                bootstrap.append(wid)
        for child in row.node.children:
            title = child.title or child.path.rsplit("/", 1)[-1]
            wid = existing.get(child.path)
            if wid is None:
                if bootstrap:
                    wid = bootstrap.pop(0)
                    ctl.call("rename-window", "-t", wid, title.replace("#", "##"))
                else:
                    wid = ctl.call(
                        "new-window",
                        "-d",
                        "-P",
                        "-F",
                        "#{window_id}",
                        "-n",
                        title.replace("#", "##"),
                        "-c",
                        row.node.cwd if child.branch else child.cwd,
                        "sleep 2147483647",
                    )[0]
                ctl.call("set-option", "-w", "-t", wid, "@nt-node", child.path)
            row.windows[child.path] = wid
            ready = ctl.call("show-options", "-wqv", "-t", wid, "@nt-ready")
            bridge = self._bridge(row, self.rows[child.path]) if child.branch else None
            bridge_hash = hashlib.sha256(bridge.encode()).hexdigest() if bridge else None
            previous_bridge = (
                ctl.call("show-options", "-wqv", "-t", wid, "@nt-bridge") if bridge else []
            )
            # Updating an attachment never reruns a terminal's launch command.
            if ready == ["1"] and (not bridge or previous_bridge == [bridge_hash]):
                self._window_name(row, child, wid)
                continue
            if ready == ["starting"]:
                raise Error(
                    f"{child.path}: interrupted startup has an unknown outcome; inspect the existing terminal before explicitly using down to recreate it"
                )
            args = [
                "respawn-pane",
                "-k",
                "-t",
                wid + ".0",
                "-c",
                row.node.cwd if child.branch else child.cwd,
            ]
            for key, value in child.env.items():
                args += ["-e", f"{key}={value}"]
            if child.branch:
                args += [bridge]
            elif child.argv:
                args += ["exec " + shlex.join(child.argv)]
            elif child.shell is not None:
                args += ["exec /bin/sh -c " + shlex.quote(child.shell)]
            else:
                shell = ctl.call("show-options", "-gv", "default-shell")[0]
                args += ["exec " + shlex.quote(shell) + " -l"]
            ctl.call("set-option", "-w", "-t", wid, "@nt-ready", "starting")
            try:
                ctl.call(*args)
            except TransportError:
                # A lost acknowledgement is not permission to replay a command.
                raise
            except Error:
                ctl.call("set-option", "-wu", "-t", wid, "@nt-ready")
                raise
            if bridge:
                ctl.call("set-option", "-w", "-t", wid, "@nt-bridge", bridge_hash)
            ctl.call("set-option", "-w", "-t", wid, "@nt-ready", "1")
            self._window_name(row, child, wid)
        if bootstrap:
            ctl.call("select-window", "-t", row.windows[row.node.children[0].path])
            for wid in bootstrap:
                ctl.call("kill-window", "-t", wid)
        # Also repair numbering in a workspace created by an older controller.
        ctl.call("move-window", "-r", "-t", "tree:")

    @staticmethod
    def _window_name(row, child, wid):
        title = child.title or child.path.rsplit("/", 1)[-1]
        # Native rename-window also disables automatic naming for this tab.
        row.control.call("rename-window", "-t", wid, title.replace("#", "##"))

    def _bridge(self, parent, child):
        args = [
            "env",
            "-u",
            "TMUX",
            "-u",
            "TMUX_PANE",
            "tmux",
            "-u",
            "-L",
            child.control.socket_name,
            "attach-session",
            "-t",
            "tree",
        ]
        if child.node.host != parent.node.host:
            args = child.control.transport.wrap(args, terminal=True)
        # This pane is only an attachment. Losing it never kills the child server.
        return "while :; do " + shlex.join(args) + "; sleep 1; done"

    def _bindings(self):
        ctl = self.root.control
        helper = shutil.which("nested-tmux-key")

        def key_script(action):
            args = (
                [str(Path(helper).resolve()), str(self.state / "rpc.sock"), action]
                if helper
                else launcher("_rpc", self.state, action)
            )
            return shlex.join(args).replace("#", "##")

        ctl.call("set-option", "-g", "prefix", "C-a")
        for key, action in {
            "M-Up": "up",
            "M-Down": "down",
            "M-Left": "left",
            "M-Right": "right",
            "C-t": "new",
        }.items():
            ctl.call("bind-key", "-T", "root", key, "run-shell", key_script(action))
        for key, action in {"C-c": "new", "c": "new", "C-a": "last", "[": "copy"}.items():
            ctl.call("bind-key", "-T", "prefix", key, "run-shell", key_script(action))
        ctl.call("bind-key", "-T", "prefix", "d", "detach-client")
        ctl.call("bind-key", "-T", "prefix", "a", "send-prefix")
        for key, operation in {"C-s": "row", "C-r": "ssh-row", "A": "rename"}.items():
            script = shlex.join(launcher("_prompt", self.state, operation)).replace("#", "##")
            ctl.call(
                "bind-key",
                "-T",
                "prefix",
                key,
                "display-popup",
                "-E",
                "-w",
                "70%",
                "-h",
                "8",
                script,
            )
        remove = key_script("remove")
        from .control import command

        ctl.call(
            "bind-key",
            "-T",
            "prefix",
            "x",
            "confirm-before",
            "-p",
            "Close this terminal or entire child row? (y/n)",
            command("run-shell", remove),
        )

    def _styles(self, paths=None):
        for path, row in self.rows.items():
            if paths is not None and path not in paths:
                continue
            try:
                row.control.call(
                    "set-option",
                    "-g",
                    "window-status-current-style",
                    "bg=colour34" if path == self.active else "bg=colour102",
                    timeout=2,
                )
                self.errors.pop(path, None)
            except Error as exc:
                # Returning to a parent must remain possible when a remote host disappears.
                self.errors[path] = str(exc)

    def navigate(self, action):
        previous = self.active
        row = self.rows[self.active]
        ctl = row.control
        if action == "down":
            self.active = self.parents.get(self.active, self.active)
        elif action == "up":
            current = ctl.call("display-message", "-p", "-t", "tree:", "#{window_id}")[0]
            for path, wid in row.windows.items():
                if current == wid and path in self.rows:
                    self.rows[path].control.call("has-session", "-t", "tree")
                    self.active = path
                    break
        elif action in {"left", "right", "last"}:
            ctl.call(
                {"left": "previous-window", "right": "next-window", "last": "last-window"}[action],
                "-t",
                "tree",
            )
        elif action == "new":
            used = {child.path.rsplit("/", 1)[-1] for child in row.node.children}
            ident, number = "shell", 2
            while ident in used:
                ident, number = f"shell-{number}", number + 1
            return self.add(self.active, {"id": ident})
        elif action == "remove":
            return self.remove(self.current_path())
        elif action == "copy":
            ctl.call("copy-mode", "-t", "tree:")
        else:
            raise Error(f"unknown navigation action: {action}")
        if previous != self.active:
            self._styles({previous, self.active})
        return self.status()

    def current_path(self):
        row = self.rows[self.active]
        current = row.control.call("display-message", "-p", "-t", "tree:", "#{window_id}")[0]
        for path, wid in row.windows.items():
            if wid == current:
                return path
        raise Error("the current window is not managed by nested-tmux")

    def _focus(self, path):
        # CLI edits can target a currently hidden branch. Reveal its full route.
        child = path
        while child in self.parents:
            parent = self.rows[self.parents[child]]
            parent.control.call("select-window", "-t", parent.windows[child])
            child = parent.node.path
        self.active = path

    @staticmethod
    def _find(data, path):
        parts = path.split("/")
        node = data["root"]
        if node["id"] != parts.pop(0):
            raise Error(f"unknown tree path: {path}")
        for part in parts:
            matches = [child for child in node.get("children", []) if child["id"] == part]
            if not matches:
                raise Error(f"unknown tree path: {path}")
            node = matches[0]
        return node

    def add(self, parent, node):
        parent = parent or self.active
        if parent not in self.rows:
            raise Error(f"not a row: {parent}")
        data = as_data(self.config)
        self._find(data, parent)["children"].append(node)
        config = parse(data)  # Validate before tmux changes or shell execution.
        # This operation will reveal the destination. Do so before launching
        # anything: tmux defers resizing hidden windows until they are selected.
        self._focus(parent)
        root_size = self._root_size()
        expected = (root_size[0], max(1, root_size[1] - parent.count("/")))
        deadline = time.monotonic() + 5
        while True:
            value = self.rows[parent].control.call(
                "display-message",
                "-p",
                "-t",
                "tree:",
                "#{window_width} #{window_height}",
                timeout=2,
            )[0]
            if tuple(map(int, value.split())) == expected:
                break
            if time.monotonic() >= deadline:
                raise Error(
                    f"terminal size has not reached {parent}; retry once its display connects"
                )
            time.sleep(0.02)
        self._install_config(config)
        self._save_metadata()
        path = parent + "/" + node["id"]
        for candidate, row in reversed(list(self.rows.items())):
            if candidate == path or candidate.startswith(path + "/"):
                row.control.connect()
                self._seed_size(row, root_size)
                self._prepare_row(row)
        self._seed_size(self.rows[parent], root_size)
        self._prepare_row(self.rows[parent])
        self._bindings()
        self.rows[parent].control.call("select-window", "-t", self.rows[parent].windows[path])
        self._focus(path if path in self.rows else parent)
        self._styles()
        return self.status()

    def _kill_row(self, row, graceful=False):
        if row.control.transport.master:
            row.control.transport.master.ensure()
        result = subprocess.run(
            row.control.transport.wrap(
                [
                    "tmux",
                    "-L",
                    row.control.socket_name,
                    *(["kill-session", "-t", "tree"] if graceful else ["kill-server"]),
                ]
            ),
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode and not any(
            text in result.stderr for text in ("no server running", "No such file or directory")
        ):
            raise Error(f"could not remove {row.node.path}: {result.stderr.strip()}")
        row.control.close()

    def remove(self, path):
        if path == self.config.root.path:
            raise Error("use down to close the entire workspace")
        parent = self.parents.get(path)
        if not parent:
            raise Error(f"unknown tree path: {path}")
        # Removing the last terminal also removes its now-empty ancestor rows.
        while len(self.rows[parent].node.children) == 1:
            path = parent
            if path == self.config.root.path:
                self.down()
                return {"stopped": True}
            parent = self.parents[path]
        data = as_data(self.config)
        container = self._find(data, parent)
        container["children"] = [
            child for child in container["children"] if child["id"] != path.rsplit("/", 1)[-1]
        ]
        config = parse(data)
        # Stop attachment loops before descendants. Never use an implicit socket.
        for candidate, row in list(self.rows.items()):
            if candidate == path or candidate.startswith(path + "/"):
                self._kill_row(row)
        wid = self.rows[parent].windows.get(path)
        if wid:
            self.rows[parent].control.call("kill-window", "-t", wid)
            del self.rows[parent].windows[path]
        if self.active == path or self.active.startswith(path + "/"):
            self.active = parent
        self._install_config(config)
        self._save_metadata()
        self._styles()
        return self.status()

    def reconnect(self):
        """Attach to surviving remote servers, never recreate or replay launches.

        Workers own only their candidate Control. Tree edits and installation
        of successful connections stay on the controller's main thread.
        """
        for path, (row, original, future) in list(self._recoveries.items()):
            if not future.done():
                continue
            del self._recoveries[path]
            try:
                candidate = future.result()
            except (Error, OSError, subprocess.TimeoutExpired) as exc:
                if self.rows.get(path) is row and row.control is original:
                    self.errors[path] = str(exc)
                    delay = min(self._retry.get(path, (0, 0.5))[1] * 2, 30)
                    self._retry[path] = (time.monotonic() + delay, delay)
            else:
                if (
                    self.rows.get(path) is row
                    and row.control is original
                    and not original.connected
                ):
                    original.close()
                    row.control = candidate
                    self._retry.pop(path, None)
                    self.errors.pop(path, None)
                    self._styles({path})
                else:
                    candidate.close()
        now = time.monotonic()
        for path, row in self.rows.items():
            if (
                row.node.host == "local"
                or row.control.connected
                or path in self._recoveries
                or now < self._retry.get(path, (0, 0))[0]
            ):
                continue

            def attach(original):
                candidate = Control(original.transport, original.socket_name, timeout=3)
                try:
                    candidate.connect(create=False)
                except Exception:
                    candidate.close()
                    raise
                # Only the attach handshake has a short recovery deadline.
                candidate.timeout = original.timeout
                return candidate

            self._recoveries[path] = (row, row.control, self._workers.submit(attach, row.control))

    def reap_exited(self):
        """Observe leaf exits through control replies, including over SSH.

        An unreachable server is not evidence of exit. Remote window IDs are
        resolved only within that row's locally owned map, never as tree paths.
        """
        exited = []
        for path, row in list(self.rows.items()):
            try:
                panes = row.control.call(
                    "list-panes",
                    "-s",
                    "-t",
                    "tree",
                    "-F",
                    "#{window_id}\t#{pane_dead}\t#{@nt-ready}",
                    timeout=2,
                )
                self.errors.pop(path, None)
                dead = {
                    fields[0]
                    for line in panes
                    if len(fields := line.split("\t")) == 3 and fields[1:] == ["1", "1"]
                }
                exited.extend(
                    child.path
                    for child in row.node.children
                    if not child.branch and row.windows.get(child.path) in dead
                )
            except Error as exc:
                self.errors[path] = str(exc)
        for path in exited:
            if self.closed:
                break
            if path in self.parents:
                try:
                    self.remove(path)
                except (Error, OSError, subprocess.TimeoutExpired) as exc:
                    self.errors[path] = str(exc)

    def rename(self, path, title):
        path = path or self.current_path()
        if path == self.config.root.path:
            raise Error("rename a terminal or child row")
        data = as_data(self.config)
        self._find(data, path)["title"] = title
        config = parse(data)
        self._metadata(config)
        row = self.rows[self.parents[path]]
        row.control.call("rename-window", "-t", row.windows[path], title.replace("#", "##"))
        self._install_config(config)
        self._save_metadata()
        return self.status()

    def dump(self):
        data = as_data(self.config)
        for node in self.config.root.walk():
            if node.branch:
                continue
            row = self.rows[self.parents[node.path]]
            try:
                cwd = row.control.call(
                    "display-message", "-p", "-t", row.windows[node.path], "#{pane_current_path}"
                )[0]
                # tmux 3.4's printable output inserts a slash before shell variables.
                cwd = re.sub(r"\\(?=\$[A-Za-z_{])", "", cwd)
                if (
                    cwd.startswith("/")
                    and not any(ord(char) < 32 for char in cwd)
                    and "#" not in cwd
                ):
                    self._find(data, node.path)["cwd"] = cwd
            except (Error, KeyError, IndexError):
                pass  # Keep launch cwd for unavailable or removed terminals.
        return data

    def status(self):
        return {
            "name": self.config.name,
            "digest": self.config.digest,
            "active": self.active,
            "errors": self.errors,
            "rows": [
                {
                    "path": path,
                    "host": row.node.host,
                    "socket": row.control.socket_name,
                    "windows": row.windows,
                }
                for path, row in self.rows.items()
            ],
        }

    def down(self):
        failures = []
        # Stop attachment loops before their child servers.
        for row in self.rows.values():
            try:
                # Normal session termination lets attached terminals exit cleanly.
                self._kill_row(row, graceful=row is self.root)
            except Error as exc:
                failures.append(f"{row.node.path}: {exc}")
        if failures:
            raise Error("could not stop all rows: " + "; ".join(failures))
        self.closed = True

    def close(self):
        self._workers.shutdown(wait=True, cancel_futures=True)
        for _, _, future in self._recoveries.values():
            if not future.cancelled():
                try:
                    future.result().close()
                except (Error, OSError, subprocess.TimeoutExpired):
                    pass
        self._recoveries.clear()
        for row in self.rows.values():
            row.control.close()
