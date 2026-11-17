from helpers import TreeCase

from nested_tmux.config import Error
from nested_tmux.control import Control, Transport


class ControlTests(TreeCase):
    def test_reconnect_never_starts_an_empty_server(self):
        from nested_tmux.inventory import socket_directory

        control = Control(Transport(), "missing-row")
        self.addCleanup(control.close)
        with self.assertRaises(Error):
            control.connect(create=False)
        self.assertFalse((socket_directory() / "missing-row").exists())

    def test_shutdown_retries_have_a_deadline(self):
        import time
        from unittest.mock import patch

        from nested_tmux.control import TransportError

        control = Control(Transport(), "stopping-row", timeout=0.15)
        error = TransportError("tmux control exited on local: %exit server exited unexpectedly")
        with patch.object(control, "_connect", side_effect=error) as connect:
            before = time.monotonic()
            with self.assertRaisesRegex(TransportError, "stopping-row.*shutting down"):
                control.connect()
            self.assertLess(time.monotonic() - before, 1)
            self.assertGreater(connect.call_count, 1)
            connect.reset_mock()
            with self.assertRaises(TransportError):
                control.connect(create=False)
            self.assertEqual(connect.call_count, 1)

    def test_round_trip_and_error_response_synchronization(self):
        tree = self.build(
            {
                "name": "test",
                "root": {"id": "root", "children": [{"id": "shell", "command": ["cat"]}]},
            }
        )
        control = tree.root.control
        for value in [
            "'single' and \"double\"",
            "semi;colon",
            "#{pane_id}",
            "a\\b",
            "%begin 1 2 3",
            "line1\nline2",
        ]:
            with self.subTest(value=value):
                control.call("set-option", "-g", "@test-literal", value)
                result = control.call("display-message", "-p", "#{@test-literal}")
                self.assertEqual(
                    "\n".join(result), value, control.call("display-message", "-p", "#{version}")
                )
        with self.assertRaisesRegex(Error, "unknown command"):
            control.call("nt-no-such-command")
        self.assertEqual(
            control.call("display-message", "-p", "still connected"), ["still connected"]
        )

    def test_ssh_command_preserves_remote_argument_boundaries(self):
        transport = Transport("devbox")
        import shlex

        args = ["tmux", "-L", "isolated", "display-message", "a ' b $HOME ; c"]
        wrapped = transport.wrap(args)
        self.assertIn("-T", wrapped)
        self.assertEqual(shlex.split(wrapped[-1]), ["exec", *args])

    def test_timeout_is_bounded_and_never_replays_mutations(self):
        control = Control(Transport(), "unused", timeout=0.01)
        with self.assertRaisesRegex(Error, "outcome is unknown"):
            control._response()

    def test_oversized_reply_is_rejected(self):
        from nested_tmux.control import MAX_LINE, MAX_RESPONSE

        control = Control(Transport(), "unused")
        control.lines.put("%begin 1 1 1")
        for _ in range(MAX_RESPONSE // MAX_LINE + 1):
            control.lines.put("x" * MAX_LINE)
        with self.assertRaisesRegex(Error, "response exceeds size limit"):
            control._response()

    def test_notifications_cannot_extend_command_deadline(self):
        from unittest.mock import patch

        control = Control(Transport(), "unused", timeout=0.01)
        for _ in range(10):
            control.lines.put("%window-renamed @1 arbitrary remote text")
        with patch("time.monotonic", side_effect=[0, 0.005, 0.02]):
            with self.assertRaisesRegex(Error, "outcome is unknown"):
                control._response()

    def test_close_does_not_wait_for_inherited_pipe_descriptors(self):
        import os
        import signal
        import sys
        import threading
        from unittest.mock import patch

        from helpers import eventually

        pidfile = self.directory / "pipe-holder.pid"
        script = r"""
import os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(30)
    os._exit(0)
with open(sys.argv[1], 'w') as stream:
    stream.write(str(pid))
print('%begin 1 1 1\n%end 1 1 1', flush=True)
for line in sys.stdin:
    if 'detach-client' in line:
        break
    print('%begin 2 2 1\n%end 2 2 1', flush=True)
"""
        transport = Transport()
        control = Control(transport, "unused", timeout=2)
        thread = None
        try:
            with patch.object(
                transport, "wrap", return_value=[sys.executable, "-c", script, str(pidfile)]
            ):
                control.connect()
            eventually(pidfile.exists)
            thread = threading.Thread(target=control.close, daemon=True)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "close waited for the pipe holder")
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if thread:
                thread.join(timeout=3)
            control.close()
