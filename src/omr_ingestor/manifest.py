"""Validation of the music.json manifest against docs/music.schema.json."""

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from omr_ingestor.paths import SCHEMA_PATH


class ManifestError(Exception):
    """Raised when a manifest cannot be read or does not match the schema.

    `problems` holds one human-readable line per schema violation.
    """

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{source}: invalid manifest ({len(problems)} problem(s)):\n{lines}")


@cache
def _validator(schema_path: Path) -> Draft202012Validator:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _location(error: ValidationError) -> str:
    """Render the error path as `items[0].sha256`, or `<root>` for the top level."""
    out = ""
    for part in error.absolute_path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "<root>"


def _describe(error: ValidationError) -> str:
    match error.validator:
        case "enum":
            allowed = ", ".join(json.dumps(v) for v in error.validator_value)
            detail = f"{json.dumps(error.instance)} is not allowed; expected one of: {allowed}"
        case "pattern" if error.absolute_path and error.absolute_path[-1] == "sha256":
            detail = f"{json.dumps(error.instance)} is not a lowercase hex sha256 (64 chars)"
        case _:
            detail = error.message
    return f"{_location(error)}: {detail}"


def validate_manifest(data: Any, *, source: str = "<manifest>", schema_path: Path = SCHEMA_PATH):
    """Validate already-parsed manifest data. Raises ManifestError listing every problem."""
    errors = sorted(
        _validator(schema_path).iter_errors(data),
        key=lambda e: (list(map(str, e.absolute_path)), e.validator),
    )
    if errors:
        raise ManifestError(source, [_describe(e) for e in errors])


def load_manifest(path: Path, *, schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Read, parse and validate a music.json file. Returns the parsed data."""
    source = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(source, [f"cannot read file: {exc.strerror}"]) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            source, [f"not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]
        ) from exc
    validate_manifest(data, source=source, schema_path=schema_path)
    return data
