from pydantic import Field

from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.adapter("fish", default_enabled=True)
class FishConfig(BaseConfig):
    comment = "闲鱼适配器配置。"

    cookies: str | dict[str, str] | None = Field(
        default="", description="登录 Cookie；留空时使用本地凭据或扫码登录", repr=False
    )
