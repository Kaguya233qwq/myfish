from typing import Any

from pydantic import BaseModel


class MessageSegment(BaseModel):
    """
    所有消息段的基类
    """

    type: str = "unknown"
    desc: str = "未知"

    def __add__(self, other: Any) -> "MessageChain":
        return MessageChain([self]) + other

    def __radd__(self, other: Any) -> "MessageChain":
        return MessageChain([other]) + self

    @property
    def summary(self) -> str:
        return f"[{self.desc}]" if not isinstance(self, Text) else self.text


class Text(MessageSegment):
    type: str = "text"
    desc: str = "文本"
    text: str


class Image(MessageSegment):
    type: str = "image"
    desc: str = "图片"
    image_url: str
    width: int = 0
    height: int = 0


class Audio(MessageSegment):
    type: str = "audio"
    desc: str = "音频"
    audio_url: str
    duration_ms: int = 0


class CustomNode(MessageSegment):
    type: str = "node"
    desc: str = "节点消息"
    content: dict[str, Any] = {}


class MessageChain(list):
    """
    核心消息链类
    """

    def __init__(self, segments: Any = None):
        super().__init__()
        if segments is None:
            return
        if isinstance(segments, list):
            for seg in segments:
                self.append(seg)
        else:
            self.append(segments)

    def append(self, item: Any) -> None:
        """重写 append 方法，支持直接添加字符串或消息段对象"""
        if isinstance(item, str):
            super().append(Text(text=item))
        elif isinstance(item, MessageSegment):
            super().append(item)
        elif isinstance(item, list):
            self.extend(item)
        else:
            raise ValueError(f"不支持的消息片段类型: {type(item)}")

    def extend(self, items: Any) -> None:
        for item in items:
            self.append(item)

    def __add__(self, other: Any) -> "MessageChain":
        result = MessageChain(self)
        result.append(other)
        return result

    def __radd__(self, other: Any) -> "MessageChain":
        result = MessageChain([other])
        result.extend(self)
        return result

    def __iadd__(self, other: Any) -> "MessageChain":
        self.append(other)
        return self

    @property
    def summary(self) -> str:
        """
        为消息链生成摘要
        """

        pure_text = ""
        for seg in self:
            if isinstance(seg, Text):
                pure_text += seg.text + " "
            elif isinstance(seg, MessageSegment):
                pure_text += f"[{seg.desc}] "
            else:
                pure_text += "[未知消息] "
        display_text = pure_text if pure_text else "(空消息)"
        return display_text.strip()
