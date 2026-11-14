from helpers import TreeCase

from nested_tmux.config import Error
from nested_tmux.control import Control, Transport


class ControlTests(TreeCase):
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
