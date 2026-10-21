import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from myfish import cli
from myfish.core.config import BotConfig
from myfish.core.dependency import DependencyError


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = Path(sys.prefix)
        self.python = Path(sys.executable)
        self.enterContext(
            patch(
                "myfish.core.dependency.runtime_paths",
                return_value=(self.environment, self.python),
            )
        )

    def test_sync_command_and_upgrade(self) -> None:
        for upgrade in (False, True):
            args = ["myfish", "sync"] + (["--upgrade"] if upgrade else [])
            with (
                patch.object(sys, "argv", args),
                patch("myfish.core.config.load_config"),
                patch("myfish.core.dependency.PluginDependencies") as factory,
            ):
                cli.main()
                factory.return_value.sync.assert_called_once_with(upgrade=upgrade)

    def test_run_refuses_unsynchronized_environment(self) -> None:
        with (
            patch.object(sys, "argv", ["myfish", "run"]),
            patch("myfish.core.config.load_config"),
            patch("myfish.core.dependency.PluginDependencies") as factory,
            patch("myfish.main.main", new_callable=AsyncMock) as start,
        ):
            factory.return_value.check.side_effect = DependencyError("myfish sync")
            with self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code, 1)
            start.assert_not_called()

    def test_handoff_does_not_load_config_or_check_dependencies(self) -> None:
        environment = Path("/unused/runtime")
        python = environment / "bin/python"
        with (
            patch(
                "myfish.core.dependency.runtime_paths",
                return_value=(environment, python),
            ),
            patch.object(Path, "is_file", return_value=True),
            patch("myfish.core.config.load_config") as load,
            patch("myfish.core.dependency.PluginDependencies") as factory,
            patch.object(cli.subprocess, "call", return_value=7) as call,
        ):
            with self.assertRaises(SystemExit) as error:
                cli.run_bot()
            self.assertEqual(error.exception.code, 7)
            call.assert_called_once_with([str(python), "-m", "myfish.cli", "run"])
            load.assert_not_called()
            factory.assert_not_called()

    def test_missing_runtime_reports_sync_without_loading_config(self) -> None:
        with (
            patch(
                "myfish.core.dependency.runtime_paths",
                return_value=(Path("/unused"), Path("/unused/python")),
            ),
            patch.object(Path, "is_file", return_value=False),
            patch("myfish.core.config.load_config") as load,
        ):
            with self.assertRaisesRegex(DependencyError, "myfish sync"):
                cli.run_bot()
            load.assert_not_called()

    def test_managed_environment_preserves_custom_bot(self) -> None:
        with (
            patch("myfish.core.config.load_config") as load,
            patch("myfish.core.dependency.PluginDependencies") as factory,
            patch.object(cli.runpy, "run_path") as run,
            patch.object(Path, "exists", return_value=True),
        ):
            cli.run_bot()
            load.assert_called_once_with()
            factory.return_value.check.assert_called_once_with()
            run.assert_called_once_with(str(Path.cwd() / "bot.py"), run_name="__main__")

    def test_default_entry_receives_loaded_config(self) -> None:
        config = BotConfig()
        with (
            patch("myfish.core.config.load_config", return_value=config) as load,
            patch("myfish.core.dependency.PluginDependencies") as factory,
            patch.object(Path, "exists", return_value=False),
            patch("myfish.main.main", new_callable=AsyncMock) as start,
        ):
            cli.run_bot()
            load.assert_called_once_with()
            factory.assert_called_once_with(config)
            factory.return_value.check.assert_called_once_with()
            start.assert_awaited_once_with(config)
