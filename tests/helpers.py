import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from nested_tmux.config import parse
from nested_tmux.control import clean_env
from nested_tmux.runtime import Tree


def eventually(function, timeout=8):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = function()
        if last:
            return last
        time.sleep(0.03)
    raise AssertionError(f"condition not met in {timeout}s (last value {last!r})")


def fixture(cwd="/tmp"):
    return {
        "name": "test",
        "root": {
            "id": "work",
            "cwd": cwd,
            "children": [
                {"id": "shell", "command": ["cat"]},
                {
                    "id": "project",
                    "children": [
                        {"id": "edit", "command": ["cat"]},
                        {
                            "id": "build",
                            "children": [
                                {"id": "one", "command": ["cat"]},
                                {"id": "two", "command": ["cat"]},
                            ],
                        },
                    ],
                },
            ],
        },
    }


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nt-test-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.tree = None

    def build(self, data=None, ssh_config=None):
        self.tree = Tree(parse(data or fixture()), self.directory, ssh_config)
        self.addCleanup(self.cleanup_tree)
        self.tree.prepare()
        return self.tree

    def cleanup_tree(self):
        # Explicit sockets only, including after failed startup or control disconnect.
        for row in self.tree.rows.values():
            subprocess.run(
                row.control.transport.wrap(["tmux", "-L", row.control.socket_name, "kill-server"]),
                env=clean_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        self.tree.close()

    def query(self, path, *args):
        return self.tree.rows[path].control.call(*args)

    def capture(self, path, target):
        return "\n".join(self.query(path, "capture-pane", "-p", "-S", "-", "-J", "-t", target))
