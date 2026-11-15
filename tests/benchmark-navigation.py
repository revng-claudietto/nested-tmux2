#!/usr/bin/env python3
"""Measure an installed package using isolated servers and actual PTY keyboard input."""

import argparse
import json
import math
import os
import runpy
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def main():
    from test_service import ServiceCase

    from nested_tmux.config import load
    from nested_tmux.control import Control, Transport, clean_env
    from nested_tmux.service import request, state_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, help="installed nested-tmux executable")
    parser.add_argument("--tmux", type=Path, required=True, help="tmux used by that package")
    parser.add_argument("--samples", type=int, default=100)
    args = parser.parse_args()
    if args.samples < 20:
        parser.error("use at least 20 samples")
    snapshot = runpy.run_path(str(ROOT / "tests" / "run-local.py"))["invoking_server_snapshot"]
    before = snapshot()
    original_path = os.environ["PATH"]
    # Use the package's tmux for isolated servers, keeping the invoking server's
    # original tmux client for the before/after inventory checks.
    os.environ["PATH"] = str(args.tmux.resolve().parent) + os.pathsep + original_path
    case = ServiceCase()
    case.setUp()
    try:
        case.config_path = case.directory / "workspace.yaml"
        case.config_path.write_text(
            "name: latency\nroot:\n  id: work\n  children:\n"
            "    - id: one\n      command: cat\n"
            "    - id: two\n      command: cat\n"
        )
        case.config = load(case.config_path)
        case.state = state_path(case.config.name, case.directory)
        case.ssh_config = None
        case.addCleanup(case.cleanup_service)
        result = subprocess.run(
            [
                str(args.binary.resolve()),
                "up",
                str(case.config_path),
                "--runtime-dir",
                str(case.directory),
            ],
            env=clean_env(),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        case.status = json.loads(result.stdout)
        terminal = case.attach_pty()
        control = Control(Transport(), case.status["rows"][0]["socket"])
        control.connect(create=False)
        case.addCleanup(control.close)
        binding = control.call("list-keys", "-T", "root", "M-Right")[0]
        script = shlex.split(binding)[-1]
        if str(case.state) not in script or not ("_rpc" in script or "nested-tmux-key" in script):
            raise RuntimeError(f"unexpected test binding: {binding}")
        print(json.dumps({"binary": str(args.binary), "binding": script}), flush=True)

        def measure(label, action):
            def drain_observer():
                # Helper/RPC measurements change windows without querying this
                # independent observer. Drain its bounded notification queue
                # outside the timed section, even in long benchmark runs.
                control.call("display-message", "-p", "#{window_id}")

            for _ in range(5):
                action()
                drain_observer()
            samples = []
            for _ in range(args.samples):
                start = time.perf_counter_ns()
                action()
                samples.append((time.perf_counter_ns() - start) / 1e6)
                drain_observer()
            ordered = sorted(samples)
            print(
                json.dumps(
                    {
                        "case": label,
                        "samples": len(samples),
                        "median_ms": round(statistics.median(samples), 3),
                        "p95_ms": round(ordered[math.ceil(len(samples) * 0.95) - 1], 3),
                        "max_ms": round(max(samples), 3),
                    }
                ),
                flush=True,
            )

        measure(
            "persistent tmux control next-window", lambda: control.call("next-window", "-t", "tree")
        )
        measure(
            "Unix socket RPC to daemon, next-window",
            lambda: request(case.state, "nav", action="right"),
        )
        measure(
            "installed key helper, shell launch through completion",
            lambda: subprocess.run(
                ["sh", "-c", script], env=clean_env(), capture_output=True, check=True, timeout=5
            ),
        )

        current = control.call("display-message", "-p", "#{window_id}")[0]

        def press_key():
            nonlocal current
            os.write(terminal, b"\x1b[1;3C")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                selected = control.call("display-message", "-p", "#{window_id}")[0]
                if selected != current:
                    current = selected
                    return
                time.sleep(0.001)
            raise RuntimeError("navigation key timed out")

        measure("real PTY Alt+Right until tmux selection changes", press_key)
    finally:
        try:
            case.doCleanups()
        finally:
            os.environ["PATH"] = original_path
        if snapshot() != before:
            raise RuntimeError("invoking tmux pane/process inventory changed")
        print("Invoking tmux server: pane IDs and process IDs unchanged.", flush=True)


if __name__ == "__main__":
    main()
