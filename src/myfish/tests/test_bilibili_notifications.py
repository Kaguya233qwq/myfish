import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from myfish.adapters.bilibili import BilibiliAdapter
from myfish.adapters.bilibili.api import BilibiliAPI, BilibiliProtocolError
from myfish.adapters.bilibili.config import BilibiliConfig
from myfish.adapters.bilibili.events import (
    BilibiliCommentEvent,
    BilibiliLikedEvent,
    BilibiliMentionEvent,
    BilibiliNotificationEvent,
    BilibiliReplyEvent,
    notification_event,
)
from myfish.adapters.bilibili.model import (
    MentionNotification,
    MentionNotificationPage,
    NotificationCursor,
    ReplyNotification,
    ReplyNotificationPage,
)
from myfish.adapters.bilibili.sync import MessageStore, NotificationSync
from myfish.core.engine import Engine
from myfish.core.plugin import Plugin, PluginMetadata


def record(identifier, timestamp):
    return ReplyNotification(
        id=identifier,
        reply_time=timestamp,
        user={"mid": 2, "nickname": "test", "avatar": ""},
        item={
            "type": "reply",
            "business_id": 1,
            "subject_id": 123,
            "root_id": 10,
            "source_id": 20,
            "target_id": 10,
            "source_content": "reply",
            "root_reply_content": "root",
        },
    )


def page(*items, end=True, cursor_id=0, timestamp=0):
    return ReplyNotificationPage(
        items=list(items),
        cursor=NotificationCursor(is_end=end, id=cursor_id, time=timestamp),
    )


def mention(identifier: int, timestamp: int) -> MentionNotification:
    return MentionNotification(
        id=identifier,
        at_time=timestamp,
        user={"mid": 2, "nickname": "test", "avatar": ""},
        item={
            "type": "reply",
            "business_id": 1,
            "subject_id": 123,
            "source_id": 20,
            "source_content": "@test hello",
            "uri": "https://www.bilibili.com/video/BV1test#reply20",
        },
    )


def mention_page(*items: MentionNotification) -> MentionNotificationPage:
    return MentionNotificationPage(
        items=list(items),
        cursor=NotificationCursor(is_end=True, id=0, time=0),
    )


class BilibiliNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "messages.sqlite3"
        self.store = MessageStore(self.path, 1)
        self.api = MagicMock(spec=BilibiliAPI)
        self.sync = NotificationSync(self.api, self.store)

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_first_poll_baselines_history_then_receives_new_notifications(self):
        self.api.get_reply_notifications.return_value = page(record(1, 1000))
        self.assertEqual(await self.sync._poll_notifications("reply"), 0)
        self.assertEqual(self.store.pending_notifications(), [])
        self.api.get_reply_notifications.return_value = page(
            record(2, 1001), record(1, 1000)
        )
        self.assertEqual(await self.sync._poll_notifications("reply"), 1)
        self.assertEqual([item.id for item in self.store.pending_notifications()], [2])
        self.assertEqual(await self.sync._poll_notifications("reply"), 0)

    async def test_http_request_preserves_notification_pagination_cursor(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"code": 0, "data": page(record(3, 1000)).model_dump()}
            )

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            result = await api.get_reply_notifications(cursor_id=10, reply_time=1001)
        self.assertEqual(result.items[0].id, 3)
        self.assertEqual(requests[0].url.path, "/x/msgfeed/reply")
        self.assertEqual(requests[0].url.params["id"], "10")
        self.assertEqual(requests[0].url.params["reply_time"], "1001")

    async def test_mention_api_preserves_cursor_and_rejects_negative_values(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={"code": 0, "data": mention_page(mention(3, 1000)).model_dump()},
            )

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            result = await api.get_mention_notifications(cursor_id=10, at_time=1001)
            with self.assertRaises(ValueError):
                await api.get_mention_notifications(at_time=-1)
            with self.assertRaises(ValueError):
                await api.get_mention_notifications(cursor_id=-1)
        self.assertEqual(result.items[0].id, 3)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/x/msgfeed/at")
        self.assertEqual(requests[0].url.params["id"], "10")
        self.assertEqual(requests[0].url.params["at_time"], "1001")

    async def test_mention_history_is_skipped_and_new_records_are_deduplicated(self):
        self.api.get_mention_notifications.return_value = mention_page(mention(1, 1000))
        self.assertEqual(await self.sync._poll_notifications("at"), 0)
        self.api.get_mention_notifications.return_value = mention_page(
            mention(2, 1000), mention(1, 1000)
        )
        self.assertEqual(await self.sync._poll_notifications("at"), 1)
        self.assertEqual(await self.sync._poll_notifications("at"), 0)
        self.assertEqual(
            [item.id for item in self.store.pending_notifications("at")], [2]
        )
        self.assertEqual(self.store.pending_notifications("reply"), [])

    async def test_mention_pagination_failure_does_not_advance_progress(self):
        self.store.stage_notifications([mention(1, 1000)], baseline=True, category="at")
        first = mention_page(mention(2, 1001))
        first.cursor = NotificationCursor(is_end=False, id=2, time=1001)
        self.api.get_mention_notifications.side_effect = [
            first,
            BilibiliProtocolError("failed"),
        ]
        with self.assertRaises(BilibiliProtocolError):
            await self.sync._poll_notifications("at")
        self.assertEqual(self.store.notification_time("at"), 1000)
        self.assertEqual(self.store.pending_notifications("at"), [])
        self.api.get_mention_notifications.assert_any_await(cursor_id=2, at_time=1001)

    async def test_mention_dispatch_retries_after_callback_failure_and_survives_restart(
        self,
    ):
        self.store.stage_notifications([mention(2, 1001)], category="at")
        reopened = MessageStore(self.path, 1)
        try:
            async with BilibiliAPI() as api:
                adapter = BilibiliAdapter(
                    api, BilibiliConfig(), Path(self.directory.name)
                )
                adapter.bot_id = "1"
                callback = AsyncMock(side_effect=RuntimeError("failed"))
                adapter.set_callback(callback)
                with self.assertRaises(RuntimeError):
                    await adapter._dispatch(reopened)
                self.assertEqual(len(reopened.pending_notifications("at")), 1)
                callback.side_effect = None
                self.assertEqual(await adapter._dispatch(reopened), 1)
                event = callback.call_args.args[0]
                self.assertIsInstance(event, BilibiliMentionEvent)
                self.assertEqual(event.user.mid, 2)
                self.assertEqual(event.target.source_content, "@test hello")
                self.assertEqual(event.timestamp_ms, 1001000)
                self.assertEqual(reopened.pending_notifications("at"), [])
                self.assertEqual(
                    reopened.stage_notifications([mention(2, 1001)], category="at"), 0
                )
        finally:
            reopened.close()

    def test_mention_event_preserves_dynamic_target_and_unknown_fields(self):
        item = mention(1, 1000)
        item.item = {
            "type": "dynamic",
            "business_id": 17,
            "subject_id": 123,
            "title": "hello",
            "uri": "https://t.bilibili.com/123",
            "future": True,
        }
        event = notification_event("1", item)
        self.assertIsInstance(event, BilibiliMentionEvent)
        self.assertEqual(event.event_type, "bilibili.mention")
        self.assertEqual(event.target.title, "hello")
        self.assertEqual(event.raw_payload["item"]["future"], True)

    def test_malformed_mention_falls_back_to_notification_event(self):
        item = MentionNotification(id=1, at_time=1000, item={"future": "value"})
        event = notification_event("1", item)
        self.assertEqual(type(event), BilibiliNotificationEvent)
        self.assertEqual(event.raw_payload["item"], {"future": "value"})

    def test_identical_ids_in_mentions_and_replies_are_independent(self):
        self.store.stage_notifications([record(2, 1001)])
        self.store.stage_notifications([mention(2, 1001)], category="at")
        self.store.acknowledge_notification(2, "at")
        self.assertEqual(len(self.store.pending_notifications("reply")), 1)
        self.assertEqual(self.store.pending_notifications("at"), [])
        with self.assertRaises(TypeError):
            self.store.stage_notifications([mention(3, 1002)], category="reply")

    async def test_same_timestamp_and_overlapping_pages_are_deduplicated(self):
        self.store.stage_notifications([record(1, 1000)], baseline=True)
        self.api.get_reply_notifications.side_effect = [
            page(
                record(3, 1000), record(2, 1000), end=False, cursor_id=2, timestamp=1000
            ),
            page(record(2, 1000), record(1, 1000)),
        ]
        self.assertEqual(await self.sync._poll_notifications("reply"), 2)
        self.assertEqual(
            [item.id for item in self.store.pending_notifications()], [2, 3]
        )
        self.api.get_reply_notifications.assert_any_await(cursor_id=2, reply_time=1000)

    async def test_pagination_failure_keeps_baseline_and_inbox_unchanged(self):
        self.store.stage_notifications([record(1, 1000)], baseline=True)
        self.api.get_reply_notifications.side_effect = [
            page(record(2, 1001), end=False, cursor_id=2, timestamp=1001),
            BilibiliProtocolError("failed"),
        ]
        with self.assertRaises(BilibiliProtocolError):
            await self.sync._poll_notifications("reply")
        self.assertEqual(self.store.notification_time(), 1000)
        self.assertEqual(self.store.pending_notifications(), [])

    async def test_nonadvancing_cursor_is_rejected(self):
        self.api.get_reply_notifications.return_value = page(record(1, 1000), end=False)
        with self.assertRaises(BilibiliProtocolError):
            await self.sync._poll_notifications("reply")
        self.assertIsNone(self.store.notification_time())

    async def test_recent_history_baseline_covers_pagination_overlap(self):
        self.api.get_reply_notifications.side_effect = [
            page(record(3, 1000), end=False, cursor_id=3, timestamp=1000),
            page(record(2, 960), record(1, 900), end=False, cursor_id=1, timestamp=900),
        ]
        self.assertEqual(await self.sync._poll_notifications("reply"), 0)
        self.assertEqual(self.api.get_reply_notifications.await_count, 2)
        self.assertEqual(self.store.notification_time(), 1000)

    def test_reopen_preserves_pending_and_prevents_redelivery_after_ack(self):
        self.store.stage_notifications([record(2, 1001)])
        reopened = MessageStore(self.path, 1)
        try:
            self.assertEqual(reopened.pending_notifications()[0].id, 2)
            reopened.acknowledge_notification(2)
            self.assertEqual(reopened.stage_notifications([record(2, 1001)]), 0)
            self.assertEqual(reopened.pending_notifications(), [])
        finally:
            reopened.close()

    async def test_dispatch_uses_typed_event_without_private_reply_callback(self):
        self.store.stage_notifications([record(2, 1001)])
        async with BilibiliAPI() as api:
            adapter = BilibiliAdapter(
                api, BilibiliConfig(notifications=True), Path(self.directory.name)
            )
            adapter.bot_id = "1"
            callback = AsyncMock(side_effect=RuntimeError("failed"))
            adapter.set_callback(callback)
            with self.assertRaises(RuntimeError):
                await adapter._dispatch(self.store)
            self.assertEqual(len(self.store.pending_notifications()), 1)
            callback.side_effect = None
            await adapter._dispatch(self.store)
            event = callback.call_args.args[0]
            self.assertIsInstance(event, BilibiliReplyEvent)
            self.assertEqual(event.target.root_id, 10)
            self.assertEqual(event.target.source_id, 20)
            self.assertEqual(event.user.mid, 2)
            self.assertEqual(event.timestamp_ms, 1001000)
            self.assertEqual(self.store.pending_notifications(), [])

    def test_unknown_notification_preserves_payload_as_base_event(self):
        item = ReplyNotification(
            id=3, reply_time=1000, item={"new_kind": "future"}, future_field="retained"
        )
        event = notification_event("1", item)
        self.assertEqual(type(event), BilibiliNotificationEvent)
        self.assertEqual(event.raw_payload["future_field"], "retained")

    def test_video_comment_has_its_own_event_type(self):
        item = record(3, 1000)
        item.item = {**item.item, "type": "video"}
        event = notification_event("1", item)
        self.assertEqual(type(event), BilibiliCommentEvent)
        self.assertEqual(event.event_type, "bilibili.comment")

    async def test_framework_runtime_polls_by_default_and_dispatches_video_comment_to_plugin(
        self,
    ):
        now = 0.0
        discoveries = 0

        def handle(request):
            nonlocal discoveries
            if request.url.path.endswith("/nav"):
                data = {"mid": 1, "uname": "test", "isLogin": True}
            elif request.url.path.endswith("/reply"):
                discoveries += 1
                records = [record(1, 1000)]
                if discoveries > 1:
                    item = record(2, 1001)
                    item.item = {**item.item, "type": "video"}
                    records.insert(0, item)
                data = page(*records).model_dump()
            elif request.url.path.endswith("/at"):
                records = [mention(1, 1000)]
                if discoveries > 1:
                    records.insert(0, mention(2, 1001))
                data = mention_page(*records).model_dump()
            elif request.url.path.endswith("/like"):
                users = [{"mid": 2, "nickname": "first"}]
                if discoveries > 1:
                    users.append({"mid": 3, "nickname": "new"})
                data = {
                    "latest": {"items": []},
                    "total": {
                        "items": [
                            {
                                "id": 11,
                                "like_time": 1000 + int(discoveries > 1),
                                "users": users,
                                "counts": len(users),
                                "item": {
                                    "item_id": 123,
                                    "pid": 456,
                                    "type": "reply",
                                    "business_id": 1,
                                },
                            }
                        ],
                        "cursor": {"is_end": True, "id": 0, "time": 0},
                    },
                }
            else:
                data = {"session_list": [], "has_more": 0}
            return httpx.Response(200, json={"code": 0, "data": data})

        async def sleep(_):
            nonlocal now
            if now:
                raise asyncio.CancelledError
            now = 11

        with tempfile.TemporaryDirectory() as directory:
            api = BilibiliAPI(
                {"SESSDATA": "test", "bili_jct": "csrf", "DedeUserID": "1"},
                transport=httpx.MockTransport(handle),
            )
            adapter = BilibiliAdapter(
                api, BilibiliConfig(poll_interval=10), Path(directory)
            )
            plugin = Plugin(PluginMetadata(id="test", name="test"))
            received: list[BilibiliCommentEvent] = []
            likes: list[BilibiliLikedEvent] = []
            mentions: list[BilibiliMentionEvent] = []

            @plugin.on_event(BilibiliCommentEvent)
            async def on_comment(event: BilibiliCommentEvent):
                received.append(event)

            @plugin.on_event(BilibiliLikedEvent)
            async def on_liked(event: BilibiliLikedEvent):
                likes.append(event)

            @plugin.on_event(BilibiliMentionEvent)
            async def on_mention(event: BilibiliMentionEvent):
                mentions.append(event)

            engine = Engine()
            engine.plugin_manager.plugins["test"] = plugin
            engine.add_adapter(adapter)
            with (
                patch(
                    "myfish.adapters.bilibili.time",
                    SimpleNamespace(monotonic=lambda: now),
                ),
                patch("myfish.adapters.bilibili.asyncio.sleep", side_effect=sleep),
                self.assertRaises(asyncio.CancelledError),
            ):
                await adapter.run()
            await asyncio.gather(*list(engine._background_tasks))
            self.assertEqual(discoveries, 2)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].notification_id, "2")
            self.assertEqual(received[0].target.type, "video")
            self.assertEqual(len(likes), 1)
            self.assertEqual([user.mid for user in likes[0].users], [3])
            self.assertEqual(likes[0].new_count, 1)
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0].notification_id, "2")
            self.assertTrue(api._client.is_closed)

    def test_notification_state_is_isolated_by_account(self):
        self.store.stage_notifications([record(2, 1001)])
        other = MessageStore(self.path, 99)
        try:
            self.assertIsNone(other.notification_time())
            self.assertEqual(other.pending_notifications(), [])
        finally:
            other.close()
