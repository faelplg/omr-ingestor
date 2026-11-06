"""Run the pipeline on EVERY manifest item, then check the parts against each other.

All items of a manifest are parts of the same piece, so after the per-item runs the
measures read per part (status.json's measure_count) are compared with each other and
with each part's expected_measures (docs/architecture.md, "Batch").

Each item goes through pipeline.run_item, exactly as `omr-ingestor run` does, in
manifest order. A failed stage or a setup problem (PipelineSetupError) is recorded and
the next item still runs. The summary goes to `<output_dir>/batch.json`.

A cross-part disagreement is a warning, not a failure: the author fixes it by hand.
The batch passes only when every item passed.
"""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from omr_ingestor.manifest import ManifestError, load_manifest
from omr_ingestor.pipeline import PipelineSetupError, run_item
from omr_ingestor.status import MeasureCount, StageState, write_text_atomic

BATCH_NAME = "batch.json"
SCHEMA_VERSION = 2

PASSED = "passed"
FAILED = "failed"
SETUP_ERROR = "setup_error"


@dataclass(frozen=True, slots=True)
class BatchItem:
    pdf: str
    instrument: str
    expected_measures: int
    status: str  # PASSED, FAILED or SETUP_ERROR
    measures_read: int | None = None  # None when the part was not counted
    suspect_count: int | None = None  # None when the durations stage did not pass
    failed_stage: str | None = None
    error: str | None = None
    status_file: Path | None = None  # None when the item did not start (setup error)
    tool_versions: dict[str, str] = field(default_factory=dict)
    audiveris_options: dict[str, str] | None = None  # None when the item did not start

    @property
    def stem(self) -> str:
        return Path(self.pdf).stem

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    @property
    def measure_count(self) -> MeasureCount | None:
        if self.measures_read is None:
            return None
        return MeasureCount(
            self.measures_read, self.expected_measures, self.expected_measures - self.measures_read
        )


@dataclass(frozen=True, slots=True)
class CrossPartCheck:
    """How the measures read per part compare across the parts of the piece."""

    all_agree: bool  # every part was counted and all read the same number of measures
    # The count most parts read; None when no part was counted or there is a tie.
    reference_count: int | None
    differ_from_others: tuple[str, ...]  # stems; every counted part when there is a tie
    differ_from_expected: tuple[str, ...]  # stems whose count is not their expected_measures
    not_counted: tuple[str, ...]  # stems with no measure count (failed before durations)
    expected_agree: bool  # the manifest gives every part the same expected_measures


@dataclass(frozen=True, slots=True)
class BatchResult:
    title: str
    items: tuple[BatchItem, ...]
    cross_part: CrossPartCheck
    summary_file: Path

    @property
    def ok(self) -> bool:
        return all(item.passed for item in self.items)


def check_cross_part(items: tuple[BatchItem, ...]) -> CrossPartCheck:
    counted = [it for it in items if it.measures_read is not None]
    tally = Counter(it.measures_read for it in counted).most_common()
    reference = None
    if tally and (len(tally) == 1 or tally[0][1] > tally[1][1]):
        reference = tally[0][0]
    if reference is None:
        others = tuple(it.stem for it in counted) if len(tally) > 1 else ()
    else:
        others = tuple(it.stem for it in counted if it.measures_read != reference)
    return CrossPartCheck(
        all_agree=bool(counted) and len(counted) == len(items) and len(tally) == 1,
        reference_count=reference,
        differ_from_others=others,
        differ_from_expected=tuple(
            it.stem for it in counted if it.measures_read != it.expected_measures
        ),
        not_counted=tuple(it.stem for it in items if it.measures_read is None),
        expected_agree=len({it.expected_measures for it in items}) <= 1,
    )


def _run_one(manifest_path: Path, index: int, entry: dict[str, Any], output_dir: Path):
    item = BatchItem(entry["pdf"], entry["instrument"], entry["expected_measures"], SETUP_ERROR)
    try:
        result = run_item(manifest_path, str(index), output_dir)
    except PipelineSetupError as exc:
        return replace(item, error=str(exc))
    status = result.status
    failed = result.failed_stage
    durations = status.stage("durations")
    counted = durations is not None and durations.status is StageState.PASSED
    return replace(
        item,
        status=PASSED if result.ok else FAILED,
        measures_read=status.measures_read,
        suspect_count=len(status.suspect_measures) if counted else None,
        failed_stage=None if failed is None else failed.name,
        error=None if failed is None else failed.detail,
        status_file=result.paths.status,
        tool_versions=dict(status.tool_versions),
        audiveris_options=None
        if status.audiveris_options is None
        else dict(status.audiveris_options),
    )


def run_batch(
    manifest_path: Path,
    output_dir: Path,
    on_item: Callable[[int, int, BatchItem], None] | None = None,
) -> BatchResult:
    """Run every item in manifest order, check the parts, write batch.json.

    on_item(position, total, item) is called after each item (position is 1-based).
    An invalid manifest raises PipelineSetupError before any item runs.
    """
    manifest_path, output_dir = Path(manifest_path), Path(output_dir)
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        raise PipelineSetupError(str(exc)) from exc
    entries = manifest["items"]
    done = []
    for index, entry in enumerate(entries):
        done.append(_run_one(manifest_path, index, entry, output_dir))
        if on_item is not None:
            on_item(index + 1, len(entries), done[-1])
    items = tuple(done)
    result = BatchResult(manifest["title"], items, check_cross_part(items), output_dir / BATCH_NAME)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(result.summary_file, dumps_batch(result, manifest_path))
    return result


def _item_dict(item: BatchItem, output_dir: Path) -> dict[str, Any]:
    count = item.measure_count
    return {
        "stem": item.stem,
        "pdf": item.pdf,
        "instrument": item.instrument,
        "status": item.status,
        "failed_stage": item.failed_stage,
        "error": item.error,
        "measures": {
            "read": item.measures_read,
            "expected": item.expected_measures,
            "missing": None if count is None else count.missing,
        },
        "suspect_count": item.suspect_count,
        "status_file": None
        if item.status_file is None
        else item.status_file.relative_to(output_dir).as_posix(),
        "tool_versions": dict(sorted(item.tool_versions.items())),
        "audiveris_options": None
        if item.audiveris_options is None
        else dict(sorted(item.audiveris_options.items())),
    }


def dumps_batch(result: BatchResult, manifest_path: Path) -> str:
    output_dir = result.summary_file.parent
    check = result.cross_part
    data = {
        "schema_version": SCHEMA_VERSION,
        "manifest": str(manifest_path),
        "title": result.title,
        "passed": result.ok,
        "items": [_item_dict(item, output_dir) for item in result.items],
        "cross_part": {
            "all_agree": check.all_agree,
            "reference_count": check.reference_count,
            "differ_from_others": list(check.differ_from_others),
            "differ_from_expected": list(check.differ_from_expected),
            "not_counted": list(check.not_counted),
            "expected_agree": check.expected_agree,
        },
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
