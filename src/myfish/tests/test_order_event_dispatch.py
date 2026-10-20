import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from myfish.adapters.fish import FishAdapter, FishBot
from myfish.adapters.fish.orders import (
    FishOrder,
    FishOrderEvent,
    FishOrderUpdatedEvent,
    OrderStatus,
)
from myfish.core.bot import Bot
from myfish.core.engine import Engine
from myfish.core.event import MessageEvent
from myfish.core.message import MessageChain, Text
from myfish.core.plugin import Plugin, PluginMetadata


class EventDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine()
        self.adapter = FishAdapter(AsyncMock())
        self.engine.add_adapter(self.adapter)
        self.plugin = Plugin(
            PluginMetadata(id="orders", name="orders", support_adapters=["fish"])
        )
        self.engine.plugin_manager.plugins["orders"] = self.plugin

    async def drain(self):
        if self.engine._background_tasks:
            await asyncio.gather(*self.engine._background_tasks)

    async def test_order_handlers_are_separate_from_messages_with_bot_injection(self):
        calls = []

        @self.plugin.on_message()
        async def message(event: MessageEvent):
            calls.append("message")

        @self.plugin.on_event(FishOrderUpdatedEvent)
        async def changed(event: FishOrderUpdatedEvent, bot: FishBot):
            self.assertIs(bot.adapter, self.adapter)
            calls.append(event.order.status)

        old = FishOrder(order_id="1", status=OrderStatus.PENDING_SHIP)
        new = old.model_copy(update={"status": OrderStatus.SHIPPED})
        await self.adapter._on_receive_callback(FishOrderUpdatedEvent("10", new, old))
        await self.drain()
        self.assertEqual(calls, [OrderStatus.SHIPPED])

    async def test_message_reply_and_on_event_message_both_work(self):
        @self.plugin.on_message()
        async def respond(event: MessageEvent, bot: Bot[FishAdapter]):
            await event.reply("hello")

        observed = []

        @self.plugin.on_event(MessageEvent)
        async def observe(event: MessageEvent):
            observed.append(event.sender_id)

        self.adapter.send = AsyncMock()
        event = MessageEvent("cid", "20", "buyer", MessageChain([Text(text="hi")]))
        await self.adapter._on_receive_callback(event)
        await self.drain()
        self.assertEqual(observed, ["20"])
        self.adapter.send.assert_awaited_once()

    async def test_wrong_event_annotation_and_adapter_are_skipped(self):
        calls = []

        @self.plugin.on_event(FishOrderEvent)
        async def wrong_type(event: MessageEvent):
            calls.append("wrong")

        await self.adapter._on_receive_callback(
            FishOrderEvent("10", FishOrder(order_id="1"))
        )
        await self.drain()
        self.assertEqual(calls, [])
        self.plugin.metadata.support_adapters = ["ilink"]

        @self.plugin.on_event(FishOrderEvent)
        async def wrong_adapter(event):
            calls.append("wrong adapter")

        await self.adapter._on_receive_callback(
            FishOrderEvent("10", FishOrder(order_id="1"))
        )
        await self.drain()
        self.assertEqual(calls, [])

    async def test_plain_json_trade_message_wakes_tracker_and_dispatches(self):
        self.adapter.bot_id = "10"
        self.adapter.order_tracker = Mock()
        self.adapter._send_ack = AsyncMock()
        self.adapter.set_callback(AsyncMock())
        trade = {
            "contentType": 26,
            "title": "已付款",
            "content": "待发货",
            "order_id": "123",
            "button_text": "发货",
            "task_id": "",
        }
        payload = {
            "1": {
                "2": "cid@goofish",
                "10": {"senderUserId": "20", "reminderTitle": "buyer"},
                "6": {"3": {"5": json.dumps(trade)}},
            }
        }
        message = {
            "body": {"syncPushPackage": {"data": [{"data": json.dumps(payload)}]}}
        }
        await self.adapter._handle_raw_message(json.dumps(message))
        self.adapter.order_tracker.request_sync.assert_called_once()
        self.adapter._on_receive_callback.assert_awaited_once()

    async def test_tracker_tasks_are_closed_when_adapter_stops(self):
        self.adapter.track_orders = True
        self.adapter.api._trade_id = lambda value: value
        self.adapter._connect_ws = AsyncMock()
        runner = AsyncMock()
        tracker = Mock()
        tracker.run = runner
        with (
            patch(
                "myfish.adapters.fish.ensure_auth",
                new_callable=AsyncMock,
                return_value={"unb": "10"},
            ),
            patch("myfish.adapters.fish.OrderTracker", return_value=tracker),
            patch(
                "myfish.core.storage.StorageManager.get_adapter_dir",
                return_value=Path("unused"),
            ),
        ):
            await self.adapter.run()
        self.assertIsNone(self.adapter.order_tracker)
        self.assertEqual(self.adapter._bg_tasks, [])
        self.adapter.api.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
