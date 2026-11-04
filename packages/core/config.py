"""Validated loaders for the YAML configs, plus the config hash used in the manifest."""

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


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
