"""Command-line entry point: `omr-ingestor`."""

import argparse
import sys
from pathlib import Path

from omr_ingestor.batch import BatchItem, BatchResult, run_batch
from omr_ingestor.instruments import InstrumentsError, load_instruments
from omr_ingestor.manifest import ManifestError, load_manifest
from omr_ingestor.musicxml import MusicXMLError
from omr_ingestor.pipeline import PipelineSetupError, RunResult, UncountedReason, run_item
from omr_ingestor.review import record_review
from omr_ingestor.status import StageState, StatusError, dumps_status


def _print_run(result: RunResult) -> None:
    for stage in result.status.stages:
        took = "" if stage.duration_s is None else f" ({stage.duration_s:.1f}s)"
        line = f"{stage.name}: {stage.status.value}{took}"
        if stage.detail and stage.status is StageState.PASSED:
            line += f" - {stage.detail}"
        print(line)
    print(f"review required: {str(result.status.review_required).lower()}")
    for signal in result.status.review_signals:
        print(f"warning: {signal.code}: {signal.detail}", file=sys.stderr)
    print(f"status: {result.paths.status}")
    failed = result.failed_stage
    if failed is not None:
        print(f"error: stage {failed.name} failed: {failed.detail}", file=sys.stderr)
    else:
        print(f"output: {result.paths.mscz}")


def _describe_item(item: BatchItem) -> str:
    if item.passed:
        return item.status + (" (review required)" if item.review_required else "")
    if item.failed_stage is not None:
        return f"{item.status} at {item.failed_stage}: {item.error}"
    return f"{item.status}: {item.error}"


def _print_item(position: int, total: int, item: BatchItem) -> None:
    print(f"[{position}/{total}] {item.stem}: {_describe_item(item)}", flush=True)


def _cell(value: object) -> str:
    return "-" if value is None else str(value)


