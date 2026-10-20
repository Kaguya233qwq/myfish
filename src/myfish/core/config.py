# myfish/core/config.py
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AdapterConfig(BaseModel):
    """单个适配器的配置模型"""

    id: str
    config: dict[str, Any] = Field(default_factory=dict)


def _default_plugins() -> dict[str, dict[str, Any]]:
    return {"fishy": {"enabled": False}, "echo": {"enabled": True}}


class BotConfig(BaseModel):
    """全局 Bot 配置模型"""

    model_config = ConfigDict(hide_input_in_errors=True)

    name: str = "MyFishBot"
    logger_level: str = "INFO"
    cmd_prefix: str = "/"

    data_dir: str = "data"  # 默认数据存储目录
    superusers: list[str] = Field(default_factory=list)
    adapters: list[AdapterConfig] = Field(default_factory=list)
    plugins: dict[str, dict[str, Any]] = Field(default_factory=_default_plugins)

    @field_validator("plugins")
    @classmethod
    def _validate_plugins(
        cls, plugins: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        result = _default_plugins()
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
            cls._config = BotConfig()
            cls._config.data_dir = str(config_path.parent / "data")
            return cls._config

        with open(config_path, "rb") as f:
            try:
                data = tomllib.load(f)
                logger.success(f"成功加载配置文件: {config_path.resolve()}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"解析 TOML 文件失败，请检查语法: {e}")
                sys.exit(1)

        try:
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
    def _generate_default_config(cls, toml_path: Path):
        """生成带有注释的默认配置文件"""
        default_toml = """# ==========================================
# MyFishBot 全局配置文件
# ==========================================

# 机器人的名称
name = "MyFishBot"

# 全局日志输出级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
# 开发时用 DEBUG，生产环境用 INFO
logger_level = "INFO"

# 触发机器人指令的前缀
cmd_prefix = "/"

# 数据存储目录，存储所有的框架、适配器与插件所产生的数据
data_dir = "data"

# 拥有最高权限的超级用户 ID 列表
# 例: superusers = ["12345678", "87654321"]
superusers = []


# ==========================================
# 适配器 (Adapters) 挂载配置
# ==========================================
# 你可以取消下方的注释来启用对应的适配器。

# 🐟 闲鱼适配器
[[adapters]]
id = "fish"
[adapters.config]
cookies = "" # 不填则扫码登陆

# 🦞 微信ClawBot适配器 (ilink)
# [[adapters]]
# id = "ilink"
# [adapters.config]

# 插件开关使用文件名或包目录名，修改后重启生效。
# 商品管理后台需先安装 myfish[fishy]，再改为 true。
[plugins.fishy]
enabled = false
host = "127.0.0.1"
port = 8787

# 回声示例插件，设为 false 关闭自动回复。
[plugins.echo]
enabled = true
"""
        try:
            toml_path.parent.mkdir(parents=True, exist_ok=True)
            with open(toml_path, "w", encoding="utf-8") as f:
                f.write(default_toml)
            logger.info(f"未检测到配置文件，已为您自动生成默认配置: {toml_path}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"自动生成默认配置文件失败: {e}")


get_config = ConfigManager.get_config
load_config = ConfigManager.load_config
