import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from myfish.adapters.bilibili.api import (
    BilibiliAPI,
    BilibiliAPIError,
    BilibiliProtocolError,
)
from myfish.adapters.bilibili.auth import load_state, login, save_state
from myfish.adapters.bilibili.model import Account
from myfish.adapters.bilibili.sign import sign_wbi

_COOKIES = {"SESSDATA": "test-session", "bili_jct": "test-csrf", "DedeUserID": "1"}


class BilibiliAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_send_checks_business_status_and_never_retries(self):
        for status, code in ((200, -101), (200, -509), (429, None), (503, None)):
            with self.subTest(status=status, code=code):
                requests = []

                def handle(
                    request,
                    captured=requests,
                    response_status=status,
                    response_code=code,
                ):
                    captured.append(request)
                    return httpx.Response(
                        response_status, json={"code": response_code, "data": {}}
                    )

                async with BilibiliAPI(
                    _COOKIES, transport=httpx.MockTransport(handle)
                ) as api:
                    api.account = Account(mid=1, uname="test", isLogin=True)
                    with self.assertRaises(BilibiliAPIError) as raised:
                        await api.send_text(2, "hello")
                    self.assertEqual(raised.exception.status, status)
                    self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(requests), 1)

    async def test_send_returns_receipt_with_stable_device_and_csrf(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"code": 0, "data": {"msg_key": 18446744073709551615}}
            )

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            api.account = Account(mid=1, uname="test", isLogin=True)
            receipt = await api.send_text(2, "I'm here，中文")
            self.assertEqual(receipt.msg_key, 18446744073709551615)
            form = httpx.QueryParams(requests[0].content.decode())
            self.assertEqual(form["msg[dev_id]"], api.device_id)
            self.assertEqual(form["csrf"], "test-csrf")
            self.assertEqual(
                json.loads(form["msg[content]"])["content"], "I'm here，中文"
            )

    async def test_non_json_and_missing_fields_are_protocol_errors(self):
        for response in (
            httpx.Response(200, text="html"),
            httpx.Response(200, json={"code": 0, "data": {}}),
        ):
            async with BilibiliAPI(
                transport=httpx.MockTransport(lambda _, result=response: result)
            ) as api:
                with self.assertRaises(BilibiliProtocolError):
                    await api.get_self_info()

    async def test_protocol_error_reports_field_without_exposing_payload(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "mid": 1,
                            "isLogin": True,
                            "uname": {"token": "private-value"},
                        },
                    },
                )
            )
        ) as api:
            with self.assertRaises(BilibiliProtocolError) as raised:
                await api.get_self_info()
        message = str(raised.exception)
        self.assertIn("Account", message)
        self.assertIn("uname", message)
        self.assertIn("string_type", message)
        self.assertNotIn("private-value", message)

    async def test_message_request_uses_sequence_bounds_and_explicit_size(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "messages": None,
                        "has_more": 0,
                        "min_seqno": 0,
                        "max_seqno": 0,
                    },
                },
            )

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            await api.get_messages(2, begin_seqno=100, end_seqno=200)
        params = requests[0].url.params
        self.assertEqual(params["begin_seqno"], "100")
        self.assertEqual(params["end_seqno"], "200")
        self.assertEqual(params["size"], "100")

    async def test_wbi_keys_are_cached_across_paginated_requests(self):
        requests = []

        def handle(request):
            requests.append(request)
            data = (
                {
                    "wbi_img": {
                        "img_url": "https://i.example/" + "a" * 32 + ".png",
                        "sub_url": "https://i.example/" + "b" * 32 + ".png",
                    }
                }
                if request.url.path.endswith("/nav")
                else {"list": {"vlist": []}}
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            await api.get_up_videos(2)
            await api.get_up_videos(2, page=2)
        self.assertEqual(len(requests), 3)
        self.assertIn("w_rid", requests[-1].url.params)
        self.assertEqual(requests[-1].url.params["pn"], "2")

    async def test_read_apis_parse_typed_results(self):
        data = [
            {"card": {"mid": "2", "name": "user"}},
            {"list": [{"mid": 2, "uname": "user"}], "total": 1},
            {"list": [{"id": 123, "title": "favorites"}]},
            {"medias": [{"bvid": "BVtest", "title": "video"}], "has_more": False},
        ]

        def handle(_):
            return httpx.Response(200, json={"code": 0, "data": data.pop(0)})

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            self.assertEqual((await api.get_user_info(2)).name, "user")
            self.assertEqual((await api.get_fans(2)).total, 1)
            self.assertEqual((await api.get_favorite_folders(2)).folders[0].id, 123)
            self.assertEqual(
                (await api.get_favorite_videos(123)).medias[0].bvid, "BVtest"
            )

    async def test_video_queries_preserve_all_parts_and_download_segments(self):
        responses = [
            {
                "bvid": "BVtest",
                "title": "video",
                "cid": 10,
                "pages": [
                    {"cid": 10, "page": 1, "part": "first"},
                    {"cid": 20, "page": 2, "part": "second"},
                ],
            },
            {
                "quality": 32,
                "durl": [
                    {"url": "https://test.invalid/1", "size": 100, "length": 1000},
                    {"url": "https://test.invalid/2", "size": 200, "length": 2000},
                ],
            },
        ]

        def handle(request):
            data = (
                {
                    "wbi_img": {
                        "img_url": "https://test.invalid/" + "a" * 32 + ".png",
                        "sub_url": "https://test.invalid/" + "b" * 32 + ".png",
                    }
                }
                if request.url.path.endswith("/nav")
                else responses.pop(0)
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            self.assertEqual(len((await api.get_video_info("BVtest")).pages), 2)
            result = await api.get_play_urls("BVtest", 10)
            self.assertEqual(len(result.durl), 2)
            self.assertEqual(result.quality, 32)

    async def test_invalid_identifiers_and_bounds_do_not_issue_requests(self):
        def handle(_):
            self.fail("Invalid input issued an HTTP request")

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            for method, args, kwargs in (
                (api.get_new_sessions, (-1,), {}),
                (api.get_sessions, (), {"begin_ts": 10, "end_ts": 5}),
                (api.get_messages, (2,), {"begin_seqno": 10, "end_seqno": 5}),
                (api.get_fans, (0,), {}),
                (api.get_up_videos, (2,), {"size": 1000}),
                (api.get_play_urls, ("", 10), {}),
            ):
                with (
                    self.subTest(method=method.__name__),
                    self.assertRaises(ValueError),
                ):
                    await method(*args, **kwargs)

    async def test_network_failure_keeps_saved_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"

            def handle(_):
                raise httpx.ConnectError("offline")

            async with BilibiliAPI(
                _COOKIES, transport=httpx.MockTransport(handle)
            ) as api:
                save_state(api, path)
                original = path.read_bytes()
                with self.assertRaises(httpx.ConnectError):
                    await api.get_self_info()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(load_state(path).cookies, _COOKIES)

    async def test_qr_login_uses_https_and_validates_account_before_saving(self):
        requests = []

        def handle(request):
            requests.append(request)
            headers = []
            if request.url.path.endswith("/generate"):
                data = {"url": "https://test.invalid/qr", "qrcode_key": "test-key"}
            elif request.url.path.endswith("/poll"):
                data = {"code": 0}
                headers = [
                    ("set-cookie", f"{name}={value}; Domain=.bilibili.com; Path=/")
                    for name, value in _COOKIES.items()
                ]
            else:
                data = {"mid": 1, "uname": "test", "isLogin": True}
            return httpx.Response(200, headers=headers, json={"code": 0, "data": data})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
                with patch("myfish.adapters.bilibili.auth.show_qrcode"):
                    await login(api, path)
                self.assertEqual(api.account.mid, 1)
                self.assertEqual(load_state(path).device_id, api.device_id)
                self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(request.url.scheme == "https" for request in requests))

    async def test_qr_expiry_does_not_replace_existing_state(self):
        def handle(request):
            data = (
                {"url": "https://test.invalid/qr", "qrcode_key": "test-key"}
                if request.url.path.endswith("/generate")
                else {"code": 86038}
            )
            return httpx.Response(200, json={"code": 0, "data": data})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text("existing")
            async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
                with (
                    patch("myfish.adapters.bilibili.auth.show_qrcode"),
                    self.assertRaises(TimeoutError),
                ):
                    await login(api, path)
            self.assertEqual(path.read_text(), "existing")


class BilibiliSigningTests(unittest.TestCase):
    def test_wbi_matches_bilink_vector_without_mutating_input(self):
        params = {"foo": "114", "bar": "514", "baz": 1919810}
        signed = sign_wbi(
            params,
            "7cd084941338484aae1ad9425b84077c",
            "4932caff0ff746eab6f01bf08b70ac45",
            1702204169,
        )
        self.assertEqual(signed["w_rid"], "6149fdadf571698ca7e6a567265cd0ee")
        self.assertNotIn("wts", params)

    def test_wbi_filters_reserved_characters_and_replaces_stale_signature(self):
        signed = sign_wbi(
            {"text": "!'()*中文", "w_rid": "stale"}, "a" * 32, "b" * 32, 1
        )
        self.assertEqual(signed["text"], "中文")
        self.assertNotEqual(signed["w_rid"], "stale")
