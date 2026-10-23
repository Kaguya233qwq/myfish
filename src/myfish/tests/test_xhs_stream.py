import asyncio
import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from myfish.adapters.xhs.api import XhsAPI
from myfish.adapters.xhs.message import _field
from myfish.adapters.xhs.model import PushMessage
from myfish.adapters.xhs.stream import XhsStream


class FakeSocket:
    def __init__(self, *, reject=False, push_id="msg"):
        self.frames = []
        self.incoming = asyncio.Queue()
        self.closed = False
        self.reject = reject
        self.push_id = push_id

    async def send(self, text):
        frame = json.loads(text)
        self.frames.append(frame)
        if frame["t"] == 2:
            if len(self.frames) == 1:
                self.incoming.put_nowait(self.push(self.push_id))
            self.incoming.put_nowait(
                json.dumps(
                    {
                        "v": 1,
                        "t": 2,
                        "m": frame["m"],
                        "b": {"a": {"c": 42 if self.reject else 0}},
                    }
                )
            )

    @staticmethod
    def push(identifier="msg"):
        payload = base64.b64encode(
            bytes.fromhex("22100a02696412036d736718012a03616263").replace(
                b"msg", identifier.encode()
            )
        ).decode()
        return json.dumps(
            {"v": 1, "t": 4, "b": {"d": {"biz": "im", "b": [{"d": payload}]}}}
        )

    async def recv(self):
        frame = await self.incoming.get()
        if isinstance(frame, Exception):
            raise frame
        return frame

    async def close(self):
        self.closed = True


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        def handle(request):
            data = (
                {"user_id": "self", "guest": False}
                if request.url.path.endswith("/me")
                else {"a_lt": "private-token", "expired_time": 60}
            )
            return httpx.Response(200, json={"success": True, "code": 0, "data": data})

        return XhsAPI({"a1": "a" * 52}, transport=httpx.MockTransport(handle))

    async def test_authentication_is_ordered_and_early_push_is_preserved(self):
        socket = FakeSocket()
        async with self.client() as api:
            with patch(
                "myfish.adapters.xhs.stream.connect", new=AsyncMock(return_value=socket)
            ):
                async with XhsStream(api) as stream:
                    events = stream.events(reconnects=0)
                    event = await anext(events)
                    self.assertIsInstance(event, PushMessage)
                    self.assertEqual(event.payload, "abc")
                    self.assertEqual([f["b"]["d"]["s"] for f in socket.frames], [0, 1])
                    self.assertEqual(
                        socket.frames[0]["b"]["d"]["b"]["authInfo"]["sid"],
                        "private-token",
                    )
                    await events.aclose()
        self.assertTrue(socket.closed)

    async def test_rejected_authentication_closes_connection(self):
        socket = FakeSocket(reject=True)
        async with self.client() as api:
            with (
                patch(
                    "myfish.adapters.xhs.stream.connect",
                    new=AsyncMock(return_value=socket),
                ),
                self.assertRaisesRegex(ValueError, "rejected"),
            ):
                await XhsStream(api).connect()
        self.assertTrue(socket.closed)
        self.assertEqual(len(socket.frames), 1)

    async def test_reconnect_keeps_message_deduplication(self):
        first, second = FakeSocket(), FakeSocket(push_id="new")
        async with self.client() as api:
            with patch(
                "myfish.adapters.xhs.stream.connect",
                new=AsyncMock(side_effect=[first, second]),
            ):
                async with XhsStream(api) as stream:
                    events = stream.events(reconnects=1)
                    original = await anext(events)
                    self.assertEqual(original.id, "msg")
                    first.incoming.put_nowait(OSError("disconnected"))
                    received = await anext(events)
                    self.assertEqual(received.id, "new")
                    second.incoming.put_nowait(second.push("new"))
                    second.incoming.put_nowait(second.push("end"))
                    final = await anext(events)
                    self.assertEqual(final.id, "end")
                    await events.aclose()
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)

    async def test_send_receipt_does_not_require_consuming_events(self):
        socket = FakeSocket()
        async with self.client() as api:
            with patch(
                "myfish.adapters.xhs.stream.connect", new=AsyncMock(return_value=socket)
            ):
                async with XhsStream(api) as stream:
                    sending = asyncio.create_task(stream.send_text("receiver", "hello"))
                    while len(socket.frames) < 3:
                        await asyncio.sleep(0)
                    identifier = socket.frames[-1]["m"]
                    payload = _field(
                        5, _field(1, identifier) + _field(2, "serverid") + _field(5, 0)
                    )
                    socket.incoming.put_nowait(
                        json.dumps(
                            {"b": {"a": {"b": base64.b64encode(payload).decode()}}}
                        )
                    )
                    ack = await asyncio.wait_for(sending, 1)
                    self.assertEqual(ack.id, "serverid")
                    self.assertEqual(socket.frames[-1]["t"], 3)
                    events = stream.events()
                    self.assertIsInstance(await anext(events), PushMessage)
                    await events.aclose()

    async def test_send_timeout_never_retries(self):
        socket = FakeSocket()
        async with self.client() as api:
            with patch(
                "myfish.adapters.xhs.stream.connect", new=AsyncMock(return_value=socket)
            ):
                async with XhsStream(api) as stream:
                    with self.assertRaises(TimeoutError):
                        await stream.send_text("receiver", "hello", timeout=0.01)
                    self.assertEqual(sum(frame["t"] == 3 for frame in socket.frames), 1)
                    self.assertEqual(stream._receipts, {})
