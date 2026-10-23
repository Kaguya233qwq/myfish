from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Self

from pydantic import JsonValue, TypeAdapter
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from myfish.core.logger import logger

from .message import decode_message, encode_text
from .model import MessageAck, NotificationPush, PushMessage
from .utils import compact_json, json_shape, now_ms

if TYPE_CHECKING:
    from .api import XhsAPI

PUSH_URL = "wss://apppush-rws.xiaohongshu.com/rwp"
_FRAME = TypeAdapter(dict[str, JsonValue])


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def decode_frame(
    frame: dict[str, JsonValue],
) -> list[PushMessage | MessageAck | NotificationPush]:
    body = _object(frame.get("b"))
    dispatch = _object(body.get("d"))
    payloads: list[str] = []
    business = dispatch.get("biz")
    data = dispatch.get("b")
    if isinstance(business, str) and business in {"im", "push"}:
        if isinstance(data, str):
            payloads.append(data)
        elif isinstance(data, list):
            for item in data:
                value = _object(item).get("d")
                if isinstance(value, str):
                    payloads.append(value)
                else:
                    logger.debug(
                        "[XHS] 无法解析推送条目: channel={!r}, shape={}",
                        business,
                        json_shape(item),
                    )
        elif data is not None:
            logger.debug(
                "[XHS] 无法解析推送列表: channel={!r}, shape={}",
                business,
                json_shape(data),
            )
    elif business is not None:
        logger.debug(
            "[XHS] 未支持的 WS 业务通道: channel={!r}, shape={}",
            str(business)[:128],
            json_shape(dispatch),
        )
    result: list[PushMessage | MessageAck | NotificationPush] = []
    for payload in payloads:
        try:
            decoded = base64.b64decode(payload, validate=True)
            if business == "push":
                notice = NotificationPush.model_validate_json(decoded)
                you = notice.payload.get("you")
                unknown = isinstance(you, dict) and bool(
                    set(you) - {"total", "title", "mentions", "likes", "connections"}
                )
                if not notice.categories or unknown:
                    logger.debug(
                        "[XHS] 未分发的推送通知: topic={!r}, shape={}",
                        notice.topic[:128],
                        json_shape(notice.payload),
                    )
                result.append(notice)
            else:
                message = decode_message(decoded)
                if message is not None:
                    result.append(message)
                else:
                    logger.debug("[XHS] 未支持的 IM 消息分支: bytes={}", len(decoded))
        except (ValueError, TypeError) as exc:
            logger.debug(
                "[XHS] 推送解析失败: channel={!r}, error={}, shape={}",
                business,
                type(exc).__name__,
                json_shape(dispatch),
            )
    ack = _object(body.get("a")).get("b")
    if isinstance(ack, str):
        try:
            message = decode_message(base64.b64decode(ack, validate=True))
            if message is not None:
                result.append(message)
        except (ValueError, TypeError) as exc:
            logger.debug("[XHS] 推送确认解析失败: error={}", type(exc).__name__)
    return result


def control_frame(state: int, payload: dict[str, JsonValue]) -> tuple[str, str]:
    message_id = uuid.uuid4().hex[:14] + "-" + f"{now_ms():x}"[-11:]
    return message_id, compact_json(
        {
            "v": 1,
            "t": 2,
            "m": message_id,
            "b": {"d": {"a": 1, "s": state, "b": payload}},
        }
    )


