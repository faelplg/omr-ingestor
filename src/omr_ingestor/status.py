"""The per-item status.json: stage results, suspect and hand-corrected measures, acceptance.

Measure numbers are kept as written in the score (strings), as in DurationReport.
The acceptance block is derived from hand_corrected and expected_measures, and the
measure_count block from measures_read and expected_measures; both are written for
people reading the file and recomputed (and checked) when reading it.

Schema version 1 had no measure_count; such files still read, with measures_read None.
"""

import json
import math
import os
import tempfile
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = (1, 2)
ACCEPTANCE_PERCENT = 5  # up to 5% of expected_measures corrected by hand
ACCEPTANCE_LIMIT = ACCEPTANCE_PERCENT / 100
RATIO_DIGITS = 6

# Pipeline stages, in order (docs/architecture.md).
STAGE_NAMES: tuple[str, ...] = (
    "audiveris",
    "xsd",
    "durations",
    "fixup",
    "xsd_after_fixup",
    "musescore",
    "mscx_check",
)


class StatusError(Exception):
    """Raised when a status.json is malformed or a status value is invalid."""


class StageState(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    status: StageState
    duration_s: float | None = None  # wall time; None when the stage did not run
    detail: str = ""

    def __post_init__(self) -> None:
        if self.name not in STAGE_NAMES:
            raise StatusError(f"unknown stage {self.name!r}; stages are {list(STAGE_NAMES)}")
        if self.duration_s is not None and (
            not math.isfinite(self.duration_s) or self.duration_s < 0
        ):
            raise StatusError(f"stage {self.name!r}: invalid duration {self.duration_s!r}")


@dataclass(frozen=True, slots=True)
class SuspectMeasure:
    number: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Acceptance:
    hand_corrected_count: int
    expected_measures: int
    ratio: float
    limit: float
    accepted: bool


@dataclass(frozen=True, slots=True)
class MeasureCount:
    read: int
    expected: int
    missing: int  # expected - read; negative when more measures were read than expected


@dataclass(frozen=True, slots=True)
class ItemStatus:
    pdf: str
    instrument: str
    expected_measures: int
    # Measures read by the OMR in the part; None until the durations stage has counted them.
    measures_read: int | None = None
    stages: tuple[Stage, ...] = ()
    suspect_measures: tuple[SuspectMeasure, ...] = ()
    # None until the human review is done; () means reviewed with nothing corrected.
    hand_corrected: tuple[str, ...] | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.expected_measures, bool)
            or not isinstance(self.expected_measures, int)
            or self.expected_measures < 1
        ):
            raise StatusError(
                f"expected_measures must be a positive integer, got {self.expected_measures!r}"
            )
        if self.measures_read is not None and (
            isinstance(self.measures_read, bool)
            or not isinstance(self.measures_read, int)
            or self.measures_read < 0
        ):
            raise StatusError(
                f"measures_read must be a non-negative integer, got {self.measures_read!r}"
            )
        names = [s.name for s in self.stages]
        if len(set(names)) != len(names):
            raise StatusError(f"duplicate stage in {names}")
        if self.hand_corrected is not None and len(set(self.hand_corrected)) != len(
            self.hand_corrected
        ):
            raise StatusError(f"duplicate measure in hand_corrected {list(self.hand_corrected)}")

    def stage(self, name: str) -> Stage | None:
        return next((s for s in self.stages if s.name == name), None)

    def with_stage(self, stage: Stage) -> "ItemStatus":
        """A copy with this stage added or replaced, keeping the pipeline order."""
        by_name = {s.name: s for s in self.stages}
        by_name[stage.name] = stage
        ordered = tuple(by_name[n] for n in STAGE_NAMES if n in by_name)
        return replace(self, stages=ordered)

    @property
    def measure_count(self) -> MeasureCount | None:
        """Measures read against expected_measures; None until they are counted."""
        if self.measures_read is None:
            return None
        return MeasureCount(
            read=self.measures_read,
            expected=self.expected_measures,
            missing=self.expected_measures - self.measures_read,
        )

    @property
    def acceptance(self) -> Acceptance | None:
        """The 5% criterion over expected_measures; None before the human review."""
        if self.hand_corrected is None:
            return None
        count = len(self.hand_corrected)
        ratio = round(count / self.expected_measures, RATIO_DIGITS)
        return Acceptance(
            hand_corrected_count=count,
            expected_measures=self.expected_measures,
            ratio=ratio,
            limit=ACCEPTANCE_LIMIT,
            # Integer math, so the boundary (exactly 5%) is not lost to float rounding.
            accepted=count * 100 <= ACCEPTANCE_PERCENT * self.expected_measures,
        )

    def to_dict(self) -> dict[str, Any]:
        """Plain data with a fixed key order."""
        acceptance = self.acceptance
        count = self.measure_count
        return {
            "schema_version": SCHEMA_VERSION,
            "pdf": self.pdf,
            "instrument": self.instrument,
            "expected_measures": self.expected_measures,
            "measure_count": None
            if count is None
            else {"read": count.read, "expected": count.expected, "missing": count.missing},
            "stages": [
                {
                    "name": s.name,
                    "status": s.status.value,
                    "duration_s": s.duration_s,
                    "detail": s.detail,
                }
                for s in self.stages
            ],
            "suspect_measures": [
                {"number": m.number, "detail": m.detail} for m in self.suspect_measures
            ],
            "hand_corrected": None if self.hand_corrected is None else list(self.hand_corrected),
            "acceptance": None
            if acceptance is None
            else {
                "hand_corrected_count": acceptance.hand_corrected_count,
                "expected_measures": acceptance.expected_measures,
                "ratio": acceptance.ratio,
                "limit": acceptance.limit,
                "accepted": acceptance.accepted,
            },
            "tool_versions": dict(sorted(self.tool_versions.items())),
        }

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "<status>") -> "ItemStatus":
        try:
            return _from_dict(cls, data, source)
        except (KeyError, TypeError, ValueError) as exc:
            raise StatusError(f"{source}: malformed status: {exc!r}") from exc


