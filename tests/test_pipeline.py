import hashlib
import json
import shutil
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from omr_ingestor.audiveris import AUDIVERIS_ENV_VAR
from omr_ingestor.cli import main
from omr_ingestor.instruments import load_instruments
from omr_ingestor.musescore import MUSESCORE_ENV_VAR
from omr_ingestor.musicxml import read_musicxml
from omr_ingestor.pipeline import PipelineSetupError, run_item, select_item
from omr_ingestor.status import STAGE_NAMES, MeasureCount, read_status, write_status
from omr_ingestor.xsd import XSD_ENV_VAR
from tests.fake_tools import make_tool
from tests.musicxml_builders import DEFAULT_ATTRS, measure, multiple_rest, note, rest, score
from tests.test_fixup import omr_score, start, stop, volta_score
from tests.test_mscx_check import mscx_xml, part_xml, style_xml

pytestmark = pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint not installed")

TRUMPET = load_instruments()["bb-trumpet"]
PDF = "pdfs/song-trumpet.pdf"
STEM = "song-trumpet"

# Accepts any score-partwise; the real MusicXML XSD is not in the repository.
ANY_SCORE_XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="score-partwise"><xs:complexType><xs:sequence>'
    '<xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>'
    '</xs:sequence><xs:anyAttribute processContents="skip"/></xs:complexType></xs:element>'
    "</xs:schema>"
)
REJECT_ALL_XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="nothing"/></xs:schema>'
)
CONTAINER = (
    '<container><rootfiles><rootfile full-path="score.xml" '
    'media-type="application/vnd.recordare.musicxml+xml"/></rootfiles></container>'
)


def trumpet_mscx(instrument_id: str = TRUMPET.id) -> str:
    t = TRUMPET.transpose
    return mscx_xml(part_xml(instrument_id, TRUMPET.name_pt, t.diatonic, t.chromatic))


def fake_audiveris(bin_dir: Path, movements: list[str] | dict[str, list[str]]) -> Path:
    """Writes one .mxl per MusicXML text in movements (or in movements[pdf stem])."""
    return make_tool(
        bin_dir,
        "Audiveris",
        f"""
        import sys, zipfile
        from pathlib import Path
        args = sys.argv[1:]
        out = Path(args[args.index("-output") + 1])
        pdf = Path(args[args.index("--") + 1])
        print("Audiveris version 5.11.0")
        movements = {movements!r}
        if isinstance(movements, dict):
            movements = movements[pdf.stem]
        for i, text in enumerate(movements, 1):
            name = pdf.stem + (f".mvt{{i}}" if len(movements) > 1 else "") + ".mxl"
            with zipfile.ZipFile(out / name, "w") as zf:
                zf.writestr("META-INF/container.xml", {CONTAINER!r})
                zf.writestr("score.xml", text)
        """,
    )


def fake_musescore(bin_dir: Path, mscx: str | dict[str, str]) -> Path:
    """Writes mscx (or mscx[pdf stem]) plus the -S style; snapshots status.json as it was."""
    return make_tool(
        bin_dir,
        "mscore",
        f"""
        import json, shutil, sys, zipfile
        from pathlib import Path
        args = sys.argv[1:]
        if args == ["--version"]:
            print("MuseScore4 4.7.5")
            sys.exit(0)
        here = Path(sys.argv[0])
        here.with_suffix(".args").write_text(json.dumps(args))
        out = Path(args[args.index("-o") + 1])
        style = Path(args[args.index("-S") + 1])
        shutil.copy(out.parent.parent / "status.json", here.with_suffix(".status.json"))
        mscx = {mscx!r}
        if isinstance(mscx, dict):
            mscx = mscx[out.parent.parent.name]
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("score.mscx", mscx)
            zf.writestr("score_style.mss", style.read_text())
        sys.exit(134)
        """,
    )


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

    del data["items"][0]["audiveris_options"]
    setup.manifest.write_text(json.dumps(data))
    assert setup.run() == 0
    assert "-option" not in log.read_text()


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