class XhsStream:
    def __init__(self, api: XhsAPI) -> None:
        self.api = api
        self._socket: ClientConnection | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._incoming: asyncio.Queue[PushMessage | MessageAck | NotificationPush] = (
            asyncio.Queue(1024)
        )
        self._receipts: dict[str, asyncio.Future[MessageAck]] = {}
        self._user_id = ""
        self._pending: deque[dict[str, JsonValue]] = deque()
        self._seen: set[str] = set()
        self._recent: deque[str] = deque()
        self._closed = False

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _ack(self, message_id: str) -> None:
        socket = self._socket
        if socket is None:
            raise RuntimeError("Push socket is not connected")
        async with asyncio.timeout(10):
            while True:
                frame = _FRAME.validate_json(await socket.recv())
                if frame.get("m") != message_id:
                    if len(self._pending) >= 256:
                        raise ValueError("Push initialization buffer exceeded")
                    self._pending.append(frame)
                    continue
                ack = _object(_object(frame.get("b")).get("a"))
                if ack.get("c") != 0:
                    raise ValueError("Push authentication or registration rejected")
                return

    async def connect(self) -> None:
        await self._disconnect()
        self._closed = False
        account = await self.api.get_self_info()
        if account.guest:
            raise ValueError("Push requires an authenticated account")
        self._user_id = account.user_id
        token = await self.api.get_push_token()
        fields = self.api.fingerprint.fields
        user_agent = fields["x1"]
        if not isinstance(user_agent, str):
            raise TypeError("Device user agent is missing")
        cookies = self.api.cookies_for_url(PUSH_URL.replace("wss://", "https://"))
        self._socket = await connect(
            PUSH_URL,
            origin=Origin("https://www.xiaohongshu.com"),
            additional_headers={
                "User-Agent": user_agent,
                "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items()),
            },
            ping_interval=None,
            open_timeout=20,
            close_timeout=5,
            max_size=4 * 1024 * 1024,
        )
        try:
            message_id, handshake = control_frame(
                0,
                {
                    "appId": "xhs-pc",
                    "authInfo": {
                        "authType": "generic",
                        "sid": token.access_token,
                        "uid": account.user_id,
                        "domain": "red",
                    },
                    "deviceInfo": {
                        "deviceId": self.api.session.tab_device_id,
                        "fingerprint": self.api.session.rwp_fingerprint,
                        "platform": "browser",
                        "os": "web",
                        "osVersion": "10.15",
                        "deviceName": "Chrome",
                        "appVersion": "152.0.0.0",
                        "userAgent": user_agent,
                    },
                    "serviceTag": "",
                    "bizInfos": [
                        {"bizName": "dqa_chatsearch", "serializeType": "protobuf"},
                        {"bizName": "xhs_dots_pc", "serializeType": "protobuf"},
                        {"bizName": "push", "serializeType": "json"},
                    ],
                    "roomInfo": [],
                    "roomInfos": [],
                    "tagInfo": [],
                    "extInfo": {},
                    "state": 1,
                },
            )
            await self._socket.send(handshake)
            await self._ack(message_id)
            message_id, register = control_frame(
                1,
                {
                    "bizInfo": {"bizName": "im", "serializeType": "protobuf"},
                    "register": True,
                },
            )
            await self._socket.send(register)
            await self._ack(message_id)
            self._heartbeat = asyncio.create_task(self._keepalive())
            self._reader = asyncio.create_task(self._read())
            logger.debug("[XHS][WS] 鉴权及通道注册完成")
        except BaseException:
            await self._disconnect()
            raise

    async def _keepalive(self) -> None:
        while self._socket is not None:
            await asyncio.sleep(25)
            socket = self._socket
            if socket is not None:
                await socket.send('{"v":1,"t":0}')

    async def _read(self) -> None:
        socket = self._socket
        if socket is None:
            raise RuntimeError("Push socket is not connected")
        try:
            while True:
                frame = (
                    self._pending.popleft()
                    if self._pending
                    else _FRAME.validate_json(
                        await asyncio.wait_for(socket.recv(), timeout=60)
                    )
                )
                if self._heartbeat is not None and self._heartbeat.done():
                    self._heartbeat.result()
                for message in decode_frame(frame):
                    if isinstance(message, MessageAck):
                        receipt = self._receipts.get(message.client_id)
                        if receipt is not None and not receipt.done():
                            receipt.set_result(message)
                            continue
                    if (
                        isinstance(message, (PushMessage, NotificationPush))
                        and message.id
                    ):
                        identifier = f"{type(message).__name__}:{message.id}"
                        if identifier in self._seen:
                            continue
                        if len(self._recent) >= 4096:
                            self._seen.discard(self._recent.popleft())
                        self._recent.append(identifier)
                        self._seen.add(identifier)
                    self._incoming.put_nowait(message)
        finally:
            for receipt in self._receipts.values():
                if not receipt.done():
                    receipt.set_exception(
                        ConnectionError(
                            "Push connection interrupted; delivery is unknown"
                        )
                    )

    async def send_text(
        self, receiver: str, text: str, *, timeout: float = 20
    ) -> MessageAck:
        socket = self._socket
        if socket is None or self._reader is None or self._reader.done():
            raise ConnectionError("Push socket is not connected")
        if len(text.encode()) > 16384:
            raise ValueError("Message text exceeds 16 KiB")
        client_id = uuid.uuid4().hex
        payload = encode_text(self._user_id, receiver, text, client_id, now_ms())
        receipt = asyncio.get_running_loop().create_future()
        self._receipts[client_id] = receipt
        try:
            async with asyncio.timeout(timeout):
                await socket.send(
                    compact_json(
                        {
                            "v": 1,
                            "t": 3,
                            "m": client_id,
                            "b": {
                                "d": {
                                    "a": 2,
                                    "c": "sendMessage",
                                    "biz": "im",
                                    "b": base64.b64encode(payload).decode(),
                                    "e": {},
                                    "s": "rrmp.b.i",
                                }
                            },
                        }
                    )
                )
                ack = await receipt
            if ack.code != 0:
                raise RuntimeError(f"XHS message rejected (code={ack.code})")
            return ack
        finally:
            self._receipts.pop(client_id, None)
            if not receipt.done():
                receipt.cancel()
            elif not receipt.cancelled():
                receipt.exception()

    async def _next_event(self) -> PushMessage | MessageAck | NotificationPush:
        reader = self._reader
        if reader is None:
            raise ConnectionError("Push socket is not connected")
        if not self._incoming.empty():
            return self._incoming.get_nowait()
        pending = asyncio.create_task(self._incoming.get())
        try:
            done, _ = await asyncio.wait(
                (pending, reader), return_when=asyncio.FIRST_COMPLETED
            )
            if pending in done:
                return pending.result()
            reader.result()
            raise ConnectionError("Push reader stopped")
        finally:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending

    async def events(
        self, *, reconnects: int = 5
    ) -> AsyncIterator[PushMessage | MessageAck | NotificationPush]:
        if reconnects < 0:
            raise ValueError("reconnects must be nonnegative")
        attempts = 0
        while not self._closed:
            try:
                if self._socket is None:
                    await self.connect()
                    if attempts:
                        logger.info("[XHS][WS] 推送连接已恢复")
                message = await self._next_event()
                attempts = 0
                yield message
            except (ConnectionClosed, OSError, TimeoutError) as exc:
                await self._disconnect()
                if self._closed:
                    return
                if attempts >= reconnects:
                    logger.error(
                        "[XHS][WS] 推送连接中断，重试次数已用尽: attempts={}, error={}",
                        attempts,
                        type(exc).__name__,
                    )
                    raise
                delay = min(2**attempts, 30)
                logger.warning(
                    "[XHS][WS] 推送连接中断，{} 秒后重连: attempt={}/{}, error={}",
                    delay,
                    attempts + 1,
                    reconnects,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
                attempts += 1

    async def _disconnect(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed, OSError):
                await reader
        self._pending.clear()
        task, self._heartbeat = self._heartbeat, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionClosed, OSError):
                await task
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close()

    async def close(self) -> None:
        self._closed = True
        await self._disconnect()
