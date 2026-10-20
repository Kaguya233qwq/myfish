import copy
import json
import unittest

from tests import test_fishy as fixtures
from tests.test_fish_listings import snapshot

from myfish.builtin_plugins.fishy.service import Preconditions


class PublishedEditTests(unittest.IsolatedAsyncioTestCase):
    product = fixtures.FishyTests.product

    async def asyncSetUp(self):
        await fixtures.FishyTests.asyncSetUp(self)
        self.product_id = await self.service.add_product(self.product(mode="reusable"))
        self.remote = {
            **snapshot(0),
            "quantity": "1",
            "itemTextDTO": {
                "title": "远端标题",
                "desc": "远端说明",
                "titleDescSeparate": True,
            },
        }

        async def detail(*args, **kwargs):
            return copy.deepcopy(self.remote)

        async def submit(item_id, payload):
            self.remote = {**copy.deepcopy(payload), "userId": "11"}
            return copy.deepcopy(self.remote)

        self.api.get_item_edit_detail.side_effect = detail
        self.api.submit_item_edit.side_effect = submit

    async def open(self, reload=False):
        response = await self.client.post(
            f"/api/products/{self.product_id}/listing-edit/open"
            + ("?reload=true" if reload else ""),
            json={},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_claimed_job_restart_marks_candidate_uncertain_before_execution(self):
        form = await self.open()
        response = await self.save(form, True)
        await self.store.execute(
            "UPDATE jobs SET status='running' WHERE id=?", (response.json()["job_id"],)
        )
        await self.store.initialize()
        self.assertEqual((await self.open())["state"], "uncertain")
        self.api.submit_item_edit.assert_not_awaited()

    async def test_editor_requires_csrf_and_does_not_accept_account_or_category_changes(
        self,
    ):
        form = await self.open()
        form["candidate"]["account_id"] = "22"
        self.assertEqual((await self.save(form)).status_code, 422)
        del self.client.headers["X-CSRF-Token"]
        response = await self.client.post(
            f"/api/products/{self.product_id}/listing-edit/open", json={}
        )
        self.assertEqual(response.status_code, 403)

    async def test_rejected_review_can_be_reconciled_without_losing_candidate(self):
        form = await self.open()
        form["candidate"]["price"] = "33.33"
        self.api.submit_item_edit.side_effect = None
        self.api.submit_item_edit.return_value = {
            **copy.deepcopy(self.remote),
            "itemStatus": "-9",
        }
        await self.save(form, True)
        await self.service.work_once()
        response = await self.client.post(
            f"/api/products/{self.product_id}/listing-edit/resolve",
            json={"decision": "not_executed"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        form = await self.open()
        self.assertEqual(
            (form["state"], form["candidate"]["price"]), ("draft", "33.33")
        )
        self.assertEqual((await self.service.products())[0]["status"], "published")

    async def test_edit_task_blocks_polish_scheduling(self):
        await self.service.polisher.configure(self.product_id, True, 24)
        form = await self.open()
        await self.save(form, True)
        await self.service.polisher.queue_due()
        self.assertEqual(
            [row["kind"] for row in await self.store.rows("SELECT kind FROM jobs")],
            ["edit"],
        )

    async def save(self, form, submit=False):
        return await self.client.put(
            f"/api/products/{self.product_id}/listing-edit",
            json={
                "revision": form["revision"],
                "candidate": form["candidate"],
                "submit": submit,
            },
        )

    async def test_candidate_uses_remote_detail_and_does_not_change_live_archive(self):
        form = await self.open()
        self.assertEqual(form["candidate"]["title"], "远端标题")
        form["candidate"].update(title="新的标题", price="12.34")
        response = await self.save(form)
        self.assertEqual(response.status_code, 200)
        product = (await self.service.products())[0]
        self.assertEqual(product["title"], "测试商品")
        self.assertEqual(product["price"], "9.90")
        self.assertEqual((await self.open())["candidate"]["title"], "新的标题")
        self.api.submit_item_edit.assert_not_awaited()

    async def test_submit_updates_original_item_only_after_matching_readback(self):
        form = await self.open()
        form["candidate"].update(
            title="新的标题", description="新的说明", price="12.34", quantity=3
        )
        self.assertEqual((await self.save(form, True)).status_code, 200)
        self.assertEqual((await self.service.products())[0]["title"], "测试商品")
        await self.service.work_once()
        call = self.api.submit_item_edit.await_args
        self.assertEqual(call.args[0], "99")
        payload = call.args[1]
        self.assertEqual(payload["itemPriceDTO"]["priceInCent"], "1234")
        self.assertEqual(payload["itemCatDTO"], self.remote["itemCatDTO"])
        product = (await self.service.products())[0]
        self.assertEqual(
            (
                product["item_id"],
                product["title"],
                product["price"],
                product["quantity"],
            ),
            ("99", "新的标题", "12.34", 1),
        )
        self.assertEqual(product["edit_state"], "applied")
        self.api.publish_item.assert_not_awaited()
        self.api.save_item_draft.assert_not_awaited()

    async def test_remote_conflict_fails_before_submission_and_can_reload(self):
        form = await self.open()
        await self.save(form, True)
        self.remote["itemPriceDTO"]["priceInCent"] = "1000"
        await self.service.work_once()
        self.api.submit_item_edit.assert_not_awaited()
        self.assertEqual((await self.open())["state"], "failed")
        self.assertEqual((await self.open(True))["candidate"]["price"], "10")

    async def test_review_keeps_old_archive_until_content_matches_and_review_ends(self):
        original = self.api.submit_item_edit.side_effect

        async def review(item_id, payload):
            result = await original(item_id, payload)
            self.remote["itemStatus"] = "-9"
            return {**result, "itemStatus": "-9"}

        self.api.submit_item_edit.side_effect = review
        form = await self.open()
        form["candidate"]["title"] = "审核的新标题"
        await self.save(form, True)
        await self.service.work_once()
        product = (await self.service.products())[0]
        self.assertEqual(
            (product["status"], product["title"], product["edit_state"]),
            ("reviewing", "测试商品", "reviewing"),
        )
        await self.service.refresh_listing(self.product_id)
        self.remote["itemStatus"] = "0"
        await self.service.refresh_listing(self.product_id)
        self.assertEqual((await self.service.products())[0]["title"], "审核的新标题")
        self.assertEqual((await self.open())["state"], "applied")

    async def test_uncertain_edit_survives_restart_and_is_reconciled_without_resending(
        self,
    ):
        form = await self.open()
        form["candidate"]["price"] = "15.00"
        await self.save(form, True)
        self.api.submit_item_edit.side_effect = TimeoutError()
        await self.service.work_once()
        await self.store.initialize()
        self.assertEqual((await self.open())["state"], "uncertain")
        for action in ("delist", "polish", "reshelf"):
            with self.assertRaises(Preconditions):
                await self.service.schedule_product(self.product_id, action)
        row = (await self.store.rows("SELECT expected FROM product_edits"))[0]
        self.remote = {**json.loads(row["expected"]), "userId": "11"}
        await self.service.refresh_listing(self.product_id)
        self.assertEqual((await self.service.products())[0]["price"], "15.00")
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "succeeded"
        )
        self.assertEqual(self.api.submit_item_edit.await_count, 1)

    async def test_stale_revision_and_invalid_or_duplicate_images_are_rejected(self):
        form = await self.open()
        self.assertEqual((await self.save(form)).status_code, 200)
        self.assertEqual((await self.save(form)).status_code, 400)
        form = await self.open()
        form["candidate"]["images"] = ["../../auth.json"]
        self.assertEqual((await self.save(form)).status_code, 400)
        form["candidate"]["images"] = ["remote:999"]
        self.assertEqual((await self.save(form)).status_code, 400)
        form["candidate"]["images"] = ["remote:0", "remote:0"]
        self.assertEqual((await self.save(form)).status_code, 422)

    async def test_cancelled_edit_leaves_archive_and_candidate_intact(self):
        form = await self.open()
        form["candidate"]["title"] = "取消候选"
        response = await self.save(form, True)
        job_id = response.json()["job_id"]
        self.assertEqual(
            (await self.client.post(f"/api/jobs/{job_id}/cancel")).status_code, 200
        )
        self.assertEqual((await self.open())["candidate"]["title"], "取消候选")
        self.assertEqual((await self.open())["state"], "draft")
        self.assertEqual((await self.service.products())[0]["title"], "测试商品")
        self.assertFalse(await self.service.work_once())

    async def test_virtual_edit_uses_one_unit_and_upload_retains_remote_image(
        self,
    ):
        media = self.store.directory / "media"
        media.mkdir()
        filename = "a" * 32 + ".jpg"
        (media / filename).write_bytes(b"test")
        self.api.upload_media.return_value = {
            "object": {"url": "https://img.alicdn.com/new.jpg", "pix": "20x30"}
        }
        form = await self.open()
        form["candidate"]["images"].append(filename)
        form["candidate"]["quantity"] = 999
        await self.save(form, True)
        await self.service.work_once()
        payload = self.api.submit_item_edit.await_args.args[1]
        self.assertEqual(payload["quantity"], "1")
        self.assertEqual(len(payload["imageInfoDOList"]), 2)
        self.assertEqual(
            payload["imageInfoDOList"][0]["url"], "https://example.com/image.jpg"
        )

    async def test_foreign_owner_and_unlisted_state_never_submit_edit(self):
        form = await self.open()
        await self.save(form, True)
        self.remote["userId"] = "22"
        await self.service.work_once()
        self.api.submit_item_edit.assert_not_awaited()
        self.remote["userId"] = "11"
        self.remote["itemStatus"] = "-2"
        response = await self.client.post(
            f"/api/products/{self.product_id}/listing-edit/open?reload=true", json={}
        )
        self.assertEqual(response.status_code, 400)

    async def test_readback_mismatch_cannot_be_resolved_as_executed(self):
        form = await self.open()
        form["candidate"]["price"] = "11.11"
        self.api.submit_item_edit.side_effect = None
        self.api.submit_item_edit.return_value = copy.deepcopy(self.remote)
        response = await self.save(form, True)
        await self.service.work_once()
        job_id = response.json()["job_id"]
        result = await self.client.post(
            f"/api/jobs/{job_id}/resolve", json={"decision": "executed"}
        )
        self.assertEqual(result.status_code, 400)
        result = await self.client.post(
            f"/api/jobs/{job_id}/resolve", json={"decision": "not_executed"}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual((await self.open())["state"], "draft")

    async def test_physical_shipping_uses_cents_and_preserves_category_and_address(
        self,
    ):
        await self.store.execute(
            "UPDATE products SET product_type='physical',mode='physical' WHERE id=?",
            (self.product_id,),
        )
        form = await self.open()
        form["candidate"].update(shipping="fixed", postage="8.55", price="20.01")
        await self.save(form, True)
        await self.service.work_once()
        payload = self.api.submit_item_edit.await_args.args[1]
        self.assertEqual(payload["itemPostFeeDTO"]["postPriceInCent"], "855")
        self.assertEqual(payload["itemPriceDTO"]["priceInCent"], "2001")
        self.assertEqual(payload["itemAddrDTO"], {"city": "测试"})

    async def test_invalid_readback_fields_after_submission_are_uncertain_not_retryable(
        self,
    ):
        original = self.api.submit_item_edit.side_effect

        async def submit(item_id, payload):
            result = await original(item_id, payload)
            result["itemPostFeeDTO"]["supportFreight"] = "unknown"
            return result

        self.api.submit_item_edit.side_effect = submit
        await self.save(await self.open(), True)
        await self.service.work_once()
        self.assertEqual((await self.open())["state"], "uncertain")
        self.assertEqual(
            (await self.store.rows("SELECT status FROM jobs"))[0]["status"], "uncertain"
        )
