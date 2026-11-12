import hashlib
import json
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from omr_ingestor.audiveris import AUDIVERIS_ENV_VAR
from omr_ingestor.cli import main
from omr_ingestor.durations import check_review_signals
from omr_ingestor.instruments import load_instruments
from omr_ingestor.musescore import MUSESCORE_ENV_VAR
from omr_ingestor.musicxml import read_musicxml
from omr_ingestor.pipeline import (
    PipelineSetupError,
    StageError,
    _annotate_review_signals,
    _durations,
    _prepare,
    run_item,
    select_item,
)
from omr_ingestor.status import STAGE_NAMES, MeasureCount, read_status, write_status
from omr_ingestor.xsd import XSD_ENV_VAR
from tests.fake_tools import ANY_SCORE_XSD, fake_audiveris, fake_musescore
from tests.mscx_builders import mscx_xml, part_xml, style_xml, two_trumpet_mscx
from tests.musicxml_builders import (
    DEFAULT_ATTRS,
    measure,
    multiple_rest,
    note,
    omr_score,
    rest,
    score,
    start,
    stop,
    synthetic_score,
    volta_score,
)

pytestmark = pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint not installed")

TRUMPET = load_instruments()["bb-trumpet"]
PDF = "pdfs/song-trumpet.pdf"
STEM = "song-trumpet"

REJECT_ALL_XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="nothing"/></xs:schema>'
)


def trumpet_mscx(instrument_id: str = TRUMPET.id) -> str:
    t = TRUMPET.transpose
    return mscx_xml(part_xml(instrument_id, TRUMPET.name_pt, t.diatonic, t.chromatic))


@dataclass
class Setup:
    manifest: Path
    output: Path
    pdf: Path
    style: Path
    bin: Path
    xsd: Path

    @property
    def item_dir(self) -> Path:
        return self.output / STEM

    def status(self):
        return read_status(self.item_dir / "status.json")

    def run(self, item: str = "0") -> int:
        return main(["run", str(self.manifest), item, "--output", str(self.output)])


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    style = home / "Estilo 2026.mss"
    style.write_text(style_xml(), encoding="utf-8")

    pdf = tmp_path / "project" / PDF
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    manifest = tmp_path / "project" / "music.json"
    item = {
        "pdf": PDF,
        "instrument": TRUMPET.id,
        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "expected_measures": 3,
    }
    manifest.write_text(
        json.dumps({"title": "Synthetic", "style": "~/Estilo 2026.mss", "items": [item]}),
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    xsd = tmp_path / "xsd" / "musicxml.xsd"
    xsd.parent.mkdir()
    xsd.write_text(ANY_SCORE_XSD, encoding="utf-8")
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(bin_dir, [omr_score()])))
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(bin_dir, trumpet_mscx())))
    monkeypatch.setenv(XSD_ENV_VAR, str(xsd))
    return Setup(manifest, tmp_path / "out", pdf, style, bin_dir, xsd)


def states(status) -> dict[str, str]:
    return {s.name: s.status.value for s in status.stages}


def test_runs_every_stage(setup: Setup, capsys):
    assert setup.run() == 0

    status = setup.status()
    assert states(status) == dict.fromkeys(STAGE_NAMES, "passed")
    assert status.pdf == PDF
    assert status.instrument == TRUMPET.id
    assert status.expected_measures == 3
    assert [m.number for m in status.suspect_measures] == ["2"]
    assert status.hand_corrected is None
    assert status.tool_versions == {"audiveris": "5.11.0", "musescore": "4.7.5"}
    assert status.audiveris_options == {}
    assert status.measure_count == MeasureCount(read=3, expected=3, missing=0)
    assert status.stage("durations").detail == (
        "1 suspect measure(s); 3 measure(s) read, 3 expected, 0 missing"
    )
    assert all(s.duration_s >= 0 for s in status.stages)

    fixed = read_musicxml(setup.item_dir / f"{STEM}.musicxml")
    assert fixed.findtext("part-list/score-part/part-name") == TRUMPET.name_pt
    assert fixed.findtext("part/measure[@number='2']/direction/direction-type/rehearsal") == "?"
    with zipfile.ZipFile(setup.item_dir / f"{STEM}.mscz") as zf:
        assert "score.mscx" in zf.namelist()

    args = json.loads((setup.bin / "mscore.args").read_text())
    assert args[:2] == ["-S", str(setup.style.resolve())]
    assert "output:" in capsys.readouterr().out


