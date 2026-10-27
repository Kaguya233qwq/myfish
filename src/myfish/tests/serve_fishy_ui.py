"""浏览器测试专用服务器，所有闲鱼调用均使用内存模拟，不连接真实平台。"""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI
from myfish.builtin_plugins.fishy import Runtime
from myfish.builtin_plugins.fishy.config import Settings


async def main():
    with tempfile.TemporaryDirectory() as directory:
        state = {"status": "待发货"}

        async def sold(**kwargs):
            return {
                "ret": ["SUCCESS"],
                "data": {
                    "module": {
                        "items": [
                            {
                                "commonData": {
                                    "orderId": "100",
                                    "orderStatus": state["status"],
                                    "itemId": "99",
                                },
                                "buyerInfoVO": {
                                    "buyerId": "22",
                                    "userNick": "UI测试买家",
                                },
                                "priceVO": {"buyNum": 1, "totalPrice": "9.90"},
                            }
                        ],
                        "nextPage": False,
                    }
                },
            }

        async def ship(order_id):
            state["status"] = "已发货"
            return {"ret": ["SUCCESS"]}

        api = AsyncMock(spec=FishAPI)
        api.polish_item.return_value = {"ret": ["SUCCESS"], "data": {}}
        listing = {
            "itemId": "99",
            "userId": "11",
            "itemStatus": 0,
            "quantity": "1",
            "itemTextDTO": {
                "title": "UI 测试资料",
                "desc": "原说明",
                "titleDescSeparate": True,
            },
            "itemPriceDTO": {"priceInCent": "990"},
            "itemPostFeeDTO": {
                "canFreeShipping": False,
                "supportFreight": False,
                "onlyTakeSelf": False,
            },
            "itemCatDTO": {"channelCatId": "202036301"},
            "itemAddrDTO": {"city": "测试"},
            "imageInfoDOList": [
                {
                    "url": "https://img.alicdn.com/ui-test.png",
                    "widthSize": 1,
                    "heightSize": 1,
                }
            ],
        }

        async def delist(item_id):
            listing["itemStatus"] = -2

        async def reshelf(item_id, *, quantity, after_sale=False):
            listing["itemStatus"] = 0
            return dict(listing)

        async def edit_detail(item_id, **kwargs):
            return dict(listing)

        api.downshelf_item.side_effect = delist
        api.reshelf_item.side_effect = reshelf
        api.get_item_edit_detail.side_effect = edit_detail

        async def submit_edit(item_id, payload):
            listing.clear()
            listing.update({**payload, "userId": "11"})
            return dict(listing)

        api.submit_item_edit.side_effect = submit_edit

        async def save_draft(*args, draft_id="", **kwargs):
            return {"ret": ["SUCCESS"], "data": {"draftId": draft_id or "54321"}}

        api.save_item_draft.side_effect = save_draft
        api.get_publish_categories.return_value = [
            {"catName": "电子资料", "channelCatId": "202036301", "catId": "50023914"},
            {"catName": "男士T恤", "channelCatId": "126910002", "catId": "50106003"},
        ]
        api.get_sold_orders.side_effect = sold
        api.ship_order.side_effect = ship
        api.publish_item.return_value = {
            "ret": ["SUCCESS"],
            "data": {"itemId": "12345"},
        }
        adapter = FishAdapter(api)
        adapter.bot_id = "11"
        adapter._active_ws = AsyncMock()

        async def confirm_send(raw):
            request = json.loads(raw)
            await adapter._handle_raw_message(
                json.dumps({"code": 200, "headers": request["headers"]})
            )

        adapter._active_ws.send.side_effect = confirm_send
        runtime = Runtime(
            Settings(port=18787, data_dir=Path(directory)),
            SimpleNamespace(adapters=[adapter], dispatch_event=AsyncMock()),
        )
        try:
            with patch(
                "myfish.builtin_plugins.fishy.StorageManager.get_root_dir",
                return_value=Path(directory),
            ):
                await runtime.start()
            failed_job = await runtime.service.store.execute(
                "INSERT INTO jobs(kind,target,account_id,status,run_at,created,updated,error) VALUES('publish','log-test','11','failed',1,1,1,'模拟任务失败')"
            )
            await runtime.service.store.execute(
                "INSERT INTO job_logs(job_id,message,location,created) VALUES(?,?,?,1)",
                (failed_job, "FishAPIError: 模拟任务失败", "api.py:10 in publish_item"),
            )
            finished = asyncio.Event()

            @runtime.server.config.app.post("/__test__/shutdown")
            async def shutdown():
                finished.set()
                return {"ok": True}

            (Path(directory) / "setup.token").write_text(
                "playwright-test-only-setup-token-123456", encoding="utf-8"
            )
            await finished.wait()
        finally:
            await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
