"""可选的卖出订单状态跟踪，使用消息触发检查及定时补偿。"""

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from loguru import logger

from myfish.adapters.fish.api import FishAPI
from myfish.adapters.fish.events import (
    FishOrderDiscoveredEvent,
    FishOrderEvent,
    FishOrderUpdatedEvent,
)
from myfish.adapters.fish.model import FishOrder
from myfish.adapters.fish.orders import parse_sold_orders


class OrderTracker:
    def __init__(
        self,
        api: FishAPI,
        account_id: str,
        callback: Callable[[FishOrderEvent], Awaitable[None]] | None = None,
        *,
        state_path: Path | None = None,
        interval: float = 60,
        max_pages: int = 5,
        max_orders: int = 5000,
    ) -> None:
        if (
            not math.isfinite(interval)
            or interval < 10
            or max_pages < 1
            or max_orders < 1
        ):
            raise ValueError(
                "订单跟踪要求 interval >= 10、max_pages >= 1、max_orders >= 1"
            )
        self.api = api
        self.account_id = account_id
        self.callback = callback
        self.state_path = state_path
        self.interval = interval
        self.max_pages = max_pages
        self.max_orders = max_orders
        self.orders: dict[str, FishOrder] = {}
        self.current_orders: dict[str, FishOrder] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._load()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if data.get("account_id") != self.account_id:
            raise ValueError("订单缓存账号不匹配")
        orders = [FishOrder.model_validate(item) for item in data["orders"]]
        self.orders = {order.order_id: order for order in orders[-self.max_orders :]}

    def _save(self, orders: dict[str, FishOrder]) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "account_id": self.account_id,
                    "orders": [
                        order.model_dump(mode="json") for order in orders.values()
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def request_sync(self) -> None:
        """唤醒跟踪器，多条消息合并为一次检查。"""
        self._wake.set()

    async def sync_once(self) -> list[FishOrderEvent]:
        async with self._lock:
            observed: dict[str, FishOrder] = {}
            for page in range(1, self.max_pages + 1):
                response = await self.api.get_sold_orders(page=page)
                orders, has_next = parse_sold_orders(response)
                observed.update({order.order_id: order for order in orders})
                if not has_next:
                    break
            updated = dict(self.orders)
            events: list[FishOrderEvent] = []
            for order_id, order in observed.items():
                previous = self.orders.get(order_id)
                if previous is None:
                    events.append(FishOrderDiscoveredEvent(self.account_id, order))
                elif previous != order:
                    events.append(
                        FishOrderUpdatedEvent(self.account_id, order, previous)
                    )
                updated.pop(order_id, None)
                updated[order_id] = order
            while len(updated) > self.max_orders:
                updated.pop(next(iter(updated)))
            # 所有页面读取成功后才更新，失败不会清空已知状态。
            self._save(updated)
            self.orders = updated
            self.current_orders = observed
            if self.callback:
                for event in events:
                    await self.callback(event)
            return events

    async def run(self) -> None:
        last_sync = 0.0
        while True:
            self._wake.clear()
            # 即使消息密集到达，也限制同账号查询频率。
            delay = max(0, 10 - (time.monotonic() - last_sync))
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.sync_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "订单状态同步失败，保留已有快照；错误类型={}", type(exc).__name__
                )
            last_sync = time.monotonic()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except TimeoutError:
                pass
