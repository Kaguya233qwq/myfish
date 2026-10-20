from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    """用于状态跟踪的最小快照"""

    model_config = ConfigDict(frozen=True)

    order_id: str
    status: OrderStatus = OrderStatus.UNKNOWN
    raw_status: str = ""
    item_id: str = ""
    buyer_id: str = ""
    buyer_name: str = ""
    amount: str = ""
    seller_rate_status: str = ""
    in_refund: bool = False
    quantity: int = Field(default=1, ge=1, le=10000)

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
        price = item.get("priceVO", {})
        amount = ""
        try:
            total = Decimal(str(price.get("totalPrice", "")))
            if total.is_finite() and total >= 0:
                amount = format(total.quantize(Decimal("0.01")), "f")
        except InvalidOperation:
            pass
        return cls(
            order_id=order_id,
            status=OrderStatus.REFUNDING
            if refunding
            else _STATUS.get(raw, OrderStatus.UNKNOWN),
            raw_status=raw,
            item_id=str(common.get("itemId") or ""),
            buyer_id=str(buyer.get("buyerId") or ""),
            buyer_name=str(buyer.get("userNick") or ""),
            amount=amount,
            seller_rate_status=str(common.get("sellerRateStatus") or ""),
            in_refund=refunding,
            quantity=int(price.get("buyNum", 1)),
        )


class PublishCategory(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    channel_cat_id: str = Field(alias="channelCatId", pattern=r"^[0-9]{1,30}$")
    cat_name: str = Field(alias="catName", min_length=1, max_length=200)
    cat_id: str = Field(default="", alias="catId", pattern=r"^[0-9]{0,30}$")
    tb_cat_id: str = Field(default="", alias="tbCatId", pattern=r"^[0-9]{0,30}$")
    leaf_id: str = Field(default="", alias="leafId", pattern=r"^[0-9]{0,30}$")
    channel_cat1_id: str = Field(
        default="", alias="channelCat1Id", pattern=r"^[0-9]{0,30}$"
    )
    channel_cat2_id: str = Field(
        default="", alias="channelCat2Id", pattern=r"^[0-9]{0,30}$"
    )
    channel_cat3_id: str = Field(
        default="", alias="channelCat3Id", pattern=r"^[0-9]{0,30}$"
    )

    def platform_data(self) -> dict[str, str]:
        return {
            key: value for key, value in self.model_dump(by_alias=True).items() if value
        }
