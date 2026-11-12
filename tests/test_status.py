import json
import os
from dataclasses import replace

import pytest

from omr_ingestor.durations import ReviewSignal
from omr_ingestor.status import (
    ItemStatus,
    PartIdentity,
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
        "part_ids": ("P1",),
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


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6])
def test_reads_legacy_schema_versions_and_preserves_review(tmp_path, version):
    path = tmp_path / "status.json"
    data = sample().to_dict()
    del data["part_identities"]
    if version < 6:
        del data["part_ids"]
    if version < 5:
        del data["text_removals"]
    if version < 4:
        del data["review_signals"]
        del data["review_required"]
    if version < 3:
        del data["audiveris_options"]
    if version == 1:
        del data["measure_count"]
    data["schema_version"] = version
    path.write_text(json.dumps(data), encoding="utf-8")
    expected = sample(
        part_ids=None if version < 6 else ("P1",),
        audiveris_options=None if version < 3 else {},
        measures_read=None if version == 1 else 51,
    )
    assert read_status(path) == expected
    write_status(read_status(path), path)
    assert read_status(path) == expected
    assert json.loads(path.read_text())["schema_version"] == 7


def test_audiveris_options_round_trip(tmp_path):
    options = {
        "org.audiveris.omr.sheet.grid.StaffProjector.staffAbscissaMargin": "1.0",
        "org.audiveris.omr.image.ImageLoading.pdfResolution": "600",
    }
    path = tmp_path / "status.json"
    item = sample(audiveris_options=options)
    write_status(item, path)
    assert read_status(path) == item
    assert list(json.loads(path.read_text())["audiveris_options"]) == sorted(options)


