import unittest
from unittest.mock import AsyncMock

from myfish.adapters.fish.api import FishAPI, FishAPIError
from myfish.adapters.fish.utils import ListingRejected, reshelf_payload


def snapshot(status=-2):
    return {
        "itemId": "99",
        "userId": "11",
        "itemStatus": status,
        "quantity": "0",
        "itemTextDTO": {"desc": "商品"},
        "itemPriceDTO": {"priceInCent": "990"},
        "itemCatDTO": {"catId": "123"},
        "itemAddrDTO": {"city": "测试"},
        "imageInfoDOList": [{"url": "https://example.com/image.jpg"}],
        "itemPostFeeDTO": {"canFreeShipping": "false", "postPriceInCent": "850"},
        "userRightsProtocols": [{"enable": "true"}],
        "private_metadata": "omit",
    }


class ListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_item_edit_uses_edit_endpoint_without_token_replay(self):
        async with FishAPI({"unb": "11"}) as api:
            api._call_mtop = AsyncMock(
                return_value={"ret": ["SUCCESS"], "data": {"itemId": "99"}}
            )
            api.get_item_edit_detail = AsyncMock(return_value=snapshot(0))
            result = await api.submit_item_edit("99", {"itemId": "99"})
            self.assertEqual(result["itemStatus"], 0)
            self.assertEqual(
                api._call_mtop.await_args.args[0], "mtop.idle.pc.idleitem.edit"
            )
            self.assertFalse(api._call_mtop.await_args.kwargs["_token_retry"])
            api.get_item_edit_detail.return_value = {**snapshot(-9), "userId": "0"}
            self.assertEqual(
                (await api.submit_item_edit("99", {"itemId": "99"}))["itemStatus"], -9
            )

    async def test_edit_rejection_and_ambiguous_responses_are_distinct(self):
        async with FishAPI({"unb": "11"}) as api:
            api._call_mtop = AsyncMock(
                return_value={"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {}}
            )
            with self.assertRaises(ListingRejected):
                await api.submit_item_edit("99", {"itemId": "99"})
            for response in (
                {"data": {}},
                {"ret": ["SUCCESS"], "data": {"itemId": "100"}},
            ):
                api._call_mtop.return_value = response
                with self.assertRaises(FishAPIError):
                    await api.submit_item_edit("99", {"itemId": "99"})

    async def test_read_only_review_status_does_not_require_public_detail(self):
        async with FishAPI({"unb": "11"}) as api:
            api._call_mtop = AsyncMock(
                return_value={
                    "ret": ["SUCCESS"],
                    "data": {**snapshot(-9), "userId": "0"},
                }
            )
            api.get_item_info = AsyncMock(
                side_effect=AssertionError("不可访问公开详情")
            )
            result = await api.get_item_edit_detail("99", verify_owner=False)
            self.assertEqual(result["itemStatus"], -9)
            api.get_item_info.assert_not_awaited()

    async def test_reshelf_submission_can_enter_review_without_public_detail(self):
        async with FishAPI({"unb": "11"}) as api:
            api.get_item_edit_detail = AsyncMock(
                side_effect=[snapshot(), {**snapshot(-9), "userId": "0"}]
            )
            api._call_mtop = AsyncMock(return_value={"ret": ["SUCCESS"], "data": {}})
            result = await api.reshelf_item("99", quantity=1)
            self.assertEqual(result["itemStatus"], -9)
            self.assertEqual(api.get_item_edit_detail.await_count, 2)
            self.assertEqual(
                api.get_item_edit_detail.await_args.kwargs, {"verify_owner": False}
            )

    async def test_hidden_edit_owner_is_resolved_from_matching_item_detail(self):
        async with FishAPI({"unb": "11"}) as api:
            detail = {**snapshot(), "userId": "0"}
            api._call_mtop = AsyncMock(
                return_value={"ret": ["SUCCESS"], "data": detail}
            )
            api.get_item_info = AsyncMock(
                return_value={
                    "ret": ["SUCCESS"],
                    "data": {
                        "itemDO": {"itemId": "99"},
                        "sellerDO": {"sellerId": "11"},
                    },
                }
            )
            result = await api.get_item_edit_detail("99")
            self.assertEqual(result["userId"], "11")
            self.assertEqual(detail["userId"], "0")
            api.get_item_info.assert_awaited_once_with("99")
            reshelf_payload(result, "99", "11", 1)

    async def test_owner_fallback_rejects_wrong_item_and_preserves_foreign_owner(self):
        async with FishAPI({"unb": "11"}) as api:
            api._call_mtop = AsyncMock(
                return_value={"ret": ["SUCCESS"], "data": {**snapshot(), "userId": "0"}}
            )
            api.get_item_info = AsyncMock(
                return_value={
                    "ret": ["SUCCESS"],
                    "data": {
                        "itemDO": {"itemId": "100"},
                        "sellerDO": {"sellerId": "11"},
                    },
                }
            )
            with self.assertRaises(ListingRejected):
                await api.get_item_edit_detail("99")
            api.get_item_info.return_value["data"]["itemDO"]["itemId"] = "99"
            api.get_item_info.return_value["data"]["sellerDO"]["sellerId"] = "22"
            result = await api.get_item_edit_detail("99")
            with self.assertRaises(ListingRejected):
                reshelf_payload(result, "99", "11", 1)
            api.get_item_info.return_value = {
                "ret": ["FAIL_BIZ_ITEM_NOT_FOUND"],
                "data": {},
            }
            with self.assertRaises(ListingRejected):
                await api.get_item_edit_detail("99")

    async def test_valid_edit_owner_needs_no_extra_request(self):
        async with FishAPI({"unb": "11"}) as api:
            api._call_mtop = AsyncMock(
                return_value={"ret": ["SUCCESS"], "data": snapshot()}
            )
            api.get_item_info = AsyncMock()
            self.assertEqual((await api.get_item_edit_detail("99"))["userId"], "11")
            api.get_item_info.assert_not_awaited()

    async def test_missing_result_code_or_changed_id_requires_reconciliation(self):
        for response in ({"data": {}}, {"ret": ["SUCCESS"], "data": {"itemId": "100"}}):
            async with FishAPI({"unb": "11"}) as api:
                api.get_item_edit_detail = AsyncMock(return_value=snapshot())
                api._call_mtop = AsyncMock(return_value=response)
                with self.assertRaises(FishAPIError):
                    await api.reshelf_item("99", quantity=1)

    def test_snapshot_keeps_original_id_and_price_without_mutation(self):
        detail = snapshot()
        result = reshelf_payload(detail, "99", "11", 3)
        self.assertEqual(result["itemId"], "99")
        self.assertEqual(result["quantity"], "3")
        self.assertEqual(result["itemStatus"], "0")
        self.assertEqual(result["itemPriceDTO"]["priceInCent"], "990")
        self.assertEqual(result["itemPostFeeDTO"]["postPriceInCent"], "850")
        self.assertIs(result["itemPostFeeDTO"]["canFreeShipping"], False)
        self.assertIs(result["userRightsProtocols"][0]["enable"], True)
        self.assertNotIn("private_metadata", result)
        self.assertEqual(detail, snapshot())

    def test_rejects_wrong_owner_sold_system_offline_sku_and_empty_inventory(self):
        for status in (0, 1, -3, -12, -9):
            with self.subTest(status=status), self.assertRaises(ListingRejected):
                reshelf_payload(snapshot(status), "99", "11", 1)
        for owner, quantity in (("22", 1), ("11", 0), ("11", True)):
            with self.assertRaises(ListingRejected):
                reshelf_payload(snapshot(), "99", owner, quantity)
        with self.assertRaises(ListingRejected):
            reshelf_payload({**snapshot(), "itemSkuList": [{"id": "1"}]}, "99", "11", 1)

    async def test_edit_then_readback_only_success_for_original_onsale_item(self):
        async with FishAPI({"unb": "11"}) as api:
            api.get_item_edit_detail = AsyncMock(side_effect=[snapshot(), snapshot(0)])
            api._call_mtop = AsyncMock(
                return_value={"ret": ["SUCCESS"], "data": {"itemId": "99"}}
            )
            result = await api.reshelf_item("99", quantity=2)
            self.assertEqual(result["itemStatus"], 0)
            call = api._call_mtop.await_args
            self.assertEqual(call.args[0], "mtop.idle.pc.idleitem.edit")
            self.assertEqual(call.args[2]["quantity"], "2")
            self.assertFalse(call.kwargs["_token_retry"])

    async def test_readback_denial_is_uncertain(self):
        for after in (ListingRejected("无法读取"),):
            async with FishAPI({"unb": "11"}) as api:
                api.get_item_edit_detail = AsyncMock(side_effect=[snapshot(), after])
                api._call_mtop = AsyncMock(
                    return_value={"ret": ["SUCCESS"], "data": {}}
                )
                with self.assertRaises(FishAPIError):
                    await api.reshelf_item("99", quantity=1)

    async def test_explicit_rejection_and_preconditions_never_create_new_item(self):
        async with FishAPI({"unb": "11"}) as api:
            api.get_item_edit_detail = AsyncMock(return_value=snapshot())
            api._call_mtop = AsyncMock(
                return_value={
                    "ret": ["FAIL_BIZ_PC_NOT_SUPPORT_PUBLISH_OR_EDIT"],
                    "data": {},
                }
            )
            with self.assertRaises(ListingRejected):
                await api.reshelf_item("99", quantity=1)
            self.assertEqual(api._call_mtop.await_count, 1)
            api._call_mtop.reset_mock()
            api.get_item_edit_detail.return_value = snapshot(1)
            with self.assertRaises(ListingRejected):
                await api.reshelf_item("99", quantity=1)
            api._call_mtop.assert_not_awaited()
