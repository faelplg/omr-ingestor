# omr-ingestor

Local pipeline that takes a sheet-music PDF (one or more parts, for one of 6 wind instruments), runs OMR on it, and produces a MuseScore `.mscz` file with the right instrument (transposition, clef, name) and the "Estilo 2026" style applied.

## Status

Every stage of `docs/architecture.md` is implemented, per item (`run`) and over a whole manifest (`batch`). The automated tests use fake Audiveris and MuseScore executables; what was measured with the real tools is listed in `docs/architecture.md`.

## How it runs

- Runs on the author's Mac (arm64), from the command line.
- Input: a `music.json` manifest (see `docs/music.schema.json` and `examples/music.example.json`) that lists the PDFs, the instrument of each one (provided at submission time, never guessed) and the expected number of measures.
- Output: one `.mscz` per PDF plus a `status.json` per item, in `output/` (kept out of git).
- Stages and decisions: `docs/architecture.md`.
- [Known accuracy limits](docs/accuracy.md): what review signals catch, tested
  Audiveris mitigations, and the <=5% hand-correction acceptance criterion.

## Usage

Install with `uv sync`. The tools come from environment variables: `OMR_AUDIVERIS`, `OMR_MUSESCORE` and `OMR_MUSICXML_XSD` (the local MusicXML 4.0 XSD); `xmllint` must be on the `PATH`.

```sh
uv run omr-ingestor validate music.json          # check the manifest
uv run omr-ingestor run -o output music.json 0   # one item: index, pdf or pdf stem
uv run omr-ingestor batch -o output music.json   # every item, then the cross-part check
```

### Multiple parts in one PDF

An item can supply `"parts": ["Trumpet 1", "Trumpet 2"]` (see the trumpet item in
`examples/music.example.json`). This non-empty list names parts in score order.
The score must contain exactly that many parts; a mismatch fails the fixup stage.
Each part receives the item's instrument, clef, transposition and sound, with its
configured display name. Each part must have one staff. Omitting `parts` keeps the
single-part behavior and the instrument's default name.

`expected_measures` applies to each part. Duration details and suspects identify
part IDs and report missing or extra measures for each part without failing the run.
When all parts have the same count, that count is recorded as `measure_count.read`
in `status.json` and `measures.read` in the batch summary, and included in the batch
cross-part comparison. If counts differ, status's `measure_count` and batch's
`measures.read` are `null`, and batch lists the item as `not_counted`; consult the
durations detail for each count. The final MuseScore
check verifies every configured part's name, instrument, transposition and clef.
One `.mscz` contains all parts.

### Review signals

A successful conversion still needs source review. `status.json` (schema 5) and
`batch.json` items (schema 3) expose `review_required` and `review_signals`, independently
of stage success and duration suspects. Each signal contains `code`, `detail`,
`part_id` and `measure` (empty strings mean score-wide or part-wide scope).

- `measure_count_mismatch`: each part is compared with `expected_measures`, even
  when all retained measures have valid durations. Missing and extra bars both
  require review. A warning at the start of the affected part asks the reader to
  compare all systems with the PDF; it does not locate the missing content.
- `part_count_mismatch`: the export must contain one part, or exactly the length
  of `parts`. This warning is saved before the existing fixup failure stops the
  item. No new corrected score is produced for an incompatible part count.
- `part_identity`: part IDs must be unique and match the score-part declarations.
  Manifest names are assigned output labels, so these checks cannot establish
  musical identity or detect swapped voices with otherwise valid structure.
  An identity mismatch fails the durations stage after recording the signal;
  fixup and conversion are skipped to prevent transforming an ambiguous part.
  Correct the MusicXML part IDs or rerun OMR before attempting instrument fixup.
- `navigation_lyric`: lyric text resembling D.C., D.S., Fine, Fim, Coda or al Fine
  requires review, including when navigation directions already exist. Its detail
  also reports when the part has no navigation direction (words, symbols or sound
  navigation attributes), including measure-level sound and barline segno/coda symbols.
  Matching is case insensitive and checks word boundaries. Ordinary words, such as
  English "fine", can also be flagged; these are review hints, not confirmed errors.

`run` prints the review state and warnings; `batch` carries the same signals in its
summary and stderr warnings. Duration and volta suspects also set `review_required`.
Stage success and exit codes retain their existing meaning: a warning does not
fail a conversion. The hand-correction acceptance ratio remains a separate human
review metric; it does not clear these warnings.

For converted scores, review signals become `Review: ...` text above the affected
measure, or above the first measure for a part-wide warning. Score-wide warnings appear
only in the first part, and multiple warnings retain status order. Existing `?` duration
marks remain. Notes, rests, ties, articulations and navigation are never repaired
by these checks. Equal-duration substitutions, artificial rest distributions and
musical continuity still require source-backed review. Status schemas 1-3 remain
readable with an empty signal list; only a rerun performs the new checks.

### Text cleanup