def test_stable_key_order_and_bytes(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_status(sample(), a)
    write_status(sample(tool_versions={"audiveris": "5.11.0", "musescore": "4.7.5"}), b)
    assert a.read_bytes() == b.read_bytes()
    data = json.loads(a.read_text(encoding="utf-8"))
    assert list(data) == [
        "schema_version",
        "review_required",
        "review_signals",
        "text_removals",
        "pdf",
        "instrument",
        "expected_measures",
        "measure_count",
        "stages",
        "suspect_measures",
        "part_ids",
        "part_identities",
        "hand_corrected",
        "acceptance",
        "tool_versions",
        "audiveris_options",
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
        (lambda d: d.update(schema_version=8), "schema_version"),
        (lambda d: d.pop("audiveris_options"), "keys"),
        (lambda d: d.update(audiveris_options=[]), "audiveris_options"),
        (lambda d: d.update(audiveris_options={"constant": 600}), "audiveris_options"),
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


def test_review_signals_round_trip_and_validate_derived_state(tmp_path):
    signal = ReviewSignal("navigation_lyric", "navigation-like lyric 'Fine'", "P1", "2")
    item = sample(measures_read=60, suspect_measures=(), review_signals=(signal,))
    path = tmp_path / "status.json"
    write_status(item, path)
    assert read_status(path) == item
    assert read_status(path).review_required
    data = item.to_dict()
    data["review_required"] = False
    with pytest.raises(StatusError, match="review_required"):
        ItemStatus.from_dict(data)
    data = item.to_dict()
    data["review_signals"][0]["measure"] = 2
    with pytest.raises(StatusError, match="measure"):
        ItemStatus.from_dict(data)
    assert not sample(measures_read=60, suspect_measures=()).review_required


@pytest.mark.parametrize(
    "change", [{"extra": "unexpected"}, {"code": ""}, {"code": "unknown"}, {"measure": None}]
)
def test_invalid_signal_file_is_rejected(tmp_path, change):
    item = sample(review_signals=(ReviewSignal("part_identity", "check IDs"),))
    data = item.to_dict()
    data["review_signals"][0].update(change)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    with pytest.raises(StatusError, match="review_signals"):
        read_status(path)


def test_signal_file_with_missing_key_is_rejected(tmp_path):
    data = sample(review_signals=(ReviewSignal("part_identity", "check IDs"),)).to_dict()
    del data["review_signals"][0]["measure"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data))
    with pytest.raises(StatusError, match="review_signals"):
        read_status(path)


@pytest.mark.parametrize(
    "counts",
    [
        {},
        {"lyrics": 1},
        dict(credits=0, headers=0, lyrics=-1, words=0, rehearsals=0, technical=0),
        dict(credits=0, headers=0, lyrics=True, words=0, rehearsals=0, technical=0),
        dict(credits=0, headers=0, lyrics=1.5, words=0, rehearsals=0, technical=0),
    ],
)
def test_invalid_text_removals_are_rejected(counts):
    data = sample().to_dict()
    data["text_removals"] = counts
    with pytest.raises(StatusError, match="text_removals"):
        ItemStatus.from_dict(data)


def test_text_removals_round_trip(tmp_path):
    counts = dict(credits=1, headers=2, lyrics=3, words=4, rehearsals=5, technical=6)
    item = sample(text_removals=counts)
    path = tmp_path / "status.json"
    write_status(item, path)
    assert read_status(path) == item


def test_per_part_corrections_round_trip_and_independent_acceptance(tmp_path):
    item = sample(
        part_ids=("P1", "P2", "P3"),
        hand_corrected={"P1": ("7", "12a", "30"), "P2": ("7", "12a", "30", "31")},
    )
    path = tmp_path / "status.json"
    write_status(item, path)
    assert read_status(path) == item
    data = item.to_dict()
    assert data["hand_corrected"]["P1"][0] == data["hand_corrected"]["P2"][0] == "7"
    assert data["acceptance"]["P1"]["ratio"] == 0.05
    assert data["acceptance"]["P1"]["accepted"] is True
    assert data["acceptance"]["P2"]["ratio"] == 0.066667
    assert data["acceptance"]["P2"]["accepted"] is False
    assert data["acceptance"]["P3"] is None
    data["acceptance"]["P2"]["accepted"] = True
    with pytest.raises(StatusError, match="acceptance"):
        ItemStatus.from_dict(data)


def test_per_part_empty_review_is_distinct_from_unreviewed():
    item = sample(part_ids=("P1", "P2"), hand_corrected={"P1": ()})
    assert item.acceptance["P1"].ratio == 0
    assert item.acceptance["P1"].accepted
    assert item.acceptance["P2"] is None
    assert sample(part_ids=("P1", "P2"), hand_corrected={}).acceptance == {
        "P1": None,
        "P2": None,
    }


@pytest.mark.parametrize(
    "hand",
    [
        {"P1": ("7", "7")},
        {"P3": ()},
        {"P1": (7,)},
        {"P1": ("",)},
        {"P1": "7"},
    ],
)
def test_invalid_per_part_corrections_are_rejected(hand):
    with pytest.raises(StatusError):
        sample(part_ids=("P1", "P2"), hand_corrected=hand)


@pytest.mark.parametrize("ids", [None, ("P1",), ("P1", "P1"), ("P1", "")])
def test_per_part_corrections_require_valid_multipart_ids(ids):
    with pytest.raises(StatusError):
        sample(part_ids=ids, hand_corrected={"P1": ()})


@pytest.mark.parametrize("corrected", [(), ("7", "12a", "30"), ("7", "12a", "30", "31")])
def test_shared_multipart_corrections_have_unknown_acceptance(corrected):
    item = sample(part_ids=("P1", "P2"), hand_corrected=corrected)
    assert item.acceptance.accepted is None
    assert item.to_dict()["acceptance"]["accepted"] is None
    assert item.acceptance.ratio == round(len(corrected) / 60, 6)
    assert item.to_dict()["hand_corrected"] == list(corrected)


def test_flat_corrections_with_unknown_part_ids_have_unknown_acceptance():
    assert sample(part_ids=None).acceptance.accepted is None


@pytest.mark.parametrize("accepted", [True, False])
def test_reads_old_multipart_flat_acceptance_without_claiming_per_part_success(accepted):
    corrected = ("1",) if accepted else tuple(str(n) for n in range(4))
    data = sample(part_ids=("P1", "P2"), hand_corrected=corrected).to_dict()
    data["acceptance"]["accepted"] = accepted
    item = ItemStatus.from_dict(data)
    assert item.acceptance.accepted is None
    assert item.to_dict()["acceptance"]["accepted"] is None
    data["acceptance"]["accepted"] = not accepted
    with pytest.raises(StatusError, match="acceptance"):
        ItemStatus.from_dict(data)


@pytest.mark.parametrize("hand", [{"P1": [7]}, {"P1": "7"}, {"P1": ["7", "7"]}])
def test_invalid_per_part_correction_files_are_rejected(hand):
    data = sample(part_ids=("P1", "P2"), hand_corrected={}).to_dict()
    data["hand_corrected"] = hand
    with pytest.raises(StatusError, match="hand_corrected"):
        ItemStatus.from_dict(data)


def test_part_identities_round_trip_and_invalidate_only_affected_acceptance(tmp_path):
    item = sample(
        part_ids=("P1", "P2"),
        part_identities={
            "P1": PartIdentity("First", ("Trumpet", "Flugelhorn")),
            "P2": PartIdentity("", ()),
        },
        hand_corrected={"P1": (), "P2": ("7",)},
        review_signals=(ReviewSignal("part_identity_changed", "identity changed", part_id="P1"),),
    )
    path = tmp_path / "status.json"
    write_status(item, path)
    assert read_status(path) == item
    assert item.review_required
    assert item.acceptance["P1"] is None
    assert item.acceptance["P2"].accepted


@pytest.mark.parametrize(
    "identities",
    [
        [],
        {},
        {"P2": {"part_name": "Other", "instruments": []}},
        {"P1": {"part_name": "Name"}},
        {"P1": {"part_name": None, "instruments": []}},
        {"P1": {"part_name": "Name", "instruments": "Trumpet"}},
        {"P1": {"part_name": "Name", "instruments": [1]}},
        {"P1": {"part_name": "Name", "instruments": [], "extra": "invalid"}},
    ],
)
def test_rejects_malformed_part_identities(identities):
    data = sample().to_dict()
    data["part_identities"] = identities
    with pytest.raises(StatusError, match="part_identities"):
        ItemStatus.from_dict(data)


@pytest.mark.parametrize("part_id", ["", "P1"])
def test_structural_identity_signal_does_not_invalidate_part_acceptance(part_id):
    item = sample(
        part_ids=("P1", "P2"),
        hand_corrected={"P1": ()},
        review_signals=(ReviewSignal("part_identity", "duplicate IDs", part_id=part_id),),
    )
    assert item.acceptance["P1"].accepted
    assert item.review_required


@pytest.mark.parametrize("corrections", [(), ("7",)])
def test_changed_single_part_identity_withholds_acceptance_and_rejects_stale_block(
    tmp_path, corrections
):
    item = sample(
        part_ids=("P1",),
        part_identities={"P1": PartIdentity("Original")},
        hand_corrected=corrections,
        review_signals=(ReviewSignal("part_identity_changed", "identity changed", part_id="P1"),),
    )
    path = tmp_path / "status.json"
    write_status(item, path)
    assert read_status(path) == item
    assert item.acceptance is None
    data = item.to_dict()
    data["acceptance"] = replace(item, review_signals=()).to_dict()["acceptance"]
    with pytest.raises(StatusError, match="acceptance"):
        ItemStatus.from_dict(data)
