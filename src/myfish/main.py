import asyncio

from myfish.adapters.fish import FishWebSocketAdapter
from myfish.core.bot import Bot
from myfish.core.logger import init_logger
from myfish.core.logger import logger


async def main():
    # 初始化日志和配置
    init_logger(level="DEBUG")
    # TODO: 这里可以添加配置文件加载的逻辑

    # 实例化适配器
    adapter = FishWebSocketAdapter()

    # 实例化bot引擎并挂载适配器
    bot = Bot(adapter=adapter)

    # Bot, 启动! 
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot 已手动停止运行")
