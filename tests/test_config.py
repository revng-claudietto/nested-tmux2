import tempfile
import unittest
from pathlib import Path

from helpers import fixture

from nested_tmux.config import Error, as_data, load, parse


class ConfigTests(unittest.TestCase):
    def test_export_preserves_automatic_and_explicit_titles(self):
        data = {
            "name": "names",
            "root": {
                "id": "work",
                "children": [
                    {"id": "automatic"},
                    {"id": "fixed", "title": "fixed"},
                    {"id": "branch", "children": [{"id": "shell"}]},
                ],
            },
        }
        config = parse(data)
        exported = as_data(config)
        self.assertNotIn("title", exported["root"]["children"][0])
        self.assertEqual(exported["root"]["children"][1]["title"], "fixed")
        self.assertEqual(parse(exported).root, config.root)

    def test_inheritance_and_remote_home(self):
        data = fixture("/tmp/local")
        data["root"]["env"] = {"BUILD": "debug"}
        data["root"]["children"][1]["host"] = "builder"
        config = parse(data)
        local, remote = config.root.children
        self.assertEqual(local.cwd, "/tmp/local")
        self.assertEqual(remote.cwd, "~")
        self.assertEqual(remote.children[1].children[0].host, "builder")
        self.assertEqual(remote.env, {"BUILD": "debug"})

    def test_reject_invalid_trees(self):
        cases = [
            {"id": "x", "children": []},
            {"id": "x", "cwd": "relative"},
            {"id": "x", "title": None},
            {"id": "x", "command": []},
            {"id": "x", "command": 1},
            {"id": "x", "command": [""]},
            {"id": "x", "commmand": "typo"},
            {"id": "x", "host": "-oProxyCommand=bad"},
            {"id": "x", "host": "remote"},
            {"id": "x", "env": {"TMUX": "wrong server"}},
            {"id": "x", "env": {"PORT": 8080}},
            {"id": "x", "children": [{"id": "a"}, {"id": "a"}]},
            {"id": "x", "command": "ls", "children": [{"id": "a"}]},
            {"id": "x", "host": "remote", "children": [{"id": "a", "host": "local"}]},
        ]
        for item in cases:
            with self.subTest(item=item), self.assertRaises(Error):
                parse({"name": "test", "root": {"id": "root", "children": [item]}})

    def test_yaml_multiline_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "work.yaml"
            path.write_text(
                "name: test\nroot:\n  id: work\n  cwd: ~/src\n  children:\n    - id: build\n      command: |\n        echo hello\n        exec bash\n"
            )
            child = load(path).root.children[0]
            self.assertEqual(child.cwd, "~/src")
            self.assertEqual(child.shell, "echo hello\nexec bash\n")
            path.write_text("name: first\nname: second\nroot: {}\n")
            with self.assertRaisesRegex(Error, "duplicate YAML key"):
                load(path)
            path.write_text("!!python/object/apply:os.system ['false']")
            with self.assertRaises(Error):
                load(path)

    def test_examples(self):
        for path in (Path(__file__).resolve().parent.parent / "examples").glob("*.yaml"):
            with self.subTest(path=path):
                load(path)
