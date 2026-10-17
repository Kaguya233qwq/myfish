import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from myfish.core.loader import PluginManager


def plugin_source(name):
    return (
        "from myfish.core.plugin import Plugin, PluginMetadata\n"
        f"plugin = Plugin(PluginMetadata(id={name!r}, name={name!r}))\n"
    )


class PluginLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.module_name = f"loader_test_{uuid.uuid4().hex}"
        self.directory = self.root / self.module_name
        self.directory.mkdir()
        self.manager = PluginManager()
        self.old_path = list(sys.path)
        self.addCleanup(self.clean_imports)

    def clean_imports(self):
        for name in list(sys.modules):
            if name == self.module_name or name.startswith(self.module_name + "."):
                sys.modules.pop(name, None)
        sys.path[:] = self.old_path

    def write(self, relative_path, source):
        path = self.directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    def test_single_file_and_package_with_relative_imports(self):
        self.write("single.py", plugin_source("single"))
        self.write("package/__init__.py", "from .handlers import plugin\n")
        self.write(
            "package/handlers.py",
            "from .config import NAME\n" + plugin_source("package"),
        )
        self.write("package/config.py", "NAME = 'package'\n")
        self.manager._load_from_dir(self.directory)
        self.assertEqual(set(self.manager.plugins), {"single", "package"})
        self.assertIn(f"{self.module_name}.package.handlers", sys.modules)
        self.assertIn(f"{self.module_name}.package.config", sys.modules)

    def test_private_files_and_directories_without_entry_are_ignored(self):
        forbidden = "raise RuntimeError('should never import')\n"
        self.write("__init__.py", "")
        self.write("_private.py", forbidden)
        self.write("_private_package/__init__.py", forbidden)
        self.write(".hidden/__init__.py", forbidden)
        self.write("no_entry/plugin.py", forbidden)
        self.write("notes.txt", forbidden)
        self.write("valid/__init__.py", plugin_source("valid"))
        self.manager._load_from_dir(self.directory)
        self.assertEqual(set(self.manager.plugins), {"valid"})

    def test_broken_package_and_missing_instance_do_not_block_other_plugins(self):
        self.write("a_broken/__init__.py", "from .missing import plugin\n")
        self.write("b_empty/__init__.py", "VALUE = 1\n")
        self.write("c_valid/__init__.py", plugin_source("valid"))
        self.manager._load_from_dir(self.directory)
        self.assertEqual(set(self.manager.plugins), {"valid"})

    def test_duplicate_plugin_names_and_same_module_name(self):
        self.write("a_file.py", plugin_source("duplicate"))
        self.write("b_package/__init__.py", plugin_source("duplicate"))
        self.write("same.py", "raise RuntimeError('package must win')\n")
        self.write("same/__init__.py", plugin_source("package wins"))
        with patch("myfish.core.loader.logger") as log:
            self.manager._load_from_dir(self.directory)
        self.assertEqual(set(self.manager.plugins), {"duplicate", "package wins"})
        self.assertEqual(log.success.call_count, 2)
        self.assertGreaterEqual(log.warning.call_count, 2)

    def test_both_builtin_and_user_roots_accept_packages(self):
        self.write("bundled/__init__.py", plugin_source("builtin"))
        user_root = self.directory / "user_plugins"
        user_root.mkdir()
        (user_root / "__init__.py").write_text("", encoding="utf-8")
        package = user_root / "external"
        package.mkdir()
        (package / "__init__.py").write_text(plugin_source("user"), encoding="utf-8")
        self.manager.builtin_plugins_dir = self.directory
        self.manager.user_plugins_dir = user_root
        # user_root 的父目录可作为另一个顶级包导入，退出时一并清理。
        self.addCleanup(lambda: sys.modules.pop("user_plugins.external", None))
        self.addCleanup(lambda: sys.modules.pop("user_plugins", None))
        self.manager.load_all_plugins()
        self.assertEqual(set(self.manager.plugins), {"builtin", "user"})


if __name__ == "__main__":
    unittest.main()