def test_manifest_options_reach_audiveris_without_leaking_to_later_runs(setup: Setup):
    data = json.loads(setup.manifest.read_text())
    option = "org.audiveris.omr.sheet.grid.StaffProjector.staffAbscissaMargin=1.0"
    data["items"][0]["audiveris_options"] = dict([option.split("=", 1)])
    setup.manifest.write_text(json.dumps(data))
    assert setup.run() == 0
    log = setup.item_dir / "audiveris" / "audiveris.log"
    assert f"-option {option}" in log.read_text()
    assert setup.status().audiveris_options == data["items"][0]["audiveris_options"]
    seen = read_status(setup.bin / "mscore.status.json")
    assert seen.audiveris_options == setup.status().audiveris_options

    del data["items"][0]["audiveris_options"]
    setup.manifest.write_text(json.dumps(data))
    assert setup.run() == 0
    assert "-option" not in log.read_text()
    assert setup.status().audiveris_options == {}


@pytest.mark.parametrize("location", ["name", "value"])
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_option_newlines_fail_manifest_validation_before_setup(
    setup: Setup, monkeypatch, capsys, location, ending
):
    data = json.loads(setup.manifest.read_text())
    name, value = "org.audiveris.omr.image.ImageLoading.pdfResolution", "600"
    if location == "name":
        name += ending
    else:
        value += ending
    data["items"][0]["audiveris_options"] = {name: value}
    setup.manifest.write_text(json.dumps(data))

    def unexpected_setup():
        pytest.fail("tool setup must not run for an invalid manifest")

    monkeypatch.setattr("omr_ingestor.pipeline.resolve_tools", unexpected_setup)
    assert setup.run() == 1
    assert "invalid manifest" in capsys.readouterr().err
    assert not setup.output.exists()


def test_status_is_written_after_each_stage(setup: Setup):
    assert setup.run() == 0
    # The copy MuseScore saw: everything before it passed, the rest still pending.
    seen = read_status(setup.bin / "mscore.status.json")
    expected = dict.fromkeys(STAGE_NAMES[:5], "passed") | {
        "musescore": "pending",
        "mscx_check": "pending",
    }
    assert states(seen) == expected
    assert [m.number for m in seen.suspect_measures] == ["2"]


def test_rerun_is_idempotent_and_keeps_the_human_review(setup: Setup):
    assert setup.run() == 0
    musicxml = (setup.item_dir / f"{STEM}.musicxml").read_bytes()
    files = sorted(p.relative_to(setup.output) for p in setup.output.rglob("*"))
    write_status(replace(setup.status(), hand_corrected=("2",)), setup.item_dir / "status.json")

    assert setup.run(STEM) == 0
    status = setup.status()
    assert (setup.item_dir / f"{STEM}.musicxml").read_bytes() == musicxml
    assert sorted(p.relative_to(setup.output) for p in setup.output.rglob("*")) == files
    assert [m.number for m in status.suspect_measures] == ["2"]
    assert status.hand_corrected == ("2",)
    data = json.loads((setup.item_dir / "status.json").read_text())
    assert data["acceptance"] == {
        "hand_corrected_count": 1,
        "expected_measures": 3,
        "ratio": 0.333333,
        "limit": 0.05,
        "accepted": False,
    }


def test_xsd_failure_stops_the_item(setup: Setup, capsys):
    setup.xsd.write_text(REJECT_ALL_XSD, encoding="utf-8")
    assert setup.run() == 1

    status = setup.status()
    assert states(status) == {
        "audiveris": "passed",
        "xsd": "failed",
        **dict.fromkeys(STAGE_NAMES[2:], "skipped"),
    }
    assert "not valid MusicXML" in status.stage("xsd").detail
    assert not (setup.item_dir / f"{STEM}.musicxml").exists()
    assert not (setup.item_dir / f"{STEM}.mscz").exists()
    assert "stage xsd failed" in capsys.readouterr().err


