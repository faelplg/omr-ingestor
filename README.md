# omr-ingestor

Local pipeline that takes a sheet-music PDF (one part, for one of 6 wind instruments), runs OMR on it, and produces a MuseScore `.mscz` file with the right instrument (transposition, clef, name) and the "Estilo 2026" style applied.

## Status

Every stage of `docs/architecture.md` is implemented, per item (`run`) and over a whole manifest (`batch`). The automated tests use fake Audiveris and MuseScore executables; what was measured with the real tools is listed in `docs/architecture.md`.

## How it runs

- Runs on the author's Mac (arm64), from the command line.
- Input: a `music.json` manifest (see `docs/music.schema.json` and `examples/music.example.json`) that lists the PDFs, the instrument of each one (provided at submission time, never guessed) and the expected number of measures.
- Output: one `.mscz` per part plus a `status.json` per item, in `output/` (kept out of git).
- Stages and decisions: `docs/architecture.md`.

## Usage

Install with `uv sync`. The tools come from environment variables: `OMR_AUDIVERIS`, `OMR_MUSESCORE` and `OMR_MUSICXML_XSD` (the local MusicXML 4.0 XSD); `xmllint` must be on the `PATH`.

```sh
uv run omr-ingestor validate music.json          # check the manifest
uv run omr-ingestor run -o output music.json 0   # one item: index, pdf or pdf stem
uv run omr-ingestor batch -o output music.json   # every item, then the cross-part check
```

### batch

Runs every item of the manifest in order, exactly as `run` does, and writes each item's `output/<pdf stem>/status.json`. A failing item does not stop the others. Since all items are parts of the same piece, it then compares the measures read per part with each other and with each part's `expected_measures`.

- `output/batch.json`: per item its stem, instrument, status (`passed`, `failed` or `setup_error`), measures read/expected/missing and suspect count; plus `cross_part`: whether all parts agree, the count most parts read, and which parts differ from the others or from their expected value.
- stdout: one line per item as it finishes, then a summary table. Disagreements are printed as warnings on stderr.
- Exit code: 0 only if every item passed. A measure-count disagreement is a warning, not a failure: the author fixes it by hand.

## PDFs never leave the Mac

The PDFs are the author's own (purchased or from a publisher). They are not committed (`.gitignore` ignores `*.pdf`) and are never sent to a server or to another machine. Tests outside the Mac use only public-domain or synthetic files.

## External tools and licenses

The pipeline calls separately installed programs from the command line. None of their code is copied or modified here.

| Tool | Target version | License | Use |
|---|---|---|---|
| Audiveris | 5.11.0 | AGPL-3.0 | OMR: PDF → MusicXML (`-batch -export`) |
| MuseScore Studio | 4.7.5 | GPL v3 | MusicXML → `.mscz` with style (`-S`, `-o`) |

If this ever becomes a service for other people, review Audiveris's AGPL first.
