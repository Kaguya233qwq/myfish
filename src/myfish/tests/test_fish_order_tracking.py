import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from myfish.adapters.fish.api import FishAPIError
from myfish.adapters.fish.events import FishOrderDiscoveredEvent, FishOrderUpdatedEvent
from myfish.adapters.fish.model import FishOrder, OrderStatus
from myfish.adapters.fish.orders import parse_sold_orders
from myfish.builtin_plugins.fishy.tracking import OrderTracker


def sold_item(order_id="1", status="待发货", **extra):
    return {
        "commonData": {"orderId": order_id, "orderStatus": status, **extra},
        "buyerInfoVO": {"buyerId": "2", "phone": "private", "address": "private"},
    }


def sold_page(*items, next_page=False):
    return {
        "ret": ["SUCCESS::ok"],
        "data": {"module": {"items": list(items), "nextPage": next_page}},
    }


class OrderTrackingTests(unittest.IsolatedAsyncioTestCase):
    def test_order_amount_and_buyer_name_are_read_from_platform(self):
        item = sold_item()
        item["buyerInfoVO"]["userNick"] = "测试买家"
        item["priceVO"] = {"totalPrice": "19.90", "buyNum": 2}
        order = FishOrder.from_sold_item(item)
        self.assertEqual(order.amount, "19.90")
        self.assertEqual(order.buyer_name, "测试买家")
        self.assertEqual(order.buyer_id, "2")
        self.assertEqual(order.quantity, 2)
        for value, expected in (
            ("0", "0.00"),
            ("0.29", "0.29"),
            (None, ""),
            ("NaN", ""),
            ("-1", ""),
            ("invalid", ""),
        ):
            item["priceVO"]["totalPrice"] = value
            self.assertEqual(FishOrder.from_sold_item(item).amount, expected)

    async def asyncSetUp(self):
        self.api = AsyncMock()
        self.callback = AsyncMock()
        self.tracker = OrderTracker(self.api, "10", self.callback)

    async def test_discovery_changes_and_duplicates(self):
        self.api.get_sold_orders.return_value = sold_page(sold_item())
        first = await self.tracker.sync_once()
        self.assertIsInstance(first[0], FishOrderDiscoveredEvent)
        self.assertEqual(first[0].event_type, "fish.order.discovered")
        self.assertEqual(await self.tracker.sync_once(), [])
        self.api.get_sold_orders.return_value = sold_page(sold_item(status="已发货"))
        changes = await self.tracker.sync_once()
        self.assertIsInstance(changes[0], FishOrderUpdatedEvent)
        self.assertEqual(changes[0].previous.status, OrderStatus.PENDING_SHIP)
        self.assertEqual(changes[0].order.status, OrderStatus.SHIPPED)
        self.assertEqual(self.callback.await_count, 2)

    async def test_ratings_and_refunds_change_without_new_trade_status(self):
        self.api.get_sold_orders.return_value = sold_page(
            sold_item(status="交易成功", sellerRateStatus="5")
        )
        await self.tracker.sync_once()
        self.assertTrue(self.tracker.orders["1"].can_rate)
        self.api.get_sold_orders.return_value = sold_page(
            sold_item(status="交易成功", sellerRateStatus="4")
        )
        self.assertEqual(len(await self.tracker.sync_once()), 1)
        self.assertTrue(self.tracker.orders["1"].is_rated)
        self.api.get_sold_orders.return_value = sold_page(
            sold_item(status="交易成功", sellerRateStatus="4", inRefund="true")
        )
        await self.tracker.sync_once()
        self.assertEqual(self.tracker.orders["1"].status, OrderStatus.REFUNDING)
        self.assertFalse(self.tracker.orders["1"].can_rate)

    async def test_partial_page_failure_does_not_commit_or_notify(self):
        original = FishOrder.from_sold_item(sold_item())
        self.tracker.orders = {"1": original}
        self.api.get_sold_orders.side_effect = [
            sold_page(sold_item(status="已发货"), next_page="true"),
            FishAPIError("failed"),
        ]
        with self.assertRaises(FishAPIError):
            await self.tracker.sync_once()
        self.assertEqual(self.tracker.orders["1"], original)
        self.callback.assert_not_awaited()

    async def test_pagination_and_maximum_cache_size(self):
        self.tracker.max_orders = 2
        self.tracker.max_pages = 2
        self.api.get_sold_orders.side_effect = [
            sold_page(sold_item("1"), next_page=True),
            sold_page(sold_item("2"), sold_item("3"), next_page=True),
        ]
        events = await self.tracker.sync_once()
        self.assertEqual(len(events), 3)
        self.assertEqual(len(self.tracker.orders), 2)
        self.assertEqual(self.api.get_sold_orders.await_count, 2)

    async def test_persistence_restart_and_account_separation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.json"
            tracker = OrderTracker(self.api, "10", self.callback, state_path=path)
            self.api.get_sold_orders.return_value = sold_page(sold_item())
            await tracker.sync_once()
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("private", saved)
            restarted = OrderTracker(self.api, "10", self.callback, state_path=path)
            self.assertEqual(await restarted.sync_once(), [])
            with self.assertRaises(ValueError):
                OrderTracker(self.api, "11", self.callback, state_path=path)
            self.assertFalse(path.with_suffix(".tmp").exists())

    async def test_run_can_be_cancelled(self):
        synced = asyncio.Event()

        async def complete():
            synced.set()

        self.tracker.sync_once = AsyncMock(side_effect=complete)
        task = asyncio.create_task(self.tracker.run())
        await asyncio.wait_for(synced.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_lock_coalesces_duplicate_observations(self):
        self.api.get_sold_orders.return_value = sold_page(sold_item())
        await asyncio.gather(self.tracker.sync_once(), self.tracker.sync_once())
        self.callback.assert_awaited_once()

    def test_unknown_status_and_refund_closed_are_not_cancelled_orders(self):
        order = FishOrder.from_sold_item(sold_item(status="退款关闭"))
        self.assertEqual(order.status, OrderStatus.UNKNOWN)
        self.assertEqual(order.raw_status, "退款关闭")
        self.assertFalse(order.can_ship)

    def test_bad_schema_is_not_empty_success(self):
        for response in (
            {},
            {"data": {"module": {"items": None}}},
            sold_page({"commonData": {}}),
            sold_page(sold_item(), next_page="maybe"),
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                parse_sold_orders(response)


if __name__ == "__main__":
    unittest.main()
