"""闲鱼订单快照及事件，不将平台交易逻辑引入通用 core。"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar

from myfish.core.event import BaseEvent
from pydantic import BaseModel, ConfigDict


class OrderStatus(str, Enum):
    UNKNOWN = "unknown"
    PENDING_PAYMENT = "pending_payment"
    PENDING_SHIP = "pending_ship"
    SHIPPED = "shipped"
    COMPLETED = "completed"
    CLOSED = "closed"
    REFUNDING = "refunding"
    REFUNDED = "refunded"


_STATUS = {
    "待付款": OrderStatus.PENDING_PAYMENT,
    "待发货": OrderStatus.PENDING_SHIP,
    "已发货": OrderStatus.SHIPPED,
    "待收货": OrderStatus.SHIPPED,
    "交易成功": OrderStatus.COMPLETED,
    "交易关闭": OrderStatus.CLOSED,
    "退款中": OrderStatus.REFUNDING,
    "退款成功": OrderStatus.REFUNDED,
    "已退款": OrderStatus.REFUNDED,
}


class FishOrder(BaseModel):
    """用于状态跟踪的最小快照，不保存收货地址、电话或完整原始响应。"""

    model_config = ConfigDict(frozen=True)

    order_id: str
    status: OrderStatus = OrderStatus.UNKNOWN
    raw_status: str = ""
    item_id: str = ""
    buyer_id: str = ""
    seller_rate_status: str = ""
    in_refund: bool = False

    @property
    def is_rated(self) -> bool:
        return self.seller_rate_status == "4"

    @property
    def can_ship(self) -> bool:
        return self.status == OrderStatus.PENDING_SHIP and not self.in_refund

    @property
    def can_rate(self) -> bool:
        return (
            self.status == OrderStatus.COMPLETED
            and self.seller_rate_status == "5"
            and not self.in_refund
        )

    @classmethod
    def from_sold_item(cls, item: dict[str, Any]) -> "FishOrder":
        common = item.get("commonData", {})
        buyer = item.get("buyerInfoVO", {})
        order_id = str(common.get("orderId") or "")
        if not order_id or not order_id.isascii() or not order_id.isdigit():
            raise ValueError("订单列表中缺少有效 orderId")
        raw = str(common.get("orderStatus") or "")
        refunding = str(common.get("inRefund", "false")).lower() == "true"
        return cls(
            order_id=order_id,
            status=OrderStatus.REFUNDING
            if refunding
            else _STATUS.get(raw, OrderStatus.UNKNOWN),
            raw_status=raw,
            item_id=str(common.get("itemId") or ""),
            buyer_id=str(buyer.get("buyerId") or ""),
            seller_rate_status=str(common.get("sellerRateStatus") or ""),
            in_refund=refunding,
        )


def parse_sold_orders(response: dict) -> tuple[list[FishOrder], bool]:
    """只接受已知列表结构，结构改变时不能当作没有订单。"""
    module = response.get("data", {}).get("module")
    if not isinstance(module, dict) or not isinstance(module.get("items"), list):
        raise ValueError("订单列表响应结构不受支持")  # noqa: TRY004
    next_page = module.get("nextPage", False)
    if next_page not in (True, False, "true", "false"):
        raise ValueError("订单分页标志无效")
    return [FishOrder.from_sold_item(item) for item in module["items"]], str(
        next_page
    ).lower() == "true"


@dataclass
class FishOrderEvent(BaseEvent):
    """新观测到订单，不表示该订单刚刚创建。"""

    event_type: ClassVar[str] = "fish.order.discovered"
    account_id: str
    order: FishOrder


@dataclass
class FishOrderUpdatedEvent(FishOrderEvent):
    event_type: ClassVar[str] = "fish.order.updated"
    previous: FishOrder
