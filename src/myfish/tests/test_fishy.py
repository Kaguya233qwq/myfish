import asyncio
import importlib.util
import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from tests.test_fish_receive import payload, push

if not importlib.util.find_spec("fastapi") or not importlib.util.find_spec("uvicorn"):
    raise unittest.SkipTest("虚拟商品 Web 测试需要 fishy 可选依赖")
from decimal import Decimal

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI, FishAPIError, FishListingUnconfirmed
from myfish.adapters.fish.events import FishOrderDiscoveredEvent
from myfish.adapters.fish.listings import FishListing, FishListingPage
from myfish.adapters.fish.model import FishOrder, OrderStatus
from myfish.builtin_plugins.fishy import Runtime, plugin
from myfish.builtin_plugins.fishy.auth import AuthError
from myfish.builtin_plugins.fishy.config import Settings
from myfish.builtin_plugins.fishy.service import Preconditions, Service
from myfish.builtin_plugins.fishy.store import Store
from myfish.builtin_plugins.fishy.web import ProductInput, create_app
from myfish.core.bot import Bot
from myfish.core.engine import Engine

PASSWORD = "test-strong-password-123"


class FishyTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_trade_notification_triggers_one_order_sync(self):
        engine = Engine()
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        adapter.bot_id = "11"
        engine.add_adapter(adapter)
        engine.plugin_manager.plugins["fishy"] = plugin
        bot = Bot(adapter)

        async def synced(*args, **kwargs):
            await engine.dispatch_event(
                bot, FishOrderDiscoveredEvent("11", FishOrder(order_id="100"))
            )

        sync = AsyncMock(side_effect=synced)
        runtime = SimpleNamespace(service=SimpleNamespace(sync=sync))
        with patch("myfish.builtin_plugins.fishy._runtime", runtime):
            await adapter._handle_raw_message(
                push(
                    {
                        "data": payload(
                            {
                                "contentType": 26,
                                "order_id": "100",
                                "title": "已付款",
                            }
                        )
                    }
                )
            )
            await asyncio.gather(*engine._background_tasks)
            sync.assert_awaited_once_with("11", order_id="100", chat_id="conversation")
            sync.reset_mock()
            await adapter._handle_raw_message(
                push(
                    {
                        "data": payload(
                            {
                                "contentType": 26,
                                "order_id": "",
                            }
                        )
                    }
                )
            )
            await asyncio.gather(*engine._background_tasks)
            sync.assert_not_awaited()

    async def test_order_tracking_uses_plugin_storage_and_emits_changes(self):
        emit = AsyncMock()
        service = Service(self.store, [self.bot], emit=emit)
        await service.add_product(self.product(mode="reusable"))
        self.platform_order()
        await service.sync("11")
        await service.sync("11")
        self.assertEqual(self.api.get_sold_orders.await_count, 2)
        emit.assert_awaited_once()
        self.assertIs(emit.await_args.args[0], self.bot)
        self.assertEqual(emit.await_args.args[1].event_type, "fish.order.discovered")
        self.assertEqual(
            service.trackers["11"].state_path,
            self.store.directory / "orders" / "11.json",
        )
        self.api.get_sold_orders.return_value["data"]["module"]["items"][0][
            "commonData"
        ]["orderStatus"] = "交易成功"
        await service.sync("11")
        self.assertEqual(emit.await_count, 2)
        self.assertEqual(emit.await_args.args[1].event_type, "fish.order.updated")
        row = (await self.store.rows("SELECT platform_status FROM orders"))[0]
        self.assertEqual(row["platform_status"], "completed")

    async def test_order_snapshots_migrate_without_duplicate_events(self):
        self.platform_order()
        root = self.store.directory / "legacy-data"
        legacy_paths = (
            self.store.directory / "orders-11.json",
            root / "adapters" / "fish" / "orders-11.json",
        )
        await Service(self.store, [self.bot]).sync("11")
        path = self.store.directory / "orders" / "11.json"
        for legacy_path in legacy_paths:
            with self.subTest(path=legacy_path):
                legacy_path.parent.mkdir(parents=True, exist_ok=True)
                path.rename(legacy_path)
                emit = AsyncMock()
                service = Service(
                    self.store,
                    [self.bot],
                    emit=emit,
                    legacy_order_dir=root / "adapters" / "fish",
                )
                await service.sync("11")
                self.assertTrue(path.is_file())
                self.assertFalse(legacy_path.exists())
                emit.assert_not_awaited()

    async def test_auth_dependencies_use_the_current_app(self):
        with tempfile.TemporaryDirectory() as directory:
            other_store = Store(Path(directory))
            await other_store.initialize()
            other_app = create_app(Service(other_store, [self.bot]), Settings())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=other_app),
                base_url="http://localhost",
                cookies=self.client.cookies,
            ) as other_client:
                self.assertEqual(
                    (await other_client.get("/api/auth/me")).status_code, 401
                )
                response = await self.client.get("/api/auth/me")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["username"], "admin")

    async def test_order_list_exposes_platform_amount_and_buyer(self):
        await self.service.add_product(self.product(mode="reusable"))
        order = self.order().model_copy(
            update={"amount": "19.90", "buyer_name": "测试买家"}
        )
        await self.service.observe("11", order)
        row = (await self.client.get("/api/orders")).json()[0]
        self.assertEqual(row["amount"], "19.90")
        self.assertEqual(row["buyer_id"], "22")
        self.assertEqual(row["buyer_name"], "测试买家")
        await self.service.observe("11", self.order())
        await self.service.observe(
            "11", order.model_copy(update={"amount": "0.00", "buyer_name": "新昵称"})
        )
        row = (await self.client.get("/api/orders")).json()[0]
        self.assertEqual(row["amount"], "0.00")
        self.assertEqual(row["buyer_name"], "新昵称")

    async def test_v6_upgrade_preserves_existing_order_delivery(self):
        await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order(), "chat")
        await self.store.execute("UPDATE orders SET delivery_state='completed'")

        def downgrade(db):
            for column in ("amount", "buyer_name"):
                db.execute(f"ALTER TABLE orders DROP COLUMN {column}")
            db.execute("PRAGMA user_version=6")

        await self.store.call(downgrade, write=True)
        await self.store.initialize()
        row = (await self.store.rows("SELECT * FROM orders"))[0]
        self.assertEqual(row["delivery_state"], "completed")
        self.assertEqual(row["chat_id"], "chat")
        self.assertEqual(row["amount"], "")
        self.assertEqual(row["buyer_name"], "")
        await self.service.observe(
            "11",
            self.order().model_copy(update={"amount": "1.88", "buyer_name": "买家"}),
        )
        row = (await self.store.rows("SELECT * FROM orders"))[0]
        self.assertEqual(row["amount"], "1.88")
        self.assertEqual(row["buyer_name"], "买家")
        self.assertEqual(row["delivery_state"], "completed")

    async def test_failed_job_persists_safe_error_logs_for_admin_only(self):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        await self.service.schedule_product(product_id, "delist")
        self.api.downshelf_item.side_effect = RuntimeError("SECRET-TOKEN-DO-NOT-LOG")
        await self.service.work_once()
        job = (await self.store.rows("SELECT * FROM jobs"))[0]
        response = await self.client.get(f"/api/jobs/{job['id']}/logs")
        self.assertEqual(response.status_code, 200)
        logs = response.json()
        self.assertIn("RuntimeError", logs[0]["message"])
        self.assertIn("service.py", logs[0]["location"])
        self.assertNotIn("SECRET-TOKEN", response.text)
        await self.app.state.auth.create_user("logoperator", PASSWORD, "operator")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        ) as operator:
            await operator.post(
                "/api/auth/login",
                json={"username": "logoperator", "password": PASSWORD},
            )
            self.assertEqual(
                (await operator.get(f"/api/jobs/{job['id']}/logs")).status_code, 403
            )

    async def test_legacy_job_error_is_available_without_new_log_record(self):
        job_id = await self.store.execute(
            "INSERT INTO jobs(kind,target,account_id,run_at,created,updated,status,error) VALUES('publish','1','11',0,0,1,'failed','旧错误')"
        )
        response = await self.client.get(f"/api/jobs/{job_id}/logs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["message"], "旧错误")

    async def test_delivery_content_can_change_while_published_without_platform_edit(
        self,
    ):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        response = await self.client.get(f"/api/products/{product_id}/delivery-content")
        self.assertEqual(response.json()["content"], "固定交付内容")
        response = await self.client.put(
            f"/api/products/{product_id}/delivery-content",
            json={"content": "https://new.example/download"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            await self.service.delivery_content(product_id),
            "https://new.example/download",
        )
        self.assertNotIn("new.example", str(await self.service.products()))
        self.assertNotIn(
            "new.example", str(await self.store.rows("SELECT * FROM audit"))
        )
        self.assertNotIn(
            "new.example",
            (await self.store.rows("SELECT content FROM products"))[0]["content"],
        )
        self.assertEqual((await self.service.products())[0]["status"], "published")
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        self.api.submit_item_edit.assert_not_awaited()

    async def test_content_update_changes_only_unsent_reserved_snapshots(self):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        for order_id, state in (
            ("100", "reserved"),
            ("101", "sending"),
            ("102", "sent"),
            ("103", "completed"),
        ):
            await self.service.observe("11", self.order(order_id), "chat")
            await self.service.reserve("11:" + order_id)
            await self.store.execute(
                "UPDATE orders SET delivery_state=? WHERE key=?",
                (state, "11:" + order_id),
            )
        self.assertEqual(
            await self.service.update_delivery_content(product_id, "新链接"), 1
        )
        rows = await self.store.rows(
            "SELECT order_id,content FROM orders ORDER BY order_id"
        )
        self.assertEqual(self.store.decrypt(rows[0]["content"]), "新链接")
        self.assertTrue(
            all(
                self.store.decrypt(row["content"]) == "固定交付内容" for row in rows[1:]
            )
        )
        await self.service.observe("11", self.order("104"), "chat")
        self.assertEqual(
            self.store.decrypt((await self.service.reserve("11:104"))["content"]),
            "新链接",
        )

    async def test_content_changed_between_reserve_and_send_uses_latest_snapshot(self):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order(), "chat")
        self.platform_order()
        original = self.service.reserve

        async def reserve(key):
            order = await original(key)
            await self.service.update_delivery_content(product_id, "发送前更新的链接")
            return order

        self.service.reserve = reserve
        await self.service.deliver("11:100")
        self.assertEqual(self.bot.send_msg.await_args.args[1].text, "发送前更新的链接")
        self.bot.send_msg.assert_awaited_once()

    async def test_content_editor_rejects_physical_empty_content_and_missing_csrf(self):
        product_id = await self.service.add_product(
            self.product(mode="physical", product_type="physical")
        )
        self.assertEqual(
            (
                await self.client.put(
                    f"/api/products/{product_id}/delivery-content",
                    json={"content": "替换"},
                )
            ).status_code,
            400,
        )
        self.assertEqual(
            (
                await self.client.put(
                    f"/api/products/{product_id}/delivery-content",
                    json={"content": "   "},
                )
            ).status_code,
            400,
        )
        del self.client.headers["X-CSRF-Token"]
        self.assertEqual(
            (
                await self.client.put(
                    f"/api/products/{product_id}/delivery-content",
                    json={"content": "替换"},
                )
            ).status_code,
            403,
        )

    async def test_automatic_listing_sync_batches_wraps_and_skips_active_tasks(self):
        first = await self.service.add_product(self.product(mode="reusable"))
        second = await self.service.add_product(
            self.product(mode="reusable").model_copy(update={"item_id": "100"})
        )
        await self.service.schedule_product(second, "delist", time.time() + 60)
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "0",
            "itemStatus": "-9",
        }
        with patch(
            "myfish.builtin_plugins.fishy.service.asyncio.sleep",
            new=AsyncMock(),
        ):
            cursor = await self.service.sync_listings("11", limit=1)
            self.assertEqual(cursor, first)
            self.assertEqual((await self.service.products())[1]["status"], "reviewing")
            self.assertEqual(
                await self.service.sync_listings("11", after_id=cursor, limit=1), first
            )
        self.assertEqual(self.api.get_item_edit_detail.await_count, 2)
        self.api.get_item_info.assert_not_awaited()

    async def test_automatic_listing_sync_stops_after_request_error_and_retains_state(
        self,
    ):
        first = await self.service.add_product(self.product(mode="reusable"))
        await self.service.add_product(
            self.product(mode="reusable").model_copy(update={"item_id": "100"})
        )
        self.api.get_item_edit_detail.side_effect = TimeoutError()
        self.assertEqual(await self.service.sync_listings("11"), first)
        self.api.get_item_edit_detail.assert_awaited_once()
        self.assertTrue(
            all(p["status"] == "published" for p in await self.service.products())
        )

    async def test_product_import_endpoint_and_explicit_type_configuration(self):
        self.api.get_listing_page.return_value = FishListingPage(
            (FishListing("99", "平台已有商品", Decimal("9.90")),), False
        )
        response = await self.client.post("/api/accounts/11/products/sync")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"imported": 1})
        products = (await self.client.get("/api/products")).json()
        self.assertEqual(products[0]["product_type"], "unknown")
        product_id = products[0]["id"]
        response = await self.client.put(
            f"/api/products/{product_id}/product-type",
            json={"product_type": "physical"},
        )
        self.assertEqual(response.status_code, 200)
        response = await self.client.post("/api/accounts/11/products/sync")
        self.assertEqual(response.json(), {"imported": 0})
        product = (await self.client.get("/api/products")).json()[0]
        self.assertEqual(product["product_type"], "physical")
        self.assertEqual(product["mode"], "physical")
        self.assertEqual(product["auto_delivery"], 0)

    async def test_product_import_reports_remote_errors_and_rejects_missing_csrf(self):
        self.api.get_listing_page.side_effect = FishAPIError("平台拒绝商品列表查询")
        response = await self.client.post("/api/accounts/11/products/sync")
        self.assertEqual(response.status_code, 502)
        self.assertIn("平台拒绝", response.json()["detail"])
        self.client.headers.pop("X-CSRF-Token")
        self.api.get_listing_page.reset_mock()
        response = await self.client.post("/api/accounts/11/products/sync")
        self.assertEqual(response.status_code, 403)
        self.api.get_listing_page.assert_not_awaited()

    async def test_runtime_discovers_account_connected_after_startup(self):
        runtime = Runtime(Settings(), SimpleNamespace())
        runtime.service = SimpleNamespace(
            bots=[self.bot],
            import_listings=AsyncMock(return_value=0),
            sync_listings=AsyncMock(return_value=0),
        )
        self.bot.adapter._active_ws = None
        sleeps = 0

        async def connect_then_stop(seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                self.assertEqual(seconds, 5)
                self.bot.adapter._active_ws = True
            else:
                raise asyncio.CancelledError

        with (
            patch(
                "myfish.builtin_plugins.fishy.asyncio.sleep",
                side_effect=connect_then_stop,
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await runtime.listing_sync_loop()
        runtime.service.import_listings.assert_awaited_once_with("11")

    async def test_runtime_automatically_syncs_only_connected_accounts(self):
        runtime = Runtime(Settings(), SimpleNamespace())
        runtime.service = SimpleNamespace(
            bots=[self.bot],
            import_listings=AsyncMock(return_value=0),
            sync_listings=AsyncMock(return_value=1),
        )
        with (
            patch(
                "myfish.builtin_plugins.fishy.asyncio.sleep",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await runtime.listing_sync_loop()
        runtime.service.import_listings.assert_awaited_once_with("11")
        runtime.service.sync_listings.assert_awaited_once_with(
            "11", after_id=0, limit=20
        )
        runtime.service.sync_listings.reset_mock()
        runtime.service.import_listings.reset_mock()
        self.bot.adapter._active_ws = None
        with (
            patch(
                "myfish.builtin_plugins.fishy.asyncio.sleep",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await runtime.listing_sync_loop()
        runtime.service.sync_listings.assert_not_awaited()
        runtime.service.import_listings.assert_not_awaited()

    async def test_review_sync_and_reshelf_block_duplicate_publish_and_polish(self):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "0",
            "itemStatus": "-9",
        }
        response = await self.client.post(
            f"/api/products/{product_id}/refresh_listing", json={}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "reviewing")
        for action in ("publish", "reshelf", "polish"):
            with self.assertRaises(Preconditions):
                await self.service.schedule_product(product_id, action)
        self.api.get_item_info.assert_not_awaited()
        await self.store.execute(
            "UPDATE products SET status='unlisted' WHERE id=?", (product_id,)
        )
        self.api.reshelf_item.return_value = {
            "itemId": "99",
            "userId": "0",
            "itemStatus": "-9",
        }
        await self.service.schedule_product(product_id, "reshelf")
        await self.service.work_once()
        self.assertEqual((await self.service.products())[0]["status"], "reviewing")
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "succeeded"
        )

    async def test_hidden_owner_does_not_override_explicit_listing_status_or_probe_public(
        self,
    ):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "0",
            "itemStatus": "0",
        }
        self.assertEqual(await self.service.refresh_listing(product_id), "published")
        self.assertEqual((await self.service.products())[0]["status"], "published")
        self.api.get_item_info.assert_not_awaited()

    async def test_reshelf_readback_error_keeps_reason_and_blocks_replay(self):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        await self.store.execute(
            "UPDATE products SET status='unlisted' WHERE id=?", (product_id,)
        )
        self.api.reshelf_item.side_effect = FishListingUnconfirmed(
            "恢复请求已提交，回查仍为主动下架，请核对平台状态"
        )
        await self.service.schedule_product(product_id, "reshelf")
        await self.service.work_once()
        job = (await self.store.rows("SELECT * FROM jobs"))[0]
        self.assertEqual(job["status"], "uncertain")
        self.assertIn("仍为主动下架", job["error"])
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "reshelf")

    async def test_delist_then_reshelf_uses_original_id(self):
        product_id = await self.service.add_product(self.product())
        await self.service.schedule_product(product_id, "delist")
        await self.service.work_once()
        self.assertEqual((await self.service.products())[0]["status"], "unlisted")
        response = await self.client.post(
            f"/api/products/{product_id}/reshelf", json={}
        )
        self.assertEqual(response.status_code, 200)
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "delist")
        await self.service.work_once()
        self.api.reshelf_item.assert_awaited_once_with("99", quantity=1)
        product = (await self.service.products())[0]
        self.assertEqual((product["status"], product["item_id"]), ("published", "99"))
        self.api.publish_item.assert_not_awaited()
        self.api.save_item_draft.assert_not_awaited()

    async def test_reshelf_timeout_blocks_replay_until_platform_reconciliation(self):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        await self.store.execute(
            "UPDATE products SET status='unlisted' WHERE id=?", (product_id,)
        )
        self.api.reshelf_item.side_effect = TimeoutError()
        await self.service.schedule_product(product_id, "reshelf")
        await self.service.work_once()
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "reshelf")
        job = (await self.store.rows("SELECT * FROM jobs"))[0]
        self.assertEqual(job["status"], "uncertain")
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": -2,
        }
        response = await self.client.post(
            f"/api/jobs/{job['id']}/resolve", json={"decision": "executed"}
        )
        self.assertEqual(response.status_code, 400)
        self.api.get_item_edit_detail.return_value["itemStatus"] = 0
        response = await self.client.post(
            f"/api/jobs/{job['id']}/resolve", json={"decision": "executed"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.service.products())[0]["status"], "published")

    async def test_disabled_delivery_mode_never_submits_reshelf(self):
        product_id = await self.service.add_product(self.product())
        await self.store.execute(
            "UPDATE products SET status='unlisted',mode='disabled' WHERE id=?",
            (product_id,),
        )
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "reshelf")
        self.api.reshelf_item.assert_not_awaited()

    async def test_refresh_listing_tracks_client_delist_but_rejects_wrong_owner(self):
        product_id = await self.service.add_product(self.product(mode="reusable"))
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": -2,
        }
        response = await self.client.post(
            f"/api/products/{product_id}/refresh_listing", json={}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.service.products())[0]["status"], "unlisted")
        self.api.get_item_edit_detail.return_value["userId"] = "22"
        with self.assertRaises(Preconditions):
            await self.service.refresh_listing(product_id)

    async def test_add_and_edit_automatically_sync_same_platform_draft(self):
        body = self.product(mode="reusable").model_dump(mode="json")
        body["item_id"] = ""
        response = await self.client.post("/api/products", json=body)
        self.assertEqual(response.status_code, 200)
        product_id = response.json()["id"]
        self.assertEqual((await self.service.products())[0]["draft_state"], "queued")
        await self.service.work_once()
        product = (await self.service.products())[0]
        self.assertEqual(product["draft_id"], "6673_1792242435499")
        self.assertEqual(product["draft_state"], "synced")
        self.assertEqual(product["status"], "draft")
        self.assertEqual(product["item_id"], "")
        self.assertGreater(product["draft_synced_at"], 0)
        self.assertEqual(self.api.save_item_draft.await_args.kwargs["draft_id"], "")
        self.assertNotIn(body["content"], str(self.api.save_item_draft.await_args))
        body["price"] = "20.50"
        self.assertEqual(
            (
                await self.client.put(f"/api/products/{product_id}", json=body)
            ).status_code,
            200,
        )
        await self.service.work_once()
        self.assertEqual(
            self.api.save_item_draft.await_args.kwargs["draft_id"], product["draft_id"]
        )
        self.assertEqual(
            self.api.save_item_draft.await_args.args[2], {"current_price": 20.5}
        )
        self.assertEqual(self.api.save_item_draft.await_count, 2)
        self.api.publish_item.assert_not_awaited()
        self.api.ship_order.assert_not_awaited()
        self.assertEqual((await self.service.products())[0]["draft_state"], "synced")

    async def test_draft_rejection_can_retry_without_images(self):
        product_id = await self.service.add_product(
            self.product().model_copy(update={"item_id": ""}), sync_draft=True
        )
        self.api.save_item_draft.return_value = {
            "ret": ["FAIL_SYS_SESSION_EXPIRED"],
            "data": {},
        }
        await self.service.work_once()
        self.assertEqual((await self.service.products())[0]["draft_state"], "local")
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "failed"
        )
        self.api.save_item_draft.return_value = {
            "ret": ["SUCCESS"],
            "data": {"draftId": "1_2"},
        }
        await self.service.schedule_product(product_id, "save_draft")
        await self.service.work_once()
        self.assertEqual((await self.service.products())[0]["draft_state"], "synced")
        self.assertEqual(self.api.save_item_draft.await_args.args[0], [])
        self.assertEqual(self.api.save_item_draft.await_args.kwargs["quantity"], 1)

    async def test_ambiguous_draft_save_blocks_duplicate_creation_and_survives_restart(
        self,
    ):
        product_id = await self.service.add_product(
            self.product().model_copy(update={"item_id": ""}), sync_draft=True
        )
        self.api.save_item_draft.side_effect = TimeoutError()
        await self.service.work_once()
        self.assertEqual((await self.service.products())[0]["draft_state"], "uncertain")
        await self.store.initialize()
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "save_draft")
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "publish")
        job = (await self.store.rows("SELECT * FROM jobs"))[0]
        response = await self.client.post(
            f"/api/jobs/{job['id']}/resolve",
            json={"decision": "executed", "draft_id": "1_2"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.service.products())[0]["draft_id"], "1_2")
        self.assertEqual(self.api.save_item_draft.await_count, 1)

    async def test_draft_operations_exclude_edit_publish_and_duplicate_jobs(self):
        data = self.product().model_copy(update={"item_id": ""})
        product_id = await self.service.add_product(data, sync_draft=True)
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "publish")
        self.assertIsNone(await self.service.schedule_product(product_id, "save_draft"))
        response = await self.client.put(
            f"/api/products/{product_id}", json=data.model_dump(mode="json")
        )
        self.assertEqual(response.status_code, 400)
        job = (await self.store.rows("SELECT * FROM jobs"))[0]
        self.assertEqual(
            (await self.client.post(f"/api/jobs/{job['id']}/cancel")).status_code, 200
        )
        self.assertEqual((await self.service.products())[0]["draft_state"], "local")
        await self.service.schedule_product(product_id, "save_draft")
        await self.service.work_once()
        await self.store.execute(
            "UPDATE products SET draft_state='syncing' WHERE id=?", (product_id,)
        )
        await self.store.initialize()
        self.assertEqual((await self.service.products())[0]["draft_state"], "uncertain")

    async def test_missing_draft_id_is_uncertain_and_existing_listing_creates_no_draft(
        self,
    ):
        product_id = await self.service.add_product(self.product(), sync_draft=True)
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        await self.store.execute(
            "UPDATE products SET item_id='',status='draft' WHERE id=?", (product_id,)
        )
        await self.service.schedule_product(product_id, "save_draft")
        self.api.save_item_draft.return_value = {"ret": ["SUCCESS"], "data": {}}
        await self.service.work_once()
        self.assertEqual((await self.service.products())[0]["draft_state"], "uncertain")

    async def test_physical_products_publish_with_selected_category_and_postage(self):
        data = self.product(
            mode="physical",
            product_type="physical",
            quantity=3,
            shipping="fixed",
            postage="8.50",
        ).model_copy(update={"item_id": "", "images": ["test.jpg"]})
        product_id = await self.service.add_product(data)
        self.api.publish_item.return_value = {
            "ret": ["SUCCESS"],
            "data": {"itemId": "888"},
        }
        await self.service.schedule_product(product_id, "publish")
        await self.service.work_once()
        call = self.api.publish_item.await_args
        self.assertEqual(call.args[3], {"choice": "一口价", "post_price": "8.50"})
        self.assertEqual(call.kwargs["quantity"], 3)
        self.assertEqual(call.kwargs["category"]["channelCatId"], "202036301")
        self.assertEqual(
            (await self.store.rows("SELECT status FROM products"))[0]["status"],
            "published",
        )

    async def test_physical_orders_never_enter_virtual_fulfillment(self):
        product_id = await self.service.add_product(
            self.product(mode="physical", product_type="physical")
        )
        await self.store.execute(
            "UPDATE products SET auto_delivery=1 WHERE id=?", (product_id,)
        )
        await self.service.observe("11", self.order(), "chat")
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        with self.assertRaises(Preconditions):
            await self.service.deliver("11:100")
        with self.assertRaises(Preconditions):
            await self.service.reserve("11:100")
        response = await self.client.post("/api/orders/11:100/deliver")
        self.assertEqual(response.status_code, 400)
        response = await self.client.put(
            f"/api/products/{product_id}/automation",
            json={"auto_delivery": True},
        )
        self.assertEqual(response.status_code, 400)
        self.api.ship_order.assert_not_awaited()
        self.bot.send_msg.assert_not_awaited()
        self.assertEqual(
            (await self.client.get("/api/orders")).json()[0]["product_type"], "physical"
        )

    async def test_catalog_requires_auth_and_draft_requires_manual_category(self):
        self.api.get_publish_categories.return_value = [
            {"channelCatId": "2", "catName": "T恤"}
        ]
        response = await self.client.post(
            "/api/catalog/categories",
            json={"account_id": "11", "product_type": "physical", "keyword": "T恤"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["channelCatId"], "2")
        self.assertIn("实物商品", self.api.get_publish_categories.await_args.args[0])
        body = self.product().model_dump(mode="json")
        body.update(item_id="", category=None)
        self.assertEqual(
            (await self.client.post("/api/products", json=body)).status_code, 400
        )
        body["category"] = {"channelCatId": "2", "catName": "电子资料"}
        response = await self.client.post("/api/products", json=body)
        self.assertEqual(response.status_code, 200)
        product_id = response.json()["id"]
        await self.service.work_once()
        body["category"] = {"channelCatId": "3", "catName": "卡券"}
        self.assertEqual(
            (
                await self.client.put(f"/api/products/{product_id}", json=body)
            ).status_code,
            200,
        )
        self.assertIn('"3"', (await self.service.products())[0]["category"])

    async def test_v1_schema_upgrade_preserves_existing_products(self):
        product_id = await self.service.add_product(self.product())

        def downgrade(db):
            for column in (
                "product_type",
                "category",
                "quantity",
                "shipping",
                "postage",
            ):
                db.execute(f"ALTER TABLE products DROP COLUMN {column}")
            db.execute("PRAGMA user_version=1")

        await self.store.call(downgrade, write=True)
        await self.store.initialize()
        row = (await self.service.products())[0]
        self.assertEqual(row["id"], product_id)
        self.assertEqual(row["product_type"], "virtual")
        self.assertEqual(row["category"], "{}")
        await self.store.initialize()
        self.assertEqual(len(await self.service.products()), 1)

    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        await self.store.initialize()
        self.api = AsyncMock()
        self.api.reshelf_item.return_value = {"itemId": "99", "itemStatus": "0"}
        self.api.save_item_draft.return_value = {
            "ret": ["SUCCESS"],
            "data": {"draftId": "6673_1792242435499"},
        }
        self.bot = SimpleNamespace(
            bot_id="11",
            adapter=SimpleNamespace(api=self.api, _active_ws=True),
            send_msg=AsyncMock(return_value=True),
        )
        self.bot.adapter.send = self.bot.send_msg
        self.bot.adapter.api.get_order_conversation = AsyncMock(return_value="")
        self.service = Service(self.store, [self.bot])
        self.app = create_app(self.service, Settings())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)
        await self.app.state.auth.create_user(
            "admin", PASSWORD, "admin", setup_token=self.store.setup_token()
        )
        await self.client.post(
            "/api/auth/login", json={"username": "admin", "password": PASSWORD}
        )
        self.me = (await self.client.get("/api/auth/me")).json()
        self.client.headers["X-CSRF-Token"] = self.me["csrf"]

    def product(self, mode="reusable", **extra):
        extra.setdefault(
            "category", {"catName": "电子资料", "channelCatId": "202036301"}
        )
        return ProductInput(
            title="测试商品",
            description="说明",
            price="9.90",
            account_id="11",
            item_id="99",
            mode=mode,
            content="固定交付内容" if mode == "reusable" else "",
            **extra,
        )

    def order(self, order_id="100", quantity=1):
        return FishOrder(
            order_id=order_id,
            status=OrderStatus.PENDING_SHIP,
            item_id="99",
            buyer_id="22",
            quantity=quantity,
        )

    def platform_order(self, order=None):
        order = order or self.order()
        self.api.get_sold_orders.return_value = {
            "ret": ["SUCCESS"],
            "data": {
                "module": {
                    "items": [
                        {
                            "commonData": {
                                "orderId": order.order_id,
                                "itemId": order.item_id,
                                "orderStatus": "待发货",
                            },
                            "buyerInfoVO": {"buyerId": order.buyer_id},
                            "priceVO": {"buyNum": order.quantity},
                        }
                    ],
                    "nextPage": False,
                }
            },
        }

    async def test_authentication_cookie_csrf_and_origin(self):
        anonymous = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(anonymous.aclose)
        self.assertEqual((await anonymous.get("/api/products")).status_code, 401)
        self.assertEqual(
            (
                await self.client.post(
                    "/api/products", json={}, headers={"X-CSRF-Token": "bad"}
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": PASSWORD},
                    headers={"Origin": "https://evil.example"},
                )
            ).status_code,
            403,
        )
        response = await anonymous.post(
            "/api/auth/login", json={"username": "admin", "password": PASSWORD}
        )
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn(
            "frame-ancestors 'none'", response.headers["content-security-policy"]
        )

    async def test_setup_cannot_be_reopened(self):
        self.assertFalse(
            (await self.client.get("/api/auth/status")).json()["needs_setup"]
        )
        response = await self.client.post(
            "/api/auth/setup",
            json={"username": "another", "password": PASSWORD, "token": "a" * 32},
        )
        self.assertEqual(response.status_code, 401)

    async def test_roles_and_disabled_account_revoke_sessions(self):
        auth = self.app.state.auth
        uid = await auth.create_user("operator", PASSWORD, "operator")
        token, csrf, _ = await auth.login("operator", PASSWORD, "local")
        other = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://localhost",
            cookies={"vg_session": token},
            headers={"X-CSRF-Token": csrf},
        )
        self.addAsyncCleanup(other.aclose)
        self.assertEqual((await other.get("/api/products")).status_code, 200)
        self.assertEqual((await other.get("/api/users")).status_code, 403)
        self.assertEqual(
            (
                await self.client.put(
                    f"/api/users/{uid}", json={"role": "operator", "active": False}
                )
            ).status_code,
            200,
        )
        self.assertEqual((await other.get("/api/products")).status_code, 401)

    async def test_password_change_revokes_all_sessions(self):
        response = await self.client.post(
            "/api/auth/password",
            json={"old_password": PASSWORD, "new_password": "new-strong-password-456"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual((await self.client.get("/api/auth/me")).status_code, 401)

    async def test_login_throttle_persists_in_database(self):
        from myfish.builtin_plugins.fishy.auth import token_hash

        for _ in range(10):
            await self.store.execute(
                "INSERT INTO login_attempts(key,created) VALUES(?,?)",
                (token_hash("user:admin"), time.time()),
            )
        with self.assertRaises(AuthError):
            await self.app.state.auth.login("admin", PASSWORD, "elsewhere")

    async def test_fixed_content_is_encrypted_and_not_in_product_lists(self):
        product_id = await self.service.add_product(
            self.product().model_copy(update={"content": "SECRET-LINK"})
        )
        row = (
            await self.store.rows(
                "SELECT content FROM products WHERE id=?", (product_id,)
            )
        )[0]
        self.assertNotIn("SECRET-LINK", row["content"])
        self.assertEqual(self.store.decrypt(row["content"]), "SECRET-LINK")
        result = await self.client.get("/api/products")
        self.assertNotIn("SECRET-LINK", result.text)
        self.assertNotIn("content", result.json()[0])
        self.assertNotIn("available", result.json()[0])

    async def test_fixed_content_is_reusable_across_concurrent_orders(self):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order("100"), "chat")
        await self.service.observe("11", self.order("101"), "chat")
        results = await asyncio.gather(
            self.service.reserve("11:100"), self.service.reserve("11:101")
        )
        self.assertEqual(
            [row["delivery_state"] for row in results], ["reserved", "reserved"]
        )
        self.assertTrue(
            all(self.store.decrypt(row["content"]) == "固定交付内容" for row in results)
        )

    async def test_fixed_content_is_independent_of_purchase_quantity(self):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order(quantity=2), "chat")
        row = await self.service.reserve("11:100")
        self.assertEqual(row["quantity"], 2)
        self.assertEqual(self.store.decrypt(row["content"]), "固定交付内容")

    async def test_delivery_retry_does_not_send_again_after_message_stage(self):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order(), "chat")
        self.platform_order()
        self.api.ship_order.side_effect = [
            httpx.ReadTimeout("ambiguous"),
            {"ret": ["SUCCESS"]},
        ]
        with self.assertRaises(httpx.ReadTimeout):
            await self.service.deliver("11:100")
        await self.service.deliver("11:100")
        await self.service.deliver("11:100")
        self.bot.send_msg.assert_awaited_once()
        self.assertEqual(self.api.ship_order.await_count, 2)
        self.assertEqual(
            (await self.store.rows("SELECT delivery_state FROM orders"))[0][
                "delivery_state"
            ],
            "completed",
        )

    async def test_ambiguous_message_is_never_blindly_resent(self):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order(), "chat")
        self.platform_order()
        self.bot.send_msg.side_effect = httpx.ReadTimeout("not sure")
        with self.assertRaises(httpx.ReadTimeout):
            await self.service.deliver("11:100")
        with self.assertRaises(Preconditions):
            await self.service.deliver("11:100")
        self.bot.send_msg.assert_awaited_once()
        self.api.ship_order.assert_not_awaited()

    async def test_fixed_content_sale_queues_one_reshelf_without_inventory(self):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order(), "chat")
        await self.service.observe("11", self.order(), "chat")
        jobs = await self.store.rows("SELECT * FROM jobs WHERE kind='reshelf'")
        self.assertEqual(len(jobs), 1)
        self.assertEqual((await self.service.products())[0]["status"], "unlisted")
        self.api.downshelf_item.assert_not_awaited()

    async def test_reusable_delivery_sends_fixed_content(self):
        await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order(), "chat")
        self.platform_order()
        await self.service.deliver("11:100")
        self.assertEqual(self.bot.send_msg.await_args.args[1].text, "固定交付内容")

    async def test_refund_or_missing_conversation_prevents_delivery(self):
        await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order())
        self.platform_order()
        with self.assertRaises(Preconditions):
            await self.service.deliver("11:100")
        self.bot.send_msg.assert_not_awaited()

    async def test_paid_order_recovers_conversation_before_manual_delivery(self):
        await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order())
        self.platform_order()
        self.bot.adapter.api.get_order_conversation.return_value = "recovered-chat"
        await self.service.deliver("11:100")
        self.bot.adapter.api.get_order_conversation.assert_awaited_once_with(
            "100", "22", "99"
        )
        self.assertEqual(self.bot.send_msg.await_args.kwargs["cid"], "recovered-chat")
        self.api.ship_order.assert_awaited_once_with("100")
        row = (await self.store.rows("SELECT * FROM orders"))[0]
        self.assertEqual(row["delivery_state"], "completed")
        self.assertEqual(row["chat_id"], "recovered-chat")

    async def test_conversation_received_during_sync_is_used(self):
        await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order())

        async def sync(*args, **kwargs):
            await self.service.observe("11", self.order(), "incoming-chat")
            return self.order()

        self.service.sync = sync
        await self.service.deliver("11:100")
        self.bot.adapter.api.get_order_conversation.assert_not_awaited()
        self.assertEqual(self.bot.send_msg.await_args.kwargs["cid"], "incoming-chat")

    async def test_unpaid_refund_and_disconnected_orders_never_send_reserved_content(
        self,
    ):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order())
        for fresh, connected in (
            (
                self.order().model_copy(update={"status": OrderStatus.PENDING_PAYMENT}),
                True,
            ),
            (self.order().model_copy(update={"in_refund": True}), True),
            (self.order(), False),
        ):
            self.service.sync = AsyncMock(return_value=fresh)
            self.bot.adapter._active_ws = connected
            with self.assertRaises(Preconditions):
                await self.service.deliver("11:100")
        self.bot.adapter.api.get_order_conversation.assert_not_awaited()
        self.bot.send_msg.assert_not_awaited()
        self.api.ship_order.assert_not_awaited()
        self.assertEqual(
            (await self.store.rows("SELECT delivery_state FROM orders"))[0][
                "delivery_state"
            ],
            "reserved",
        )

    async def test_sent_order_can_finish_shipping_without_a_chat_connection(self):
        await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order())
        await self.service.reserve("11:100")
        await self.store.execute("UPDATE orders SET delivery_state='sent'")
        self.platform_order()
        self.bot.adapter._active_ws = None
        await self.service.deliver("11:100")
        self.bot.send_msg.assert_not_awaited()
        self.bot.adapter.api.get_order_conversation.assert_not_awaited()
        self.api.ship_order.assert_awaited_once_with("100")

    async def test_restart_marks_inflight_jobs_uncertain(self):
        product_id = await self.service.add_product(
            self.product().model_copy(update={"item_id": ""})
        )
        job = await self.service.enqueue("publish", str(product_id), "11")
        await self.store.execute("UPDATE jobs SET status='running' WHERE id=?", (job,))
        await self.store.initialize()
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "uncertain"
        )

    async def test_job_dedup_and_schedule(self):
        first = await self.service.enqueue(
            "deliver", "11:100", "11", time.time() + 3600
        )
        second = await self.service.enqueue("deliver", "11:100", "11")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertFalse(await self.service.work_once())

    async def test_automatic_delivery_enqueues_only_once(self):
        await self.service.add_product(self.product(auto_delivery=True))
        self.platform_order()
        await self.service.observe("11", self.order(), "chat")
        await self.service.observe("11", self.order(), "chat")
        self.assertEqual(
            len(await self.store.rows("SELECT * FROM jobs WHERE kind='deliver'")), 1
        )
        while await self.service.work_once():
            pass
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs WHERE kind='deliver'"))[0][
                "status"
            ],
            "succeeded",
        )
        self.bot.send_msg.assert_awaited_once()

    async def test_concurrent_delivery_cannot_send_content_twice(self):
        await self.service.add_product(self.product(mode="reusable"))
        await self.service.observe("11", self.order(), "chat")
        self.platform_order()
        await asyncio.gather(
            self.service.deliver("11:100"),
            self.service.deliver("11:100"),
            return_exceptions=True,
        )
        self.bot.send_msg.assert_awaited_once()

    async def test_refund_cancels_unsent_delivery_snapshot(self):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order(), "chat")
        self.platform_order()
        self.api.get_sold_orders.return_value["data"]["module"]["items"][0][
            "commonData"
        ]["inRefund"] = "true"
        with self.assertRaises(Preconditions):
            await self.service.deliver("11:100")
        self.assertEqual(
            (await self.store.rows("SELECT delivery_state FROM orders"))[0][
                "delivery_state"
            ],
            "pending",
        )
        self.bot.send_msg.assert_not_awaited()

    async def test_publish_records_platform_id_and_prevents_repeat(self):
        data = self.product().model_copy(update={"item_id": "", "images": ["test.jpg"]})
        product_id = await self.service.add_product(data)
        self.api.publish_item.return_value = {
            "ret": ["SUCCESS"],
            "data": {"itemId": "999"},
        }
        await self.service.schedule_product(product_id, "publish")
        self.assertTrue(await self.service.work_once())
        product = (await self.store.rows("SELECT * FROM products"))[0]
        self.assertEqual(product["item_id"], "999")
        self.assertEqual(product["status"], "published")
        self.assertEqual(self.api.publish_item.await_args.kwargs["quantity"], 1)
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "publish")

    async def test_admin_reconciliation_does_not_expose_delivery_content(self):
        await self.service.add_product(self.product())
        await self.service.observe("11", self.order(), "chat")
        await self.service.reserve("11:100")
        await self.store.execute(
            "UPDATE orders SET delivery_state='sending' WHERE key='11:100'"
        )
        job = await self.service.enqueue("deliver", "11:100", "11")
        await self.store.execute(
            "UPDATE jobs SET status='uncertain' WHERE id=?", (job,)
        )
        response = await self.client.post(
            f"/api/jobs/{job}/resolve", json={"decision": "executed"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (await self.store.rows("SELECT delivery_state FROM orders"))[0][
                "delivery_state"
            ],
            "sent",
        )
        self.assertNotIn("固定交付内容", (await self.client.get("/api/orders")).text)

    async def test_real_plugin_http_server_starts_and_closes(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(
                Settings(port=port, data_dir=Path(directory)),
                SimpleNamespace(adapters=[], dispatch_event=AsyncMock()),
            )
            try:
                with patch(
                    "myfish.builtin_plugins.fishy.StorageManager.get_root_dir",
                    return_value=Path(directory),
                ):
                    await runtime.start()
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False
                ) as client:
                    self.assertTrue(
                        (await client.get("/api/auth/status")).json()["needs_setup"]
                    )
                    response = await client.get("/")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('id="app"', response.text)
            finally:
                await runtime.stop()
            self.assertFalse(runtime.tasks)


if __name__ == "__main__":
    unittest.main()
