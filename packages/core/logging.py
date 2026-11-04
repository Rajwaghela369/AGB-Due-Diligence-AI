"""One logger factory, so pipeline runs and the app log the same way."""

import logging
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        logging.basicConfig(
            level=os.getenv("AGB_LOG_LEVEL", "INFO"),
            format=_FORMAT,
            datefmt="%H:%M:%S",
        )
        _configured = True
    return logging.getLogger(name)
