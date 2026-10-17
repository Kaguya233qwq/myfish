"""显式执行的真实只读检查；不被 unittest discover 自动运行。"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from loguru import logger
from myfish.adapters.fish.api import FishAPI, FishAPIError
from myfish.adapters.fish.orders import parse_sold_orders


async def check(auth_path: Path, order_id: str | None = None) -> dict:
    cookies = json.loads(auth_path.read_text(encoding="utf-8"))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": {}}
    async with FishAPI(cookies) as api:
        checks = {
            "sold_orders": api.get_sold_orders,
            "pending_ship_orders": lambda: api.get_sold_orders(query_code="NOT_SHIP"),
            "pending_ratings": api.get_pending_ratings,
            "refund_orders": api.get_refund_orders,
            "refunded_orders": lambda: api.get_refund_orders("5"),
        }
        selected_order_id = order_id
        # 只在内存接收更新，避免覆盖运行中机器人的凭证文件。
        with patch("myfish.adapters.fish.api.save_local_auth"):
            for name, call in checks.items():
                try:
                    response = await call()
                    item = {
                        "result": "SUCCESS",
                        "data_keys": sorted(response.get("data", {})),
                    }
                    if name == "sold_orders":
                        orders, has_next = parse_sold_orders(response)
                        item.update(order_count=len(orders), has_next=has_next)
                        if selected_order_id is None and orders:
                            selected_order_id = orders[0].order_id
                    report["checks"][name] = item
                except FishAPIError as exc:
                    report["checks"][name] = {"result": "FAILED", "error": str(exc)}
                except Exception as exc:  # noqa: BLE001
                    report["checks"][name] = {
                        "result": "FAILED",
                        "error_type": type(exc).__name__,
                    }
                await asyncio.sleep(1)
            if selected_order_id:
                try:
                    response = await api.get_order_detail(selected_order_id)
                    report["checks"]["order_detail"] = {
                        "result": "SUCCESS",
                        "data_keys": sorted(response.get("data", {})),
                    }
                except FishAPIError as exc:
                    report["checks"]["order_detail"] = {
                        "result": "FAILED",
                        "error": str(exc),
                    }
                except Exception as exc:  # noqa: BLE001
                    report["checks"]["order_detail"] = {
                        "result": "FAILED",
                        "error_type": type(exc).__name__,
                    }
            else:
                report["checks"]["order_detail"] = {
                    "result": "SKIPPED",
                    "reason": "No sold orders or explicit order ID",
                }
    report["write_operations"] = (
        "NOT_TESTED: requires a designated real order and allowed actions"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument("--order-id")
    args = parser.parse_args()
    logger.remove()
    result = asyncio.run(check(args.auth, args.order_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(
        any(item["result"] == "FAILED" for item in result["checks"].values())
    )
