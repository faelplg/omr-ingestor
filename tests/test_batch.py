import hashlib
import json
import shutil
from pathlib import Path

import pytest

from omr_ingestor import pipeline
from omr_ingestor.audiveris import AUDIVERIS_ENV_VAR
from omr_ingestor.batch import FAILED, PASSED, BatchItem, BatchResult, check_cross_part
from omr_ingestor.cli import _print_batch, main
from omr_ingestor.musescore import MUSESCORE_ENV_VAR
from omr_ingestor.pipeline import UncountedReason
from omr_ingestor.status import read_status
from omr_ingestor.xsd import XSD_ENV_VAR
from tests.musicxml_builders import DEFAULT_ATTRS, measure, note, score
from tests.test_fixup import omr_score
from tests.test_mscx_check import instrument_mscx, style_xml
from tests.test_pipeline import ANY_SCORE_XSD, fake_audiveris, fake_musescore

pytestmark = pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint not installed")

# stem -> instrument; the three parts of one synthetic piece, 3 measures each.
PARTS = {"song-trumpet": "bb-trumpet", "song-trombone": "trombone", "song-flute": "flute"}


def four_measures() -> str:
    q = note(1)
    return score(
        {"P1": [measure(n, (DEFAULT_ATTRS if n == 1 else "") + q * 4) for n in (1, 2, 3, 4)]}
    )


class Batch:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.output = root / "out"
        self.bin = root / "bin"
        self.manifest = root / "project" / "music.json"
        self.movements = {stem: [omr_score()] for stem in PARTS}
        items = []
        for stem, instrument in PARTS.items():
            pdf = root / "project" / "pdfs" / f"{stem}.pdf"
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(f"%PDF-1.4 {stem}".encode())
            sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            items.append(
                {
                    "pdf": f"pdfs/{stem}.pdf",
                    "instrument": instrument,
                    "sha256": sha,
                    "expected_measures": 3,
                }
            )
        self.manifest.write_text(
            json.dumps({"title": "Synthetic", "style": "~/Estilo 2026.mss", "items": items}),
            encoding="utf-8",
        )
        mscx = {stem: instrument_mscx(instrument) for stem, instrument in PARTS.items()}
        monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(self.bin, mscx)))

    def run(self) -> int:
        exe = fake_audiveris(self.bin, self.movements)
        self.monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(exe))
        return main(["batch", "-o", str(self.output), str(self.manifest)])

    def summary(self) -> dict:
        return json.loads((self.output / "batch.json").read_text(encoding="utf-8"))

    def items(self) -> dict[str, dict]:
        return {it["stem"]: it for it in self.summary()["items"]}


@pytest.fixture
def batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Batch:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / "Estilo 2026.mss").write_text(style_xml(), encoding="utf-8")
    xsd = tmp_path / "xsd" / "musicxml.xsd"
    xsd.parent.mkdir()
    xsd.write_text(ANY_SCORE_XSD, encoding="utf-8")
    monkeypatch.setenv(XSD_ENV_VAR, str(xsd))
    return Batch(tmp_path, monkeypatch)


def test_all_items_pass_and_agree(batch: Batch, capsys):
    assert batch.run() == 0

    for stem, instrument in PARTS.items():
        status = read_status(batch.output / stem / "status.json")
        assert status.instrument == instrument
        assert (batch.output / stem / f"{stem}.mscz").exists()
    summary = batch.summary()
    assert summary["schema_version"] == 2
    assert summary["passed"] is True
    assert [it["stem"] for it in summary["items"]] == list(PARTS)
    assert summary["items"][0] == {
        "stem": "song-trumpet",
        "pdf": "pdfs/song-trumpet.pdf",
        "instrument": "bb-trumpet",
        "status": "passed",
        "failed_stage": None,
        "error": None,
        "measures": {"read": 3, "expected": 3, "missing": 0},
        "suspect_count": 1,
        "status_file": "song-trumpet/status.json",
        "tool_versions": {"audiveris": "5.11.0", "musescore": "4.7.5"},
        "audiveris_options": {},
    }
    assert summary["cross_part"] == {
        "all_agree": True,
        "reference_count": 3,
        "differ_from_others": [],
        "differ_from_expected": [],
        "not_counted": [],
        "expected_agree": True,
    }
    out, err = capsys.readouterr()
    assert "[3/3] song-flute: passed" in out
    assert "song-trombone  trombone    passed     3         3        0         1" in out
    assert "cross-part: all 3 part(s) read 3 measure(s)" in out
    assert err == ""