By default, fixup removes credits, work and movement headers, identification text
(creators, rights, source, relations and miscellaneous fields), lyrics, free text
`words`, original rehearsal marks, and text-like technical marks (`fingering`,
`pluck`, `other-technical`). Fret and string positions, hammer-ons, pull-offs and
tapping techniques remain.
Encoding metadata, part names and instrument data remain. Clefs, signatures, notes,
rests, accidentals, articulations, ties, slurs, tuplets, dynamics, barlines, repeats,
voltas and navigation (`sound`, segno and coda) retain their musical content.
The whole direction, including text and positioning, is kept when its `sound`
has `dacapo`, `dalsegno`, `fine`, `tocoda`, `coda` or `segno`, or when any of its
words matches the shared navigation pattern used by review signals (for example
"D.S. al Coda", "D.C.", "Fine", Portuguese "fim" or "To Coda"), even without a
`sound`. When stripping is ambiguous, cleanup keeps text: a direction matching
the navigation pattern is kept whole, including any unrelated words in it.
Other words, such as tempo text, "rit." and "solo", are removed when their
direction does not qualify for preservation.
Instrument and volta fixes still apply as described above.

Set `"keep_text": true` on an individual manifest item to keep its original text.
Omitting it or setting it to `false` enables cleanup on both `run` and `batch`.
Review signals, including `navigation_lyric`, are always computed from the raw
export. Cleanup runs once across all parts before our `?` marks and `Review: ...`
annotations are inserted, before final XSD validation and MuseScore conversion.

`status.json` schema 5 records `text_removals` with element counts for `credits`,
`headers`, `lyrics`, `words`, `rehearsals` and `technical`. A removed work or
miscellaneous block counts as one element, regardless of its children. Empty
wrappers do not count. With `keep_text: true` all counts are zero. Before fixup
completes, or when reading status schemas 1-4, the field is `null` (unknown).

### Audiveris settings per input

An item can supply `audiveris_options`, a map of fully qualified constant names to scalar strings. Each entry becomes a separate `-option name=value` argument, recorded in the item's `audiveris/audiveris.log`. Names must start with `org.audiveris.omr.`; values allow letters, digits, `_`, `.`, `+` and `-`. Omitting the map keeps the installed Audiveris defaults. Options apply only to that item on both `run` and `batch`. Audiveris owns the constant names and their meanings; check its log for unsupported settings when changing tool versions.

The effective override map is recorded next to `tool_versions` in `status.json` (schema 5) and each `batch.json` item (schema 3). An empty map means tool defaults were used. Older status schemas remain readable with unknown options represented as `null`; batch items that never started also record `null`.

For a single-instrument page surrounded by a decorative frame:

```json
"audiveris_options": {
  "org.audiveris.omr.sheet.grid.StaffProjector.staffAbscissaMargin": "1.0"
}
```

[Audiveris 5.11's default projection margin](https://github.com/Audiveris/audiveris/blob/5.11.0/app/src/main/java/org/audiveris/omr/sheet/grid/StaffProjector.java) is 15 staff interlines. A nearby vertical frame can then be mistaken for a connector between successive systems. Reducing the margin to one interline excludes the frame on the Carnaval test pages, restoring independent systems, their barlines and the 2/4 signature. Do not apply this setting globally: other layouts can need a wider margin for braces or brackets. For small vector pages that still lose a barline, also test `org.audiveris.omr.image.ImageLoading.pdfResolution` with the string value `"600"`. Higher resolution alone does not correct the false system connections.

### batch

Runs every item of the manifest in order, exactly as `run` does, and writes each item's `output/<pdf stem>/status.json`. A failing item does not stop the others. Since all items are parts of the same piece, it then compares the measures read per part with each other and with each part's `expected_measures`.

- `output/batch.json`: per item its stem, instrument, status (`passed`, `failed` or `setup_error`), measures read/expected/missing and suspect count; plus `cross_part`: whether all parts agree, the count most parts read, and which parts differ from the others or from their expected value.
- stdout: one line per item as it finishes, then a summary table. Disagreements are printed as warnings on stderr.
- Exit code: 0 only if every item passed. A measure-count disagreement is a warning, not a failure: the author fixes it by hand.

## Development

Verify changes with `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest -q`.

## PDFs never leave the Mac

The PDFs are the author's own (purchased or from a publisher). They are not committed (`.gitignore` ignores `*.pdf`) and are never sent to a server or to another machine. Tests outside the Mac use only public-domain or synthetic files.

## External tools and licenses

The pipeline calls separately installed programs from the command line. None of their code is copied or modified here.

| Tool | Target version | License | Use |
|---|---|---|---|
| Audiveris | 5.11.0 | AGPL-3.0 | OMR: PDF → MusicXML (`-batch -export`) |
| MuseScore Studio | 4.7.5 | GPL v3 | MusicXML → `.mscz` with style (`-S`, `-o`) |

If this ever becomes a service for other people, review Audiveris's AGPL first.

## Documentation checksums

`MANIFEST.sha256` stays docs-only in scope, covering only the documentation and
supporting files explicitly listed below. It excludes `src/`, `tests/`,
`pyproject.toml` and generated run outputs; it is not a complete release-integrity
manifest. New files in `docs/` are not included automatically: add them to the
regeneration command's file list when extending coverage.

From the repository root, regenerate after changing a listed file:

```sh
shasum -a 256 \
  ./.gitignore \
  ./README.md \
  ./docs/accuracy.md \
  ./docs/architecture.md \
  ./docs/music.schema.json \
  ./examples/music.example.json \
  ./instruments.json > MANIFEST.sha256
```

Then verify, also from the repository root:

```sh
shasum -a 256 -c MANIFEST.sha256
```

CI does not yet run this check; [#31](https://github.com/faelplg/omr-ingestor/issues/31)
tracks adding it.
