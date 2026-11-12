"""The per-item status.json: stage results, suspect and hand-corrected measures, acceptance.

Measure numbers are kept as written in the score (strings), as in DurationReport.
The acceptance block is derived from hand_corrected and expected_measures, and the
measure_count block from measures_read and expected_measures; both are written for
people reading the file and recomputed (and checked) when reading it.

Schema version 1 had no measure_count; such files still read, with measures_read None.
Versions 1 and 2 did not record audiveris_options; these read as None (unknown).
Versions 1-3 did not record review signals; these read as an empty tuple.
Versions 1-4 did not record text removals; these read as None (unknown).
Versions 1-5 did not record part_ids; shared correction lists retain item-wide acceptance.
Versions 1-6 did not record part identities; reruns retain ID-only matching.
"""

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from omr_ingestor.durations import REVIEW_SIGNAL_CODES, ReviewSignal
from omr_ingestor.fixup import TEXT_REMOVAL_CATEGORIES

SCHEMA_VERSION = 7
READABLE_SCHEMA_VERSIONS = (1, 2, 3, 4, 5, 6, 7)
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
    accepted: bool | None


@dataclass(frozen=True, slots=True)
class MeasureCount:
    read: int
    expected: int
    missing: int  # expected - read; negative when more measures were read than expected


@dataclass(frozen=True, slots=True)
class PartIdentity:
    part_name: str
    instruments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _expect(self.part_name, str, "part identity name")
        _expect(self.instruments, tuple, "part identity instruments")
        for name in self.instruments:
            _expect(name, str, "part identity instrument name")


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
    hand_corrected: tuple[str, ...] | dict[str, tuple[str, ...]] | None = None
    # MusicXML IDs in score order; reruns retain prior IDs until counting succeeds.
    part_ids: tuple[str, ...] | None = None
    # Raw score metadata, before instrument fixup; None keeps legacy ID-only matching.
    part_identities: dict[str, PartIdentity] | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)
    # Overrides passed to Audiveris; {} uses tool defaults, None means not recorded.
    audiveris_options: dict[str, str] | None = field(default_factory=dict)

    review_signals: tuple[ReviewSignal, ...] = ()
    # None before cleanup or for legacy files; zero counts when keep_text is true.
    text_removals: dict[str, int] | None = None

    @property
    def review_required(self) -> bool:
        return bool(
            self.review_signals
            or self.suspect_measures
            or (self.measure_count is not None and self.measure_count.missing != 0)
        )

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
        if self.text_removals is not None:
            _validate_text_removals(self.text_removals, "text_removals")
        names = [s.name for s in self.stages]
        if len(set(names)) != len(names):
            raise StatusError(f"duplicate stage in {names}")
        if self.part_ids is not None:
            _validate_measures(self.part_ids, "part_ids")
        if self.part_identities is not None:
            _expect(self.part_identities, dict, "part_identities")
            if self.part_ids is None or set(self.part_identities) != set(self.part_ids):
                raise StatusError("part_identities must match part_ids exactly")
            for identity in self.part_identities.values():
                _expect(identity, PartIdentity, "part_identities entry")
        if isinstance(self.hand_corrected, dict):
            if self.part_ids is None or len(self.part_ids) < 2:
                raise StatusError("per-part hand_corrected requires multipart part_ids")
            for part_id, measures in self.hand_corrected.items():
                if part_id not in self.part_ids:
                    raise StatusError(f"unknown part ID in hand_corrected: {part_id!r}")
                _validate_measures(measures, f"hand_corrected.{part_id}")
        elif self.hand_corrected is not None:
            _validate_measures(self.hand_corrected, "hand_corrected")

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

    def _acceptance(self, measures: tuple[str, ...]) -> Acceptance:
        count = len(measures)
        return Acceptance(
            hand_corrected_count=count,
            expected_measures=self.expected_measures,
            ratio=round(count / self.expected_measures, RATIO_DIGITS),
            limit=ACCEPTANCE_LIMIT,
            # Integer math preserves the exact 5% boundary.
            accepted=count * 100 <= ACCEPTANCE_PERCENT * self.expected_measures,
        )

    @property
    def reviewed_part_ids(self) -> tuple[str, ...]:
        """Reviewed IDs; a shared list identifies a part only for confirmed single-part items."""
        if isinstance(self.hand_corrected, dict):
            return tuple(self.hand_corrected)
        if (
            self.hand_corrected is not None
            and self.part_ids is not None
            and len(self.part_ids) == 1
        ):
            return self.part_ids
        return ()

    @property
    def acceptance(self) -> Acceptance | dict[str, Acceptance | None] | None:
        """Per-part 5% criterion; flat lists require confirmed single-part identity."""
        if self.hand_corrected is None:
            return None
        if isinstance(self.hand_corrected, dict):
            return {
                part_id: self._acceptance(self.hand_corrected[part_id])
                if part_id in self.hand_corrected
                and not any(
                    signal.code == "part_identity_changed" and signal.part_id == part_id
                    for signal in self.review_signals
                )
                else None
                for part_id in self.part_ids
            }
        acceptance = self._acceptance(self.hand_corrected)
        if self.part_ids is None or len(self.part_ids) != 1:
            return replace(acceptance, accepted=None)
        if any(
            signal.code == "part_identity_changed" and signal.part_id == self.part_ids[0]
            for signal in self.review_signals
        ):
            return None
        return acceptance

    def to_dict(self) -> dict[str, Any]:
        """Plain data with a fixed key order."""
        count = self.measure_count
        return {
            "schema_version": SCHEMA_VERSION,
            "review_required": self.review_required,
            "review_signals": [
                {"code": s.code, "detail": s.detail, "part_id": s.part_id, "measure": s.measure}
                for s in self.review_signals
            ],
            "text_removals": None if self.text_removals is None else dict(self.text_removals),
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
            "part_ids": None if self.part_ids is None else list(self.part_ids),
            "part_identities": None
            if self.part_identities is None
            else {
                pid: {"part_name": identity.part_name, "instruments": list(identity.instruments)}
                for pid, identity in self.part_identities.items()
            },
            "hand_corrected": {
                part_id: list(measures) for part_id, measures in sorted(self.hand_corrected.items())
            }
            if isinstance(self.hand_corrected, dict)
            else None
            if self.hand_corrected is None
            else list(self.hand_corrected),
            "acceptance": acceptance_dict(self.acceptance),
            "tool_versions": dict(sorted(self.tool_versions.items())),
            "audiveris_options": None
            if self.audiveris_options is None
            else dict(sorted(self.audiveris_options.items())),
        }

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "<status>") -> "ItemStatus":
        try:
            return _from_dict(cls, data, source)
        except (KeyError, TypeError, ValueError) as exc:
            raise StatusError(f"{source}: malformed status: {exc!r}") from exc


