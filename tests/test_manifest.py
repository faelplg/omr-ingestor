import json

import pytest

from omr_ingestor.cli import main
from omr_ingestor.instruments import load_instruments
from omr_ingestor.manifest import ManifestError, load_manifest, validate_manifest
from omr_ingestor.paths import SCHEMA_PATH
from tests.conftest import EXAMPLE_PATH


def problems_of(data) -> list[str]:
    with pytest.raises(ManifestError) as info:
        validate_manifest(data)
    return info.value.problems


def test_example_manifest_is_valid():
    data = load_manifest(EXAMPLE_PATH)
    assert len(data["items"]) == 2


@pytest.mark.parametrize(
    "options,valid",
    [
        ({}, True),
        ({"org.audiveris.omr.sheet.grid.StaffProjector.staffAbscissaMargin": "1.0"}, True),
        ({"org.audiveris.omr.image.ImageLoading.pdfResolution": "600"}, True),
        ({"org.audiveris.omr.image.ImageLoading.pdfResolution": 600}, False),
        ({"-run": "SomeClass"}, False),
        ({"org.audiveris.omr.image.ImageLoading.pdfResolution": "600\nfoo=true"}, False),
        ({"org.audiveris.omr.image.ImageLoading.pdfResolution": "600\n"}, False),
        ({"org.audiveris.omr.image.ImageLoading.pdfResolution": "600\r\n"}, False),
        ({"org.audiveris.omr.image.ImageLoading.pdfResolution\n": "600"}, False),
        ({"org.audiveris.omr.image.ImageLoading.pdfResolution\r\n": "600"}, False),
    ],
)
def test_audiveris_options_schema(example, options, valid):
    example["items"][0]["audiveris_options"] = options
    if valid:
        validate_manifest(example)
    else:
        assert any("audiveris_options" in p for p in problems_of(example))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.pop("style"), "<root>: 'style' is a required property"),
        (lambda d: d.update(items=[]), "items: [] should be non-empty"),
        (lambda d: d.update(extra=1), "<root>: Additional properties are not allowed"),
        (lambda d: d["items"][0].pop("sha256"), "items[0]: 'sha256' is a required property"),
        (
            lambda d: d["items"][1].update(instrument="piano"),
            'items[1].instrument: "piano" is not allowed; expected one of: "bb-trumpet"',
        ),
        (
            lambda d: d["items"][0].update(sha256="ABC"),
            'items[0].sha256: "ABC" is not a lowercase hex sha256',
        ),
        (
            lambda d: d["items"][0].update(expected_measures=0),
            "items[0].expected_measures: 0 is less",
        ),
        (lambda d: d["items"][0].update(expected_measures=1.5), "is not of type 'integer'"),
    ],
)
def test_invalid_manifest_reports_location(example, mutate, expected):
    mutate(example)
    problems = problems_of(example)
    assert any(p.startswith(expected) or expected in p for p in problems), problems


def test_all_problems_are_reported_at_once(example):
    example["items"][0]["instrument"] = "piano"
    example["items"][1]["expected_measures"] = -3
    problems = problems_of(example)
    assert [p.split(":")[0] for p in problems] == [
        "items[0].instrument",
        "items[1].expected_measures",
    ]


def test_invalid_json_file_names_line(write_json):
    path = write_json('{"title": "x",\n "items": [}')
    with pytest.raises(ManifestError, match=r"not valid JSON at line 2"):
        load_manifest(path)


def test_missing_file(tmp_path):
    with pytest.raises(ManifestError, match="cannot read file"):
        load_manifest(tmp_path / "nope.json")


def test_schema_enum_matches_instruments_json():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    enum = schema["properties"]["items"]["items"]["properties"]["instrument"]["enum"]
    assert sorted(enum) == sorted(load_instruments().ids)


def test_cli_validate(capsys, example, write_json):
    assert main(["validate", str(EXAMPLE_PATH)]) == 0
    assert "OK (2 item(s))" in capsys.readouterr().out

    example["items"][0]["instrument"] = "piano"
    assert main(["validate", str(write_json(example))]) == 1
    assert "items[0].instrument" in capsys.readouterr().err
