"""Compile the same small client used by the Nix package for source-tree tests."""

import os
import shlex
import subprocess
from pathlib import Path


def build_helper(directory, timeout_ms=None):
    binary = Path(directory) / "nested-tmux-key"
    subprocess.run(
        [
            *shlex.split(os.environ.get("CC", "cc")),
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            *([f"-DNT_KEY_TIMEOUT_MS={timeout_ms}"] if timeout_ms else []),
            str(Path(__file__).resolve().parents[1] / "native" / "key.c"),
            "-o",
            str(binary),
        ],
        check=True,
    )
    return binary
