import hashlib
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError
from myfish.adapters.xhs.utils import MNS_ALPHABET, decode_base64


class XhsAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_post_signature_binds_the_transmitted_utf8_bytes(self):
        requests: list[httpx.Request] = []

        async def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True, "code": 0, "data": {}})

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            await api.post("/api/test", {"text": "中文😀", "number": 42})
        request = requests[0]
        self.assertEqual(request.content, '{"text":"中文😀","number":42}'.encode())
        envelope = json.loads(
            decode_base64(request.headers["x-s"].removeprefix("XYS_"))
        )
        encrypted = decode_base64(
            envelope["x3"].removeprefix("mns0301_"), alphabet=MNS_ALPHABET
        )
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/xhs_signing.json").read_text(
                encoding="utf-8"
            )
        )
        plain = bytes(
            value ^ key
            for value, key in zip(
                encrypted, bytes.fromhex(fixture["keystream"]), strict=True
            )
        )
        seed = struct.unpack_from("<I", plain, 4)[0]
        digest = hashlib.md5(request.url.raw_path + request.content).digest()
        self.assertEqual(
            bytes(value ^ (seed & 0xFF) for value in plain[36:44]), digest[:8]
        )
        self.assertEqual(
            struct.unpack_from("<I", plain, 32)[0],
            len(request.url.raw_path + request.content),
        )

    async def test_cookie_selection_keeps_hosts_separate(self):
        async with XhsAPI({"a1": "a" * 52}) as api:
            api._client.cookies.set("acw_tc", "web", domain="www.xiaohongshu.com")
            api._client.cookies.set("acw_tc", "security", domain="as.xiaohongshu.com")
            self.assertEqual(
                api.cookies_for_url("https://www.xiaohongshu.com/")["acw_tc"], "web"
            )
            self.assertEqual(
                api.cookies_for_url("https://as.xiaohongshu.com/")["acw_tc"], "security"
            )

    async def test_signed_requests_match_new_chat_endpoints(self):
        requests = []

        async def handle(request):
            requests.append(request)
            data = (
                {"page": 2, "total": 0, "next_ts": 0, "chats": []}
                if request.url.path.endswith("chats")
                else {"out_message_list": [], "result": {"success": True, "code": 0}}
            )
            return httpx.Response(200, json={"success": True, "code": 0, "data": data})

        async with XhsAPI(
            {"a1": "a" * 52, "web_session": "test"},
            transport=httpx.MockTransport(handle),
        ) as api:
            await api.get_chats(page=2)
            await api.get_message_history("abc123", last_id=42)
            expected_urls = [
                "https://edith.xiaohongshu.com/api/im/web/v3/chats?limit=20&complete=true&page=2&source=pc",
                "https://edith.xiaohongshu.com/api/im/web/messages/history?chat_user_id=abc123&last_id=42&start_id=0&limit=20",
            ]
            for request, expected in zip(requests, expected_urls, strict=True):
                self.assertEqual(str(request.url), expected)
                self.assertTrue(request.headers["x-s"].startswith("XYS_"))
                self.assertIn("x-s-common", request.headers)
            self.assertEqual(requests[0].url.path, "/api/im/web/v3/chats")
            self.assertEqual(requests[1].url.params["last_id"], "42")

    async def test_http_and_business_rejections_do_not_leak_response(self):
        for status, body in [
            (406, {}),
            (200, {"success": False, "code": -100}),
            (200, {}),
        ]:
            with self.subTest(status=status, body=body):
                async with XhsAPI(
                    {"a1": "a" * 52, "web_session": "secret"},
                    transport=httpx.MockTransport(
                        lambda _, status=status, body=body: httpx.Response(
                            status, json=body
                        )
                    ),
                ) as api:
                    with self.assertRaises(XhsAPIError) as raised:
                        await api.get_self_info()
                    self.assertNotIn("secret", str(raised.exception))

    async def test_invalid_input_does_not_send_request(self):
        async with XhsAPI(
            {"a1": "a" * 52, "web_session": "test"},
            transport=httpx.MockTransport(
                lambda _: self.fail("Unexpected HTTP request")
            ),
        ) as api:
            with self.assertRaises(ValueError):
                await api.get_chats(page=-1)
            with self.assertRaises(ValueError):
                await api.get_message_history("user&injected=1")

    async def test_follow_and_unfollow_send_explicit_target(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"success": True, "code": 0, "data": {}})

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            await api.follow_user("user123")
            await api.unfollow_user("user123")
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/api/sns/web/v1/user/follow",
                "/api/sns/web/v1/user/unfollow",
            ],
        )
        self.assertTrue(
            all(
                json.loads(request.content) == {"target_user_id": "user123"}
                for request in requests
            )
        )

    async def test_relationship_rejections_warn_and_preserve_error_without_retry(self):
        for operation in ("follow_user", "unfollow_user"):
            for code in (-9043, 42):
                requests = []

                def handle(request, response_code=code, captured_requests=requests):
                    captured_requests.append(request)
                    return httpx.Response(
                        200, json={"success": False, "code": response_code}
                    )

                with (
                    self.subTest(operation=operation, code=code),
                    patch("myfish.adapters.xhs.api.logger.warning") as warning,
                ):
                    async with XhsAPI(
                        {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
                    ) as api:
                        with self.assertRaises(XhsAPIError) as error:
                            await getattr(api, operation)("user123")
                    self.assertEqual(error.exception.code, code)
                    self.assertEqual(len(requests), 1)
                    warning.assert_called_once()

    async def test_invalid_relationship_target_never_sends(self):
        async with XhsAPI(
            {"a1": "a" * 52},
            transport=httpx.MockTransport(
                lambda _: self.fail("Unexpected HTTP request")
            ),
        ) as api:
            for operation in (api.follow_user, api.unfollow_user):
                for target in ("", "user&extra=1", "用户"):
                    with self.assertRaises(ValueError):
                        await operation(target)
