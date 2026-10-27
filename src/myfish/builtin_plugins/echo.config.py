from pydantic import Field, StrictBool

from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.plugin("echo")
class EchoConfig(BaseConfig):
    comment = "回声示例插件。"

    enabled: StrictBool = Field(default=True, description="是否启用插件")