def test_a_failing_item_does_not_stop_the_others(batch: Batch, capsys):
    batch.movements["song-trumpet"] = []  # the first item: Audiveris writes no .mxl
    (batch.root / "project" / "pdfs" / "song-trombone.pdf").write_bytes(b"changed")
    assert batch.run() == 1

    items = batch.items()
    assert items["song-trumpet"]["status"] == FAILED
    assert items["song-trumpet"]["failed_stage"] == "audiveris"
    assert "no .mxl file" in items["song-trumpet"]["error"]
    assert items["song-trumpet"]["measures"] == {"read": None, "expected": 3, "missing": None}
    assert items["song-trombone"]["status"] == "setup_error"
    assert "sha256 is" in items["song-trombone"]["error"]
    assert items["song-trombone"]["status_file"] is None
    assert not (batch.output / "song-trombone").exists()
    assert items["song-flute"]["status"] == PASSED
    assert read_status(batch.output / "song-flute" / "status.json").measures_read == 3
    assert batch.summary()["passed"] is False
    assert batch.summary()["cross_part"]["not_counted"] == ["song-trumpet", "song-trombone"]
    assert batch.summary()["cross_part"]["all_agree"] is False

    out, err = capsys.readouterr()
    assert "[1/3] song-trumpet: failed at audiveris:" in out
    assert "song-trumpet   bb-trumpet  failed (audiveris)     -         3        -         -" in out
    assert "error: 2 of 3 item(s) did not pass: song-trumpet, song-trombone" in err
    assert (
        "warning: measures not counted (failed before durations): song-trumpet, song-trombone"
    ) in err
    assert "parts read different" not in err


def test_item_directory_conflict_does_not_abort_batch(batch: Batch):
    batch.output.mkdir()
    conflict = batch.output / "song-trumpet"
    conflict.write_text("existing file")

    assert batch.run() == 1

    items = batch.items()  # Reads batch.json, which must still be written.
    assert list(items) == list(PARTS)
    assert items["song-trumpet"]["status"] == "setup_error"
    assert str(conflict) in items["song-trumpet"]["error"]
    assert items["song-trumpet"]["status_file"] is None
    assert conflict.read_text() == "existing file"
    for stem in ("song-trombone", "song-flute"):
        assert items[stem]["status"] == PASSED
        assert (batch.output / stem / f"{stem}.mscz").is_file()
    assert batch.summary()["passed"] is False


@pytest.mark.parametrize("started", [False, True], ids=["initial-write", "mid-run-write"])
def test_item_status_write_error_does_not_abort_batch(batch: Batch, monkeypatch, started):
    write_status = pipeline.write_status

    def fail_status_write(status, path):
        if path.parent.name == "song-trumpet" and (
            not started or status.stage("audiveris").status == "passed"
        ):
            raise PermissionError("status write denied")
        write_status(status, path)

    monkeypatch.setattr(pipeline, "write_status", fail_status_write)
    assert batch.run() == 1

    items = batch.items()
    assert items["song-trumpet"]["status"] == (FAILED if started else "setup_error")
    assert items["song-trumpet"]["status_file"] == (
        "song-trumpet/status.json" if started else None
    )
    assert items["song-trumpet"]["audiveris_options"] == ({} if started else None)
    status_file = batch.output / "song-trumpet/status.json"
    assert status_file.is_file() is started
    if started:
        assert read_status(status_file).stage("audiveris").status == "pending"
        assert list((batch.output / "song-trumpet/audiveris").glob("*.musicxml"))
    assert items["song-trumpet"]["error"] == "status write denied"
    assert items["song-trombone"]["status"] == PASSED
    assert items["song-flute"]["status"] == PASSED
    assert batch.summary()["passed"] is False


@pytest.mark.parametrize("failure", ["input-read", "initial-status-write"])
def test_rerun_io_error_before_first_write_does_not_use_stale_status(
    batch: Batch, monkeypatch, failure
):
    assert batch.run() == 0
    status_file = batch.output / "song-trumpet/status.json"
    previous = status_file.read_bytes()
    assert all(s.status == "passed" for s in read_status(status_file).stages)

    if failure == "input-read":
        sha256_of = pipeline.sha256_of

        def fail_input_read(path):
            if path.stem == "song-trumpet":
                raise PermissionError("input read denied")
            return sha256_of(path)

        monkeypatch.setattr(pipeline, "sha256_of", fail_input_read)
        message = "input read denied"
    else:
        write_status = pipeline.write_status

        def fail_initial_write(status, path):
            if path == status_file:
                raise PermissionError("initial status write denied")
            write_status(status, path)

        monkeypatch.setattr(pipeline, "write_status", fail_initial_write)
        message = "initial status write denied"

    assert batch.run() == 1
    items = batch.items()
    assert items["song-trumpet"]["status"] == "setup_error"
    assert items["song-trumpet"]["status_file"] is None
    assert items["song-trumpet"]["audiveris_options"] is None
    assert items["song-trumpet"]["error"] == message
    assert status_file.read_bytes() == previous
    assert items["song-trombone"]["status"] == PASSED
    assert items["song-flute"]["status"] == PASSED


