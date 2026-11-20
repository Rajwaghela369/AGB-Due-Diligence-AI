"""Langfuse access: traces, datasets and scores.

Named to avoid shadowing the `langfuse` package on import.

Every client gets `mask=boundary().langfuse_mask`, so each observation's
input, output and metadata is masked before the SDK sends it. The hook reads the boundary
at call time, so a client built before `configure` still masks. Scores' comments and
dataset items are not observations; their writers mask them explicitly.
"""

from langfuse import Langfuse

from core.masking import boundary
from core.settings import load_settings


def client() -> Langfuse:
    settings = load_settings()
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        raise RuntimeError("Langfuse keys are not set; copy .env.example to .env")

    return Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
        mask=boundary().langfuse_mask,
    )
