import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from myfish.adapters.xhs.api import XhsAPI, XhsVerificationRequired
from myfish.adapters.xhs.auth import (
    decrypt_ssk,
    finish_login,
    read_with_verification,
    show_qrcode,
    verify_risk,
)
from myfish.adapters.xhs.device import DeviceProfile
from myfish.adapters.xhs.utils import decode_base64


class XhsVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_formal_login_negotiates_and_persists_account_key(self):
        server = X25519PrivateKey.generate()
        secret = bytes(range(32))
        requests = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/qrcode/status"):
                client = X25519PublicKey.from_public_bytes(
                    base64.b64decode(request.url.params["client_public_key_base64"])
                )
                nonce = bytes(12)
                encrypted = nonce + AESGCM(server.exchange(client)).encrypt(
                    nonce, secret, None
                )
                data = {
                    "code_status": 2,
                    "login_info": {
                        "session": "account-session",
                        "ssk": base64.b64encode(encrypted).decode(),
                    },
                }
            else:
                data = {"guest": False, "user_id": "self"}
            return httpx.Response(200, json={"success": True, "code": 0, "data": data})

        api = XhsAPI.create(profile=DeviceProfile())
        await api._client.aclose()
        api._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle), cookies=api.cookie_values()
        )
        api.set_cookie("web_session", "visitor")
        api.session_secret = b"v" * 32
        api.session.dsl = "123"
        api.session.tiga_timestamp = 1
        api.use_current_signing = True
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "myfish.adapters.xhs.auth._SSK_SERVER_KEY",
                server.public_key().public_bytes_raw(),
            ),
            patch("builtins.print"),
        ):
            output = Path(directory) / "auth.json"
            async with api:
                await finish_login(api, output, "qr", "code")
                self.assertEqual(api.session_secret, secret)
                self.assertEqual(api.cookie_values()["web_session"], "account-session")
            async with XhsAPI.from_state(output.with_suffix(".state.json")) as restored:
                self.assertEqual(restored.session_secret, secret)
            self.assertEqual(len(requests), 2)

    def test_ssk_rejects_wrong_length_and_authentication_tag(self):
        private = X25519PrivateKey.generate()
        for size in (0, 28, 59, 61):
            with self.subTest(size=size), self.assertRaises(ValueError):
                decrypt_ssk(private, base64.b64encode(bytes(size)).decode())
        with self.assertRaises(InvalidTag):
            decrypt_ssk(private, base64.b64encode(bytes(60)).decode())

    async def test_missing_formal_ssk_does_not_save_visitor_key(self):
        async with XhsAPI(
            {"a1": "a" * 52, "web_session": "visitor", "webBuild": "6.47.2"},
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "success": True,
                        "code": 0,
                        "data": {
                            "code_status": 2,
                            "login_info": {"session": "account"},
                        },
                    },
                )
            ),
        ) as api:
            api.set_profile_fields({**api.fingerprint.fields, "x83": "", "x84": ""})
            api.use_current_signing = True
            api.session_secret = b"v" * 32
            api.session.dsl = "123"
            api.session.tiga_timestamp = 1
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "auth.json"
                with self.assertRaisesRegex(RuntimeError, "SSK"):
                    await finish_login(api, output, "qr", "code")
                self.assertFalse(output.exists())
                self.assertFalse(output.with_suffix(".state.json").exists())

    async def test_challenge_preserves_metadata_without_leaking_tokens_or_retrying(
        self,
    ):
        for status, verify_type, method in (
            (461, "124", "qrcode"),
            (471, "124", "qrcode"),
            (461, "201", "sms"),
            (471, "201", "sms"),
        ):
            requests = []

            def handle(
                request,
                captured=requests,
                response_status=status,
                challenge_type=verify_type,
            ):
                captured.append(request)
                return httpx.Response(
                    response_status,
                    headers={
                        "verifyuuid": "private-challenge",
                        "verifytype": challenge_type,
                    },
                    text="private response",
                )

            async with XhsAPI(
                {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
            ) as api:
                with self.assertRaises(XhsVerificationRequired) as raised:
                    await api.post("/api/test", {})
            self.assertEqual(len(requests), 1)
            self.assertEqual(raised.exception.status, status)
            self.assertEqual(raised.exception.verify_uuid, "private-challenge")
            self.assertEqual(raised.exception.verify_type, verify_type)
            self.assertEqual(raised.exception.verification_method, method)
            self.assertNotIn("private", str(raised.exception))

    async def test_verification_bootstraps_isolated_session_and_signs_security_requests(
        self,
    ):
        for terminal in (3, 4, 5, 6):
            steps = []
            statuses = iter([1, 2, terminal])
            requests = []

            def handle(request, captured=requests, recorded=steps, states=statuses):
                captured.append(request)
                recorded.append(request.url.path)
                data = (
                    {"rid": "rid&test"}
                    if request.url.path.endswith("/init")
                    else {"status": next(states)}
                )
                return httpx.Response(
                    200, json={"success": True, "code": 0, "data": data}
                )

            original = XhsAPI({"a1": "a" * 52, "web_session": "account"})
            original.set_profile_fields(
                {**original.fingerprint.fields, "x83": "", "x84": ""}
            )
            original.use_current_signing = True
            original.session_secret = b"a" * 32
            verifier = XhsAPI(
                original.cookie_values(), transport=httpx.MockTransport(handle)
            )

            async def initialize(client, recorded=steps):
                recorded.append("initialize")
                client.session.dsl = "123"
                client.session.tiga_timestamp = 1

            async def activate(client, recorded=steps):
                recorded.append("activate")
                client.set_cookie("web_session", "visitor")
                client.session_secret = b"v" * 32

            async def profile(client, recorded=steps):
                recorded.append("profile")

            with (
                tempfile.TemporaryDirectory() as directory,
                patch("myfish.adapters.xhs.auth.XhsAPI", return_value=verifier),
                patch(
                    "myfish.adapters.xhs.auth.initialize_device", side_effect=initialize
                ),
                patch("myfish.adapters.xhs.auth.activate", side_effect=activate),
                patch("myfish.adapters.xhs.auth.report_profile", side_effect=profile),
                patch("myfish.adapters.xhs.auth.show_qrcode") as show,
                patch("myfish.adapters.xhs.auth.asyncio.sleep", new_callable=AsyncMock),
                patch("builtins.print"),
            ):
                challenge = XhsVerificationRequired(
                    status=471, verify_uuid="uuid&test", verify_type="124"
                )
                async with original:
                    if terminal == 4:
                        await verify_risk(
                            original, challenge, Path(directory) / "auth.json"
                        )
                    else:
                        with self.assertRaises(RuntimeError):
                            await verify_risk(
                                original, challenge, Path(directory) / "auth.json"
                            )
                    self.assertEqual(original.cookie_values()["web_session"], "account")
                    self.assertEqual(original.session_secret, b"a" * 32)
                self.assertEqual(steps[:3], ["initialize", "activate", "profile"])
                self.assertIn("rid=rid%26test", show.call_args.args[0])
                for request in requests:
                    self.assertEqual(request.url.host, "as.xiaohongshu.com")
                    self.assertIn("xsecappid=fe-login", request.headers["cookie"])
                    body = json.loads(request.content)
                    self.assertEqual(body["verifyBiz"], "471")
                    envelope = json.loads(decode_base64(request.headers["x-s"][4:]))
                    self.assertTrue(envelope["x3"].startswith("mns0201_"))

    async def test_read_retries_once_only_after_successful_verification(self):
        challenge = XhsVerificationRequired(
            status=461, verify_uuid="uuid", verify_type="124"
        )
        async with XhsAPI({"a1": "a" * 52}) as api:
            for failures in ([challenge, "ok"], [challenge, challenge]):
                read = AsyncMock(side_effect=failures)
                with patch(
                    "myfish.adapters.xhs.auth.verify_risk", new_callable=AsyncMock
                ) as verify:
                    if failures[-1] == "ok":
                        self.assertEqual(
                            await read_with_verification(
                                api, Path("/tmp/auth.json"), read
                            ),
                            "ok",
                        )
                    else:
                        with self.assertRaises(XhsVerificationRequired):
                            await read_with_verification(
                                api, Path("/tmp/auth.json"), read
                            )
                    self.assertEqual(read.await_count, 2)
                    verify.assert_awaited_once()
            read = AsyncMock(side_effect=challenge)
            with patch(
                "myfish.adapters.xhs.auth.verify_risk", side_effect=TimeoutError
            ):
                with self.assertRaises(TimeoutError):
                    await read_with_verification(api, Path("/tmp/auth.json"), read)
                self.assertEqual(read.await_count, 1)

    async def test_unsupported_challenge_does_not_initialize(self):
        async with XhsAPI({"a1": "a" * 52}) as api:
            with patch("myfish.adapters.xhs.auth.initialize_device") as initialize:
                with self.assertRaisesRegex(RuntimeError, "120"):
                    await verify_risk(
                        api,
                        XhsVerificationRequired(
                            status=471, verify_uuid="uuid", verify_type="120"
                        ),
                        Path("/tmp/auth.json"),
                    )
                initialize.assert_not_called()

    async def test_sms_challenge_never_initializes_qr_or_sends_sms(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(500)

        async with XhsAPI(
            {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
        ) as api:
            with patch("myfish.adapters.xhs.auth.initialize_device") as initialize:
                with self.assertRaisesRegex(RuntimeError, "201.*短信验证码"):
                    await verify_risk(
                        api,
                        XhsVerificationRequired(
                            status=461,
                            verify_uuid="private-challenge",
                            verify_type="201",
                        ),
                        Path("/tmp/auth.json"),
                    )
                initialize.assert_not_called()
        self.assertEqual(requests, [])

    def test_qrcode_image_has_private_permissions(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("builtins.print"),
            patch("qrcode.QRCode.print_ascii"),
        ):
            path = Path(directory) / "verify.png"
            show_qrcode("https://www.xiaohongshu.com", path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
