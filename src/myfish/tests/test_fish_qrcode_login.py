import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from myfish.adapters.fish import FishAdapter, qrcode_login
from myfish.adapters.fish.api import FishAPI


class QRLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_keeps_server_session_cookies(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/mini_login.htm":
                return httpx.Response(
                    200,
                    text='window.viewData = {"loginFormData":{"test":"value"}};',
                    headers={"set-cookie": "qr_session=session-value; Path=/; Secure"},
                )
            return httpx.Response(200, json={"content": {"data": {"qrCodeStatus": "NEW"}}})

        api = FishAPI({})
        await api.client.aclose()
        api.client = httpx.AsyncClient(
            transport=httpx.MockTransport(respond), headers={"cookie": ""}
        )
        try:
            self.assertEqual(await api._get_login_params(), {"test": "value", "umidTag": "SERVER"})
            await api.qrcode_poll("t", "ck")
            self.assertIn("qr_session=session-value", requests[1].headers["cookie"])
            self.assertEqual(requests[0].url.params["site"], "77")
        finally:
            await api.close()

    async def poll(self, data):
        api = FishAPI({})
        await api.client.aclose()
        api.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"content": {"data": data}})
        ))
        try:
            return await api.qrcode_poll("test-t", "test-ck")
        finally:
            await api.close()

    async def test_redirect_flag_is_not_url(self):
        result = await self.poll({"iframeRedirect": True})
        self.assertEqual(result, {"status": "ERROR", "redirect_url": ""})

    async def test_verification_urls(self):
        for field in ("iframeRedirectUrl", "url", "redirectUrl", "iframeRedirect"):
            with self.subTest(field=field):
                data = {"iframeRedirect": True, field: "https://passport.goofish.com/verify"}
                result = await self.poll(data)
                self.assertEqual(result["redirect_url"], "https://passport.goofish.com/verify")
        for value, expected in (
            ("//passport.goofish.com/verify", "https://passport.goofish.com/verify"),
            ("/verify", "https://passport.goofish.com/verify"),
            (True, ""),
            ("javascript:alert(1)", ""),
        ):
            with self.subTest(value=value):
                result = await self.poll({"iframeRedirect": True, "url": value})
                self.assertEqual(result["redirect_url"], expected)

    async def test_normal_statuses(self):
        for status in ("NEW", "SCANED", "EXPIRED", "CANCELED", "CONFIRMED"):
            with self.subTest(status=status), patch.object(FishAPI, "save_cookies") as save:
                self.assertEqual(await self.poll({"qrCodeStatus": status}), {"status": status})
                self.assertEqual(save.call_count, int(status == "CONFIRMED"))

    async def test_missing_url_message(self):
        client = AsyncMock()
        client.qrcode_gen.return_value = {"content": "test", "t": "t", "ck": "ck"}
        client.qrcode_poll.return_value = {"status": "ERROR", "redirect_url": ""}
        with patch("myfish.adapters.fish.asyncio.sleep", new_callable=AsyncMock), \
                patch("myfish.adapters.fish.qrcode.QRCode"), \
                patch("myfish.adapters.fish.logger") as log:
            self.assertIsNone(await qrcode_login(client))
            self.assertIn("未返回有效", log.warning.call_args.args[0])

    async def test_failed_login_stops_adapter(self):
        api = AsyncMock()
        adapter = FishAdapter(api)
        adapter._connect_ws = AsyncMock()
        with patch("myfish.adapters.fish.ensure_auth", new_callable=AsyncMock, return_value=None):
            await adapter.run()
        api.close.assert_awaited_once()
        adapter._connect_ws.assert_not_awaited()
        self.assertEqual(adapter._bg_tasks, [])


if __name__ == "__main__":
    unittest.main()