def _print_batch(result: BatchResult) -> None:
    rows = [("item", "instrument", "status", "read", "expected", "missing", "suspects")]
    for item in result.items:
        count = item.measure_count
        status = (
            item.status if item.failed_stage is None else f"{item.status} ({item.failed_stage})"
        )
        if item.passed and item.review_required:
            status += " (review required)"
        rows.append(
            (
                item.stem,
                item.instrument,
                status,
                _cell(item.measures_read),
                str(item.expected_measures),
                _cell(None if count is None else count.missing),
                _cell(item.suspect_count),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    print()
    for row in rows:
        text = [c.ljust(w) for c, w in zip(row[:3], widths[:3], strict=True)]
        text += [c.rjust(w) for c, w in zip(row[3:], widths[3:], strict=True)]
        print("  ".join(text).rstrip())
    print()

    for item in result.items:
        if item.acceptance is not None:
            entries = (
                item.acceptance
                if isinstance(item.acceptance, dict)
                else {
                    "legacy item-wide" if item.acceptance.accepted is None else "": item.acceptance
                }
            )
            for part_id, acceptance in entries.items():
                detail = (
                    "not reviewed"
                    if acceptance is None
                    else (
                        f"{acceptance.hand_corrected_count}/{acceptance.expected_measures} "
                        f"({acceptance.ratio:.2%}), "
                        + (
                            "per-part acceptance unknown"
                            if acceptance.accepted is None
                            else "accepted"
                            if acceptance.accepted
                            else "above 5% limit"
                        )
                    )
                )
                label = f" {part_id}" if part_id else ""
                print(f"hand corrections: {item.stem}{label}: {detail}")
        for signal in item.review_signals:
            print(f"warning: {item.stem}: {signal.code}: {signal.detail}", file=sys.stderr)
    check = result.cross_part
    reads = {it.stem: it.measures_read for it in result.items}
    expected = {it.stem: it.expected_measures for it in result.items}
    if check.all_agree:
        print(
            f"cross-part: all {len(result.items)} part(s) read {check.reference_count} measure(s)"
        )
    if check.differ_from_others:
        others = "" if check.reference_count is None else f" (most read {check.reference_count})"
        listed = ", ".join(f"{s} read {reads[s]}" for s in check.differ_from_others)
        print(
            f"warning: the parts disagree on the measure count{others}: {listed}", file=sys.stderr
        )
    if check.differ_from_expected:
        listed = ", ".join(
            f"{s} read {reads[s]}, expected {expected[s]}" for s in check.differ_from_expected
        )
        print(f"warning: measure count differs from expected_measures: {listed}", file=sys.stderr)
    if check.not_counted:
        uncounted = [it for it in result.items if it.stem in check.not_counted]
        for reason in UncountedReason:
            stems = [it.stem for it in uncounted if it.uncounted_reason is reason]
            if stems:
                print(f"warning: {reason}: {', '.join(stems)}", file=sys.stderr)
    if not check.expected_agree:
        listed = ", ".join(f"{s} {n}" for s, n in expected.items())
        print(f"warning: the manifest expects different measure counts: {listed}", file=sys.stderr)
    print(f"summary: {result.summary_file}")
    failed = [it for it in result.items if not it.passed]
    if failed:
        print(
            f"error: {len(failed)} of {len(result.items)} item(s) did not pass: "
            + ", ".join(it.stem for it in failed),
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr-ingestor")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="validate a music.json manifest")
    val.add_argument("manifest", type=Path)
    sub.add_parser("instruments", help="list the instruments from instruments.json")
    run = sub.add_parser(
        "run",
        help="run the whole pipeline on one manifest item",
        description=(
            "Run every stage on one item and write <output>/<pdf stem>/status.json after "
            "each stage. Tools come from OMR_AUDIVERIS, OMR_MUSESCORE and OMR_MUSICXML_XSD."
        ),
    )
    run.add_argument("manifest", type=Path)
    run.add_argument("item", help="0-based item index, the item's pdf value, or the pdf stem")
    run.add_argument("-o", "--output", type=Path, required=True, help="output folder")
    batch = sub.add_parser(
        "batch",
        help="run the whole pipeline on every manifest item and compare the parts",
        description=(
            "Run every item in manifest order, as `run` does; a failing item does not stop "
            "the others. Then compare the measures read per part with each other and with "
            "expected_measures, and write <output>/batch.json. A measure-count disagreement "
            "is a warning; the exit code is 0 only if every item passed."
        ),
    )
    batch.add_argument("manifest", type=Path)
    batch.add_argument("-o", "--output", type=Path, required=True, help="output folder")
    review = sub.add_parser("review", help="record hand-corrected measures after source review")
    review.add_argument("item_dir", type=Path, help="item folder containing status.json")
    review.add_argument(
        "measures", nargs="*", help="measure numbers exactly as written in MusicXML"
    )
    review.add_argument("--part", help="MusicXML part ID; required for multipart items")
    review.add_argument("--replace", action="store_true", help="replace the part's correction list")
    review.add_argument(
        "--dry-run", action="store_true", help="print updated status without writing"
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            data = load_manifest(args.manifest)
            print(f"{args.manifest}: OK ({len(data['items'])} item(s))")
        elif args.command == "run":
            result = run_item(args.manifest, args.item, args.output)
            _print_run(result)
            return 0 if result.ok else 1
        elif args.command == "review":
            try:
                result = record_review(
                    args.item_dir,
                    args.measures,
                    part_id=args.part,
                    replace_existing=args.replace,
                    dry_run=args.dry_run,
                )
            except (StatusError, MusicXMLError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(dumps_status(result), end="")
            if args.replace and any(stage.status is StageState.FAILED for stage in result.stages):
                print("hint: rerun to complete failed stages", file=sys.stderr)
        elif args.command == "batch":
            result = run_batch(args.manifest, args.output, on_item=_print_item)
            _print_batch(result)
            return 0 if result.ok else 1
        else:
            for inst in load_instruments():
                t = inst.transpose
                print(
                    f"{inst.id}\t{inst.clef}\t"
                    f"diatonic={t.diatonic} chromatic={t.chromatic} octave={t.octave_change}"
                )
    except (ManifestError, InstrumentsError, PipelineSetupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
