import fcntl
import json
import os
import pty
import select
import socket
import struct
import subprocess
import termios
import threading
import time

import yaml
from helpers import TreeCase, eventually, fixture

from nested_tmux.cli import start
from nested_tmux.config import Error, load, parse
from nested_tmux.control import clean_env
from nested_tmux.service import request, state_path


class ServiceCase(TreeCase):
    def launch(self, data=None, ssh_config=None):
        self.config_path = self.directory / "workspace.yaml"
        self.config_path.write_text(yaml.safe_dump(data or fixture()))
        self.config = load(self.config_path)
        self.state = state_path(self.config.name, self.directory)
        self.ssh_config = ssh_config
        self.addCleanup(self.cleanup_service)
        self.status = start(self.config, self.config_path, self.state, ssh_config)
        return self.status

    def cleanup_service(self):
        try:
            request(self.state, "down")
        except (OSError, Error):
            pass
        # Also covers stopped/crashed controllers and partially built trees.
        from nested_tmux.runtime import Tree

        tree = Tree(self.config, self.state, self.ssh_config)
        try:
            tree.recover(create=False)
        except Error:
            pass
        for row in tree.rows.values():
            subprocess.run(
                row.control.transport.wrap(["tmux", "-L", row.control.socket_name, "kill-server"]),
                env=clean_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        tree.close()

    def tmux(self, path, *args):
        from nested_tmux.control import Transport

        row = next(row for row in self.status["rows"] if row["path"] == path)
        result = subprocess.run(
            Transport(row["host"], self.ssh_config).wrap(["tmux", "-L", row["socket"], *args]),
            env=clean_env(),
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return result.stdout.strip()

    def attach_pty(self, command=None, environment=None, cwd=None):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        env = {**clean_env(), "TERM": "xterm-256color", **(environment or {})}
        proc = subprocess.Popen(
            command
            or ["tmux", "-L", self.status["rows"][0]["socket"], "attach-session", "-t", "tree"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
        os.close(slave)
        self.output = bytearray()
        done = threading.Event()

        def drain():
            while not done.is_set():
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        self.output.extend(os.read(master, 65536))
                    except OSError:
                        return

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()

        def close():
            proc.terminate()
            proc.wait(timeout=5)
            done.set()
            thread.join(timeout=2)
            os.close(master)

        self.addCleanup(close)
        eventually(lambda: len(self.output) > 0)
        return master


class ServiceTests(ServiceCase):
    def test_no_yaml_start_edit_export_and_restart(self):
        from nested_tmux.runtime import launcher

        self.config = parse(
            {
                "name": "default",
                "root": {
                    "id": "work",
                    "cwd": str(self.directory),
                    "children": [{"id": "shell"}],
                },
            }
        )
        self.state = state_path("default", self.directory)
        self.ssh_config = None
        self.addCleanup(self.cleanup_service)
        binary = os.environ.get("NESTED_TMUX_TEST_BINARY")
        command = [binary] if binary else launcher()
        terminal = self.attach_pty(
            command, {"XDG_RUNTIME_DIR": str(self.directory)}, self.directory
        )
        eventually(lambda: (self.state / "rpc.sock").exists())
        self.status = request(self.state, "status")
        self.assertEqual(
            self.tmux("work", "display-message", "-p", "#{pane_current_path}"), str(self.directory)
        )
        os.write(terminal, b"\x14")
        eventually(lambda: len(request(self.state, "status")["rows"][0]["windows"]) == 2)

        def cli(*args):
            result = subprocess.run(
                [*command, *args, "--runtime-dir", str(self.directory)],
                cwd=self.directory,
                env=clean_env(),
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.status = json.loads(cli("add-row", "project", "--parent", "work"))
        cli("add-terminal", "job", "--parent", "work/project", "--command", "cat")
        cli("rename", "work/project/job", "Job")
        saved = self.directory / "saved.yaml"
        cli("dump", "-o", str(saved))
        self.assertEqual(load(saved).root.children[-1].children[-1].title, "Job")
        cli("nav", "down")
        cli("remove", "work/project")
        before = self.tmux("work", "list-panes", "-a", "-F", "#{pane_pid}")
        cli("stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        cli("up")
        self.assertEqual(before, self.tmux("work", "list-panes", "-a", "-F", "#{pane_pid}"))

    def test_original_creation_keys_branch_prompt_and_removal(self):
        self.launch()
        terminal = self.attach_pty()
        os.write(terminal, b"\x14")  # Ctrl+t
        eventually(
            lambda: len(self.tmux("work", "list-windows", "-F", "#{window_id}").splitlines()) == 3
        )
        os.write(terminal, b"\x01\x03")  # Ctrl+a, Ctrl+c
        eventually(
            lambda: len(self.tmux("work", "list-windows", "-F", "#{window_id}").splitlines()) == 4
        )
        os.write(terminal, b"\x01\x13")  # Ctrl+a, Ctrl+s
        eventually(lambda: b"New row id" in self.output)
        os.write(terminal, b"scratch\r")
        eventually(lambda: request(self.state, "status")["active"] == "work/scratch")
        # RPC finishes before the popup process exits and the terminal redraws.
        time.sleep(0.2)
        os.write(terminal, b"\x1b[1;3B")
        eventually(lambda: request(self.state, "status")["active"] == "work")
        os.write(terminal, b"\x01x")
        eventually(lambda: b"Close this terminal" in self.output)
        os.write(terminal, b"y")
        eventually(
            lambda: all(
                row["path"] != "work/scratch" for row in request(self.state, "status")["rows"]
            )
        )

    def test_runtime_edit_dump_restart_and_bootstrap_from_export(self):
        import contextlib
        import io
        import shlex

        from nested_tmux.cli import main
        from nested_tmux.runtime import Tree

        self.launch()
        self.status = request(
            self.state,
            "add",
            parent="work",
            node={"id": "scratch", "children": [{"id": "shell", "command": ["cat"]}]},
        )
        cwd = self.directory / "runtime cwd"
        cwd.mkdir()
        launch = "cd " + shlex.quote(str(cwd)) + "; exec cat"
        self.status = request(
            self.state, "add", parent="work/scratch", node={"id": "job", "command": launch}
        )
        request(self.state, "rename", path="work/scratch/job", title="Runtime job")
        eventually(
            lambda: (
                self.tmux("work/scratch", "display-message", "-p", "#{pane_current_path}")
                == str(cwd)
            )
        )
        dumped = self.directory / "export.yaml"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "dump",
                        str(self.config_path),
                        "--runtime-dir",
                        str(self.directory),
                        "-o",
                        str(dumped),
                    ]
                ),
                0,
            )
        export = load(dumped)
        scratch = export.root.children[-1]
        self.assertEqual(scratch.path, "work/scratch")
        self.assertEqual(scratch.children[-1].cwd, str(cwd))
        self.assertEqual(scratch.children[-1].shell, launch)
        self.assertEqual(scratch.children[-1].title, "Runtime job")
        before = self.tmux("work/scratch", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        request(self.state, "stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = start(self.config, self.config_path, self.state, None)
        self.assertEqual(
            before, self.tmux("work/scratch", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        )
        restored = Tree(export, self.directory / "copy")
        try:
            restored.prepare()
            self.assertIn("work/scratch", restored.rows)
            self.assertEqual(len(restored.rows["work/scratch"].windows), 2)
        finally:
            restored.down()
            restored.close()
        self.status = request(self.state, "remove", path="work/scratch/job")
        self.assertEqual(
            len(next(r for r in self.status["rows"] if r["path"] == "work/scratch")["windows"]), 1
        )
        self.status = request(self.state, "remove", path="work/scratch")
        self.assertNotIn("scratch", self.tmux("work", "list-windows", "-F", "#{window_name}"))

    def test_editing_hidden_row_reveals_ancestors(self):
        self.launch()
        self.status = request(
            self.state, "add", parent="work/project/build", node={"id": "new", "command": ["cat"]}
        )
        self.assertEqual(self.status["active"], "work/project/build")
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "project")
        self.assertEqual(
            self.tmux("work/project", "display-message", "-p", "#{window_name}"), "build"
        )

    def test_real_keyboard_navigation_and_visible_nested_rows(self):
        self.launch()
        terminal = self.attach_pty()
        os.write(terminal, b"\x1b[1;3C")  # Alt+Right, from a real PTY, never send-keys.
        eventually(
            lambda: self.tmux("work", "display-message", "-p", "#{window_name}") == "project"
        )
        os.write(terminal, b"\x1b[1;3D")  # Alt+Left
        eventually(lambda: self.tmux("work", "display-message", "-p", "#{window_name}") == "shell")
        os.write(terminal, b"\x01\x01")  # Ctrl+a, Ctrl+a: last tab.
        eventually(
            lambda: self.tmux("work", "display-message", "-p", "#{window_name}") == "project"
        )
        os.write(terminal, b"\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project")
        os.write(terminal, b"\x1b[1;3C")
        eventually(
            lambda: self.tmux("work/project", "display-message", "-p", "#{window_name}") == "build"
        )
        os.write(terminal, b"\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project/build")
        # The outer screen contains both inner row bars as actual terminal content.
        eventually(
            lambda: (
                "one" in self.tmux("work", "capture-pane", "-p")
                and "edit" in self.tmux("work", "capture-pane", "-p")
            )
        )
        os.write(terminal, b"HELLO_LEAF\r")
        eventually(lambda: "HELLO_LEAF" in self.tmux("work/project/build", "capture-pane", "-p"))
        os.write(terminal, b"\x1b[1;3B")
        eventually(lambda: request(self.state, "status")["active"] == "work/project")
        self.assertEqual(
            self.tmux("work/project", "show-options", "-gv", "window-status-current-style"),
            "bg=colour34",
        )
        self.assertEqual(
            self.tmux("work", "show-options", "-gv", "window-status-current-style"), "bg=colour102"
        )

    def test_stop_restart_and_recreate_missing_server(self):
        self.launch()
        before = self.tmux("work/project", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        request(self.state, "stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = start(self.config, self.config_path, self.state, None)
        self.assertEqual(
            before, self.tmux("work/project", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        )
        self.tmux("work/project/build", "kill-server")
        self.status = start(self.config, self.config_path, self.state, None)
        self.assertIn(
            "one", self.tmux("work/project/build", "list-windows", "-F", "#{window_name}")
        )
        self.assertEqual(
            before, self.tmux("work/project", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        )

    def test_yaml_changes_do_not_override_a_live_workspace(self):
        self.launch()
        before = self.tmux("work", "list-panes", "-a", "-F", "#{pane_pid}")
        data = fixture()
        data["root"]["children"][0]["command"] = ["sleep", "12345"]
        self.config_path.write_text(yaml.safe_dump(data))
        start(load(self.config_path), self.config_path, self.state, None)
        self.assertEqual(before, self.tmux("work", "list-panes", "-a", "-F", "#{pane_pid}"))

    def test_rpc_permissions_and_malformed_requests(self):
        self.launch()
        self.assertEqual((self.state / "rpc.sock").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for payload in [
            b"not json\n",
            b"[]\n",
            b'{"op":"exec","command":"false"}\n',
            b"x" * 4097 + b"\n",
        ]:
            with self.subTest(payload=payload[:40]), socket.socket(socket.AF_UNIX) as sock:
                sock.connect(str(self.state / "rpc.sock"))
                sock.sendall(payload)
                response = json.loads(sock.makefile("rb").readline())
                self.assertIn("error", response)
        self.assertEqual(request(self.state, "status")["active"], "work")
