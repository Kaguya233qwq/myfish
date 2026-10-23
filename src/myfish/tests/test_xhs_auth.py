import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from myfish.adapters.xhs.api import XhsAPI
from myfish.adapters.xhs.auth import login


class XhsAuthTests(unittest.IsolatedAsyncioTestCase):
    async def run_login(
        self, *, guest: bool, session: str = "authenticated"
    ) -> tuple[bool, int]:
        payloads = iter(
            [
                {},
                {"qr_id": "qr", "code": "code", "url": "https://www.xiaohongshu.com"},
                {"codeStatus": 0},
                {},
                {"codeStatus": 2},
                {"code_status": 2, "login_info": {"session": session}},
                {"guest": guest, "user_id": "self"},
            ]
        )
        requests = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"success": True, "code": 0, "data": next(payloads)},
                headers={"set-cookie": "gid=profile; Domain=.xiaohongshu.com; Path=/"},
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("myfish.adapters.xhs.auth.qrcode.QRCode"),
            patch("builtins.print"),
        ):
            output = Path(directory) / "cookies.json"
            async with (
                XhsAPI(
                    {"a1": "a" * 52, "web_session": "visitor"},
                    transport=httpx.MockTransport(handle),
                ) as api,
            ):
                if guest or session in ("", "visitor"):
                    with self.assertRaises(RuntimeError):
                        await login(api, output)
                    self.assertFalse(output.exists())
                else:
                    await login(api, output)
                    saved = json.loads(output.read_text())
                    self.assertEqual(saved["web_session"], session)
                    self.assertIn(
                        "web_session=authenticated", requests[-1].headers["cookie"]
                    )
                    self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                body = json.loads(requests[0].content)
                self.assertIn("client_public_key_base64", body)
                self.assertEqual(requests[5].headers["x-login-mode"], "")
                self.assertTrue(requests[0].headers["x-s"].startswith("XYS_"))
            return output.exists(), len(requests)

    async def test_confirmed_session_is_verified_before_private_atomic_save(self):
        exists, count = await self.run_login(guest=False)
        self.assertTrue(exists)
        self.assertEqual(count, 7)

    async def test_guest_or_unchanged_session_is_never_saved(self):
        await self.run_login(guest=True)
        await self.run_login(guest=False, session="visitor")
        await self.run_login(guest=False, session="")

    async def test_expired_qrcode_does_not_finalize_or_save_credentials(self):
        payloads = iter(
            [
                {"session": "visitor"},
                {"qr_id": "qr", "code": "code", "url": "https://www.xiaohongshu.com"},
                {"codeStatus": 0},
                {},
                {"codeStatus": 3},
            ]
        )

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"success": True, "code": 0, "data": next(payloads)},
                headers={"set-cookie": "gid=profile; Domain=.xiaohongshu.com; Path=/"},
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("myfish.adapters.xhs.auth.qrcode.QRCode"),
            patch("builtins.print"),
        ):
            output = Path(directory) / "cookies.json"
            async with XhsAPI(
                {"a1": "a" * 52}, transport=httpx.MockTransport(handle)
            ) as api:
                with self.assertRaisesRegex(RuntimeError, "二维码已过期"):
                    await login(api, output)
            self.assertFalse(output.exists())