def test_audiveris_without_output_fails(setup: Setup, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [])))
    assert setup.run() == 1
    status = setup.status()
    assert status.stage("audiveris").status.value == "failed"
    assert "no .mxl file" in status.stage("audiveris").detail


def test_several_movements_fail_the_audiveris_stage(setup: Setup, monkeypatch):
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [omr_score()] * 2)))
    assert setup.run() == 1
    assert "2 movements" in setup.status().stage("audiveris").detail


def test_wrong_mscx_fails_the_final_check(setup: Setup, monkeypatch: pytest.MonkeyPatch):
    exe = fake_musescore(setup.bin, trumpet_mscx("flute"))
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(exe))
    assert setup.run() == 1
    status = setup.status()
    assert states(status)["musescore"] == "passed"
    assert status.stage("mscx_check").status.value == "failed"
    assert "instrument: FAIL (found flute, expected bb-trumpet)" in (
        status.stage("mscx_check").detail
    )
    assert not (setup.item_dir / f"{STEM}.mscz").exists()


def test_rejected_rerun_keeps_the_previous_mscz(setup: Setup, monkeypatch):
    assert setup.run() == 0
    mscz = setup.item_dir / f"{STEM}.mscz"
    good = mscz.read_bytes()

    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, trumpet_mscx("flute"))))
    assert setup.run() == 1
    assert mscz.read_bytes() == good
    status = setup.status()
    assert states(status)["mscx_check"] == "failed"
    assert f"kept as {STEM}.rejected.mscz" in status.stage("mscx_check").detail
    rejected = setup.item_dir / f"{STEM}.rejected.mscz"
    with zipfile.ZipFile(rejected) as zf:
        assert 'Instrument id="flute"' in zf.read("score.mscx").decode()
    assert not (setup.item_dir / f"{STEM}.staged.mscz").exists()

    # A later good run publishes again and drops the rejected file.
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, trumpet_mscx())))
    assert setup.run() == 0
    assert not rejected.exists()


def test_volta_rewrites_and_suspects_reach_the_status(setup: Setup, monkeypatch):
    xml = volta_score(
        start("1") + stop("1", "discontinue"),
        start("2"),
        stop("2", repeat=True),
        start("2") + stop("2"),
        start("1") + stop("1", "discontinue"),
        start("1") + stop("1"),
        start("2") + stop("2"),
    )
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))
    assert setup.run() == 0

    status = setup.status()
    reason = "ending 1 (measure 7) has the same number as the ending right before it; left as is"
    assert [(m.number, m.detail) for m in status.suspect_measures] == [
        ("6", reason),
        ("7", reason),
    ]
    assert status.stage("fixup").detail == (
        "bb-trumpet applied to part P1, 3 mark(s); ending 1 rejoined over measures 2-4"
    )


def test_multi_measure_rests_are_marked_for_review(setup: Setup, monkeypatch):
    q, whole = note(1), rest(4, measure=True)
    xml = score(
        {
            "P1": [
                measure(1, DEFAULT_ATTRS + q * 4),
                measure(2, multiple_rest(1) + whole),
                measure(3, multiple_rest(1) + q * 3),
            ]
        }
    )
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))
    assert setup.run() == 0

    status = setup.status()
    assert status.measure_count == MeasureCount(read=3, expected=3, missing=0)
    reason = "multi-measure rest read as 1 bar(s); check the real length in the PDF"
    assert [(m.number, m.detail) for m in status.suspect_measures] == [
        ("2", f"part P1 measure 2: {reason}"),
        ("3", f"part P1 measure 3: 3 quarter(s), expected 4 (4/4); {reason}"),
    ]
    fixed = read_musicxml(setup.item_dir / f"{STEM}.musicxml")
    for number in ("2", "3"):
        marks = fixed.findall(
            f"part/measure[@number='{number}']/direction/direction-type/rehearsal"
        )
        assert [m.text for m in marks] == ["?"]


