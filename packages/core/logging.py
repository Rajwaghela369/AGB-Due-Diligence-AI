"""One logger factory, so pipeline runs and the app log the same way."""

import logging
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"

# Chatty on every HTTP call and every dataset resolution step; our own logs
# are unreadable otherwise.
_NOISY = ("httpx", "httpcore", "datasets", "fsspec", "filelock", "urllib3")

_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        logging.basicConfig(
            level=os.getenv("AGB_LOG_LEVEL", "INFO"),
            format=_FORMAT,
            datefmt="%H:%M:%S",
        )
        for noisy in _NOISY:
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True
    return logging.getLogger(name)
