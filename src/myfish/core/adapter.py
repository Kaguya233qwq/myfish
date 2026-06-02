from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, TypeVar


from .message import MessageChain, MessageSegment


class BaseAPI:
    """API基类"""

    pass


API_T = TypeVar("API_T", bound="BaseAPI")


OnReceiveCallback = Callable[..., Awaitable[None]]
AdapterT = TypeVar("AdapterT", bound="BaseAdapter[Any]")


@dataclass
class AdapterMetaData:
    """适配器元数据类，包含适配器的基本信息和配置项"""

    id: str
    name: str
    description: str
    author: str
    version: str


class BaseAdapter(ABC, Generic[API_T]):
    """通信适配器的基类，所有平台的驱动必须继承自这个类并实现其抽象方法

    Adapter 的职责是处理与对应平台服务器的底层通信细节，包括但不限于：
    - 建立和维护与对应平台服务器的连接
    - 接收原始消息数据并进行数据清洗
    - 将解析后的消息封装成标准的MessageEvent对象，并触发 callback 传递给 Bot 分发器
    - 接收 Bot 发出的 Message 对象，将其转换成适合底层通信协议的格式，并发送给平台服务器
    """

    meta_data: AdapterMetaData

    def __init__(self, api: API_T):
        self._on_receive_callback: OnReceiveCallback | None = None
        self.api = api
        self.bot_id: str = ""

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
        raise NotImplementedError("适配器必须实现 send 方法进行消息发送")

    @abstractmethod
    async def run(self):
        """启动底层通信驱动"""
        raise NotImplementedError("适配器必须实现 run 方法启动通信驱动")

    @classmethod
    @abstractmethod
    def setup(cls, **kwargs) -> "BaseAdapter":
        """
        适配器组装入口
        所有的第三方适配器必须实现此方法
        接收一个配置字典，实例化 API 对象并返回组装好的 Adapter 实例
        """
        raise NotImplementedError("适配器必须实现 setup 方法进行实例化")
