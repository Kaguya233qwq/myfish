"""显式只读检查虚拟与实物类目查询及手动选择，不发布商品，不保存凭证。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from loguru import logger

from myfish.adapters.fish.api import FishAPI


async def main():
    auth = Path(__file__).resolve().parents[3] / "data/adapters/fish/auth.json"
    async with FishAPI(json.loads(auth.read_text(encoding="utf-8"))) as api:
        with patch("myfish.adapters.fish.api.save_local_auth"):
            for kind, text in (
                ("virtual", "虚拟商品 电子资料"),
                ("physical", "实物商品 衣服"),
            ):
                options = await api.get_publish_categories(text)
                assert options, f"{kind}: no category options"
                print(
                    kind,
                    "options",
                    len(options),
                    json.dumps(options[:2], ensure_ascii=True),
                )
                selected = options[1]
                result = await api._get_public_channel(text, [], category=selected)
                assert any(
                    str(code).split("::", 1)[0] == "SUCCESS"
                    for code in result.get("ret", [])
                )
                print(kind, "selection_query", "SUCCESS", selected["channelCatId"])


if __name__ == "__main__":
    logger.remove()
    asyncio.run(main())
