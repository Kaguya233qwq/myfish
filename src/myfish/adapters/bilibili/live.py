import asyncio
import base64
import json
import struct
import time
import zlib
from collections.abc import Awaitable, Callable

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException
from websockets.typing import Origin

from myfish.core.logger import logger

from .api import BilibiliAPI, BilibiliAPIError, BilibiliProtocolError
from .events import (
    BilibiliDanmakuEvent,
    BilibiliGiftEvent,
    BilibiliLiveEvent,
    BilibiliLiveInteractionEvent,
    BilibiliLiveStatusEvent,
)
from .model import ApiResponse, LiveDanmaku, LiveGift, LiveInteraction, LivePacket

_HEADER = struct.Struct(">IHHII")
_MAX_PACKET = 16 * 1024 * 1024
_JSON = TypeAdapter(dict[str, JsonValue])


def encode_packet(operation: int, body: bytes = b"", sequence: int = 1) -> bytes:
    return (
        _HEADER.pack(len(body) + _HEADER.size, _HEADER.size, 1, operation, sequence)
        + body
    )


def decode_packets(data: bytes) -> list[LivePacket]:
    remaining = _MAX_PACKET
    packets: list[LivePacket] = []

    def decode(payload: bytes, depth: int) -> None:
        nonlocal remaining
        if depth > 4 or len(payload) > remaining:
            raise BilibiliProtocolError("Live packet exceeds decoding limit")
        remaining -= len(payload)
        offset = 0
        while offset < len(payload):
            if len(payload) - offset < _HEADER.size:
                raise BilibiliProtocolError("Truncated live packet header")
            length, header_size, version, operation, _ = _HEADER.unpack_from(
                payload, offset
            )
            if (
                header_size < _HEADER.size
                or length < header_size
                or offset + length > len(payload)
            ):
                raise BilibiliProtocolError("Invalid live packet length")
            body = payload[offset + header_size : offset + length]
            if version == 2:
                inflater = zlib.decompressobj()
                try:
                    expanded = inflater.decompress(body, remaining + 1)
                except zlib.error:
                    raise BilibiliProtocolError(
                        "Invalid compressed live packet"
                    ) from None
                if (
                    not inflater.eof
                    or inflater.unused_data
                    or len(expanded) > remaining
                ):
                    raise BilibiliProtocolError("Invalid compressed live packet size")
                decode(expanded, depth + 1)
            elif version in (0, 1):
                packets.append(LivePacket(operation, version, body))
            else:
                raise BilibiliProtocolError(
                    f"Unsupported live protocol version: {version}"
                )
            offset += length

    decode(data, 0)
    return packets


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80 and value < 1 << 64:
            return value, offset
    raise ValueError("Invalid protobuf integer")


