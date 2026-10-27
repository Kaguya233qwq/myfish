from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from myfish.core.registry import AdapterRegistry

from .config import AdapterConfig, BotConfig
from .dependency import plugin_entries
from .logger import logger
from .plugin import Plugin

if TYPE_CHECKING:
    from .engine import Engine


class PluginManager:
    """全局插件注册表与生命周期管理器"""

    def __init__(self, plugin_configs: dict[str, dict[str, Any]] | None = None):
        self.plugins: dict[str, Plugin] = {}
        self._plugin_configs = BotConfig(plugins=plugin_configs or {}).plugins

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
                logger.warning(f"无法创建插件目录 {self.user_plugins_dir}，权限不足。")

        if self.user_plugins_dir.exists():
            self._load_from_dir(self.user_plugins_dir)

    def _load_from_dir(self, directory: Path) -> None:
        """
        加载目录内的单文件插件，以及通过 __init__.py 暴露 Plugin 的包。
        包内子模块由包入口自行导入，不作为独立插件递归扫描。
        """
        if not directory.is_dir():
            return

        parent_dir = str(directory.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

        package_name = directory.name

        for name, file in plugin_entries(directory, self._plugin_configs, logger):
            module_name = f"{package_name}.{name}"

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
                    logger.warning(f"模块 {file.name} 中未找到 Plugin 实例，已跳过。")

            except Exception as e:  # noqa: BLE001
                logger.error(f"❌ 加载插件 {file.name} 失败: {e}")


def load_adapters(adapter_configs: list[AdapterConfig], engine: Engine):
    """
    动态加载适配器模块并挂载到引擎
    """
    if not adapter_configs:
        logger.warning("配置文件中未提供任何适配器，bot 将无法连接任何平台。")
        return

    for conf in adapter_configs:
        if not conf.enabled:
            continue
        adapter_id = conf.id
        module_name = f"myfish.adapters.{adapter_id}"

        try:
            logger.debug(f"正在加载外部适配器包: {module_name}")
            importlib.import_module(module_name)
        except ImportError as e:
            logger.error(
                f"❌ 模块加载失败 [{module_name}]: {e}\n(请检查包名拼写或是否已安装相关依赖) "
            )
            continue

        try:
            logger.info(f"正在装载适配器: [{adapter_id}]...")
            config_dict = conf.config
            adapter_instance = AdapterRegistry.build(adapter_id, **config_dict)
            engine.add_adapter(adapter_instance)
            logger.success(f"✅ 适配器 [{adapter_id}] 装载成功")

        except ValueError as e:
            logger.error(f"⚠️ 适配器 [{adapter_id}] 配置错误: {e}")
        except Exception as e:
            logger.exception(f"❌ 装载适配器 [{adapter_id}] 时发生未知错误: {e}")  # noqa: TRY401