@pytest.mark.parametrize(
    ("expected", "missing", "detail"),
    [
        (5, 2, "3 measure(s) read, 5 expected, 2 missing"),
        (2, -1, "3 measure(s) read, 2 expected, 1 extra"),
    ],
)
def test_measure_count_difference_reaches_the_status(setup: Setup, expected, missing, detail):
    data = json.loads(setup.manifest.read_text(encoding="utf-8"))
    data["items"][0]["expected_measures"] = expected
    setup.manifest.write_text(json.dumps(data), encoding="utf-8")
    assert setup.run() == 0

    status = setup.status()
    assert status.measure_count == MeasureCount(read=3, expected=expected, missing=missing)
    assert status.stage("durations").detail.endswith(detail)
    saved = json.loads((setup.item_dir / "status.json").read_text())
    assert saved["measure_count"] == {"read": 3, "expected": expected, "missing": missing}


def test_page_size_comes_from_the_style(setup: Setup):
    setup.style.write_text(style_xml("8.27", "11.69"), encoding="utf-8")  # A4 portrait
    assert setup.run() == 0
    assert "found 210.1 x 296.9 mm" in setup.status().stage("mscx_check").detail


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s, mp: mp.delenv(MUSESCORE_ENV_VAR), MUSESCORE_ENV_VAR),
        (lambda s, mp: mp.delenv(XSD_ENV_VAR), XSD_ENV_VAR),
        (lambda s, mp: s.pdf.write_bytes(b"changed"), "sha256 is"),
        (lambda s, mp: s.style.unlink(), "style file not found"),
    ],
)
def test_setup_problems_fail_before_any_stage(setup: Setup, monkeypatch, capsys, change, message):
    change(setup, monkeypatch)
    assert setup.run() == 1
    assert message in capsys.readouterr().err
    assert not setup.output.exists()


def test_refuses_status_of_another_item(setup: Setup):
    assert setup.run() == 0
    path = setup.item_dir / "status.json"
    write_status(replace(setup.status(), pdf="other/song-trumpet.pdf"), path)
    before = path.read_bytes()
    with pytest.raises(PipelineSetupError, match="two items share the PDF stem"):
        run_item(setup.manifest, "0", setup.output)
    assert path.read_bytes() == before


def test_select_item():
    items = [{"pdf": "a/x.pdf"}, {"pdf": "b/x.pdf"}, {"pdf": "c/y.pdf"}]
    manifest = {"items": items}
    assert select_item(manifest, "1") == (1, items[1])
    assert select_item(manifest, "b/x.pdf") == (1, items[1])
    assert select_item(manifest, "y") == (2, items[2])
    with pytest.raises(PipelineSetupError, match="ambiguous"):
        select_item(manifest, "x")
    with pytest.raises(PipelineSetupError, match="out of range"):
        select_item(manifest, "3")
    with pytest.raises(PipelineSetupError, match="no item matches"):
        select_item(manifest, "z")


def multipart_xml():
    return score(
        {
            "P1": [measure(1, DEFAULT_ATTRS + note(4)), measure(2, note(4))],
            "P2": [measure(1, DEFAULT_ATTRS + note(4)), measure(2, note(2))],
        }
    )


def configure_parts(setup, parts):
    manifest = json.loads(setup.manifest.read_text())
    if parts is not None:
        manifest["items"][0]["parts"] = parts
    setup.manifest.write_text(json.dumps(manifest))


def test_two_parts_fixup_and_final_check(setup, monkeypatch):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [multipart_xml()])))
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, two_trumpet_mscx())))
    assert setup.run() == 0
    fixed = read_musicxml(setup.item_dir / f"{STEM}.musicxml")
    assert [p.findtext("part-name") for p in fixed.findall("part-list/score-part")] == [
        "Trumpet 1",
        "Trumpet 2",
    ]
    for part in fixed.findall("part"):
        assert part.findtext("measure/attributes/clef/sign") == "G"
        assert part.findtext("measure/attributes/clef/line") == "2"
        assert part.findtext("measure/attributes/transpose/chromatic") == "-2"
        assert part.findtext("measure/attributes/transpose/diatonic") == "-1"
    assert [
        p.findtext("score-instrument/instrument-sound")
        for p in fixed.findall("part-list/score-part")
    ] == ["brass.trumpet.bflat"] * 2
    assert fixed.find("part[@id='P1']/measure/direction/direction-type/rehearsal") is None
    assert "2 measure(s) read, 3 expected" in fixed.findtext(
        "part[@id='P1']/measure/direction/direction-type/words"
    )
    mark = "part[@id='P2']/measure[@number='2']/direction/direction-type/rehearsal"
    assert fixed.findtext(mark) == "?"
    status = setup.status()
    assert status.measure_count == MeasureCount(read=2, expected=3, missing=1)
    assert len(status.suspect_measures) == 1
    assert status.suspect_measures[0].detail.startswith("part P2 measure 2:")
    assert "part P1: 2 measure(s) read, 3 expected, 1 missing" in status.stage("durations").detail
    assert "part P2: 2 measure(s) read, 3 expected, 1 missing" in status.stage("durations").detail
    assert "part_count: pass (found 2, expected 2)" in status.stage("mscx_check").detail
    assert all(state == "passed" for state in states(status).values())


