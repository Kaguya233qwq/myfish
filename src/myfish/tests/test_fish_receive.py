import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI
from myfish.adapters.fish.events import FishTradeEvent
from myfish.adapters.fish.message import Content
from myfish.core.event import MessageEvent
from myfish.core.message import Image, Text


def b64(value):
    return base64.b64encode(json.dumps(value).encode()).decode()


def payload(content=None, *, sender="20", field="5"):
    if content is None:
        content = {"contentType": 1, "text": {"text": "hello"}}
    return {
        "1": {
            "1": {"1": "20@goofish"},
            "2": "conversation@goofish",
            "10": {"senderUserId": sender, "senderNick": "buyer"},
            "6": {"3": {field: b64(content) if field == "1" else json.dumps(content)}},
        }
    }


def push(*items):
    return json.dumps({"lwp": "/s/sync", "body": {"syncPushPackage": {"data": items}}})


class ReceiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = FishAdapter(AsyncMock(spec=FishAPI))
        self.adapter.bot_id = "10"
        self.callback = AsyncMock()
        self.adapter.set_callback(self.callback)

    async def test_json_base64_and_native_envelopes_reach_callback(self):
        for encoded in (json.dumps(payload()), b64(payload()), payload(), "packed"):
            with self.subTest(encoding=type(encoded).__name__):
                self.callback.reset_mock()
                with patch(
                    "myfish.adapters.fish.decrypt", return_value=payload()
                ) as decode:
                    await self.adapter._handle_raw_message(push({"data": encoded}))
                self.callback.assert_awaited_once()
                event = self.callback.await_args.args[0]
                self.assertEqual(event.messages[0].text, "hello")
                self.assertEqual(event.cid, "conversation")
                self.assertEqual(event.sender_id, "20")
                self.assertEqual(event.sender_name, "buyer")
                self.assertEqual(decode.call_count, int(encoded == "packed"))

    async def test_base64_content_at_alternate_field(self):
        await self.adapter._handle_raw_message(push({"data": payload(field="1")}))
        self.assertEqual(self.callback.await_args.args[0].messages[0].text, "hello")

    async def test_image_content_is_preserved(self):
        content = {
            "contentType": 2,
            "image": {
                "pics": [{"url": "https://example.com/a.jpg", "width": 2, "height": 3}]
            },
        }
        await self.adapter._handle_raw_message(
            push({"data": payload(content, field="1")})
        )
        image = self.callback.await_args.args[0].messages[0]
        self.assertIsInstance(image, Image)
        self.assertEqual(image.image_url, "https://example.com/a.jpg")

    async def test_empty_invalid_and_broken_messages_do_not_drop_rest_of_batch(self):
        with patch(
            "myfish.adapters.fish.decrypt", side_effect=ValueError("private data")
        ):
            await self.adapter._handle_raw_message(
                push(
                    None,
                    {"data": "broken"},
                    {"data": {"1": {"5": {}, "10": {"senderUserId": "20"}}}},
                    {"data": payload({"contentType": 999})},
                    {"data": payload({"contentType": 1, "text": None})},
                    {"data": payload()},
                )
            )
        self.callback.assert_awaited_once()

    async def test_numeric_and_fallback_sender_are_normalized(self):
        for sender in (20, None, "20@goofish"):
            self.callback.reset_mock()
            await self.adapter._handle_raw_message(
                push({"data": payload(sender=sender)})
            )
            self.assertEqual(self.callback.await_args.args[0].sender_id, "20")

    async def test_self_messages_are_not_dispatched(self):
        await self.adapter._handle_raw_message(
            push({"data": payload(sender="10@goofish")})
        )
        self.callback.assert_not_awaited()

    async def test_malformed_frames_do_not_raise_or_ack_responses(self):
        self.adapter._active_ws = AsyncMock()
        for raw in (
            "broken",
            "[]",
            '{"code":200}',
            '{"body":[]}',
            '{"body":{"syncPushPackage":null}}',
        ):
            await self.adapter._handle_raw_message(raw)
        self.assertEqual(self.adapter._active_ws.send.await_count, 2)
        self.callback.assert_not_awaited()

    async def test_trade_card_dispatches_after_unsupported_item(self):
        trade = {
            "contentType": 26,
            "title": "已付款",
            "content": "待发货",
            "order_id": "123",
            "button_text": "发货",
            "task_id": "",
        }
        await self.adapter._handle_raw_message(
            push(
                {"data": payload({"contentType": 999})},
                {"data": payload(trade, field="1")},
            )
        )
        self.callback.assert_awaited_once()
        event = self.callback.await_args.args[0]
        self.assertIsInstance(event, FishTradeEvent)
        self.assertNotIsInstance(event, MessageEvent)
        self.assertEqual(event.order_id, "123")
        self.assertEqual(event.cid, "conversation")
        self.assertEqual(event.account_id, "10")

    def test_custom_messages_accept_platform_object_and_segment_list(self):
        for content in (
            {"contentType": 1, "text": {"text": "hello"}},
            [{"type": "text", "text": "hello"}],
        ):
            chain = Content.model_validate(
                {"contentType": 101, "custom": {"data": b64(content)}}
            ).to_message_chain()
            self.assertIsInstance(chain[0], Text)
            self.assertEqual(chain[0].text, "hello")


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = FishAdapter(AsyncMock(spec=FishAPI))
        self.adapter.api.get_access_token.return_value = "test-token"
        self.adapter.api.device_id = "test-device"
        self.adapter.api.cookies_dict = {}
        self.ws = AsyncMock()
        self.adapter._active_ws = self.ws
        self.sent = []
        self.frames = asyncio.Queue()

        async def send(raw):
            message = json.loads(raw)
            self.sent.append(message)
            if message.get("lwp"):
                await self.frames.put(
                    json.dumps(
                        {"headers": {"mid": message["headers"]["mid"]}, "code": 200}
                    )
                )

        self.ws.send.side_effect = send
        self.ws.recv.side_effect = self.frames.get

    async def test_register_must_succeed_before_sync_is_sent(self):
        original = self.ws.recv.side_effect

        async def recv():
            if len(self.sent) == 1:
                self.assertEqual(self.sent[0]["lwp"], "/reg")
            return await original()

        self.ws.recv.side_effect = recv
        self.assertTrue(await self.adapter._init_connection())
        self.assertEqual(
            [m["lwp"] for m in self.sent], ["/reg", "/r/SyncStatus/ackDiff"]
        )

    async def test_registration_rejection_does_not_send_sync(self):
        self.ws.recv.side_effect = lambda: json.dumps(
            {"headers": {"mid": self.sent[-1]["headers"]["mid"]}, "code": 401}
        )
        self.assertFalse(await self.adapter._init_connection())
        self.assertEqual(len(self.sent), 1)

    async def test_sync_rejection_is_not_reported_as_connected(self):
        async def recv():
            return json.dumps(
                {
                    "headers": {"mid": self.sent[-1]["headers"]["mid"]},
                    "code": 200 if len(self.sent) == 1 else 400,
                }
            )

        self.ws.recv.side_effect = recv
        self.assertFalse(await self.adapter._init_connection())

    async def test_push_during_initialization_is_dispatched(self):
        callback = AsyncMock()
        self.adapter.bot_id = "10"
        self.adapter.set_callback(callback)
        await self.frames.put(push({"data": payload()}))
        self.assertTrue(await self.adapter._init_connection())
        callback.assert_awaited_once()

    async def test_registration_timeout_propagates_for_reconnect(self):
        self.ws.recv.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await self.adapter._init_connection()

    async def test_failed_initialization_reconnects_and_cancels_heartbeat(self):
        self.adapter._init_connection = AsyncMock(side_effect=[False, True])
        started = asyncio.Event()
        stopped = asyncio.Event()
        refresh_stopped = asyncio.Event()

        async def heartbeat():
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        async def messages():
            await started.wait()
            if False:
                yield ""

        async def refresh():
            try:
                await asyncio.Future()
            finally:
                refresh_stopped.set()

        self.adapter._heart_beat_loop = heartbeat
        self.adapter._keep_token_alive_loop = refresh
        self.ws.__aiter__.side_effect = messages
        context = AsyncMock()
        context.__aenter__.return_value = self.ws
        pause_count = 0

        async def pause(_):
            nonlocal pause_count
            pause_count += 1
            if pause_count == 2:
                raise asyncio.CancelledError

        with (
            patch("myfish.adapters.fish.websockets.connect", return_value=context),
            patch("myfish.adapters.fish.asyncio.sleep", side_effect=pause),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.adapter._connect_ws()
        self.assertEqual(self.adapter._init_connection.await_count, 2)
        self.assertTrue(stopped.is_set())
        self.assertTrue(refresh_stopped.is_set())
        self.assertIsNone(self.adapter._active_ws)


if __name__ == "__main__":
    unittest.main()
