import base64
import json
import re
from typing import Any, ClassVar, Type

from pydantic import (
    BaseModel,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from myfish.core.logger import logger
from myfish.core.message import Audio, Image, MessageChain, MessageSegment, Text


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


class FishTradeCard(MessageSegment):
    """闲鱼交易动态卡片 (拍下、改价、已付款等)"""

    type: str = "fish_trade_card"
    desc: str = "交易卡片"

    title: str  # 标题，如 "我已拍下，待付款"
    content: str  # 描述，如 "请双方沟通及时确认价格"
    order_id: str  # 订单 ID
    button_text: str  # 按钮文字，如 "修改价格" 或 "已付款"
    task_id: str  # 任务 ID


class FishPayloadNode(BaseModel):
    """
    闲鱼协议节点基类
    """

    _registry: ClassVar[dict[int, Type["FishPayloadNode"]]] = {}

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


class FishTradeCardNode(FishPayloadNode):
    """交易动态卡片(兼容拍下和改价状态)"""

    content_type = 26

    title: str
    content: str
    order_id: str
    button_text: str
    task_id: str

    @model_validator(mode="before")
    @classmethod
    def pre_process(cls, data: Any) -> Any:
        if isinstance(data, dict) and "dxCard" in data:
            main = data.get("dxCard", {}).get("item", {}).get("main", {})
            ex = main.get("exContent", {})
            button = ex.get("button", {})
            args = main.get("clickParam", {}).get("args", {})

            # 从 fleamarket://... 链接中提取订单号
            target_url = main.get("targetUrl", "") or button.get("targetUrl", "")
            order_id_match = re.search(r"(?:id|bizOrderId)=(\d+)", target_url)
            order_id = order_id_match.group(1) if order_id_match else ""

            return {
                "title": ex.get("title", ""),
                "content": ex.get("desc", ""),
                "order_id": order_id,
                "button_text": button.get("text", ""),
                "task_id": args.get("task_id", ""),
            }
        return data

    def to_core(self) -> FishTradeCard:
        return FishTradeCard(
            title=self.title,
            content=self.content,
            order_id=self.order_id,
            button_text=self.button_text,
            task_id=self.task_id,
        )

    @model_serializer
    def serialize(self):
        raise NotImplementedError("FishTradeCard 是只读的，不允许发送")


class FishCustomNode(FishPayloadNode):
    """闲鱼的富媒体/多消息段混合容器"""

    content_type = 101

    @classmethod
    def decode(cls, payload: dict) -> list[MessageSegment]:
        segments = []
        custom_data_b64 = payload.get("custom", {}).get("data", "")
        if custom_data_b64:
            try:
                custom_str = base64.b64decode(custom_data_b64).decode("utf-8")
                for item in json.loads(custom_str):
                    if item.get("type") == "text":
                        segments.append(Text(**item))
                    elif item.get("type") == "image":
                        segments.append(Image(**item))
            except Exception as e:
                logger.error(f"解析自定义富文本消息失败: {e}")
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


class Content(BaseModel):
    """收消息时的总路由"""

    content_type: int = Field(alias="contentType", default=0)
    model_config = {"extra": "allow"}

    def to_message_chain(self) -> MessageChain:
        msg_chain = MessageChain()
        node_class = FishPayloadNode._registry.get(self.content_type)
        if node_class:
            msg_chain.extend(node_class.decode(self.model_dump(by_alias=True)))
        return msg_chain


class Sender(BaseModel):
    name: str = Field(alias="reminderTitle", default="")
    user_id: str = Field(alias="senderUserId", default="")
    raw_text: str = Field(alias="reminderContent", default="")


class MessageBody(BaseModel):
    cid_raw: str = Field(alias="2", default="")
    timestamp: int = Field(alias="5", default=0)
    sender: Sender = Field(alias="10", default_factory=Sender)
    content: Content = Field(alias="6", default_factory=Content)

    @property
    def cid(self) -> str:
        return self.cid_raw.split("@")[0] if self.cid_raw else ""

    @field_validator("content", mode="before")
    @classmethod
    def extract_content(cls, v: Any) -> Any:
        if isinstance(v, dict):
            try:
                inner_payload = v.get("3", {}).get("5")
                if isinstance(inner_payload, str):
                    return json.loads(inner_payload)
            except Exception:
                pass
        return v if isinstance(v, dict) else {}


class RecievedMessagePayload(BaseModel):
    data: MessageBody = Field(alias="1", default_factory=MessageBody)


_ENCODE_MAP = {
    Text: FishTextNode,
    Image: FishImageNode,
    Audio: FishAudioNode,
    SystemTip: FishSystemTipNode,
    FishTradeCard: FishTradeCardNode,
    ItemCard: FishItemCardNode,
    MessageChain: FishCustomNode,
}


def auto_encode(message: MessageSegment | MessageChain) -> tuple[dict, int]:
    """发消息时的总路由 (被 adapter.send 调用)"""

    node_class = _ENCODE_MAP.get(type(message))

    if node_class and hasattr(node_class, "encode"):
        return node_class.encode(message)

    raise ValueError(f"❌ 闲鱼适配器不支持发送或未注册类型: {type(message).__name__}")
