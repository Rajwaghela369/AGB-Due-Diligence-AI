"""Project paths, resolved once from the repository root."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CONFIGS = ROOT / "configs"
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
PROCESSED = DATA / "processed"
GOLD = DATA / "gold"
STORES = ROOT / "stores"
REPORTS = ROOT / "reports"
