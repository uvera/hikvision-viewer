"""Shared logging setup for terminal and desktop launches."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from hikvision_viewer.config_loader import app_config_dir

_LOGGER_SETUP_DONE = False


def _log_level_from_env() -> int:
    raw = os.environ.get("HIKVISION_LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


def _log_file_path() -> Path:
    custom = os.environ.get("HIKVISION_LOG_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    return app_config_dir() / "hikvision-viewer.log"


def configure_logging() -> Path:
    """Initialize root logging once and return the active log file path."""
    global _LOGGER_SETUP_DONE
    log_path = _log_file_path()
    if _LOGGER_SETUP_DONE:
        return log_path

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(
        level=_log_level_from_env(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    _LOGGER_SETUP_DONE = True
    logging.getLogger(__name__).info("Logging initialized: file=%s", log_path)
    return log_path
