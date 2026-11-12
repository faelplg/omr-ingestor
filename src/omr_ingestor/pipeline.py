"""Run every stage of the pipeline on ONE manifest item (docs/architecture.md).

Layout of the per-item folder `<output_dir>/<pdf stem>/`:

    status.json           rewritten (atomically) after every stage
    audiveris/            Audiveris output and the unzipped raw MusicXML
    <stem>.musicxml       the MusicXML after the instrument fix-up
    <stem>.mscz           the MuseScore file with the style applied, published only
                          after the final check passes
    <stem>.staged.mscz    the new MuseScore output while it waits for the final check
    <stem>.rejected.mscz  the last output that failed the final check, kept to inspect

Everything is recomputed on each run, from the PDF, so a rerun gives the same files.
The human review (hand_corrected) is kept from the previous status.json.

Problems with the setup (manifest, item selector, tools, XSD, style, PDF, sha256) raise
PipelineSetupError before any stage runs and leave status.json untouched. A stage that
fails stops the item: it is recorded as failed, the stages after it as skipped.
"""

import hashlib
import os
import shutil
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from omr_ingestor import mscx_check, musescore
from omr_ingestor.audiveris import AudiverisError, resolve_audiveris, run_audiveris
from omr_ingestor.durations import (
    DurationReport,
    ReviewSignal,
    check_durations,
    check_review_signals,
)
from omr_ingestor.fixup import fix_instrument, strip_text
from omr_ingestor.instruments import Instrument, InstrumentsError, load_instruments
from omr_ingestor.manifest import ManifestError, load_manifest
from omr_ingestor.mscx_check import A5_LANDSCAPE, MM_PER_INCH, MscxCheckError, PageSize
from omr_ingestor.musescore import MuseScoreError, resolve_musescore
from omr_ingestor.musicxml import MusicXMLError, part_ids, read_musicxml, write_musicxml
from omr_ingestor.status import (
    STAGE_NAMES,
    ItemStatus,
    PartIdentity,
    Stage,
    StageState,
    StatusError,
    SuspectMeasure,
    read_status,
    write_status,
)
from omr_ingestor.xsd import XsdError, resolve_xsd_path, validate_xsd

STATUS_NAME = "status.json"
AUDIVERIS_DIR = "audiveris"
XMLLINT = "xmllint"

# Errors that mean "this stage failed"; anything else is a bug and propagates.
_STAGE_ERRORS = (
    AudiverisError,
    XsdError,
    MusicXMLError,
    MuseScoreError,
    MscxCheckError,
    OSError,
)


class PipelineSetupError(Exception):
    """The item cannot start: bad manifest or selector, missing tool, file or checksum."""


class PipelineIOError(OSError):
    """I/O failed after this invocation successfully wrote its initial status."""

    def __init__(self, message: str, *, status_file: Path) -> None:
        super().__init__(message)
        self.status_file = status_file


class StageError(Exception):
    """A stage ran but its result is not acceptable."""


@dataclass(frozen=True, slots=True)
class Tools:
    audiveris: Path
    musescore: Path
    xsd: Path


@dataclass(frozen=True, slots=True)
class ItemPaths:
    item_dir: Path
    status: Path
    audiveris_dir: Path
    musicxml: Path
    mscz: Path
    staged_mscz: Path
    rejected_mscz: Path

    @classmethod
    def for_item(cls, output_dir: Path, pdf: str) -> "ItemPaths":
        stem = Path(pdf).stem
        item_dir = Path(output_dir) / stem
        return cls(
            item_dir=item_dir,
            status=item_dir / STATUS_NAME,
            audiveris_dir=item_dir / AUDIVERIS_DIR,
            musicxml=item_dir / f"{stem}.musicxml",
            mscz=item_dir / f"{stem}.mscz",
            staged_mscz=item_dir / f"{stem}.staged.mscz",
            rejected_mscz=item_dir / f"{stem}.rejected.mscz",
        )


class UncountedReason(StrEnum):
    BEFORE_DURATIONS = "measures not counted (failed before durations)"
    MISSING_PARTS = 'multipart score requires the item\'s "parts" configuration'
    DIFFERENT_COUNTS = "parts read different measure counts"
    NO_PARTS = "score has no parts"