def acceptance_dict(
    acceptance: Acceptance | dict[str, Acceptance | None] | None,
) -> dict[str, Any] | None:
    if isinstance(acceptance, dict):
        return {
            part_id: None if value is None else asdict(value)
            for part_id, value in acceptance.items()
        }
    return None if acceptance is None else asdict(acceptance)


def _validate_measures(value: Any, where: str) -> None:
    _expect(value, tuple, where)
    for number in value:
        if not _expect(number, str, where).strip():
            raise StatusError(f"{where}: empty measure or part ID")
    if len(set(value)) != len(value):
        raise StatusError(f"duplicate measure or part ID in {where}: {list(value)}")


def _expect(value: Any, typ: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(value, typ) or (isinstance(value, bool) and typ is not bool):
        raise StatusError(f"{where}: unexpected value {value!r}")
    return value


def _validate_text_removals(value: Any, where: str) -> None:
    _expect(value, dict, where)
    if set(value) != set(TEXT_REMOVAL_CATEGORIES):
        raise StatusError(f"{where}: expected exactly keys {list(TEXT_REMOVAL_CATEGORIES)}")
    for category, count in value.items():
        if _expect(count, int, f"{where}.{category}") < 0:
            raise StatusError(f"{where}.{category}: count must be non-negative")


_TOP_KEYS = (
    "schema_version",
    "review_required",
    "review_signals",
    "text_removals",
    "pdf",
    "instrument",
    "expected_measures",
    "measure_count",
    "stages",
    "suspect_measures",
    "part_ids",
    "part_identities",
    "hand_corrected",
    "acceptance",
    "tool_versions",
    "audiveris_options",
)
_V6_TOP_KEYS = tuple(k for k in _TOP_KEYS if k != "part_identities")
_V5_TOP_KEYS = tuple(k for k in _V6_TOP_KEYS if k != "part_ids")
_V4_TOP_KEYS = tuple(k for k in _V5_TOP_KEYS if k != "text_removals")
_V3_TOP_KEYS = tuple(k for k in _V4_TOP_KEYS if k not in ("review_required", "review_signals"))
_V2_TOP_KEYS = tuple(k for k in _V3_TOP_KEYS if k != "audiveris_options")
_V1_TOP_KEYS = tuple(k for k in _V2_TOP_KEYS if k != "measure_count")


def _from_dict(cls: type[ItemStatus], data: Any, source: str) -> ItemStatus:
    _expect(data, dict, source)
    version = data.get("schema_version")
    if version not in READABLE_SCHEMA_VERSIONS or isinstance(version, bool):
        raise StatusError(
            f"{source}: schema_version {version!r}, expected one of "
            f"{list(READABLE_SCHEMA_VERSIONS)}"
        )
    keys_by_version = {
        1: _V1_TOP_KEYS,
        2: _V2_TOP_KEYS,
        3: _V3_TOP_KEYS,
        4: _V4_TOP_KEYS,
        5: _V5_TOP_KEYS,
        6: _V6_TOP_KEYS,
        7: _TOP_KEYS,
    }
    keys = keys_by_version[version]
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
    if isinstance(hand, dict) and version >= 6:
        hand = {
            _expect(part_id, str, f"{source}.hand_corrected key"): tuple(
                _expect(n, str, f"{source}.hand_corrected.{part_id}[{i}]")
                for i, n in enumerate(_expect(numbers, list, f"{source}.hand_corrected.{part_id}"))
            )
            for part_id, numbers in hand.items()
        }
    elif hand is not None:
        hand = tuple(
            _expect(n, str, f"{source}.hand_corrected[{i}]")
            for i, n in enumerate(_expect(hand, list, f"{source}.hand_corrected"))
        )
    ids = data.get("part_ids")
    if ids is not None:
        ids = tuple(
            _expect(n, str, f"{source}.part_ids") for n in _expect(ids, list, f"{source}.part_ids")
        )
    identities = data.get("part_identities")
    if identities is not None:
        _expect(identities, dict, f"{source}.part_identities")
        parsed = {}
        for pid, identity in identities.items():
            where = f"{source}.part_identities.{pid}"
            _expect(identity, dict, where)
            if set(identity) != {"part_name", "instruments"}:
                raise StatusError(f"{where}: expected part_name and instruments")
            parsed[pid] = PartIdentity(
                part_name=_expect(identity["part_name"], str, f"{where}.part_name"),
                instruments=tuple(
                    _expect(name, str, f"{where}.instruments")
                    for name in _expect(identity["instruments"], list, f"{where}.instruments")
                ),
            )
        identities = parsed
    versions = _expect(data["tool_versions"], dict, f"{source}.tool_versions")
    for k, v in versions.items():
        _expect(v, str, f"{source}.tool_versions.{k}")
    options = data.get("audiveris_options")
    if options is not None:
        _expect(options, dict, f"{source}.audiveris_options")
        for k, v in options.items():
            _expect(k, str, f"{source}.audiveris_options key")
            _expect(v, str, f"{source}.audiveris_options.{k}")
    signals = []
    signal_keys = {"code", "detail", "part_id", "measure"}
    for i, signal in enumerate(
        _expect(data.get("review_signals", []), list, f"{source}.review_signals")
    ):
        where = f"{source}.review_signals[{i}]"
        _expect(signal, dict, where)
        if set(signal) != signal_keys:
            raise StatusError(f"{where}: expected exactly keys {sorted(signal_keys)}")
        fields = {key: _expect(signal[key], str, f"{where}.{key}") for key in signal_keys}
        if fields["code"] not in REVIEW_SIGNAL_CODES:
            raise StatusError(f"{where}.code: unknown review signal code {fields['code']!r}")
        signals.append(ReviewSignal(**fields))
    item = cls(
        review_signals=tuple(signals),
        text_removals=data.get("text_removals"),
        pdf=_expect(data["pdf"], str, f"{source}.pdf"),
        instrument=_expect(data["instrument"], str, f"{source}.instrument"),
        expected_measures=_expect(data["expected_measures"], int, f"{source}.expected_measures"),
        measures_read=measures_read,
        stages=stages,
        suspect_measures=suspects,
        hand_corrected=hand,
        part_ids=ids,
        part_identities=identities,
        tool_versions=dict(versions),
        audiveris_options=None if options is None else dict(options),
    )
    if version >= 4 and (
        _expect(data["review_required"], bool, f"{source}.review_required") != item.review_required
    ):
        raise StatusError(f"{source}: review_required does not match review signals and suspects")
    if count != item.to_dict()["measure_count"]:
        raise StatusError(
            f"{source}: measure_count {count!r} does not match its read count and expected_measures"
        )
    expected_acceptance = item.to_dict()["acceptance"]
    # Old files recorded a boolean for flat lists, even without single-part identity.
    # Validate their original calculation, then normalize to unknown acceptance.
    legacy_acceptance = (
        acceptance_dict(item._acceptance(hand))
        if isinstance(hand, tuple) and (item.part_ids is None or len(item.part_ids) != 1)
        else expected_acceptance
    )
    if data["acceptance"] not in (expected_acceptance, legacy_acceptance):
        raise StatusError(
            f"{source}: acceptance {data['acceptance']!r} does not match hand_corrected and "
            "expected_measures"
        )
    return item


def dumps_status(item: ItemStatus) -> str:
    return json.dumps(item.to_dict(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def write_status(item: ItemStatus, path: Path) -> None:
    """Write status.json atomically (see write_text_atomic)."""
    write_text_atomic(Path(path), dumps_status(item))


def write_text_atomic(path: Path, text: str) -> None:
    """Write a UTF-8 text file atomically: a temp file in the same folder, then os.replace."""
    path = Path(path)
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
