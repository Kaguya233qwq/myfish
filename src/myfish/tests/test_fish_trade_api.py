import json
import unittest
from unittest.mock import patch

import httpx
from myfish.adapters.fish.api import FishAPI, FishAPIError


class TradeAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.result = {"ret": ["SUCCESS::调用成功"], "data": {"module": {"items": []}}}
        self.api = FishAPI({"_m_h5_tk": "test_signing_token", "unb": "1"})
        await self.api.client.aclose()

        def handle(request):
            self.requests.append(request)
            return httpx.Response(
                200, json=self.result, headers={"set-cookie": "updated=value; Path=/"}
            )

        self.api.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.save = patch("myfish.adapters.fish.api.save_local_auth").start()
        self.addCleanup(patch.stopall)

    async def asyncTearDown(self):
        await self.api.close()

    def payload(self):
        from urllib.parse import parse_qs

        return json.loads(parse_qs(self.requests[-1].content.decode())["data"][0])

    async def test_seller_list_contract_and_request_header_isolation(self):
        await self.api.get_sold_orders(page=2, page_size=10, query_code="NOT_SHIP")
        request = self.requests[-1]
        self.assertEqual(
            request.url.params["api"], "mtop.taobao.idle.trade.merchant.sold.get"
        )
        self.assertEqual(request.url.params["type"], "originaljson")
        self.assertEqual(request.headers["origin"], "https://seller.goofish.com")
        self.assertEqual(
            self.payload(),
            {
                "pageNumber": 2,
                "rowsPerPage": 10,
                "orderIds": "",
                "queryCode": "NOT_SHIP",
                "orderSearchParam": "{}",
            },
        )
        self.assertNotIn("cookie", self.api.client.headers)
        await self.api.get_order_detail("123")
        self.assertNotEqual(
            self.requests[-1].headers.get("origin"), "https://seller.goofish.com"
        )
        self.assertEqual(self.payload(), {"tid": "123"})
        self.assertEqual(self.api.cookies_dict["updated"], "value")
        self.save.assert_called_once()

    async def test_write_protocols(self):
        await self.api.ship_order("123", " 已交付 ")
        self.assertEqual(
            self.requests[-1].url.params["api"],
            "mtop.taobao.idle.logistic.consign.dummy",
        )
        self.assertEqual(
            self.payload(),
            {
                "orderId": "123",
                "tradeText": "已交付",
                "picList": [],
                "newUnconsign": True,
            },
        )
        await self.api.request_red_flower("123")
        self.assertEqual(
            self.requests[-1].url.params["api"], "mtop.taobao.idlemessage.red.flower"
        )
        self.assertEqual(self.payload(), {"orderId": "123", "channel": "list"})
        await self.api.rate_buyer("123", " 谢谢 ")
        self.assertEqual(
            self.requests[-1].url.params["api"], "mtop.taobao.idle.rate.create"
        )
        self.assertEqual(self.requests[-1].url.params["v"], "4.0")
        self.assertEqual(
            self.payload(),
            {"tradeId": "123", "rate": 1, "feedback": "谢谢", "createOrAppend": 0},
        )
        await self.api.skip_bargain_and_ship("123", "456", "789")
        self.assertEqual(
            self.requests[-1].url.params["api"],
            "mtop.idle.groupon.activity.seller.freeshipping",
        )
        self.assertEqual(
            self.payload(), {"bizOrderId": "123", "itemId": 456, "buyerId": 789}
        )

    async def test_rating_and_refund_list_contracts(self):
        await self.api.get_pending_ratings()
        self.assertEqual(
            self.requests[-1].url.params["api"], "mtop.taobao.idle.merchant.rate.list"
        )
        self.assertEqual(self.payload()["rateSearchParam"], {"sellerRateStatus": "5"})
        await self.api.get_refund_orders("5", page=2)
        self.assertEqual(
            self.requests[-1].url.params["api"], "mtop.taobao.idle.merchant.refund.list"
        )
        self.assertEqual(
            self.payload(),
            {
                "pageNumber": 2,
                "rowsPerPage": 20,
                "queryType": "refund",
                "refundSearchParam": {"disputeStatus": "5", "queryCode": "ALL"},
            },
        )

    async def test_failures_are_not_retried_or_treated_as_success(self):
        for ret in (
            ["FAIL_SYS_SESSION_EXPIRED::sensitive"],
            ["FAIL_SYS_USER_VALIDATE::https://secret"],
            [],
            "SUCCESS",
            ["NOT_SUCCESS"],
        ):
            with self.subTest(ret=ret):
                self.result = {"ret": ret, "data": {}}
                previous_count = len(self.requests)
                with self.assertRaises(FishAPIError) as raised:
                    await self.api.ship_order("123")
                self.assertNotIn("sensitive", str(raised.exception))
                self.assertNotIn("https://secret", str(raised.exception))
                self.assertEqual(len(self.requests), previous_count + 1)

    async def test_invalid_arguments_never_send_requests(self):
        for invalid in ("", "abc", "１２３", True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await self.api.get_order_detail(invalid)
        for kwargs in (
            {"page": 0},
            {"page": True},
            {"page_size": 101},
            {"query_code": "bad"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.api.get_sold_orders(**kwargs)
        with self.assertRaises(ValueError):
            await self.api.rate_buyer("123", " ")
        with self.assertRaises(ValueError):
            await self.api.get_refund_orders("6")
        self.assertEqual(self.requests, [])

    async def test_signs_exact_payload_with_current_cookie(self):
        with patch(
            "myfish.adapters.fish.api.generate_sign", return_value="signature"
        ) as sign:
            await self.api.rate_buyer("123", "评价内容")
        signed = sign.call_args.args
        from urllib.parse import parse_qs

        sent = parse_qs(self.requests[-1].content.decode())["data"][0]
        self.assertEqual(signed[2], sent)
        self.assertEqual(signed[1], "test")
        self.assertEqual(self.requests[-1].url.params["sign"], "signature")


if __name__ == "__main__":
    unittest.main()
