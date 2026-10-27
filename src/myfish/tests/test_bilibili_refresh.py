import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from myfish.adapters.bilibili.api import (
    BilibiliAPI,
    BilibiliAPIError,
    BilibiliProtocolError,
)
from myfish.adapters.bilibili.auth import load_state, refresh_credentials, save_state
from myfish.adapters.bilibili.model import Account
from myfish.adapters.bilibili.sign import correspond_path

_COOKIES = {"SESSDATA": "old-session", "bili_jct": "old-csrf", "DedeUserID": "1"}


class BilibiliRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_persists_new_pair_before_confirming_old_token(self):
        requests = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"

            def handle(request):
                requests.append(request)
                headers = []
                if request.url.path.endswith("/cookie/info"):
                    data = {"refresh": True, "timestamp": 1000}
                elif "/correspond/1/" in request.url.path:
                    return httpx.Response(
                        200,
                        text='<div id="1-name">'
                        + "a" * 32
                        + "</div><script>throw new Error()</script>",
                    )
                elif request.url.path.endswith("/cookie/refresh"):
                    self.assertEqual(
                        request.headers["origin"], "https://www.bilibili.com"
                    )
                    form = httpx.QueryParams(request.content.decode())
                    self.assertEqual(form["refresh_token"], "old-token")
                    self.assertEqual(form["csrf"], "old-csrf")
                    data = {"status": 0, "refresh_token": "new-token"}
                    headers = [
                        (
                            "set-cookie",
                            "SESSDATA=new-session; Domain=.bilibili.com; Path=/",
                        ),
                        (
                            "set-cookie",
                            "bili_jct=new-csrf; Domain=.bilibili.com; Path=/",
                        ),
                    ]
                elif request.url.path.endswith("/nav"):
                    self.assertIn("SESSDATA=new-session", request.headers["cookie"])
                    data = {"mid": 1, "uname": "test", "isLogin": True}
                else:
                    state = load_state(path)
                    self.assertEqual(
                        state.refresh_token.get_secret_value(), "new-token"
                    )
                    self.assertEqual(
                        state.confirmation_token.get_secret_value(), "old-token"
                    )
                    form = httpx.QueryParams(request.content.decode())
                    self.assertEqual(form["refresh_token"], "old-token")
                    self.assertEqual(form["csrf"], "new-csrf")
                    data = None
                return httpx.Response(
                    200, headers=headers, json={"code": 0, "data": data}
                )

            async with BilibiliAPI(
                _COOKIES, transport=httpx.MockTransport(handle)
            ) as api:
                api.account = Account(mid=1, uname="test", isLogin=True)
                api.refresh_token = SecretStr("old-token")
                self.assertTrue(await refresh_credentials(api, path))
                self.assertEqual(api.cookie_values()["SESSDATA"], "new-session")
                self.assertIsNone(load_state(path).confirmation_token)
                self.assertNotIn("new-token", repr(load_state(path)))
            self.assertEqual(len(requests), 5)

    async def test_pending_confirmation_is_resumed_without_another_refresh(self):
        requests = []

        def handle(request):
            requests.append(request)
            self.assertTrue(request.url.path.endswith("/confirm/refresh"))
            return httpx.Response(200, json={"code": 0, "data": None})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            async with BilibiliAPI(
                _COOKIES, transport=httpx.MockTransport(handle)
            ) as api:
                api.refresh_token = SecretStr("new-token")
                api.confirmation_token = SecretStr("old-token")
                save_state(api, path)
                self.assertTrue(await refresh_credentials(api, path))
                self.assertIsNone(load_state(path).confirmation_token)
            self.assertEqual(len(requests), 1)

    async def test_refresh_to_another_account_does_not_replace_saved_credentials(self):
        requests = []

        def handle(request):
            requests.append(request)
            headers = []
            if request.url.path.endswith("/cookie/info"):
                data = {"refresh": True, "timestamp": 1000}
            elif "/correspond/1/" in request.url.path:
                return httpx.Response(
                    200, text='<div id="1-name">' + "a" * 32 + "</div>"
                )
            elif request.url.path.endswith("/cookie/refresh"):
                data = {"status": 0, "refresh_token": "other-token"}
                headers = [
                    (
                        "set-cookie",
                        "SESSDATA=other-session; Domain=.bilibili.com; Path=/",
                    )
                ]
            else:
                data = {"mid": 99, "uname": "other", "isLogin": True}
            return httpx.Response(200, headers=headers, json={"code": 0, "data": data})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            async with BilibiliAPI(
                _COOKIES, transport=httpx.MockTransport(handle)
            ) as api:
                api.account = Account(mid=1, uname="test", isLogin=True)
                api.refresh_token = SecretStr("old-token")
                save_state(api, path)
                before = path.read_bytes()
                with self.assertRaises(BilibiliProtocolError):
                    await refresh_credentials(api, path)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(api.cookie_values(), _COOKIES)
                self.assertEqual(api.account.mid, 1)
            self.assertEqual(len(requests), 4)

    async def test_confirmation_failure_keeps_recoverable_state(self):
        def handle(_):
            return httpx.Response(503)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            async with BilibiliAPI(
                _COOKIES, transport=httpx.MockTransport(handle)
            ) as api:
                api.refresh_token = SecretStr("new-token")
                api.confirmation_token = SecretStr("old-token")
                save_state(api, path)
                with self.assertRaises(BilibiliAPIError):
                    await refresh_credentials(api, path)
                self.assertEqual(
                    load_state(path).confirmation_token.get_secret_value(), "old-token"
                )

    async def test_no_refresh_token_skips_all_refresh_requests(self):
        def handle(_):
            self.fail("No refresh token should not issue requests")

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            self.assertFalse(await refresh_credentials(api, Path("unused")))

    async def test_cookie_info_without_refresh_does_not_rotate_credentials(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(
                200, json={"code": 0, "data": {"refresh": False, "timestamp": 1000}}
            )

        async with BilibiliAPI(_COOKIES, transport=httpx.MockTransport(handle)) as api:
            api.refresh_token = SecretStr("old-token")
            self.assertFalse(await refresh_credentials(api, Path("unused")))
            self.assertEqual(api.cookie_values(), _COOKIES)
        self.assertEqual(len(requests), 1)

    async def test_invalid_refresh_page_does_not_write_or_confirm(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("/info"):
                return httpx.Response(
                    200, json={"code": 0, "data": {"refresh": True, "timestamp": 1000}}
                )
            return httpx.Response(200, text="<div>no token</div>")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            async with BilibiliAPI(
                _COOKIES, transport=httpx.MockTransport(handle)
            ) as api:
                api.refresh_token = SecretStr("old-token")
                save_state(api, path)
                original = path.read_bytes()
                with self.assertRaises(BilibiliProtocolError):
                    await refresh_credentials(api, path)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(api.cookie_values(), _COOKIES)
        self.assertEqual(len(requests), 2)

    async def test_qr_poll_captures_refresh_token_without_confirming_stale_state(self):
        async with BilibiliAPI(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"code": 0, "data": {"code": 0, "refresh_token": "qr-token"}},
                )
            )
        ) as api:
            api.confirmation_token = SecretStr("stale")
            await api.poll_login_qrcode("test")
            self.assertEqual(api.refresh_token.get_secret_value(), "qr-token")
            self.assertIsNone(api.confirmation_token)


class BilibiliRefreshCryptoTests(unittest.TestCase):
    def test_correspond_path_uses_oaep_sha256_and_millisecond_payload(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with patch("myfish.adapters.bilibili.sign._REFRESH_KEY", private.public_key()):
            encrypted = bytes.fromhex(correspond_path(1234567890000))
        self.assertEqual(
            private.decrypt(
                encrypted,
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            ),
            b"refresh_1234567890000",
        )
