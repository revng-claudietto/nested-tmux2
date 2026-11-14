from helpers import TreeCase, eventually, fixture

from nested_tmux.config import Error


class TreeTests(TreeCase):
    def test_create_navigate_and_restart_without_replaying_commands(self):
        tree = self.build()
        before = {
            path: self.query(path, "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
            for path in tree.rows
        }
        self.assertEqual(tree.navigate("up")["active"], "work")  # A terminal is not a child row.
        tree.navigate("right")
        self.assertEqual(tree.navigate("up")["active"], "work/project")
        tree.navigate("right")
        self.assertEqual(tree.navigate("up")["active"], "work/project/build")
        self.assertEqual(tree.navigate("down")["active"], "work/project")
        self.assertEqual(tree.navigate("down")["active"], "work")
        self.assertEqual(tree.navigate("down")["active"], "work")
        tree.prepare()
        after = {
            path: self.query(path, "list-panes", "-a", "-F", "#{pane_id}:#{pane_pid}")
            for path in tree.rows
        }
        self.assertEqual(before, after)

    def test_command_cwd_environment_and_quick_exit(self):
        cwd = self.directory / "path with ' quote and $dollar"
        cwd.mkdir()
        data = {
            "name": "test",
            "root": {
                "id": "work",
                "cwd": str(cwd),
                "env": {"NT_VALUE": "quoted ' $HOME ; value"},
                "children": [
                    {
                        "id": "job",
                        "command": 'printf "cwd=%s\\nvalue=%s\\n" "$PWD" "$NT_VALUE"\nexit 7',
                    }
                ],
            },
        }
        tree = self.build(data)
        wid = tree.root.windows["work/job"]
        eventually(
            lambda: self.query("work", "display-message", "-p", "-t", wid, "#{pane_dead}") == ["1"]
        )
        output = self.capture("work", wid)
        self.assertIn(f"cwd={cwd}", output)
        self.assertIn("value=quoted ' $HOME ; value", output)
        self.assertEqual(
            self.query("work", "display-message", "-p", "-t", wid, "#{pane_dead_status}"), ["7"]
        )

    def test_argv_is_not_interpreted_as_shell(self):
        data = fixture()
        sentinel = self.directory / "injected"
        literal = f"' \" $HOME ; $(touch {sentinel}) \\ #"
        data["root"]["children"] = [{"id": "literal", "command": ["printf", "%s", literal]}]
        tree = self.build(data)
        wid = tree.root.windows["work/literal"]
        eventually(lambda: literal in self.capture("work", wid))
        self.assertFalse(sentinel.exists())

    def test_interrupted_startup_does_not_replay_an_uncertain_command(self):
        tree = self.build()
        wid = tree.root.windows["work/shell"]
        before = self.query("work", "display-message", "-p", "-t", wid, "#{pane_pid}")
        self.query("work", "set-option", "-w", "-t", wid, "@nt-ready", "starting")
        with self.assertRaisesRegex(Error, "unknown outcome"):
            tree.prepare()
        self.assertEqual(
            before, self.query("work", "display-message", "-p", "-t", wid, "#{pane_pid}")
        )
