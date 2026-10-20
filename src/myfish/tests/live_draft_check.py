"""显式真实写入检查：创建一份有测试标记的草稿并更新，不调用商品上架接口。"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from myfish.adapters.fish.api import FishAPI


async def check(auth: Path, draft_id=""):
    async with FishAPI(json.loads(auth.read_text(encoding="utf-8"))) as api:
        original = api._call_mtop

        async def guarded(name, *args, **kwargs):
            if name == "mtop.idle.pc.idleitem.publish":
                raise RuntimeError("Live draft check must never publish an item")
            return await original(name, *args, **kwargs)

        api._call_mtop = guarded
        with patch("myfish.adapters.fish.api.save_local_auth"):
            options = await api.get_publish_categories("虚拟商品 电子资料")
            category = options[0]
            title = f"MyFish草稿同步测试{int(time.time())}（请勿发布）"
            if not draft_id:
                response = await api.save_item_draft(
                    [],
                    title,
                    {"current_price": 1},
                    {"choice": "无需邮寄"},
                    category=category,
                )
                codes = [str(c).split("::", 1)[0] for c in response.get("ret", [])]
                print("create", codes, "data_keys", sorted(response.get("data", {})))
                assert "SUCCESS" in codes, "Draft create rejected"
                draft_id = str(response.get("data", {}).get("draftId") or "")
                print("draft_id", draft_id, flush=True)
                assert draft_id, "Draft ID missing; check platform before retrying"
            response = await api.save_item_draft(
                [],
                title + "-已更新",
                {"current_price": 2},
                {"choice": "无需邮寄"},
                category=category,
                draft_id=draft_id,
            )
            codes = [str(c).split("::", 1)[0] for c in response.get("ret", [])]
            print(
                "update",
                codes,
                "data_keys",
                sorted(response.get("data", {})),
                "draft_id",
                draft_id,
            )
            assert "SUCCESS" in codes, "Draft update rejected"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--auth",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "data/adapters/fish/auth.json",
    )
    parser.add_argument("--draft-id", default="")
    args = parser.parse_args()
    logger.remove()
    asyncio.run(check(args.auth, args.draft_id))
