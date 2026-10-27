import asyncio
import random
import time
from pathlib import Path
from typing import Self

import httpx

from myfish.core.adapter import AdapterMetaData, BaseAdapter
from myfish.core.bot import Bot
from myfish.core.event import MessageEvent
from myfish.core.logger import logger
from myfish.core.message import MessageChain, MessageSegment, Text
from myfish.core.registry import AdapterRegistry
from myfish.core.storage import StorageManager

from .api import BilibiliAPI, BilibiliAPIError, BilibiliProtocolError
from .auth import load_state, login, refresh_credentials, save_state
from .config import BilibiliConfig
from .events import (
    BilibiliDanmakuEvent,
    BilibiliLikedEvent,
    BilibiliLiveEvent,
    BilibiliMentionEvent,
    notification_event,
)
from .live import LiveClient
from .message import decode_message
from .model import NotificationCategory, SendReceipt
from .sync import MessageStore, MessageSync, NotificationSync

BilibiliBot = Bot["BilibiliAdapter"]


@AdapterRegistry.register
class BilibiliAdapter(BaseAdapter[BilibiliAPI]):
    meta_data = AdapterMetaData(
        id="bilibili",
        name="Bilibili适配器",
        description="B站私信、账号通知与直播适配器",
        author="Kaguya233qwq",
        version="0.1.0",
    )

    def __init__(
        self, api: BilibiliAPI, config: BilibiliConfig, directory: Path
    ) -> None:
        super().__init__(api)
        self.config = config
        self.directory = directory
        self._active_until = 0.0

    @classmethod
    def setup(cls, **kwargs: object) -> Self:
        config = BilibiliConfig.model_validate(kwargs)
        directory = StorageManager.get_adapter_dir("bilibili")
        state = load_state(directory / "auth.json") if not config.cookies else None
        api = BilibiliAPI(
            config.cookies or (state.cookies if state else {}), timeout=config.timeout
        )
        if state is not None:
            api.device_id = state.device_id
            api.refresh_token = state.refresh_token
            api.confirmation_token = state.confirmation_token
        return cls(api, config, directory)

    async def send(
        self,
        target_id: str,
        message: MessageSegment | MessageChain,
        cid: str = "",
    ) -> SendReceipt:
        chain = (
            MessageChain(message) if isinstance(message, MessageSegment) else message
        )
        if not chain or any(not isinstance(segment, Text) for segment in chain):
            raise ValueError("Bilibili sending currently supports text messages only")
        if not target_id.isascii() or not target_id.isdecimal() or int(target_id) <= 0:
            raise ValueError("Bilibili target ID must be a positive numeric UID")
        if cid and cid != f"private:{target_id}":
            raise ValueError("Bilibili conversation does not match target ID")
        text = "".join(segment.text for segment in chain if isinstance(segment, Text))
        try:
            receipt = await self.api.send_text(int(target_id), text)
        except (BilibiliAPIError, httpx.TransportError) as exc:
            logger.warning(
                "[Bilibili][发送失败] -> {}: {}", target_id, type(exc).__name__
            )
            raise
        self._active_until = time.monotonic() + self.config.active_duration
        logger.info("[Bilibili][发送] -> {}: {}", target_id, chain.summary)
        return receipt

    async def _dispatch(self, store: MessageStore) -> int:
        if self._on_receive_callback is None:
            raise RuntimeError("Bilibili adapter has no event callback")
        pending = await asyncio.to_thread(store.pending)
        count = len(pending)
        for talker, message in pending:
            event = MessageEvent(
                cid=f"private:{talker}",
                sender_id=str(message.sender_uid),
                sender_name=str(message.sender_uid),
                messages=decode_message(message),
                raw_payload=message.model_dump(mode="json"),
            )
            await self._on_receive_callback(event)
            await asyncio.to_thread(store.acknowledge, talker, message.msg_seqno)
            logger.info("[Bilibili][接收] <- {}: {}", event.sender_id, event.summary)
        if self.config.notifications:
            categories: tuple[NotificationCategory, ...] = ("reply", "like", "at")
            for category in categories:
                for record in await asyncio.to_thread(
                    store.pending_notifications, category
                ):
                    notification = notification_event(self.bot_id, record)
                    await self._on_receive_callback(notification)
                    await asyncio.to_thread(
                        store.acknowledge_notification, record.key, category
                    )
                    count += 1
                    if isinstance(notification, BilibiliLikedEvent):
                        logger.info(
                            "[Bilibili][收到点赞] users={}, target_type={}, target_id={}, new_count={}, count={}",
                            [(user.nickname, user.mid) for user in notification.users],
                            notification.target.type,
                            notification.target.item_id,
                            notification.new_count,
                            notification.count,
                        )
                    elif isinstance(notification, BilibiliMentionEvent):
                        logger.info(
                            "[Bilibili][有人@我] user={}({}), target_type={}, subject_id={}",
                            notification.user.nickname,
                            notification.user.mid,
                            notification.target.type,
                            notification.target.subject_id,
                        )
                    else:
                        logger.info(
                            "[Bilibili][事件] {}: id={}",
                            notification.event_type,
                            notification.notification_id,
                        )
        return count

    async def _refresh_login(self) -> float:
        try:
            await refresh_credentials(self.api, self.directory / "auth.json")
        except (BilibiliAPIError, BilibiliProtocolError, httpx.TransportError) as exc:
            logger.warning("[Bilibili] 登录态刷新暂缓: error={}", type(exc).__name__)
            return time.monotonic() + 3600
        return time.monotonic() + 86400

    async def _receive_live(self, event: BilibiliLiveEvent) -> None:
        if self._on_receive_callback is None:
            raise RuntimeError("Bilibili adapter has no event callback")
        await self._on_receive_callback(event)
        if isinstance(event, BilibiliDanmakuEvent):
            logger.info(
                "[Bilibili][直播弹幕] room={}, user={}({}): {}",
                event.room_id,
                event.danmaku.uname,
                event.danmaku.uid,
                event.danmaku.text,
            )
        elif type(event) is not BilibiliLiveEvent:
            logger.info(
                "[Bilibili][直播事件] {}: room={}", event.event_type, event.room_id
            )

    async def run(self) -> None:
        store: MessageStore | None = None
        live_tasks: list[asyncio.Task[None]] = []
        try:
            try:
                account = await self.api.get_self_info()
            except BilibiliAPIError as exc:
                if exc.code != -101:
                    raise
                await login(self.api, self.directory / "auth.json")
                account = self.api.account
            if account is None:
                raise RuntimeError("Bilibili login did not establish an account")
            self.bot_id = str(account.mid)
            await asyncio.to_thread(save_state, self.api, self.directory / "auth.json")
            opening = asyncio.create_task(
                asyncio.to_thread(
                    MessageStore, self.directory / "messages.sqlite3", account.mid
                )
            )
            try:
                opened_store = await asyncio.shield(opening)
            except asyncio.CancelledError:
                store = await opening
                raise
            store = opened_store
            sync = MessageSync(self.api, store)
            notifications = NotificationSync(self.api, store)
            live_tasks = [
                asyncio.create_task(
                    LiveClient(self.api, room_id, self.bot_id, self._receive_live).run()
                )
                for room_id in self.config.live_rooms
            ]
            failures = 0
            reconciled_at = 0.0
            auth_check_at = 0.0
            logger.success("[Bilibili] 账号 {} 已启动", self.bot_id)
            if self.config.notifications:
                logger.info(
                    "[Bilibili] 适配器通知事件监听：启用: interval={}s, active_interval={}s",
                    self.config.poll_interval,
                    self.config.active_interval,
                )
            else:
                logger.info("[Bilibili] 适配器通知事件监听：禁用: notifications=false")
            while True:
                started_at = time.monotonic()
                try:
                    for task in live_tasks:
                        if task.done():
                            task.result()
                    if time.monotonic() >= auth_check_at:
                        auth_check_at = await self._refresh_login()
                    if self.config.notifications and await notifications.poll():
                        self._active_until = (
                            time.monotonic() + self.config.active_duration
                        )
                    if await self._dispatch(store):
                        self._active_until = (
                            time.monotonic() + self.config.active_duration
                        )
                    initialized = await sync.initialize()
                    reconcile = (
                        time.monotonic() - reconciled_at
                        >= self.config.reconcile_interval
                    )
                    if not initialized:
                        count = await sync.poll(reconcile=reconcile)
                        if count:
                            self._active_until = (
                                time.monotonic() + self.config.active_duration
                            )
                    if initialized or reconcile:
                        reconciled_at = time.monotonic()
                    await self._dispatch(store)
                    failures = 0
                    interval = (
                        self.config.active_interval
                        if time.monotonic() < self._active_until
                        else self.config.poll_interval
                    )
                except BilibiliAPIError as exc:
                    if exc.code == -101:
                        logger.warning(
                            "[Bilibili] 登录已失效，请重新启动适配器扫码登录"
                        )
                        raise
                    failures += 1
                    interval = min(
                        300, self.config.poll_interval * 2 ** min(failures, 5)
                    )
                    logger.warning(
                        "[Bilibili] 同步请求失败: HTTP={}, code={}",
                        exc.status,
                        exc.code,
                    )
                except (httpx.TransportError, BilibiliProtocolError) as exc:
                    failures += 1
                    interval = min(
                        300, self.config.poll_interval * 2 ** min(failures, 5)
                    )
                    logger.warning(
                        "[Bilibili] 同步暂缓: error={}",
                        f"{type(exc).__name__}: {exc}"
                        if isinstance(exc, BilibiliProtocolError)
                        else type(exc).__name__,
                    )
                delay = (
                    interval * random.uniform(1, 1.1)
                    if failures
                    else max(0, interval - (time.monotonic() - started_at))
                )
                await asyncio.sleep(delay)
        finally:
            try:
                for task in live_tasks:
                    task.cancel()
                await asyncio.gather(*live_tasks, return_exceptions=True)
                if store is not None:
                    await asyncio.to_thread(store.close)
            finally:
                await self.api.close()
