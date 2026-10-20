import base64
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_validator,
)

from myfish.core.event import BaseEvent, MessageEvent
from myfish.core.logger import logger
from myfish.core.message import Audio, Image, MessageChain, MessageSegment, Text

from .events import FishTradeEvent
from .utils import load_message_json


class ItemCard(MessageSegment):
    type: str = "item_card"
    desc: str = "商品卡片"
    item_id: str
    title: str
    price: str
    main_pic: str
    url: str
    item_tip: str = ""


class SystemTip(MessageSegment):
    """灰条系统提示消息 (仅接收)"""

    type: str = "system_tip"
    desc: str = "系统提示"
    tip_text: str


class FishEventSegment(MessageSegment, ABC):
    """可转换为平台事件的消息段。"""

    @abstractmethod
    def to_event(self, account_id: str, body: "MessageBody") -> BaseEvent:
        raise NotImplementedError


class TradeCard(FishEventSegment):
    """交易通知卡片（仅接收）。"""

    type: str = "trade_card"
    desc: str = "交易通知"
    order_id: str = Field(pattern=r"^[0-9]+$")
    title: str = ""
    content: str = ""
    button_text: str = ""
    task_id: str = ""

    def to_event(self, account_id: str, body: "MessageBody") -> FishTradeEvent:
        return FishTradeEvent(
            account_id=account_id,
            cid=body.cid,
            sender_id=body.sender.user_id,
            sender_name=body.sender.name,
            order_id=self.order_id,
            title=self.title,
            content=self.content,
            button_text=self.button_text,
            task_id=self.task_id,
        )


class FishPayloadNode(BaseModel):
    """
    闲鱼协议节点基类
    """

    _registry: ClassVar[dict[int, type["FishPayloadNode"]]] = {}

    content_type: ClassVar[int] = 0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.content_type != 0:
            cls._registry[cls.content_type] = cls

    @classmethod
    def decode(cls, payload: dict[str, Any]) -> list[MessageSegment]:
        """把闲鱼的json反序列化为标准MessageSegment对象"""
        raise NotImplementedError

    @classmethod
    def encode(cls, msg) -> tuple[dict[str, Any], int]:
        """将MessageSegment序列化为闲鱼协议格式的字典"""
        raise NotImplementedError


class FishImageNode(FishPayloadNode):
    content_type = 2

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        pics = payload.get("image", {}).get("pics", [])
        return [
            Image(
                image_url=pic.get("url", ""),
                width=pic.get("width", 0),
                height=pic.get("height", 0),
            )
            for pic in pics
        ]

    @classmethod
    def encode(cls, msg: Image) -> tuple[dict, int]:
        return {
            "contentType": 2,
            "image": {
                "pics": [
                    {
                        "type": 0,
                        "url": msg.image_url,
                        "width": msg.width,
                        "height": msg.height,
                    }
                ]
            },
        }, 2


class FishAudioNode(FishPayloadNode):
    content_type = 3

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        audio_data = payload.get("audio", {})
        return [
            Audio(
                audio_url=audio_data.get("url", ""),
                duration_ms=audio_data.get("duration", 0),
            )
        ]

    @classmethod
    def encode(cls, msg: Audio) -> tuple[dict, int]:
        return {
            "contentType": 3,
            "audio": {
                "url": msg.audio_url,
                "duration": msg.duration_ms,
            },
        }, 2


class FishSystemTipNode(FishPayloadNode):
    content_type = 14

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        tip_text = payload.get("systemTip", {}).get("tipText", "") or payload.get(
            "tip", {}
        ).get("tip", "")
        return [SystemTip(tip_text=tip_text)] if tip_text else []

    @classmethod
    def encode(cls, msg: SystemTip) -> tuple[dict, int]:
        return {
            "contentType": 14,
            "tip": {
                "argInfo": {},
                "tip": msg.tip_text,
            },
        }, 2


class FishCustomNode(FishPayloadNode):
    """闲鱼的富媒体/多消息段混合容器"""

    content_type = 101

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        segments = []
        decoded = load_message_json(payload.get("custom", {}).get("data"))
        if isinstance(decoded, dict):
            decoded = [decoded]
        if isinstance(decoded, list):
            for item in decoded:
                if not isinstance(item, dict):
                    continue
                try:
                    if item.get("type") == "text":
                        segments.append(Text(**item))
                    elif item.get("type") == "image":
                        segments.append(Image(**item))
                    else:
                        segments.extend(Content.model_validate(item).to_message_chain())
                except (ValueError, TypeError, AttributeError) as exc:
                    logger.warning("自定义消息段解析失败: {}", type(exc).__name__)
        return segments

    @classmethod
    def encode(cls, msg: MessageChain) -> tuple[dict, int]:
        return {
            "contentType": 101,
            "custom": {
                "type": 2,
                "data": base64.b64encode(
                    json.dumps([seg.model_dump() for seg in msg]).encode("utf-8")
                ).decode("utf-8"),
            },
        }, 2


class FishTextNode(FishPayloadNode):
    content_type = 1

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        text_val = payload.get("text", {}).get("text", "")
        return [Text(text=text_val)] if text_val else []

    @classmethod
    def encode(cls, msg: Text) -> tuple[dict, int]:
        return {"contentType": 1, "text": {"text": msg.text}}, 1


