import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from myfish.adapters.fish.api import FishAPI, FishPolishError


class PolishAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_success_daily_limit_and_rejections(self):
        response = {"ret": ["SUCCESS::调用成功"], "data": {}}
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=response)

        async with FishAPI({"_m_h5_tk": "token_123", "unb": "11"}) as api:
            await api.client.aclose()
            api.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            with patch("myfish.adapters.fish.api.save_local_auth"):
                result = await api.polish_item("99")
                self.assertIn("SUCCESS::调用成功", result["ret"])
                request = requests[-1]
                self.assertEqual(
                    request.url.path, "/h5/mtop.taobao.idle.item.polish/1.0/"
                )
                self.assertEqual(request.url.params["v"], "1.0")
                self.assertEqual(
                    json.loads(parse_qs(request.content.decode())["data"][0]),
                    {"itemId": "99"},
                )
                for code in (
                    "FAIL_BIZ_ITEM_ERROR_POLISH_DUPLICATE",
                    "IDLEITEM_POLISH_AGAIN",
                    "FAIL_BIZ_IDLEITEM_POLISH_AGAIN",
                ):
                    response = {"ret": [code + "::一天只能擦亮一次"], "data": {}}
                    result = await api.polish_item("99")
                    self.assertNotIn("SUCCESS", result["ret"])
                for value in (
                    ["FAIL_SYS_SESSION_EXPIRED::secret"],
                    ["FAIL_BIZ_UNSUPPORTED_ITEM_STATUS"],
                    [],
                    "SUCCESS",
                    ["NOT_SUCCESS"],
                ):
                    response = {"ret": value, "data": {}}
                    before = len(requests)
                    with self.assertRaises(FishPolishError) as caught:
                        await api.polish_item("99")
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertEqual(len(requests), before + 1)
                before = len(requests)
                with self.assertRaises(ValueError):
                    await api.polish_item("bad-id")
                self.assertEqual(len(requests), before)
