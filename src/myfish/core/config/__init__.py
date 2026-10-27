import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import (
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
)

from .builder import CompiledConfig, ConfigBuilder
from .registry import BaseConfig, ConfigRegistry, ConfigSpec, ConfigurationError

__all__ = [
    "AdapterConfig",
    "BaseConfig",
    "BotConfig",
    "CompiledConfig",
    "ConfigBuilder",
    "ConfigManager",
    "ConfigRegistry",
    "ConfigSpec",
    "ConfigurationError",
    "get_config",
    "load_config",
]


class AdapterConfig(BaseConfig):
    """单个适配器的配置模型"""

    id: str
    enabled: StrictBool = Field(default=True, description="是否启用适配器")
    config: dict[str, Any] = Field(default_factory=dict)


def _default_plugins() -> dict[str, dict[str, Any]]:
    return ConfigRegistry.plugin_defaults()


class BotConfig(BaseConfig):
    """全局 Bot 配置模型"""

    comment = ("Myfish 全局配置。", "修改配置后执行 myfish sync，再启动框架。")

    name: str = Field(default="MyFishBot", description="机器人名称")
    logger_level: str = Field(
        default="INFO", description="日志级别：DEBUG、INFO、WARNING、ERROR、CRITICAL"
    )
    cmd_prefix: str = Field(default="/", description="指令前缀")

    data_dir: str = Field(default="data", description="框架、适配器与插件的数据目录")
    superusers: list[str] = Field(default_factory=list, description="超级用户 ID 列表")
    adapters: list[AdapterConfig] = Field(default_factory=list)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=_default_plugins)

    @field_validator("plugins")
    @classmethod
    def _validate_plugins(
        cls, plugins: dict[str, dict[str, Any]], info: ValidationInfo
    ) -> dict[str, dict[str, Any]]:
        selected = (
            info.context.get("config_specs") if isinstance(info.context, dict) else None
        )
        specs = (
            [spec for spec in selected if isinstance(spec, ConfigSpec)]
            if isinstance(selected, list)
            else None
        )
        result = ConfigRegistry.plugin_defaults(specs)
        for name, settings in plugins.items():
            if "enabled" in settings and not isinstance(settings["enabled"], bool):
                raise ValueError(f"插件 {name} 的 enabled 必须为布尔值")
            result[name] = {**result.get(name, {}), **settings}
        return result


class ConfigManager:
    """全局配置管理器，负责加载和提供 Bot 配置"""

    _config: BotConfig | None = None

    @classmethod
    def load_config(cls) -> BotConfig:
        """加载配置，若不存在则生成默认并返回"""
        # 读取环境变量，如果没有，则默认在当前目录生成
        env_path = os.getenv("MYFISH_CONFIG_PATH", "config.toml")
        config_path = Path(env_path)
        if not config_path.exists():
            cls._generate_default_config(config_path)

        with open(config_path, "rb") as f:
            try:
                data = tomllib.load(f)
                logger.success(f"成功加载配置文件: {config_path.resolve()}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"解析 TOML 文件失败，请检查语法: {e}")
                sys.exit(1)

        try:
            issues = ConfigBuilder(BotConfig).validate(data)
            if issues:
                raise ValueError("\n".join(issues))
            cls._config = BotConfig(**data)
        except Exception as e:  # noqa: BLE001
            logger.error(f"配置内容校验失败，请检查字段类型:\n{e}")
            sys.exit(1)

        return cls._config

    @classmethod
    def get_config(cls) -> BotConfig:
        """获取已加载的全局配置"""
        if cls._config is None:
            raise RuntimeError(
                "配置尚未加载，请先在入口处调用 ConfigManager.load_config()"
            )
        return cls._config

    @classmethod
    def sync_config(cls, path: Path | None = None) -> CompiledConfig:
        target = path or Path(os.getenv("MYFISH_CONFIG_PATH", "config.toml"))
        compiled = ConfigBuilder(BotConfig).sync(target)
        if compiled.backup is not None:
            logger.info("配置备份已保存: {}", compiled.backup)
        for note in compiled.notes:
            logger.warning("{}", note)
        logger.info(
            "配置已更新: {}" if compiled.changed else "配置无需更新: {}",
            target.resolve(),
        )
        return compiled

    @classmethod
    def _generate_default_config(cls, toml_path: Path) -> None:
        directory = Path(
            os.getenv("MYFISH_PLUGINS_DIR", str(toml_path.parent / "plugins"))
        )
        ConfigBuilder(BotConfig, plugins_dir=directory).sync(toml_path)
        logger.info("默认配置已生成: {}", toml_path)


get_config = ConfigManager.get_config
load_config = ConfigManager.load_config
