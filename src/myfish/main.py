import asyncio

from myfish.core.config import load_config
from myfish.core.engine import Engine
from myfish.core.loader import load_adapters
from myfish.core.logger import init_logger
from myfish.core.logger import logger


async def main():
    # 初始化配置
    config = load_config()
    init_logger(config.logger_level)
    logger.info("配置加载成功，正在启动 Bot...")
    if not config.adapters:
        logger.warning("配置文件中未提供任何适配器，bot 将无法连接任何平台。")
        logger.warning("请编辑 config.toml 添加适配器配置后重启。")
        logger.info("Bot将自动退出")
        return
    engine = Engine()
    load_adapters(config.adapters, engine)
    # Bot, 启动!
    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot 已手动停止运行")
