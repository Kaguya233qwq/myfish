from typing import Generic
from .adapter import AdapterT
from .message import MessageSegment, MessageChain


class Bot(Generic[AdapterT]):
    """上下文 Bot 实例，负责单个平台的 API 调用。它会被注入到插件的 Handler 中"""

    def __init__(self, adapter: AdapterT):
        self.adapter: AdapterT = adapter

    async def send_msg(
        self, target_id: str, message: MessageSegment | MessageChain, cid: str = ""
    ):
        """纯粹的发送代理"""
        await self.adapter.send(target_id, message, cid)

    @property
    def bot_id(self) -> str:
        """获取当前 Bot 的 ID"""
        return getattr(self.adapter, "bot_id", "unknown")
