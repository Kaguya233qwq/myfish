"""短时接收验证，不加载插件、不发送聊天消息、不保存凭证。"""

import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import websockets
from loguru import logger

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI
from myfish.adapters.fish.utils import load_message_json
from myfish.core.event import MessageEvent


async def check(auth: Path, seconds: int):
    cookies = json.loads(auth.read_text(encoding="utf-8"))
    report = {"frames": 0, "push_items": 0, "message_events": 0}

    async def received(event):
        if not isinstance(event, MessageEvent):
            return
        report["message_events"] += 1
        print(
            json.dumps(
                {
                    "received": True,
                    "segments": [segment.type for segment in event.messages],
                }
            ),
            flush=True,
        )

    with patch("myfish.adapters.fish.api.save_local_auth"):
        async with FishAPI(cookies) as api:
            adapter = FishAdapter(api)
            adapter.bot_id = str(cookies.get("unb", ""))
            adapter.set_callback(received)
            async with websockets.connect(
                adapter.base_url, additional_headers=adapter._get_headers()
            ) as ws:
                adapter._active_ws = ws
                if not await adapter._init_connection():
                    print(json.dumps({"connected": False}), flush=True)
                    return False
                print(
                    json.dumps({"connected": True, "listen_seconds": seconds}),
                    flush=True,
                )
                heartbeat = asyncio.create_task(adapter._heart_beat_loop())
                try:
                    async with asyncio.timeout(seconds):
                        async for raw in ws:
                            report["frames"] += 1
                            frame = load_message_json(raw)
                            body = (
                                frame.get("body") if isinstance(frame, dict) else None
                            )
                            package = (
                                body.get("syncPushPackage")
                                if isinstance(body, dict)
                                else None
                            )
                            if isinstance(package, dict) and isinstance(
                                package.get("data"), list
                            ):
                                report["push_items"] += len(package["data"])
                            await adapter._handle_raw_message(raw)
                except TimeoutError:
                    pass
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    adapter._active_ws = None
    print(json.dumps(report), flush=True)
    return bool(report["message_events"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth", type=Path, required=True)
    parser.add_argument(
        "--seconds", type=int, default=45, choices=range(5, 301), metavar="5..300"
    )
    args = parser.parse_args()
    logger.remove()
    raise SystemExit(not asyncio.run(check(args.auth, args.seconds)))
