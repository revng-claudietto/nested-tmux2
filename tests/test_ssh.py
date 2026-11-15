import os
import shlex
import socket
import subprocess
import time
from unittest.mock import patch

from helpers import eventually, fixture
from ssh_server import SSHServer
from test_service import ServiceCase

from nested_tmux.control import Transport, clean_env
from nested_tmux.service import request
from nested_tmux.ssh import SSHMaster


class SSHTests(ServiceCase):
    def server(self, **options):
        ssh_directory = self.directory / "ssh"
        ssh_directory.mkdir(mode=0o700)
        server = SSHServer(ssh_directory, **options)
        self.addCleanup(server.close)
        return server

    def test_remote_exit_closes_only_its_branch(self):
        server = self.server()
        data = fixture()
        data["root"]["children"][1] = {
            "id": "remote",
            "host": "nt-test-host",
            "children": [{"id": "shell", "command": ["cat"]}],
        }
        self.launch(data, server.client_config)
        local_pid = self.tmux("work", "display-message", "-p", "#{pane_pid}")
        terminal = self.attach_pty()
        os.write(terminal, b"\x1b[1;3C\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/remote")
        # Wait for the remote visual connection before sending EOF to cat.
        os.write(terminal, b"REMOTE_READY\r")
        eventually(lambda: "REMOTE_READY" in self.tmux("work/remote", "capture-pane", "-p"))
        os.write(terminal, b"\x04")
        eventually(lambda: len(request(self.state, "status")["rows"]) == 1)
        self.assertEqual(request(self.state, "status")["active"], "work")
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{pane_pid}"), local_pid)
        self.assertEqual(
            [n["id"] for n in request(self.state, "dump")["root"]["children"]], ["shell"]
        )

    def test_remote_branch_shortcut_and_dump_live_cwd(self):
        server = self.server()
        self.launch(ssh_config=server.client_config)
        terminal = self.attach_pty()
        os.write(terminal, b"\x01\x12")  # Ctrl+a, Ctrl+r
        eventually(lambda: b"New row id" in self.output)
        os.write(terminal, b"remote\r")
        eventually(lambda: b"SSH host alias" in self.output)
        os.write(terminal, b"nt-test-host\r")
        eventually(lambda: b"Remote cwd" in self.output)
        os.write(terminal, (str(self.directory) + "\r").encode())
        eventually(lambda: request(self.state, "status")["active"] == "work/remote")
        self.status = request(self.state, "status")
        time.sleep(0.2)  # Let the popup close before sending shell input.
        cwd = self.directory / "remote cwd"
        cwd.mkdir()
        os.write(terminal, ("cd " + shlex.quote(str(cwd)) + "\r").encode())
        eventually(
            lambda: (
                self.tmux("work/remote", "display-message", "-p", "#{pane_current_path}")
                == str(cwd)
            )
        )
        remote = request(self.state, "dump")["root"]["children"][-1]
        self.assertEqual(remote["host"], "nt-test-host")
        self.assertEqual(remote["children"][0]["cwd"], str(cwd))
        self.assertEqual(remote["children"][0]["id"], "shell")
        # Disconnect its control stream: export still contains the remote tree,
        # with the configured launch cwd as fallback instead of guessing.
        server.pause()
        self.addCleanup(server.resume)
        SSHMaster(self.state, "nt-test-host", server.client_config).stop()
        eventually(lambda: request(self.state, "status")["errors"])
        unavailable = request(self.state, "dump")["root"]["children"][-1]
        self.assertEqual(unavailable["host"], "nt-test-host")
        self.assertEqual(
            unavailable["children"][0].get("cwd", unavailable["cwd"]), str(self.directory)
        )

    def test_runtime_remote_row_add_dump_and_remove(self):
        server = self.server()
        self.launch(ssh_config=server.client_config)
        self.status = request(
            self.state,
            "add",
            parent="work",
            node={
                "id": "remote",
                "host": "nt-test-host",
                "cwd": str(self.directory),
                "children": [{"id": "shell", "command": ["cat"]}],
            },
        )
        self.status = request(
            self.state, "add", parent="work/remote", node={"id": "job", "command": ["cat"]}
        )
        eventually(
            lambda: self.tmux("work/remote", "display-message", "-p", "#{window_name}") == "cat"
        )
        self.assertEqual(
            self.tmux("work/remote", "display-message", "-p", "#{pane_current_path}"),
            str(self.directory),
        )
        exported = request(self.state, "dump")["root"]["children"][-1]
        self.assertEqual(exported["host"], "nt-test-host")
        self.assertEqual(exported["children"][-1]["command"], ["cat"])
        self.assertNotIn("title", exported["children"][-1])
        self.status = request(self.state, "remove", path="work/remote")
        self.assertNotIn("work/remote", [row["path"] for row in self.status["rows"]])

    def test_remote_tree_keyboard_and_reconnect_preserve_processes(self):
        server = self.server()
        data = fixture()
        data["root"]["children"][1]["host"] = "nt-test-host"
        self.launch(data, server.client_config)
        terminal = self.attach_pty()
        os.write(terminal, b"\x1b[1;3C\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project")
        os.write(terminal, b"REMOTE_INPUT\r")
        eventually(lambda: "REMOTE_INPUT" in self.tmux("work/project", "capture-pane", "-p"))
        before = self.tmux("work/project", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        os.write(terminal, b"\x1b[1;3C\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project/build")
        os.write(terminal, b"\x1b[1;3B\x1b[1;3B")
        eventually(lambda: request(self.state, "status")["active"] == "work")
        # Sever both control and terminal clients remotely. Both recover without up.
        self.tmux("work/project", "detach-client", "-a")
        eventually(
            lambda: (
                "1"
                in self.tmux(
                    "work/project", "list-clients", "-F", "#{client_control_mode}"
                ).splitlines()
            )
        )
        eventually(lambda: not request(self.state, "status")["errors"])
        self.assertEqual(
            before, self.tmux("work/project", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        )
        eventually(lambda: "edit" in self.tmux("work", "capture-pane", "-p"))
        os.write(terminal, b"\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project")

    def test_shared_connection_recovers_after_network_loss(self):
        server = self.server()
        baseline = server.authentications
        data = fixture()
        data["root"]["children"][1]["host"] = "nt-test-host"
        data["root"]["children"].append(
            {
                "id": "other",
                "host": "nt-test-host",
                "children": [{"id": "shell", "command": ["cat"]}],
            }
        )
        self.launch(data, server.client_config)
        terminal = self.attach_pty()
        remote = [row["path"] for row in self.status["rows"] if row["host"] != "local"]
        before = {
            path: self.tmux(path, "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
            for path in remote
        }
        self.assertEqual(server.authentications, baseline + 1)
        master = SSHMaster(self.state, "nt-test-host", server.client_config)
        self.assertEqual(master.path.stat().st_mode & 0o777, 0o600)
        # Navigation sends commands through the existing channel, without auth.
        os.write(terminal, b"\x1b[1;3C\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project")
        os.write(terminal, b"\x1b[1;3C\x1b[1;3D")
        eventually(
            lambda: self.tmux("work/project", "display-message", "-p", "#{window_name}") == "edit"
        )
        self.assertEqual(server.authentications, baseline + 1)
        server.pause()
        self.addCleanup(server.resume)
        master.stop()
        eventually(lambda: request(self.state, "status")["errors"])
        # Local navigation remains usable during background recovery.
        os.write(terminal, b"\x1b[1;3B")
        eventually(lambda: request(self.state, "status")["active"] == "work")
        self.assertEqual(len(request(self.state, "status")["rows"]), 4)
        server.resume()
        eventually(lambda: not request(self.state, "status")["errors"], timeout=20)
        self.assertEqual(server.authentications, baseline + 2)
        for path in remote:
            self.assertEqual(
                before[path], self.tmux(path, "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
            )
        os.write(terminal, b"\x1b[1;3A")
        eventually(lambda: request(self.state, "status")["active"] == "work/project")
        eventually(lambda: "edit" in self.tmux("work", "capture-pane", "-p"))
        os.write(terminal, b"RECOVERED_INPUT\r")
        eventually(lambda: "RECOVERED_INPUT" in self.tmux("work/project", "capture-pane", "-p"))
        # Migrate an old attachment command without restarting remote terminals.
        wid = self.status["rows"][0]["windows"]["work/project"]
        self.tmux("work", "set-option", "-w", "-t", wid, "@nt-bridge", "old")
        request(self.state, "reconcile")
        self.assertEqual(
            before["work/project"],
            self.tmux("work/project", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"),
        )
        request(self.state, "remove", path="work/other")
        self.assertEqual(server.authentications, baseline + 2)
        # The detached master also outlives the Python controller itself.
        from nested_tmux.cli import start

        request(self.state, "stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = start(self.config, self.config_path, self.state, server.client_config)
        self.assertEqual(server.authentications, baseline + 2)
        self.assertEqual(
            before["work/project"],
            self.tmux("work/project", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"),
        )

    def test_reconnect_does_not_recreate_a_missing_remote_server(self):
        server = self.server()
        data = fixture()
        data["root"]["children"][1] = {
            "id": "remote",
            "host": "nt-test-host",
            "children": [{"id": "shell", "command": ["cat"]}],
        }
        self.launch(data, server.client_config)
        local = self.tmux("work", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        self.tmux("work/remote", "kill-server")
        eventually(lambda: request(self.state, "status")["errors"])
        time.sleep(2)  # Let at least one automatic attach attempt finish.
        self.assertEqual(len(request(self.state, "status")["rows"]), 2)
        with self.assertRaises(subprocess.CalledProcessError):
            self.tmux("work/remote", "has-session", "-t", "tree")
        self.assertEqual(
            local, self.tmux("work", "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
        )
        # Explicit up may recreate it and must clear the stale connection error.
        request(self.state, "reconcile")
        eventually(lambda: not request(self.state, "status")["errors"])
        self.tmux("work/remote", "has-session", "-t", "tree")

    def test_inherited_ssh_forwarding_and_local_hooks_are_disabled(self):
        server = self.server(allow_agent_forwarding=True)
        agent_socket = self.directory / "agent.sock"
        agent = subprocess.Popen(
            ["ssh-agent", "-D", "-a", str(agent_socket)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def stop_agent():
            agent.terminate()
            agent.wait(timeout=5)

        self.addCleanup(stop_agent)
        eventually(agent_socket.exists)
        env = {**clean_env(), "SSH_AUTH_SOCK": str(agent_socket)}
        marker = self.directory / "local-hook-ran"
        master_socket = self.directory / "shared-master"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            forwarded_port = probe.getsockname()[1]
        with server.client_config.open("a") as config:
            config.write(f"""  ForwardAgent yes
  ForwardX11 yes
  PermitLocalCommand yes
  LocalCommand touch {marker}
  ControlMaster auto
  ControlPersist yes
  ControlPath {master_socket}
  LocalForward 127.0.0.1:{forwarded_port} 127.0.0.1:22
""")
        # Positive control: this agent really can be forwarded through this sshd.
        probe = subprocess.run(
            [
                "ssh",
                "-F",
                str(server.client_config),
                "-o",
                "BatchMode=yes",
                "-o",
                "PermitLocalCommand=no",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "ControlPath=none",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPersist=no",
                "nt-test-host",
                "printenv SSH_AUTH_SOCK",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertTrue(probe.stdout.strip())
        # Establish an unrelated permissive master first. The application must
        # never inherit that master's agent, even when ssh_config selects it.
        foreign = [
            "ssh",
            "-F",
            str(server.client_config),
            "-S",
            str(master_socket),
            "-oPermitLocalCommand=no",
            "-oClearAllForwardings=yes",
        ]
        subprocess.run(foreign + ["-N", "-f", "nt-test-host"], env=env, check=True, timeout=10)
        self.addCleanup(
            lambda: subprocess.run(
                foreign + ["-O", "exit", "nt-test-host"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        )
        master = SSHMaster(self.directory, "nt-test-host", server.client_config)
        self.addCleanup(master.stop)
        with patch.dict(os.environ, {"SSH_AUTH_SOCK": str(agent_socket)}):
            master.ensure()
        self.assertTrue(master_socket.exists())
        self.assertNotEqual(master.path, master_socket)
        transport = Transport("nt-test-host", server.client_config, master)
        args = transport.wrap(
            ["sh", "-c", 'printf "agent=%s display=%s\\n" "$SSH_AUTH_SOCK" "$DISPLAY"; sleep 2']
        )
        process = subprocess.Popen(
            args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "agent= display=")
            self.assertFalse(marker.exists())
            self.assertTrue(master_socket.exists())
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", forwarded_port)), 0)
            self.assertEqual(process.wait(timeout=10), 0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        effective = subprocess.run(
            [args[0], "-G", *args[1:]],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        self.assertIn("forwardx11 no\n", effective)
        self.assertIn("forwardagent no\n", effective)

    def test_remote_escape_sequences_cannot_write_local_clipboard_or_run_commands(self):
        server = self.server()
        data = fixture()
        data["root"]["children"] = [data["root"]["children"][1]]
        data["root"]["children"][0]["host"] = "nt-test-host"
        self.launch(data, server.client_config)
        self.attach_pty()
        marker = self.directory / "remote-command-ran-locally"
        # A remote tmux server may be compromised and enable its own passthrough.
        self.tmux("work/project", "set-option", "-g", "allow-passthrough", "on")
        self.tmux("work/project", "set-option", "-s", "set-clipboard", "on")
        hostile = (
            "\033]52;c;SE9TVElMRQ==\a\033Ptmux;\033\033]52;c;SE9TVElMRQ==\a\033\\"
            "\033Ptmux;\033\033kFORGED_NAME\033\033\\\033\\"
        )
        payload = f"printf '%s' {shlex.quote(hostile)}; printf '%s\\n' {shlex.quote('$(touch ' + str(marker) + ')')}; sleep 30"
        self.tmux("work/project", "respawn-pane", "-k", "-t", "tree:", payload)
        eventually(lambda: "$(touch" in self.tmux("work", "capture-pane", "-p"))
        self.assertFalse(marker.exists())
        self.assertNotIn(b"SE9TVElMRQ==", self.output)
        self.assertEqual(self.tmux("work", "show-options", "-sv", "set-clipboard"), "off")
        self.assertEqual(self.tmux("work", "show-options", "-gv", "allow-passthrough"), "off")
        self.assertEqual(self.tmux("work", "show-options", "-gv", "allow-rename"), "off")
        self.assertEqual(self.tmux("work", "display-message", "-p", "#{window_name}"), "project")
        self.assertEqual(request(self.state, "status")["active"], "work")
