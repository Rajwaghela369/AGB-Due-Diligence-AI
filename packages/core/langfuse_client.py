"""Langfuse access: traces, datasets and scores.

Named to avoid shadowing the `langfuse` package on import.
"""

from langfuse import Langfuse

from core.settings import load_settings


def client() -> Langfuse:
    settings = load_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        raise RuntimeError("Langfuse keys are not set; copy .env.example to .env")

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