class FishItemCardNode(FishPayloadNode):
    content_type = 7

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        item_info = payload.get("itemCard", {}).get("item", {})
        action_info = payload.get("itemCard", {}).get("action", {}).get("page", {})
        if not item_info:
            return []
        return [
            ItemCard(
                item_id=str(item_info.get("itemId", "")),
                title=item_info.get("title", ""),
                price=str(item_info.get("price", "")),
                main_pic=item_info.get("mainPic", ""),
                url=action_info.get("url", ""),
            )
        ]


class FishTradeNode(FishPayloadNode):
    content_type = 26

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        if "dxCard" not in payload:
            return [TradeCard.model_validate(payload)]
        main = payload.get("dxCard", {}).get("item", {}).get("main", {})
        content = main.get("exContent", {})
        button = content.get("button", {})
        args = main.get("clickParam", {}).get("args", {})
        order_ids = set()
        for target in (main.get("targetUrl"), button.get("targetUrl")):
            if not isinstance(target, str):
                continue
            query = parse_qs(urlsplit(target).query, keep_blank_values=True)
            for key in ("bizOrderId", "id"):
                order_ids.update(query.get(key, []))
        if len(order_ids) != 1:
            raise ValueError("交易通知缺少唯一订单号")
        return [
            TradeCard(
                order_id=order_ids.pop(),
                title=content.get("title", ""),
                content=content.get("desc", ""),
                button_text=button.get("text", ""),
                task_id=args.get("task_id", ""),
            )
        ]


class Content(BaseModel):
    """收消息时的总路由"""

    content_type: int = Field(alias="contentType", default=0)
    model_config = {"extra": "allow"}

    def to_message_chain(self) -> MessageChain:
        msg_chain = MessageChain()
        node_class = FishPayloadNode._registry.get(self.content_type)
        if node_class:
            try:
                msg_chain.extend(node_class.decode(self.model_dump(by_alias=True)))
            except (ValueError, TypeError, AttributeError) as exc:
                logger.warning("消息段解析失败: {}", type(exc).__name__)
        return msg_chain


class Sender(BaseModel):
    name: str = Field(alias="reminderTitle", default="")
    user_id: str = Field(alias="senderUserId", default="")
    raw_text: str = Field(alias="reminderContent", default="")

    @field_validator("user_id", mode="before")
    @classmethod
    def normalize_id(cls, value: Any) -> str:
        return str(value).split("@", 1)[0] if value is not None else ""


class MessageBody(BaseModel):
    cid_raw: str = Field(alias="2", default="")
    timestamp: int = Field(alias="5", default=0)
    sender: Sender = Field(alias="10", default_factory=Sender)
    content: Content = Field(alias="6", default_factory=Content)

    def to_events(self, account_id: str, raw_payload: dict) -> Iterator[BaseEvent]:
        messages = MessageChain()
        events: list[BaseEvent] = []
        for segment in self.content.to_message_chain():
            if isinstance(segment, FishEventSegment):
                events.append(segment.to_event(account_id, self))
            else:
                messages.append(segment)
        if messages:
            yield MessageEvent(
                cid=self.cid,
                sender_id=self.sender.user_id,
                sender_name=self.sender.name,
                messages=messages,
                raw_payload=raw_payload,
            )
        yield from events

    @model_validator(mode="before")
    @classmethod
    def extract_sender(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        sender = value.get("10")
        sender = dict(sender) if isinstance(sender, dict) else {}
        identity = value.get("1")
        if not sender.get("senderUserId") and isinstance(identity, dict):
            sender["senderUserId"] = identity.get("1", "")
        sender["reminderTitle"] = (
            sender.get("senderNick") or sender.get("reminderTitle") or ""
        )
        return {**value, "10": sender}

    @property
    def cid(self) -> str:
        return self.cid_raw.split("@")[0] if self.cid_raw else ""

    @field_validator("content", mode="before")
    @classmethod
    def extract_content(cls, v: Any) -> Any:
        if isinstance(v, dict):
            custom = v.get("3")
            candidates = [v]
            if isinstance(custom, dict):
                candidates.extend([custom.get("5"), custom.get("1")])  # pyright: ignore[reportArgumentType]
            for candidate in candidates:
                decoded = load_message_json(candidate)
                if isinstance(decoded, dict) and "contentType" in decoded:
                    return decoded
        return v if isinstance(v, dict) else {}


class RecievedMessagePayload(BaseModel):
    data: MessageBody = Field(alias="1", default_factory=MessageBody)


_ENCODE_MAP = {
    Text: FishTextNode,
    Image: FishImageNode,
    Audio: FishAudioNode,
    SystemTip: FishSystemTipNode,
    MessageChain: FishCustomNode,
}


def auto_encode(message: MessageSegment | MessageChain) -> tuple[dict, int]:
    """发消息时的总路由 (被 adapter.send 调用)"""

    if isinstance(message, MessageChain):
        for segment in message:
            if type(segment) not in _ENCODE_MAP:
                raise ValueError(f"闲鱼适配器不支持发送类型: {type(segment).__name__}")
    node_class = _ENCODE_MAP.get(type(message))

    if node_class and hasattr(node_class, "encode"):
        return node_class.encode(message)

    raise ValueError(f"❌ 闲鱼适配器不支持发送或未注册类型: {type(message).__name__}")