@pytest.mark.parametrize("parts", [None, ["Only one"], ["One", "Two", "Three"]])
def test_multipart_count_mismatch_fails_before_writing(setup, monkeypatch, parts):
    configure_parts(setup, parts)
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [multipart_xml()])))
    assert setup.run() == 1
    status = setup.status()
    assert status.stage("fixup").status.value == "failed"
    assert "score has 2 parts" in status.stage("fixup").detail
    assert status.review_required
    assert "part_count_mismatch" in [s.code for s in status.review_signals]
    if parts is not None:
        assert f"manifest parts expects {len(parts)}" in status.stage("fixup").detail
    else:
        assert status.stage("fixup").detail == (
            "score has 2 parts ['P1', 'P2']; list their names in the item's \"parts\""
        )
    assert status.stage("musescore").status.value == "skipped"
    assert not (setup.item_dir / f"{STEM}.musicxml").exists()


@pytest.mark.parametrize("second_count", [1, 2, 4])
def test_multipart_measure_counts_and_batch_summary(setup, monkeypatch, second_count, capsys):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
    xml = score(
        {
            pid: [
                measure(n, (DEFAULT_ATTRS if n == 1 else "") + note(4)) for n in range(1, count + 1)
            ]
            for pid, count in (("P1", 2), ("P2", second_count))
        }
    )
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, two_trumpet_mscx())))
    assert main(["batch", "-o", str(setup.output), str(setup.manifest)]) == 0
    status = setup.status()
    delta = 3 - second_count
    difference = f"{delta} missing" if delta >= 0 else f"{-delta} extra"
    assert status.stage("durations").detail == (
        "0 suspect measure(s); part P1: 2 measure(s) read, 3 expected, 1 missing; "
        f"part P2: {second_count} measure(s) read, 3 expected, {difference}"
    )
    summary = json.loads((setup.output / "batch.json").read_text())
    cross = summary["cross_part"]
    warnings = capsys.readouterr().err
    if second_count == 2:
        assert status.measure_count == MeasureCount(read=2, expected=3, missing=1)
        assert summary["items"][0]["measures"] == {"read": 2, "expected": 3, "missing": 1}
        assert cross["not_counted"] == []
        assert cross["differ_from_expected"] == [STEM]
        assert cross["reference_count"] == 2
        assert "warning:" in warnings
        assert "expected" in warnings
    else:
        assert status.measures_read is None
        assert cross["not_counted"] == [STEM]
        assert f"warning: parts read different measure counts: {STEM}" in warnings
        assert "failed before durations" not in warnings


@pytest.mark.parametrize("parts", [None, ["Trumpet 1", "Trumpet 2"]])
def test_score_without_parts_has_its_own_error(setup, monkeypatch, parts):
    configure_parts(setup, parts)
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [score({})])))
    assert setup.run() == 1
    status = setup.status()
    assert status.stage("fixup").detail == "score has no parts"
    assert status.stage("musescore").status.value == "skipped"


