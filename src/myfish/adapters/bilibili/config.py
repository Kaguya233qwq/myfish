from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator, model_validator

from myfish.core.config import BaseConfig, ConfigRegistry


@ConfigRegistry.adapter("bilibili")
class BilibiliConfig(BaseConfig, extra="forbid"):
    comment = ("Bilibili 适配器配置。", "首次启动时使用 B站 App 扫码登录。")

    cookies: dict[str, str] = Field(default_factory=dict, repr=False)
    poll_interval: float = Field(default=20, ge=1, description="普通轮询间隔，单位为秒")
    active_interval: float = Field(default=3, ge=1)
    active_duration: float = Field(default=60, ge=0)
    reconcile_interval: float = Field(default=300, ge=30)
    timeout: float = Field(default=20, gt=0)
    notifications: bool = True
    live_rooms: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=list, description="订阅的直播房间 ID，留空不连接直播间"
    )

    @field_validator("live_rooms")
    @classmethod
    def unique_rooms(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_intervals(self) -> BilibiliConfig:
        if self.active_interval > self.poll_interval:
            raise ValueError("active_interval must not exceed poll_interval")
        return self
