import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from myfish.adapters.bilibili.api import BilibiliAPI, BilibiliProtocolError
from myfish.adapters.bilibili.events import BilibiliLikedEvent, notification_event
from myfish.adapters.bilibili.model import (
    LikeHistory,
    LikeNotification,
    LikeNotificationPage,
    LikeSection,
    MentionNotificationPage,
    NotificationCursor,
    NotificationUser,
    ReplyNotificationPage,
)
from myfish.adapters.bilibili.sync import MessageStore, NotificationSync


def liked(identifier=10, timestamp=1000, counts=1, users=(2,)):
    return LikeNotification(
        id=identifier,
        like_time=timestamp,
        counts=counts,
        users=[NotificationUser(mid=mid, nickname=f"user-{mid}") for mid in users],
        item={
            "item_id": 123,
            "pid": 456,
            "type": "reply",
            "business_id": 1,
            "title": "test",
        },
    )


def page(*items, latest=(), end=True, cursor_id=0, timestamp=0):
    return LikeNotificationPage(
        latest=LikeSection(items=list(latest)),
        total=LikeHistory(
            items=list(items),
            cursor=NotificationCursor(is_end=end, id=cursor_id, time=timestamp),
        ),
    )


class BilibiliLikeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "messages.sqlite3"
        self.store = MessageStore(self.path, 1)
        self.api = MagicMock(spec=BilibiliAPI)
        self.api.get_mention_notifications.return_value = MentionNotificationPage(
            cursor=NotificationCursor(is_end=True, id=0, time=0), items=[]
        )
        self.sync = NotificationSync(self.api, self.store)

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_first_like_poll_baselines_history_then_emits_new_liker(self):
        self.api.get_like_notifications.return_value = page(liked())
        self.assertEqual(await self.sync._poll_likes(), 0)
        self.api.get_like_notifications.return_value = page(
            liked(), liked(11, 1001, users=(3,))
        )
        self.assertEqual(await self.sync._poll_likes(), 1)
        pending = self.store.pending_notifications("like")
        event = notification_event("1", pending[0])
        self.assertIsInstance(event, BilibiliLikedEvent)
        self.assertEqual(event.user.mid, 3)
        self.assertEqual(event.target.item_id, 123)
        self.assertEqual(event.target.pid, 456)
        self.assertEqual(event.count, 1)

    async def test_latest_and_total_duplicates_only_dispatch_once(self):
        self.store.stage_notifications([], category="like", baseline=True)
        self.api.get_like_notifications.return_value = page(liked(), latest=[liked()])
        self.assertEqual(await self.sync._poll_likes(), 1)
        self.assertEqual(await self.sync._poll_likes(), 0)
        self.assertEqual(len(self.store.pending_notifications("like")), 1)

    async def test_latest_and_total_snapshots_of_same_notification_emit_once(self):
        self.store.stage_notifications([], category="like", baseline=True)
        self.api.get_like_notifications.return_value = page(
            liked(counts=2, users=(2, 3)), latest=[liked()]
        )
        self.assertEqual(await self.sync._poll_likes(), 1)
        event = notification_event("1", self.store.pending_notifications("like")[0])
        self.assertEqual(event.count, 2)
        self.assertEqual([user.mid for user in event.users], [2])
        self.assertIsNone(event.new_count)
        self.assertEqual(len(event.reported_users), 2)

    async def test_recent_count_one_is_never_subtracted_from_historical_total(self):
        old = liked(timestamp=100, counts=1015, users=(2, 3, 4, 5, 6))
        other = liked(11, 1000)
        self.api.get_like_notifications.return_value = page(other, old)
        self.assertEqual(await self.sync._poll_likes(), 0)
        recent = liked(timestamp=1001, counts=1, users=(7,))
        self.api.get_like_notifications.return_value = page(other, old, latest=[recent])
        self.assertEqual(await self.sync._poll_likes(), 0)
        self.store.close()
        self.store = MessageStore(self.path, 1)
        self.api.get_mention_notifications.return_value = MentionNotificationPage(
            cursor=NotificationCursor(is_end=True, id=0, time=0), items=[]
        )
        self.sync = NotificationSync(self.api, self.store)
        cumulative = liked(timestamp=1001, counts=1016, users=(2, 3, 4, 5, 6))
        self.api.get_like_notifications.return_value = page(cumulative, other)
        self.assertEqual(await self.sync._poll_likes(), 1)
        event = notification_event("1", self.store.pending_notifications("like")[0])
        self.assertEqual(event.new_count, 1)
        self.assertEqual(event.count, 1016)
        self.assertEqual([user.mid for user in event.users], [7])
        self.assertEqual(await self.sync._poll_likes(), 0)

    def test_uncached_historical_total_only_establishes_a_comparable_snapshot(self):
        self.store.stage_notifications([], category="like", baseline=True)
        self.assertEqual(
            self.store.stage_notifications(
                [liked(counts=1016, users=(2, 3, 4))], category="like"
            ),
            0,
        )
        self.assertEqual(self.store.pending_notifications("like"), [])
        self.store.save_like_hints([liked(timestamp=1001, users=(7,))], baseline=False)
        self.assertEqual(
            self.store.stage_notifications(
                [liked(timestamp=1001, counts=1017, users=(2, 3, 7))], category="like"
            ),
            1,
        )
        event = notification_event("1", self.store.pending_notifications("like")[0])
        self.assertEqual(event.new_count, 1)
        self.assertEqual([user.mid for user in event.users], [7])

    def test_legacy_counter_with_unknown_scope_is_recalibrated(self):
        path = Path(self.directory.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(path)
        legacy.execute(
            "CREATE TABLE like_snapshots(uid INTEGER,id TEXT,payload TEXT,users TEXT,PRIMARY KEY(uid,id))"
        )
        legacy.execute(
            "INSERT INTO like_snapshots VALUES(?,?,?,?)",
            (1, "10", liked().model_dump_json(), "[2]"),
        )
        legacy.commit()
        legacy.close()
        store = MessageStore(path, 1)
        try:
            self.assertEqual(
                store.stage_notifications(
                    [liked(timestamp=1001, counts=1016, users=(2, 3, 4))],
                    category="like",
                ),
                0,
            )
            self.assertEqual(store.pending_notifications("like"), [])
        finally:
            store.close()

    def test_historical_users_are_not_assumed_to_be_new_likers(self):
        for identifier, has_hint in ((10, False), (20, True)):
            self.store.stage_notifications(
                [liked(identifier, counts=1015, users=(2, 3, 4, 5, 6))],
                category="like",
                baseline=True,
            )
            if has_hint:
                self.store.save_like_hints(
                    [liked(identifier, timestamp=1001, users=(7,))], baseline=False
                )
            self.assertEqual(
                self.store.stage_notifications(
                    [
                        liked(
                            identifier,
                            timestamp=1001,
                            counts=1016,
                            users=(8, 9, 10, 11, 12),
                        )
                    ],
                    category="like",
                ),
                1,
            )
            event = notification_event(
                "1", self.store.pending_notifications("like")[-1]
            )
            self.assertEqual(event.new_count, 1)
            self.assertEqual(
                [user.mid for user in event.users], [7] if has_hint else []
            )

    async def test_unified_sync_keeps_likes_working_when_reply_endpoint_fails(self):
        self.store.stage_notifications([], category="like", baseline=True)
        self.api.get_reply_notifications.side_effect = BilibiliProtocolError("failed")
        self.api.get_like_notifications.return_value = page(liked())
        self.assertEqual(await self.sync.poll(), 1)
        self.assertEqual(len(self.store.pending_notifications("like")), 1)
        self.assertFalse(self.sync._ready)

    async def test_unified_sync_initializes_all_notification_sources(self):
        self.api.get_reply_notifications.return_value = ReplyNotificationPage(
            cursor=NotificationCursor(is_end=True, id=0, time=0), items=[]
        )
        self.api.get_like_notifications.return_value = page()
        self.assertEqual(await self.sync.poll(), 0)
        self.assertTrue(self.sync._ready)
        self.api.get_reply_notifications.assert_awaited_once()
        self.api.get_like_notifications.assert_awaited_once()
        self.api.get_mention_notifications.assert_awaited_once()

    async def test_aggregated_notification_update_retains_all_returned_users(self):
        self.store.stage_notifications([liked()], category="like", baseline=True)
        updated = liked(timestamp=1001, counts=8, users=(2, 3))
        self.api.get_like_notifications.return_value = page(updated)
        self.assertEqual(await self.sync._poll_likes(), 1)
        event = notification_event("1", self.store.pending_notifications("like")[0])
        self.assertEqual(event.users, [])
        self.assertIsNone(event.user)
        self.assertEqual([user.mid for user in event.reported_users], [2, 3])
        self.assertEqual(event.new_count, 7)
        self.assertEqual(event.count, 8)
        self.assertEqual(await self.sync._poll_likes(), 0)

    async def test_user_order_and_read_marker_do_not_create_duplicate_likes(self):
        first = liked(counts=2, users=(2, 3))
        self.store.stage_notifications([first], category="like", baseline=True)
        changed = liked(counts=2, users=(3, 2))
        changed = LikeNotification.model_validate(
            {**changed.model_dump(), "notice_state": 99}
        )
        self.api.get_like_notifications.return_value = page(changed)
        self.assertEqual(await self.sync._poll_likes(), 0)

    async def test_timestamp_only_change_and_user_list_rotation_do_not_emit_likes(self):
        self.store.stage_notifications(
            [liked(counts=2, users=(2, 3))], category="like", baseline=True
        )
        self.api.get_like_notifications.return_value = page(
            liked(timestamp=1001, counts=2, users=(3,))
        )
        self.assertEqual(await self.sync._poll_likes(), 0)
        self.api.get_like_notifications.return_value = page(
            liked(timestamp=1002, counts=2, users=(2,))
        )
        self.assertEqual(await self.sync._poll_likes(), 0)

    async def test_count_decrease_does_not_emit_liked_event(self):
        self.store.stage_notifications(
            [liked(counts=2, users=(2, 3))], category="like", baseline=True
        )
        self.api.get_like_notifications.return_value = page(
            liked(timestamp=1001, counts=1, users=(2,))
        )
        self.assertEqual(await self.sync._poll_likes(), 0)
        self.assertEqual(self.store.pending_notifications("like"), [])

    async def test_increment_without_new_user_does_not_guess_liker(self):
        self.store.stage_notifications([liked()], category="like", baseline=True)
        self.api.get_like_notifications.return_value = page(
            liked(timestamp=1001, counts=2)
        )
        self.assertEqual(await self.sync._poll_likes(), 1)
        event = notification_event("1", self.store.pending_notifications("like")[0])
        self.assertEqual(event.users, [])
        self.assertIsNone(event.user)
        self.assertEqual(event.new_count, 1)
        self.assertEqual([user.mid for user in event.reported_users], [2])

    async def test_failed_later_page_does_not_commit_like_progress(self):
        self.store.stage_notifications([liked()], category="like", baseline=True)
        self.api.get_like_notifications.side_effect = [
            page(liked(11, 1001), end=False, cursor_id=11, timestamp=1001),
            BilibiliProtocolError("failed"),
        ]
        with self.assertRaises(BilibiliProtocolError):
            await self.sync._poll_likes()
        self.assertEqual(self.store.notification_time("like"), 1000)
        self.assertEqual(self.store.pending_notifications("like"), [])

    async def test_like_pagination_preserves_cursor_and_overlap(self):
        self.store.stage_notifications([], category="like", baseline=True)
        self.api.get_like_notifications.side_effect = [
            page(liked(11, 1001), end=False, cursor_id=11, timestamp=1001),
            page(liked(), latest=[liked(11, 1001)]),
        ]
        self.assertEqual(await self.sync._poll_likes(), 2)
        self.api.get_like_notifications.assert_any_await(cursor_id=11, like_time=1001)

    async def test_http_like_request_parses_latest_and_total_and_uses_like_cursor(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"code": 0, "data": page(liked()).model_dump()}
            )

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            result = await api.get_like_notifications(cursor_id=10, like_time=1000)
        self.assertEqual(result.total.items[0].users[0].mid, 2)
        self.assertEqual(requests[0].url.path, "/x/msgfeed/like")
        self.assertEqual(requests[0].url.params["like_time"], "1000")

    def test_like_revisions_and_pending_state_survive_reopen(self):
        first = liked()
        second = liked(timestamp=1001, counts=2, users=(3, 2))
        self.store.stage_notifications([first, second], category="like")
        self.store.acknowledge_notification(first.key, "like")
        reopened = MessageStore(self.path, 1)
        try:
            self.assertEqual(len(reopened.pending_notifications("like")), 1)
            self.assertEqual(
                reopened.stage_notifications([first, second], category="like"), 0
            )
            self.assertEqual(reopened.pending_notifications(), [])
        finally:
            reopened.close()