def test_io_error_recovery_does_not_probe_status_file(batch: Batch, monkeypatch):
    status_file = batch.output / "song-trumpet/status.json"
    is_file = Path.is_file
    write_status = pipeline.write_status

    def deny_status_probe(path, *args, **kwargs):
        if path == status_file:
            raise PermissionError("status probe denied")
        return is_file(path, *args, **kwargs)

    def fail_initial_write(status, path):
        if path == status_file:
            raise PermissionError("initial status write denied")
        write_status(status, path)

    monkeypatch.setattr(Path, "is_file", deny_status_probe)
    monkeypatch.setattr(pipeline, "write_status", fail_initial_write)
    assert batch.run() == 1
    items = batch.items()
    assert items["song-trumpet"]["status"] == "setup_error"
    assert items["song-trumpet"]["status_file"] is None
    assert items["song-trumpet"]["error"] == "initial status write denied"
    assert items["song-trombone"]["status"] == PASSED
    assert items["song-flute"]["status"] == PASSED


def test_batch_records_each_items_options_on_success_failure_and_setup_error(batch: Batch):
    options = {"org.audiveris.omr.sheet.grid.StaffProjector.staffAbscissaMargin": "1.0"}
    data = json.loads(batch.manifest.read_text())
    data["items"][0]["audiveris_options"] = options
    data["items"][1]["audiveris_options"] = options
    batch.manifest.write_text(json.dumps(data))
    batch.movements["song-trumpet"] = []
    (batch.root / "project/pdfs/song-trombone.pdf").write_bytes(b"changed")
    assert batch.run() == 1
    items = batch.items()
    assert items["song-trumpet"]["audiveris_options"] == options
    assert items["song-trombone"]["audiveris_options"] is None
    assert items["song-trombone"]["tool_versions"] == {}
    assert items["song-flute"]["audiveris_options"] == {}
    for stem in ("song-trumpet", "song-flute"):
        saved = json.loads((batch.output / stem / "status.json").read_text())
        assert items[stem]["audiveris_options"] == saved["audiveris_options"]
        assert items[stem]["tool_versions"] == saved["tool_versions"]

    batch.movements["song-trumpet"] = [omr_score()]
    assert batch.run() == 1
    assert batch.items()["song-trumpet"]["status"] == PASSED
    assert batch.items()["song-trumpet"]["audiveris_options"] == options
    assert read_status(batch.output / "song-trumpet/status.json").audiveris_options == options


def test_parts_that_disagree_are_a_warning(batch: Batch, capsys):
    batch.movements["song-flute"] = [four_measures()]
    assert batch.run() == 0

    assert batch.items()["song-flute"]["measures"] == {"read": 4, "expected": 3, "missing": -1}
    assert batch.summary()["cross_part"] == {
        "all_agree": False,
        "reference_count": 3,
        "differ_from_others": ["song-flute"],
        "differ_from_expected": ["song-flute"],
        "not_counted": [],
        "expected_agree": True,
    }
    out, err = capsys.readouterr()
    assert "cross-part" not in out
    assert (
        "warning: the parts disagree on the measure count (most read 3): song-flute read 4" in err
    )
    assert (
        "warning: measure count differs from expected_measures: song-flute read 4, expected 3"
        in err
    )
    assert "error:" not in err


def test_invalid_manifest_runs_nothing(batch: Batch, capsys):
    batch.manifest.write_text("{}", encoding="utf-8")
    assert batch.run() == 1
    assert not batch.output.exists()
    assert "invalid manifest" in capsys.readouterr().err


def part(stem: str, read: int | None, expected: int = 3) -> BatchItem:
    return BatchItem(f"{stem}.pdf", "flute", expected, PASSED, measures_read=read)


def test_cross_part_tie_lists_every_counted_part():
    check = check_cross_part((part("a", 3), part("b", 4, expected=4), part("c", None)))
    assert check.reference_count is None
    assert check.differ_from_others == ("a", "b")
    assert check.differ_from_expected == ()
    assert check.not_counted == ("c",)
    assert check.expected_agree is False
    assert check.all_agree is False


def test_batch_warns_once_for_each_uncounted_cause(tmp_path, capsys):
    items = (
        BatchItem("early.pdf", "bb-trumpet", 3, FAILED, failed_stage="audiveris"),
        BatchItem(
            "multi.pdf",
            "bb-trumpet",
            3,
            PASSED,
            uncounted_reason=UncountedReason.DIFFERENT_COUNTS,
        ),
        BatchItem(
            "other-multi.pdf",
            "bb-trumpet",
            3,
            FAILED,
            suspect_count=1,
            failed_stage="fixup",
            uncounted_reason=UncountedReason.DIFFERENT_COUNTS,
        ),
    )
    _print_batch(BatchResult("Synthetic", items, check_cross_part(items), tmp_path / "batch.json"))
    warnings = [
        line for line in capsys.readouterr().err.splitlines() if line.startswith("warning:")
    ]
    assert warnings == [
        "warning: measures not counted (failed before durations): early",
        "warning: parts read different measure counts: multi, other-multi",
    ]
