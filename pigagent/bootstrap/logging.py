# pigagent/bootstrap/logging.py
"""Loguru configuration  -  called once at module load time."""

import logging
import sys
from pathlib import Path

from loguru import logger

from config import get_config


class _InterceptHandler(logging.Handler):
    """Forward standard library logging to loguru."""
    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.bind(name=record.name, function=record.funcName, line=record.lineno).opt(
            depth=0, exception=record.exc_info
        ).log(level, record.getMessage())


_config = get_config()

logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

# Capture livekit / third-party loggers
for _name in ("livekit", "livekit.agents", "livekit_api", "opentelemetry"):
    _lg = logging.getLogger(_name)
    _lg.handlers = [_InterceptHandler()]
    _lg.propagate = False
    _lg.setLevel(logging.INFO)

logger.remove()

logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    ),
    level=_config.LOG_LEVEL.upper(),
    colorize=True,
)

if _config.LOG_TO_FILE:
    log_file = Path(_config.LOG_FILE_PATH)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        _config.LOG_FILE_PATH,
        rotation=_config.LOG_ROTATION,
        retention=_config.LOG_RETENTION,
        level=_config.LOG_LEVEL.upper(),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} - {message}"
        ),
        enqueue=True,
    )
    logger.info(f"File logging enabled: {_config.LOG_FILE_PATH}")
