"""CLI review tests with synthetic raw scores and real status serialization."""

import json
from dataclasses import replace

import pytest

from omr_ingestor.cli import main
from omr_ingestor.durations import ReviewSignal
from omr_ingestor.status import (
    ItemStatus,
    PartIdentity,
    Stage,
    StageState,
    read_status,
    write_status,
)
from tests.musicxml_builders import measure, score


@pytest.fixture
def item(tmp_path):
    raw = tmp_path / "audiveris" / "song"
    raw.mkdir(parents=True)
    (raw / "song.musicxml").write_text(
        score({"P1": [measure(n, "") for n in ("0", "7", "12a", "30")]}), encoding="utf-8"
    )
    write_status(ItemStatus("song.pdf", "bb-trumpet", 60), tmp_path / "status.json")
    return tmp_path


def multipart(item):
    raw = next((item / "audiveris").rglob("*.musicxml"))
    raw.write_text(
        score({pid: [measure(n, "") for n in ("7", "12a", "30")] for pid in ("P1", "P2")}),
        encoding="utf-8",
    )
    return item


def snapshot(item):
    return {str(p.relative_to(item)): p.read_bytes() for p in item.rglob("*") if p.is_file()}


def test_review_single_part_round_trip_acceptance(item, capsys):
    before = snapshot(item)
    assert main(["review", str(item), "0", "12a"]) == 0
    status = read_status(item / "status.json")
    assert status.hand_corrected == ("0", "12a")
    assert status.part_ids == ("P1",)
    assert status.part_identities == {"P1": PartIdentity("OMR P1")}
    data = json.loads(capsys.readouterr().out)
    assert data == status.to_dict()
    assert data["schema_version"] == 7
    assert data["acceptance"]["ratio"] == 0.033333
    assert data["acceptance"]["accepted"] is True
    assert {k: v for k, v in snapshot(item).items() if k != "status.json"} == {
        k: v for k, v in before.items() if k != "status.json"
    }


def test_review_multipart_keeps_unreviewed_null(item):
    multipart(item)
    assert main(["review", str(item), "7", "--part", "P1"]) == 0
    status = read_status(item / "status.json")
    assert status.hand_corrected == {"P1": ("7",)}
    assert status.acceptance["P1"].ratio == 0.016667
    assert status.acceptance["P2"] is None
    assert main(["review", str(item), "--part", "P2", "--replace"]) == 0
    status = read_status(item / "status.json")
    assert status.hand_corrected == {"P1": ("7",), "P2": ()}
    assert status.acceptance["P2"].accepted is True


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["--part", "missing"], "unknown part"),
        (["99"], "measure '99' not found in part P1 of"),
        (["7", "7"], "duplicate measure numbers in review: ['7']"),
        ([""], "measure '' not found in part P1 of"),
    ],
)
def test_review_rejects_invalid_input_without_writing(item, capsys, args, error):
    before = snapshot(item)
    assert main(["review", str(item), *args]) == 1
    assert error in capsys.readouterr().err
    assert snapshot(item) == before


def test_review_requires_part_for_multipart(item, capsys):
    multipart(item)
    before = snapshot(item)
    assert main(["review", str(item), "7"]) == 1
    assert "requires --part" in capsys.readouterr().err
    assert snapshot(item) == before


@pytest.mark.parametrize("multi", [False, True])
def test_review_add_replace_and_duplicate(item, multi):
    if multi:
        multipart(item)
    suffix = ["--part", "P1"]
    assert main(["review", str(item), "7", *suffix]) == 0
    assert main(["review", str(item), "12a", *suffix]) == 0
    status = read_status(item / "status.json")
    assert (status.hand_corrected["P1"] if multi else status.hand_corrected) == ("7", "12a")
    before = snapshot(item)
    assert main(["review", str(item), "7", *suffix]) == 1
    assert snapshot(item) == before
    assert main(["review", str(item), "30", *suffix, "--replace"]) == 0
    status = read_status(item / "status.json")
    assert (status.hand_corrected["P1"] if multi else status.hand_corrected) == ("30",)
    assert main(["review", str(item), *suffix, "--replace"]) == 0
    status = read_status(item / "status.json")
    assert (status.hand_corrected["P1"] if multi else status.hand_corrected) == ()


