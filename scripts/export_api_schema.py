"""Export frontend types from the frozen Pydantic contracts, not handwritten copies.

The frontend generator consumes this one aggregate schema today. Replacing its contents
with FastAPI's OpenAPI document later leaves the Make target and freshness check unchanged.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from app.api import schemas
from app.domain import ChatIn, ChatResult, DecisionIn, Interrupt, ReviewSummary, StoredEvent

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "web" / "src" / "api" / "schema.json"


def contract_models() -> list[type[BaseModel]]:
    """Return API models declared in schemas.py, plus from app.domain the persisted event
    envelope, the `interrupt` event value, the bodies the review service takes too (decision,
    chat), the summary, and the chat's `chat_result` event value."""
    api_models = [
        value
        for value in vars(schemas).values()
        if inspect.isclass(value)
        and issubclass(value, BaseModel)
        and value.__module__ == schemas.__name__
    ]
    domain_models = [StoredEvent, Interrupt, DecisionIn, ChatIn, ChatResult, ReviewSummary]
    return sorted([*api_models, *domain_models], key=lambda model: model.__name__)


def render_schema() -> str:
    models = contract_models()
    refs, definitions = models_json_schema(
        [(model, "validation") for model in models],
        title="AGB Check API contracts",
        description="Generated from app.api.schemas and app.domain.StoredEvent.",
    )
    definitions.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {model.__name__: refs[(model, "validation")] for model in models},
            "additionalProperties": False,
        }
    )
    return json.dumps(definitions, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args()
    rendered = render_schema()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"{OUTPUT.relative_to(ROOT)} is stale; run make web-gen")
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
