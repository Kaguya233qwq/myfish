import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from myfish.core.config import BotConfig
from myfish.core.dependency import DependencyError, PluginDependencies


class PluginDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = PluginDependencies(BotConfig(), self.root)
        self.package = self.root / "framework" / "src" / "myfish"
        self.package.mkdir(parents=True)
        (self.package.parent.parent / "pyproject.toml").write_text(
            '[project]\nname="myfish"\nversion="1.0"\n', encoding="utf-8"
        )
        self.manager.package = self.package
        self.env = patch.dict(
            "os.environ", {"MYFISH_PLUGINS_DIR": str(self.root / "plugins")}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def plugin(self, name, requirement="example>=1", builtin=False):
        directory = (
            self.package / "builtin_plugins" if builtin else self.root / "plugins"
        )
        package = directory / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("raise AssertionError('must not import')")
        (package / "requirements.txt").write_text(requirement)
        return package

    def test_discovery_skips_disabled_and_never_imports(self):
        self.plugin("fishy", builtin=True)
        self.plugin("disabled")
        enabled = self.plugin("enabled")
        self.manager.config = BotConfig(
            plugins={"disabled": {"enabled": False}, "fishy": {"enabled": True}}
        )
        framework, files, _ = self.manager.inputs()
        self.assertIn("[fishy]", framework)
        self.assertEqual(files, [enabled / "requirements.txt"])

    def test_xhs_adapter_selects_extra_and_changes_fingerprint(self):
        before = self.manager.inputs()[2]
        self.manager.config = BotConfig(adapters=[{"id": "xhs"}])
        framework, _, after = self.manager.inputs()
        self.assertIn("[xhs]", framework)
        self.assertNotEqual(before, after)

    def test_only_package_dependencies_and_nested_changes_are_discovered(self):
        package = self.plugin("example", "-r nested.txt\n")
        nested = package / "nested.txt"
        nested.write_text("example==1\n")
        (package.parent / "example.py").write_text("raise AssertionError")
        (package.parent / "example.requirements.txt").write_text("wrong")
        (package.parent / "single.py").write_text("raise AssertionError")
        single = package.parent / "single.requirements.txt"
        single.write_text("single-dependency")
        _, files, before = self.manager.inputs()
        self.assertEqual(files, [package / "requirements.txt"])
        single.write_text("ignored-dependency-change")
        self.assertEqual(before, self.manager.inputs()[2])
        nested.write_text("example==2\n")
        self.assertNotEqual(before, self.manager.inputs()[2])

    def test_installed_framework_fingerprint_survives_environment_handoff(self):
        self.plugin("fishy", builtin=True)
        (self.package.parent.parent / "pyproject.toml").unlink()
        self.manager.config = BotConfig(plugins={"fishy": {"enabled": True}})
        with patch("myfish.core.dependency.version", return_value="1.0"):
            before = self.manager.inputs()[2]
            self.manager.package = self.root / "runtime" / "site-packages" / "myfish"
            self.package = self.manager.package
            self.plugin("fishy", builtin=True)
            self.assertEqual(before, self.manager.inputs()[2])

    def fake_uv(self, *args):
        self.calls.append(args)
        if args[:2] == ("pip", "compile"):
            Path(args[args.index("-o") + 1]).write_text("example==1\n")
        elif args[0] == "venv":
            self.manager.python.parent.mkdir(parents=True)
            self.manager.python.touch()

    def synchronize(self, upgrade=False):
        self.calls = []
        with (
            patch.object(self.manager, "uv", side_effect=self.fake_uv),
            patch.object(self.manager, "snapshot", return_value=[["example", "1"]]),
        ):
            self.manager.sync(upgrade)

    def test_sync_lock_reuse_upgrade_and_environment_check(self):
        self.synchronize()
        self.assertEqual([call[0] for call in self.calls], ["pip", "venv", "pip"])
        with patch.object(self.manager, "snapshot", return_value=[["example", "1"]]):
            self.manager.check()
        with (
            patch.object(self.manager, "snapshot", return_value=[]),
            self.assertRaisesRegex(DependencyError, "myfish sync"),
        ):
            self.manager.check()
        self.synchronize(upgrade=True)
        self.assertIn("--upgrade", self.calls[0])
        self.assertNotIn("venv", [call[0] for call in self.calls])
        self.manager.lock.write_text("tampered")
        with self.assertRaises(DependencyError):
            self.manager.check()

    def test_unchanged_sync_reuses_lock_without_resolution(self):
        self.synchronize()
        self.synchronize()
        self.assertEqual([call[:2] for call in self.calls], [("pip", "sync")])

    def test_resolution_failure_preserves_lock_and_success_marker(self):
        self.synchronize()
        original = self.manager.lock.read_bytes()
        with (
            patch.object(self.manager, "uv", side_effect=DependencyError("conflict")),
            self.assertRaises(DependencyError),
        ):
            self.manager.sync(upgrade=True)
        self.assertEqual(self.manager.lock.read_bytes(), original)
        self.assertTrue(self.manager.state.exists())
        self.assertFalse((self.manager.directory / "syncing").exists())

    def test_install_failure_invalidates_success_marker(self):
        self.synchronize()

        def fail(*args):
            if args[:2] == ("pip", "sync"):
                raise DependencyError("install failed")
            self.fake_uv(*args)

        with (
            patch.object(self.manager, "uv", side_effect=fail),
            self.assertRaises(DependencyError),
        ):
            self.manager.sync()
        self.assertFalse(self.manager.state.exists())

    def test_missing_state_and_concurrent_sync_are_rejected(self):
        with self.assertRaisesRegex(DependencyError, "myfish sync"):
            self.manager.check()
        guard = self.manager.directory / "syncing"
        guard.mkdir(parents=True)
        with self.assertRaisesRegex(DependencyError, "同步正在进行"):
            self.manager.sync()
        self.assertTrue(guard.exists())


if __name__ == "__main__":
    unittest.main()
