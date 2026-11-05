import copy
import json
from pathlib import Path

import pytest

from omr_ingestor.paths import REPO_ROOT

EXAMPLE_PATH = REPO_ROOT / "examples" / "music.example.json"


@pytest.fixture
def example() -> dict:
    return copy.deepcopy(json.loads(EXAMPLE_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def write_json(tmp_path: Path):
    def _write(data, name: str = "data.json") -> Path:
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path

    return _write
