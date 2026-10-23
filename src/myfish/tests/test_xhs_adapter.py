import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from myfish.adapters.xhs import XhsAdapter
from myfish.adapters.xhs.api import XhsAPI
from myfish.adapters.xhs.model import PushMessage, XhsConfig
from myfish.core.message import Image, MessageChain, Text


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name) / "adapters" / "xhs"
        storage = patch(
            "myfish.adapters.xhs.utils.StorageManager.get_adapter_dir",
            return_value=self.data_dir,
        )
        self.storage = storage.start()
        self.addCleanup(storage.stop)
        self.adapter = XhsAdapter(XhsAPI.create(), XhsConfig())
        self.adapter.bot_id = "self"
        self.callback = AsyncMock()
        self.adapter._on_receive_callback = self.callback

    def message(self, **changes):
        payload = {
            "sender": "peer",
            "receiver": "self",
            "timestamp": self.adapter._started_at_ms,
            "message_id": "serverid",
            "content": json.dumps(
                {"nickname": "昵称", "content_type": 1, "content": "hello"}
            ),
        }
        payload.update(changes)
        return PushMessage(
            client_id="clientid",
            id="pushid",
            timestamp_ms=1,
            payload=json.dumps(payload),
        )

    async def test_inbound_message_becomes_framework_event(self):
        await self.adapter._handle_message(self.message())
        event = self.callback.await_args.args[0]
        self.assertEqual(event.sender_id, "peer")
        self.assertEqual(event.cid, "peer")
        self.assertEqual(event.sender_name, "昵称")
        self.assertEqual(event.plain_text, "hello")

    async def test_self_group_revoked_other_recipient_and_stale_are_ignored(self):
        for changes in (
            {"sender": "self"},
            {"group_chat": True},
            {"revoked": True},
            {"receiver": "other"},
            {"timestamp": 1},
        ):
            await self.adapter._handle_message(self.message(**changes))
        self.callback.assert_not_awaited()

    async def test_offline_push_is_opt_in(self):
        self.adapter.config.receive_offline = True
        await self.adapter._handle_message(self.message(timestamp=1))
        self.callback.assert_awaited_once()

    async def test_unsupported_segment_rejects_entire_chain_before_send(self):
        with self.assertRaises(ValueError):
            await self.adapter.send(
                "peer",
                MessageChain(
                    [Text(text="hello"), Image(image_url="https://example.com/image")]
                ),
            )

    def test_authentication_files_use_adapter_storage(self):
        self.storage.assert_called_with("xhs")
        self.assertEqual(self.adapter.auth_file, self.data_dir / "auth.json")
        self.assertEqual(self.adapter.state_file, self.data_dir / "auth.state.json")