def _expect(value: Any, typ: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(value, typ) or (isinstance(value, bool) and typ is not bool):
        raise StatusError(f"{where}: unexpected value {value!r}")
    return value


_TOP_KEYS = (
    "schema_version",
    "pdf",
    "instrument",
    "expected_measures",
    "measure_count",
    "stages",
    "suspect_measures",
    "hand_corrected",
    "acceptance",
    "tool_versions",
)
_V1_TOP_KEYS = tuple(k for k in _TOP_KEYS if k != "measure_count")


def _from_dict(cls: type[ItemStatus], data: Any, source: str) -> ItemStatus:
    _expect(data, dict, source)
    version = data.get("schema_version")
    if version not in READABLE_SCHEMA_VERSIONS or isinstance(version, bool):
        raise StatusError(
            f"{source}: schema_version {version!r}, expected one of "
            f"{list(READABLE_SCHEMA_VERSIONS)}"
        )
    keys = _TOP_KEYS if version == SCHEMA_VERSION else _V1_TOP_KEYS
    if sorted(data) != sorted(keys):
        raise StatusError(f"{source}: keys {sorted(data)}, expected {sorted(keys)}")
    count = data.get("measure_count")
    measures_read = None
    if count is not None:
        measures_read = _expect(
            _expect(count, dict, f"{source}.measure_count")["read"],
            int,
            f"{source}.measure_count.read",
        )
    stages = tuple(
        Stage(
            name=_expect(s["name"], str, f"{source}.stages[{i}].name"),
            status=StageState(s["status"]),
            duration_s=None
            if s["duration_s"] is None
            else float(_expect(s["duration_s"], (int, float), f"{source}.stages[{i}].duration_s")),
            detail=_expect(s["detail"], str, f"{source}.stages[{i}].detail"),
        )
        for i, s in enumerate(_expect(data["stages"], list, f"{source}.stages"))
    )
    suspects = tuple(
        SuspectMeasure(
            number=_expect(m["number"], str, f"{source}.suspect_measures[{i}].number"),
            detail=_expect(m["detail"], str, f"{source}.suspect_measures[{i}].detail"),
        )
        for i, m in enumerate(_expect(data["suspect_measures"], list, f"{source}.suspect_measures"))
    )
    hand = data["hand_corrected"]
    if hand is not None:
        hand = tuple(
            _expect(n, str, f"{source}.hand_corrected[{i}]")
            for i, n in enumerate(_expect(hand, list, f"{source}.hand_corrected"))
        )
    versions = _expect(data["tool_versions"], dict, f"{source}.tool_versions")
    for k, v in versions.items():
        _expect(v, str, f"{source}.tool_versions.{k}")
    item = cls(
        pdf=_expect(data["pdf"], str, f"{source}.pdf"),
        instrument=_expect(data["instrument"], str, f"{source}.instrument"),
        expected_measures=_expect(data["expected_measures"], int, f"{source}.expected_measures"),
        measures_read=measures_read,
        stages=stages,
        suspect_measures=suspects,
        hand_corrected=hand,
        tool_versions=dict(versions),
    )
    if count != item.to_dict()["measure_count"]:
        raise StatusError(
            f"{source}: measure_count {count!r} does not match its read count and expected_measures"
        )
    if data["acceptance"] != item.to_dict()["acceptance"]:
        raise StatusError(
            f"{source}: acceptance {data['acceptance']!r} does not match hand_corrected and "
            "expected_measures"
        )
    return item


def dumps_status(item: ItemStatus) -> str:
    return json.dumps(item.to_dict(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def write_status(item: ItemStatus, path: Path) -> None:
    """Write status.json atomically: a temp file in the same folder, then os.replace."""
    path = Path(path)
    text = dumps_status(item)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_status(path: Path) -> ItemStatus:
    source = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise StatusError(f"{source}: cannot read file: {exc.strerror}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StatusError(
            f"{source}: not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    return ItemStatus.from_dict(data, source=source)
