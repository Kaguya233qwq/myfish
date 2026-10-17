import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from myfish.adapters.fish import qrcode_login
from myfish.adapters.fish.verification import parse_verification_page, verify_identity


PAGE_URL = "https://passport.goofish.com/iv/mini/identity_verify.htm?htoken=test"
PAGE = '''new Qrcode({text: "https://market.m.taobao.com/verify?token=test", width:166});
jQuery.ajax({url:'https://passport.goofish.com/iv/photoVerify/check.do?htoken=test'});'''


class IdentityVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def run_flow(self, codes, *, entry=False, login=False):
        requests = []
        remaining = iter(codes)

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("mini_login_check.htm"):
                return httpx.Response(302, headers={"location": "/iv/mini/normal_validate.htm", "set-cookie": "entry=one; Path=/"})
            if request.url.path.endswith("normal_validate.htm"):
                return httpx.Response(200, text='window.location.href = "/iv/mini/verify_modes.htm?htoken=test&firstIn=true&_umidfg="+window._iv_umidfg;', headers={"set-cookie": "normal=two; Path=/"})
            if request.url.path.endswith("verify_modes.htm"):
                return httpx.Response(302, headers={"location": PAGE_URL, "set-cookie": "modes=three; Path=/"})
            if request.url.path.endswith("identity_verify.htm"):
                return httpx.Response(200, text=PAGE, headers={"set-cookie": "iv=session; Path=/"})
            if request.url.path.endswith("check.do"):
                code = next(remaining)
                return httpx.Response(
                    200,
                    json={"content": {"code": code, "url": "/done"}},
                    headers={"set-cookie": f"verify_progress={code}; Path=/"},
                )
            if request.url.path == "/done":
                return httpx.Response(302, headers={"location": "/finished", "set-cookie": "unb=test; Path=/"})
            return httpx.Response(200)

        api = Mock()
        api.cookies_dict = {}
        def collect(response):
            api.cookies_dict.update(dict(response.cookies))
        api.save_cookies.side_effect = collect
        api.client = httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True)
        try:
            with patch("myfish.adapters.fish.verification.asyncio.sleep", new_callable=AsyncMock), \
                    patch("myfish.adapters.fish.verification.qrcode.QRCode") as qr:
                entry_url = "https://passport.goofish.com/iv/remote/pc/mini_login_check.htm?havana_iv_token=test"
                if login:
                    api.get_mh5tk = AsyncMock()
                    api.qrcode_gen = AsyncMock(return_value={"content": "login", "t": "t", "ck": "ck"})
                    api.qrcode_poll = AsyncMock(return_value={"status": "ERROR", "redirect_url": entry_url})
                    api.get_token = AsyncMock(return_value={"data": {"accessToken": "test"}})
                    with patch("myfish.adapters.fish.qrcode.QRCode", qr):
                        result = await qrcode_login(api)
                else:
                    result = await verify_identity(api, entry_url if entry else PAGE_URL)
            return result, api, requests, qr
        finally:
            await api.client.aclose()

    async def test_success_session_and_callback(self):
        success, api, requests, qr = await self.run_flow([0, "1", 2, "3"])
        self.assertTrue(success)
        qr.return_value.add_data.assert_called_once_with("https://market.m.taobao.com/verify?token=test")
        self.assertIn("iv=session", requests[1].headers["cookie"])
        self.assertEqual(requests[1].headers["referer"], PAGE_URL)
        self.assertEqual(api.save_cookies.call_count, 7)
        self.assertEqual(api.save_cookies.call_args_list[-2].args[0].status_code, 302)
        self.assertEqual(api.cookies_dict["verify_progress"], "3")
        self.assertEqual(api.cookies_dict["unb"], "test")
        self.assertIn("verify_progress=0", requests[2].headers["cookie"])

    async def test_rejected(self):
        success, api, requests, _ = await self.run_flow(["-1"])
        self.assertFalse(success)
        self.assertEqual(api.cookies_dict["verify_progress"], "-1")
        self.assertEqual(len(requests), 2)

    async def test_remote_entry_reaches_terminal_qr_and_completes(self):
        success, api, requests, qr = await self.run_flow(["0", "3"], entry=True)
        self.assertTrue(success)
        qr.return_value.print_ascii.assert_called_once_with(invert=True)
        self.assertEqual(api.cookies_dict["entry"], "one")
        self.assertEqual(api.cookies_dict["normal"], "two")
        self.assertEqual(api.cookies_dict["modes"], "three")
        self.assertEqual(api.cookies_dict["unb"], "test")
        mode_request = next(r for r in requests if r.url.path.endswith("verify_modes.htm"))
        self.assertNotIn("_umidfg", mode_request.url.params)
        self.assertIn("normal=two", mode_request.headers["cookie"])

    async def test_login_calls_real_remote_entry_verification(self):
        result, api, _, qr = await self.run_flow(["3"], login=True)
        self.assertEqual(result["unb"], "test")
        self.assertEqual(qr.return_value.print_ascii.call_count, 2)
        api.get_token.assert_awaited_once()

    async def test_unknown_state(self):
        success, _, _, _ = await self.run_flow(["unknown"])
        self.assertFalse(success)

    async def test_timeout(self):
        api = Mock()
        api.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE)))
        try:
            with patch("myfish.adapters.fish.verification.qrcode.QRCode"):
                self.assertFalse(await verify_identity(api, PAGE_URL, timeout=0))
        finally:
            await api.client.aclose()

    async def test_continue_login_after_verification(self):
        client = AsyncMock()
        client.cookies_dict = {}
        client.qrcode_gen.return_value = {"content": "test", "t": "t", "ck": "ck"}
        client.qrcode_poll.side_effect = [
            {"status": "ERROR", "redirect_url": PAGE_URL}, {"status": "CONFIRMED"}
        ]
        with patch("myfish.adapters.fish.verify_identity", new_callable=AsyncMock, return_value=True) as verify, \
                patch("myfish.adapters.fish.asyncio.sleep", new_callable=AsyncMock), \
                patch("myfish.adapters.fish.qrcode.QRCode"):
            await qrcode_login(client)
        verify.assert_awaited_once_with(client, PAGE_URL)
        self.assertEqual(client.qrcode_poll.await_count, 2)
        self.assertEqual(client.qrcode_gen.await_count, 2)

    async def test_expired_session_regenerates_login_qr_with_verification_cookies(self):
        client = AsyncMock()
        client.cookies_dict = {"unb": "test", "verified_cookie": "keep"}
        client.client = Mock(headers={"cookie": "old"})
        client.qrcode_gen.side_effect = [
            {"content": "old", "t": "old-t", "ck": "old-ck"},
            {"content": "new", "t": "new-t", "ck": "new-ck"},
        ]
        client.qrcode_poll.side_effect = [
            {"status": "ERROR", "redirect_url": PAGE_URL},
            {"status": "SCANED"},
            {"status": "CONFIRMED"},
        ]
        client.get_token.return_value = {"data": {}, "ret": ["FAIL_SYS_SESSION_EXPIRED::Session过期"]}
        with patch("myfish.adapters.fish.verify_identity", new_callable=AsyncMock, return_value=True) as verify, \
                patch("myfish.adapters.fish.asyncio.sleep", new_callable=AsyncMock), \
                patch("myfish.adapters.fish.qrcode.QRCode") as qr:
            result = await qrcode_login(client)
        self.assertEqual(result["verified_cookie"], "keep")
        self.assertEqual(client.qrcode_gen.await_count, 2)
        self.assertEqual([call.args for call in client.qrcode_poll.await_args_list],
                         [("old-t", "old-ck"), ("new-t", "new-ck"), ("new-t", "new-ck")])
        self.assertEqual(qr.return_value.print_ascii.call_count, 2)
        verify.assert_awaited_once()

    def test_parser_rejects_other_pages_and_hosts(self):
        with self.assertRaises(ValueError):
            parse_verification_page("expired", PAGE_URL)
        with self.assertRaises(ValueError):
            parse_verification_page(PAGE.replace("passport.goofish.com/iv/photo", "example.com/iv/photo"), PAGE_URL)


if __name__ == "__main__":
    unittest.main()
