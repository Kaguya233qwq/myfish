import json
import unittest
from pathlib import Path

import httpx

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError, XhsVerificationRequired
from myfish.adapters.xhs.creator import CreatorAPI
from myfish.adapters.xhs.ds import CreatorDS
from myfish.adapters.xhs.model import SmsCodeSent, SmsReceiver

_FIXTURE = json.loads((Path(__file__).parent / "fixtures/xhs_creator.json").read_text())
_CHALLENGE = XhsVerificationRequired(
    status=461, verify_uuid="test-challenge", verify_type="201"
)
_PAYLOAD = {
    "verifyUuid": "test-challenge",
    "verifyType": "201",
    "verifyBiz": 461,
    "sourceSite": "",
}


class CreatorSmsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests: list[httpx.Request] = []
        self.response: dict[str, object] = {}

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, json=self.response)

        self.account = XhsAPI({"a1": "a" * 52, "web_session": "test-account"})
        self.creator = CreatorAPI(self.account, transport=httpx.MockTransport(handle))
        self.creator.ds = CreatorDS.from_script(
            f"function getdss() {{ return '{_FIXTURE['anchor']}'; }}; "
            f"var program='{_FIXTURE['program']}';"
        )
        self.creator._ds_loaded_at = self.creator.session.clock()

    async def asyncTearDown(self):
        await self.creator.close()
        await self.account.close()

    async def test_init_only_initializes_without_sending(self):
        self.response = {
            "success": True,
            "code": 0,
            "data": {"receiver": "***1234"},
        }
        receiver = await self.creator.init_sms_verification(_CHALLENGE)
        self.assertEqual(receiver.receiver, "***1234")
        self.assertNotIn("1234", repr(receiver))
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.url.path, "/api/redcaptcha/v2/vc/init")
        self.assertEqual(json.loads(request.content), _PAYLOAD)

    async def test_send_uses_creator_session_and_returns_request_id(self):
        self.response = {
            "success": True,
            "code": 0,
            "data": {"rid": "test-request", "expireSec": 60},
        }
        sent = await self.creator.send_sms_code(_CHALLENGE)
        self.assertEqual(sent.rid, "test-request")
        self.assertEqual(sent.expire_seconds, 60)
        self.assertNotIn("test-request", repr(sent))
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            str(request.url), "https://edith.xiaohongshu.com/api/redcaptcha/v2/vc/send"
        )
        self.assertEqual(json.loads(request.content), _PAYLOAD)
        self.assertIn("web_session=test-account", request.headers["cookie"])
        self.assertTrue(request.headers["x-s"].startswith("XYS_"))
        self.assertNotIn("x-rap-param", request.headers)

    async def test_check_preserves_code_and_count_without_retrying_publish(self):
        for count in (0, 1):
            with self.subTest(count=count):
                self.requests.clear()
                self.response = {"success": True, "code": 0}
                await self.creator.check_sms_code(
                    _CHALLENGE, rid="test-request", code="012345", check_count=count
                )
                self.assertEqual(len(self.requests), 1)
                request = self.requests[0]
                self.assertEqual(request.url.path, "/api/redcaptcha/v2/vc/check")
                self.assertEqual(
                    json.loads(request.content),
                    {
                        **_PAYLOAD,
                        "rid": "test-request",
                        "code": "012345",
                        "checkCount": count,
                    },
                )

    async def test_business_failures_are_errors_even_when_success_is_true(self):
        for code, message in (
            (40002, "验证码已失效"),
            (40003, "验证码输入错误"),
            (40012, "已超过今天发送短信条数限制"),
            (49999, "未知错误"),
            (None, "缺少业务状态"),
        ):
            with self.subTest(code=code):
                self.requests.clear()
                self.response = {"success": True, "msg": message}
                if code is not None:
                    self.response["code"] = code
                if code == 40012:
                    self.response["data"] = {"expireSec": 0}
                with self.assertRaises(XhsAPIError) as raised:
                    if code == 40012:
                        await self.creator.send_sms_code(_CHALLENGE)
                    else:
                        await self.creator.check_sms_code(
                            _CHALLENGE, rid="test-request", code="012345"
                        )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(len(self.requests), 1)
                self.assertNotIn("012345", str(raised.exception))

    async def test_invalid_input_does_not_issue_requests(self):
        for code in ("", "12345", "1234567", "１２３４５６", "abcdef"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                await self.creator.check_sms_code(_CHALLENGE, rid="rid", code=code)
        for count in (-1, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                await self.creator.check_sms_code(
                    _CHALLENGE, rid="rid", code="012345", check_count=count
                )
        with self.assertRaises(ValueError):
            await self.creator.check_sms_code(_CHALLENGE, rid="", code="012345")
        for challenge in (
            XhsVerificationRequired(status=461, verify_uuid="id", verify_type="124"),
            XhsVerificationRequired(status=200, verify_uuid="id", verify_type="201"),
            XhsVerificationRequired(status=461, verify_uuid="", verify_type="201"),
        ):
            for method in (
                self.creator.init_sms_verification,
                self.creator.send_sms_code,
            ):
                with self.assertRaises(ValueError):
                    await method(challenge)
        self.assertEqual(self.requests, [])

    def test_sms_models_hide_sensitive_fields(self):
        self.assertNotIn("private", repr(SmsReceiver(receiver="private")))
        self.assertNotIn("private", repr(SmsCodeSent(rid="private", expireSec=60)))
