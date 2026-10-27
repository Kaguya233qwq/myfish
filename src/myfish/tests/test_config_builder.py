import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ConfigDict, Field, SecretStr

from myfish.core.config import (
    AdapterConfig,
    BaseConfig,
    BotConfig,
    CompiledConfig,
    ConfigBuilder,
    ConfigRegistry,
    ConfigSpec,
    ConfigurationError,
)
from myfish.core.dependency import PluginDependencies, plugin_entries


class Root(BaseConfig):
    comment = "框架说明"
    name: str = Field(default="test", description="名称")
    adapters: list[AdapterConfig] = Field(default_factory=list)
    plugins: dict[str, dict[str, object]] = Field(default_factory=dict)


class ConfigBuilderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "config.toml"

    def tearDown(self):
        self.directory.cleanup()

    def builder(self, model, *, migrate=None):
        return ConfigBuilder(
            Root, [ConfigSpec("plugin", "custom", model, migrate=migrate)]
        )

    def test_generation_comments_nested_models_and_optional_values(self):
        class Child(BaseConfig):
            comment = "服务设置"
            port: int = Field(default=9000, description="服务端口")

        class PluginConfig(BaseConfig):
            comment = ("插件说明", "第二行")
            enabled: bool = True
            child: Child = Field(default_factory=Child)
            optional: str | None = None

        compiled = self.builder(PluginConfig).sync(self.path)
        data = tomllib.loads(compiled.text)
        self.assertEqual(data["plugins"]["custom"]["child"]["port"], 9000)
        self.assertNotIn("optional", data["plugins"]["custom"])
        self.assertIn("# 服务设置\n[plugins.custom.child]", compiled.text)
        self.assertIn("# 服务端口", compiled.text)
        self.assertIn("# 框架说明", compiled.text)

    def test_repeated_sync_is_stable_and_does_not_create_backups(self):
        first = ConfigBuilder(BotConfig).sync(self.path)
        second = ConfigBuilder(BotConfig).sync(self.path)
        self.assertEqual(first.text, second.text)
        self.assertFalse(second.changed)
        self.assertIsNone(second.backup)

    def test_adapter_headings_and_inline_dictionary_format(self):
        class Settings(BaseConfig):
            comment = ("示例适配器配置。", "登录设置。")
            cookies: dict[str, str] = Field(
                default_factory=dict, description="登录凭据"
            )

        builder = ConfigBuilder(Root, [ConfigSpec("adapter", "custom", Settings)])
        first = builder.sync(self.path)
        self.assertIn(
            "\n\n#####################\n# 示例适配器配置。\n# 登录设置。\n[[adapters]]",
            first.text,
        )
        self.assertIn("[adapters.config]\ncookies = {} # 登录凭据", first.text)
        data = tomllib.loads(first.text)
        self.assertNotIn("comment", data["adapters"][0]["config"])
        self.assertEqual(builder.sync(self.path).text, first.text)

    def test_dictionary_tables_become_inline_without_losing_values_or_comments(self):
        class Settings(BaseConfig):
            cookies: dict[str, str] = Field(default_factory=dict)

        self.path.write_text(
            '[[adapters]]\nid = "custom"\n[adapters.config.cookies] # 凭据备注\n'
            '# 用户备注\nSESSDATA = "keep" # 会话备注\n'
        )
        builder = ConfigBuilder(Root, [ConfigSpec("adapter", "custom", Settings)])
        result = builder.sync(self.path)
        self.assertNotIn("[adapters.config.cookies]", result.text)
        self.assertIn('cookies = {SESSDATA = "keep"} # 凭据备注', result.text)
        self.assertIn("# 用户备注", result.text)
        self.assertIn("# 会话备注", result.text)
        self.assertEqual(
            tomllib.loads(result.text)["adapters"][0]["config"]["cookies"],
            {"SESSDATA": "keep"},
        )
        self.assertEqual(builder.sync(self.path).text, result.text)

    def test_removed_description_preserves_user_inline_comment(self):
        class Old(BaseConfig):
            enabled: bool = True
            count: int = Field(default=1, description="数量")

        class New(BaseConfig):
            enabled: bool = True
            count: int = 1

        self.builder(Old).sync(self.path)
        self.path.write_text(
            self.path.read_text().replace("# 数量", "# 数量；自定数量")
        )
        result = self.builder(New).sync(self.path)
        self.assertIn("count = 1 # 自定数量", result.text)
        self.assertFalse(self.builder(New).compile(self.path).changed)

    def test_existing_values_user_comments_and_literal_marker_text_are_preserved(self):
        class PluginConfig(BaseConfig):
            enabled: bool = True
            port: int = Field(default=8787, description="新说明")
            note: str = ""

        original = '# 用户说明\nname = "mine" # 原来的名字\n[plugins.custom]\nport = 9000 # 我的端口\nnote = """第一行\n# 这是字符串内容\n"""\n'
        self.path.write_text(original)
        compiled = self.builder(PluginConfig).sync(self.path)
        data = tomllib.loads(compiled.text)
        self.assertEqual(data["name"], "mine")
        self.assertEqual(data["plugins"]["custom"]["port"], 9000)
        self.assertIn("# 这是字符串内容", data["plugins"]["custom"]["note"])
        self.assertIn("# 用户说明", compiled.text)
        self.assertIn("；我的端口", compiled.text)
        self.assertIn("；原来的名字", compiled.text)
        self.assertEqual(compiled.backup.read_text(), original)
        if os.name != "nt":
            self.assertEqual(compiled.backup.stat().st_mode & 0o777, 0o600)

    def test_block_and_field_comments_update_without_removing_user_comments(self):
        class Old(BaseConfig):
            comment = "旧块说明"
            enabled: bool = True
            count: int = Field(default=1, description="旧字段说明")

        class New(BaseConfig):
            comment = "新块说明"
            enabled: bool = True
            count: int = Field(default=2, description="新字段说明")

        self.builder(Old).sync(self.path)
        self.path.write_text(
            self.path.read_text().replace(
                "count = 1 # 旧字段说明", "# 我的备注\ncount = 8 # 旧字段说明；我选的值"
            )
        )
        compiled = self.builder(New).sync(self.path)
        self.assertNotIn("旧块说明", compiled.text)
        self.assertNotIn("旧字段说明", compiled.text)
        self.assertIn("新块说明", compiled.text)
        self.assertIn("新字段说明", compiled.text)
        self.assertIn("# 我的备注", compiled.text)
        self.assertIn("count = 8 # 新字段说明；我选的值", compiled.text)
        self.assertFalse(self.builder(New).compile(self.path).changed)

    def test_removed_fields_require_previous_schema_and_unknown_values_survive(self):
        class Old(BaseConfig):
            model_config = ConfigDict(extra="allow")
            enabled: bool = True
            retired: int = 1

        class New(BaseConfig):
            model_config = ConfigDict(extra="allow")
            enabled: bool = True
            added: int = 3

        self.builder(Old).sync(self.path)
        self.path.write_text(
            self.path.read_text().replace(
                "retired = 1", 'retired = 2\ncustom_value = "keep"'
            )
        )
        compiled = self.builder(New).sync(self.path)
        settings = tomllib.loads(compiled.text)["plugins"]["custom"]
        self.assertNotIn("retired", settings)
        self.assertEqual(settings["added"], 3)
        self.assertEqual(settings["custom_value"], "keep")

    def test_first_migration_does_not_guess_that_unknown_fields_are_retired(self):
        class PluginConfig(BaseConfig):
            model_config = ConfigDict(extra="allow")
            enabled: bool = True

        self.path.write_text('[plugins.custom]\nold_field = "keep"\n')
        compiled = self.builder(PluginConfig).sync(self.path)
        self.assertEqual(
            tomllib.loads(compiled.text)["plugins"]["custom"]["old_field"], "keep"
        )
        self.assertTrue(any("old_field" in note for note in compiled.notes))

    def test_dynamic_dictionaries_and_secrets_are_not_redacted_or_pruned(self):
        class PluginConfig(BaseConfig):
            enabled: bool = True
            token: SecretStr = SecretStr("")
            cookies: dict[str, str] = Field(default_factory=dict)

        self.path.write_text(
            '[plugins.custom]\ntoken = "private-value"\n[plugins.custom.cookies]\nSESSDATA = "private-cookie"\n'
        )
        compiled = self.builder(PluginConfig).sync(self.path)
        self.assertIn('token = "private-value"', compiled.text)
        self.assertIn('SESSDATA = "private-cookie"', compiled.text)
        state = self.path.with_name("config.toml.schema.json").read_text()
        self.assertNotIn("private-value", state)
        self.assertNotIn("private-cookie", state)
        self.assertNotIn("private-value", repr(compiled.changes))

    def test_nested_lists_receive_new_defaults_and_remove_only_known_old_fields(self):
        class OldNode(BaseConfig):
            retired: int = 1
            port: int = 9

        class Old(BaseConfig):
            enabled: bool = True
            nodes: list[OldNode] = Field(default_factory=lambda: [OldNode()])

        class NewNode(BaseConfig):
            port: int = 10
            timeout: int = 20

        class New(BaseConfig):
            enabled: bool = True
            nodes: list[NewNode] = Field(default_factory=list)

        self.builder(Old).sync(self.path)
        result = self.builder(New).sync(self.path)
        self.assertEqual(
            tomllib.loads(result.text)["plugins"]["custom"]["nodes"],
            [{"port": 9, "timeout": 20}],
        )

    def test_explicit_rename_preserves_user_value_and_detects_conflicts(self):
        class PluginConfig(BaseConfig):
            enabled: bool = True
            timeout: int = Field(
                default=20, json_schema_extra={"x-myfish-renamed-from": "old_timeout"}
            )

        self.path.write_text("[plugins.custom]\nold_timeout = 7 # 用户选择\n")
        result = self.builder(PluginConfig).sync(self.path)
        settings = tomllib.loads(result.text)["plugins"]["custom"]
        self.assertEqual(settings["timeout"], 7)
        self.assertNotIn("old_timeout", settings)
        self.assertIn("# 用户选择", result.text)
        self.path.write_text("[plugins.custom]\nold_timeout = 7\ntimeout = 9\n")
        original = self.path.read_bytes()
        with self.assertRaises(ConfigurationError):
            self.builder(PluginConfig).sync(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_explicit_migration_changes_types_and_preserves_nested_comments(self):
        class Child(BaseConfig):
            port: int = 9
            token: str = ""

        class PluginConfig(BaseConfig):
            enabled: bool = True
            child: Child = Field(default_factory=Child)

        def migrate(data):
            child = data.get("child", {})
            if "port" in child:
                child["port"] = int(child["port"])
            return data

        self.path.write_text(
            '[plugins.custom.child]\nport = "8" # 旧格式\ntoken = "keep" # 私有备注\n'
        )
        result = self.builder(PluginConfig, migrate=migrate).sync(self.path)
        self.assertEqual(
            tomllib.loads(result.text)["plugins"]["custom"]["child"]["port"], 8
        )
        self.assertIn("# 旧格式", result.text)
        self.assertIn("# 私有备注", result.text)
        self.assertFalse(
            self.builder(PluginConfig, migrate=migrate).compile(self.path).changed
        )

    def test_invalid_configuration_does_not_write_or_expose_values(self):
        class PluginConfig(BaseConfig):
            enabled: bool = True
            port: int = Field(default=8787, ge=1)

        self.path.write_text('[plugins.custom]\nport = "private-invalid-value"\n')
        original = self.path.read_bytes()
        with self.assertRaises(ConfigurationError) as error:
            self.builder(PluginConfig).sync(self.path)
        self.assertNotIn("private-invalid-value", str(error.exception))
        self.assertIn("port", str(error.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob("*.bak.*")))
        self.assertFalse(self.path.with_name("config.toml.schema.json").exists())

    def test_new_adapters_only_follow_bootstrap_defaults_for_new_files(self):
        class Settings(BaseConfig):
            token: str = ""

        builder = ConfigBuilder(
            Root, [ConfigSpec("adapter", "custom", Settings, default_enabled=True)]
        )
        self.assertTrue(
            tomllib.loads(builder.compile(self.path).text)["adapters"][0]["enabled"]
        )
        self.path.write_text('name = "existing"\n')
        self.assertFalse(
            tomllib.loads(builder.sync(self.path).text)["adapters"][0]["enabled"]
        )

    def test_unregistered_components_remain_opaque(self):
        self.path.write_text(
            '[[adapters]]\nid = "unknown"\n[adapters.config]\nprivate = "keep"\n[plugins.unknown]\ntoken = "keep"\n'
        )
        compiled = ConfigBuilder(Root, []).sync(self.path)
        data = tomllib.loads(compiled.text)
        self.assertEqual(data["adapters"][0]["config"], {"private": "keep"})
        self.assertEqual(data["plugins"]["unknown"], {"token": "keep"})

    def test_disabled_component_can_have_unconfigured_required_fields(self):
        class Settings(BaseConfig):
            token: str

        compiled = ConfigBuilder(
            Root, [ConfigSpec("adapter", "custom", Settings)]
        ).sync(self.path)
        self.assertFalse(tomllib.loads(compiled.text)["adapters"][0]["enabled"])
        self.assertNotIn("token", tomllib.loads(compiled.text)["adapters"][0]["config"])
        self.assertIn("必填字段 token", compiled.text)

    def test_check_does_not_create_files(self):
        path = self.path.parent / "absent" / "config.toml"
        compiled = ConfigBuilder(Root, []).compile(path)
        self.assertTrue(compiled.changed)
        self.assertFalse(path.parent.exists())

    def test_reusing_builder_keeps_compilations_independent(self):
        class Settings(BaseConfig):
            enabled: bool = True
            count: int = Field(default=1, description="数量")

        self.path.write_text("[plugins.custom]\ncount = 9 # 自定义数量\n")
        builder = self.builder(Settings)
        first = builder.compile(self.path)
        second_path = self.path.parent / "other.toml"
        second = builder.compile(second_path)
        self.assertIsInstance(first, CompiledConfig)
        self.assertEqual(tomllib.loads(first.text)["plugins"]["custom"]["count"], 9)
        self.assertEqual(tomllib.loads(second.text)["plugins"]["custom"]["count"], 1)
        self.assertNotIn("自定义数量", second.text)
        self.assertIsNot(first.state, second.state)
        self.assertIsNot(first.changes, second.changes)
        self.assertFalse(second_path.exists())
        self.assertEqual(builder.compile(self.path).text, first.text)

    def test_concurrent_edit_is_not_overwritten(self):
        class Settings(BaseConfig):
            enabled: bool = True

        def migrate(data):
            self.path.write_text('name = "edited"\n')
            return data

        self.path.write_text('name = "original"\n')
        with self.assertRaisesRegex(ConfigurationError, "发生变化"):
            self.builder(Settings, migrate=migrate).sync(self.path)
        self.assertEqual(self.path.read_text(), 'name = "edited"\n')

    def test_migration_failure_is_sanitized_and_does_not_write(self):
        class Settings(BaseConfig):
            enabled: bool = True

        def migrate(_):
            raise ValueError("private-error-value")

        self.path.write_text('name = "original"\n')
        with self.assertRaises(ConfigurationError) as error:
            self.builder(Settings, migrate=migrate).sync(self.path)
        self.assertNotIn("private-error-value", str(error.exception))
        self.assertEqual(self.path.read_text(), 'name = "original"\n')

    def test_disabled_xhs_does_not_request_optional_dependencies(self):
        manager = PluginDependencies(
            BotConfig(adapters=[{"id": "xhs", "enabled": False}]), self.path.parent
        )
        requirement, _, _ = manager.inputs()
        self.assertNotIn("[xhs]", requirement)


class ConfigDiscoveryTests(unittest.TestCase):
    def test_builtin_discovery_does_not_import_runtime_packages(self):
        code = """import sys
from myfish.core.config import ConfigRegistry
entries = ConfigRegistry.discover()
assert {entry.name for entry in entries} >= {"fish", "ilink", "xhs", "echo", "fishy"}
assert not any(name in sys.modules for name in ("myfish.adapters.fish", "myfish.adapters.bilibili", "myfish.adapters.xhs", "myfish.builtin_plugins.fishy"))
"""
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_external_metadata_is_loaded_without_package_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "custom"
            package.mkdir()
            (package / "__init__.py").write_text(
                'raise RuntimeError("runtime must not load")\n'
            )
            (package / "config.py").write_text(
                'from myfish.core.config import BaseConfig\nfrom myfish.core.config import ConfigRegistry as Registry\n@Registry.plugin("custom")\nclass Settings(BaseConfig):\n    enabled: bool = False\n    value: int = 3\n'
            )
            entries = ConfigRegistry.discover(root)
            spec = next(spec for spec in entries if spec.name == "custom")
            self.assertEqual(spec.model().value, 3)

    def test_unregistered_legacy_config_is_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "legacy"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "config.py").write_text(
                'raise RuntimeError("not a metadata entrypoint")\n'
            )
            self.assertNotIn(
                "legacy",
                [spec.name for spec in ConfigRegistry.discover(Path(directory))],
            )

    def test_single_file_config_sidecar_is_not_loaded_as_a_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text("")
            (root / "sample.config.py").write_text(
                'from myfish.core.config import BaseConfig\nfrom myfish.core.config import ConfigRegistry\n@ConfigRegistry.plugin("sample")\nclass Settings(BaseConfig):\n    enabled: bool = True\n'
            )
            entries = list(plugin_entries(root, {}))
            self.assertEqual([name for name, _ in entries], ["sample"])
            self.assertIn(
                "sample", [spec.name for spec in ConfigRegistry.discover(root)]
            )


class ConfigCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "config.toml"

    def tearDown(self):
        self.directory.cleanup()

    def command(self, *arguments):
        environment = os.environ.copy()
        environment["MYFISH_CONFIG_PATH"] = str(self.path)
        environment.pop("MYFISH_PLUGINS_DIR", None)
        return subprocess.run(
            [sys.executable, "-m", "myfish.cli", *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def test_check_is_read_only_and_sync_then_check_succeeds(self):
        result = self.command("config", "check")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.path.exists())
        result = self.command("config", "sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.path.exists())
        content = self.path.read_bytes()
        result = self.command("config", "check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.path.read_bytes(), content)

    def test_init_generates_config_without_copying_parent_project_plugins(self):
        plugins = self.root / "plugins"
        plugins.mkdir()
        (plugins / "parent_plugin.py").write_text("")
        (plugins / "parent_plugin.config.py").write_text(
            'from myfish.core.config import BaseConfig\nfrom myfish.core.config import ConfigRegistry\n@ConfigRegistry.plugin("parent_plugin")\nclass Settings(BaseConfig):\n    enabled: bool = False\n'
        )
        result = self.command("init", "new_bot")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = tomllib.loads((self.root / "new_bot" / "config.toml").read_text())
        self.assertNotIn("parent_plugin", data["plugins"])
        self.assertTrue(
            any(
                entry["id"] == "fish" and entry["enabled"] for entry in data["adapters"]
            )
        )

    def test_dependency_sync_runs_after_configuration_migration(self):
        from myfish.cli import main

        order = []
        with (
            patch.object(sys, "argv", ["myfish", "sync"]),
            patch(
                "myfish.core.config.ConfigManager.sync_config",
                side_effect=lambda: order.append("config"),
            ),
            patch("myfish.core.config.load_config", return_value=BotConfig()),
            patch("myfish.core.dependency.PluginDependencies") as dependencies,
        ):
            dependencies.return_value.sync.side_effect = lambda **_: order.append(
                "dependencies"
            )
            main()
        self.assertEqual(order, ["config", "dependencies"])
