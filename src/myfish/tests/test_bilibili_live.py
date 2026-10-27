import asyncio
import base64
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from websockets.asyncio.client import ClientConnection

from myfish.adapters.bilibili import BilibiliAdapter
from myfish.adapters.bilibili.api import (
    BilibiliAPI,
    BilibiliAPIError,
    BilibiliProtocolError,
)
from myfish.adapters.bilibili.config import BilibiliConfig
from myfish.adapters.bilibili.events import (
    BilibiliDanmakuEvent,
    BilibiliGiftEvent,
    BilibiliLiveEvent,
    BilibiliLiveInteractionEvent,
    BilibiliLiveStatusEvent,
)
from myfish.adapters.bilibili.live import (
    LiveClient,
    decode_packets,
    encode_packet,
    live_event,
)
from myfish.adapters.bilibili.model import LiveConnection, LivePacket, LiveRoom


def packet(operation: int, body: bytes, version: int = 1) -> bytes:
    return struct.pack(">IHHII", len(body) + 16, 16, version, operation, 1) + body


def danmaku():
    return {
        "cmd": "DANMU_MSG:4:0:2:2:2:0",
        "info": [[0, 1, 25, 0xFFFFFF, 1700000000000], "hello", [2, "test"]],
    }


def connection():
    return LiveConnection.model_validate(
        {
            "token": "test-token",
            "host_list": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}],
        }
    )


class BilibiliLiveProtocolTests(unittest.TestCase):
    def test_combined_and_nested_compressed_packets(self):
        command = encode_packet(5, json.dumps(danmaku()).encode())
        heartbeat = encode_packet(3, struct.pack(">I", 100))
        compressed = packet(5, zlib.compress(command + heartbeat), 2)
        result = decode_packets(packet(5, zlib.compress(compressed), 2))
        self.assertEqual([item.operation for item in result], [5, 3])
        self.assertEqual(json.loads(result[0].body), danmaku())
        self.assertEqual(result[1].body, struct.pack(">I", 100))

    def test_invalid_and_truncated_packets_are_rejected(self):
        for payload in (
            b"short",
            struct.pack(">IHHII", 0, 16, 1, 5, 1),
            struct.pack(">IHHII", 100, 16, 1, 5, 1),
            packet(5, b"bad", 2),
            packet(5, b"bad", 3),
        ):
            with (
                self.subTest(payload=payload),
                self.assertRaises(BilibiliProtocolError),
            ):
                decode_packets(payload)

    def test_decompression_size_and_depth_are_bounded(self):
        expanded = encode_packet(5, b"x" * 2000)
        with (
            patch("myfish.adapters.bilibili.live._MAX_PACKET", 512),
            self.assertRaises(BilibiliProtocolError),
        ):
            decode_packets(packet(5, zlib.compress(expanded), 2))
        nested = encode_packet(5, b"{}")
        for _ in range(6):
            nested = packet(5, zlib.compress(nested), 2)
        with self.assertRaises(BilibiliProtocolError):
            decode_packets(nested)

    def test_danmaku_suffix_and_masked_sender_are_supported(self):
        data = danmaku()
        data["info"][2][0] = 0
        event = live_event("1", 100, data)
        self.assertIsInstance(event, BilibiliDanmakuEvent)
        self.assertEqual(event.danmaku.text, "hello")
        self.assertEqual(event.danmaku.uid, 0)
        self.assertEqual(event.timestamp_ms, 1700000000000)
        self.assertEqual(event.room_id, 100)

    def test_gift_and_status_events_have_typed_content(self):
        event = live_event(
            "1",
            100,
            {
                "cmd": "SEND_GIFT",
                "data": {
                    "uid": 2,
                    "uname": "test",
                    "giftId": 1,
                    "giftName": "test-gift",
                    "num": 2,
                    "timestamp": 1700000000,
                },
            },
        )
        self.assertIsInstance(event, BilibiliGiftEvent)
        self.assertEqual(event.gift.num, 2)
        self.assertEqual(event.timestamp_ms, 1700000000000)
        for command, status in (("LIVE", True), ("PREPARING", False)):
            event = live_event("1", 100, {"cmd": command, "send_time": 1700000000000})
            self.assertIsInstance(event, BilibiliLiveStatusEvent)
            self.assertEqual(event.live, status)

    def test_both_interaction_versions_have_sender_and_kind(self):
        protobuf = b"\x08\x02\x12\x04test\x28\x02\x30\x64\x38\x03"
        modern = {
            "cmd": "INTERACT_WORD_V2",
            "data": {"pb": base64.b64encode(protobuf).decode()},
        }
        legacy = {
            "cmd": "INTERACT_WORD",
            "data": {"uid": 2, "uname": "test", "msg_type": 2, "timestamp": 3},
        }
        for data in (modern, legacy):
            with self.subTest(command=data["cmd"]):
                event = live_event("1", 100, data)
                self.assertIsInstance(event, BilibiliLiveInteractionEvent)
                self.assertEqual(event.interaction.kind, "follow")
                self.assertEqual(event.interaction.uid, 2)
                self.assertEqual(event.timestamp_ms, 3000)

    def test_unknown_and_malformed_events_preserve_raw_payload(self):
        for data in (
            {"cmd": "FUTURE", "data": {"future": True}},
            {"cmd": "DANMU_MSG", "info": []},
            {"cmd": "INTERACT_WORD_V2", "data": {"pb": "invalid"}},
            {
                "cmd": "INTERACT_WORD_V2",
                "data": {"pb": base64.b64encode(b"\x12\x7fshort").decode()},
            },
        ):
            with self.subTest(data=data):
                event = live_event("1", 100, data)
                self.assertEqual(type(event), BilibiliLiveEvent)
                self.assertEqual(event.raw_payload, data)

    def test_room_configuration_is_positive_and_deduplicated(self):
        self.assertEqual(
            BilibiliConfig(live_rooms=[100, 100, 200]).live_rooms, [100, 200]
        )
        with self.assertRaises(ValueError):
            BilibiliConfig(live_rooms=[0])


class BilibiliLiveClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = MagicMock(spec=BilibiliAPI)
        self.api.cookie_values.return_value = {"buvid3": "test-device"}
        self.api.get_live_connection.return_value = connection()
        self.api.get_live_room.return_value = LiveRoom(
            room_id=100, uid=2, live_status=1
        )
        self.callback = AsyncMock()
        self.client = LiveClient(self.api, 1, "3", self.callback)

    async def test_authentication_and_coalesced_events_are_dispatched(self):
        socket = AsyncMock(spec=ClientConnection)
        socket.recv.return_value = encode_packet(8, b'{"code":0}') + encode_packet(
            5, json.dumps(danmaku()).encode()
        )
        socket.__aiter__.return_value = []
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=socket)
        context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("myfish.adapters.bilibili.live.connect", return_value=context),
            self.assertRaises(BilibiliProtocolError),
        ):
            await self.client._session(100, 0)
        auth_packet = decode_packets(socket.send.call_args_list[0].args[0])[0]
        auth = json.loads(auth_packet.body)
        self.assertEqual(auth_packet.operation, 7)
        self.assertEqual(auth["roomid"], 100)
        self.assertEqual(auth["uid"], 3)
        self.assertEqual(auth["buvid"], "test-device")
        self.assertEqual(auth["protover"], 2)
        self.assertEqual(auth["key"], "test-token")
        self.callback.assert_awaited_once()
        self.assertIsInstance(self.callback.call_args.args[0], BilibiliDanmakuEvent)
        context.__aexit__.assert_awaited_once()

    async def test_failed_authentication_never_dispatches_buffered_messages(self):
        socket = AsyncMock(spec=ClientConnection)
        socket.recv.return_value = encode_packet(
            5, json.dumps(danmaku()).encode()
        ) + encode_packet(8, b'{"code":-101}')
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=socket)
        context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch("myfish.adapters.bilibili.live.connect", return_value=context),
            self.assertRaises(BilibiliAPIError),
        ):
            await self.client._session(100, 0)
        self.callback.assert_not_awaited()

    async def test_disconnect_resolves_room_again_and_retries_with_backoff(self):
        with (
            patch.object(
                self.client,
                "_session",
                side_effect=[
                    BilibiliProtocolError("disconnect"),
                    asyncio.CancelledError,
                ],
            ) as session,
            patch(
                "myfish.adapters.bilibili.live.asyncio.sleep", new_callable=AsyncMock
            ) as sleep,
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.client.run()
        self.assertEqual(session.await_count, 2)
        self.assertEqual(session.call_args_list[0].args, (100, 0))
        self.assertEqual(session.call_args_list[1].args, (100, 1))
        sleep.assert_awaited_once_with(1)

    async def test_expired_login_is_not_retried(self):
        with (
            patch.object(
                self.client,
                "_session",
                side_effect=BilibiliAPIError(status=101, code=-101),
            ),
            patch(
                "myfish.adapters.bilibili.live.asyncio.sleep", new_callable=AsyncMock
            ) as sleep,
            self.assertRaises(BilibiliAPIError),
        ):
            await self.client.run()
        sleep.assert_not_awaited()

    async def test_heartbeat_reply_updates_deadline_and_missing_reply_times_out(self):
        with patch(
            "myfish.adapters.bilibili.live.time",
            SimpleNamespace(monotonic=lambda: 10.0),
        ):
            await self.client._dispatch(LivePacket(3, 1, b"\x00\x00\x00\x01"), 100)
        self.assertEqual(self.client._last_heartbeat, 10)
        socket = AsyncMock(spec=ClientConnection)
        with (
            patch(
                "myfish.adapters.bilibili.live.time",
                SimpleNamespace(monotonic=lambda: 70.0),
            ),
            self.assertRaises(BilibiliProtocolError),
        ):
            await self.client._heartbeat(socket)
        socket.send.assert_not_awaited()

    async def test_heartbeat_uses_protocol_packets_and_increasing_sequence(self):
        socket = AsyncMock(spec=ClientConnection)
        self.client._last_heartbeat = 10.0
        with (
            patch(
                "myfish.adapters.bilibili.live.time",
                SimpleNamespace(monotonic=lambda: 10.0),
            ),
            patch(
                "myfish.adapters.bilibili.live.asyncio.sleep",
                side_effect=[None, asyncio.CancelledError],
            ) as sleep,
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.client._heartbeat(socket)
        self.assertEqual(
            [
                struct.unpack(">IHHII", call.args[0])[3:]
                for call in socket.send.call_args_list
            ],
            [(2, 2), (2, 3)],
        )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30, 30])

    async def test_adapter_forwards_live_events_to_standard_callback(self):
        async with BilibiliAPI() as api:
            adapter = BilibiliAdapter(api, BilibiliConfig(), Path("unused"))
            adapter.set_callback(self.callback)
            event = live_event("1", 100, danmaku())
            await adapter._receive_live(event)
        self.callback.assert_awaited_once_with(event)

    async def test_adapter_shutdown_cancels_live_listener(self):
        def handle(request):
            data = (
                {"mid": 1, "uname": "test", "isLogin": True}
                if request.url.path.endswith("/nav")
                else {"session_list": [], "has_more": 0}
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        cancelled = False

        async def listen():
            nonlocal cancelled
            try:
                await asyncio.Event().wait()
            finally:
                cancelled = True

        with tempfile.TemporaryDirectory() as directory:
            api = BilibiliAPI(
                {"SESSDATA": "test", "bili_jct": "test", "DedeUserID": "1"},
                transport=httpx.MockTransport(handle),
            )
            adapter = BilibiliAdapter(
                api,
                BilibiliConfig(live_rooms=[100], notifications=False),
                Path(directory),
            )
            adapter.set_callback(AsyncMock())
            with (
                patch("myfish.adapters.bilibili.LiveClient") as client,
                patch(
                    "myfish.adapters.bilibili.asyncio.sleep",
                    side_effect=asyncio.CancelledError,
                ),
                self.assertRaises(asyncio.CancelledError),
            ):
                client.return_value.run = listen
                await adapter.run()
            self.assertTrue(cancelled)
            self.assertTrue(api._client.is_closed)


class BilibiliLiveAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_room_resolution_and_signed_connection_request(self):
        requests = []

        def handle(request):
            requests.append(request)
            data = (
                {"room_id": 100, "short_id": 1, "uid": 2, "live_status": 1}
                if request.url.path.endswith("/room_init")
                else {
                    "token": "test-token",
                    "host_list": [
                        {"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}
                    ],
                }
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        async with BilibiliAPI(
            {"buvid3": "test"}, transport=httpx.MockTransport(handle)
        ) as api:
            room = await api.get_live_room(1)
            with patch.object(
                api, "_wbi_params", return_value={"id": "100", "w_rid": "signature"}
            ):
                result = await api.get_live_connection(room.room_id)
        self.assertEqual(room.room_id, 100)
        self.assertEqual(requests[0].url.params["id"], "1")
        self.assertEqual(requests[1].url.params["w_rid"], "signature")
        self.assertEqual(result.host_list[0].wss_port, 443)

    async def test_danmaku_sending_requires_csrf_and_returns_receipt(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"code": 0, "data": {"mode_info": {"mode": 1}}}
            )

        async with BilibiliAPI(
            {"bili_jct": "csrf"}, transport=httpx.MockTransport(handle)
        ) as api:
            receipt = await api.send_live_danmaku(100, "hello", color=0x123456)
        form = httpx.QueryParams(requests[0].content.decode())
        self.assertEqual(requests[0].url.path, "/msg/send")
        self.assertEqual(form["csrf"], "csrf")
        self.assertEqual(form["csrf_token"], "csrf")
        self.assertEqual(form["color"], str(0x123456))
        self.assertEqual(form["msg"], "hello")
        self.assertEqual(receipt.mode_info["mode"], 1)

    async def test_send_failure_is_propagated_without_retry(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 10031})

        async with BilibiliAPI(
            {"bili_jct": "csrf"}, transport=httpx.MockTransport(handle)
        ) as api:
            with self.assertRaises(BilibiliAPIError) as raised:
                await api.send_live_danmaku(100, "hello")
        self.assertEqual(raised.exception.code, 10031)
        self.assertEqual(len(requests), 1)

    async def test_empty_send_response_is_not_reported_as_success(self):
        async with BilibiliAPI(
            {"bili_jct": "csrf"},
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"code": 0, "data": None})
            ),
        ) as api:
            with self.assertRaises(BilibiliProtocolError):
                await api.send_live_danmaku(100, "hello")

    async def test_invalid_live_parameters_never_issue_requests(self):
        def handle(_):
            self.fail("Invalid parameters issued a request")

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            for method, args, kwargs in (
                (api.get_live_room, (0,), {}),
                (api.get_live_connection, (0,), {}),
                (api.send_live_danmaku, (100, " "), {}),
                (api.send_live_danmaku, (100, "test"), {"color": -1}),
                (api.send_live_danmaku, (100, "test"), {}),
            ):
                with (
                    self.subTest(method=method.__name__),
                    self.assertRaises(ValueError),
                ):
                    await method(*args, **kwargs)
