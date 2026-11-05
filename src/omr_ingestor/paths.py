"""Default locations of the data files kept at the repository root."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "docs" / "music.schema.json"
INSTRUMENTS_PATH = REPO_ROOT / "instruments.json"