@pytest.mark.parametrize("second_count", [1, 2])
def test_batch_multipart_without_configuration_reports_missing_parts(
    setup, monkeypatch, capsys, second_count
):
    xml = score(
        {
            pid: [
                measure(n, (DEFAULT_ATTRS if n == 1 else "") + note(4)) for n in range(1, count + 1)
            ]
            for pid, count in (("P1", 2), ("P2", second_count))
        }
    )
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))
    assert main(["batch", "-o", str(setup.output), str(setup.manifest)]) == 1
    status = setup.status()
    assert status.measures_read is None
    assert status.stage("fixup").detail == (
        "score has 2 parts ['P1', 'P2']; list their names in the item's \"parts\""
    )
    warnings = capsys.readouterr().err
    assert (
        f'warning: multipart score requires the item\'s "parts" configuration: {STEM}' in warnings
    )
    assert "parts read different measure counts" not in warnings
    assert "failed before durations" not in warnings


def test_review_signals_reach_status_batch_cli_and_score(setup, monkeypatch, capsys):
    from omr_ingestor.batch import run_batch
    from omr_ingestor.cli import _print_batch

    xml = score(
        {
            "P1": [
                measure(
                    1, DEFAULT_ATTRS + note(4, extra="<lyric><text>D.C. al Fine</text></lyric>")
                ),
                measure(2, note(4)),
            ]
        }
    )
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))
    result = run_batch(setup.manifest, setup.output)
    assert result.ok
    status = setup.status()
    assert not status.suspect_measures
    assert status.review_required
    assert [s.code for s in status.review_signals] == ["measure_count_mismatch", "navigation_lyric"]
    assert result.items[0].review_required
    assert result.items[0].review_signals == status.review_signals
    summary = json.loads(result.summary_file.read_text())
    assert summary["items"][0]["review_required"] is True
    assert summary["items"][0]["review_signals"] == status.to_dict()["review_signals"]
    fixed = read_musicxml(setup.item_dir / f"{STEM}.musicxml")
    words = [el.text for el in fixed.findall("part/measure/direction/direction-type/words")]
    assert any("2 measure(s) read, 3 expected" in w for w in words)
    assert any("navigation-like lyric" in w for w in words)
    assert words == ["Review: " + signal.detail for signal in status.review_signals]
    raw = read_musicxml(setup.item_dir / "audiveris" / f"{STEM}.musicxml")
    assert fixed.find(".//lyric") is None
    assert raw.find(".//lyric") is not None
    for raw_note in raw.findall(".//note"):
        for lyric in raw_note.findall("lyric"):
            raw_note.remove(lyric)
    assert [ET.tostring(n) for n in fixed.findall("part/measure/note")] == [
        ET.tostring(n) for n in raw.findall("part/measure/note")
    ]
    assert status.text_removals["lyrics"] == 1
    _print_batch(result)
    assert "navigation_lyric" in capsys.readouterr().err
    assert setup.run() == 0
    assert "review required: true" in capsys.readouterr().out


@pytest.mark.parametrize("multipart", [False, True])
def test_clean_item_has_no_review_signals(setup, monkeypatch, multipart):
    parts = {
        pid: [measure(n, (DEFAULT_ATTRS if n == 1 else "") + note(4)) for n in range(1, 4)]
        for pid in (["P1", "P2"] if multipart else ["P1"])
    }
    if multipart:
        configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
        monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, two_trumpet_mscx())))
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [score(parts)])))
    assert setup.run() == 0
    status = setup.status()
    assert not status.suspect_measures
    assert status.review_signals == ()
    assert status.review_required is False
    fixed = read_musicxml(setup.item_dir / f"{STEM}.musicxml")
    assert not fixed.findall("part/measure/direction")


def test_score_wide_annotation_appears_once_with_duplicate_part_ids():
    root = ET.fromstring(
        score({pid: [measure(1, DEFAULT_ATTRS + note(4))] for pid in ["P1", "P2"]})
    )
    root.findall("part")[1].set("id", "P1")
    signals = check_review_signals(root, 1, 2)
    assert [signal.code for signal in signals] == ["part_identity"]
    _annotate_review_signals(root, signals)
    parts = root.findall("part")
    assert (
        parts[0].findtext("measure/direction/direction-type/words")
        == "Review: " + signals[0].detail
    )
    assert not parts[1].findall("measure/direction")


