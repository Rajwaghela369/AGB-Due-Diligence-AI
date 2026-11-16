"""Migrate an index manifest after moving runtime-only config out of kb.yaml."""

import argparse

from core import qdrant
from core.config import index_config_hash, load_yaml
from core.paths import CONFIGS


def migrate(collection: str, expected_old_hash: str) -> str:
    kb = load_yaml(CONFIGS / "kb.yaml")
    paragraphs = load_yaml(CONFIGS / "kb_paragraphs.yaml")
    expected_new_hash = index_config_hash(kb, paragraphs)
    client = qdrant.client()
    manifest = qdrant.read_manifest(client, collection)
    if manifest is None:
        raise RuntimeError(f"collection {collection!r} has no manifest")
    if manifest.config_hash != expected_old_hash:
        raise RuntimeError(
            f"refusing to migrate {collection!r}: expected old hash "
            f"{expected_old_hash}, found {manifest.config_hash}"
        )
    if manifest.embedding_model != kb["embedding"]["model"]:
        raise RuntimeError("manifest embedding model does not match kb.yaml")

    migrated = manifest.model_copy(update={"config_hash": expected_new_hash})
    qdrant.write_manifest(client, collection, migrated)
    return expected_new_hash


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default=load_yaml(CONFIGS / "kb.yaml")["store"]["alias"])
    parser.add_argument("--expected-old-hash", required=True)
    args = parser.parse_args()
    print(migrate(args.collection, args.expected_old_hash))


if __name__ == "__main__":
    main()
