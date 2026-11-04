# omr-ingestor architecture

Planned flow. The diagram is **inferred**: only stage 5 (`-S`) has been tested; the rest has not run. Source: OMR study (v4, 2026-11-04).

```mermaid
flowchart TB
  subgraph MAN["Input (author's Mac, PDFs never leave it)"]
    MJ["music.json (manifest)<br/>items: PDF, instrument, sha256<br/>expected measures, style path"]
  end

  subgraph PIPE["Per-item pipeline (one folder per part, idempotent)"]
    AUD["1. Audiveris 5.11.0<br/>-batch -export<br/>checks the number of .mxl files, not just the exit code"]
    UNZ["Unzip .mxl<br/>handles multiple movements and pages"]
    XSD1["2. MusicXML 4.0 XSD (xmllint, local)"]
    DUR["3. Duration per measure<br/>suspect measures go to the report"]
    DIV["Warning: given instrument<br/>differs from the clef/range read"]
    FIX["4. Instrument fix-up (option B)<br/>transpose, clef, part-name, layout<br/>validates its own result"]
    XSD2["XSD again, after the fix-up"]
    MS["5. MuseScore 4.7.5 with -S<br/>Estilo 2026.mss (batch via -j)"]
    VF["6. Final check on the .mscx inside the .mscz<br/>instrument, transposition, clef,<br/>page size from the style"]
    OUT["formatted .mscz"]
  end

  TAB["instruments.json<br/>table of the 6 instruments"]
  REP["status.json per item<br/>stages, suspect measures,<br/>timings, tool versions"]
  HUM["Human review: the author plays it and checks<br/>hand_corrected: list of measures<br/>goal: up to 5% (denominator = expected_measures)"]
  LOTE["Batch: check across the 5 parts<br/>same number of measures"]

  MJ --> AUD --> UNZ --> XSD1
  XSD1 -- FAIL --> HUM
  XSD1 -- PASS --> DUR --> FIX
  AUD -.-> DIV
  DIV -.-> REP
  TAB --> FIX
  FIX --> XSD2 --> MS --> VF --> OUT
  DUR -.-> REP
  VF -.-> REP
  OUT --> HUM
  HUM -- "fixes and re-enters" --> XSD1
  HUM --> REP
  OUT --> LOTE

  subgraph BANC["Measurement bench (research only, public-domain scores)"]
    GAB["MIDI ground truth"]
    PR["Precision and recall<br/>onset + pitch, time per page"]
  end
  UNZ -.-> PR
  GAB -.-> PR
```

## Stages

1. **Audiveris.** Runs `-batch -export` on the PDF. Checks the number of `.mxl` files produced, not just the exit code. Unzips and handles multiple movements.
2. **XSD.** Validates the MusicXML against the local 4.0 XSD (xmllint). On failure, it goes to human review.
3. **Duration per measure.** Lists measures that do not add up in `status.json`.
4. **Instrument fix-up.** Applies `instruments.json` to the given instrument: `<transpose>`, clef, name, sound; removes the OMR page layout. Adds a rehearsal mark on suspect measures. Runs the XSD again.
5. **MuseScore.** Converts to `.mscz` with `-S "Estilo 2026.mss"`.
6. **Final check on the `.mscx`.** Opens the `.mscz`, reads the `.mscx` and checks instrument, transposition, clef and the style's page size.

After that: human review (the author plays and corrects), re-entry through the XSD, and, in batch mode, a check that all parts have the same number of measures.

## Decisions already made

- The instrument is given at submission time, one of the 6 ids in `instruments.json`; the pipeline does not guess it from the PDF. Provided by the author (2026-11-03).
- Acceptance criterion: up to 5% of the measures corrected by hand per part; denominator = `expected_measures`. Provided (2026-11-03).
- A measure whose duration does not add up becomes a rehearsal mark in the `.mscz` and the flow continues; the list goes in `status.json`. Provided (2026-11-04). Whether the mark survives `-S` is a guess, untested.
- `-S` with validated MusicXML on MuseScore 4.7.5 on 2026-11-04: the style in the `.mscz` is identical to Estilo 2026 (diff 0, A5 landscape). Measured, 1 file with 2 parts.
- The final check on the `.mscx` is mandatory because the `mscore` exit code is not reliable (it exited 134 with the file generated). Measured (2026-11-04).
- Runs on the author's Mac; PDFs never leave it. Provided (2026-11-03).
