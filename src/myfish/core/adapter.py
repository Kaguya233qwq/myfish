from abc import ABC, abstractmethod
from typing import Awaitable, Callable, TypeVar

from .message import MessageChain, MessageSegment


class BaseAPI:
    """API基类"""

    pass


API_T = TypeVar("API_T", bound="BaseAPI")


OnReceiveCallback = Callable[..., Awaitable[None]]
AdapterT = TypeVar("AdapterT", bound="BaseAdapter")


class BaseAdapter(ABC):
    """通信适配器的基类，所有平台的驱动必须继承自这个类并实现其抽象方法

    Adapter 的职责是处理与对应平台服务器的底层通信细节，包括但不限于：
    - 建立和维护与对应平台服务器的连接
    - 接收原始消息数据并进行数据清洗
    - 将解析后的消息封装成标准的MessageEvent对象，并触发 callback 传递给 Bot 分发器
    - 接收 Bot 发出的 Message 对象，将其转换成适合底层通信协议的格式，并发送给平台服务器
    """

    def __init__(self):
        self._on_receive_callback: OnReceiveCallback | None = None

    def set_callback(self, callback: OnReceiveCallback):
        """
        挂载回调函数
        """
        self._on_receive_callback = callback

    @abstractmethod
    async def send(
        self, target_id: str, message: MessageSegment | MessageChain, cid: str = ""
    ):
        """发送消息的抽象接口"""
        pass

    @abstractmethod
    async def run(self):
        """启动底层通信驱动"""
        pass
