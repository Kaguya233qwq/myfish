import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI, FishAPIError
from myfish.adapters.fish.orders import order_conversation


def conversation(cid="chat", *, buyer="22", item="99", seller="11", order=""):
    extension = {"itemId": item, "itemSellerId": seller}
    if order:
        extension["orderId"] = order
    return {
        "singleChatUserConversation": {
            "singleChatConversation": {
                "cid": cid + "@goofish",
                "pairFirst": "11@goofish",
                "pairSecond": buyer + "@goofish",
                "extension": extension,
            }
        }
    }


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def test_matches_buyer_item_and_seller_and_prefers_order(self):
        rows = [
            conversation("wrong-buyer", buyer="33"),
            conversation("wrong-item", item="98"),
            conversation("wrong-seller", seller="33"),
            conversation("wrong-order", order="101"),
            conversation("general"),
            conversation("order-chat", order="100"),
        ]
        self.assertEqual(
            order_conversation(rows, "11", "22", "99", "100"), "order-chat"
        )

    def test_single_item_conversation_is_usable_without_order_extension(self):
        self.assertEqual(
            order_conversation([conversation()], "11", "22", "99", "100"), "chat"
        )

    def test_ambiguous_or_invalid_conversations_cannot_be_selected(self):
        with self.assertRaises(ValueError):
            order_conversation(
                [conversation("a"), conversation("b")], "11", "22", "99", "100"
            )
        self.assertEqual(
            order_conversation(
                [None, {}, conversation("bad@id")], "11", "22", "99", "100"
            ),
            "",
        )

    async def test_response_is_routed_while_push_messages_still_dispatch(self):
        api = FishAPI({"unb": "11"})
        self.addAsyncCleanup(api.close)
        adapter = FishAdapter(api)
        adapter.bot_id = "11"
        adapter._active_ws = AsyncMock()
        requests = asyncio.Queue()
        adapter._active_ws.send.side_effect = requests.put
        task = asyncio.create_task(api.get_order_conversation("100", "22", "99"))
        request = json.loads(await requests.get())
        self.assertEqual(request["lwp"], "/r/Conversation/listNewest")
        self.assertEqual(request["body"], [0, 1000])
        callback = AsyncMock()
        adapter.set_callback(callback)
        payload = {
            "1": {
                "2": "chat@goofish",
                "10": {"senderUserId": "22"},
                "6": {"contentType": 1, "text": {"text": "hi"}},
            }
        }
        await adapter._handle_raw_message(
            json.dumps({"body": {"syncPushPackage": {"data": [{"data": payload}]}}})
        )
        await adapter._handle_raw_message(
            json.dumps(
                {
                    "code": 200,
                    "headers": {"mid": request["headers"]["mid"]},
                    "body": [conversation()],
                }
            )
        )
        self.assertEqual(await task, "chat")
        callback.assert_awaited_once()
        self.assertEqual(adapter._pending_requests, {})

    async def test_rejected_and_cancelled_requests_are_cleaned_up(self):
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        adapter._active_ws = AsyncMock()
        requests = asyncio.Queue()
        adapter._active_ws.send.side_effect = requests.put
        task = asyncio.create_task(
            adapter._request_ws("/r/Conversation/listNewest", [0, 1000])
        )
        request = json.loads(await requests.get())
        await adapter._handle_raw_message(
            json.dumps({"code": 403, "headers": {"mid": request["headers"]["mid"]}})
        )
        with self.assertRaises(FishAPIError):
            await task
        task = asyncio.create_task(
            adapter._request_ws("/r/Conversation/listNewest", [0, 1000])
        )
        await requests.get()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(adapter._pending_requests, {})


if __name__ == "__main__":
    unittest.main()
