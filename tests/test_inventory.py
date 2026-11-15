import contextlib
import io
import json
import sys
from unittest.mock import patch

from helpers import eventually, fixture
from ssh_server import SSHServer
from test_service import ServiceCase

from nested_tmux.cli import main, start
from nested_tmux.config import Error, as_data, parse
from nested_tmux.inventory import (
    Inventory,
    Record,
    digest,
    run,
    select_rows,
    socket_name,
    terminate,
)
from nested_tmux.runtime import Tree
from nested_tmux.service import request, state_path
from nested_tmux.ssh import SSHMaster


class InventoryTests(ServiceCase):
    def cli(self, operation, *arguments, config=True, expected=0, runtime=None):
        args = [operation]
        if config:
            args.append(str(self.config_path))
        args += ["--runtime-dir", str(runtime or self.directory)]
        if getattr(self, "ssh_config", None):
            args += ["--ssh-config", str(self.ssh_config)]
        output, diagnostic = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
            result = main([*args, *arguments])
        self.assertEqual(result, expected, diagnostic.getvalue() + output.getvalue())
        return output.getvalue(), diagnostic.getvalue()

    def server(self):
        directory = self.directory / "ssh"
        directory.mkdir(mode=0o700)
        server = SSHServer(directory)
        self.addCleanup(server.close)
        return server

    def remote(self):
        server = self.server()
        data = fixture()
        data["root"]["children"][1]["host"] = "nt-test-host"
        self.launch(data, server.client_config)
        return server

    def test_list_orphans_without_controller_and_workspace_isolation(self):
        self.launch()
        self.status = request(
            self.state,
            "add",
            parent="work",
            node={
                "id": "scratch",
                "children": [{"id": "shell", "command": ["cat"]}],
            },
        )
        before = self.config_path.read_bytes()
        self.cli("stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        other = self.build({**fixture(), "name": "other"})
        output, _ = self.cli("ls")
        self.assertIn("test\tlocal\twork/scratch", output)
        self.assertNotIn("other", output)
        self.assertFalse((self.state / "rpc.sock").exists())
        self.cli("kill", "--all", "--local")
        output, _ = self.cli("ls")
        self.assertEqual(output, "WORKSPACE\tHOST\tROW\n")
        other.root.control.call("has-session", "-t", "tree")
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_nonrecursive_kill_preserves_descendant_server(self):
        self.launch()
        before = self.tmux("work/project/build", "list-panes", "-a", "-F", "#{pane_pid}")
        self.cli("kill", "work/project", "--no-recursive")
        self.assertEqual(
            self.tmux("work/project/build", "list-panes", "-a", "-F", "#{pane_pid}"), before
        )
        self.assertEqual(len(request(self.state, "status")["rows"]), 1)
        output, _ = self.cli("ls")
        self.assertIn("work/project/build", output)
        self.assertNotIn("local\twork/project\n", output)
        self.cli("down", "--local")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.assertEqual(self.cli("ls")[0], "WORKSPACE\tHOST\tROW\n")

    def test_recursive_kill_uses_path_components(self):
        self.launch()
        self.status = request(
            self.state,
            "add",
            parent="work",
            node={
                "id": "project-other",
                "children": [{"id": "shell", "command": ["cat"]}],
            },
        )
        self.cli("kill", "work/project")
        output, _ = self.cli("ls")
        self.assertIn("work/project-other", output)
        self.assertNotIn("work/project/build", output)
        self.assertEqual(len(request(self.state, "status")["rows"]), 2)

    def test_local_named_kill_does_not_query_remote_survivors(self):
        server = self.server()
        data = fixture()
        data["root"]["children"].append(
            {
                "id": "remote",
                "host": "nt-test-host",
                "children": [{"id": "shell", "command": ["cat"]}],
            }
        )
        tree = self.build(data, server.client_config)
        remote = tree.rows["work/remote"].control
        before = remote.call("list-panes", "-a", "-F", "#{pane_pid}")
        inventory = Inventory(tree.config.name, self.directory, server.client_config, tree.masters)
        with patch.object(
            remote, "call", side_effect=AssertionError("remote query during local kill")
        ):
            result = terminate(
                inventory, {"local", "nt-test-host"}, path="work/project", local=True, tree=tree
            )
        self.assertFalse(result["errors"])
        self.assertEqual(remote.call("list-panes", "-a", "-F", "#{pane_pid}"), before)

    def test_local_shutdown_and_remote_reuse_after_runtime_loss(self):
        server = self.remote()
        before = {
            path: self.tmux(path, "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
            for path in ("work/project", "work/project/build")
        }
        original = self.status
        authentications = server.authentications
        with patch.object(SSHMaster, "ensure", side_effect=AssertionError("remote access")):
            self.assertNotIn("nt-test-host", self.cli("ls", "--local")[0])
        self.cli("kill", "--all", "--local")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.assertFalse(any(path.is_socket() for path in self.state.glob("s-*")))
        self.assertEqual(server.authentications, authentications)
        self.assertIn("nt-test-host\twork/project", self.cli("ls")[0])
        # No machine/runtime-directory identity is needed to find the servers.
        new_runtime = self.directory / "new"
        self.state = state_path(self.config.name, new_runtime)
        self.status = start(self.config, self.config_path, self.state, self.ssh_config)
        for path, processes in before.items():
            self.assertEqual(
                self.tmux(path, "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}"), processes
            )
        self.assertEqual(
            [r["socket"] for r in self.status["rows"]], [r["socket"] for r in original["rows"]]
        )
        self.cli("down", runtime=new_runtime)
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.assertEqual(self.cli("ls", runtime=new_runtime)[0], "WORKSPACE\tHOST\tROW\n")

    def test_only_configured_remote_hosts_are_discovered(self):
        self.remote()
        self.status = request(
            self.state,
            "add",
            parent="work",
            node={
                "id": "orphan",
                "host": "nt-test-host",
                "children": [{"id": "shell", "command": ["cat"]}],
            },
        )
        self.assertIn("nt-test-host\twork/orphan", self.cli("ls")[0])
        # A configuration without this host must not query it, even when its
        # live tree still knows that host. --local likewise never uses SSH.
        from nested_tmux.config import dump_yaml

        self.config_path.write_text(dump_yaml(fixture()))
        with patch.object(SSHMaster, "ensure", side_effect=AssertionError("remote access")):
            self.assertNotIn("nt-test-host", self.cli("ls")[0])
        self.cli("kill", "--all")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        # Put the host back into config solely to discover and clean survivors.
        self.config_path.write_text(dump_yaml(as_data(self.config)))
        self.assertIn("nt-test-host\twork/orphan", self.cli("ls")[0])
        self.cli("kill", "--all")

    def test_discovery_failure_reports_partial_results_and_kill_progress(self):
        server = self.remote()
        server.pause()
        self.addCleanup(server.resume)
        SSHMaster(self.state, "nt-test-host", self.ssh_config).stop()
        output, errors = self.cli("ls", expected=1)
        self.assertIn("local\twork", output)
        self.assertIn("nt-test-host", errors)
        output, errors = self.cli("kill", "--all", expected=1)
        self.assertIn('"path": "work"', output)
        self.assertIn("nt-test-host", errors)
        eventually(lambda: not (self.state / "rpc.sock").exists())
        server.resume()
        self.cli("kill", "--all")

    def test_unsaved_local_rows_are_not_recreated(self):
        self.launch()
        self.status = request(
            self.state,
            "add",
            parent="work",
            node={
                "id": "scratch",
                "children": [{"id": "shell", "command": ["cat"]}],
            },
        )
        self.cli("kill", "--all", "--local")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.status = start(self.config, self.config_path, self.state, None)
        self.assertNotIn("scratch", self.cli("ls")[0])

    def test_runtime_directory_cannot_create_second_controller(self):
        self.launch()
        original = self.tmux("work", "list-panes", "-a", "-F", "#{pane_pid}")
        output, _ = self.cli("up", runtime=self.directory / "different")
        self.assertEqual(json.loads(output)["rows"], self.status["rows"])
        self.assertEqual(self.tmux("work", "list-panes", "-a", "-F", "#{pane_pid}"), original)
        self.assertFalse(
            (state_path(self.config.name, self.directory / "different") / "rpc.sock").exists()
        )

    def test_stopped_controller_master_cleanup_from_different_runtime(self):
        self.remote()
        self.cli("stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        self.assertTrue(any(path.is_socket() for path in self.state.glob("s-*")))
        self.cli("kill", "--all", "--local", runtime=self.directory / "different")
        self.assertFalse(any(path.is_socket() for path in self.state.glob("s-*")))
        self.cli("kill", "--all")

    def test_listing_inherits_running_controllers_ssh_configuration(self):
        self.remote()
        config = self.ssh_config
        try:
            self.ssh_config = None
            self.assertIn("nt-test-host\twork/project", self.cli("ls")[0])
        finally:
            self.ssh_config = config

    def test_metadata_cannot_supply_hosts_paths_or_other_workspaces(self):
        inventory = Inventory("test", self.directory)
        name = socket_name("test", "work/remote")
        row = inventory.decode("nt-test-host", f"{name}|1|test|work/remote|0\n")[0]
        self.assertEqual(row.host, "nt-test-host")
        for forged in (
            "/tmp/other|1|test|work/remote|0",
            f"{name}|1|test|work/../../local|0",
            f"{name}|1|test|work/remote|1",
            f"{name}|1|test|work/other|0",
        ):
            with self.subTest(forged=forged), self.assertRaises(Error):
                inventory.decode("nt-test-host", forged)
        self.assertEqual(inventory.decode("nt-test-host", f"{name}|1|other|work/remote|0"), [])
        with self.assertRaises(Error):
            select_rows([row, Record("local", row.path, row.socket)], row.path)
        self.assertEqual(
            select_rows([row, Record("local", row.path, row.socket)], row.path, "nt-test-host"),
            [row],
        )
        local = Record("local", "work/remote/child", socket_name("test", "work/remote/child"))
        self.assertEqual(select_rows([row, local], row.path, "nt-test-host"), [row])

    def test_discovery_output_and_time_are_bounded(self):
        with self.assertRaisesRegex(Error, "output exceeds"):
            run([sys.executable, "-c", "print('x' * (2 * 1024 * 1024))"])
        with self.assertRaisesRegex(Error, "timed out"):
            run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)

    def exercise_legacy(self, remote=False):
        # Build the previous socket naming scheme, then remove the newly added
        # identity tags to simulate an upgrade from a running old controller.
        data = fixture()
        ssh_config = None
        if remote:
            ssh_config = self.server().client_config
            data["root"]["children"][1]["host"] = "nt-test-host"
        config = parse(data)
        tree = Tree(config, self.directory, ssh_config)
        self.tree = tree
        self.addCleanup(self.cleanup_tree)
        for path, row in tree.rows.items():
            suffix = "root" if row is tree.root else digest(path)
            row.control.socket_name = f"nt-{tree.legacy_namespace}-{suffix}"
            row.control.connect()
        tree._save_metadata()
        for row in reversed(list(tree.rows.values())):
            tree._prepare_row(row)
            for key in ("@nt2-version", "@nt2-workspace", "@nt2-row", "@nt2-root"):
                # The remote case simulates an upgrade interrupted just after
                # tagging the local root, before tagging any remote server.
                if not (remote and row is tree.root):
                    row.control.call("set-option", "-gu", key)
        before = {
            p: r.control.call("list-panes", "-a", "-F", "#{pane_pid}") for p, r in tree.rows.items()
        }
        tree.close()
        restored = Tree(config, self.directory, ssh_config)
        self.tree = restored
        restored.prepare()
        for path, pids in before.items():
            self.assertEqual(
                restored.rows[path].control.call("list-panes", "-a", "-F", "#{pane_pid}"), pids
            )
        inventory = Inventory(config.name, self.directory, ssh_config, restored.masters)
        hosts = {row.node.host for row in restored.rows.values()}
        rows, errors = inventory.scan(hosts)
        self.assertFalse(errors)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row.socket.startswith("nt-") for row in rows))
        if remote:
            result = terminate(inventory, hosts, all_rows=True, local=True, tree=restored)
            self.assertFalse(result["errors"])
            resumed = Tree(config, self.directory, ssh_config)
            self.tree = resumed
            resumed.prepare()
            for path, pids in before.items():
                if path != "work":
                    self.assertEqual(
                        resumed.rows[path].control.call("list-panes", "-a", "-F", "#{pane_pid}"),
                        pids,
                    )

    def test_legacy_adoption_preserves_processes(self):
        self.exercise_legacy()

    def test_legacy_remote_rows_survive_local_shutdown(self):
        self.exercise_legacy(remote=True)
