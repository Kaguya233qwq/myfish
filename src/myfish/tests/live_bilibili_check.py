import argparse
import asyncio

from myfish.adapters.bilibili import BilibiliAdapter
from myfish.core.config import ConfigManager
from myfish.core.event import BaseEvent, MessageEvent
from myfish.core.logger import init_logger, logger


async def check(
    duration: float, notifications: bool, live_rooms: list[int] | None = None
) -> None:
    config = ConfigManager.get_config()
    options: dict[str, object] = {}
    for adapter in config.adapters:
        if adapter.id == "bilibili":
            options.update(adapter.config)
            break
    if notifications:
        options["notifications"] = True
    if live_rooms is not None:
        options["live_rooms"] = live_rooms
    adapter = BilibiliAdapter.setup(**options)

    async def receive(event: BaseEvent) -> None:
        if isinstance(event, MessageEvent):
            logger.info(
                "[Bilibili][验收] 私信: sender={}, types={}",
                event.sender_id,
                [item.type for item in event.messages],
            )
        else:
            logger.info("[Bilibili][验收] 事件: {}", event.event_type)

    adapter.set_callback(receive)
    try:
        async with asyncio.timeout(duration):
            await adapter.run()
    except TimeoutError:
        if not adapter.bot_id:
            raise RuntimeError("Bilibili login was not completed") from None
        logger.info("[Bilibili] 验收监听结束")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bilibili 登录与消息接收验收，不加载插件、不发送消息"
    )
    parser.add_argument("--duration", type=float, default=180)
    parser.add_argument("--notifications", action="store_true")
    parser.add_argument("--live-room", type=int, action="append")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.live_room is not None and any(room <= 0 for room in args.live_room):
        parser.error("--live-room must be positive")
    ConfigManager.load_config()
    init_logger(ConfigManager.get_config().logger_level)
    asyncio.run(check(args.duration, args.notifications, args.live_room))


if __name__ == "__main__":
    main()
