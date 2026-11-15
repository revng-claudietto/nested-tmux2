import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from native_helper import build_helper


class KeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="nt-key-unit-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.short_deadline = build_helper(cls.temp.name, timeout_ms=150)
        cls.binary = shutil.which("nested-tmux-key") or cls.short_deadline

    def exchange(self, chunks, *, binary=None, delay=0):
        with tempfile.TemporaryDirectory(prefix="nt-key-socket-") as directory:
            path = str(Path(directory) / "rpc.sock")
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                server.listen(1)
                server.settimeout(3)
                received = []
                errors = []

                def respond():
                    try:
                        client, _ = server.accept()
                        with client, client.makefile("rb") as stream:
                            client.settimeout(3)
                            received.append(json.loads(stream.readline(4096)))
                            for chunk in chunks:
                                time.sleep(delay)
                                try:
                                    client.sendall(chunk)
                                except (BrokenPipeError, ConnectionResetError):
                                    break
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=respond, daemon=True)
                thread.start()
                start = time.monotonic()
                result = subprocess.run(
                    [str(binary or self.binary), path, "right"],
                    capture_output=True,
                    timeout=3,
                )
                elapsed = time.monotonic() - start
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(received, [{"op": "key", "action": "right"}])
                return result, elapsed

    def test_fragmented_success_and_sanitized_errors(self):
        result, _ = self.exchange([b"O", b"K", b"\n"], delay=0.01)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"")
        result, _ = self.exchange([b"ERR remote \x1b[31m\0\xff failure\n"])
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"remote ?[31m?? failure", result.stderr)
        self.assertNotIn(b"\x1b", result.stderr)

    def test_missing_malformed_and_oversized_replies_fail(self):
        for chunks in [[], [b"OK"], [b"OOPS\n"], [b"OK\nERR extra\n"], [b"x" * 4096]]:
            with self.subTest(chunks=str(chunks)[:40]):
                result, _ = self.exchange(chunks)
                self.assertEqual(result.returncode, 1)

    def test_deadline_covers_stalls_and_slow_partial_replies(self):
        for chunks, delay in [([b"OK\n"], 0.3), ([b"x"] * 50, 0.02)]:
            with self.subTest(delay=delay):
                result, elapsed = self.exchange(chunks, binary=self.short_deadline, delay=delay)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b"outcome is unknown (not retried)", result.stderr)
                self.assertLess(elapsed, 1)

    def test_bad_actions_paths_and_missing_controller_fail(self):
        with tempfile.TemporaryDirectory(prefix="nt-key-missing-") as directory:
            for path, action in [
                (str(Path(directory) / "absent.sock"), "right"),
                ("x" * 200, "right"),
                (str(Path(directory) / "absent.sock"), 'right";touch /tmp/invalid'),
            ]:
                with self.subTest(action=action, path=path):
                    result = subprocess.run(
                        [str(self.binary), path, action], capture_output=True, timeout=3
                    )
                    self.assertEqual(result.returncode, 1)
