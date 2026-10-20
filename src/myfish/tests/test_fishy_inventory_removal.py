"""库存功能移除和固定交付升级回归。"""

import unittest

from myfish.builtin_plugins.fishy.service import Preconditions
from myfish.tests import test_fishy as fixtures


class InventoryRemovalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FishyTests.asyncSetUp
    product = fixtures.FishyTests.product
    order = fixtures.FishyTests.order

    async def legacy_data(self):
        product_id = await self.service.add_product(self.product())
        for order_id in ("100", "101"):
            await self.service.observe("11", self.order(order_id), "chat")
        self.original = self.store.encrypt("legacy-card-secret")
        await self.store.execute(
            "CREATE TABLE stock(id INTEGER PRIMARY KEY,product_id INTEGER,content TEXT,"
            "fingerprint TEXT,state TEXT,order_key TEXT)"
        )
        await self.store.execute(
            "INSERT INTO stock VALUES(1,?,?,'legacy','reserved','11:100')",
            (product_id, self.original),
        )
        await self.store.execute(
            "UPDATE products SET mode='stock',auto_delivery=1 WHERE id=?", (product_id,)
        )
        await self.store.execute("UPDATE orders SET content=?", (self.original,))
        await self.store.execute(
            "UPDATE orders SET delivery_state='sending' WHERE key='11:101'"
        )
        await self.service.enqueue("deliver", "11:100", "11")
        running = await self.service.enqueue("deliver", "11:101", "11")
        await self.store.execute(
            "UPDATE jobs SET status='running' WHERE id=?", (running,)
        )
        await self.store.execute("PRAGMA user_version=8")
        await self.store.initialize()
        return product_id

    async def test_inventory_mode_and_import_endpoint_are_removed(self):
        body = self.product().model_dump(mode="json")
        body["mode"] = "stock"
        response = await self.client.post("/api/products", json=body)
        self.assertEqual(response.status_code, 422)
        response = await self.client.post(
            "/api/products/1/stock", json={"lines": ["A"]}
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(hasattr(self.service, "import_stock"))

    async def test_new_database_has_no_inventory_table_or_metrics(self):
        tables = await self.store.rows(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        self.assertNotIn("stock", {row["name"] for row in tables})
        dashboard = (await self.client.get("/api/dashboard")).json()
        self.assertNotIn("stock", dashboard)
        self.assertEqual(dashboard["pending_jobs"], 0)

    async def test_upgrade_preserves_old_secrets_and_stops_pending_work(self):
        product_id = await self.legacy_data()
        row = (await self.service.products())[0]
        self.assertEqual((row["mode"], row["auto_delivery"]), ("disabled", 0))
        self.assertNotIn("available", row)
        secret = (await self.store.rows("SELECT content FROM stock"))[0]["content"]
        self.assertEqual(secret, self.original)
        self.assertEqual(self.store.decrypt(secret), "legacy-card-secret")
        jobs = await self.store.rows("SELECT kind,target,status FROM jobs")
        self.assertTrue(any(row["status"] == "uncertain" for row in jobs))
        self.assertFalse(any(row["status"] == "pending" for row in jobs))
        await self.service.observe("11", self.order("102"), "chat")
        self.assertFalse(await self.service.work_once())
        with self.assertRaises(Preconditions):
            await self.service.deliver("11:100")
        self.bot.send_msg.assert_not_awaited()
        self.api.reshelf_item.assert_not_awaited()
        self.assertEqual(row["id"], product_id)

    async def test_old_mode_cannot_be_enabled_or_delivered_without_fixed_content(self):
        product_id = await self.legacy_data()
        response = await self.client.put(
            f"/api/products/{product_id}/automation", json={"auto_delivery": True}
        )
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/api/orders/11:100/deliver")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(await self.service.delivery_content(product_id), "")

    async def test_explicit_fixed_content_replaces_only_unsent_snapshots(self):
        product_id = await self.legacy_data()
        response = await self.client.put(
            f"/api/products/{product_id}/delivery-content",
            json={"content": "https://example.com/download"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reserved_updated"], 1)
        orders = await self.store.rows("SELECT * FROM orders ORDER BY order_id")
        self.assertEqual(
            self.store.decrypt(orders[0]["content"]), "https://example.com/download"
        )
        self.assertEqual(orders[1]["content"], self.original)
        await self.store.initialize()
        row = (await self.service.products())[0]
        self.assertEqual((row["mode"], row["auto_delivery"]), ("reusable", 0))
        response = await self.client.put(
            f"/api/products/{product_id}/automation", json={"auto_delivery": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (await self.store.rows("SELECT content FROM stock"))[0]["content"],
            self.original,
        )


if __name__ == "__main__":
    unittest.main()
