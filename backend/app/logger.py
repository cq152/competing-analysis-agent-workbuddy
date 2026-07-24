"""统一日志配置：替代散落的 print，便于生产期定位问题。

W2 起步阶段保持简单（控制台输出）；W3/W4 可扩展为文件日志 + JSON 格式 + 飞书错误 channel。
"""
import logging
import sys
from datetime import datetime

from .config import settings


def setup_logger(name: str = "bot", level: str = "INFO") -> logging.Logger:
    """创建统一日志器（幂等：重复调用不会重复挂 handler）。"""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    logger.propagate = False

    if not logger.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(console)

    return logger


# 默认日志器（模块级单例），其他文件直接 `from app.logger import log`
log = setup_logger(level=settings.log_level)