def test_duplicate_part_ids_stop_before_fixup_and_save_signal(setup, monkeypatch, capsys):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
    xml = score(
        {
            pid: [measure(n, (DEFAULT_ATTRS if n == 1 else "") + note(4)) for n in range(1, 4)]
            for pid in ("P1", "P2")
        }
    ).replace('id="P2"', 'id="P1"')
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))

    def forbidden_fixup(*args, **kwargs):
        pytest.fail("instrument fixup must not run with ambiguous part IDs")

    monkeypatch.setattr("omr_ingestor.pipeline.fix_instrument", forbidden_fixup)
    assert setup.run() == 1
    status = setup.status()
    assert [signal.code for signal in status.review_signals] == ["part_identity"]
    assert status.review_required
    assert status.stage("durations").status == "failed"
    assert status.stage("fixup").status == "skipped"
    assert status.stage("musescore").status == "skipped"
    error = capsys.readouterr().err
    assert "part IDs" in error and "unique" in error
    assert "before instrument fixup" in error
    assert not (setup.item_dir / f"{STEM}.musicxml").exists()
    assert not list(setup.item_dir.glob("*.mscz"))
    assert not (setup.bin / "mscore.args").exists()


@pytest.mark.parametrize("keep_text", [False, True])
def test_text_policy_runs_once_for_multipart_and_preserves_review(setup, monkeypatch, keep_text):
    data = json.loads(setup.manifest.read_text())
    data["items"][0].update(parts=["Trumpet 1", "Trumpet 2"], keep_text=keep_text)
    setup.manifest.write_text(json.dumps(data))
    root = synthetic_score()
    monkeypatch.setenv(
        AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [ET.tostring(root, encoding="unicode")]))
    )
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, two_trumpet_mscx())))
    assert setup.run() == 0
    status = setup.status()
    assert status.review_signals == check_review_signals(root, 3, 2)
    fixed = read_musicxml(setup.item_dir / f"{STEM}.musicxml")
    assert (fixed.find(".//lyric") is not None) == keep_text
    assert status.text_removals == dict(
        credits=0 if keep_text else 1,
        headers=0 if keep_text else 8,
        lyrics=0 if keep_text else 2,
        words=0 if keep_text else 6,
        rehearsals=0 if keep_text else 2,
        technical=0 if keep_text else 6,
    )
    words = [el.text for el in fixed.iter("words")]
    assert all("Review: " + s.detail in words for s in status.review_signals)
    assert fixed.findtext(".//rehearsal") == ("A" if keep_text else "?")


@pytest.mark.parametrize("legacy", [False, True])
def test_multipart_hand_corrections_survive_rerun_and_reach_batch(
    setup, monkeypatch, capsys, legacy
):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [multipart_xml()])))
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, two_trumpet_mscx())))
    manifest = json.loads(setup.manifest.read_text())
    manifest["items"][0]["expected_measures"] = 60
    setup.manifest.write_text(json.dumps(manifest))
    assert setup.run() == 0
    status = setup.status()
    assert status.part_ids == ("P1", "P2")
    corrections = ("1",) if legacy else {"P1": ("1",), "P2": ("1", "2")}
    reviewed = replace(status, hand_corrected=corrections)
    data = reviewed.to_dict()
    if legacy:
        data["schema_version"] = 5
        del data["part_ids"]
        data["acceptance"]["accepted"] = True
    (setup.item_dir / "status.json").write_text(json.dumps(data))
    capsys.readouterr()

    assert main(["batch", "-o", str(setup.output), str(setup.manifest)]) == 0
    status = setup.status()
    assert status.hand_corrected == corrections
    assert status.part_ids == ("P1", "P2")
    summary = json.loads((setup.output / "batch.json").read_text())
    acceptance = summary["items"][0]["acceptance"]
    assert acceptance == status.to_dict()["acceptance"] == reviewed.to_dict()["acceptance"]
    out = capsys.readouterr().out
    if legacy:
        assert summary["items"][0]["acceptance_scope"] == "legacy_item_wide"
        assert acceptance["ratio"] == 0.016667
        assert acceptance["accepted"] is None
        assert (
            f"hand corrections: {STEM} legacy item-wide: 1/60 (1.67%), per-part acceptance unknown"
        ) in out
    else:
        assert summary["items"][0]["acceptance_scope"] == "per_part"
        assert acceptance["P1"]["ratio"] == 0.016667
        assert acceptance["P2"]["ratio"] == 0.033333
        assert f"hand corrections: {STEM} P1: 1/60 (1.67%), accepted" in out
        assert f"hand corrections: {STEM} P2: 2/60 (3.33%), accepted" in out


