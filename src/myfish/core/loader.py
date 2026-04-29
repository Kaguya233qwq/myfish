import importlib
import inspect
import os
import sys
from pathlib import Path

from .logger import logger

from .plugin import Plugin


class PluginManager:
    """全局插件注册表与生命周期管理器"""

    def __init__(self):
        self.plugins: dict[str, Plugin] = {}

        self.package_root = Path(__file__).resolve().parent.parent
        self.builtin_plugins_dir = self.package_root / "builtin_plugins"

        default_user_plugins = Path.cwd() / "plugins"
        self.user_plugins_dir = Path(
            os.getenv("MYFISH_PLUGINS_DIR", str(default_user_plugins))
        )

    def load_all_plugins(self) -> None:
        """加载所有插件：先加载内置插件，再加载外部插件"""

        # 加载内部插件
        if self.builtin_plugins_dir.exists():
            logger.info("开始加载内置插件...")
            self._load_from_dir(self.builtin_plugins_dir)

        # 加载外部插件
        logger.info("开始加载外部插件...")
        if not self.user_plugins_dir.exists():
            try:
                self.user_plugins_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"已自动创建外部插件目录: {self.user_plugins_dir}")
            except PermissionError:
                logger.warning(
                    f"无法创建插件目录 {self.user_plugins_dir}，权限不足。"
                )

        if self.user_plugins_dir.exists():
            self._load_from_dir(self.user_plugins_dir)

    def _load_from_dir(self, directory: Path) -> None:
        """
        核心装载引擎：从指定绝对路径动态扫描并加载插件。
        """
        if not directory.is_dir():
            return

        parent_dir = str(directory.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

        package_name = directory.name

        for file in directory.iterdir():
            if (
                file.is_file()
                and file.suffix == ".py"
                and not file.name.startswith("_")
            ):
                module_name = f"{package_name}.{file.stem}"

                try:
                    module = importlib.import_module(module_name)
                    plugin_instance = None

                    for _, obj in inspect.getmembers(module):
                        if isinstance(obj, Plugin):
                            plugin_instance = obj
                            break

                    if plugin_instance:
                        plugin_name = plugin_instance.metadata.name
                        if plugin_name in self.plugins:
                            logger.warning(
                                f"插件命名冲突跳过: [{plugin_name}] ({file.name})"
                            )
                            continue

                        self.plugins[plugin_name] = plugin_instance
                        logger.success(
                            f"✅ 成功加载插件: [{plugin_name}] v{plugin_instance.metadata.version}"
                        )
                    else:
                        logger.warning(
                            f"模块 {file.name} 中未找到 Plugin 实例，已跳过。"
                        )

                except Exception as e:
                    logger.error(f"❌ 加载插件 {file.name} 失败: {e}")