def test_review_dry_run_writes_nothing(item, capsys):
    multipart(item)
    before = snapshot(item)
    assert main(["review", str(item), "7", "--part", "P1", "--dry-run"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert ItemStatus.from_dict(data).acceptance["P1"].accepted is True
    assert data["acceptance"]["P2"] is None
    assert snapshot(item) == before


def test_review_renews_only_selected_changed_identity(item):
    multipart(item)
    assert main(["review", str(item), "7", "--part", "P1"]) == 0
    assert main(["review", str(item), "12a", "--part", "P2"]) == 0
    path = item / "status.json"
    prior = read_status(path)
    write_status(
        replace(
            prior,
            part_identities={**prior.part_identities, "P1": PartIdentity("Old name")},
            review_signals=(
                ReviewSignal("part_identity_changed", "changed", part_id="P1"),
                ReviewSignal("navigation_lyric", "review navigation", part_id="P1"),
            ),
        ),
        path,
    )
    before = snapshot(item)
    assert main(["review", str(item), "30", "--part", "P1"]) == 1
    assert snapshot(item) == before
    assert main(["review", str(item), "30", "--part", "P1", "--replace"]) == 0
    renewed = read_status(path)
    assert renewed.part_identities == prior.part_identities
    assert renewed.hand_corrected == {"P1": ("30",), "P2": ("12a",)}
    assert renewed.acceptance["P1"].accepted is True
    assert renewed.acceptance["P2"] == prior.acceptance["P2"]
    assert [s.code for s in renewed.review_signals] == ["navigation_lyric"]


def test_review_legacy_flat_requires_explicit_replacement(item):
    multipart(item)
    path = item / "status.json"
    write_status(replace(read_status(path), hand_corrected=("7",)), path)
    assert main(["review", str(item), "12a", "--part", "P1"]) == 1
    assert main(["review", str(item), "12a", "--part", "P1", "--replace"]) == 0
    status = read_status(path)
    assert status.hand_corrected == {"P1": ("12a",)}
    assert status.part_identities is not None
    assert status.acceptance["P2"] is None


def test_review_preserves_legacy_provenance_for_other_parts(item):
    multipart(item)
    path = item / "status.json"
    write_status(
        replace(read_status(path), part_ids=("P1", "P2"), hand_corrected={"P2": ("7",)}), path
    )
    assert main(["review", str(item), "12a", "--part", "P1"]) == 0
    status = read_status(path)
    assert status.part_identities is None
    assert status.hand_corrected == {"P1": ("12a",), "P2": ("7",)}


@pytest.mark.parametrize("failure", ["missing", "multiple", "duplicate_ids", "invalid_status"])
def test_review_rejects_unsafe_artifacts(item, failure, capsys):
    raw = next((item / "audiveris").rglob("*.musicxml"))
    if failure == "missing":
        raw.unlink()
    elif failure == "multiple":
        raw.with_name("other.musicxml").write_bytes(raw.read_bytes())
    elif failure == "duplicate_ids":
        raw.write_text(
            score({"P1": [measure("7", "")]}).replace(
                "</score-partwise>", '<part id="P1"/></score-partwise>'
            )
        )
    else:
        data = json.loads((item / "status.json").read_text())
        data["acceptance"] = {}
        (item / "status.json").write_text(json.dumps(data))
    before = snapshot(item)
    assert main(["review", str(item), "7"]) == 1
    error = capsys.readouterr().err
    expected = {
        "missing": "expected one raw Audiveris MusicXML, found 0",
        "multiple": "expected one raw Audiveris MusicXML, found 2",
        "duplicate_ids": f"{raw}: unsafe part identity",
        "invalid_status": "does not match hand_corrected",
    }
    assert expected[failure] in error
    assert snapshot(item) == before


def test_review_adding_to_legacy_review_does_not_invent_identity(item):
    path = item / "status.json"
    write_status(replace(read_status(path), hand_corrected=("7",)), path)
    assert main(["review", str(item), "12a"]) == 0
    status = read_status(path)
    assert status.part_identities is None
    assert status.hand_corrected == ("7", "12a")
    assert main(["review", str(item), "30", "--replace"]) == 0
    assert read_status(path).part_identities == {"P1": PartIdentity("OMR P1")}


def test_review_rejects_measure_from_other_part(item):
    multipart(item)
    raw = next((item / "audiveris").rglob("*.musicxml"))
    raw.write_text(score({"P1": [measure("7", "")], "P2": [measure("12a", "")]}))
    before = snapshot(item)
    assert main(["review", str(item), "12a", "--part", "P1"]) == 1
    assert snapshot(item) == before


def test_review_rejects_ambiguous_measure_number(item):
    raw = next((item / "audiveris").rglob("*.musicxml"))
    raw.write_text(score({"P1": [measure("7", ""), measure("7", "")]}))
    before = snapshot(item)
    assert main(["review", str(item), "7"]) == 1
    assert snapshot(item) == before


def test_review_write_failure_preserves_status(item, monkeypatch, capsys):
    def fail_replace(*args):
        raise OSError("synthetic atomic replacement failure")

    monkeypatch.setattr("omr_ingestor.status.os.replace", fail_replace)
    before = snapshot(item)
    assert main(["review", str(item), "7"]) == 1
    assert "atomic replacement failure" in capsys.readouterr().err
    assert snapshot(item) == before


def test_review_rejects_missing_recorded_measure(item, capsys):
    path = item / "status.json"
    write_status(replace(read_status(path), hand_corrected=("99",)), path)
    before = snapshot(item)
    assert main(["review", str(item), "7"]) == 1
    assert "prior measure '99' is absent or ambiguous" in capsys.readouterr().err
    assert snapshot(item) == before


@pytest.mark.parametrize("multi", [False, True])
def test_review_empty_requires_explicit_replacement(item, multi, capsys):
    if multi:
        multipart(item)
    before = snapshot(item)
    assert main(["review", str(item), "--part", "P1"]) == 1
    assert "no measures given; use --replace to record a review with no corrections" in (
        capsys.readouterr().err
    )
    assert snapshot(item) == before


def test_review_added_part_allows_changed_identity_replacement(item, capsys):
    multipart(item)
    assert main(["review", str(item), "7", "--part", "P1"]) == 0
    assert main(["review", str(item), "12a", "--part", "P2"]) == 0
    path = item / "status.json"
    prior = read_status(path)
    write_status(
        replace(
            prior,
            review_signals=(ReviewSignal("part_identity_changed", "changed", part_id="P1"),),
            stages=(Stage("durations", StageState.FAILED, detail="part identity changed"),),
        ),
        path,
    )
    raw = next((item / "audiveris").rglob("*.musicxml"))
    raw.write_text(
        score({pid: [measure("7", ""), measure("12a", "")] for pid in ("P1", "P2", "P3")}).replace(
            "OMR P1", "Changed P1"
        )
    )
    capsys.readouterr()
    assert main(["review", str(item), "7", "--part", "P1", "--replace"]) == 0
    status = read_status(path)
    assert status.part_ids == ("P1", "P2", "P3")
    assert status.part_identities == {
        "P1": PartIdentity("Changed P1"),
        "P2": PartIdentity("OMR P2"),
        "P3": PartIdentity("OMR P3"),
    }
    assert status.hand_corrected == prior.hand_corrected
    assert status.acceptance["P1"].accepted
    assert status.acceptance["P2"] == prior.acceptance["P2"]
    assert status.acceptance["P3"] is None
    assert status.review_signals == ()
    assert "rerun to complete failed stages" in capsys.readouterr().err


def test_review_missing_part_requires_archive_and_rerun(item, capsys):
    multipart(item)
    assert main(["review", str(item), "7", "--part", "P1"]) == 0
    raw = next((item / "audiveris").rglob("*.musicxml"))
    raw.write_text(score({"P1": [measure("7", "")]}))
    capsys.readouterr()
    before = snapshot(item)
    assert main(["review", str(item), "7", "--part", "P1", "--replace"]) == 1
    assert "archive status.json and rerun" in capsys.readouterr().err
    assert snapshot(item) == before


@pytest.mark.parametrize("command", ["run", "batch", "validate", "instruments"])
def test_review_error_handling_does_not_hide_other_command_errors(monkeypatch, command):
    def fail(*args, **kwargs):
        raise OSError("synthetic unrelated command failure")

    entry = {
        "run": "run_item",
        "batch": "run_batch",
        "validate": "load_manifest",
        "instruments": "load_instruments",
    }
    monkeypatch.setattr(f"omr_ingestor.cli.{entry[command]}", fail)
    args = {
        "run": ["music.json", "0", "-o", "output"],
        "batch": ["music.json", "-o", "output"],
        "validate": ["music.json"],
        "instruments": [],
    }
    with pytest.raises(OSError, match="unrelated command failure"):
        main([command, *args[command]])
