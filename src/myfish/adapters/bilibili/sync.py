import asyncio
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import Literal

import httpx
from pydantic import TypeAdapter

from myfish.core.logger import logger

from .api import BilibiliAPI, BilibiliAPIError, BilibiliProtocolError
from .model import (
    LikeNotification,
    LikeUpdate,
    MentionNotification,
    Message,
    NotificationCategory,
    ReplyNotification,
    Session,
)

_USER_IDS = TypeAdapter(list[int])


class MessageStore:
    def __init__(self, path: Path, account_id: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.account_id = account_id
        self._lock = RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                uid INTEGER PRIMARY KEY, session_ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                uid INTEGER NOT NULL, talker INTEGER NOT NULL,
                seq INTEGER NOT NULL, PRIMARY KEY(uid, talker)
            );
            CREATE TABLE IF NOT EXISTS inbox (
                uid INTEGER NOT NULL, talker INTEGER NOT NULL,
                seq INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(uid, talker, seq)
            );
            CREATE TABLE IF NOT EXISTS notification_sources (
                uid INTEGER NOT NULL, category TEXT NOT NULL, timestamp INTEGER NOT NULL,
                PRIMARY KEY(uid,category)
            );
            CREATE TABLE IF NOT EXISTS notification_seen (
                uid INTEGER NOT NULL, category TEXT NOT NULL, id TEXT NOT NULL,
                PRIMARY KEY(uid,category,id)
            );
            CREATE TABLE IF NOT EXISTS notification_inbox (
                uid INTEGER NOT NULL, category TEXT NOT NULL, id TEXT NOT NULL,
                timestamp INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(uid,category,id)
            );
            CREATE TABLE IF NOT EXISTS like_snapshots (
                uid INTEGER NOT NULL, id TEXT NOT NULL,
                payload TEXT NOT NULL, users TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'total',
                PRIMARY KEY(uid,id)
            );
            CREATE TABLE IF NOT EXISTS like_hints (
                uid INTEGER NOT NULL, id TEXT NOT NULL,
                payload TEXT NOT NULL, baseline INTEGER NOT NULL,
                PRIMARY KEY(uid,id)
            );
        """)
        if "scope" not in {
            row[1] for row in self._db.execute("PRAGMA table_info(like_snapshots)")
        }:
            self._db.execute(
                "ALTER TABLE like_snapshots ADD COLUMN scope TEXT NOT NULL DEFAULT 'unknown'"
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def watermark(self) -> int | None:
        with self._lock:
            row = self._db.execute(
                "SELECT session_ts FROM accounts WHERE uid=?", (self.account_id,)
            ).fetchone()
        return row[0] if row is not None else None

    def cursor(self, talker: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT seq FROM sessions WHERE uid=? AND talker=?",
                (self.account_id, talker),
            ).fetchone()
        return row[0] if row is not None else 0

    def initialize(self, sessions: list[Session]) -> None:
        with self._lock, self._db:
            for session in sessions:
                self._set_cursor(session.talker_id, session.max_seqno)
            self._set_watermark(max((item.session_ts for item in sessions), default=0))

    def _set_cursor(self, talker: int, seq: int) -> None:
        self._db.execute(
            "INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(uid,talker) "
            "DO UPDATE SET seq=MAX(seq,excluded.seq)",
            (self.account_id, talker, seq),
        )

    def _set_watermark(self, timestamp: int) -> None:
        self._db.execute(
            "INSERT INTO accounts VALUES(?,?) ON CONFLICT(uid) "
            "DO UPDATE SET session_ts=MAX(session_ts,excluded.session_ts)",
            (self.account_id, timestamp),
        )

    def advance(self, timestamp: int) -> None:
        with self._lock, self._db:
            self._set_watermark(timestamp)

    def stage(self, talker: int, messages: list[Message]) -> int:
        inserted = 0
        with self._lock, self._db:
            cursor = self.cursor(talker)
            for message in sorted(messages, key=lambda item: item.msg_seqno):
                if message.msg_seqno <= cursor:
                    continue
                if message.sender_uid != self.account_id and message.msg_status == 0:
                    result = self._db.execute(
                        "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?)",
                        (
                            self.account_id,
                            talker,
                            message.msg_seqno,
                            message.model_dump_json(),
                        ),
                    )
                    inserted += result.rowcount
            self._set_cursor(
                talker, max((item.msg_seqno for item in messages), default=cursor)
            )
        return inserted

    def pending(self, limit: int = 100) -> list[tuple[int, Message]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT talker,payload FROM inbox WHERE uid=? ORDER BY seq LIMIT ?",
                (self.account_id, limit),
            ).fetchall()
        return [
            (talker, Message.model_validate_json(payload)) for talker, payload in rows
        ]

    def acknowledge(self, talker: int, sequence: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM inbox WHERE uid=? AND talker=? AND seq=?",
                (self.account_id, talker, sequence),
            )

    def notification_time(self, category: NotificationCategory = "reply") -> int | None:
        with self._lock:
            row = self._db.execute(
                "SELECT timestamp FROM notification_sources WHERE uid=? AND category=?",
                (self.account_id, category),
            ).fetchone()
        return row[0] if row is not None else None

    def stage_notifications(
        self,
        records: Sequence[ReplyNotification | MentionNotification | LikeNotification],
        *,
        baseline: bool = False,
        category: NotificationCategory = "reply",
        baseline_before: int | None = None,
    ) -> int:
        inserted = 0
        with self._lock, self._db:
            watermark = self.notification_time(category) or 0
            for record in sorted(records, key=lambda item: (item.timestamp, item.id)):
                model = {
                    "reply": ReplyNotification,
                    "like": LikeNotification,
                    "at": MentionNotification,
                }[category]
                if not isinstance(record, model):
                    raise TypeError("Notification does not match its category")
                result = self._db.execute(
                    "INSERT OR IGNORE INTO notification_seen VALUES(?,?,?)",
                    (self.account_id, category, record.key),
                )
                pending: ReplyNotification | MentionNotification | LikeUpdate | None = (
                    None
                )
                if isinstance(record, LikeNotification):
                    pending = self._like_update(
                        record,
                        baseline=baseline
                        or not bool(result.rowcount)
                        or (
                            baseline_before is not None
                            and record.timestamp < baseline_before
                        ),
                    )
                elif result.rowcount and not baseline:
                    pending = record
                if pending is not None:
                    self._db.execute(
                        "INSERT INTO notification_inbox VALUES(?,?,?,?,?)",
                        (
                            self.account_id,
                            category,
                            record.key,
                            record.timestamp,
                            pending.model_dump_json(),
                        ),
                    )
                    inserted += 1
                watermark = max(watermark, record.timestamp)
            self._db.execute(
                "INSERT INTO notification_sources VALUES(?,?,?) "
                "ON CONFLICT(uid,category) DO UPDATE SET timestamp=excluded.timestamp",
                (self.account_id, category, watermark),
            )
        return inserted

    def _like_update(
        self, record: LikeNotification, *, baseline: bool
    ) -> LikeUpdate | None:
        row = self._db.execute(
            "SELECT payload,users,scope FROM like_snapshots WHERE uid=? AND id=?",
            (self.account_id, str(record.id)),
        ).fetchone()
        previous = (
            LikeNotification.model_validate_json(row[0])
            if row is not None and row[2] == "total"
            else None
        )
        known = set(_USER_IDS.validate_json(row[1])) if row is not None else set()
        if previous is not None and previous.key == record.key:
            return None
        if previous is not None and (
            record.like_time < previous.like_time
            or (
                record.like_time == previous.like_time
                and record.counts < previous.counts
            )
        ):
            return None
        hint_row = self._db.execute(
            "SELECT payload,baseline FROM like_hints WHERE uid=? AND id=?",
            (self.account_id, str(record.id)),
        ).fetchone()
        hint = (
            LikeNotification.model_validate_json(hint_row[0])
            if hint_row is not None
            else None
        )
        hint_baseline = bool(hint_row[1]) if hint_row is not None else False
        if hint is not None and hint.like_time > record.like_time:
            hint = None
        reported = {user.mid: user for user in record.users}
        if hint is not None:
            reported.update((user.mid, user) for user in hint.users)
            baseline = baseline or hint_baseline
            self._db.execute(
                "DELETE FROM like_hints WHERE uid=? AND id=?",
                (self.account_id, str(record.id)),
            )
        if hint is not None:
            candidates = hint.users
        elif len(record.users) == record.counts and (
            previous is not None or record.counts == 1
        ):
            candidates = record.users
        else:
            candidates = []
        users = [user for user in candidates if user.mid not in known]
        increase = (
            max(0, record.counts - previous.counts)
            if previous is not None
            else (1 if record.counts == 1 else None)
        )
        known.update(reported)
        self._db.execute(
            "INSERT INTO like_snapshots VALUES(?,?,?,?,'total') ON CONFLICT(uid,id) "
            "DO UPDATE SET payload=excluded.payload,users=excluded.users,scope='total'",
            (
                self.account_id,
                str(record.id),
                record.model_dump_json(),
                _USER_IDS.dump_json(sorted(known)).decode(),
            ),
        )
        if not baseline and record.counts > 0 and (users or increase):
            return LikeUpdate(
                notification=record,
                users=users,
                new_count=increase,
                reported_users=list(reported.values()),
            )
        return None

    def save_like_hints(
        self, records: Sequence[LikeNotification], *, baseline: bool
    ) -> None:
        with self._lock, self._db:
            for record in records:
                row = self._db.execute(
                    "SELECT payload FROM like_hints WHERE uid=? AND id=?",
                    (self.account_id, str(record.id)),
                ).fetchone()
                if row is not None:
                    previous = LikeNotification.model_validate_json(row[0])
                    if (
                        previous.like_time > record.like_time
                        or previous.key == record.key
                    ):
                        continue
                self._db.execute(
                    "INSERT INTO like_hints VALUES(?,?,?,?) ON CONFLICT(uid,id) "
                    "DO UPDATE SET payload=excluded.payload,baseline=excluded.baseline",
                    (
                        self.account_id,
                        str(record.id),
                        record.model_dump_json(),
                        int(baseline),
                    ),
                )

    def pending_notifications(
        self, category: NotificationCategory = "reply"
    ) -> list[ReplyNotification | MentionNotification | LikeUpdate]:
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM notification_inbox WHERE uid=? AND category=? "
                "ORDER BY timestamp,id LIMIT 100",
                (self.account_id, category),
            ).fetchall()
        model = {
            "reply": ReplyNotification,
            "like": LikeUpdate,
            "at": MentionNotification,
        }[category]
        return [model.model_validate_json(row[0]) for row in rows]

    def acknowledge_notification(
        self, notification_id: int | str, category: NotificationCategory = "reply"
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                "DELETE FROM notification_inbox WHERE uid=? AND category=? AND id=?",
                (self.account_id, category, str(notification_id)),
            )


class MessageSync:
    def __init__(self, api: BilibiliAPI, store: MessageStore) -> None:
        self.api = api
        self.store = store

    async def _sessions(self, begin_ts: int = 0) -> list[Session]:
        sessions: dict[int, Session] = {}
        end_ts: int | None = None
        previous_boundary: int | None = None
        for _ in range(1000):
            page = await self.api.get_sessions(begin_ts=begin_ts, end_ts=end_ts)
            items = page.session_list or []
            for session in items:
                if session.session_type == 1 and session.system_msg_type == 0:
                    previous = sessions.get(session.talker_id)
                    if previous is None or session.max_seqno > previous.max_seqno:
                        sessions[session.talker_id] = session
            if not page.has_more:
                return list(sessions.values())
            if not items:
                raise BilibiliProtocolError("Empty session page has more results")
            boundary = min(item.session_ts for item in items)
            if previous_boundary is not None and boundary >= previous_boundary:
                raise BilibiliProtocolError("Session pagination did not advance")
            previous_boundary = boundary
            end_ts = boundary + 1
        raise BilibiliProtocolError("Session pagination limit exceeded")

    async def initialize(self) -> bool:
        if await asyncio.to_thread(self.store.watermark) is not None:
            return False
        sessions = await self._sessions()
        await asyncio.to_thread(self.store.initialize, sessions)
        return True

    async def _messages(self, session: Session, begin: int) -> list[Message]:
        messages: dict[int, Message] = {}
        end: int | None = None
        for _ in range(1000):
            page = await self.api.get_messages(
                session.talker_id, begin_seqno=begin, end_seqno=end
            )
            items = page.messages or []
            for message in items:
                if message.msg_seqno > begin:
                    if message.receiver_type != 1 or {
                        message.sender_uid,
                        message.receiver_id,
                    } != {self.store.account_id, session.talker_id}:
                        raise BilibiliProtocolError("Message does not match session")
                    messages[message.msg_seqno] = message
            if not page.has_more:
                if max(messages, default=begin) < session.max_seqno:
                    raise BilibiliProtocolError(
                        "Message history has not reached the advertised sequence"
                    )
                return list(messages.values())
            if not items:
                raise BilibiliProtocolError("Empty message page has more results")
            boundary = min(item.msg_seqno for item in items)
            if boundary <= begin:
                if max(messages, default=begin) < session.max_seqno:
                    raise BilibiliProtocolError("Message history is incomplete")
                return list(messages.values())
            if end is not None and boundary >= end:
                raise BilibiliProtocolError("Message pagination did not advance")
            end = boundary
        raise BilibiliProtocolError("Message pagination limit exceeded")

    async def poll(self, *, reconcile: bool = False) -> int:
        watermark = await asyncio.to_thread(self.store.watermark)
        if watermark is None:
            raise RuntimeError("Message sync must be initialized")
        begin = max(0, watermark - 2_000_000)
        if reconcile:
            sessions = await self._sessions()
        else:
            page = await self.api.get_new_sessions(begin)
            sessions = (
                await self._sessions(begin)
                if page.has_more
                else page.session_list or []
            )
        inserted = 0
        failure: (
            BilibiliAPIError | BilibiliProtocolError | httpx.TransportError | None
        ) = None
        for session in sessions:
            if session.session_type != 1 or session.system_msg_type != 0:
                continue
            cursor = await asyncio.to_thread(self.store.cursor, session.talker_id)
            if session.max_seqno <= cursor:
                continue
            try:
                messages = await self._messages(session, cursor)
            except (
                BilibiliAPIError,
                BilibiliProtocolError,
                httpx.TransportError,
            ) as exc:
                if isinstance(exc, BilibiliAPIError) and exc.code == -101:
                    raise
                logger.warning(
                    "[Bilibili] 会话补拉失败: talker={}, error={}",
                    session.talker_id,
                    f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, BilibiliProtocolError)
                    else type(exc).__name__,
                )
                failure = exc
                continue
            inserted += await asyncio.to_thread(
                self.store.stage, session.talker_id, messages
            )
        if failure is not None:
            raise failure
        await asyncio.to_thread(
            self.store.advance,
            max((item.session_ts for item in sessions), default=watermark),
        )
        return inserted


class NotificationSync:
    def __init__(self, api: BilibiliAPI, store: MessageStore) -> None:
        self.api = api
        self.store = store
        self._ready = False

    async def poll(self) -> int:
        count = 0
        categories: tuple[NotificationCategory, ...] = ("reply", "like", "at")
        for category in categories:
            try:
                if category == "like":
                    count += await self._poll_likes()
                else:
                    count += await self._poll_notifications(category)
            except (
                BilibiliAPIError,
                BilibiliProtocolError,
                httpx.TransportError,
            ) as exc:
                if isinstance(exc, BilibiliAPIError) and exc.code == -101:
                    raise
                logger.warning(
                    "[Bilibili] 通知事件同步异常: category={}, error={}",
                    category,
                    f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, BilibiliProtocolError)
                    else type(exc).__name__,
                )
        if not self._ready:
            self._ready = all(
                [
                    await asyncio.to_thread(self.store.notification_time, category)
                    is not None
                    for category in categories
                ]
            )
            if self._ready:
                logger.info("[Bilibili] 适配器通知事件监听已就绪，正在监听事件..")
        logger.debug("[Bilibili] 账号通知同步完成: new={}", count)
        return count

    async def _poll_notifications(self, category: Literal["reply", "at"]) -> int:
        watermark = await asyncio.to_thread(self.store.notification_time, category)
        boundary = max(0, watermark - 60) if watermark is not None else None
        cursor_id, cursor_time = 0, 0
        cursors: set[tuple[int, int]] = set()
        records: dict[int, ReplyNotification | MentionNotification] = {}
        for _ in range(1000):
            if category == "reply":
                page = await self.api.get_reply_notifications(
                    cursor_id=cursor_id, reply_time=cursor_time
                )
            else:
                page = await self.api.get_mention_notifications(
                    cursor_id=cursor_id, at_time=cursor_time
                )
            items = page.items or []
            if boundary is None:
                boundary = max(
                    0, max((item.timestamp for item in items), default=0) - 60
                )
            for item in items:
                if item.timestamp >= boundary:
                    records[item.id] = item
            cursor = page.cursor
            if cursor.is_end or (cursor.time > 0 and cursor.time < boundary):
                count = await asyncio.to_thread(
                    self.store.stage_notifications,
                    list(records.values()),
                    baseline=watermark is None,
                    category=category,
                )
                return count
            next_cursor = cursor.id, cursor.time
            if (
                not items
                or next_cursor in cursors
                or next_cursor == (cursor_id, cursor_time)
            ):
                raise BilibiliProtocolError("Notification pagination did not advance")
            cursors.add(next_cursor)
            cursor_id, cursor_time = next_cursor
        raise BilibiliProtocolError("Notification pagination limit exceeded")

    async def _poll_likes(self) -> int:
        watermark = await asyncio.to_thread(self.store.notification_time, "like")
        boundary = max(0, watermark - 60) if watermark is not None else None
        cursor_id, cursor_time = 0, 0
        cursors: set[tuple[int, int]] = set()
        records: dict[int, LikeNotification] = {}
        for index in range(1000):
            page = await self.api.get_like_notifications(
                cursor_id=cursor_id, like_time=cursor_time
            )
            items = list(page.total.items or [])
            if index == 0 and page.latest is not None:
                await asyncio.to_thread(
                    self.store.save_like_hints,
                    page.latest.items or [],
                    baseline=watermark is None,
                )
            if boundary is None:
                boundary = max(
                    0, max((item.like_time for item in items), default=0) - 60
                )
            for item in items:
                previous = records.get(item.id)
                if previous is None or (
                    item.like_time,
                    item.counts,
                    len(item.users),
                ) >= (previous.like_time, previous.counts, len(previous.users)):
                    records[item.id] = item
            cursor = page.total.cursor
            if cursor.is_end or (cursor.time > 0 and cursor.time < boundary):
                count = await asyncio.to_thread(
                    self.store.stage_notifications,
                    list(records.values()),
                    category="like",
                    baseline=watermark is None,
                    baseline_before=boundary,
                )
                return count
            next_cursor = cursor.id, cursor.time
            if (
                not page.total.items
                or next_cursor in cursors
                or next_cursor == (cursor_id, cursor_time)
            ):
                raise BilibiliProtocolError(
                    "Like notification pagination did not advance"
                )
            cursors.add(next_cursor)
            cursor_id, cursor_time = next_cursor
        raise BilibiliProtocolError("Like notification pagination limit exceeded")
