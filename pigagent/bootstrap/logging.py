# pigagent/bootstrap/logging.py
"""Loguru configuration  -  called once at module load time."""

import sys
from pathlib import Path

from loguru import logger

from config import get_config

_config = get_config()

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
