from pydantic import Field

from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.adapter("xhs")
class XhsConfig(BaseConfig, extra="forbid"):
    comment = ("小红书适配器配置。", "登录与设备信息由适配器保存。")

    receive_offline: bool = False
    reconnects: int = Field(default=5, ge=0, le=10)
