"""验证真实订单和会话读取；交付写入使用临时数据库及 Mock。"""

import argparse
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import websockets
from loguru import logger

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI
from myfish.adapters.fish.orders import parse_sold_orders
from myfish.builtin_plugins.fishy.service import Service
from myfish.builtin_plugins.fishy.store import Store
from myfish.builtin_plugins.fishy.web import ProductInput


async def check(auth: Path, database: Path):
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM orders WHERE delivery_state='pending' ORDER BY updated DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise ValueError("没有待交付订单")
    order = dict(row)
    cookies = json.loads(auth.read_text(encoding="utf-8"))
    if str(cookies.get("unb")) != order["account_id"]:
        raise ValueError("凭证与订单账号不匹配")
    with patch("myfish.adapters.fish.api.save_local_auth"):
        async with FishAPI(cookies) as api:
            orders, _ = parse_sold_orders(await api.get_sold_orders())
            fresh = next(
                (item for item in orders if item.order_id == order["order_id"]), None
            )
            if not fresh or not fresh.can_ship:
                raise ValueError("订单不处于待发货状态")
            adapter = FishAdapter(api)
            adapter.bot_id = order["account_id"]
            adapter.send = AsyncMock(return_value=True)
            api.ship_order = AsyncMock()
            async with websockets.connect(
                adapter.base_url, additional_headers=adapter._get_headers()
            ) as ws:
                adapter._active_ws = ws
                if not await adapter._init_connection():
                    raise ValueError("WebSocket 初始化失败")

                async def receive():
                    async for raw in ws:
                        await adapter._handle_raw_message(raw)

                receiver = asyncio.create_task(receive())
                heartbeat = asyncio.create_task(adapter._heart_beat_loop())
                try:
                    with tempfile.TemporaryDirectory() as directory:
                        store = Store(Path(directory))
                        await store.initialize()
                        service = Service(
                            store,
                            [
                                SimpleNamespace(
                                    bot_id=order["account_id"], adapter=adapter
                                )
                            ],
                        )
                        await service.add_product(
                            ProductInput(
                                title="交付验证",
                                description="交付验证",
                                price="1",
                                account_id=order["account_id"],
                                item_id=fresh.item_id,
                                mode="reusable",
                                content="TEST-CONTENT",
                            )
                        )
                        await service.observe(order["account_id"], fresh)
                        key = f"{order['account_id']}:{fresh.order_id}"
                        await service.deliver(key)
                        result = (
                            await store.rows("SELECT * FROM orders WHERE key=?", (key,))
                        )[0]
                        assert result["delivery_state"] == "completed"
                        assert result["chat_id"]
                        adapter.send.assert_awaited_once()
                        api.ship_order.assert_awaited_once_with(fresh.order_id)
                        print(
                            json.dumps(
                                {
                                    "paid_order_verified": True,
                                    "conversation_recovered": True,
                                    "delivery_flow": "PASS",
                                    "chat_send": "MOCK",
                                    "platform_ship": "MOCK",
                                    "database": "TEMPORARY",
                                }
                            )
                        )
                finally:
                    receiver.cancel()
                    heartbeat.cancel()
                    await asyncio.gather(receiver, heartbeat, return_exceptions=True)
                    adapter._active_ws = None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    logger.remove()
    asyncio.run(check(args.auth, args.database))
