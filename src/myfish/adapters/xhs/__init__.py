import asyncio
from collections import OrderedDict
from typing import Self

import httpx
from pydantic import JsonValue, ValidationError

from myfish.core.adapter import AdapterMetaData, BaseAdapter
from myfish.core.bot import Bot
from myfish.core.event import MessageEvent
from myfish.core.logger import logger
from myfish.core.message import MessageChain, MessageSegment, Text
from myfish.core.registry import AdapterRegistry

from .api import XhsAPI, XhsAPIError
from .device import DeviceProfile
from .events import notification_event
from .message import decode_content
from .model import (
    IncomingMessage,
    MessageAck,
    Notification,
    NotificationCategory,
    NotificationPush,
    PushMessage,
    XhsConfig,
)
from .stream import XhsStream
from .utils import get_auth_file, json_shape, now_ms

XhsBot = Bot["XhsAdapter"]


@AdapterRegistry.register
class XhsAdapter(BaseAdapter[XhsAPI]):
    meta_data = AdapterMetaData(
        id="xhs",
        name="小红书适配器",
        description="小红书适配器",
        author="Kaguya233qwq",
        version="0.1.0",
    )

    def __init__(
        self, api: XhsAPI, config: XhsConfig, *, needs_login: bool = False
    ) -> None:
        super().__init__(api)
        self.config = config
        self.auth_file = get_auth_file()
        self.state_file = self.auth_file.with_suffix(".state.json")
        self._needs_login = needs_login
        self._running = False
        self._authenticated = False
        self._started_at_ms = now_ms()
        self._send_lock = asyncio.Lock()
        self._stream: XhsStream | None = None
        self._notification_seen: dict[
            NotificationCategory, OrderedDict[tuple[str, int], None]
        ] = {
            "mentions": OrderedDict(),
            "likes": OrderedDict(),
            "connections": OrderedDict(),
        }

    @classmethod
    def setup(cls, **kwargs: object) -> Self:
        config = XhsConfig.model_validate(kwargs)
        state_file = get_auth_file().with_suffix(".state.json")
        if state_file.exists():
            return cls(XhsAPI.from_state(state_file), config)
        api = XhsAPI.create(profile=DeviceProfile.from_environment())
        return cls(api, config, needs_login=True)

    async def send(
        self,
        target_id: str,
        message: MessageSegment | MessageChain,
        cid: str = "",
    ) -> MessageAck:
        chain = (
            MessageChain([message]) if isinstance(message, MessageSegment) else message
        )
        if not chain or any(not isinstance(segment, Text) for segment in chain):
            raise ValueError(
                "XHS sending currently supports nonempty text messages only"
            )
        text = "".join(segment.text for segment in chain if isinstance(segment, Text))
        if not text.strip():
            raise ValueError("Message text must not be empty")
        if not self._running or self._stream is None:
            raise RuntimeError("XHS adapter is not running")
        async with self._send_lock:
            try:
                receipt = await self._stream.send_text(target_id, text)
            except Exception as exc:
                logger.warning(
                    "[XHS][发送失败] -> {}: {}", target_id, type(exc).__name__
                )
                raise
            else:
                logger.info("[XHS][发送] -> {}: {}", target_id, chain.summary)
                return receipt
            finally:
                self.api.save_state(self.state_file)

    async def _handle_message(self, pushed: PushMessage) -> None:
        try:
            message = IncomingMessage.model_validate_json(pushed.payload)
            if (
                message.group_chat
                or message.revoked
                or message.sender_id == self.bot_id
                or message.receiver_id != self.bot_id
            ):
                return
            if (
                not self.config.receive_offline
                and message.timestamp_ms < self._started_at_ms
            ):
                return
            chain = decode_content(message.content)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning("[XHS] 收到无法解析的私信数据，已跳过")
            logger.debug("[XHS] 私信解析失败: error={}", type(exc).__name__)
            return
        if not chain:
            logger.debug("[XHS] 跳过尚未支持的消息类型")
            return
        event = MessageEvent(
            cid=message.sender_id,
            sender_id=message.sender_id,
            sender_name=message.sender_name,
            messages=chain,
            raw_payload=message.model_dump(mode="json", by_alias=True),
        )
        logger.info(
            "[XHS][接收] <- {}({}): {}",
            event.sender_name,
            event.sender_id,
            event.summary,
        )
        if self._on_receive_callback is not None:
            await self._on_receive_callback(event)

    async def _refresh_notifications(
        self, category: NotificationCategory, *, initial: bool = False
    ) -> None:
        seen = self._notification_seen[category]
        pending: dict[tuple[str, int], dict[str, JsonValue]] = {}
        cursor = ""
        cursors = {cursor}
        try:
            while True:
                page = await self.api.get_notifications(category, cursor=cursor)
                reached_seen = False
                for raw in page.message_list:
                    identifier, timestamp = raw.get("id"), raw.get("time")
                    if (
                        not isinstance(identifier, str)
                        or not isinstance(timestamp, int)
                        or isinstance(timestamp, bool)
                    ):
                        logger.debug(
                            "[XHS] 通知标识无法解析: category={}, shape={}",
                            category,
                            json_shape(raw),
                        )
                        continue
                    key = (identifier, timestamp)
                    if key in seen:
                        reached_seen = True
                    else:
                        pending[key] = raw
                if initial or reached_seen or not page.has_more:
                    break
                cursor = str(page.cursor)
                if not page.message_list or not cursor or cursor in cursors:
                    raise ValueError("Notification cursor did not advance")
                cursors.add(cursor)
        except (XhsAPIError, httpx.TransportError, ValueError) as exc:
            logger.warning(
                "[XHS] 通知列表获取失败: category={}, error={}, status={}, code={}",
                category,
                type(exc).__name__,
                exc.status if isinstance(exc, XhsAPIError) else None,
                exc.code if isinstance(exc, XhsAPIError) else None,
            )
            return
        for key, raw in sorted(pending.items(), key=lambda item: item[0][1]):
            kind = raw.get("type")
            event = None
            if kind not in (
                "follow/you",
                "liked/item",
                "liked/comment",
                "comment/item",
                "comment/comment",
            ):
                logger.debug(
                    "[XHS] 未支持的互动通知: category={}, type={!r}, shape={}",
                    category,
                    str(kind)[:128],
                    json_shape(raw),
                )
            else:
                try:
                    notice = Notification.model_validate(raw)
                    event = notification_event(self.bot_id, notice, raw_payload=raw)
                except (ValueError, TypeError) as exc:
                    logger.debug(
                        "[XHS] 互动通知解析失败: category={}, type={!r}, error={}, shape={}",
                        category,
                        kind,
                        type(exc).__name__,
                        json_shape(raw),
                    )
            if (
                event is not None
                and not initial
                and (
                    self.config.receive_offline
                    or (key[1] + 1) * 1000 > self._started_at_ms
                )
            ):
                logger.info(
                    "[XHS][接收事件] {} <- {}({}), notification_id={}",
                    event.event_type,
                    event.user.nickname,
                    event.user.user_id,
                    event.notification_id,
                )
                if self._on_receive_callback is not None:
                    await self._on_receive_callback(event)
            seen[key] = None
            if len(seen) > 4096:
                seen.popitem(last=False)

    async def run(self) -> None:
        async with self.api:
            try:
                if self._needs_login:
                    from .auth import initialize_device, login

                    await initialize_device(self.api)
                    await login(self.api, self.auth_file, current_sign=True)
                    self._needs_login = False
                from .auth import read_with_verification

                account = await read_with_verification(
                    self.api, self.auth_file, self.api.get_self_info
                )
                if account.guest:
                    raise RuntimeError(
                        "XHS credentials belong to a guest; sign in again"
                    )
                self.bot_id = account.user_id
                self._authenticated = True
                self._started_at_ms = now_ms()
                async with XhsStream(self.api) as stream:
                    self._stream = stream
                    self._running = True
                    self.api.save_state(self.state_file)
                    for category in self._notification_seen:
                        await self._refresh_notifications(category, initial=True)
                    self.api.save_state(self.state_file)
                    logger.success("[XHS] 私信和互动通知连接已就绪")
                    async for pushed in stream.events(
                        reconnects=self.config.reconnects
                    ):
                        if isinstance(pushed, PushMessage):
                            await self._handle_message(pushed)
                        elif isinstance(pushed, NotificationPush):
                            for category in pushed.categories:
                                await self._refresh_notifications(category)
                        self.api.save_state(self.state_file)
            finally:
                self._running = False
                self._stream = None
                if self._authenticated:
                    self.api.save_state(self.state_file)
