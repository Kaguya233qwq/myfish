import asyncio

from myfish.core.config import BotConfig, load_config
from myfish.core.engine import Engine
from myfish.core.loader import load_adapters
from myfish.core.logger import init_logger, logger


async def main(config: BotConfig | None = None) -> None:
    if config is None:
        config = load_config()
    init_logger(config.logger_level)
    logger.info("配置加载成功，正在启动 Bot...")
    if not config.adapters:
        logger.warning("配置文件中未提供任何适配器，bot 将无法连接任何平台。")
        logger.warning("请编辑 config.toml 添加适配器配置后重启。")
        logger.info("继续启动插件服务；平台消息功能在配置适配器后可用。")
    engine = Engine(plugin_configs=config.plugins)
    load_adapters(config.adapters, engine)
    # Bot, 启动!
    await engine.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot 已手动停止运行")