def _interaction_v2(data: dict[str, JsonValue]) -> LiveInteraction:
    encoded = data.get("pb")
    if not isinstance(encoded, str):
        raise TypeError("Missing interaction protobuf")
    payload = base64.b64decode(encoded, validate=True)
    fields: dict[int, int | bytes] = {}
    offset = 0
    while offset < len(payload):
        tag, offset = _varint(payload, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value, offset = _varint(payload, offset)
            fields[number] = value
        elif wire == 2:
            length, offset = _varint(payload, offset)
            fields[number] = payload[offset : offset + length]
            offset += length
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        else:
            raise ValueError("Unsupported protobuf wire type")
        if offset > len(payload):
            raise ValueError("Truncated interaction protobuf")
    name = fields.get(2, b"")
    if not isinstance(name, bytes) or 5 not in fields:
        raise ValueError("Invalid interaction fields")
    return LiveInteraction.model_validate(
        {
            "uid": fields.get(1, 0),
            "uname": name.decode("utf-8"),
            "msg_type": fields[5],
            "roomid": fields.get(6, 0),
            "timestamp": fields.get(7, 0),
        }
    )


def _timestamp(value: JsonValue, fallback: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value if value >= 1_000_000_000_000 else value * 1000
    return fallback


def live_event(
    account_id: str, room_id: int, payload: dict[str, JsonValue]
) -> BilibiliLiveEvent:
    raw_command = payload.get("cmd")
    command = (
        raw_command.split(":", 1)[0] if isinstance(raw_command, str) else "UNKNOWN"
    )
    common = account_id, room_id, command
    timestamp = _timestamp(payload.get("send_time"), int(time.time() * 1000))
    try:
        if command == "DANMU_MSG":
            message = LiveDanmaku.model_validate(payload)
            return BilibiliDanmakuEvent(
                *common, _timestamp(message.timestamp, timestamp), payload, message
            )
        if command == "SEND_GIFT":
            gift = LiveGift.model_validate(payload.get("data"))
            return BilibiliGiftEvent(
                *common, _timestamp(gift.timestamp, timestamp), payload, gift
            )
        if command in ("INTERACT_WORD", "INTERACT_WORD_V2"):
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("Missing interaction data")
            interaction = (
                _interaction_v2(data)
                if command == "INTERACT_WORD_V2"
                else LiveInteraction.model_validate(data)
            )
            return BilibiliLiveInteractionEvent(
                *common,
                _timestamp(interaction.timestamp, timestamp),
                payload,
                interaction,
            )
        if command in ("LIVE", "PREPARING"):
            timestamp = _timestamp(payload.get("live_time"), timestamp)
            return BilibiliLiveStatusEvent(
                *common, timestamp, payload, command == "LIVE"
            )
    except (TypeError, ValueError):
        pass
    logger.debug(
        "[Bilibili] 未解析的直播事件: cmd={}, fields={}", command, sorted(payload)
    )
    return BilibiliLiveEvent(*common, timestamp, payload)


class LiveClient:
    def __init__(
        self,
        api: BilibiliAPI,
        room_id: int,
        account_id: str,
        callback: Callable[[BilibiliLiveEvent], Awaitable[None]],
    ) -> None:
        self.api = api
        self.room_id = room_id
        self.account_id = account_id
        self.callback = callback
        self._last_heartbeat = 0.0

    async def _dispatch(self, packet: LivePacket, room_id: int) -> None:
        if packet.operation == 3:
            self._last_heartbeat = time.monotonic()
        elif packet.operation == 5:
            try:
                payload = _JSON.validate_json(packet.body)
            except ValidationError:
                raise BilibiliProtocolError("Invalid live command JSON") from None
            await self.callback(live_event(self.account_id, room_id, payload))

    async def _receive(self, socket: ClientConnection, room_id: int) -> None:
        async for frame in socket:
            if not isinstance(frame, bytes):
                raise BilibiliProtocolError("Expected binary live frame")
            for packet in decode_packets(frame):
                await self._dispatch(packet, room_id)

    async def _heartbeat(self, socket: ClientConnection) -> None:
        sequence = 2
        while True:
            if time.monotonic() - self._last_heartbeat >= 60:
                raise BilibiliProtocolError("Live heartbeat timed out")
            await socket.send(encode_packet(2, sequence=sequence))
            sequence += 1
            await asyncio.sleep(30)

    async def _session(self, room_id: int, attempt: int) -> None:
        connection = await self.api.get_live_connection(room_id)
        host = connection.host_list[attempt % len(connection.host_list)]
        auth = {
            "uid": int(self.account_id),
            "roomid": room_id,
            "protover": 2,
            "platform": "web",
            "type": 2,
            "key": connection.token.get_secret_value(),
            "buvid": self.api.cookie_values().get("buvid3", ""),
        }
        async with connect(
            f"wss://{host.host}:{host.wss_port}/sub",
            origin=Origin("https://live.bilibili.com"),
            user_agent_header="Mozilla/5.0",
            ping_interval=None,
            open_timeout=10,
            close_timeout=5,
            max_size=_MAX_PACKET,
        ) as socket:
            await socket.send(encode_packet(7, json.dumps(auth).encode()))
            buffered: list[LivePacket] = []
            authenticated = False
            async with asyncio.timeout(10):
                while not authenticated:
                    frame = await socket.recv()
                    if not isinstance(frame, bytes):
                        raise BilibiliProtocolError(
                            "Expected binary live authentication"
                        )
                    for packet in decode_packets(frame):
                        if packet.operation == 8:
                            try:
                                reply = ApiResponse.model_validate_json(packet.body)
                            except ValidationError:
                                raise BilibiliProtocolError(
                                    "Invalid live authentication reply"
                                ) from None
                            if reply.code != 0:
                                raise BilibiliAPIError(status=101, code=reply.code)
                            authenticated = True
                        else:
                            buffered.append(packet)
                    if len(buffered) > 1000:
                        raise BilibiliProtocolError(
                            "Too many unauthenticated live packets"
                        )
            self._last_heartbeat = time.monotonic()
            logger.info("[Bilibili][直播] 已连接: room={}", room_id)
            tasks = [asyncio.create_task(self._heartbeat(socket))]
            try:
                for packet in buffered:
                    await self._dispatch(packet, room_id)
                tasks.append(asyncio.create_task(self._receive(socket, room_id)))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                raise BilibiliProtocolError("Live connection closed")
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        failures = 0
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                room = await self.api.get_live_room(self.room_id)
                await self._session(room.room_id, attempt)
            except (
                BilibiliAPIError,
                BilibiliProtocolError,
                httpx.TransportError,
                WebSocketException,
                OSError,
                TimeoutError,
            ) as exc:
                if isinstance(exc, BilibiliAPIError) and exc.code == -101:
                    raise
                if time.monotonic() - started >= 60:
                    failures = 0
                delay = min(60, 2 ** min(failures, 6))
                failures += 1
                attempt += 1
                logger.warning(
                    "[Bilibili][直播] 连接中断: room={}, error={}, retry={}s",
                    self.room_id,
                    f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, BilibiliProtocolError)
                    else type(exc).__name__,
                    delay,
                )
                logger.debug("[Bilibili][直播] 连接错误: {}", exc)
                await asyncio.sleep(delay)
