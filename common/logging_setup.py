"""Shared process-level logging configuration."""

from __future__ import annotations

import logging
import os


_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(logger_name: str) -> logging.Logger:
    """Apply LOG_LEVEL consistently and warn if it is not a supported name."""
    raw_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = _LEVELS.get(raw_level)
    invalid = level is None
    if invalid:
        level = logging.INFO

    logging.basicConfig(level=level, format=_FORMAT)
    logging.getLogger().setLevel(level)
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    if invalid:
        logger.warning("Unsupported LOG_LEVEL=%r; using INFO", raw_level)
    return logger
