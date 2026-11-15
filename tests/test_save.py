import contextlib
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from nested_tmux.cli import main
from nested_tmux.config import Error, discover, load, save


class SaveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nt-save-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.config = self.directory / "config"
        self.destination = self.config / "nested-tmux2" / "work.yaml"
        self.data = {
            "name": "work",
            "root": {"id": "desk", "children": [{"id": "shell", "command": ["cat"]}]},
        }
        env = patch.dict(
            os.environ,
            HOME=str(self.directory / "home"),
            XDG_CONFIG_HOME=str(self.config),
            XDG_CONFIG_DIRS=str(self.directory / "system"),
        )
        env.start()
        self.addCleanup(env.stop)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*args, "--runtime-dir", str(self.directory / "runtime")])
        return code, out.getvalue(), err.getvalue()

    def test_save_replaces_recipe_privately_and_dump_stays_on_stdout(self):
        with patch("nested_tmux.cli.request", return_value=self.data):
            code, out, err = self.cli("dump", "--name", "work")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(yaml.safe_load(out), self.data)
            self.assertFalse(self.destination.exists())
            self.assertEqual(
                self.cli("save", "--name", "work"), (0, f"Saved {self.destination}\n", "")
            )
            self.assertEqual(yaml.safe_load(self.destination.read_text()), self.data)
            self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.destination.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(discover("work"), self.destination)
            self.destination.write_text("root: [broken\n")
            self.assertEqual(self.cli("save", "--name", "work")[0], 1)
            self.assertEqual(self.destination.read_text(), "root: [broken\n")
            self.assertEqual(self.cli("save", "--name", "work", "--replace")[0], 0)
            self.assertEqual(load(self.destination).name, "work")

    def test_default_workspace_and_home_fallback(self):
        data = {**self.data, "name": "default"}
        for value in (None, "", "relative"):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = value
                with patch("nested_tmux.cli.request", return_value=data):
                    self.assertEqual(self.cli("save")[0], 0)
                path = self.directory / "home/.config/nested-tmux2/default.yaml"
                self.assertEqual(load(path).name, "default")

    def test_explicit_source_only_selects_workspace_and_is_not_overwritten(self):
        source = self.directory / "source.yml"
        original = yaml.safe_dump(self.data)
        source.write_text(original)
        changed = copy.deepcopy(self.data)
        changed["root"]["children"].append({"id": "extra"})
        with patch("nested_tmux.cli.request", return_value=changed):
            self.assertEqual(self.cli("save", str(source))[0], 0)
        self.assertEqual(source.read_text(), original)
        self.assertEqual(len(load(self.destination).root.children), 2)

    def test_system_and_yml_recipes_are_shadowed_by_user_yaml(self):
        for path in (
            self.directory / "system/nested-tmux2/work.yaml",
            self.destination.with_suffix(".yml"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(self.data))
        save(self.data, name="work")
        self.assertEqual(discover("work"), self.destination)

    def test_failed_replace_preserves_original_and_cleans_temporary_file(self):
        save(self.data, name="work")
        original = self.destination.read_bytes()
        with patch("nested_tmux.config.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                save(self.data, name="work")
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(list(self.destination.parent.iterdir()), [self.destination])

    def test_save_replaces_symlink_without_writing_its_target(self):
        target = self.directory / "managed.yaml"
        target.write_text("managed externally\n")
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(target)
        save(self.data, name="work", replace=True)
        self.assertFalse(self.destination.is_symlink())
        self.assertEqual(target.read_text(), "managed externally\n")
        self.assertEqual(load(self.destination).name, "work")

    def test_missing_controller_does_not_create_recipe(self):
        with patch("nested_tmux.cli.request", side_effect=FileNotFoundError("no controller")):
            self.assertEqual(self.cli("save", "--name", "work")[0], 1)
        self.assertFalse(self.config.exists())

    def test_additive_save_preserves_commands_settings_and_removed_nodes(self):
        saved = copy.deepcopy(self.data)
        saved["root"]["cwd"] = "/original"
        saved["root"]["children"] = [
            {"id": "shell", "command": "exec my-editor", "env": {"MODE": "saved"}},
            {"id": "closed", "command": "exec long-job"},
            {"id": "branch", "children": [{"id": "old", "command": "exec old-job"}]},
        ]
        save(saved, name="work")
        live = copy.deepcopy(self.data)
        live["root"]["cwd"] = "/current"
        live["root"]["children"] = [
            {"id": "shell", "cwd": "/current", "env": {"MODE": "live"}},
            {"id": "branch", "children": [{"id": "new", "command": ["cat"]}]},
            {"id": "extra", "command": ["cat"]},
        ]
        with patch("nested_tmux.cli.request", return_value=live):
            self.assertEqual(self.cli("save", "--name", "work")[0], 0)
            first = self.destination.read_bytes()
            self.assertEqual(self.cli("save", "--name", "work")[0], 0)
            self.assertEqual(self.destination.read_bytes(), first)
            merged = yaml.safe_load(first)
            self.assertEqual(merged["root"]["cwd"], "/original")
            children = merged["root"]["children"]
            self.assertEqual(
                [node["id"] for node in children], ["shell", "closed", "branch", "extra"]
            )
            self.assertEqual(children[0], saved["root"]["children"][0])
            self.assertEqual([node["id"] for node in children[2]["children"]], ["old", "new"])
            self.assertEqual(self.cli("save", "--name", "work", "--replace")[0], 0)
            self.assertEqual(yaml.safe_load(self.destination.read_text()), live)

    def test_merge_conflicts_leave_recipe_unchanged(self):
        save(self.data, name="work")
        original = self.destination.read_bytes()
        for root in (
            {"id": "different", "children": [{"id": "shell"}]},
            {"id": "desk", "children": [{"id": "shell", "children": [{"id": "child"}]}]},
            {
                "id": "desk",
                "children": [{"id": "shell", "host": "remote", "children": [{"id": "child"}]}],
            },
        ):
            with self.subTest(root=root), self.assertRaisesRegex(Error, "save --replace"):
                save({**self.data, "root": root}, name="work")
            self.assertEqual(self.destination.read_bytes(), original)
