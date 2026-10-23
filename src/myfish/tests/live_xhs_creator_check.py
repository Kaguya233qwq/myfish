import asyncio

import httpx

from myfish.adapters.xhs.api import XhsAPI, XhsAPIError
from myfish.adapters.xhs.creator import CreatorAPI
from myfish.adapters.xhs.utils import get_auth_file
from myfish.core.config import ConfigManager
from myfish.core.logger import init_logger, logger


async def check() -> None:
    state = get_auth_file().with_suffix(".state.json")
    async with (
        XhsAPI.from_state(state) as account,
        CreatorAPI(account) as creator,
    ):
        await creator.initialize()
        await creator.get_self_info()
        logger.success(
            "[XHS][Creator] 账号信息验证通过: HTTP 200, success=true, code=0"
        )


def main() -> None:
    ConfigManager.load_config()
    init_logger(ConfigManager.get_config().logger_level)
    try:
        asyncio.run(check())
    except XhsAPIError as exc:
        logger.error(
            "[XHS][Creator] 请求被拒绝: HTTP {}, code={}", exc.status, exc.code
        )
    except httpx.TransportError as exc:
        logger.error("[XHS][Creator] 网络请求失败: error={}", type(exc).__name__)
    except (ValueError, OSError) as exc:
        logger.error(
            "[XHS][Creator] 设备或会话初始化失败: error={}", type(exc).__name__
        )
    else:
        return
    raise SystemExit(1)


if __name__ == "__main__":
    main()
