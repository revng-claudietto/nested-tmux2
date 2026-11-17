import fcntl
import json
import os
import pty
import select
import shlex
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
        # Runtime-added/removed hosts may no longer appear in the initial YAML.
        # Stop only master sockets inside this test's private workspace.
        from nested_tmux.ssh import SSHMaster

        for path in self.state.glob("s-*"):
            if path.is_socket():
                master = SSHMaster(self.state, "nt-test-host", self.ssh_config)
                master.path = path
                master.stop()

    def tmux(self, path, *args):
        from nested_tmux.control import Transport
        from nested_tmux.ssh import SSHMaster

        row = next(row for row in self.status["rows"] if row["path"] == path)
        master = (
            SSHMaster(self.state, row["host"], self.ssh_config) if row["host"] != "local" else None
        )
        result = subprocess.run(
            Transport(row["host"], self.ssh_config, master).wrap(
                ["tmux", "-L", row["socket"], *args]
            ),
            env=clean_env(),
            text=True,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return result.stdout.strip()

    def attach_pty(self, command=None, environment=None, cwd=None, size=(140, 40)):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
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
        self.attachment = proc
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
    def test_new_tabs_inherit_live_cwd_and_keep_shell_label(self):
        self.launch(
            {
                "name": "test",
                "root": {
                    "id": "work",
                    "cwd": str(self.directory),
                    "children": [{"id": "shell"}],
                },
            }
        )
        terminal = self.attach_pty()
        cwd = self.directory / "tab cwd with ' quote"
        cwd.mkdir()
        os.write(terminal, ("cd " + shlex.quote(str(cwd)) + "\r").encode())
        eventually(
            lambda: self.tmux("work", "display-message", "-p", "#{pane_current_path}") == str(cwd)
        )
        for count, keys in enumerate((b"\x14", b"\x01c"), start=2):
            os.write(terminal, keys)  # Ctrl+t and Ctrl+a, c
            eventually(lambda: len(request(self.state, "status")["rows"][0]["windows"]) == count)
            self.assertEqual(
                self.tmux("work", "display-message", "-p", "#{pane_current_path}"), str(cwd)
            )
            self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "shell")
        children = request(self.state, "dump")["root"]["children"]
        self.assertEqual([child["id"] for child in children], ["shell", "shell-2", "shell-3"])
        self.assertEqual([child["title"] for child in children[1:]], ["shell", "shell"])
        # The CLI's add-terminal RPC inherits too, unless --cwd was supplied.
        request(self.state, "add", node={"id": "inherited", "command": ["cat"]})
        self.assertEqual(
            self.tmux("work", "display-message", "-p", "#{pane_current_path}"), str(cwd)
        )
        request(
            self.state,
            "add",
            node={"id": "explicit", "cwd": str(self.directory), "command": ["cat"]},
        )
        self.assertEqual(
            self.tmux("work", "display-message", "-p", "#{pane_current_path}"), str(self.directory)
        )

    def test_discovered_recipe_start_save_and_live_tree_restart(self):
        from nested_tmux.runtime import launcher

        config_home = self.directory / "config"
        recipe = config_home / "nested-tmux2" / "work.yaml"
        recipe.parent.mkdir(parents=True)
        recipe.write_text(
            yaml.safe_dump(
                {
                    "root": {
                        "id": "desk",
                        "cwd": str(self.directory),
                        "children": [
                            {"id": "job", "command": ["cat"]},
                        ],
                    }
                }
            )
        )
        self.config = load(recipe, name="work")
        self.state = state_path("work", self.directory)
        self.ssh_config = None
        self.addCleanup(self.cleanup_service)
        binary = os.environ.get("NESTED_TMUX_TEST_BINARY")
        command = [binary] if binary else launcher()

        def cli(*args):
            result = subprocess.run(
                [*command, *args, "--name", "work", "--runtime-dir", str(self.directory)],
                env={**clean_env(), "XDG_CONFIG_HOME": str(config_home)},
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.status = json.loads(cli("up"))
        self.assertEqual(self.status["rows"][0]["path"], "desk")
        self.assertEqual(
            self.tmux("desk", "display-message", "-p", "#{pane_current_path}"), str(self.directory)
        )
        self.assertEqual(
            self.tmux("desk", "display-message", "-p", "#{pane_current_command}"), "cat"
        )
        cli("add-terminal", "extra", "--command", "cat")
        before = self.tmux("desk", "list-panes", "-a", "-F", "#{pane_pid}")
        self.assertEqual(cli("save"), f"Saved {recipe}\n")
        self.assertEqual(load(recipe).name, "work")
        self.assertEqual(len(load(recipe).root.children), 2)
        cli("stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = json.loads(cli("up"))
        self.assertEqual(self.tmux("desk", "list-panes", "-a", "-F", "#{pane_pid}"), before)
        cli("down")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = json.loads(cli("up"))
        self.assertEqual(len(self.status["rows"][0]["windows"]), 2)

    def test_id_default_titles_and_manual_rename_survive_restart(self):
        data = fixture()
        data["root"]["children"][0] = {"id": "shell"}
        self.launch(data)
        terminal = self.attach_pty()
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "shell")
        self.assertNotIn("title", request(self.state, "dump")["root"]["children"][0])
        # Existing automatic program names migrate without restarting the shell.
        before = self.tmux("work", "display-message", "-p", "#{pane_pid}")
        self.tmux("work", "rename-window", "bash")
        self.tmux("work", "set-option", "-w", "automatic-rename", "on")
        request(self.state, "reconcile")
        eventually(lambda: self.tmux("work", "display-message", "-p", "#{window_name}") == "shell")
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{pane_pid}"), before)
        os.write(terminal, b"sleep 30\r")
        eventually(
            lambda: self.tmux("work", "display-message", "-p", "#{pane_current_command}") == "sleep"
        )
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "shell")
        self.assertEqual(self.tmux("work", "show-options", "-wv", "automatic-rename"), "off")
        os.write(terminal, b"\x03")
        request(self.state, "stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = start(self.config, self.config_path, self.state, None)
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "shell")
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{pane_pid}"), before)
        # A manual name equal to the node ID must still remain explicit in YAML.
        os.write(terminal, b"\x01A")
        eventually(lambda: b"New title:" in self.output)
        os.write(terminal, b"shell\r")
        eventually(
            lambda: request(self.state, "dump")["root"]["children"][0].get("title") == "shell"
        )
        time.sleep(0.2)  # Allow the popup helper to exit before typing again.
        os.write(terminal, b"sleep 30\r")
        eventually(
            lambda: self.tmux("work", "display-message", "-p", "#{pane_current_command}") == "sleep"
        )
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "shell")
        request(self.state, "stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = start(self.config, self.config_path, self.state, None)
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "shell")
        self.assertEqual(self.tmux("work", "show-options", "-wv", "automatic-rename"), "off")
        self.assertEqual(request(self.state, "dump")["root"]["children"][0]["title"], "shell")
        self.assertEqual(
            self.tmux("work", "display-message", "-p", "-t", "tree:1", "#{window_name}"), "project"
        )

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
        eventually(
            lambda: (
                self.tmux("work", "list-windows", "-F", "#{window_index}:#{window_name}")
                == "0:shell"
            )
        )
        self.assertEqual(self.tmux("work", "show-options", "-gv", "status-left"), "")
        os.write(terminal, b"\x14")
        eventually(lambda: len(request(self.state, "status")["rows"][0]["windows"]) == 2)
        eventually(
            lambda: (
                self.tmux("work", "list-windows", "-F", "#{window_index}:#{window_name}")
                == "0:shell\n1:shell"
            )
        )

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

    def test_shell_exit_prunes_empty_rows_and_last_terminal_ends_workspace(self):
        self.launch(
            {
                "name": "exit-test",
                "root": {
                    "id": "work",
                    "children": [
                        {"id": "shell"},
                        {
                            "id": "project",
                            "children": [
                                {"id": "tools", "children": [{"id": "shell"}]},
                            ],
                        },
                    ],
                },
            }
        )
        terminal = self.attach_pty()
        root_pid = self.tmux("work", "display-message", "-p", "#{pane_pid}")
        # Go into two nested rows, then exit their only shell from the real PTY.
        os.write(terminal, b"\x1b[1;3C\x1b[1;3A\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project/tools")
        os.write(terminal, b"exit\r")
        eventually(lambda: len(request(self.state, "status")["rows"]) == 1)
        self.assertEqual(request(self.state, "status")["active"], "work")
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{pane_pid}"), root_pid)
        self.assertEqual(
            [n["id"] for n in request(self.state, "dump")["root"]["children"]], ["shell"]
        )
        # Exited branches do not come back on controller restart.
        request(self.state, "stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = start(self.config, self.config_path, self.state, None)
        self.assertEqual(len(self.status["rows"]), 1)
        os.write(terminal, b"exit\r")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        eventually(lambda: self.attachment.poll() is not None)
        self.assertEqual(self.attachment.returncode, 0)

    def test_named_workspaces_are_independent_without_yaml(self):
        from nested_tmux.runtime import launcher

        binary = os.environ.get("NESTED_TMUX_TEST_BINARY")
        command = [binary] if binary else launcher()

        def cli(*args):
            result = subprocess.run(
                [*command, *args, "--runtime-dir", str(self.directory)],
                cwd=self.directory,
                env=clean_env(),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        for name in ("alpha", "beta"):
            self.addCleanup(
                subprocess.run,
                [*command, "down", "--name", name, "--runtime-dir", str(self.directory)],
                env=clean_env(),
                capture_output=True,
                timeout=30,
            )
        alpha = json.loads(cli("up", "--name", "alpha"))
        beta = json.loads(cli("--name", "beta", "up"))
        self.assertNotEqual(alpha["rows"][0]["socket"], beta["rows"][0]["socket"])
        cli("add-terminal", "job", "--name", "alpha", "--command", "cat")
        cli("rename", "--name", "alpha", "work/job", "Job")
        cli("nav", "--name", "alpha", "new")
        saved = yaml.safe_load(cli("dump", "--name", "alpha"))
        self.assertEqual(saved["name"], "alpha")
        self.assertEqual([n["id"] for n in saved["root"]["children"]], ["shell", "job", "shell-2"])
        self.assertEqual(saved["root"]["children"][1]["title"], "Job")
        self.assertEqual(json.loads(cli("status", "--name", "beta")), beta)
        cli("stop", "--name", "alpha")
        eventually(lambda: not (state_path("alpha", self.directory) / "rpc.sock").exists())
        resumed = json.loads(cli("up", "--name", "alpha"))
        self.assertEqual(len(resumed["rows"][0]["windows"]), 3)
        cli("down", "--name", "alpha")
        self.assertEqual(json.loads(cli("status", "--name", "beta")), beta)
        terminal = self.attach_pty(
            [*command, "--name", "beta"],
            {"XDG_RUNTIME_DIR": str(self.directory)},
            self.directory,
        )
        eventually(lambda: b"[beta]" in self.output)
        os.write(terminal, b"\x01d")
        eventually(lambda: self.attachment.poll() is not None)
        self.assertEqual(self.attachment.returncode, 0)
        self.assertEqual(json.loads(cli("status", "--name", "beta"))["name"], "beta")

    def test_name_overrides_yaml_name_and_is_exported(self):
        import contextlib
        import io

        from nested_tmux.cli import main

        self.launch()
        # Select the live YAML workspace using just its name.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["dump", "--name", self.config.name, "--runtime-dir", str(self.directory)]), 0
            )
        self.assertEqual(yaml.safe_load(output.getvalue())["name"], self.config.name)
        data = fixture()
        data["name"] = "unused-recipe-name"
        self.config_path.write_text(yaml.safe_dump(data))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "up",
                        str(self.config_path),
                        "--name",
                        self.config.name,
                        "--runtime-dir",
                        str(self.directory),
                    ]
                ),
                0,
            )
        self.assertEqual(json.loads(output.getvalue())["rows"], self.status["rows"])

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
        from nested_tmux.config import as_data, parse

        restored = Tree(parse({**as_data(export), "name": "copy"}), self.directory / "copy")
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

    def test_keyboard_acknowledgement_and_errors(self):
        self.launch()
        for action, expected in [("right", b"OK\n"), ("unknown\u001b[31m", b"ERR ")]:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.connect(str(self.state / "rpc.sock"))
                sock.sendall(json.dumps({"op": "key", "action": action}).encode() + b"\n")
                with sock.makefile("rb") as stream:
                    response = stream.readline(4096)
                self.assertTrue(response.startswith(expected), response)
                self.assertNotIn(b"\x1b", response)
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "project")
