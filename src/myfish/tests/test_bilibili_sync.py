import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from myfish.adapters.bilibili.api import BilibiliAPI, BilibiliProtocolError
from myfish.adapters.bilibili.model import Message, MessagePage, Session, SessionPage
from myfish.adapters.bilibili.sync import MessageStore, MessageSync


def session(talker=2, seq=100, timestamp=3_000_000):
    return Session(
        talker_id=talker, session_type=1, session_ts=timestamp, max_seqno=seq
    )


def message(seq, *, sender=2, receiver=1, status=0):
    return Message(
        sender_uid=sender,
        receiver_id=receiver,
        receiver_type=1,
        msg_type=1,
        content='{"content":"hello"}',
        msg_seqno=seq,
        msg_key=18446744073709550000 + seq,
        timestamp=1000,
        msg_status=status,
    )


def page(*messages, more=0):
    return MessagePage(
        messages=list(messages),
        has_more=more,
        min_seqno=min((item.msg_seqno for item in messages), default=0),
        max_seqno=max((item.msg_seqno for item in messages), default=0),
    )


class BilibiliSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "messages.sqlite3"
        self.store = MessageStore(self.path, 1)
        self.store.initialize([session()])
        self.api = MagicMock(spec=BilibiliAPI)
        self.sync = MessageSync(self.api, self.store)

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_burst_backfills_pages_and_deduplicates_same_second_messages(self):
        self.api.get_new_sessions.return_value = SessionPage(
            session_list=[session(seq=103)], has_more=0
        )
        self.api.get_messages.side_effect = [
            page(message(103), message(102), more=1),
            page(message(102), message(101)),
        ]
        self.assertEqual(await self.sync.poll(), 3)
        self.assertEqual(
            [item.msg_seqno for _, item in self.store.pending()], [101, 102, 103]
        )
        self.assertEqual(self.store.cursor(2), 103)
        self.assertEqual(
            self.api.get_messages.call_args_list[1].kwargs,
            {"begin_seqno": 100, "end_seqno": 102},
        )
        self.assertEqual(await self.sync.poll(), 0)
        self.assertEqual(self.api.get_messages.await_count, 2)

    async def test_failed_later_page_does_not_commit_cursor_or_partial_inbox(self):
        self.api.get_new_sessions.return_value = SessionPage(
            session_list=[session(seq=103, timestamp=5_000_000)], has_more=0
        )
        self.api.get_messages.side_effect = [
            page(message(103), more=1),
            BilibiliProtocolError("failed"),
        ]
        with self.assertRaises(BilibiliProtocolError):
            await self.sync.poll()
        self.assertEqual(self.store.cursor(2), 100)
        self.assertEqual(self.store.watermark(), 3_000_000)
        self.assertEqual(self.store.pending(), [])

    async def test_incomplete_or_stalled_history_does_not_advance(self):
        self.api.get_new_sessions.return_value = SessionPage(
            session_list=[session(seq=103)], has_more=0
        )
        for pages in (
            [page(message(102))],
            [page(more=1)],
            [page(message(103), more=1)] * 2,
        ):
            with self.subTest(pages=pages):
                self.api.get_messages.side_effect = pages
                with self.assertRaises(BilibiliProtocolError):
                    await self.sync.poll()
                self.assertEqual(self.store.cursor(2), 100)

    async def test_foreign_session_message_is_rejected(self):
        self.api.get_new_sessions.return_value = SessionPage(
            session_list=[session(seq=101)], has_more=0
        )
        self.api.get_messages.return_value = page(message(101, receiver=99))
        with self.assertRaises(BilibiliProtocolError):
            await self.sync.poll()
        self.assertEqual(self.store.pending(), [])

    async def test_truncated_discovery_falls_back_to_overlapping_session_pages(self):
        self.api.get_new_sessions.return_value = SessionPage(
            session_list=[session(seq=101)], has_more=1
        )
        self.api.get_sessions.side_effect = [
            SessionPage(
                session_list=[session(seq=101, timestamp=5_000_000)], has_more=1
            ),
            SessionPage(
                session_list=[
                    session(seq=101, timestamp=5_000_000),
                    session(3, seq=1, timestamp=4_000_000),
                ],
                has_more=0,
            ),
        ]
        self.api.get_messages.side_effect = [
            page(message(101)),
            page(message(1, sender=3)),
        ]
        self.assertEqual(await self.sync.poll(), 2)
        self.api.get_sessions.assert_any_await(begin_ts=1_000_000, end_ts=5_000_001)
        self.assertEqual(self.store.watermark(), 5_000_000)

    async def test_equal_timestamp_page_without_progress_fails_closed(self):
        self.api.get_new_sessions.return_value = SessionPage(has_more=1)
        self.api.get_sessions.return_value = SessionPage(
            session_list=[session(seq=101)], has_more=1
        )
        with self.assertRaises(BilibiliProtocolError):
            await self.sync.poll()
        self.assertEqual(self.api.get_sessions.await_count, 2)
        self.assertEqual(self.store.cursor(2), 100)

    async def test_failed_discovery_keeps_watermark(self):
        self.api.get_new_sessions.side_effect = BilibiliProtocolError("failed")
        with self.assertRaises(BilibiliProtocolError):
            await self.sync.poll()
        self.assertEqual(self.store.watermark(), 3_000_000)

    async def test_failed_conversation_does_not_block_other_conversations(self):
        self.api.get_new_sessions.return_value = SessionPage(
            session_list=[session(seq=101, timestamp=5_000_000), session(3, seq=1)],
            has_more=0,
        )
        self.api.get_messages.side_effect = [
            BilibiliProtocolError("failed"),
            page(message(1, sender=3)),
        ]
        with self.assertRaises(BilibiliProtocolError):
            await self.sync.poll()
        self.assertEqual(self.store.cursor(2), 100)
        self.assertEqual(self.store.cursor(3), 1)
        self.assertEqual(self.store.watermark(), 3_000_000)
        self.assertEqual(self.store.pending()[0][0], 3)

    async def test_first_start_only_establishes_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MessageStore(Path(directory) / "state.sqlite3", 1)
            try:
                sync = MessageSync(self.api, store)
                self.api.get_sessions.return_value = SessionPage(
                    session_list=[session(seq=300)], has_more=0
                )
                self.assertTrue(await sync.initialize())
                self.assertEqual(store.cursor(2), 300)
                self.assertEqual(store.pending(), [])
                self.assertFalse(await sync.initialize())
                self.api.get_messages.assert_not_awaited()
            finally:
                store.close()

    async def test_reconcile_finds_sessions_outside_discovery_window(self):
        self.api.get_sessions.return_value = SessionPage(
            session_list=[session(seq=101)], has_more=0
        )
        self.api.get_messages.return_value = page(message(101))
        self.assertEqual(await self.sync.poll(reconcile=True), 1)
        self.api.get_new_sessions.assert_not_awaited()

    def test_reopen_preserves_pending_messages_and_consumed_cursor(self):
        self.store.stage(2, [message(102), message(101)])
        self.store.acknowledge(2, 101)
        reopened = MessageStore(self.path, 1)
        try:
            self.assertEqual(reopened.cursor(2), 102)
            self.assertEqual([item.msg_seqno for _, item in reopened.pending()], [102])
            self.assertEqual(reopened.pending()[0][1].msg_key, 18446744073709550102)
            self.assertEqual(reopened.stage(2, [message(101), message(102)]), 0)
        finally:
            reopened.close()

    def test_account_and_talker_cursors_are_isolated(self):
        self.store.stage(2, [message(101)])
        self.store.stage(3, [message(1, sender=3)])
        other = MessageStore(self.path, 99)
        try:
            self.assertIsNone(other.watermark())
            self.assertEqual(other.cursor(2), 0)
            self.assertEqual(other.pending(), [])
            self.assertEqual(self.store.cursor(3), 1)
        finally:
            other.close()

    def test_outgoing_and_revoked_messages_only_advance_cursor(self):
        self.store.stage(
            2, [message(101, sender=1, receiver=2), message(102, status=1)]
        )
        self.assertEqual(self.store.cursor(2), 102)
        self.assertEqual(self.store.pending(), [])
