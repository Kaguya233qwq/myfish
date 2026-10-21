from __future__ import annotations

import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI, FishAPIError
from myfish.adapters.fish.listings import FishListing, FishListingPage
from myfish.builtin_plugins.fishy.service import Preconditions, Service
from myfish.builtin_plugins.fishy.store import Store
from myfish.core.bot import Bot


def card(item_id: str | int, status: int = 0) -> dict[str, object]:
    return {
        "cardType": 1003,
        "cardData": {
            "id": item_id,
            "title": f"商品 {item_id}",
            "itemStatus": status,
            "priceInfo": {"price": "12.30"},
        },
    }


def response(*cards: dict[str, object], more: bool = False) -> dict[str, object]:
    return {
        "ret": ["SUCCESS::调用成功"],
        "data": {"cardList": list(cards), "nextPage": more},
    }


def listing_page(
    *ids: str, more: bool = False, cursor: str | None = None
) -> FishListingPage:
    return FishListingPage(
        items=tuple(
            FishListing(item_id, f"商品 {item_id}", Decimal("12.30")) for item_id in ids
        ),
        has_more=more,
        next_page_model=cursor,
    )


class ListingAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_selling_cards_and_preserves_decimal_price(self) -> None:
        async with FishAPI({"unb": "11"}) as api:
            api._call_mtop = AsyncMock(
                return_value=response(
                    card(99),
                    card("100", 1),
                    card("101", -2),
                    {"cardType": 999, "cardData": {"ad": True}},
                )
            )
            page = await api.get_listing_page("11")
            self.assertEqual(
                page.items, (FishListing("99", "商品 99", Decimal("12.30")),)
            )
            self.assertFalse(page.has_more)
            request = api._call_mtop.await_args
            self.assertEqual(request.args[0], "mtop.idle.web.xyh.item.list")
            self.assertEqual(
                request.args[2],
                {
                    "userId": "11",
                    "pageNumber": 1,
                    "pageSize": 20,
                    "needGroupInfo": True,
                },
            )

    async def test_forwards_server_cursors_and_selected_group(self) -> None:
        async with FishAPI({"unb": "11"}) as api:
            api._call_mtop = AsyncMock(
                return_value={
                    "ret": ["SUCCESS"],
                    "data": {
                        "cardList": [card("99")],
                        "nextPage": True,
                        "nextPageModel": "cursor",
                        "nextPageNum": "next-2",
                        "itemGroupList": [
                            {
                                "groupId": 123,
                                "groupName": "综合",
                                "defaultGroup": True,
                                "groupType": 0,
                            }
                        ],
                    },
                }
            )
            first = await api.get_listing_page("11")
            api._call_mtop.return_value = response(card("100"))
            second = await api.get_listing_page("11", 2, previous=first)
            request = api._call_mtop.await_args.args[2]
            self.assertEqual(request["nextPageModel"], "cursor")
            self.assertEqual(request["nextPageNum"], "next-2")
            self.assertEqual(request["groupId"], 123)
            self.assertEqual(request["groupName"], "综合")
            self.assertTrue(request["defaultGroup"])
            self.assertEqual(second.group, first.group)

    async def test_rejection_and_malformed_data_are_not_empty_success(self) -> None:
        responses = [
            {"ret": ["FAIL_SYS_USER_VALIDATE"], "data": {}},
            {"data": {"cardList": [], "nextPage": False}},
            {"ret": ["SUCCESS"], "data": {}},
            response({"cardType": 1003}),
            response(
                {
                    "cardType": 1003,
                    "cardData": {
                        "id": "99",
                        "title": "商品",
                        "itemStatus": 0,
                        "priceInfo": {"price": "NaN"},
                    },
                }
            ),
        ]
        async with FishAPI({"unb": "11"}) as api:
            for value in responses:
                with self.subTest(value=value):
                    api._call_mtop = AsyncMock(return_value=value)
                    with self.assertRaises(FishAPIError):
                        await api.get_listing_page("11")
            api._call_mtop = AsyncMock(return_value=response())
            self.assertEqual((await api.get_listing_page("11")).items, ())


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        await self.store.initialize()
        self.api = AsyncMock(spec=FishAPI)
        adapter = FishAdapter(self.api)
        adapter.bot_id = "11"
        self.service = Service(self.store, [Bot(adapter)])

    async def test_imports_multiple_pages_and_leaves_automation_disabled(self) -> None:
        self.api.get_listing_page.side_effect = [
            listing_page("99", more=True, cursor="next"),
            listing_page("100"),
        ]
        with patch(
            "myfish.builtin_plugins.fishy.catalog.asyncio.sleep", new=AsyncMock()
        ):
            self.assertEqual(await self.service.import_listings("11"), 2)
        products = await self.service.products()
        self.assertEqual({product["item_id"] for product in products}, {"99", "100"})
        for product in products:
            self.assertEqual(product["account_id"], "11")
            self.assertEqual(product["price"], "12.30")
            self.assertEqual(product["status"], "published")
            self.assertEqual(product["product_type"], "unknown")
            self.assertEqual(product["mode"], "disabled")
            self.assertFalse(product["auto_delivery"])
            self.assertFalse(product["auto_polish"])
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])

    async def test_repeat_import_preserves_local_content_and_pending_work(self) -> None:
        self.api.get_listing_page.return_value = listing_page("99")
        await self.service.import_listings("11")
        product_id = (await self.service.products())[0]["id"]
        await self.service.configure_imported_type(product_id, "virtual")
        await self.service.update_delivery_content(product_id, "本机交付内容")
        await self.store.execute(
            "UPDATE products SET title='本地标题',auto_delivery=1,auto_polish=1,status='unlisted' WHERE id=?",
            (product_id,),
        )
        before = await self.store.rows("SELECT * FROM products")
        self.assertEqual(await self.service.import_listings("11"), 0)
        self.assertEqual(await self.store.rows("SELECT * FROM products"), before)
        self.assertEqual(
            await self.service.delivery_content(product_id), "本机交付内容"
        )

    async def test_failed_later_page_retains_imports_and_can_retry(self) -> None:
        self.api.get_listing_page.side_effect = [
            listing_page("99", more=True, cursor="next"),
            FishAPIError("denied"),
        ]
        with (
            patch(
                "myfish.builtin_plugins.fishy.catalog.asyncio.sleep", new=AsyncMock()
            ),
            self.assertRaises(FishAPIError),
        ):
            await self.service.import_listings("11")
        self.assertEqual(len(await self.service.products()), 1)
        self.api.get_listing_page.side_effect = None
        self.api.get_listing_page.return_value = listing_page("99", "100")
        self.assertEqual(await self.service.import_listings("11"), 1)

    async def test_empty_remote_list_does_not_delete_local_products(self) -> None:
        self.api.get_listing_page.return_value = listing_page("99")
        await self.service.import_listings("11")
        self.api.get_listing_page.return_value = listing_page()
        self.assertEqual(await self.service.import_listings("11"), 0)
        self.assertEqual(len(await self.service.products()), 1)

    async def test_repeated_cursor_stops_requests(self) -> None:
        self.api.get_listing_page.return_value = listing_page(
            "99", more=True, cursor="same"
        )
        with (
            patch(
                "myfish.builtin_plugins.fishy.catalog.asyncio.sleep", new=AsyncMock()
            ),
            self.assertRaisesRegex(FishAPIError, "游标重复"),
        ):
            await self.service.import_listings("11")
        self.assertEqual(self.api.get_listing_page.await_count, 2)
        self.assertEqual(len(await self.service.products()), 1)

    async def test_same_item_id_is_scoped_to_account(self) -> None:
        other_adapter = FishAdapter(self.api)
        other_adapter.bot_id = "22"
        self.service.bots = [*self.service.bots, Bot(other_adapter)]
        self.api.get_listing_page.return_value = listing_page("99")
        await self.service.import_listings("11")
        await self.service.import_listings("22")
        self.assertEqual(len(await self.service.products()), 2)
        with self.assertRaises(Preconditions):
            await self.service.import_listings("33")

    async def test_concurrent_sync_is_idempotent(self) -> None:
        self.api.get_listing_page.return_value = listing_page("99")
        results = await asyncio.gather(
            self.service.import_listings("11"), self.service.import_listings("11")
        )
        self.assertEqual(sorted(results), [0, 1])

    async def test_imported_type_must_be_explicit_and_cannot_be_overwritten(
        self,
    ) -> None:
        self.api.get_listing_page.return_value = listing_page("99", "100")
        await self.service.import_listings("11")
        products = await self.service.products()
        await self.service.configure_imported_type(products[0]["id"], "physical")
        await self.service.configure_imported_type(products[1]["id"], "virtual")
        products = await self.service.products()
        self.assertEqual(products[0]["mode"], "physical")
        self.assertEqual(products[1]["mode"], "disabled")
        with self.assertRaises(Preconditions):
            await self.service.configure_imported_type(products[0]["id"], "virtual")


if __name__ == "__main__":
    unittest.main()
