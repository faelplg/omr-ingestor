"""Synthetic text cleanup coverage; no score-derived fixtures."""

import os
import xml.etree.ElementTree as ET

import pytest

from omr_ingestor.fixup import TEXT_REMOVAL_CATEGORIES, strip_text
from omr_ingestor.musicxml import parse_musicxml, write_musicxml
from omr_ingestor.xsd import validate_xsd
from tests.musicxml_builders import DEFAULT_ATTRS, measure, score

HEADER = (
    "<work><work-number>1</work-number><work-title>Title</work-title></work>"
    "<movement-number>2</movement-number><movement-title>Movement</movement-title>"
    '<identification><creator type="composer">Composer</creator><rights>Rights</rights>'
    "<encoding><software>Synthetic</software></encoding><source>Source</source>"
    '<relation>Related work</relation><miscellaneous><miscellaneous-field name="x">'
    "Header</miscellaneous-field></miscellaneous></identification>"
    "<credit><credit-words>Credit</credit-words></credit>"
)
TECHNICAL = (
    "<fingering>1</fingering><pluck>p</pluck><fret>3</fret><string>1</string>"
    '<hammer-on type="start">H</hammer-on><pull-off type="stop">P</pull-off>'
    "<tap>T</tap><other-technical>Text</other-technical>"
)
NOTE = (
    "<note><pitch><step>C</step><alter>1</alter><octave>4</octave></pitch>"
    '<duration>4</duration><tie type="start"/><type>whole</type><accidental>sharp</accidental>'
    "<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes>"
    '</time-modification><notations><tied type="start"/><slur type="start"/>'
    '<tuplet type="start"/><articulations><staccato/></articulations>'
    "<technical><up-bow/>" + TECHNICAL + "</technical><dynamics><f/></dynamics></notations>"
    "<lyric><text>D.C. al Fine</text></lyric></note>"
)
DIRECTIONS = (
    "<direction><direction-type><words>Free text</words></direction-type></direction>"
    "<direction><direction-type><rehearsal>A</rehearsal></direction-type></direction>"
    "<direction><direction-type><words>D.C.</words></direction-type>"
    '<offset>2</offset><sound dacapo="yes"/></direction>'
    '<direction><direction-type><segno/></direction-type><sound segno="s"/></direction>'
    '<direction><direction-type><coda/></direction-type><sound coda="c"/></direction>'
    "<direction><direction-type><words>Loud</words></direction-type>"
    "<direction-type><dynamics><ff/></dynamics></direction-type></direction>"
)


def synthetic_score():
    attrs = DEFAULT_ATTRS.replace("<time>", "<key><fifths>2</fifths></key><time>").replace(
        "</attributes>", "<clef><sign>G</sign><line>2</line></clef></attributes>"
    )
    score_parts = "".join(
        f'<score-part id="{p}"><part-name>Trumpet {p}</part-name>'
        f'<score-instrument id="{p}-I1"><instrument-name>Trumpet</instrument-name>'
        "<instrument-sound>brass.trumpet</instrument-sound></score-instrument>"
        f'<midi-instrument id="{p}-I1"><midi-channel>1</midi-channel>'
        "<midi-program>57</midi-program></midi-instrument></score-part>"
        for p in ("P1", "P2")
    )
    return parse_musicxml(
        score(
            {
                p: [
                    measure(
                        1,
                        attrs
                        + DIRECTIONS
                        + NOTE
                        + (
                            "<note><rest/><duration>1</duration></note>"
                            '<barline location="right"><ending number="1" type="start"/>'
                            '<repeat direction="backward"/></barline>'
                        ),
                    )
                ]
                for p in ("P1", "P2")
            },
            header=HEADER,
            score_parts=score_parts,
        )
    )


def xml(el):
    return ET.tostring(el)


def test_all_categories_are_removed_in_every_part_and_counted():
    root = synthetic_score()
    assert strip_text(root) == dict(
        credits=1, headers=8, lyrics=2, words=6, rehearsals=2, technical=16
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
        "words",
        "rehearsal",
        "fingering",
        "pluck",
        "fret",
        "string",
        "hammer-on",
        "pull-off",
        "tap",
        "other-technical",
    ):
        assert root.find(".//" + tag) is None, tag
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
    "offset,sound,expected",
    [
        ("<offset>3</offset>", '<sound dacapo="yes"/>', "3"),
        ('<offset sound="no">3</offset>', '<sound dacapo="yes"/>', None),
        ("<offset>3</offset>", '<sound dacapo="yes"><offset>5</offset></sound>', "5"),
    ],
)
def test_text_only_direction_keeps_sound_and_playback_offset(offset, sound, expected):
    root = parse_musicxml(
        score(
            {
                "P1": [
                    measure(
                        1,
                        "<direction><direction-type><words>D.C.</words></direction-type>"
                        + offset
                        + sound
                        + "</direction>",
                    )
                ]
            }
        )
    )
    strip_text(root)
    assert root.find(".//direction") is None
    snd = root.find("part/measure/sound")
    assert snd is not None and snd.get("dacapo") == "yes"
    assert snd.findtext("offset") == expected


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
