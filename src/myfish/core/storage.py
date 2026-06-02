from pathlib import Path
from myfish.core.config import get_config


class StorageManager:
    """全局统一的持久化存储管理器"""

    @classmethod
    def get_root_dir(cls) -> Path:
        """获取全局数据根目录，并确保其存在"""
        config = get_config()
        root_path = Path(config.data_dir).resolve()
        root_path.mkdir(parents=True, exist_ok=True)
        return root_path

    @classmethod
    def get_adapter_dir(cls, adapter_id: str) -> Path:
        """
        获取指定适配器的数据目录
        """
        adapter_path = cls.get_root_dir() / "adapters" / adapter_id
        adapter_path.mkdir(parents=True, exist_ok=True)
        return adapter_path

    @classmethod
    def get_plugin_dir(cls, plugin_name: str) -> Path:
        """
        获取指定插件的数据目录
        """
        plugin_path = cls.get_root_dir() / "plugins" / plugin_name
        plugin_path.mkdir(parents=True, exist_ok=True)
        return plugin_path
