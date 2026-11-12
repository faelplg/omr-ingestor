# Known accuracy limits

Passing the pipeline does not establish musical fidelity. Acceptance still
requires source-backed human review. The **<=5%** hand-correction ratio is
computed **per part**: that part's corrected measure count divided by the
item's `expected_measures`. Single-part items keep the `hand_corrected` list
format. Multipart items use a mapping keyed by MusicXML part ID, for example
`{"P1": ["7"], "P2": ["7", "12a"]}`. A measure may be recorded in both parts,
but duplicates within a part are rejected. Status schema 7 records `part_ids`,
raw `part_identities` and per-part `acceptance`; batch schema 4 reports the
same ratios in `batch.json` and the terminal summary. Missing mapping entries
are unreviewed (`null` acceptance); empty lists mean reviewed with no
corrections. Every part needs source-backed review and must independently meet
the <=5% limit.

On rerun, identity-backed per-part reviews require the same ID, raw part name
and instrument names. Changed or missing identities stop before fixup and
conversion, retain the old corrections for inspection, and set affected parts'
acceptance to `null`. A `part_identity_changed` signal in `status.json`
explains why each part needs source review again. Names and instruments come
from the raw OMR score, before manifest-based fixup overwrites them. Identical
or absent metadata can still hide swapped music; this check does not prove
musical continuity. After reviewing an intentionally changed part against the
source again, replace its review with the command:

```sh
uv run omr-ingestor review output/song-duet 7 12a --part P1 --replace --dry-run
uv run omr-ingestor review output/song-duet 7 12a --part P1 --replace
```

This records the current raw identity for that part, clears only its
`part_identity_changed` signal, and recomputes the entire acceptance block,
including `null` for unreviewed parts. Other review signals remain. Rerun the
pipeline to complete stages that stopped on the identity change. Missing or
reordered prior IDs still require a rerun before recording corrections.
Alternatively, restoring the original score clears the change signal and
restores acceptance on a successful rerun.

Older status files still load. Schemas 1-6 have no identity metadata and keep
ID-only matching for existing corrections, including on repeated reruns. The
command preserves that provenance when other parts still have legacy reviews.
Archive the prior status and rerun before recording fresh source-backed reviews
for all parts to enable metadata checking.

A multipart item's old shared list keeps its
original item-wide ratio, including after rerunning, but `accepted` is `null`.
A <=5% shared ratio does not establish per-part acceptance. The batch JSON marks
its `acceptance_scope` as `legacy_item_wide`, and the terminal summary identifies
it as legacy item-wide with per-part acceptance unknown. After source review,
use `review output/<item> --part P1 --replace` to replace the shared list with a
review for P1, then record each remaining part separately. This
explicit replacement discards the shared list; it is never assigned to a part
automatically. Flat lists with unknown part IDs also have unknown acceptance until a rerun establishes their
part identity; a confirmed single part retains boolean acceptance and has no
shared label in the terminal summary. See [Architecture](architecture.md) for the
status fields and migration workflow.

Use `review output/<item> 7 12a` for a single-part item, or add `--part P1`
for a multipart item. Without `--replace`, the command adds measures; duplicate
entries and unknown labels are errors. With no measures, `--replace` records an
empty list (reviewed with no corrections). Acceptance ratios use the status
model's six-decimal rounding. No score files are modified. A zero-suspect report
is not acceptance, and recording corrections does not clear musical review warnings.

