"""只读检查商品列表及编辑详情。"""

import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from myfish.adapters.fish.api import FishAPI


async def check(auth: Path, item_id: str):
    cookies = json.loads(auth.read_text(encoding="utf-8"))
    async with FishAPI(cookies) as api:
        calls = [
            (
                "personal_items",
                "mtop.idle.web.xyh.item.list",
                {
                    "userId": str(cookies.get("unb", "")),
                    "needGroupInfo": True,
                    "pageNumber": 1,
                    "pageSize": 20,
                },
                None,
            ),
            (
                "seller_offline",
                "mtop.alibaba.idle.seller.pc.common.item.search",
                {
                    "pageNumber": 1,
                    "pageSize": 20,
                    "bizType": "commonPro",
                    "searchRequest": "{}",
                    "itemStatus": "-2",
                },
                {
                    "origin": "https://seller.goofish.com",
                    "referer": "https://seller.goofish.com/?site=COMMONPRO",
                    "idle_site_biz_code": "COMMONPRO",
                    "idle_user_group_member_id": "",
                },
            ),
        ]
        seller_headers = calls[1][3]
        calls[1:1] = [
            (
                "seller_identity",
                "mtop.alibaba.idle.seller.platform.user.business.identity.get",
                {},
                seller_headers,
            ),
            (
                "seller_onsale",
                "mtop.alibaba.idle.seller.pc.common.item.search",
                {**calls[1][2], "itemStatus": "0,-9"},
                seller_headers,
            ),
        ]
        if item_id:
            calls.append(
                (
                    "edit_detail",
                    "mtop.idle.pc.idleitem.editDetail",
                    {"itemId": api._trade_id(item_id)},
                    None,
                )
            )
        with patch("myfish.adapters.fish.api.save_local_auth"):
            for name, api_name, payload, headers in calls:
                response = await api._call_mtop(
                    api_name, "1.0", payload, headers=headers
                )
                data = response.get("data", {})
                print(
                    json.dumps(
                        {
                            "check": name,
                            "codes": [
                                str(code).split("::", 1)[0]
                                for code in response.get("ret", [])
                            ],
                            "data_keys": sorted(data),
                            "item_status": data.get("itemStatus"),
                            "permission_code": data.get("permissionCode"),
                            "business_codes": [
                                row.get("bizCode")
                                for row in data.get("data", [])
                                if isinstance(row, dict)
                            ]
                            if name == "seller_identity"
                            and isinstance(data.get("data"), list)
                            else None,
                        },
                        ensure_ascii=True,
                    )
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--item-id", default="")
    args = parser.parse_args()
    logger.remove()
    asyncio.run(check(args.auth, args.item_id))
