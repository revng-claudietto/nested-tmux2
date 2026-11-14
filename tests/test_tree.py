from helpers import TreeCase, eventually, fixture

from nested_tmux.config import Error


class TreeTests(TreeCase):
    def test_explicit_titles_remain_literal_tmux_format_text(self):
        marker = self.directory / "title-executed"
        title = f"#{{pane_id}} #(touch {marker})"
        data = fixture()
        data["root"]["children"][0]["title"] = title
        tree = self.build(data)
        self.assertEqual(self.query("work", "display-message", "-p", "#{window_name}"), [title])
        tree.rename("work/shell", title + " renamed")
        tree.prepare()
        self.assertEqual(
            self.query("work", "display-message", "-p", "#{window_name}"), [title + " renamed"]
        )
        self.assertFalse(marker.exists())

    def test_create_navigate_and_restart_without_replaying_commands(self):
        tree = self.build()
        for path in tree.rows:
            self.assertEqual(self.query(path, "list-windows", "-F", "#{window_index}"), ["0", "1"])
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
        tree.reap_exited()
        self.assertTrue(tree.closed)

    def test_remove_last_child_prunes_ancestors_and_closes_root(self):
        tree = self.build()
        tree.remove("work/project/build/one")
        self.assertEqual(
            self.query("work/project/build", "list-windows", "-F", "#{window_index}"), ["0"]
        )
        tree.remove("work/project/build/two")
        self.assertNotIn("work/project/build", tree.rows)
        tree.remove("work/project/edit")
        self.assertNotIn("work/project", tree.rows)
        self.assertEqual(tree.remove("work/shell"), {"stopped": True})
        self.assertTrue(tree.closed)

    def test_unreachable_row_is_not_treated_as_exited(self):
        tree = self.build()
        before = tree.config
        tree.rows["work/project/build"].control.close()
        tree.reap_exited()
        self.assertEqual(tree.config, before)
        self.assertIn("work/project/build", tree.errors)

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
