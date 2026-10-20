from dataclasses import dataclass
from typing import ClassVar

from myfish.core.event import BaseEvent

from .model import FishOrder


@dataclass
class FishTradeEvent(BaseEvent):
    """平台交易动态通知。"""

    event_type: ClassVar[str] = "fish.trade"
    account_id: str
    order_id: str
    cid: str
    sender_id: str
    sender_name: str = ""
    title: str = ""
    content: str = ""
    button_text: str = ""
    task_id: str = ""


@dataclass
class FishOrderEvent(BaseEvent):
    """订单事件公共基类，订阅时使用具体事件类型。"""

    event_type: ClassVar[str] = "fish.order"
    account_id: str
    order: FishOrder


@dataclass
class FishOrderDiscoveredEvent(FishOrderEvent):
    """首次发现订单的事件。"""

    event_type: ClassVar[str] = "fish.order.discovered"


@dataclass
class FishOrderUpdatedEvent(FishOrderEvent):
    """订单状态更新事件"""

    event_type: ClassVar[str] = "fish.order.updated"
    previous: FishOrder
