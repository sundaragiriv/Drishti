"""Logging setup using loguru."""

import sys

from loguru import logger

from signal_scanner.config import LOG_DIR


def setup_logger() -> None:
    """Configure loguru with stderr and rotating file handlers."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()

    # Colorized stderr at INFO level
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # Rotating file at DEBUG level
    logger.add(
        LOG_DIR / "scanner_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="50 MB",
        retention="7 days",
        compression="zip",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} - {message}"
        ),
    )