@dataclass(frozen=True, slots=True)
class RunResult:
    paths: ItemPaths
    status: ItemStatus
    uncounted_reason: UncountedReason | None = UncountedReason.BEFORE_DURATIONS

    @property
    def ok(self) -> bool:
        return all(s.status is StageState.PASSED for s in self.status.stages)

    @property
    def failed_stage(self) -> Stage | None:
        return next((s for s in self.status.stages if s.status is StageState.FAILED), None)


def select_item(manifest: dict[str, Any], selector: str) -> tuple[int, dict[str, Any]]:
    """Find an item by 0-based index, by its exact `pdf` value, or by the PDF file stem."""
    items = manifest["items"]
    if selector.isdigit():
        index = int(selector)
        if index >= len(items):
            raise PipelineSetupError(
                f"item index {index} is out of range; the manifest has {len(items)} item(s)"
            )
        return index, items[index]
    for matches in (
        [i for i, it in enumerate(items) if it["pdf"] == selector],
        [i for i, it in enumerate(items) if Path(it["pdf"]).stem == selector],
    ):
        if len(matches) == 1:
            return matches[0], items[matches[0]]
        if len(matches) > 1:
            raise PipelineSetupError(
                f"item selector {selector!r} is ambiguous: it matches items {matches}"
            )
    known = ", ".join(it["pdf"] for it in items)
    raise PipelineSetupError(
        f"no item matches {selector!r}; use an index (0-{len(items) - 1}), a pdf or a pdf "
        f"stem from: {known}"
    )


