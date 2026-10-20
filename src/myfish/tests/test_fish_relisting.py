import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from myfish.adapters.fish.api import FishAPI, FishListingUnconfirmed
from myfish.adapters.fish.model import FishOrder, OrderStatus
from myfish.adapters.fish.utils import ListingRejected
from myfish.builtin_plugins.fishy.service import Service
from myfish.builtin_plugins.fishy.store import Store
from myfish.tests.test_fish_listings import snapshot


class SaleRelistingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        await self.store.initialize()
        self.api = AsyncMock()
        self.api.reshelf_item.return_value = {"itemId": "99", "itemStatus": "0"}
        self.bot = SimpleNamespace(
            bot_id="11", adapter=SimpleNamespace(api=self.api, _active_ws=True)
        )
        self.service = Service(self.store, [self.bot])
        await self.store.execute(
            "INSERT INTO products(id,title,description,price,account_id,item_id,mode,content,status,created,updated) "
            "VALUES(1,'test','','1','11','99','reusable','','published',0,0)"
        )
        await self.store.execute(
            "UPDATE products SET content=? WHERE id=1",
            (self.store.encrypt("download-link"),),
        )
        self.order = FishOrder(
            order_id="100", item_id="99", buyer_id="22", status=OrderStatus.PENDING_SHIP
        )
        self.api.get_sold_orders.return_value = self.page()

    def page(self, *, status="待发货", refund=False):
        return {
            "data": {
                "module": {
                    "items": [
                        {
                            "commonData": {
                                "orderId": "100",
                                "itemId": "99",
                                "orderStatus": status,
                                "inRefund": refund,
                            },
                            "buyerInfoVO": {"buyerId": "22"},
                            "priceVO": {"buyNum": 1},
                        }
                    ],
                    "nextPage": False,
                }
            }
        }

    async def test_sale_decrements_once_and_queues_one_persistent_reshelf(self):
        await asyncio.gather(
            *[self.service.observe("11", self.order) for _ in range(3)]
        )
        product = (await self.service.products())[0]
        self.assertEqual(product["mode"], "reusable")
        self.assertEqual(
            (await self.store.rows("SELECT COUNT(*) AS n FROM jobs"))[0]["n"], 1
        )
        await self.store.initialize()
        self.service = Service(self.store, [self.bot])
        self.assertTrue(await self.service.work_once())
        self.api.reshelf_item.assert_awaited_once_with(
            "99", quantity=1, after_sale=True
        )
        await self.service.observe("11", self.order)
        self.assertFalse(await self.service.work_once())
        self.assertEqual((await self.service.products())[0]["status"], "published")

    async def test_disabled_delivery_never_reopens_listing(self):
        await self.store.execute("UPDATE products SET mode='disabled' WHERE id=1")
        await self.service.observe("11", self.order)
        self.assertFalse(await self.service.work_once())
        self.api.reshelf_item.assert_not_awaited()

    async def test_changed_order_item_does_not_relist_original_product(self):
        await self.service.observe("11", self.order)
        self.api.get_sold_orders.return_value["data"]["module"]["items"][0][
            "commonData"
        ]["itemId"] = "98"
        await self.service.work_once()
        self.api.reshelf_item.assert_not_awaited()

    async def test_unpaid_refunding_and_physical_orders_do_not_prepare_delivery_or_relist(
        self,
    ):
        for order in (
            self.order.model_copy(update={"status": OrderStatus.PENDING_PAYMENT}),
            self.order.model_copy(update={"in_refund": True}),
        ):
            await self.service.observe("11", order)
        await self.store.execute(
            "UPDATE products SET product_type='physical' WHERE id=1"
        )
        await self.service.observe("11", self.order)
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])

    async def test_refund_before_execution_clears_unsent_content_and_skips_relisting(
        self,
    ):
        await self.service.observe("11", self.order)
        self.api.get_sold_orders.return_value = self.page(refund=True)
        await self.service.work_once()
        self.api.reshelf_item.assert_not_awaited()
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "skipped"
        )

    async def test_uncertain_reshelf_is_not_retried(self):
        await self.service.observe("11", self.order)
        self.api.reshelf_item.side_effect = FishListingUnconfirmed("结果待核对")
        await self.service.work_once()
        await self.service.observe("11", self.order)
        self.assertFalse(await self.service.work_once())
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "uncertain"
        )
        self.api.reshelf_item.assert_awaited_once()

    async def test_manual_delist_is_not_overridden(self):
        await self.store.execute("UPDATE products SET status='unlisted' WHERE id=1")
        await self.service.observe("11", self.order)
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        self.api.reshelf_item.assert_not_awaited()

    async def test_confirmed_failed_sale_reshelf_can_be_explicitly_scheduled_again(
        self,
    ):
        await self.service.observe("11", self.order)
        self.api.reshelf_item.side_effect = FishListingUnconfirmed("结果待核对")
        await self.service.work_once()
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": "1",
        }
        _, state = await self.service.listing_state(1)
        self.assertEqual(state, "unlisted")
        await self.store.execute(
            "UPDATE jobs SET status='cancelled' WHERE status='uncertain'"
        )
        self.api.reshelf_item.side_effect = None
        await self.service.schedule_product(1, "reshelf")
        await self.service.work_once()
        self.assertEqual(self.api.reshelf_item.await_count, 2)
        self.api.reshelf_item.assert_awaited_with("99", quantity=1, after_sale=True)

    async def test_refund_preserves_already_sent_content(self):
        await self.service.observe("11", self.order)
        await self.store.execute(
            "UPDATE orders SET delivery_state='sent' WHERE key='11:100'"
        )
        before = (await self.store.rows("SELECT content FROM orders"))[0]["content"]
        await self.service.observe(
            "11", self.order.model_copy(update={"in_refund": True})
        )
        row = (await self.store.rows("SELECT * FROM orders"))[0]
        self.assertEqual(row["delivery_state"], "sent")
        self.assertEqual(row["content"], before)

    async def test_disabled_delivery_after_queue_blocks_pending_reshelf(self):
        await self.service.observe("11", self.order)
        await self.store.execute("UPDATE products SET mode='disabled' WHERE id=1")
        await self.service.work_once()
        self.api.reshelf_item.assert_not_awaited()
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "failed"
        )


class SoldItemAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_sold_item_requires_explicit_sale_context_and_preserves_original_id(
        self,
    ):
        async with FishAPI({"unb": "11"}) as api:
            api.get_item_edit_detail = AsyncMock(return_value=snapshot(1))
            api._call_mtop = AsyncMock(
                return_value={"ret": ["SUCCESS"], "data": {"itemId": "99"}}
            )
            with self.assertRaises(ListingRejected):
                await api.reshelf_item("99", quantity=1)
            api._call_mtop.assert_not_awaited()
            api.get_item_edit_detail.side_effect = [snapshot(1), snapshot(0)]
            result = await api.reshelf_item("99", quantity=1, after_sale=True)
            self.assertEqual(result["itemStatus"], 0)
            payload = api._call_mtop.await_args.args[2]
            self.assertEqual(
                (payload["itemId"], payload["itemStatus"], payload["quantity"]),
                ("99", "0", "1"),
            )
            self.assertFalse(api._call_mtop.await_args.kwargs["_token_retry"])

    async def test_already_online_skips_write_and_system_offline_is_rejected(self):
        async with FishAPI({"unb": "11"}) as api:
            api.get_item_edit_detail = AsyncMock(return_value=snapshot(0))
            api._call_mtop = AsyncMock()
            await api.reshelf_item("99", quantity=1, after_sale=True)
            api._call_mtop.assert_not_awaited()
            api.get_item_edit_detail.return_value = snapshot(-12)
            with self.assertRaises(ListingRejected):
                await api.reshelf_item("99", quantity=1, after_sale=True)
            api._call_mtop.assert_not_awaited()
