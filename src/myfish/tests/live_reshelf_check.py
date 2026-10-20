"""检查指定商品；仅 --execute 提交恢复上架。"""

import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from myfish.adapters.fish.api import FishAPI
from myfish.adapters.fish.utils import reshelf_payload


async def check(args):
    cookies = json.loads(args.auth.read_text(encoding="utf-8"))
    with patch("myfish.adapters.fish.api.save_local_auth"):
        async with FishAPI(cookies) as api:
            detail = await api.get_item_edit_detail(args.item_id)
            reshelf_payload(
                detail, args.item_id, str(cookies.get("unb", "")), args.quantity
            )
            print(
                json.dumps(
                    {
                        "check": "preconditions",
                        "item_status": detail.get("itemStatus"),
                        "quantity": args.quantity,
                    }
                )
            )
            if args.execute:
                result = await api.reshelf_item(args.item_id, quantity=args.quantity)
                print(
                    json.dumps(
                        {
                            "check": "reshelf",
                            "item_status": result.get("itemStatus"),
                            "same_item": str(result.get("itemId")) == args.item_id,
                        }
                    )
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    logger.remove()
    asyncio.run(check(parser.parse_args()))
