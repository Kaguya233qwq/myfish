import os
from typing import Any, Iterable, Union

from pydantic import BaseModel, Field


class MessageSegment(BaseModel):
    """
    所有消息段的基类
    """

    type: str = "unknown"
    desc: str = "未知"
    extra: dict[str, Any] = Field(default_factory=dict)

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


class Video(MessageSegment):
    type: str = "video"
    desc: str = "视频"
    video_url: str
    thumb_url: str = ""
    width: int = 0
    height: int = 0
    duration_ms: int = 0


class File(MessageSegment):
    """扩展核心：文件消息"""

    type: str = "file"
    desc: str = "文件"
    file_id: str
    file_name: str
    file_size: int = 0

    @property
    def extension(self) -> str:
        """
        获取文件扩展名
        """
        _, ext = os.path.splitext(self.file_name)
        return ext.lstrip(".").lower()

    def is_type(self, *extensions: str) -> bool:
        """
        断言文件类型，支持传入多个后缀名进行匹配
        """
        clean_exts = [e.lstrip(".").lower() for e in extensions]
        return self.extension in clean_exts


class CustomNode(MessageSegment):
    type: str = "node"
    desc: str = "节点消息"
    content: dict[str, Any] = {}


AppendableItem = Union[str, MessageSegment, Iterable[Union[str, MessageSegment]]]


class MessageChain(list[MessageSegment]):
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

    def append(self, item: AppendableItem) -> None:
        """重写 append 方法，支持直接添加字符串或消息段对象"""
        if isinstance(item, str):
            super().append(Text(text=item))
        elif isinstance(item, MessageSegment):
            super().append(item)
        elif isinstance(item, list):
            self.extend(item)
        else:
            raise ValueError(f"不支持的消息片段类型: {type(item)}")

    def extend(self, items: Iterable[Union[str, MessageSegment]]) -> None:
        for item in items:
            self.append(item)

    def __add__(self, other: AppendableItem) -> "MessageChain":  # pyright: ignore[reportIncompatibleMethodOverride]
        result = MessageChain(self)
        result.append(other)
        return result

    def __radd__(self, other: AppendableItem) -> "MessageChain":
        result = MessageChain([other])
        result.extend(self)
        return result

    def __iadd__(self, other: AppendableItem) -> "MessageChain":
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
