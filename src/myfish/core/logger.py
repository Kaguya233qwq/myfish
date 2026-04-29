import sys
import loguru


def init_logger(level: str = "INFO"):
    """初始化日志系统"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
    )


logger = loguru.logger.bind(name="myfish")
