from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.adapter("ilink")
class ILinkConfig(BaseConfig):
    comment = "微信 iLink 适配器"
