#!/usr/bin/env python3
"""Run isolated tests and check the invoking tmux server was not modified."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def invoking_server_snapshot():
    current = os.environ.get("TMUX")
    if not current:
        return None
    socket = current.rsplit(",", 2)[0]
    result = subprocess.run(
        [
            "tmux",
            "-S",
            socket,
            "list-panes",
            "-a",
            "-F",
            "#{session_id} #{window_id} #{pane_id} #{pane_pid}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(result.stdout.splitlines())


if __name__ == "__main__":
    before = invoking_server_snapshot()
    original_path = os.environ["PATH"]
    with tempfile.TemporaryDirectory(prefix="nt-key-test-") as directory:
        # Automatic recipe discovery must never read the invoking user's recipes.
        os.environ["XDG_CONFIG_HOME"] = str(Path(directory) / "config")
        os.environ["XDG_CONFIG_DIRS"] = str(Path(directory) / "system-config")
        sockets = Path(directory) / "t"
        sockets.mkdir(mode=0o700)
        os.environ["TMUX_TMPDIR"] = str(sockets)
        if not shutil.which("nested-tmux-key"):
            from native_helper import build_helper

            build_helper(directory)
            os.environ["PATH"] = directory + os.pathsep + original_path
        try:
            suite = (
                unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
                if len(sys.argv) > 1
                else unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
            )
            result = unittest.TextTestRunner(verbosity=2).run(suite)
        finally:
            os.environ["PATH"] = original_path
    after = invoking_server_snapshot()
    if before != after:
        raise SystemExit("Invoking tmux server's pane/process inventory changed during tests")
    if before is not None:
        print("Invoking tmux server: pane IDs and process IDs unchanged.")
    raise SystemExit(not result.wasSuccessful())
