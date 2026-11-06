import json
import os

import pytest

from omr_ingestor.status import (
    ItemStatus,
    Stage,
    StageState,
    StatusError,
    SuspectMeasure,
    read_status,
    write_status,
)


def sample(**changes) -> ItemStatus:
    fields = {
        "pdf": "pdfs/song-trumpet.pdf",
        "instrument": "bb-trumpet",
        "expected_measures": 60,
        "measures_read": 51,
        "stages": (
            Stage("audiveris", StageState.PASSED, 41.25),
            Stage("durations", StageState.PASSED, 0.03, "2 suspect measures"),
            Stage("musescore", StageState.PENDING),
        ),
        "suspect_measures": (
            SuspectMeasure("7", "part P1 measure 7: 3 quarter(s), expected 4 (4/4)"),
            SuspectMeasure("12a"),
        ),
        "hand_corrected": ("7", "12a", "30"),
        "tool_versions": {"musescore": "4.7.5", "audiveris": "5.11.0"},
    }
    fields.update(changes)
    return ItemStatus(**fields)


def test_round_trip(tmp_path):
    path = tmp_path / "status.json"
    item = sample()
    write_status(item, path)
    assert read_status(path) == item


def test_round_trip_before_review(tmp_path):
    path = tmp_path / "status.json"
    item = sample(
        hand_corrected=None,
        measures_read=None,
        stages=(),
        suspect_measures=(),
        tool_versions={},
    )
    write_status(item, path)
    assert read_status(path) == item
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["acceptance"] is None
    assert data["measure_count"] is None


def test_measure_count_records_missing_and_extra_measures():
    assert sample().to_dict()["measure_count"] == {"read": 51, "expected": 60, "missing": 9}
    assert sample(measures_read=62).measure_count.missing == -2


def test_reads_schema_version_1(tmp_path):
    path = tmp_path / "status.json"
    data = sample().to_dict()
    del data["measure_count"]
    data["schema_version"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    assert read_status(path) == sample(measures_read=None)


def test_stable_key_order_and_bytes(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_status(sample(), a)
    write_status(sample(tool_versions={"audiveris": "5.11.0", "musescore": "4.7.5"}), b)
    assert a.read_bytes() == b.read_bytes()
    data = json.loads(a.read_text(encoding="utf-8"))
    assert list(data) == [
        "schema_version",
        "pdf",
        "instrument",
        "expected_measures",
        "measure_count",
        "stages",
        "suspect_measures",
        "hand_corrected",
        "acceptance",
        "tool_versions",
    ]
    assert list(data["stages"][0]) == ["name", "status", "duration_s", "detail"]
    assert list(data["tool_versions"]) == ["audiveris", "musescore"]
    assert data["acceptance"] == {
        "hand_corrected_count": 3,
        "expected_measures": 60,
        "ratio": 0.05,
        "limit": 0.05,
        "accepted": True,
    }


@pytest.mark.parametrize(
    ("corrected", "expected_measures", "accepted"),
    [(3, 60, True), (4, 60, False), (0, 1, True), (1, 20, True), (1, 19, False)],
)
def test_acceptance_is_five_percent_of_expected_measures(corrected, expected_measures, accepted):
    item = sample(
        expected_measures=expected_measures,
        hand_corrected=tuple(str(n) for n in range(corrected)),
    )
    assert item.acceptance.accepted is accepted
    assert item.acceptance.ratio == round(corrected / expected_measures, 6)


def test_acceptance_is_none_before_review():
    assert sample(hand_corrected=None).acceptance is None


def test_with_stage_replaces_and_keeps_pipeline_order():
    item = sample().with_stage(Stage("musescore", StageState.FAILED, 3.0, "exit 134"))
    item = item.with_stage(Stage("fixup", StageState.PASSED, 0.1))
    assert [s.name for s in item.stages] == ["audiveris", "durations", "fixup", "musescore"]
    assert item.stage("musescore").status is StageState.FAILED


def test_write_leaves_no_temp_file_and_keeps_old_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    write_status(sample(), path)
    before = path.read_bytes()

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_status(sample(hand_corrected=()), path)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Stage("unknown", StageState.PASSED), "unknown stage"),
        (lambda: Stage("xsd", StageState.PASSED, -1.0), "invalid duration"),
        (lambda: sample(expected_measures=0), "positive integer"),
        (lambda: sample(measures_read=-1), "non-negative integer"),
        (lambda: sample(hand_corrected=("1", "1")), "duplicate measure"),
        (
            lambda: sample(stages=(Stage("xsd", StageState.PASSED),) * 2),
            "duplicate stage",
        ),
    ],
)
def test_invalid_values_raise(make, message):
    with pytest.raises(StatusError, match=message):
        make()


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d["acceptance"].update(accepted=False), "does not match"),
        (lambda d: d.update(schema_version=3), "schema_version"),
        (lambda d: d["measure_count"].update(missing=0), "measure_count"),
        (lambda d: d.update(measure_count=None, schema_version=1), "keys"),
        (lambda d: d.update(extra=1), "keys"),
        (lambda d: d["stages"][0].update(status="done"), "malformed status"),
        (lambda d: d.update(expected_measures="60"), "expected_measures"),
    ],
)
def test_read_rejects_bad_files(tmp_path, edit, message):
    path = tmp_path / "status.json"
    data = sample().to_dict()
    edit(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StatusError, match=message):
        read_status(path)
