from pathlib import Path

from pydantic import Field, StrictBool

from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.plugin("fishy")
class Settings(BaseConfig):
    comment = ("渔渔商品管理后台。", "启用后执行 myfish sync 安装依赖。")

    enabled: StrictBool = Field(default=False, description="是否启用插件")
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    secure_cookies: bool = False
    session_hours: int = Field(default=12, ge=1, le=72)
    public_origin: str = ""
    poll_seconds: int = Field(default=60, ge=10, le=3600)
    listing_sync_seconds: int = Field(default=300, ge=60, le=86400)
    listing_sync_batch: int = Field(default=20, ge=1, le=100)
    data_dir: Path | None = None
