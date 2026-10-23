import hashlib
import json
import struct
import unittest
from copy import deepcopy
from pathlib import Path

import httpx

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError, XhsVerificationRequired
from myfish.adapters.xhs.creator import CreatorAPI
from myfish.adapters.xhs.ds import CreatorDS
from myfish.adapters.xhs.sign import SigningContext, creator_headers
from myfish.adapters.xhs.utils import compact_json, decode_base64

_FIXTURE = json.loads((Path(__file__).parent / "fixtures/xhs_creator.json").read_text())


def script(program: str = _FIXTURE["program"]) -> str:
    return f"function getdss() {{ return '{_FIXTURE['anchor']}'; }}; var program='{program}';"


class CreatorSigningTests(unittest.TestCase):
    def test_ds_matches_server_script_vectors(self):
        for sample in (_FIXTURE, *_FIXTURE["variants"]):
            ds = CreatorDS.from_script(script(sample["program"]))
            for case in sample["vectors"]:
                with self.subTest(initial=ds.initial, data=case["input"]):
                    self.assertEqual(
                        ds.digest(bytes.fromhex(case["input"])).hex(), case["output"]
                    )

    def test_ds_rejects_unknown_or_truncated_programs(self):
        program = bytearray.fromhex(_FIXTURE["program"])
        program[100] ^= 1
        cases = [
            "",
            "function getdss(){return '1';}",
            script(program.hex()),
            script(_FIXTURE["program"][:-20]),
            script() + "var extra='" + _FIXTURE["program"] + "';",
        ]
        for value in cases:
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                CreatorDS.from_script(value)
        ds = CreatorDS.from_script(script())
        for size in (0, 23, 25, 64):
            with self.subTest(size=size), self.assertRaises(ValueError):
                ds.digest(bytes(size))

    def test_creator_headers_match_reference_for_both_tiers(self):
        ds = CreatorDS.from_script(script())
        for case in _FIXTURE["signatures"]:
            args = case["args"]
            payload = args["data"]
            body = compact_json(payload) if isinstance(payload, dict) else payload
            context = SigningContext(
                args["now"],
                args["loadts"],
                args["seq"],
                args["envConst"],
                args["version"],
                bytes(args["envFpTail"]),
            )
            digest = ds.digest(
                struct.pack("<Q", args["now"])
                + hashlib.md5(args["api"].encode()).digest()
            )
            headers = creator_headers(
                args["api"],
                body,
                {"x19": "Linux x86_64"},
                context,
                a1=args["a1"],
                b1=args["b1"],
                ds_anchor=args["dslPair"],
                ds_digest=digest,
                tier=args["tier"],
                has_body=isinstance(payload, dict),
            )
            self.assertEqual(headers["x-s"], case["expected"]["xs"])
            self.assertEqual(headers["x-s-common"], case["expected"]["xs_common"])
            self.assertEqual(headers["x-t"], case["expected"]["xt"])

    def test_ready_signing_requires_creator_ds(self):
        context = SigningContext(1000, 900, 1, 1337, 0)
        with self.assertRaisesRegex(ValueError, "DS digest"):
            creator_headers(
                "/api/test",
                "",
                {"x19": "Win32"},
                context,
                a1="a" * 52,
                b1="test",
                ds_anchor="900;1",
                ds_digest=None,
            )


class CreatorAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_creator_isolates_cookies_fingerprint_and_signing_sequence(self):
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/publish/publish":
                return httpx.Response(
                    200,
                    headers={
                        "set-cookie": "customer-sso-sid=creator-only; HttpOnly; Path=/"
                    },
                )
            if request.url.path == "/api/sec/v1/ds":
                return httpx.Response(200, text=script())
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "code": 0,
                    "data": {
                        "userId": "account",
                        "userName": "nickname",
                        "phone": "private-phone",
                    },
                },
            )

        async with XhsAPI(
            {"a1": "a" * 52, "web_session": "private-session", "webBuild": "6.47.2"},
            transport=httpx.MockTransport(handle),
        ) as account:
            original_cookies = account.cookie_values()
            original_fields = deepcopy(account.fingerprint.fields)
            account.session_secret = b"s" * 32
            async with CreatorAPI(
                account, transport=httpx.MockTransport(handle)
            ) as creator:
                await creator.initialize()
                info = await creator.get_self_info()
                self.assertEqual(info.user_id, "account")
                self.assertEqual(info.nickname, "nickname")
                self.assertFalse(hasattr(info, "phone"))
                self.assertEqual(creator.session.sequence, 1)
                self.assertEqual(
                    creator._client.cookies.get("customer-sso-sid"), "creator-only"
                )
            self.assertEqual(account.cookie_values(), original_cookies)
            self.assertEqual(account.fingerprint.fields, original_fields)
            self.assertEqual(account.session.sequence, 0)
            self.assertEqual(account.session_secret, b"s" * 32)
        envelope = json.loads(decode_base64(requests[-1].headers["x-s"][4:]))
        common = json.loads(decode_base64(requests[-1].headers["x-s-common"]))
        self.assertEqual(envelope["x1"], "ugc")
        self.assertTrue(envelope["x3"].startswith("mns0101_"))
        self.assertEqual(set(envelope), {"x0", "x1", "x2", "x3", "x4"})
        self.assertEqual(common["x4"], "1.26.0")
        self.assertIn("customer-sso-sid=creator-only", requests[-1].headers["cookie"])
        self.assertNotIn("customer-sso-sid", requests[1].headers.get("cookie", ""))

    async def test_ds_cache_refreshes_before_expiry_request_is_signed(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("/ds"):
                return httpx.Response(200, text=script())
            return httpx.Response(
                200, json={"success": True, "code": 0, "data": {"userId": "account"}}
            )

        async with (
            XhsAPI({"a1": "a" * 52, "web_session": "session"}) as account,
            CreatorAPI(account, transport=httpx.MockTransport(handle)) as creator,
        ):
            clock = [1000]
            creator.session.clock = lambda: clock[0]
            await creator.get_self_info()
            clock[0] += 299999
            await creator.get_self_info()
            clock[0] += 1
            await creator.get_self_info()
        self.assertEqual([r.url.path for r in requests].count("/api/sec/v1/ds"), 2)

    async def test_signatures_bind_posted_utf8_bytes_and_refresh_per_request(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"success": True, "code": 0, "data": {}})

        async with (
            XhsAPI({"a1": "a" * 52, "web_session": "session"}) as account,
            CreatorAPI(account, transport=httpx.MockTransport(handle)) as creator,
        ):
            creator.ds = CreatorDS.from_script(script())
            creator._ds_loaded_at = creator.session.clock()
            payload = {"title": "中文😀"}
            for _ in range(2):
                await creator._request(
                    "POST", "/api/test?cursor=abc%3D", payload=payload
                )
        self.assertEqual(requests[0].content, compact_json(payload).encode())
        self.assertEqual(requests[0].url.raw_path, b"/api/test?cursor=abc%3D")
        self.assertNotEqual(requests[0].headers["x-s"], requests[1].headers["x-s"])

    async def test_rejections_are_not_retried_and_do_not_expose_response(self):
        for status, body in [
            (406, {}),
            (461, {}),
            (461, {"success": True, "code": 0, "data": {}}),
            (471, {}),
            (200, {"success": False, "code": -1}),
            (200, {"success": True, "code": 0, "data": {"userId": None}}),
        ]:
            requests = []

            def handle(
                request, captured=requests, status_code=status, response_body=body
            ):
                captured.append(request)
                return httpx.Response(
                    status_code,
                    json=response_body,
                    headers={"verifyuuid": "private-uuid", "verifytype": "124"},
                )

            async with (
                XhsAPI({"a1": "a" * 52, "web_session": "private-session"}) as account,
                CreatorAPI(account, transport=httpx.MockTransport(handle)) as creator,
            ):
                creator.ds = CreatorDS.from_script(script())
                creator._ds_loaded_at = creator.session.clock()
                with self.assertRaises(XhsAPIError) as error:
                    await creator.get_self_info()
                self.assertNotIn("private", str(error.exception))
                if status in (461, 471):
                    self.assertIsInstance(error.exception, XhsVerificationRequired)
                    self.assertEqual(error.exception.verify_type, "124")
            self.assertEqual(len(requests), 1)
