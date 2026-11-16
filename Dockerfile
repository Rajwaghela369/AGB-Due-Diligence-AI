# The API image. It serves `app.api.main:build_app` and nothing else: the pipelines and
# evals stay on the host, so the image needs only the `app` extra.
FROM python:3.12-slim

# uv comes from its own image, so the version is pinned by tag rather than by installer script.
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so a source change does not reinstall them.
COPY pyproject.toml uv.lock .python-version ./
COPY packages ./packages
COPY app ./app
COPY pipelines ./pipelines
COPY evals ./evals
RUN uv sync --extra app --frozen

COPY configs ./configs
# The statute chunks the API validates at start-up. They are rebuildable, so they are
# copied from the build context, which must contain data/processed/bgb_chunks.jsonl.
COPY data/processed/bgb_chunks.jsonl ./data/processed/bgb_chunks.jsonl

# Immutable source PDFs live here; compose mounts a volume over it.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/stores/files \
    && chown -R app:app /app/stores
USER app

EXPOSE 8000

# One worker on purpose. Review runs, their event logs and the SSE fan-out live in this
# process's memory until they move to Postgres LISTEN/NOTIFY. A second worker would split
# a review's events from the clients watching it.
CMD ["uvicorn", "app.api.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
