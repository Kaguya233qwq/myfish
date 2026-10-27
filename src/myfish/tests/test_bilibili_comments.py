import json
import time
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from myfish.adapters.bilibili.api import (
    BilibiliAPI,
    BilibiliAPIError,
    BilibiliProtocolError,
)
from myfish.adapters.bilibili.model import Comment, CommentRef

_COOKIES = {"SESSDATA": "test-session", "bili_jct": "test-csrf", "DedeUserID": "1"}
_REF = CommentRef(oid=123, kind=1)


def comment(identifier, *, root=0, oid=123, kind=1):
    return {
        "rpid": identifier,
        "oid": oid,
        "type": kind,
        "root": root,
        "parent": root,
        "ctime": 1000,
        "like": 2,
        "member": {"mid": "2", "uname": "user"},
        "content": {"message": "I'm here，中文"},
    }


class BilibiliCommentTests(unittest.IsolatedAsyncioTestCase):
    async def test_comment_pagination_preserves_opaque_offset_and_wbi_signature(self):
        requests = []
        offset = '{"type":3,"direction":1,"Data":{"cursor":"next/+=中文"}}'

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("/nav"):
                data = {
                    "wbi_img": {
                        "img_url": "https://i.example/" + "a" * 32 + ".png",
                        "sub_url": "https://i.example/" + "b" * 32 + ".png",
                    }
                }
            elif len(requests) == 2:
                data = {
                    "cursor": {
                        "is_end": False,
                        "pagination_reply": {"next_offset": offset},
                    },
                    "replies": [comment(10), comment(20)],
                    "hots": [comment(99)],
                }
            else:
                params = request.url.params
                self.assertEqual(json.loads(params["pagination_str"])["offset"], offset)
                self.assertEqual(params["mode"], "2")
                self.assertEqual(params["oid"], "123")
                self.assertEqual(params["type"], "1")
                self.assertIn("w_rid", params)
                data = {
                    "cursor": {"is_end": True},
                    "replies": [comment(20), comment(30)],
                }
            return httpx.Response(200, json={"code": 0, "data": data})

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            result = [item async for item in api.iter_comments(_REF)]
        self.assertEqual([item.rpid for item in result], [10, 20, 30])
        self.assertEqual(result[0].member.name, "user")
        self.assertEqual(result[0].content.message, "I'm here，中文")
        self.assertEqual(len(requests), 3)

    async def test_nonadvancing_and_missing_pagination_offsets_raise(self):
        for cursor in (
            {"is_end": False},
            {"is_end": False, "pagination_reply": {"next_offset": ""}},
            {"is_end": False, "pagination_reply": {"next_offset": "same"}},
        ):
            with self.subTest(cursor=cursor):
                requests = []

                def handle(request, captured=requests, result_cursor=cursor):
                    captured.append(request)
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {"cursor": result_cursor, "replies": []},
                        },
                    )

                async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
                    api._wbi = "a" * 32, "b" * 32
                    api._wbi_loaded = time.monotonic()
                    with self.assertRaisesRegex(
                        BilibiliProtocolError, "pagination did not advance"
                    ):
                        _ = [item async for item in api.iter_comments(_REF)]
                self.assertLessEqual(len(requests), 2)

    async def test_comment_for_wrong_subject_is_rejected(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "cursor": {"is_end": True},
                            "replies": [comment(10, oid=999)],
                        },
                    },
                )
            )
        ) as api:
            api._wbi = "a" * 32, "b" * 32
            api._wbi_loaded = time.monotonic()
            with self.assertRaisesRegex(BilibiliProtocolError, "does not match"):
                _ = [item async for item in api.iter_comments(_REF)]

    async def test_send_top_level_root_reply_and_nested_reply(self):
        for root, parent, expected_parent in ((0, 0, 0), (10, 0, 10), (10, 20, 20)):
            with self.subTest(root=root, parent=parent):
                requests = []

                def handle(
                    request,
                    captured=requests,
                    expected_root=root,
                    expected_target=expected_parent,
                ):
                    captured.append(request)
                    form = httpx.QueryParams(request.content.decode())
                    self.assertEqual(form["root"], str(expected_root))
                    self.assertEqual(form["parent"], str(expected_target))
                    self.assertEqual(form["message"], "测试 I'm here")
                    self.assertEqual(form["csrf"], "test-csrf")
                    self.assertEqual(
                        request.headers["origin"], "https://www.bilibili.com"
                    )
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "rpid": 30,
                                "root": expected_root,
                                "parent": expected_target,
                            },
                        },
                    )

                async with BilibiliAPI(
                    _COOKIES, transport=httpx.MockTransport(handle)
                ) as api:
                    receipt = await api.send_comment(
                        _REF, "测试 I'm here", root=root, parent=parent
                    )
                    self.assertEqual(receipt.rpid, 30)
                self.assertEqual(len(requests), 1)

    async def test_reply_to_comment_uses_its_actual_subject_and_parent(self):
        requests = []

        def handle(request):
            requests.append(request)
            form = httpx.QueryParams(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "rpid": 30,
                        "root": int(form["root"]),
                        "parent": int(form["parent"]),
                    },
                },
            )

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            await api.reply_comment(
                Comment.model_validate(comment(10, oid=999, kind=17)), "reply"
            )
            await api.reply_comment(
                Comment.model_validate(comment(20, root=10, oid=999, kind=17)), "reply"
            )
        forms = [httpx.QueryParams(request.content.decode()) for request in requests]
        self.assertEqual((forms[0]["root"], forms[0]["parent"]), ("10", "10"))
        self.assertEqual((forms[1]["root"], forms[1]["parent"]), ("10", "20"))
        self.assertEqual(forms[1]["oid"], "999")
        self.assertEqual(forms[1]["type"], "17")

    async def test_like_and_unlike_preserve_action_and_subject(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0})

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            await api.like_comment(_REF, 10)
            await api.like_comment(_REF, 10, liked=False)
        forms = [httpx.QueryParams(request.content.decode()) for request in requests]
        self.assertEqual([form["action"] for form in forms], ["1", "0"])
        self.assertTrue(
            all(form["rpid"] == "10" and form["oid"] == "123" for form in forms)
        )

    async def test_comment_business_rejections_propagate_without_retry(self):
        for code in (-509, 12002, 12003, 12015, 12016, 12035, 12051):
            with self.subTest(code=code):
                requests = []

                def handle(request, captured=requests, response_code=code):
                    captured.append(request)
                    return httpx.Response(200, json={"code": response_code})

                async with BilibiliAPI(
                    _COOKIES, transport=httpx.MockTransport(handle)
                ) as api:
                    with (
                        patch("myfish.adapters.bilibili.api.logger.info") as info,
                        self.assertRaises(BilibiliAPIError) as raised,
                    ):
                        await api.send_comment(_REF, "test")
                    self.assertEqual(raised.exception.code, code)
                    info.assert_not_called()
                self.assertEqual(len(requests), 1)

    async def test_success_envelope_with_captcha_is_not_a_send_receipt(self):
        async with BilibiliAPI(
            _COOKIES,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"code": 0, "data": {"need_captcha": True}}
                )
            ),
        ) as api:
            with self.assertRaises(BilibiliAPIError) as raised:
                await api.send_comment(_REF, "test")
        self.assertEqual(raised.exception.code, 12015)

    async def test_network_failure_does_not_retry_comment_submission(self):
        requests = []

        def handle(request):
            requests.append(request)
            raise httpx.ReadTimeout("uncertain result")

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            with self.assertRaises(httpx.ReadTimeout):
                await api.send_comment(_REF, "test")
        self.assertEqual(len(requests), 1)

    async def test_replies_request_uses_root_and_page_parameters(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "page": {"num": 2, "size": 20, "count": 30},
                        "root": comment(10),
                        "replies": [comment(20, root=10)],
                    },
                },
            )

        async with BilibiliAPI(transport=httpx.MockTransport(handle)) as api:
            result = await api.get_comment_replies(_REF, 10, page=2)
        self.assertEqual(result.root.rpid, 10)
        self.assertEqual(result.replies[0].root, 10)
        self.assertEqual(requests[0].url.params["root"], "10")
        self.assertEqual(requests[0].url.params["pn"], "2")

    async def test_invalid_inputs_do_not_issue_requests(self):
        def handle(_):
            self.fail("Invalid input issued an HTTP request")

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            for text, root, parent in (
                ("", 0, 0),
                (" " * 2, 0, 0),
                ("x" * 1001, 0, 0),
                ("test", -1, 0),
                ("test", 0, 10),
            ):
                with (
                    self.subTest(text_size=len(text), root=root),
                    self.assertRaises(ValueError),
                ):
                    await api.send_comment(_REF, text, root=root, parent=parent)
            with self.assertRaises(ValueError):
                await api.like_comment(_REF, 0)
            with self.assertRaises(ValueError):
                await api.get_comment_replies(_REF, 10, size=21)
        with self.assertRaises(ValidationError):
            CommentRef(oid=0, kind=1)