def resolve_input_path(raw: str, base: Path) -> Path:
    """Expand ~ and resolve a relative path against base (the manifest folder)."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else Path(base) / path


def resolve_tools() -> Tools:
    """The tools from $OMR_AUDIVERIS, $OMR_MUSESCORE and $OMR_MUSICXML_XSD, plus xmllint."""
    problems = []
    found: dict[str, Path] = {}
    for name, resolve, errors in (
        ("audiveris", resolve_audiveris, AudiverisError),
        ("musescore", resolve_musescore, MuseScoreError),
        ("xsd", resolve_xsd_path, XsdError),
    ):
        try:
            found[name] = resolve()
        except errors as exc:
            problems.append(str(exc))
    if shutil.which(XMLLINT) is None:
        problems.append(f"{XMLLINT} not found; it ships with libxml2")
    if problems:
        raise PipelineSetupError("; ".join(problems))
    return Tools(**found)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def style_page_size(style: Path) -> PageSize:
    """The page size set by the .mss style; A5 landscape (Estilo 2026) if it sets none."""
    try:
        root = ET.parse(style).getroot()
    except ET.ParseError as exc:
        raise PipelineSetupError(f"style {style} is not well-formed XML: {exc}") from exc
    el = root.find("Style")
    width = el.findtext("pageWidth") if el is not None else None
    height = el.findtext("pageHeight") if el is not None else None
    if width is None or height is None:
        return A5_LANDSCAPE
    try:
        return PageSize(float(width) * MM_PER_INCH, float(height) * MM_PER_INCH, style.stem)
    except ValueError:
        raise PipelineSetupError(
            f"style {style}: page size {width!r} x {height!r} is not a number"
        ) from None


def _previous_status(path: Path, pdf: str) -> ItemStatus | None:
    if not path.exists():
        return None
    try:
        previous = read_status(path)
    except StatusError as exc:
        raise PipelineSetupError(
            f"cannot reuse {path}: {exc}; fix or remove it (it may hold the human review)"
        ) from exc
    if previous.pdf != pdf:
        raise PipelineSetupError(
            f"{path} belongs to {previous.pdf!r}, not {pdf!r}; two items share the PDF stem"
        )
    return previous


@dataclass
class _Context:
    tools: Tools
    instrument: Instrument
    pdf: Path
    style: Path
    page_size: PageSize
    paths: ItemPaths
    status: ItemStatus
    parts: tuple[str, ...] | None = None
    keep_text: bool = False
    raw_musicxml: Path | None = None
    report: DurationReport | None = None
    uncounted_reason: UncountedReason | None = UncountedReason.BEFORE_DURATIONS

    def save(self) -> None:
        write_status(self.status, self.paths.status)

    def set_version(self, tool: str, version: str | None) -> None:
        if version:
            versions = {**self.status.tool_versions, tool: version}
            self.status = replace(self.status, tool_versions=versions)


def _audiveris(ctx: _Context) -> str:
    result = run_audiveris(
        ctx.pdf,
        ctx.paths.audiveris_dir,
        audiveris=ctx.tools.audiveris,
        options=ctx.status.audiveris_options,
    )
    ctx.set_version("audiveris", result.version)
    files = result.musicxml_files
    if len(files) != 1:
        names = ", ".join(p.name for p in files)
        raise StageError(
            f"Audiveris produced {len(files)} movements ({names}); only one per PDF is supported"
        )
    ctx.raw_musicxml = files[0]
    return f"{files[0].name}, exit code {result.exit_code}"


def _xsd(ctx: _Context) -> str:
    validate_xsd(ctx.raw_musicxml, ctx.tools.xsd, xmllint=XMLLINT)
    return "valid"


def _durations(ctx: _Context) -> str:
    root = read_musicxml(ctx.raw_musicxml)
    ids = tuple(part_ids(root))
    identities = {
        part.get("id", ""): PartIdentity(
            part_name=part.findtext("part-name") or "",
            instruments=tuple(
                instrument.findtext("instrument-name") or ""
                for instrument in part.findall("score-instrument")
            ),
        )
        for part in root.findall("part-list/score-part")
    }
    if isinstance(ctx.status.hand_corrected, dict) and ctx.status.part_identities is not None:
        mismatches = tuple(
            ReviewSignal(
                code="part_identity",
                part_id=pid,
                detail=(
                    f"part {pid} identity changed: recorded {ctx.status.part_identities[pid]!r}, "
                    f"current {identities.get(pid)!r}; hand corrections withheld, "
                    "review this part against the source again"
                ),
            )
            for pid in ctx.status.hand_corrected
            if pid not in ids or identities.get(pid) != ctx.status.part_identities[pid]
        )
        if mismatches:
            ctx.status = replace(ctx.status, review_signals=mismatches)
            raise StageError(
                "part identity changed; recorded hand corrections require review again"
            )
    if not set(ctx.status.part_ids or ()).issubset(ids):
        raise StageError(
            "recorded part IDs do not match the current score: prior parts are missing"
        )
    signals = check_review_signals(
        root, ctx.status.expected_measures, len(ctx.parts) if ctx.parts is not None else 1
    )
    if isinstance(ctx.status.hand_corrected, dict) and (
        len(ids) < 2 or any(signal.code == "part_identity" for signal in signals)
    ):
        raise StageError("recorded hand_corrected part IDs do not match the current score")
    report = check_durations(root)
    ctx.report = report
    suspects = report.suspects
    counts = [len(part.findall("measure")) for part in root.findall("part")]
    # A shared count lets batch compare multipart items with the rest of the piece.
    ctx.status = replace(
        ctx.status,
        suspect_measures=tuple(SuspectMeasure(m.number, m.describe()) for m in suspects),
        review_signals=signals,
        measures_read=counts[0]
        if len(counts) == 1 or (ctx.parts is not None and len(set(counts)) == 1)
        else None,
    )
    if ctx.status.measures_read is not None:
        ctx.uncounted_reason = None
    elif not counts:
        ctx.uncounted_reason = UncountedReason.NO_PARTS
    elif ctx.parts is None:
        ctx.uncounted_reason = UncountedReason.MISSING_PARTS
    else:
        ctx.uncounted_reason = UncountedReason.DIFFERENT_COUNTS
    if any(signal.code == "part_identity" for signal in ctx.status.review_signals):
        raise StageError(
            "unsafe part identity: part IDs must be unique and match the score-part "
            "declarations; correct the MusicXML part IDs or rerun OMR before instrument fixup"
        )
    ctx.status = replace(
        ctx.status,
        part_ids=ids,
        # Do not retroactively attach new metadata to legacy review records.
        part_identities=None
        if ctx.status.part_identities is None and ctx.status.hand_corrected is not None
        else identities,
    )
    expected = ctx.status.expected_measures
    read = "/".join(map(str, counts)) or "0"
    if ctx.parts is not None:
        details = []
        for part, count in zip(root.findall("part"), counts, strict=True):
            missing = expected - count
            difference = f"{missing} missing" if missing >= 0 else f"{-missing} extra"
            details.append(
                f"part {part.get('id', '')}: {count} measure(s) read, "
                f"{expected} expected, {difference}"
            )
        return "; ".join([f"{len(suspects)} suspect measure(s)", *details])
    detail = f"{len(suspects)} suspect measure(s); {read} measure(s) read, {expected} expected"
    if (count := ctx.status.measure_count) is not None:
        if count.missing >= 0:
            detail += f", {count.missing} missing"
        else:
            detail += f", {-count.missing} extra"
    elif any(c != expected for c in counts):
        detail += " (count differs)"
    return detail


def _annotate_review_signals(root: ET.Element, signals: tuple[ReviewSignal, ...]) -> None:
    """Insert warnings in signal order, with score-wide warnings in the first part."""
    parts = root.findall("part")
    # Count warnings belong to the whole part, so anchor them at its first bar.
    # The annotation deliberately makes no claim about where recognition lost content.
    insertion_positions: dict[ET.Element, int] = {}
    for signal in signals:
        for part in parts if signal.part_id else parts[:1]:
            if signal.part_id and signal.part_id != part.get("id", ""):
                continue
            measures = part.findall("measure")
            target_measure = (
                next((m for m in measures if m.get("number", "") == signal.measure), None)
                if signal.measure
                else next(iter(measures), None)
            )
            if target_measure is not None:
                direction = ET.Element("direction", placement="above")
                ET.SubElement(ET.SubElement(direction, "direction-type"), "words").text = (
                    "Review: " + signal.detail
                )
                if target_measure not in insertion_positions:
                    at = 0
                    while at < len(target_measure) and target_measure[at].tag in (
                        "print",
                        "attributes",
                    ):
                        at += 1
                    insertion_positions[target_measure] = at
                at = insertion_positions[target_measure]
                target_measure.insert(at, direction)
                insertion_positions[target_measure] = at + 1


def _fixup(ctx: _Context) -> str:
    root = read_musicxml(ctx.raw_musicxml)
    parts = root.findall("part")
    if not parts:
        raise StageError("score has no parts")
    if ctx.parts is None:
        if len(parts) != 1:
            raise StageError(
                f"score has {len(parts)} parts {part_ids(root)}; "
                'list their names in the item\'s "parts"'
            )
        names = [None]
    else:
        if len(parts) != len(ctx.parts):
            raise StageError(
                f"score has {len(parts)} parts; manifest parts expects {len(ctx.parts)}"
            )
        names = ctx.parts
    removals = strip_text(root, keep_text=ctx.keep_text)
    details = []
    added = []
    for part, name in zip(parts, names, strict=True):
        part_id = part.get("id", "")
        result = fix_instrument(
            root,
            ctx.instrument,
            part_id=part_id,
            part_name=name,
            suspect_measures=ctx.report.suspect_numbers(part_id),
        )
        prefix = f"part {part_id}: " if ctx.parts is not None else ""
        added.extend(SuspectMeasure(s.measure, prefix + s.reason) for s in result.suspects)
        details.append(
            f"{ctx.instrument.id} applied to part {part_id}, {len(result.marked)} mark(s)"
        )
        details.extend(prefix + r.describe() for r in result.rewrites)
    _annotate_review_signals(root, ctx.status.review_signals)
    target = ctx.paths.musicxml
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        write_musicxml(root, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    ctx.status = replace(
        ctx.status,
        text_removals=removals,
        suspect_measures=ctx.status.suspect_measures + tuple(added),
    )
    return "; ".join(details)


def _xsd_after_fixup(ctx: _Context) -> str:
    validate_xsd(ctx.paths.musicxml, ctx.tools.xsd, xmllint=XMLLINT)
    return "valid"


def _musescore(ctx: _Context) -> str:
    # Staged, so a bad conversion never replaces the last good .mscz (see _mscx_check).
    staged = ctx.paths.staged_mscz
    result = musescore.convert_to_mscz(
        ctx.paths.musicxml, ctx.style, staged, musescore=ctx.tools.musescore
    )
    ctx.set_version("musescore", result.version)
    return f"{staged.name}, exit code {result.exit_code}"


def _mscx_check(ctx: _Context) -> str:
    """Check the staged .mscz; publish it on success, move it aside as rejected otherwise."""
    paths = ctx.paths
    try:
        result = mscx_check.check_mscz(
            paths.staged_mscz, ctx.instrument, page_size=ctx.page_size, parts=ctx.parts
        )
        if not result.passed:
            raise StageError("; ".join(c.describe() for c in result.failures))
    except (MscxCheckError, StageError) as exc:
        os.replace(paths.staged_mscz, paths.rejected_mscz)
        raise StageError(f"{exc} (kept as {paths.rejected_mscz.name})") from exc
    os.replace(paths.staged_mscz, paths.mscz)
    paths.rejected_mscz.unlink(missing_ok=True)
    return "; ".join(c.describe() for c in result.checks)


_STAGES: tuple[tuple[str, Callable[[_Context], str]], ...] = (
    ("audiveris", _audiveris),
    ("xsd", _xsd),
    ("durations", _durations),
    ("fixup", _fixup),
    ("xsd_after_fixup", _xsd_after_fixup),
    ("musescore", _musescore),
    ("mscx_check", _mscx_check),
)


def _prepare(manifest_path: Path, selector: str, output_dir: Path) -> _Context:
    try:
        manifest = load_manifest(manifest_path)
        instruments = load_instruments()
    except (ManifestError, InstrumentsError) as exc:
        raise PipelineSetupError(str(exc)) from exc
    _, item = select_item(manifest, selector)
    base = Path(manifest_path).parent
    pdf = resolve_input_path(item["pdf"], base)
    if not pdf.is_file():
        raise PipelineSetupError(f"PDF not found: {pdf}")
    actual = sha256_of(pdf)
    if actual != item["sha256"]:
        raise PipelineSetupError(f"{pdf}: sha256 is {actual}, the manifest says {item['sha256']}")
    style = resolve_input_path(manifest["style"], base)
    if not style.is_file():
        raise PipelineSetupError(f"style file not found: {style}")
    tools = resolve_tools()

    paths = ItemPaths.for_item(output_dir, item["pdf"])
    previous = _previous_status(paths.status, item["pdf"])
    status = ItemStatus(
        pdf=item["pdf"],
        instrument=item["instrument"],
        expected_measures=item["expected_measures"],
        stages=tuple(Stage(name, StageState.PENDING) for name in STAGE_NAMES),
        hand_corrected=previous.hand_corrected if previous else None,
        part_ids=previous.part_ids if previous else None,
        part_identities=previous.part_identities if previous else None,
        # Keep invalidated reviews unknown until the new score passes identity checks.
        review_signals=tuple(
            signal for signal in previous.review_signals if signal.code == "part_identity"
        )
        if previous and previous.part_identities is not None
        else (),
        audiveris_options=dict(item.get("audiveris_options", {})),
    )
    return _Context(
        tools=tools,
        keep_text=item.get("keep_text", False),
        parts=tuple(item["parts"]) if "parts" in item else None,
        instrument=instruments[item["instrument"]],
        pdf=pdf,
        style=style,
        page_size=style_page_size(style),
        paths=paths,
        status=status,
    )


def run_item(manifest_path: Path, selector: str, output_dir: Path) -> RunResult:
    """Run all stages on one item; stop at the first failure. See the module docstring."""
    ctx = _prepare(Path(manifest_path), selector, Path(output_dir))
    ctx.paths.item_dir.mkdir(parents=True, exist_ok=True)
    ctx.save()
    try:
        return _run_stages(ctx)
    except OSError as exc:
        raise PipelineIOError(str(exc), status_file=ctx.paths.status) from exc


def _run_stages(ctx: _Context) -> RunResult:
    for i, (name, stage) in enumerate(_STAGES):
        start = time.monotonic()
        try:
            detail = stage(ctx)
        except (*_STAGE_ERRORS, StageError) as exc:
            ctx.status = ctx.status.with_stage(
                Stage(name, StageState.FAILED, time.monotonic() - start, str(exc))
            )
            for later, _ in _STAGES[i + 1 :]:
                ctx.status = ctx.status.with_stage(Stage(later, StageState.SKIPPED))
            ctx.save()
            break
        ctx.status = ctx.status.with_stage(
            Stage(name, StageState.PASSED, time.monotonic() - start, detail)
        )
        ctx.save()
    return RunResult(ctx.paths, ctx.status, ctx.uncounted_reason)
