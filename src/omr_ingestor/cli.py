"""Command-line entry point: `omr-ingestor`."""

import argparse
import sys
from pathlib import Path

from omr_ingestor.instruments import InstrumentsError, load_instruments
from omr_ingestor.manifest import ManifestError, load_manifest
from omr_ingestor.pipeline import PipelineSetupError, RunResult, run_item
from omr_ingestor.status import StageState


def _print_run(result: RunResult) -> None:
    for stage in result.status.stages:
        took = "" if stage.duration_s is None else f" ({stage.duration_s:.1f}s)"
        line = f"{stage.name}: {stage.status.value}{took}"
        if stage.detail and stage.status is StageState.PASSED:
            line += f" - {stage.detail}"
        print(line)
    print(f"status: {result.paths.status}")
    failed = result.failed_stage
    if failed is not None:
        print(f"error: stage {failed.name} failed: {failed.detail}", file=sys.stderr)
    else:
        print(f"output: {result.paths.mscz}")


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
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            data = load_manifest(args.manifest)
            print(f"{args.manifest}: OK ({len(data['items'])} item(s))")
        elif args.command == "run":
            result = run_item(args.manifest, args.item, args.output)
            _print_run(result)
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
