import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from myfish.adapters.bilibili import BilibiliAdapter
from myfish.adapters.bilibili.api import BilibiliAPI, BilibiliAPIError
from myfish.adapters.bilibili.config import BilibiliConfig
from myfish.adapters.bilibili.message import RawMessage, VideoCard, decode_message
from myfish.adapters.bilibili.model import Message
from myfish.adapters.bilibili.sync import MessageStore
from myfish.core.adapter import BaseAdapter
from myfish.core.event import MessageEvent
from myfish.core.message import Image, MessageChain, Text
from myfish.core.registry import AdapterRegistry


def incoming(content='{"content":"I\'m here"}', kind=1):
    return Message(
        sender_uid=2,
        receiver_id=1,
        receiver_type=1,
        msg_type=kind,
        content=content,
        msg_seqno=101,
        msg_key=123,
        timestamp=1000,
    )


class BilibiliAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_registers_standard_adapter_and_rejects_unknown_options(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "myfish.adapters.bilibili.StorageManager.get_adapter_dir",
                return_value=Path(directory),
            ),
        ):
            adapter = AdapterRegistry.build("bilibili", poll_interval=10)
            self.assertIsInstance(adapter, BaseAdapter)
            self.assertEqual(adapter.meta_data.id, "bilibili")
            await adapter.api.close()
            with self.assertRaises(ValueError):
                BilibiliAdapter.setup(auth_file="elsewhere")

    async def test_text_chain_is_sent_as_one_message_and_failure_propagates(self):
        api = BilibiliAPI()
        api.send_text = AsyncMock(side_effect=BilibiliAPIError(status=200, code=-509))
        adapter = BilibiliAdapter(api, BilibiliConfig(), Path("unused"))
        try:
            with self.assertRaises(BilibiliAPIError):
                await adapter.send(
                    "2",
                    MessageChain([Text(text="hello"), Text(text=" world")]),
                    "private:2",
                )
            api.send_text.assert_awaited_once_with(2, "hello world")
            with self.assertRaises(ValueError):
                await adapter.send("2", Image(image_url="https://test.invalid/img"))
            with self.assertRaises(ValueError):
                await adapter.send("2", Text(text="hello"), "private:3")
            self.assertEqual(api.send_text.await_count, 1)
        finally:
            await api.close()

    async def test_dispatch_uses_standard_event_and_only_acknowledges_after_callback(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            store = MessageStore(Path(directory) / "messages.sqlite3", 1)
            api = BilibiliAPI()
            adapter = BilibiliAdapter(api, BilibiliConfig(), Path(directory))
            callback = AsyncMock(side_effect=RuntimeError("handler failed"))
            adapter.set_callback(callback)
            store.stage(2, [incoming()])
            try:
                with self.assertRaises(RuntimeError):
                    await adapter._dispatch(store)
                self.assertEqual(len(store.pending()), 1)
                callback.side_effect = None
                await adapter._dispatch(store)
                event = callback.call_args.args[0]
                self.assertIsInstance(event, MessageEvent)
                self.assertEqual(event.cid, "private:2")
                self.assertEqual(event.sender_id, "2")
                self.assertEqual(event.plain_text, "I'm here")
                self.assertEqual(store.pending(), [])
            finally:
                store.close()
                await api.close()

    async def test_cancellation_closes_client_and_preserves_state(self):
        def handle(request):
            data = (
                {"mid": 1, "uname": "test", "isLogin": True}
                if request.url.path.endswith("/nav")
                else {"session_list": [], "has_more": 0}
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        with tempfile.TemporaryDirectory() as directory:
            api = BilibiliAPI(
                {"SESSDATA": "test", "bili_jct": "csrf", "DedeUserID": "1"},
                transport=httpx.MockTransport(handle),
            )
            adapter = BilibiliAdapter(api, BilibiliConfig(), Path(directory))
            adapter.set_callback(AsyncMock())
            with (
                patch(
                    "myfish.adapters.bilibili.asyncio.sleep",
                    side_effect=asyncio.CancelledError,
                ),
                self.assertRaises(asyncio.CancelledError),
            ):
                await adapter.run()
            self.assertTrue(api._client.is_closed)
            self.assertTrue((Path(directory) / "auth.json").exists())
            self.assertEqual(adapter.bot_id, "1")

    async def test_polling_recovers_from_server_failure_and_enters_active_mode(self):
        discoveries = 0

        def handle(request):
            nonlocal discoveries
            if request.url.path.endswith("/nav"):
                data = {"mid": 1, "uname": "test", "isLogin": True}
            elif request.url.path.endswith("/get_sessions"):
                data = {
                    "session_list": [
                        {
                            "talker_id": 2,
                            "session_type": 1,
                            "session_ts": 3_000_000,
                            "max_seqno": 100,
                        }
                    ],
                    "has_more": 0,
                }
            elif request.url.path.endswith("/new_sessions"):
                discoveries += 1
                if discoveries == 1:
                    return httpx.Response(503)
                data = {
                    "session_list": [
                        {
                            "talker_id": 2,
                            "session_type": 1,
                            "session_ts": 5_000_000,
                            "max_seqno": 101,
                        }
                    ],
                    "has_more": 0,
                }
            else:
                data = {
                    "messages": [incoming().model_dump()],
                    "has_more": 0,
                    "min_seqno": 101,
                    "max_seqno": 101,
                }
            return httpx.Response(200, json={"code": 0, "data": data})

        with tempfile.TemporaryDirectory() as directory:
            api = BilibiliAPI(
                {"SESSDATA": "test", "bili_jct": "csrf", "DedeUserID": "1"},
                transport=httpx.MockTransport(handle),
            )
            adapter = BilibiliAdapter(api, BilibiliConfig(), Path(directory))
            callback = AsyncMock()
            adapter.set_callback(callback)
            with (
                patch(
                    "myfish.adapters.bilibili.asyncio.sleep",
                    side_effect=[None, None, asyncio.CancelledError],
                ) as sleep,
                patch(
                    "myfish.adapters.bilibili.time",
                    SimpleNamespace(monotonic=lambda: 0.0),
                ),
                self.assertRaises(asyncio.CancelledError),
            ):
                await adapter.run()
            callback.assert_awaited_once()
            intervals = [call.args[0] for call in sleep.call_args_list]
            self.assertTrue(20 <= intervals[0] <= 22)
            self.assertTrue(40 <= intervals[1] <= 44)
            self.assertTrue(3 <= intervals[2] <= 3.3)
            store = MessageStore(Path(directory) / "messages.sqlite3", 1)
            try:
                self.assertEqual(store.cursor(2), 101)
                self.assertEqual(store.pending(), [])
            finally:
                store.close()

    async def test_polling_deducts_processing_time_but_preserves_failure_backoff(self):
        for elapsed, active, failed, expected in (
            (2.0, False, False, 3.0),
            (7.0, False, False, 0.0),
            (2.0, True, False, 1.0),
            (7.0, False, True, 10.0),
        ):
            with self.subTest(elapsed=elapsed, active=active, failed=failed):
                await self._assert_poll_delay(elapsed, active, failed, expected)

    async def _assert_poll_delay(
        self, elapsed: float, active: bool, failed: bool, expected: float
    ) -> None:
        now = 0.0

        async def initialize() -> bool:
            nonlocal now
            now += elapsed
            if failed:
                raise httpx.ReadTimeout("timeout")
            return True

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"mid": 1, "uname": "test", "isLogin": True},
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            api = BilibiliAPI(
                {"SESSDATA": "test", "bili_jct": "csrf", "DedeUserID": "1"},
                transport=httpx.MockTransport(handle),
            )
            adapter = BilibiliAdapter(
                api,
                BilibiliConfig(poll_interval=5, notifications=False),
                Path(directory),
            )
            adapter.set_callback(AsyncMock())
            if active:
                adapter._active_until = 100
            with (
                patch(
                    "myfish.adapters.bilibili.time",
                    SimpleNamespace(monotonic=lambda: now),
                ),
                patch("myfish.adapters.bilibili.random.uniform", return_value=1.0),
                patch.object(
                    adapter,
                    "_refresh_login",
                    AsyncMock(return_value=float("inf")),
                ),
                patch(
                    "myfish.adapters.bilibili.MessageSync.initialize",
                    side_effect=initialize,
                ),
                patch(
                    "myfish.adapters.bilibili.asyncio.sleep",
                    side_effect=asyncio.CancelledError,
                ) as sleep,
                self.assertRaises(asyncio.CancelledError),
            ):
                await adapter.run()
            sleep.assert_awaited_once_with(expected)

    def test_receiving_maps_text_images_cards_and_unknown_messages(self):
        self.assertEqual(decode_message(incoming())[0].text, "I'm here")
        self.assertIsInstance(
            decode_message(incoming('{"url":"https://test.invalid/img"}', 2))[0], Image
        )
        card = decode_message(incoming('{"title":"video","bvid":"BVtest"}', 7))[0]
        self.assertIsInstance(card, VideoCard)
        self.assertEqual(card.bvid, "BVtest")
        self.assertIsInstance(decode_message(incoming("not-json", 99))[0], RawMessage)

    def test_polling_intervals_are_validated(self):
        with self.assertRaises(ValueError):
            BilibiliConfig(poll_interval=1, active_interval=3)
        with self.assertRaises(ValueError):
            BilibiliConfig(poll_interval=0)
