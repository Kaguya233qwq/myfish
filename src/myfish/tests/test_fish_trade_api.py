import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from myfish.adapters.fish.api import FishAPI, FishAPIError, FishPublishPreparationError


class TradeAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_edit_platform_draft_are_separate_from_publish(self):
        self.api._prepare_item_payload = AsyncMock(
            return_value={"itemTextDTO": {"desc": "草稿"}, "uniqueCode": "123"}
        )
        self.result = {"ret": ["SUCCESS"], "data": {"draftId": "6673_1792242435499"}}
        await self.api.save_item_draft(
            [], "草稿", {"current_price": 1}, {"choice": "无需邮寄"}
        )
        self.assertEqual(
            self.requests[-1].url.params["api"], "mtop.idle.idleitem.draft.publish"
        )
        self.assertNotIn("draftId", self.payload())
        await self.api.save_item_draft(
            [],
            "修改草稿",
            {"current_price": 2},
            {"choice": "无需邮寄"},
            draft_id="6673_1792242435499",
        )
        self.assertEqual(
            self.requests[-1].url.params["api"], "mtop.idle.idleitem.draft.edit"
        )
        self.assertEqual(self.payload()["draftId"], "6673_1792242435499")
        self.assertFalse(
            any(
                r.url.params["api"] == "mtop.idle.pc.idleitem.publish"
                for r in self.requests
            )
        )
        with self.assertRaises(ValueError):
            await self.api.save_item_draft(
                [], "草稿", None, {}, draft_id="bad\nidentifier"
            )

    async def test_manual_category_survives_recommendation_and_physical_postage(self):
        self.api.upload_media = AsyncMock(
            return_value={"object": {"pix": "2x3", "url": "https://example.com/image"}}
        )
        self.api._get_public_channel = AsyncMock(
            return_value={
                "ret": ["SUCCESS"],
                "data": {
                    "cardList": [
                        {
                            "cardData": {
                                "propertyId": "-10000",
                                "propertyName": "分类",
                                "valuesList": [
                                    {
                                        "channelCatId": "999",
                                        "catName": "错误推荐",
                                        "isClicked": "1",
                                    }
                                ],
                            }
                        },
                        {
                            "cardData": {
                                "propertyId": "10",
                                "propertyName": "品牌",
                                "valuesList": [
                                    {
                                        "valueId": "12",
                                        "valueName": "测试品牌",
                                        "isClicked": "1",
                                    }
                                ],
                            }
                        },
                    ]
                },
            }
        )
        self.api._get_default_location = AsyncMock(return_value={"data": {}})
        selected = {"channelCatId": "2", "catId": "100", "catName": "男士T恤"}
        await self.api.publish_item(
            ["image.jpg"],
            "实物",
            {"current_price": 10},
            {"choice": "一口价", "post_price": "8.50"},
            quantity=3,
            category=selected,
        )
        self.assertEqual(
            self.api._get_public_channel.await_args.kwargs["category"], selected
        )
        payload = self.payload()
        self.assertEqual(payload["itemCatDTO"]["channelCatId"], "2")
        self.assertEqual(payload["itemPostFeeDTO"]["postPriceInCent"], "850")
        self.assertTrue(payload["itemPostFeeDTO"]["supportFreight"])
        self.assertEqual(payload["itemLabelExtList"][0]["channelCateId"], "2")
        self.assertEqual(payload["itemLabelExtList"][1]["valueId"], "12")

    async def test_publish_recommended_label_without_tb_category(self):
        self.api.upload_media = AsyncMock(
            return_value={"object": {"pix": "2x3", "url": "https://example.com/image"}}
        )
        self.api._get_public_channel = AsyncMock(
            return_value={
                "data": {
                    "categoryPredictResult": {"sugShow": "1"},
                    "cardList": [
                        {
                            "cardData": {
                                "propertyId": "-10000",
                                "propertyName": "类型",
                                "valuesList": [
                                    {
                                        "isClicked": "1",
                                        "catName": "虚拟商品",
                                        "channelCatId": "2",
                                        "catId": "100",
                                        "channelCat1Id": "10",
                                        "channelCat2Id": "20",
                                        "channelCat3Id": "30",
                                    }
                                ],
                            }
                        }
                    ],
                }
            }
        )
        self.api._get_default_location = AsyncMock(return_value={"data": {}})
        await self.api.publish_item(
            ["test.jpg"], "说明", {"current_price": 1}, {"choice": "无需邮寄"}
        )
        label = self.payload()["itemLabelExtList"][0]
        self.assertEqual(label["channelCateId"], "2")
        self.assertNotIn("tbCatId", label)
        self.assertEqual(self.payload()["itemCatDTO"]["channelCatId"], "2")
        self.assertEqual(self.payload()["itemCatDTO"]["channelCat3Id"], "30")
        self.assertEqual(self.payload()["itemCatDTO"]["catId"], "100")
        self.api._get_public_channel.return_value["data"]["cardList"][0]["cardData"][
            "valuesList"
        ][0]["tbCatId"] = "3"
        await self.api.publish_item(
            ["test.jpg"], "说明", {"current_price": 1}, {"choice": "无需邮寄"}
        )
        self.assertEqual(self.payload()["itemLabelExtList"][0]["tbCatId"], "3")
        self.api._get_public_channel.return_value = {
            "data": {"categoryPredictResult": {"sugShow": "1"}}
        }
        before = len(self.requests)
        with self.assertRaisesRegex(FishPublishPreparationError, "有效频道类目"):
            await self.api.publish_item(
                ["test.jpg"], "说明", {"current_price": 1}, {"choice": "无需邮寄"}
            )
        self.assertEqual(len(self.requests), before)

    async def test_upload_non_json_identifies_preparation_failure(self):
        self.api.client._transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html>login</html>")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.jpg"
            path.write_bytes(b"image")
            with self.assertRaisesRegex(
                FishPublishPreparationError, "图片上传返回非 JSON"
            ):
                await self.api.upload_media(str(path))

    async def test_token_retry_is_bounded(self):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(
                200,
                json={"ret": ["FAIL_SYS_TOKEN_EXOIRED"]},
                headers={
                    "set-cookie": f"_m_h5_tk=token{len(self.requests)}_999; Path=/"
                },
            )

        self.api.client._transport = httpx.MockTransport(handle)
        with self.assertRaises(FishAPIError):
            await self.api.get_sold_orders()
        self.assertEqual(len(self.requests), 2)

    async def test_upload_uses_multipart_and_current_login_cookie(self):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(
                200, json={"object": {"pix": "1x1", "url": "https://example.com/a"}}
            )

        self.api.client.headers["content-type"] = "application/x-www-form-urlencoded"
        self.api.client._transport = httpx.MockTransport(handle)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.jpg"
            path.write_bytes(b"test-image")
            await self.api.upload_media(str(path))
        request = self.requests[-1]
        self.assertTrue(
            request.headers["content-type"].startswith("multipart/form-data; boundary=")
        )
        self.assertIn("_m_h5_tk=test_signing_token", request.headers["cookie"])
        self.assertIn(b"image/jpeg", request.content)

    async def test_token_rejection_retries_once_with_updated_signature(self):
        def handle(request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return httpx.Response(
                    200,
                    json={"ret": ["FAIL_SYS_TOKEN_EXOIRED"]},
                    headers={"set-cookie": "_m_h5_tk=newtoken_999; Path=/"},
                )
            return httpx.Response(200, json={"ret": ["SUCCESS"], "data": {}})

        self.api.client._transport = httpx.MockTransport(handle)
        await self.api.get_sold_orders()
        self.assertEqual(len(self.requests), 2)
        self.assertNotEqual(
            self.requests[0].url.params["sign"], self.requests[1].url.params["sign"]
        )
        self.assertIn("_m_h5_tk=newtoken_999", self.requests[1].headers["cookie"])

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

    async def test_downshelf_requires_explicit_platform_confirmation(self):
        self.result = {"ret": ["SUCCESS"], "data": {"success": True}}
        await self.api.downshelf_item("123")
        self.assertEqual(
            self.requests[-1].url.params["api"], "mtop.taobao.idle.item.downshelf"
        )
        self.assertEqual(self.requests[-1].url.params["v"], "2.0")
        self.assertEqual(self.payload(), {"itemId": "123"})
        self.result["data"]["success"] = False
        with self.assertRaises(FishAPIError):
            await self.api.downshelf_item("123")

    async def test_publish_quantity_and_decimal_price_are_exact(self):
        self.api.upload_media = AsyncMock(
            return_value={
                "object": {"pix": "100x100", "url": "https://example.com/image.jpg"}
            }
        )
        self.api._get_public_channel = AsyncMock(
            return_value={"data": {"categoryPredictResult": {"channelCatId": "2"}}}
        )
        self.api._get_default_location = AsyncMock(return_value={"data": {}})
        await self.api.publish_item(
            ["test.jpg"],
            "说明",
            {"current_price": 0.29, "original_price": 0.29},
            {"choice": "一口价", "post_price": 0.29},
            quantity=2,
        )
        self.assertEqual(self.payload()["quantity"], "2")
        self.assertEqual(self.payload()["itemPriceDTO"]["priceInCent"], "29")
        self.assertEqual(self.payload()["itemPostFeeDTO"]["postPriceInCent"], "29")

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
