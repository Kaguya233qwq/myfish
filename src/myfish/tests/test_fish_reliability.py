import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from myfish.adapters.fish import FishAdapter, _ensure_auth
from myfish.adapters.fish.api import (
    FishAPI,
    FishAPIError,
    FishListingUnconfirmed,
    FishMessageUnconfirmed,
    token_from_response,
)
from myfish.adapters.fish.message import Content, ItemCard, auto_encode
from myfish.core.message import MessageChain, Text
from myfish.tests.test_fish_listings import snapshot


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_refresh_clears_session_before_qr_login(self):
        async with FishAPI({"unb": "11"}) as api:
            api.client.cookies.set("session", "old")
            api.get_token = AsyncMock(
                side_effect=[
                    {"ret": ["SUCCESS"], "data": {}},
                    {"ret": ["FAIL_SYS_SESSION_EXPIRED"], "data": {}},
                ]
            )
            with (
                patch("myfish.adapters.fish.rm_local_auth") as remove,
                patch(
                    "myfish.adapters.fish.qrcode_login", new_callable=AsyncMock
                ) as qr,
            ):
                qr.return_value = {"unb": "22"}
                self.assertEqual(await _ensure_auth(api), {"unb": "22"})
            self.assertEqual(api.cookies_dict, {})
            self.assertEqual(list(api.client.cookies.items()), [])
            remove.assert_called_once()
            qr.assert_awaited_once_with(api)

    async def test_rejected_refresh_does_not_accept_old_cookies(self):
        api = AsyncMock(spec=FishAPI)
        api.cookies_dict = {"unb": "11"}
        api.get_token.return_value = {"ret": ["FAIL_SYS_UNKNOWN"], "data": {}}
        with patch("myfish.adapters.fish.qrcode_login", new_callable=AsyncMock) as qr:
            self.assertIsNone(await _ensure_auth(api))
            self.assertEqual(api.get_token.await_count, 2)
            qr.assert_not_awaited()

    async def test_missing_nickname_and_successful_second_attempt(self):
        api = AsyncMock(spec=FishAPI)
        api.cookies_dict = {"unb": "11"}
        api.get_token.side_effect = [
            {"ret": ["SUCCESS"], "data": {}},
            {"ret": ["SUCCESS::ok"], "data": {"accessToken": "new"}},
        ]
        self.assertEqual(await _ensure_auth(api), {"unb": "11"})

    async def test_validation_on_refresh_stops_without_scanning(self):
        api = AsyncMock(spec=FishAPI)
        api.cookies_dict = {"unb": "11"}
        api.get_token.side_effect = [
            {"ret": ["SUCCESS"], "data": {}},
            {"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {}},
        ]
        with patch("myfish.adapters.fish.qrcode_login", new_callable=AsyncMock) as qr:
            self.assertIsNone(await _ensure_auth(api))
            qr.assert_not_awaited()

    async def test_token_requires_success_and_nonempty_string(self):
        async with FishAPI({"unb": "11"}) as api:
            for response in (
                {"ret": ["FAIL"], "data": {"accessToken": "old"}},
                {"data": {"accessToken": "old"}},
                {"ret": ["SUCCESS"], "data": None},
                {"ret": ["SUCCESS"], "data": {"accessToken": " "}},
                {"ret": ["SUCCESS"], "data": {"accessToken": 42}},
            ):
                with self.subTest(response=response):
                    api.get_token = AsyncMock(return_value=response)
                    self.assertEqual(await api.get_access_token(), "")
            self.assertEqual(
                token_from_response(
                    {"ret": ["SUCCESS"], "data": {"accessToken": "new"}}
                ),
                "new",
            )


class SendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.adapter = FishAdapter(AsyncMock(spec=FishAPI))
        self.adapter.bot_id = "11"
        self.adapter._active_ws = AsyncMock()
        self.requests = asyncio.Queue()
        self.adapter._active_ws.send.side_effect = self.requests.put

    async def respond(self, request, code):
        await self.adapter._handle_raw_message(
            json.dumps({"code": code, "headers": request["headers"]})
        )

    async def test_waits_for_matching_confirmation_and_preserves_payload(self):
        task = asyncio.create_task(self.adapter.send("22", Text(text="hello"), "chat"))
        request = json.loads(await self.requests.get())
        self.assertFalse(task.done())
        self.assertEqual(request["lwp"], "/r/MessageSend/sendByReceiverScope")
        self.assertEqual(request["body"][0]["cid"], "chat@goofish")
        await self.respond({"headers": {"mid": "unrelated"}}, 200)
        self.assertFalse(task.done())
        await self.respond(request, 200)
        self.assertTrue(await task)
        self.assertEqual(self.adapter._pending_requests, {})

    async def test_rejection_returns_false(self):
        task = asyncio.create_task(self.adapter.send("22", Text(text="hello")))
        request = json.loads(await self.requests.get())
        await self.respond(request, 403)
        self.assertFalse(await task)
        self.assertEqual(self.adapter._pending_requests, {})

    async def test_timeout_is_uncertain_and_never_retries(self):
        timeout = asyncio.timeout
        with (
            patch(
                "myfish.adapters.fish.asyncio.timeout",
                side_effect=lambda _: timeout(0.01),
            ),
            self.assertRaises(FishMessageUnconfirmed),
        ):
            await self.adapter.send("22", Text(text="hello"))
        self.assertEqual(self.requests.qsize(), 1)
        self.assertEqual(self.adapter._pending_requests, {})

    async def test_disconnect_is_uncertain_and_cancellation_propagates(self):
        task = asyncio.create_task(self.adapter.send("22", Text(text="hello")))
        request = json.loads(await self.requests.get())
        self.adapter._pending_requests[request["headers"]["mid"]].set_exception(
            FishAPIError("connection lost")
        )
        with self.assertRaises(FishMessageUnconfirmed):
            await task
        task = asyncio.create_task(self.adapter.send("22", Text(text="hello")))
        await self.requests.get()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.adapter._pending_requests, {})

    async def test_item_card_is_receive_only_and_rejected_before_sending(self):
        content = {
            "contentType": 7,
            "itemCard": {
                "item": {
                    "itemId": "99",
                    "title": "test",
                    "price": "1",
                    "mainPic": "image",
                },
                "action": {"page": {"url": "item"}},
            },
        }
        card = Content.model_validate(content).to_message_chain()[0]
        self.assertIsInstance(card, ItemCard)
        with self.assertRaises(ValueError):
            auto_encode(card)
        self.assertFalse(await self.adapter.send("22", card))
        self.assertFalse(
            await self.adapter.send("22", MessageChain([Text(text="test"), card]))
        )
        self.assertTrue(self.requests.empty())


class TokenRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_refresh_does_not_register_or_close_a_new_connection(self):
        replacement = AsyncMock()

        async def replace_connection():
            self.adapter._active_ws = replacement
            return "new-token"

        self.adapter.api.get_access_token.side_effect = replace_connection
        self.adapter._request_ws = AsyncMock()
        with patch("myfish.adapters.fish.asyncio.sleep", AsyncMock()):
            await self.adapter._keep_token_alive_loop()
        self.adapter._request_ws.assert_not_awaited()
        self.ws.close.assert_not_awaited()
        replacement.close.assert_not_awaited()

    def setUp(self):
        self.adapter = FishAdapter(AsyncMock(spec=FishAPI))
        self.adapter.api.device_id = "device"
        self.adapter.api.get_access_token.return_value = "new-token"
        self.ws = AsyncMock()
        self.adapter._active_ws = self.ws

    async def test_refresh_keeps_registered_session_without_sending_reg_again(self):
        self.adapter._ws_token = "registered-token"
        self.adapter._request_ws = AsyncMock()
        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
        with patch("myfish.adapters.fish.asyncio.sleep", sleep):
            await self.adapter._keep_token_alive_loop()
        self.adapter.api.get_access_token.assert_awaited_once()
        self.adapter._request_ws.assert_not_awaited()
        self.ws.send.assert_not_awaited()
        self.ws.close.assert_not_awaited()
        self.assertEqual(self.adapter._ws_token, "registered-token")
        self.assertEqual(self.adapter._token_refresh_stage, "waiting")
        self.assertEqual(self.adapter._pending_requests, {})

    async def test_failure_closes_connection_for_reconnect(self):
        for failure in ("empty", "http", "rejected", "timeout"):
            with self.subTest(failure=failure):
                self.adapter.api.get_access_token = AsyncMock(return_value="new-token")
                self.adapter._request_ws = AsyncMock()
                if failure == "empty":
                    self.adapter.api.get_access_token.return_value = ""
                elif failure == "http":
                    self.adapter.api.get_access_token.side_effect = httpx.ReadTimeout(
                        "test"
                    )
                else:
                    self.adapter.api.get_access_token.side_effect = (
                        FishAPIError("rejected")
                        if failure == "rejected"
                        else TimeoutError()
                    )
                self.ws.close.reset_mock()
                with patch("myfish.adapters.fish.asyncio.sleep", AsyncMock()):
                    await self.adapter._keep_token_alive_loop()
                self.ws.close.assert_awaited_once()


class EditReadbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_readback_failure_after_submission_is_uncertain_without_retry(self):
        for operation in ("edit", "reshelf"):
            for error in (httpx.ReadTimeout("test"), FishAPIError("invalid response")):
                with self.subTest(operation=operation, error=type(error).__name__):
                    async with FishAPI({"unb": "11"}) as api:
                        api._call_mtop = AsyncMock(
                            return_value={"ret": ["SUCCESS"], "data": {}}
                        )
                        api.get_item_edit_detail = AsyncMock(
                            side_effect=[snapshot(), error]
                            if operation == "reshelf"
                            else error
                        )
                        with self.assertRaises(FishListingUnconfirmed) as caught:
                            if operation == "reshelf":
                                await api.reshelf_item("99", quantity=1)
                            else:
                                await api.submit_item_edit("99", {"itemId": "99"})
                        self.assertIs(caught.exception.__cause__, error)
                        api._call_mtop.assert_awaited_once()