The initial six-instrument review ([#15](https://github.com/faelplg/omr-ingestor/issues/15))
found useful duration suspects, missed multi-measure rests, tied whole notes
collapsed into empty bars, missing articulations and extra slurs. Wrong structures
near a flagged measure can remain unflagged. Compare surrounding measures and all
systems with the source, not just the marked bars.

The 2026-11-07 diagnosis and OCR/resolution experiment traced the #21-#25 defect
classes and the OCR/navigation findings below to Audiveris 5.11 recognition/export,
before our fixup or MuseScore. The #15 findings come from the earlier review.
These are generic findings; no copyrighted note content, excerpts or images are
reproduced here. The linked issues track the unresolved accuracy work.

## What the existing checks catch

| Defect class | Existing signal and limit | Tested per-item mitigation |
| --- | --- | --- |
| Whole notes output as whole-measure rests ([#21](https://github.com/faelplg/omr-ingestor/issues/21)) | In the diagnosed case Audiveris exported empty bars and MuseScore supplied invisible measure-rest placeholders. Duration suspects catch the empty internal bars. An equal-duration wrong rest would not be caught by duration alone. | Portuguese OCR at 400 or 600 dpi recovered the missing whole notes in one tested input. Neither recovered others, and 600 dpi lost a previously retained whole note on another input. No general cure. |
| Spurious staccatos and other articulations ([#22](https://github.com/faelplg/omr-ingestor/issues/22)) | No articulation detector. False marks already present in raw MusicXML survive fixup; text cleanup does not remove them. Missing articulations, extra slurs and wrong ties also need source review. | Installing OCR data and selecting Portuguese reduced text-related false marks. 600 dpi removed them in one input, but other inputs gained new marks or retained ornaments. OCR disabled on the tested multipart input preserved its parts but left false articulations. |
| Augmentation dots replacing a following rest ([#23](https://github.com/faelplg/omr-ingestor/issues/23)) | Equal-duration substitutions pass the duration check. The issue reports an eighth rest; the diagnosed example was a sixteenth rest, so the eighth-rest variant remains unconfirmed. | Portuguese OCR plus 600 dpi restored the diagnosed note/rest separation. 400 dpi restored the rest but retained a false dot, introducing a duration suspect. This does not validate a blanket dot-removal rule. |
| Dropped system or last measure ([#24](https://github.com/faelplg/omr-ingestor/issues/24)) | `measure_count_mismatch` compares each part with `expected_measures` even when all retained bars have valid durations. It cannot locate the loss; the warning is anchored at the part's first measure. Batch also compares available counts. | Neither Portuguese OCR nor 300/400/600 dpi recovered the dropped final system. A separate input also retained a missing measure throughout the matrix. Higher resolution can reduce false marks while leaving completeness broken. |
| Small duration errors ([#25](https://github.com/faelplg/omr-ingestor/issues/25)) | Duration suspects catch a missing short rest when the bar total changes. They cannot detect wrong note/rest distributions that preserve the total, and pickup/implicit-bar allowances limit detection. | Portuguese OCR with 400 or 600 dpi restored the diagnosed missing short rest; 600 also corrected the equal-duration substitution above. Source review is still required. |
| Missed multi-measure rests and collapsed tied whole notes ([#15](https://github.com/faelplg/omr-ingestor/issues/15)) | Recognized `<multiple-rest>` starts are always suspect, even with valid duration. Empty bars are suspect when duration fails; count checks can reveal missing bars. Neither recovers lost rests or pitches or detects every collapse. | No general setting was validated for these initial findings. Test input-specific recognition settings and correct from the source. |
| OCR splits a multipart score into artificial parts | `part_count_mismatch` flags an unexpected count and fixup stops. `part_identity` checks IDs against declarations; `part_identity_changed` compares raw names and instruments on reruns with identity-backed per-part reviews. It cannot detect voice swaps with identical metadata or artificial rest distributions. Padded parts can all pass duration checks. | Disable OCR with `TesseractOCR.useOCR=false` at default resolution for the affected multipart input. The tested control kept Portuguese selected and disabled OCR, retaining two continuous parts; `useOCR=false` alone was not run separately. Selecting English with OCR enabled still produced extra parts. |
| D.C. and other navigation recognized as lyrics or lost | `navigation_lyric` flags matching lyric text, even if navigation directions already exist. It cannot detect navigation absent from both lyrics and directions or reconstruct its placement/meaning. | No matrix configuration exported D.C. as correct navigation. Installed OCR languages alone did not solve it. With OCR disabled or at higher resolution, missing text can also remove the lyric hint. Verify navigation manually. |

## Audiveris settings per item

Use `audiveris_options` on the individual manifest item for both `run` and `batch`.
The fully qualified constants used in the experiment are:

| Constant | Value and use |
| --- | --- |
| `org.audiveris.omr.text.Language.defaultSpecification` | `"por"` selects Portuguese OCR for Portuguese source text. Select the source language, not a global preference based on this test set. |
| `org.audiveris.omr.image.ImageLoading.pdfResolution` | `"600"` improved hollow-head/rest recognition on one input and reduced false marks on another. Omit to retain the installed default (300 dpi in the tested 5.11 setup). |
| `org.audiveris.omr.text.tesseract.TesseractOCR.useOCR` | `"false"` for multipart inputs where OCR fragments logical parts. The tested control kept Portuguese selected and disabled OCR at default resolution; `useOCR=false` alone was not run separately. |

For an input where Portuguese OCR and higher resolution have been checked:

```json
"audiveris_options": {
  "org.audiveris.omr.text.Language.defaultSpecification": "por",
  "org.audiveris.omr.image.ImageLoading.pdfResolution": "600"
}
```

OCR language models must actually be installed in Audiveris's `tessdata` folder;
a language override does not install them. Check the fresh Audiveris log for
available languages and missing-model warnings. The tested 5.11 app used legacy
Tesseract OCR and compatible `tessdata` 4.1.0 models (`eng`, `por`, `ita`), not
LSTM-only best/fast models. Italian availability was checked, not its accuracy.

The recommended-settings evidence supports three input-specific choices:
Portuguese OCR at default resolution where increased resolution regressed notes;
Portuguese OCR at 600 dpi where recovery or false-mark reduction was verified;
and Portuguese selected with OCR disabled at default resolution where OCR split
a multipart score. The last control did not test `useOCR=false` alone, although
the recommended-settings map omits the language override.
Do not combine or apply these settings globally. At 600 dpi, another input lost
ties while its durations still passed, and another lost a whole note. All choices
still require human review, including missing navigation and system coverage.

The experiment covered five inputs, 15 OCR/resolution runs and two multipart
controls. It inspected Audiveris exports, without new MuseScore conversions, and
did not establish full transcription accuracy. The earlier stage comparison
established that the diagnosed defects were already present before fixup.
The local diagnosis, experiment report and `recommended-settings.json` remain
outside the repository with the score-derived evidence. No additional musical
detectors or automatic note/rest repairs are implemented by these notes.
