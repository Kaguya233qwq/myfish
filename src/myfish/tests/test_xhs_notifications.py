import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from myfish.adapters.xhs import XhsAdapter
from myfish.adapters.xhs.api import XhsAPI, XhsAPIError
from myfish.adapters.xhs.events import (
    XhsCommentEvent,
    XhsFollowEvent,
    XhsLikeEvent,
    notification_event,
)
from myfish.adapters.xhs.model import (
    Notification,
    NotificationPage,
    NotificationPush,
    XhsConfig,
)
from myfish.adapters.xhs.stream import decode_frame
from myfish.core.bot import Bot
from myfish.core.engine import Engine
from myfish.core.plugin import Plugin, PluginMetadata


def notice(kind="comment/item", identifier="notice", timestamp=10):
    value = {
        "id": identifier,
        "type": kind,
        "time": timestamp,
        "user_info": {"userid": "actor", "nickname": "昵称", "image": "avatar"},
        "item_info": {"id": "note", "type": "note_info", "content": "帖子摘要"},
    }
    if kind.startswith("comment/") or kind == "liked/comment":
        value["comment_info"] = {"id": "comment", "content": "评论正文"}
    if kind == "follow/you":
        value["user"] = value.pop("user_info")
        value.pop("item_info")
    return value


def push(you, *, identifier="push", topic="message_center"):
    data = {
        "pushid": identifier,
        "topic": topic,
        "ts": 10000,
        "payload": json.dumps({"you": you}),
    }
    return base64.b64encode(json.dumps(data).encode()).decode()


class NotificationTests(unittest.TestCase):
    def test_all_observed_types_retain_content_and_actor(self):
        for kind, expected in (
            ("follow/you", XhsFollowEvent),
            ("liked/item", XhsLikeEvent),
            ("liked/comment", XhsLikeEvent),
            ("comment/item", XhsCommentEvent),
            ("comment/comment", XhsCommentEvent),
        ):
            with self.subTest(kind=kind):
                event = notification_event(
                    "self", Notification.model_validate(notice(kind))
                )
                self.assertIsInstance(event, expected)
                self.assertEqual(event.user.user_id, "actor")
                self.assertEqual(event.timestamp_ms, 10000)
                if isinstance(event, XhsCommentEvent):
                    self.assertEqual(event.comment.content, "评论正文")
                    self.assertEqual(event.note.content, "帖子摘要")

    def test_push_decodes_partial_counters_without_assuming_delta(self):
        events = decode_frame(
            {
                "b": {
                    "d": {
                        "biz": "push",
                        "b": [{"d": push({"mentions": 1, "total": 3})}],
                    }
                }
            }
        )
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], NotificationPush)
        self.assertEqual(events[0].categories, ("mentions",))

    def test_malformed_push_does_not_drop_next_batched_event(self):
        with patch("myfish.adapters.xhs.stream.logger") as logger:
            events = decode_frame(
                {
                    "b": {
                        "d": {
                            "biz": "push",
                            "b": [{"d": "not-base64"}, {"d": push({"connections": 1})}],
                        }
                    }
                }
            )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].categories, ("connections",))
        logger.debug.assert_called()

    def test_unknown_topic_and_channel_are_diagnosed(self):
        with patch("myfish.adapters.xhs.stream.logger") as logger:
            decode_frame(
                {"b": {"d": {"biz": "push", "b": push({}, topic="new_topic")}}}
            )
            decode_frame({"b": {"d": {"biz": "unknown", "b": "private-payload"}}})
        self.assertEqual(logger.debug.call_count, 2)
        self.assertNotIn("private-payload", str(logger.debug.call_args_list))


class NotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        storage = patch(
            "myfish.adapters.xhs.utils.StorageManager.get_adapter_dir",
            return_value=Path(directory.name),
        )
        storage.start()
        self.addCleanup(storage.stop)
        self.api = XhsAPI.create()
        self.adapter = XhsAdapter(self.api, XhsConfig())
        self.adapter.bot_id = "self"
        self.adapter._started_at_ms = 10000
        self.callback = AsyncMock()
        self.adapter.set_callback(self.callback)

    async def test_baseline_is_silent_and_duplicate_push_does_not_repeat_event(self):
        old = notice(identifier="old", timestamp=9)
        new = notice(identifier="new", timestamp=11)
        self.api.get_notifications = AsyncMock(
            side_effect=[
                NotificationPage(message_list=[old]),
                NotificationPage(message_list=[new, old]),
                NotificationPage(message_list=[new, old]),
            ]
        )
        await self.adapter._refresh_notifications("mentions", initial=True)
        self.callback.assert_not_awaited()
        await self.adapter._refresh_notifications("mentions")
        await self.adapter._refresh_notifications("mentions")
        self.callback.assert_awaited_once()
        self.assertEqual(self.callback.await_args.args[0].notification_id, "new")

    async def test_pagination_recovers_all_new_events_in_time_order(self):
        self.adapter._notification_seen["likes"][("old", 9)] = None
        self.api.get_notifications = AsyncMock(
            side_effect=[
                NotificationPage(
                    message_list=[notice("liked/item", "newest", 12)],
                    has_more=True,
                    cursor="9007199254740999",
                ),
                NotificationPage(
                    message_list=[
                        notice("liked/item", "older", 11),
                        notice("liked/item", "old", 9),
                    ]
                ),
            ]
        )
        await self.adapter._refresh_notifications("likes")
        self.assertEqual(
            [call.args[0].notification_id for call in self.callback.await_args_list],
            ["older", "newest"],
        )
        self.assertEqual(
            self.api.get_notifications.await_args.kwargs["cursor"], "9007199254740999"
        )

    async def test_unknown_and_malformed_notifications_do_not_block_valid_event(self):
        unknown = {
            "id": "unknown",
            "time": 11,
            "type": "new/type",
            "secret": "private-text",
        }
        malformed = notice(identifier="broken", timestamp=12)
        malformed.pop("user_info")
        self.api.get_notifications = AsyncMock(
            return_value=NotificationPage(
                message_list=[
                    unknown,
                    malformed,
                    notice(identifier="valid", timestamp=13),
                ]
            )
        )
        with patch("myfish.adapters.xhs.logger") as logger:
            await self.adapter._refresh_notifications("mentions")
        self.callback.assert_awaited_once()
        self.assertEqual(logger.debug.call_count, 2)
        self.assertNotIn("private-text", str(logger.debug.call_args_list))

    async def test_repeated_follow_with_changed_timestamp_is_new_event(self):
        self.adapter._notification_seen["connections"][("same-id", 9)] = None
        self.api.get_notifications = AsyncMock(
            return_value=NotificationPage(
                message_list=[notice("follow/you", "same-id", 11)]
            )
        )
        await self.adapter._refresh_notifications("connections")
        self.assertIsInstance(self.callback.await_args.args[0], XhsFollowEvent)

    async def test_failed_fetch_preserves_seen_state(self):
        self.api.get_notifications = AsyncMock(
            side_effect=XhsAPIError(status=200, code=-1)
        )
        await self.adapter._refresh_notifications("mentions")
        self.assertEqual(self.adapter._notification_seen["mentions"], {})
        self.callback.assert_not_awaited()

    async def test_engine_dispatches_typed_notification_to_plugin(self):
        engine = Engine()
        plugin = Plugin(PluginMetadata(id="test", name="test"))
        received = AsyncMock()

        @plugin.on_event(XhsCommentEvent)
        async def handle(event: XhsCommentEvent):
            await received(event)

        engine.plugin_manager.plugins["test"] = plugin
        event = notification_event("self", Notification.model_validate(notice()))
        await engine.dispatch_event(Bot(self.adapter), event)

        await asyncio.gather(*engine._background_tasks)
        received.assert_awaited_once_with(event)


class NotificationAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_query_preserves_string_cursor(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "code": 0,
                    "data": {
                        "message_list": [],
                        "cursor": 1,
                        "strCursor": "9007199254740999",
                        "has_more": False,
                    },
                },
            )

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(respond)
        ) as api:
            page = await api.get_notifications("connections", cursor="9007199254740998")
        self.assertEqual(page.cursor, "9007199254740999")
        self.assertEqual(requests[0].url.path, "/api/sns/web/v1/you/connections")
        self.assertEqual(requests[0].url.params["cursor"], "9007199254740998")
