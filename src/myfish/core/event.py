from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Union


from . import message


@dataclass
class MessageEvent:
    """消息事件上下文（Context），封装了当前消息的所有信息及快捷操作"""

    cid: str  # 会话 ID
    sender_id: str  # 发送者 ID
    sender_name: str  # 发送者昵称
    messages: message.MessageChain  # 消息链
    raw_payload: dict[str, Any] = field(default_factory=dict)  # 原始消息载荷

    _callback_func: Callable[[Any], Awaitable[None]] | None = field(
        default=None, repr=False
    )

    async def reply(
        self, msg: Union[str, message.MessageSegment, message.MessageChain]
    ):
        """回复一条消息"""
        if isinstance(msg, str):
            msg = message.Text(text=msg)
        if self._callback_func:
            await self._callback_func(msg)

    @property
    def plain_text(self) -> str:
        """获取消息纯文本"""
        return "".join(
            [seg.text for seg in self.messages if isinstance(seg, message.Text)]
        )

    @property
    def has_image(self) -> bool:
        """判断是否包含图片"""
        return any(isinstance(seg, message.Image) for seg in self.messages)

    @property
    def summary(self) -> str:
        """
        为消息链生成摘要
        """
        pure_text = ""
        for seg in self.messages:
            if isinstance(seg, message.Text):
                pure_text += seg.text + " "
            else:
                desc = getattr(seg, "desc", seg.type)
                pure_text += f"[{desc}] "
        display_text = pure_text if pure_text else "(空消息)"
        return display_text.strip()
