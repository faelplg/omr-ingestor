"""Synthetic text cleanup coverage; no score-derived fixtures."""

import os
import xml.etree.ElementTree as ET

import pytest

from omr_ingestor.fixup import TEXT_REMOVAL_CATEGORIES, strip_text
from omr_ingestor.musicxml import parse_musicxml, write_musicxml
from omr_ingestor.xsd import validate_xsd
from tests.musicxml_builders import measure, score, synthetic_score


def xml(el):
    return ET.tostring(el)


def test_all_categories_are_removed_in_every_part_and_counted():
    root = synthetic_score()
    assert strip_text(root) == dict(
        credits=1, headers=8, lyrics=2, words=6, rehearsals=2, technical=6
    )
    for tag in (
        "credit",
        "work",
        "movement-title",
        "movement-number",
        "creator",
        "rights",
        "source",
        "relation",
        "miscellaneous",
        "lyric",
        "rehearsal",
        "fingering",
        "pluck",
        "other-technical",
    ):
        assert root.find(".//" + tag) is None, tag
    assert [w.text for w in root.iter("words")] == ["D.C.", "D.S. al Coda", "To Coda"] * 2
    assert root.findtext("identification/encoding/software") == "Synthetic"
    once = xml(root)
    assert strip_text(root) == dict.fromkeys(TEXT_REMOVAL_CATEGORIES, 0)
    assert xml(root) == once


@pytest.mark.parametrize(
    "path",
    [
        ".//pitch",
        ".//rest",
        ".//duration",
        ".//accidental",
        ".//articulations",
        ".//tie",
        ".//tied",
        ".//slur",
        ".//tuplet",
        ".//time-modification",
        ".//dynamics",
        ".//barline",
        ".//repeat",
        ".//ending",
        ".//time",
        ".//key",
        ".//clef",
        ".//score-instrument",
        ".//midi-instrument",
        ".//part-name",
        ".//segno",
        ".//coda",
        ".//up-bow",
        ".//fret",
        ".//string",
        ".//hammer-on",
        ".//pull-off",
        ".//tap",
        ".//symbol",
        ".//sound",
    ],
)
def test_musical_content_survives_exactly(path):
    root = synthetic_score()
    before = [xml(el) for el in root.findall(path)]
    assert before
    strip_text(root)
    assert [xml(el) for el in root.findall(path)] == before


def test_keep_text_preserves_the_entire_document():
    root = synthetic_score()
    before = xml(root)
    assert strip_text(root, keep_text=True) == dict.fromkeys(TEXT_REMOVAL_CATEGORIES, 0)
    assert xml(root) == before


@pytest.mark.parametrize(
    "attribute,text",
    [
        ("dacapo", "D.C. al Fine"),
        ("dalsegno", "D.S. al Coda"),
        ("fine", "Fine"),
        ("tocoda", "To Coda"),
        ("coda", "Coda"),
        ("segno", "Segno"),
    ],
)
@pytest.mark.parametrize("offset", ["<offset>3</offset>", '<offset sound="yes">3</offset>'])
def test_navigation_direction_stays_unchanged(attribute, text, offset):
    direction = (
        '<direction placement="above"><direction-type><words>'
        + text
        + "</words></direction-type>"
        + offset
        + "<voice>1</voice><staff>1</staff>"
        + f'<sound {attribute}="yes"/></direction>'
    )
    root = parse_musicxml(score({"P1": [measure(1, direction)]}))
    before = xml(root)
    assert strip_text(root)["words"] == 0
    assert xml(root) == before


def test_ds_al_coda_and_to_coda_stay_unchanged_together():
    root = synthetic_score()
    before = [xml(d) for d in root.findall(".//direction") if d.find("sound") is not None]
    strip_text(root)
    assert [xml(d) for d in root.findall(".//direction") if d.find("sound") is not None] == before
    assert [w.text for w in root.iter("words")] == ["D.C.", "D.S. al Coda", "To Coda"] * 2


def test_words_and_symbol_direction_keeps_its_symbol():
    root = parse_musicxml(
        score(
            {
                "P1": [
                    measure(
                        1,
                        "<direction><direction-type><words>solo</words>"
                        "<symbol>coda</symbol></direction-type></direction>",
                    )
                ]
            }
        )
    )
    assert strip_text(root)["words"] == 1
    assert xml(root.find(".//direction")) == (
        b"<direction><direction-type><symbol>coda</symbol></direction-type></direction>"
    )


def test_empty_wrappers_are_removed_but_mixed_notations_survive():
    root = parse_musicxml(
        score(
            {
                "P1": [
                    measure(
                        1,
                        "<note><rest/><duration>1</duration><notations><technical>"
                        "<fingering>1</fingering></technical></notations></note>",
                    )
                ]
            }
        )
    )
    strip_text(root)
    assert root.find(".//notations") is None
    root = synthetic_score()
    strip_text(root)
    assert root.find(".//notations/technical/up-bow") is not None
    assert all(d.find("direction-type") is not None for d in root.iter("direction"))


@pytest.mark.skipif(not os.environ.get("OMR_MUSICXML_XSD"), reason="local MusicXML XSD needed")
def test_stripping_synthetic_score_preserves_xsd_validity(tmp_path):
    root = synthetic_score()
    path = tmp_path / "synthetic.musicxml"
    write_musicxml(root, path)
    validate_xsd(path)
    strip_text(root)
    write_musicxml(root, path)
    validate_xsd(path)


def test_notes_without_text_remain_identical_including_empty_notations():
    root = parse_musicxml(
        score(
            {
                "P1": [
                    measure(
                        1,
                        "<note><rest/><duration>1</duration><notations><technical/></notations></note>",
                    )
                ]
            }
        )
    )
    before = xml(root.find(".//note"))
    strip_text(root)
    assert xml(root.find(".//note")) == before
