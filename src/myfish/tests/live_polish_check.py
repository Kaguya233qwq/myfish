"""显式真实写入检查：只擦亮一件后台已关联的在售商品，不发布或修改商品内容。"""

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from myfish.adapters.fish.api import FishAPI


async def main(product_id: int):
    root = Path(__file__).resolve().parents[3]
    path = root / "data/plugins/fishy/fishy.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True) as db:
        row = db.execute(
            "SELECT item_id,account_id,status FROM products WHERE id=?", (product_id,)
        ).fetchone()
    if not row or row[2] != "published" or not row[0]:
        raise ValueError("选择的商品必须是在售且已有平台 ID 的商品")
    cookies = json.loads(
        (root / "data/adapters/fish/auth.json").read_text(encoding="utf-8")
    )
    if str(cookies.get("unb")) != row[1]:
        raise ValueError("账号与商品所属账号不一致")
    async with FishAPI(cookies) as api:
        with patch("myfish.adapters.fish.api.save_local_auth"):
            result = await api.polish_item(row[0])
            print(
                json.dumps(
                    {
                        "product_id": product_id,
                        "ret": result.get("ret"),
                        "data": result.get("data"),
                    },
                    ensure_ascii=True,
                )
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-id", type=int, required=True)
    args = parser.parse_args()
    logger.remove()
    asyncio.run(main(args.product_id))
