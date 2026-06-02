# myfish/core/config.py
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from loguru import logger

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        raise ImportError("Python 3.11 以下版本需要安装 tomli 库: pip install tomli")


class AdapterConfig(BaseModel):
    """单个适配器的配置模型"""

    id: str
    config: Dict[str, Any] = Field(default_factory=dict)


class BotConfig(BaseModel):
    """全局 Bot 配置模型"""

    name: str = "MyFishBot"
    logger_level: str = "INFO"
    cmd_prefix: str = "/"

    data_dir: str = "data"  # 默认数据存储目录
    superusers: List[str] = Field(default_factory=list)
    adapters: List[AdapterConfig] = Field(default_factory=list)


class ConfigManager:
    """全局配置管理器，负责加载和提供 Bot 配置"""

    _config: Optional[BotConfig] = None

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
            except Exception as e:
                logger.error(f"解析 TOML 文件失败，请检查语法: {e}")
                sys.exit(1)

        try:
            cls._config = BotConfig(**data)
        except Exception as e:
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

# 🐟 官方闲鱼适配器
# [[adapters]]
# id = "fish"
# [adapters.config]
# cookies = ""

# 🦞 微信ClawBot适配器 (ilink)
# [[adapters]]
# id = "ilink"
# [adapters.config]
"""
        try:
            toml_path.parent.mkdir(parents=True, exist_ok=True)
            with open(toml_path, "w", encoding="utf-8") as f:
                f.write(default_toml)
            logger.info(f"未检测到配置文件，已为您自动生成默认配置: {toml_path}")
        except Exception as e:
            logger.error(f"自动生成默认配置文件失败: {e}")


get_config = ConfigManager.get_config
load_config = ConfigManager.load_config