@pytest.mark.parametrize("replacement", ['"P3"', '"P1"', '""'])
def test_rerun_preserves_corrections_when_part_ids_change(setup, monkeypatch, replacement):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [multipart_xml()])))
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, two_trumpet_mscx())))
    assert setup.run() == 0
    reviewed = replace(setup.status(), hand_corrected={"P2": ("1",)})
    write_status(reviewed, setup.item_dir / "status.json")
    xml = multipart_xml().replace('"P2"', replacement)
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))
    assert setup.run() == 1
    status = setup.status()
    assert status.hand_corrected == {"P2": ("1",)}
    assert status.part_ids == ("P1", "P2")
    assert "part IDs do not match" in status.stage("durations").detail
    # Rejected score data must not be published alongside the previous review IDs.
    assert status.measures_read is None
    assert status.suspect_measures == ()
    assert status.review_signals == ()


@pytest.mark.parametrize("corrections", [{"P2": ("1",)}, {"P1": ("1",)}, {}])
@pytest.mark.parametrize("replacement", ['"P3"', '"P1"', '""'])
def test_duration_part_id_failure_leaves_context_and_status_file_unchanged(
    setup, replacement, corrections
):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
    ctx = _prepare(setup.manifest, "0", setup.output)
    ctx.status = replace(
        ctx.status, part_ids=("P1", "P2"), hand_corrected=corrections, measures_read=2
    )
    setup.item_dir.mkdir(parents=True)
    write_status(ctx.status, ctx.paths.status)
    before_status = ctx.status
    before_bytes = ctx.paths.status.read_bytes()
    ctx.raw_musicxml = setup.item_dir / "rerun.musicxml"
    ctx.raw_musicxml.write_text(multipart_xml().replace('"P2"', replacement))

    with pytest.raises(StageError, match="part IDs do not match"):
        _durations(ctx)

    assert ctx.status is before_status
    assert ctx.report is None
    assert ctx.paths.status.read_bytes() == before_bytes


@pytest.mark.parametrize("corrections", [{}, {"P1": ("1",)}, (), None])
def test_rerun_rejects_removal_of_unreviewed_part(setup, monkeypatch, corrections):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2"])
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [multipart_xml()])))
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(setup.bin, two_trumpet_mscx())))
    assert setup.run() == 0
    reviewed = replace(setup.status(), hand_corrected=corrections)
    write_status(reviewed, setup.item_dir / "status.json")
    xml = multipart_xml().replace('"P2"', '"P3"')
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(fake_audiveris(setup.bin, [xml])))

    assert setup.run() == 1

    status = setup.status()
    assert status.part_ids == ("P1", "P2")
    assert status.hand_corrected == corrections
    assert "part IDs do not match" in status.stage("durations").detail
    assert status.measures_read is None
    assert status.suspect_measures == ()
    assert status.review_signals == ()


def test_duration_allows_additional_part_and_preserves_unreviewed_identity(setup):
    configure_parts(setup, ["Trumpet 1", "Trumpet 2", "Trumpet 3"])
    ctx = _prepare(setup.manifest, "0", setup.output)
    ctx.status = replace(ctx.status, part_ids=("P1", "P2"), hand_corrected={"P1": ("1",)})
    setup.item_dir.mkdir(parents=True)
    ctx.raw_musicxml = setup.item_dir / "rerun.musicxml"
    ctx.raw_musicxml.write_text(
        score(
            {
                pid: [measure(1, DEFAULT_ATTRS + note(4)), measure(2, note(4))]
                for pid in ("P1", "P2", "P3")
            }
        )
    )

    _durations(ctx)

    assert ctx.status.part_ids == ("P1", "P2", "P3")
    assert ctx.status.hand_corrected == {"P1": ("1",)}
    assert ctx.status.acceptance["P2"] is None
    assert ctx.status.acceptance["P3"] is None
