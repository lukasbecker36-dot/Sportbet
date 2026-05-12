"""Shared logging setup: writes to scrape.log and the console."""

import logging
import sys

try:
    import config
except ImportError:  # pragma: no cover - config is created by copying config.example.py
    config = None

_LOG_PATH = getattr(config, "SCRAPE_LOG", "scrape.log") if config else "scrape.log"
_CONFIGURED = False


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    file_handler = logging.FileHandler(_LOG_PATH)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    root = logging.getLogger("sportbet")
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"sportbet.{name}")
