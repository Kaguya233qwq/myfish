import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from myfish.adapters.fish import FishAdapter, FishBot
from myfish.adapters.fish.api import FishAPI
from myfish.adapters.fish.events import (
    FishOrderDiscoveredEvent,
    FishOrderEvent,
    FishOrderUpdatedEvent,
    FishTradeEvent,
)
from myfish.adapters.fish.model import FishOrder, OrderStatus
from myfish.core.bot import Bot
from myfish.core.engine import Engine
from myfish.core.event import MessageEvent
from myfish.core.message import MessageChain, Text
from myfish.core.plugin import Plugin, PluginMetadata


class EventDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = Engine()
        self.adapter = FishAdapter(AsyncMock(spec=FishAPI))
        self.engine.add_adapter(self.adapter)
        self.plugin = Plugin(
            PluginMetadata(id="orders", name="orders", support_adapters=["fish"])
        )
        self.engine.plugin_manager.plugins["orders"] = self.plugin

    async def drain(self):
        if self.engine._background_tasks:
            await asyncio.gather(*self.engine._background_tasks)

    async def test_discovery_and_update_route_to_their_own_handlers(self):
        calls = []

        @self.plugin.on_event(FishOrderDiscoveredEvent)
        async def discovered(event: FishOrderEvent):
            calls.append(("discovered", event))

        @self.plugin.on_event(FishOrderUpdatedEvent)
        async def updated(event: FishOrderEvent):
            calls.append(("updated", event))

        @self.plugin.on_event(FishOrderEvent)
        async def base(event: FishOrderEvent):
            calls.append(("base", event))

        old = FishOrder(order_id="1", status=OrderStatus.PENDING_SHIP)
        new = old.model_copy(update={"status": OrderStatus.SHIPPED})
        discovery = FishOrderDiscoveredEvent("10", old)
        change = FishOrderUpdatedEvent("10", new, old)
        self.assertIsInstance(discovery, FishOrderEvent)
        self.assertIsInstance(change, FishOrderEvent)
        self.assertNotIsInstance(change, FishOrderDiscoveredEvent)
        self.assertNotIsInstance(discovery, FishOrderUpdatedEvent)
        await self.adapter._on_receive_callback(discovery)
        await self.drain()
        self.assertEqual(calls, [("discovered", discovery)])
        await self.adapter._on_receive_callback(change)
        await self.drain()
        self.assertEqual(calls, [("discovered", discovery), ("updated", change)])
        self.assertEqual(change.previous, old)
        self.assertEqual(change.order, new)

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

        @self.plugin.on_event(FishOrderDiscoveredEvent)
        async def wrong_type(event: MessageEvent):
            calls.append("wrong")

        await self.adapter._on_receive_callback(
            FishOrderDiscoveredEvent("10", FishOrder(order_id="1"))
        )
        await self.drain()
        self.assertEqual(calls, [])
        self.plugin.metadata.support_adapters = ["ilink"]

        @self.plugin.on_event(FishOrderDiscoveredEvent)
        async def wrong_adapter(event):
            calls.append("wrong adapter")

        await self.adapter._on_receive_callback(
            FishOrderDiscoveredEvent("10", FishOrder(order_id="1"))
        )
        await self.drain()
        self.assertEqual(calls, [])

    async def test_plain_json_trade_message_dispatches(self):
        self.adapter.bot_id = "10"
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
        callback = self.adapter._on_receive_callback
        callback.assert_awaited_once()
        self.assertIsInstance(callback.await_args_list[0].args[0], FishTradeEvent)

    async def test_adapter_shutdown_closes_connection_tasks(self):
        self.adapter._connect_ws = AsyncMock()
        with patch(
            "myfish.adapters.fish._ensure_auth",
            new_callable=AsyncMock,
            return_value={"unb": "10"},
        ):
            await self.adapter.run()
        self.assertFalse(hasattr(self.adapter, "order_tracker"))
        self.assertIsNone(self.adapter._active_ws)
        self.adapter.api.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
