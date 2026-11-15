import fcntl
import json
import os
import signal
import struct
import subprocess
import sys
import termios
import time
from unittest.mock import patch

import yaml
from helpers import eventually
from ssh_server import SSHServer
from test_service import ServiceCase

from nested_tmux.cli import size_argument, terminal_size
from nested_tmux.config import load
from nested_tmux.control import clean_env
from nested_tmux.runtime import launcher
from nested_tmux.service import request, state_path


class GeometryTests(ServiceCase):
    def setup_recipe(self, remote=False):
        self.probe = self.directory / "size-probe.py"
        self.probe.write_text("""\
import json, os, signal, sys
output = open(sys.argv[1], "a", buffering=1)
def record(event):
    size = os.get_terminal_size(0)
    output.write(json.dumps([event, size.columns, size.lines]) + "\\n")
    print("\\033[?1049h\\033[H\\033[2J" + str(size), end="", flush=True)
signal.signal(signal.SIGWINCH, lambda *args: record("resize"))
record("start")
while True:
    signal.pause()
""")
        self.ssh_config = None
        if remote:
            directory = self.directory / "ssh"
            directory.mkdir(mode=0o700)
            server = SSHServer(directory)
            self.addCleanup(server.close)
            self.ssh_config = server.client_config
        data = {
            "name": "geometry",
            "root": {
                "id": "work",
                "children": [
                    self.probe_node("outer"),
                    {
                        "id": "branch",
                        "host": "nt-test-host" if remote else "local",
                        "children": [
                            self.probe_node("inner"),
                            {"id": "deep", "children": [self.probe_node("deepest")]},
                        ],
                    },
                    {
                        "id": "hidden",
                        "host": "nt-test-host" if remote else "local",
                        "children": [self.probe_node("hidden")],
                    },
                ],
            },
        }
        self.config_path = self.directory / "workspace.yaml"
        self.config_path.write_text(yaml.safe_dump(data))
        self.config = load(self.config_path)
        self.state = state_path(self.config.name, self.directory)
        self.addCleanup(self.cleanup_service)
        binary = os.environ.get("NESTED_TMUX_TEST_BINARY")
        self.command = [binary] if binary else launcher()
        self.up = [*self.command, "up", str(self.config_path), "--runtime-dir", str(self.directory)]
        if self.ssh_config:
            self.up += ["--ssh-config", str(self.ssh_config)]

    def probe_node(self, ident):
        return {
            "id": ident,
            "command": [sys.executable, str(self.probe), str(self.directory / f"{ident}.log")],
        }

    def readings(self, ident):
        path = self.directory / f"{ident}.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.endswith("]")]

    def check_start(self, width, height):
        for ident, depth in (("outer", 1), ("inner", 2), ("deepest", 3)):
            readings = eventually(lambda: self.readings(ident))
            self.assertEqual(readings[0], ["start", width, height - depth], readings)

    def exercise_attached(self, remote):
        self.setup_recipe(remote)
        terminal = self.attach_pty([*self.up, "--attach"])
        eventually(lambda: (self.state / "rpc.sock").exists(), timeout=30)
        self.status = request(self.state, "status")
        self.check_start(140, 40)
        # Both child rows started hidden. Showing them must not change the
        # dimensions that their full-screen application saw at its first draw.
        for action in ("right", "up", "right", "up"):
            request(self.state, "nav", action=action)
        time.sleep(0.3)
        for ident, depth in (("outer", 1), ("inner", 2), ("deepest", 3)):
            self.assertTrue(
                all(size[1:] == [140, 40 - depth] for size in self.readings(ident)),
                self.readings(ident),
            )
        # The initial sizing must not leave window-size=manual behind.
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        os.kill(self.attachment.pid, signal.SIGWINCH)
        eventually(lambda: self.readings("deepest")[-1][1:] == [160, 47])
        # tmux intentionally postpones resizing hidden windows until selection.
        request(self.state, "nav", action="down")
        request(self.state, "nav", action="left")
        eventually(lambda: self.readings("inner")[-1][1:] == [160, 48])
        request(self.state, "nav", action="down")
        request(self.state, "nav", action="left")
        eventually(lambda: self.readings("outer")[-1][1:] == [160, 49])
        self.status = request(
            self.state,
            "add",
            parent="work/hidden",
            node={"id": "added", "children": [self.probe_node("new")]},
        )
        self.assertEqual(eventually(lambda: self.readings("new"))[0], ["start", 160, 47])
        time.sleep(0.2)
        self.assertTrue(all(size[1:] == [160, 47] for size in self.readings("new")))

    def test_first_frame_size_and_later_resize_locally(self):
        self.exercise_attached(remote=False)

    def test_first_frame_size_and_later_resize_over_ssh(self):
        self.exercise_attached(remote=True)

    def test_headless_explicit_size_and_attach(self):
        self.setup_recipe()
        result = subprocess.run(
            [*self.up, "--size", "121x35"],
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.status = json.loads(result.stdout)
        self.check_start(121, 35)
        self.attach_pty(size=(121, 35))
        time.sleep(0.3)
        self.assertTrue(all(size[1:] == [121, 32] for size in self.readings("deepest")))
        # A size hint cannot override an already attached terminal, and
        # reconciling must not restart any application or temporarily shrink it.
        result = subprocess.run(
            [*self.up, "--size", "80x24"],
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for ident, depth in (("outer", 1), ("inner", 2), ("deepest", 3)):
            self.assertEqual(sum(row[0] == "start" for row in self.readings(ident)), 1)
            self.assertTrue(all(row[1:] == [121, 35 - depth] for row in self.readings(ident)))

    def test_terminal_detection_ignores_environment_and_checks_other_fds(self):
        with patch.dict(os.environ, COLUMNS="999", LINES="888"):
            with patch("os.get_terminal_size", side_effect=OSError):
                self.assertIsNone(terminal_size())
            with patch("os.get_terminal_size", side_effect=[OSError(), os.terminal_size((90, 30))]):
                self.assertEqual(terminal_size(), (90, 30))
        self.assertEqual(size_argument("140x40"), (140, 40))
        import argparse

        for value in ("0x40", "140x0", "10001x40", "1;bad", "-1x20", "140x40x2"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                size_argument(value)
