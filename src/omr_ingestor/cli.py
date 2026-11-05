"""Command-line entry point: `omr-ingestor`."""

import argparse
import sys
from pathlib import Path

from omr_ingestor.instruments import InstrumentsError, load_instruments
from omr_ingestor.manifest import ManifestError, load_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omr-ingestor")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="validate a music.json manifest")
    val.add_argument("manifest", type=Path)
    sub.add_parser("instruments", help="list the instruments from instruments.json")
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            data = load_manifest(args.manifest)
            print(f"{args.manifest}: OK ({len(data['items'])} item(s))")
        else:
            for inst in load_instruments():
                t = inst.transpose
                print(
                    f"{inst.id}\t{inst.clef}\t"
                    f"diatonic={t.diatonic} chromatic={t.chromatic} octave={t.octave_change}"
                )
    except (ManifestError, InstrumentsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
