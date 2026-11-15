import os
import re
import signal
import subprocess

from helpers import TreeCase, eventually
from ssh_server import SSHServer

from nested_tmux.config import Error
from nested_tmux.ssh import SSHMaster, Transport, clean_env


class SSHMasterTests(TreeCase):
    def server(self, **options):
        directory = self.directory / "ssh"
        directory.mkdir(mode=0o700)
        server = SSHServer(directory, **options)
        self.addCleanup(server.close)
        return server

    def master(self, server, name="workspace", **options):
        master = SSHMaster(self.directory / name, "nt-test-host", server.client_config, **options)
        self.addCleanup(master.stop)
        master.ensure()
        return master

    def channel(self, master, *args):
        return subprocess.run(
            Transport("nt-test-host", master.transport.ssh_config, master).wrap(list(args)),
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_idle_persistence_and_workspace_isolation(self):
        server = self.server()
        baseline = server.authentications
        first = self.master(server, persist=2)
        connection = self.channel(first, "printenv", "SSH_CONNECTION")
        self.assertEqual(connection.returncode, 0, connection.stderr)
        self.assertEqual(
            self.channel(first, "printenv", "SSH_CONNECTION").stdout, connection.stdout
        )
        self.assertEqual(server.authentications, baseline + 1)
        second = self.master(server, "other", persist=2)
        self.assertNotEqual(second.path, first.path)
        self.assertNotEqual(
            self.channel(second, "printenv", "SSH_CONNECTION").stdout, connection.stdout
        )
        self.assertEqual(server.authentications, baseline + 2)
        eventually(lambda: not first.path.exists())
        first.ensure()
        self.assertEqual(self.channel(first, "true").returncode, 0)
        self.assertEqual(server.authentications, baseline + 3)

    def test_channel_limit_and_missing_master_never_open_another_connection(self):
        server = self.server(max_sessions=1)
        baseline = server.authentications
        master = self.master(server)
        transport = Transport("nt-test-host", server.client_config, master)
        held = subprocess.Popen(
            transport.wrap(["sh", "-c", "echo ready; exec cat"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_env(),
        )
        try:
            self.assertEqual(held.stdout.readline(), b"ready\n")
            self.assertNotEqual(self.channel(master, "true").returncode, 0)
            self.assertEqual(server.authentications, baseline + 1)
        finally:
            held.communicate(timeout=5)
        self.assertEqual(held.returncode, 0)
        master.stop()
        self.assertNotEqual(self.channel(master, "true").returncode, 0)
        self.assertEqual(server.authentications, baseline + 1)

    def test_stale_master_is_replaced(self):
        server = self.server()
        baseline = server.authentications
        master = self.master(server)
        code, detail = master._run(master.command("check"))
        self.assertEqual(code, 0)
        pid = int(re.search(r"pid=(\d+)", detail)[1])
        os.kill(pid, signal.SIGKILL)  # PID obtained only from this test's private master.
        eventually(lambda: master._run(master.command("check"))[0] != 0)
        master.ensure()
        self.assertEqual(self.channel(master, "true").returncode, 0)
        self.assertEqual(server.authentications, baseline + 2)

    def test_unsafe_master_paths_are_rejected(self):
        master = SSHMaster(self.directory, "nt-test-host")
        victim = self.directory / "victim"
        victim.write_text("unchanged")
        master.path.symlink_to(victim)
        with self.assertRaisesRegex(Error, "unsafe SSH master socket"):
            master.ensure()
        self.assertEqual(victim.read_text(), "unchanged")
        for name in ("%h", "${HOME}", "~", "x" * 100):
            with self.assertRaises(Error):
                SSHMaster(self.directory / name, "nt-test-host")
