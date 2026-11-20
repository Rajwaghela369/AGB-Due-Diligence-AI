"""Validated loaders for the YAML configs, plus the config hash used in the manifest."""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

# Top-level kb.yaml keys that never change an indexed vector. Retrieval policy
# lived in kb.yaml until Phase 4 and is runtime config in app.yaml now; the gate
# threshold decides whether an alias moves, not what gets embedded. Excluding
# them means tuning either never makes the app refuse its own index.
LINEAGE_EXCLUDED = ("retrieval", "gate")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def config_hash(*configs: dict[str, Any]) -> str:
    """Deterministic hash of the configs that produced an artifact.

    Written into the store manifest so the app can refuse an index that was
    built from a different configuration.
    """
    payload = json.dumps(configs, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def index_config_hash(kb: dict[str, Any], paragraphs: dict[str, Any]) -> str:
    """The index's lineage: only the kb.yaml keys that shape the vectors."""
    indexed = {key: value for key, value in kb.items() if key not in LINEAGE_EXCLUDED}
    return config_hash(indexed, paragraphs)
