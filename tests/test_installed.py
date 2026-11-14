"""Exercise the installed wrapper, daemon, and keyboard helpers away from source."""

import json
import os
import subprocess
import unittest

import yaml
from helpers import eventually, fixture
from test_service import ServiceCase

from nested_tmux.config import load
from nested_tmux.control import clean_env
from nested_tmux.service import state_path


class InstalledTests(ServiceCase):
    @unittest.skipUnless(
        os.environ.get("NESTED_TMUX_TEST_BINARY"), "installed package test runs in nix build"
    )
    def test_installed_cli_controller_and_keyboard_helper(self):
        self.config_path = self.directory / "workspace.yaml"
        self.config_path.write_text(yaml.safe_dump(fixture()))
        self.config = load(self.config_path)
        self.state = state_path(self.config.name, self.directory)
        self.ssh_config = None
        self.addCleanup(self.cleanup_service)

        def cli(op, *args):
            result = subprocess.run(
                [
                    os.environ["NESTED_TMUX_TEST_BINARY"],
                    op,
                    str(self.config_path),
                    "--runtime-dir",
                    str(self.directory),
                    *args,
                ],
                cwd=self.directory,
                env=clean_env(),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.status = json.loads(cli("up"))
        terminal = self.attach_pty()
        os.write(terminal, b"\x14")
        eventually(
            lambda: len(self.tmux("work", "list-windows", "-F", "#{window_id}").splitlines()) == 3
        )
        self.status = json.loads(cli("add-row", "scratch"))
        exported = yaml.safe_load(cli("dump"))
        self.assertEqual(exported["root"]["children"][-1]["id"], "scratch")
        cli("remove", "work/scratch")
        cli("stop")
        eventually(lambda: not (self.state / "rpc.sock").exists())
        cli("down")
