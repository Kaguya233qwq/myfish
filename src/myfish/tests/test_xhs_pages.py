import unittest

import httpx

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError


def chat(identifier: str) -> dict:
    return {
        "user_id": "self",
        "chat_user_id": identifier,
        "start_store_id": 0,
        "max_store_id": 1,
        "last_msg_content": "",
        "last_msg_time": 0,
        "update_time": 0,
        "chat_status": 0,
        "info": {},
    }


def message(identifier: str, store_id: int) -> dict:
    return {
        "id": identifier,
        "uuid": identifier,
        "sender_id": "peer",
        "receiver_id": "self",
        "store_id": store_id,
        "created_at": 0,
        "content": "private",
        "group_chat": False,
        "revoked": False,
    }


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_timestamp_cursor_and_overlap(self):
        requests = []
        pages = iter(
            [
                {
                    "page": 0,
                    "total": 2,
                    "next_ts": 100,
                    "chats": [chat("a"), chat("b")],
                },
                {"page": 0, "total": 2, "next_ts": -1, "chats": [chat("b"), chat("c")]},
            ]
        )

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"success": True, "code": 0, "data": next(pages)}
            )

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            result = [item.chat_user_id async for item in api.iter_chats(limit=2)]
        self.assertEqual(result, ["a", "b", "c"])
        self.assertNotIn("next_ts", requests[0].url.params)
        self.assertEqual(requests[1].url.params["next_ts"], "100")

    async def test_history_cursor_overlap_and_stalled_page(self):
        requests = []
        pages = iter(
            [
                [message("a", 10), message("b", 9)],
                [message("b", 9), message("c", 8)],
                [message("c", 8)],
            ]
        )

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "code": 0,
                    "data": {
                        "out_message_list": next(pages),
                        "result": {"success": True, "code": 0},
                    },
                },
            )

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            result = [
                item.id async for item in api.iter_message_history("peer", limit=2)
            ]
        self.assertEqual(result, ["a", "b", "c"])
        self.assertEqual([r.url.params["last_id"] for r in requests], ["0", "9", "8"])

    async def test_nested_history_rejection_and_bad_schema_are_sanitized(self):
        for data in (
            {
                "out_message_list": [],
                "result": {"success": False, "code": 42, "message": "private"},
            },
            {"private": "sensitive response"},
        ):
            async with XhsAPI(
                {"a1": "a" * 52},
                transport=httpx.MockTransport(
                    lambda _, data=data: httpx.Response(
                        200, json={"success": True, "code": 0, "data": data}
                    )
                ),
            ) as api:
                with self.assertRaises(XhsAPIError) as error:
                    await api.get_message_history("peer")
                self.assertNotIn("private", str(error.exception))
                self.assertNotIn("sensitive", str(error.exception))
