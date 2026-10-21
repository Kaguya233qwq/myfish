import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from myfish.core.config import BotConfig, ConfigManager
from myfish.main import main


class PluginConfigTests(unittest.TestCase):
    def test_default_fishy_is_disabled_and_instances_do_not_share_settings(self):
        first, second = BotConfig(), BotConfig()
        first.plugins["fishy"]["enabled"] = True
        self.assertFalse(second.plugins["fishy"]["enabled"])
        self.assertTrue(second.plugins["echo"]["enabled"])

    def test_custom_settings_merge_defaults_without_losing_plugin_values(self):
        config = BotConfig(
            plugins={"fishy": {"port": 9000}, "custom": {"token": "private"}}
        )
        self.assertEqual(config.plugins["fishy"], {"enabled": False, "port": 9000})
        self.assertEqual(config.plugins["custom"], {"token": "private"})

    def test_explicit_enable_and_disable_are_preserved(self):
        config = BotConfig(
            plugins={"fishy": {"enabled": True}, "echo": {"enabled": False}}
        )
        self.assertTrue(config.plugins["fishy"]["enabled"])
        self.assertFalse(config.plugins["echo"]["enabled"])

    def test_enabled_requires_boolean_without_exposing_other_settings(self):
        for value in ("false", "true", 0, 1, None, []):
            with self.subTest(value=value), self.assertRaises(ValidationError) as error:
                BotConfig(
                    plugins={"custom": {"enabled": value, "token": "private-token"}}
                )
            self.assertNotIn("private-token", str(error.exception))

    def test_generated_toml_has_explicit_fishy_switch_and_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            ConfigManager._generate_default_config(path)
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(data["plugins"]["fishy"]["enabled"])
            self.assertTrue(data["plugins"]["echo"]["enabled"])
            with (
                patch.dict("os.environ", {"MYFISH_CONFIG_PATH": str(path)}),
                patch.object(ConfigManager, "_config", None),
            ):
                config = ConfigManager.load_config()
                self.assertFalse(config.plugins["fishy"]["enabled"])
                self.assertTrue(config.plugins["echo"]["enabled"])


class MainPluginConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_passes_plugin_configuration_to_engine(self):
        config = BotConfig(
            plugins={"fishy": {"enabled": True}, "echo": {"enabled": False}}
        )
        with (
            patch("myfish.main.load_config", return_value=config) as load,
            patch("myfish.main.init_logger"),
            patch("myfish.main.load_adapters"),
            patch("myfish.main.Engine") as factory,
        ):
            factory.return_value.run = AsyncMock()
            await main()
            load.assert_called_once_with()
        factory.assert_called_once_with(plugin_configs=config.plugins)
        factory.return_value.run.assert_awaited_once()

    async def test_main_reuses_configuration_supplied_by_cli(self):
        config = BotConfig(plugins={"fishy": {"enabled": True}})
        with (
            patch("myfish.main.load_config") as load,
            patch("myfish.main.init_logger"),
            patch("myfish.main.load_adapters") as adapters,
            patch("myfish.main.Engine") as factory,
        ):
            factory.return_value.run = AsyncMock()
            await main(config)
            load.assert_not_called()
            factory.assert_called_once_with(plugin_configs=config.plugins)
            adapters.assert_called_once_with(config.adapters, factory.return_value)
            factory.return_value.run.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
