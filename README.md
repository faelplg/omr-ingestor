# omr-ingestor

Local pipeline that takes a sheet-music PDF (one part, for one of 6 wind instruments), runs OMR on it, and produces a MuseScore `.mscz` file with the right instrument (transposition, clef, name) and the "Estilo 2026" style applied.

## Status

**Skeleton, nothing implemented.** This repo only holds documentation, the instrument table and the manifest schema. No script runs yet.

## How it will run (planned)

- Runs on the author's Mac (arm64), from the command line.
- Input: a `music.json` manifest (see `docs/music.schema.json` and `examples/music.example.json`) that lists the PDFs, the instrument of each one (provided at submission time, never guessed) and the expected number of measures.
- Output: one `.mscz` per part plus a `status.json` per item, in `output/` (kept out of git).
- Stages and decisions: `docs/architecture.md`.

## PDFs never leave the Mac

The PDFs are the author's own (purchased or from a publisher). They are not committed (`.gitignore` ignores `*.pdf`) and are never sent to a server or to another machine. Tests outside the Mac use only public-domain or synthetic files.

## External tools and licenses

The pipeline calls separately installed programs from the command line. None of their code is copied or modified here.

| Tool | Target version | License | Use |
|---|---|---|---|
| Audiveris | 5.11.0 | AGPL-3.0 | OMR: PDF → MusicXML (`-batch -export`) |
| MuseScore Studio | 4.7.5 | GPL v3 | MusicXML → `.mscz` with style (`-S`, `-o`) |

If this ever becomes a service for other people, review Audiveris's AGPL first.
