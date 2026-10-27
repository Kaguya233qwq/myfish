from pydantic import Field, JsonValue, TypeAdapter

from myfish.core.logger import logger
from myfish.core.message import Image, MessageChain, MessageSegment, Text

from .model import Message

_OBJECT = TypeAdapter(dict[str, JsonValue])


class VideoCard(MessageSegment):
    type: str = "bilibili_video"
    desc: str = "B站视频卡片"
    title: str = ""
    bvid: str = ""
    thumb: str = ""
    content: dict[str, JsonValue] = Field(default_factory=dict, repr=False)


class RawMessage(MessageSegment):
    type: str = "bilibili_raw"
    desc: str = "未识别的B站消息"
    msg_type: int
    content: str = Field(repr=False)


def decode_message(message: Message) -> MessageChain:
    try:
        content = _OBJECT.validate_json(message.content)
    except ValueError:
        content = {}
    if message.msg_type == 1 and isinstance(content.get("content"), str):
        return MessageChain(Text(text=str(content["content"])))
    if message.msg_type == 2 and isinstance(content.get("url"), str):
        return MessageChain(Image(image_url=str(content["url"])))
    if message.msg_type == 7:
        return MessageChain(
            VideoCard(
                title=str(content["title"])
                if isinstance(content.get("title"), str)
                else "",
                bvid=str(content["bvid"])
                if isinstance(content.get("bvid"), str)
                else "",
                thumb=str(content["thumb"])
                if isinstance(content.get("thumb"), str)
                else "",
                content=content,
            )
        )
    logger.debug(
        "[Bilibili] 无法解析消息: type={}, fields={}", message.msg_type, sorted(content)
    )
    return MessageChain(RawMessage(msg_type=message.msg_type, content=message.content))
