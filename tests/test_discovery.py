import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nested_tmux.cli import main
from nested_tmux.config import Error, discover


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nt-config-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.home = self.directory / "home"
        self.config = self.directory / "config"
        self.system = self.directory / "system"
        env = patch.dict(
            os.environ,
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.config),
            XDG_CONFIG_DIRS=str(self.system),
        )
        env.start()
        self.addCleanup(env.stop)

    def recipe(self, directory, filename="work.yaml", contents=None):
        path = directory / "nested-tmux2" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents or "root: {id: work, children: [{id: shell}]}\n")
        return path

    def check(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["check", *args])
        return code, out.getvalue(), err.getvalue()

    def test_xdg_precedence_and_extensions(self):
        system = self.recipe(self.system)
        user = self.recipe(self.config, "work.yml")
        self.assertEqual(discover("work"), user)
        preferred = self.recipe(self.config)
        self.assertEqual(discover("work"), preferred)
        preferred.unlink()
        user.unlink()
        self.assertEqual(discover("work"), system)
        system.unlink()
        self.assertIsNone(discover("work"))

    def test_home_fallback_for_unset_empty_and_relative_xdg_home(self):
        fallback = self.recipe(self.home / ".config")
        for value in (None, "", "relative"):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = value
                self.assertEqual(discover("work"), fallback)

    def test_config_dirs_order_and_relative_entries(self):
        first = self.recipe(self.system)
        self.recipe(self.directory / "second")
        with patch.dict(
            os.environ,
            XDG_CONFIG_DIRS=f"relative::{self.system}:{self.directory / 'second'}",
        ):
            self.assertEqual(discover("work"), first)

    def test_workspace_name_cannot_escape_config_directory(self):
        for name in ("../escape", "/tmp/escape", "", "a/b"):
            with self.subTest(name=name), self.assertRaises(Error):
                discover(name)

    def test_discovered_name_optional_and_cannot_redirect_workspace(self):
        path = self.recipe(self.config)
        self.assertEqual(self.check("--name", "work"), (0, "work: valid (1 rows)\n", ""))
        path.write_text("name: another\n" + path.read_text())
        self.assertEqual(self.check("--name", "work"), (0, "work: valid (1 rows)\n", ""))
        self.recipe(self.config, "default.yaml")
        self.assertEqual(self.check(), (0, "default: valid (1 rows)\n", ""))

    def test_explicit_file_overrides_discovery(self):
        self.recipe(self.config, contents="invalid: recipe\n")
        explicit = self.recipe(
            self.directory / "explicit",
            contents="name: explicit\nroot: {id: work, children: [{id: shell}]}\n",
        )
        self.assertEqual(self.check(str(explicit)), (0, "explicit: valid (1 rows)\n", ""))
        self.assertEqual(
            self.check(str(explicit), "--name", "work"), (0, "work: valid (1 rows)\n", "")
        )

    def test_invalid_or_missing_explicit_recipe_does_not_fall_back(self):
        self.assertEqual(self.check("--name", "work")[0], 0)
        path = self.recipe(self.config, contents="root: [broken\n")
        code, _, err = self.check("--name", "work")
        self.assertEqual(code, 1)
        self.assertIn(str(path), err)
        self.assertEqual(self.check(str(self.directory / "missing.yaml"))[0], 1)
        path.unlink()
        path.symlink_to(self.directory / "missing.yaml")
        self.assertEqual(self.check("--name", "work")[0], 1)
