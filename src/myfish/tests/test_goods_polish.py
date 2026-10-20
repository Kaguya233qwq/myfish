import asyncio
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

if not importlib.util.find_spec("fastapi") or not importlib.util.find_spec("uvicorn"):
    raise unittest.SkipTest("商品擦亮测试需要 fishy 可选依赖")
from myfish.adapters.fish.api import FishPolishError
from myfish.builtin_plugins.fishy.polish import (
    ALREADY_POLISHED_MESSAGE,
    INELIGIBLE_ERRORS,
)
from myfish.builtin_plugins.fishy.service import Preconditions, Service
from myfish.builtin_plugins.fishy.settings import Settings
from myfish.builtin_plugins.fishy.store import Store
from myfish.builtin_plugins.fishy.web import ProductInput, create_app


class GoodsPolishTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_never_waiting_and_paused_states_clear_old_ineligible_errors(
        self,
    ):
        product_id = await self.product(enabled=True)
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": "0",
        }
        for state in ("never", "waiting", "paused"):
            for enabled in (0, 1):
                for error in INELIGIBLE_ERRORS:
                    with self.subTest(state=state, enabled=enabled, error=error):
                        await self.store.execute(
                            "UPDATE products SET polish_status=?,auto_polish=?,polish_error=?,polish_next_at=? WHERE id=?",
                            (state, enabled, error, time.time() + 21600, product_id),
                        )
                        await self.service.refresh_listing(product_id)
                        row = await self.row(product_id)
                        self.assertEqual(row["auto_polish"], enabled)
                        self.assertEqual(row["polish_error"], "")
                        self.assertEqual(
                            row["polish_status"], "waiting" if enabled else "paused"
                        )
                        self.assertEqual(row["polish_next_at"], 0)

    async def test_configure_clears_legacy_never_error_without_waiting_for_next_polish(
        self,
    ):
        product_id = await self.product(enabled=True)
        for enabled in (False, True):
            await self.store.execute(
                "UPDATE products SET polish_status='never',polish_error=?,polish_next_at=? WHERE id=?",
                (INELIGIBLE_ERRORS[0], time.time() + 21600, product_id),
            )
            await self.service.polisher.configure(product_id, enabled, 24)
            row = await self.row(product_id)
            self.assertEqual(row["polish_error"], "")
            self.assertEqual(row["auto_polish"], enabled)

    async def test_unrelated_error_in_never_state_is_not_removed_by_listing_sync(self):
        product_id = await self.product(enabled=True)
        await self.store.execute(
            "UPDATE products SET polish_status='never',polish_error='other failure' WHERE id=?",
            (product_id,),
        )
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": "0",
        }
        await self.service.refresh_listing(product_id)
        self.assertEqual((await self.row(product_id))["polish_error"], "other failure")

    async def test_claimed_restart_backs_off_account_and_never_replays(self):
        first = await self.product(enabled=True)
        await self.product("100", enabled=True)
        await self.service.polisher.queue_due()
        await self.store.execute(
            "UPDATE jobs SET status='running' WHERE target=?", (str(first),)
        )
        await self.store.initialize()
        self.assertEqual((await self.row(first))["polish_status"], "uncertain")
        gate = (await self.store.rows("SELECT next_attempt FROM polish_accounts"))[0][
            "next_attempt"
        ]
        self.assertGreater(gate, time.time() + 3500)
        self.assertFalse(await self.service.work_once())
        self.api.polish_item.assert_not_awaited()

    async def test_v3_upgrade_preserves_products_and_defaults_auto_off(self):
        product_id = await self.product()

        def downgrade(db):
            db.execute("DROP INDEX polish_due")
            for column in (
                "auto_polish",
                "polish_interval_hours",
                "polish_next_at",
                "polish_last_at",
                "polish_status",
                "polish_error",
            ):
                db.execute(f"ALTER TABLE products DROP COLUMN {column}")
            db.execute("DROP TABLE polish_accounts")
            db.execute("PRAGMA user_version=3")

        await self.store.call(downgrade, write=True)
        await self.store.initialize()
        row = await self.row(product_id)
        self.assertEqual(row["item_id"], "99")
        self.assertEqual(row["auto_polish"], 0)
        self.assertEqual(row["polish_interval_hours"], 24)

    async def test_rate_limit_backs_off_and_claimed_job_restart_requires_review(self):
        product_id = await self.product(enabled=True)
        self.api.polish_item.side_effect = FishPolishError(["FAIL_SYS_FLOW_LIMIT"])
        await self.service.polisher.queue_due()
        await self.service.work_once()
        gate = (await self.store.rows("SELECT next_attempt FROM polish_accounts"))[0][
            "next_attempt"
        ]
        self.assertGreater(gate, time.time() + 21000)
        await self.store.execute(
            "UPDATE products SET polish_status='queued',polish_next_at=0 WHERE id=?",
            (product_id,),
        )
        await self.store.execute("UPDATE jobs SET status='running'")
        await self.store.initialize()
        self.assertEqual((await self.row(product_id))["polish_status"], "uncertain")
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "polish")

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name))
        await self.store.initialize()
        self.api = AsyncMock()
        self.api.polish_item.return_value = {"ret": ["SUCCESS"], "data": {}}
        self.bot = SimpleNamespace(
            bot_id="11", adapter=SimpleNamespace(api=self.api, _active_ws=True)
        )
        self.service = Service(self.store, [self.bot])

    async def product(self, item_id="99", enabled=False):
        return await self.service.add_product(
            ProductInput(
                title="擦亮测试",
                price="9.90",
                account_id="11",
                item_id=item_id,
                mode="physical",
                product_type="physical",
                auto_polish=enabled,
            )
        )

    async def row(self, product_id):
        return (
            await self.store.rows("SELECT * FROM products WHERE id=?", (product_id,))
        )[0]

    async def test_opt_in_auto_queue_only_published_items_and_deduplicates(self):
        product_id = await self.product()
        await self.product("", enabled=True)
        await self.service.polisher.queue_due()
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        await self.service.polisher.configure(product_id, True, 24)
        await self.service.polisher.queue_due()
        await self.service.polisher.queue_due()
        self.assertEqual(len(await self.store.rows("SELECT * FROM jobs")), 1)
        before = time.time()
        await self.service.work_once()
        row = await self.row(product_id)
        self.assertEqual(row["polish_status"], "polished")
        self.assertGreaterEqual(row["polish_last_at"], before)
        self.assertGreaterEqual(row["polish_next_at"], before + 86400)
        await self.service.polisher.queue_due()
        self.api.polish_item.assert_awaited_once_with("99")

    async def test_daily_duplicate_is_skipped_and_manual_cannot_bypass_cooldown(self):
        product_id = await self.product(enabled=True)
        self.api.polish_item.return_value = {
            "ret": ["FAIL_BIZ_ITEM_ERROR_POLISH_DUPLICATE::一天只能擦亮一次"],
            "data": {},
        }
        await self.service.polisher.queue_due()
        await self.service.work_once()
        row = await self.row(product_id)
        self.assertEqual(row["polish_status"], "already_polished")
        self.assertEqual(row["polish_last_at"], 0)
        job = (await self.store.rows("SELECT * FROM jobs"))[0]
        self.assertEqual(job["status"], "skipped")
        self.assertEqual(job["error"], ALREADY_POLISHED_MESSAGE)
        await self.store.initialize()
        with self.assertRaisesRegex(Preconditions, "平台已确认.*下次可擦亮时间"):
            await self.service.schedule_product(product_id, "polish")
        self.api.polish_item.assert_awaited_once()

    async def test_manual_click_reuses_queued_and_running_auto_polish(self):
        product_id = await self.product(enabled=True)
        await self.service.polisher.queue_due()
        original = (await self.store.rows("SELECT * FROM jobs"))[0]
        self.assertEqual(
            await self.service.schedule_product(product_id, "polish"), original["id"]
        )
        started, finish = asyncio.Event(), asyncio.Event()

        async def polish(item_id):
            started.set()
            await finish.wait()
            return {"ret": ["SUCCESS"], "data": {}}

        self.api.polish_item.side_effect = polish
        worker = asyncio.create_task(self.service.work_once())
        try:
            await asyncio.wait_for(started.wait(), 5)
            self.assertEqual(
                await self.service.schedule_product(product_id, "polish"),
                original["id"],
            )
        finally:
            finish.set()
            await worker
        jobs = await self.store.rows("SELECT * FROM jobs")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "succeeded")
        self.assertEqual(jobs[0]["error"], "")
        self.api.polish_item.assert_awaited_once_with("99")
        self.assertGreater((await self.row(product_id))["polish_last_at"], 0)
        with self.assertRaisesRegex(Preconditions, "平台已确认.*下次可擦亮时间"):
            await self.service.schedule_product(product_id, "polish")

    async def test_local_cooldown_skip_explains_reason_without_platform_request(self):
        product_id = await self.product()
        await self.service.schedule_product(product_id, "polish")
        await self.store.execute(
            "UPDATE products SET polish_next_at=? WHERE id=?",
            (time.time() + 3600, product_id),
        )
        await self.service.work_once()
        job = (await self.store.rows("SELECT * FROM jobs"))[0]
        self.assertEqual(job["status"], "skipped")
        self.assertIn("尚未到下次擦亮时间：", job["error"])
        self.assertEqual((await self.row(product_id))["polish_last_at"], 0)
        self.api.polish_item.assert_not_awaited()

    async def test_success_response_takes_priority_over_duplicate_code(self):
        product_id = await self.product()
        self.api.polish_item.return_value = {
            "ret": ["SUCCESS::成功", "FAIL_BIZ_IDLEITEM_POLISH_AGAIN"],
            "data": {},
        }
        await self.service.schedule_product(product_id, "polish")
        await self.service.work_once()
        row = await self.row(product_id)
        self.assertEqual(row["polish_status"], "polished")
        self.assertGreater(row["polish_last_at"], 0)
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "succeeded"
        )

    async def test_platform_polish_again_marks_confirmed_and_clears_stale_error(self):
        product_id = await self.product(enabled=True)
        await self.store.execute(
            "UPDATE products SET polish_error='旧失败信息' WHERE id=?", (product_id,)
        )
        self.api.polish_item.return_value = {
            "ret": ["FAIL_BIZ_IDLEITEM_POLISH_AGAIN::宝贝已经擦亮过了"],
            "data": {},
        }
        before = time.time()
        await self.service.polisher.queue_due()
        await self.service.work_once()
        row = await self.row(product_id)
        self.assertEqual(row["polish_status"], "already_polished")
        self.assertEqual(row["polish_error"], "")
        self.assertEqual(row["polish_last_at"], 0)
        self.assertGreaterEqual(row["polish_next_at"], before + 86400)
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "skipped"
        )

    async def test_account_spacing_does_not_block_other_job_types(self):
        first = await self.product("99", enabled=True)
        second = await self.product("100", enabled=True)
        await self.service.polisher.queue_due()
        await self.service.work_once()
        self.assertFalse(await self.service.work_once())
        self.assertEqual(self.api.polish_item.await_count, 1)
        await self.service.schedule_product(first, "delist")
        await self.service.work_once()
        self.api.downshelf_item.assert_awaited_once_with("99")
        await self.store.execute("UPDATE polish_accounts SET next_attempt=0")
        await self.service.work_once()
        self.assertEqual((await self.row(second))["polish_status"], "polished")

    async def test_disable_cancels_pending_and_offline_accounts_are_not_queued(self):
        product_id = await self.product(enabled=True)
        self.bot.adapter._active_ws = False
        await self.service.polisher.queue_due()
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        self.bot.adapter._active_ws = True
        await self.service.polisher.queue_due()
        await self.service.polisher.configure(product_id, False, 24)
        self.assertFalse(await self.service.work_once())
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "cancelled"
        )
        self.api.polish_item.assert_not_awaited()

    async def test_delist_supersedes_pending_polish(self):
        product_id = await self.product(enabled=True)
        await self.service.polisher.queue_due()
        await self.service.schedule_product(product_id, "delist")
        await self.service.work_once()
        self.assertEqual((await self.row(product_id))["status"], "unlisted")
        self.api.polish_item.assert_not_awaited()

    async def test_login_failure_backs_off_whole_account(self):
        product_id = await self.product(enabled=True)
        await self.product("100", enabled=True)
        self.api.polish_item.side_effect = FishPolishError(["FAIL_SYS_SESSION_EXPIRED"])
        await self.service.polisher.queue_due()
        await self.service.work_once()
        self.assertFalse(await self.service.work_once())
        self.assertEqual((await self.row(product_id))["polish_status"], "error")
        gate = (await self.store.rows("SELECT next_attempt FROM polish_accounts"))[0][
            "next_attempt"
        ]
        self.assertGreater(gate, time.time() + 3500)
        self.api.polish_item.assert_awaited_once()

    async def test_ineligible_item_pauses_auto_without_deleting_product(self):
        product_id = await self.product(enabled=True)
        self.api.polish_item.side_effect = FishPolishError(
            ["FAIL_BIZ_UNSUPPORTED_ITEM_STATUS"]
        )
        await self.service.polisher.queue_due()
        await self.service.work_once()
        row = await self.row(product_id)
        self.assertEqual(row["auto_polish"], 1)
        self.assertEqual(row["polish_status"], "ineligible")
        self.assertEqual(row["status"], "published")
        await self.service.polisher.queue_due()
        self.assertEqual(len(await self.store.rows("SELECT * FROM jobs")), 1)

    async def test_app_relisting_clears_stale_error_and_resumes_enabled_polishing(self):
        product_id = await self.product(enabled=True)
        self.api.polish_item.side_effect = FishPolishError(
            ["FAIL_BIZ_UNSUPPORTED_ITEM_STATUS"]
        )
        await self.service.polisher.queue_due()
        await self.service.work_once()
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": "0",
        }
        gate = (await self.store.rows("SELECT next_attempt FROM polish_accounts"))[0][
            "next_attempt"
        ]
        await self.service.refresh_listing(product_id)
        row = await self.row(product_id)
        self.assertEqual(
            (row["auto_polish"], row["polish_status"], row["polish_error"]),
            (1, "waiting", ""),
        )
        self.assertEqual(
            (await self.store.rows("SELECT next_attempt FROM polish_accounts"))[0][
                "next_attempt"
            ],
            gate,
        )
        self.api.polish_item.side_effect = None
        await self.service.polisher.queue_due()
        self.assertFalse(await self.service.work_once())
        await self.store.execute("UPDATE polish_accounts SET next_attempt=0")
        self.assertTrue(await self.service.work_once())
        self.assertEqual((await self.row(product_id))["polish_status"], "polished")

    async def test_legacy_disabled_setting_stays_off_when_error_is_cleared(self):
        product_id = await self.product()
        await self.store.execute(
            "UPDATE products SET polish_status='ineligible',polish_error='旧暂停信息',polish_next_at=? WHERE id=?",
            (time.time() + 21600, product_id),
        )
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": "0",
        }
        await self.service.refresh_listing(product_id)
        row = await self.row(product_id)
        self.assertEqual(
            (row["auto_polish"], row["polish_status"], row["polish_error"]),
            (0, "paused", ""),
        )
        await self.service.polisher.queue_due()
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        await self.service.polisher.configure(product_id, True, 24)
        await self.service.polisher.queue_due()
        self.assertEqual(len(await self.store.rows("SELECT * FROM jobs")), 1)

    async def test_offline_review_and_foreign_listing_do_not_clear_pause(self):
        product_id = await self.product(enabled=True)
        await self.store.execute(
            "UPDATE products SET polish_status='ineligible',polish_error='暂停原因' WHERE id=?",
            (product_id,),
        )
        for status in ("-2", "-9"):
            self.api.get_item_edit_detail.return_value = {
                "itemId": "99",
                "userId": "11",
                "itemStatus": status,
            }
            await self.service.refresh_listing(product_id)
            self.assertEqual((await self.row(product_id))["polish_error"], "暂停原因")
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "22",
            "itemStatus": "0",
        }
        with self.assertRaises(Preconditions):
            await self.service.refresh_listing(product_id)
        self.assertEqual((await self.row(product_id))["polish_status"], "ineligible")

    async def test_recovery_preserves_success_cooldown_and_other_error_states(self):
        product_id = await self.product(enabled=True)
        last_at = time.time()
        await self.store.execute(
            "UPDATE products SET polish_status='ineligible',polish_error='暂停原因',polish_last_at=?,polish_interval_hours=48 WHERE id=?",
            (last_at, product_id),
        )
        self.api.get_item_edit_detail.return_value = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": "0",
        }
        await self.service.refresh_listing(product_id)
        self.assertEqual(
            (await self.row(product_id))["polish_next_at"], last_at + 48 * 3600
        )
        await self.service.polisher.queue_due()
        self.assertEqual(await self.store.rows("SELECT * FROM jobs"), [])
        for state in ("uncertain", "error", "already_polished"):
            await self.store.execute(
                "UPDATE products SET polish_status=?,polish_error='保留信息' WHERE id=?",
                (state, product_id),
            )
            await self.service.refresh_listing(product_id)
            row = await self.row(product_id)
            self.assertEqual(
                (row["polish_status"], row["polish_error"]), (state, "保留信息")
            )

    async def test_plugin_reshelf_clears_ineligible_error_only_after_confirmed_online(
        self,
    ):
        product_id = await self.product(enabled=True)
        await self.store.execute(
            "UPDATE products SET status='unlisted',polish_status='ineligible',polish_error='暂停原因' WHERE id=?",
            (product_id,),
        )
        self.api.reshelf_item.return_value = {"itemId": "99", "itemStatus": "0"}
        await self.service.schedule_product(product_id, "reshelf")
        await self.service.work_once()
        row = await self.row(product_id)
        self.assertEqual(
            (row["polish_status"], row["polish_error"], row["auto_polish"]),
            ("waiting", "", 1),
        )

    async def test_explicit_reenable_or_disable_removes_ineligible_error(self):
        product_id = await self.product(enabled=True)
        for enabled, expected in ((False, "paused"), (True, "waiting")):
            await self.store.execute(
                "UPDATE products SET polish_status='ineligible',polish_error='旧提示' WHERE id=?",
                (product_id,),
            )
            await self.service.polisher.configure(product_id, enabled, 24)
            row = await self.row(product_id)
            self.assertEqual(
                (row["polish_status"], row["polish_error"]), (expected, "")
            )

    async def test_timeout_and_restart_preserve_uncertainty(self):
        product_id = await self.product(enabled=True)
        self.api.polish_item.side_effect = TimeoutError()
        await self.service.polisher.queue_due()
        await self.service.work_once()
        await self.store.initialize()
        self.assertEqual((await self.row(product_id))["polish_status"], "uncertain")
        await self.service.polisher.queue_due()
        with self.assertRaises(Preconditions):
            await self.service.schedule_product(product_id, "polish")
        self.assertEqual(len(await self.store.rows("SELECT * FROM jobs")), 1)
        self.api.polish_item.assert_awaited_once()

    async def test_http_permissions_interval_validation_and_configuration(self):
        product_id = await self.product()
        app = create_app(self.service, Settings())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            url = f"/api/products/{product_id}/polish-settings"
            self.assertEqual(
                (
                    await client.put(url, json={"enabled": True, "interval_hours": 24})
                ).status_code,
                401,
            )
            await app.state.auth.create_user(
                "admin",
                "test-strong-password-123",
                "admin",
                setup_token=self.store.setup_token(),
            )
            await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "test-strong-password-123"},
            )
            me = (await client.get("/api/auth/me")).json()
            client.headers["X-CSRF-Token"] = me["csrf"]
            self.assertEqual(
                (
                    await client.put(url, json={"enabled": True, "interval_hours": 1})
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.put(url, json={"enabled": True, "interval_hours": 48})
                ).status_code,
                200,
            )
            self.assertEqual((await self.row(product_id))["polish_interval_hours"], 48)
            self.assertEqual(
                (
                    await client.post(f"/api/products/{product_id}/polish", json={})
                ).status_code,
                200,
            )
            await self.service.work_once()
            self.assertEqual(
                (
                    await client.post(f"/api/products/{product_id}/polish", json={})
                ).status_code,
                400,
            )
