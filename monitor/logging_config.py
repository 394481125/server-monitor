from __future__ import annotations

import logging
import os
import sys


LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def configured_log_level() -> tuple[str, int]:
    raw = os.environ.get("SERVER_MONITOR_LOG_LEVEL", os.environ.get("LOG_LEVEL", "INFO"))
    name = str(raw).strip().upper()
    if name not in LOG_LEVELS:
        raise RuntimeError("LOG_LEVEL 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
    return name, LOG_LEVELS[name]


def configure_logging() -> int:
    _name, level = configured_log_level()
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("server_monitor").setLevel(level)
    return level
