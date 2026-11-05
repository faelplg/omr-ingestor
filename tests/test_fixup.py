import xml.etree.ElementTree as ET

import pytest

from omr_ingestor.durations import check_durations
from omr_ingestor.fixup import FixupError, fix_instrument, fix_instrument_file
from omr_ingestor.instruments import load_instruments
from omr_ingestor.musicxml import MusicXMLError, parse_musicxml, read_musicxml
from tests.musicxml_builders import measure, note, score

INSTRUMENTS = load_instruments()

OMR_HEADER = (
    "<defaults><scaling><millimeters>7</millimeters><tenths>40</tenths></scaling>"
    "<page-layout><page-height>1700</page-height><page-width>1200</page-width></page-layout>"
    "<system-layout><system-distance>120</system-distance></system-layout>"
    "<staff-layout><staff-distance>60</staff-distance></staff-layout></defaults>"
)
OMR_SCORE_PART = (
    '<score-part id="P1"><part-name>Voice</part-name>'
    "<part-name-display><display-text>Voice</display-text></part-name-display>"
    '<score-instrument id="P1-I1"><instrument-name>Piano</instrument-name></score-instrument>'
    '<score-instrument id="P1-I2"><instrument-name>Organ</instrument-name></score-instrument>'
    '<midi-instrument id="P1-I1"><midi-channel>3</midi-channel>'
    "<midi-program>1</midi-program><volume>80</volume></midi-instrument>"
    "</score-part>"
)
FIRST_ATTRS = (
    "<attributes><divisions>1</divisions><key><fifths>0</fifths></key>"
    "<time><beats>4</beats><beat-type>4</beat-type></time>"
    "<clef><sign>F</sign><line>4</line></clef>"
    "<transpose><diatonic>0</diatonic><chromatic>-12</chromatic></transpose></attributes>"
)
PRINT = (
    '<print new-system="yes"><system-layout>'
    "<system-distance>90</system-distance></system-layout></print>"
)
C_CLEF = "<attributes><clef><sign>C</sign><line>4</line></clef></attributes>"
Q = note(1)
I2_NOTE = note(1, extra='<instrument id="P1-I2"/>')


def omr_score() -> str:
    return score(
        {
            "P1": [
                measure(1, PRINT + FIRST_ATTRS + Q * 4, attrs=' width="300"'),
                measure(2, Q * 3, attrs=' width="250"'),
                measure(3, PRINT + C_CLEF + I2_NOTE * 4),
            ]
        },
        header=OMR_HEADER,
        score_parts=OMR_SCORE_PART,
    )


def fixed(instrument_id: str, **kwargs) -> ET.Element:
    root = parse_musicxml(omr_score())
    fix_instrument(root, INSTRUMENTS[instrument_id], **kwargs)
    return root


def child_tags(el: ET.Element) -> list[str]:
    return [c.tag for c in el]


def test_tenor_sax_gets_transpose_clef_name_and_sound():
    root = fixed("tenor-saxophone")
    attrs = root.find("part/measure/attributes")
    assert child_tags(attrs) == ["divisions", "key", "time", "clef", "transpose"]
    assert ET.tostring(attrs.find("clef"), encoding="unicode") == (
        "<clef><sign>G</sign><line>2</line></clef>"
    )
    assert ET.tostring(attrs.find("transpose"), encoding="unicode") == (
        "<transpose><diatonic>-1</diatonic><chromatic>-2</chromatic>"
        "<octave-change>-1</octave-change></transpose>"
    )
    sp = root.find("part-list/score-part")
    assert child_tags(sp) == ["part-name", "score-instrument", "midi-instrument"]
    assert sp.findtext("part-name") == "Sax tenor em Sib"
    assert sp.find("score-instrument").get("id") == "P1-I1"
    assert sp.findtext("score-instrument/instrument-name") == "Sax tenor em Sib"
    assert sp.findtext("score-instrument/instrument-sound") == "wind.reed.saxophone.tenor"
    midi = sp.find("midi-instrument")
    assert (midi.get("id"), midi.findtext("midi-channel"), midi.findtext("midi-program")) == (
        "P1-I1",
        "3",
        "67",
    )
    assert {i.get("id") for i in root.iterfind("part/measure/note/instrument")} == {"P1-I1"}


def test_concert_pitch_instrument_has_no_transpose_and_one_clef():
    root = fixed("trombone")
    assert root.find(".//transpose") is None
    clefs = root.findall(".//clef")
    assert len(clefs) == 1
    assert (clefs[0].findtext("sign"), clefs[0].findtext("line")) == ("F", "4")
    # The attributes that only held the clef change are gone.
    assert root.find("part/measure[@number='3']/attributes") is None


def test_page_layout_is_removed():
    root = fixed("flute")
    assert root.find(".//print") is None
    assert child_tags(root.find("defaults")) == ["scaling"]
    assert all("width" not in m.attrib for m in root.iter("measure"))


def test_suspect_measures_get_a_rehearsal_mark_once():
    root = parse_musicxml(omr_score())
    suspects = check_durations(root).suspect_numbers("P1")
    assert suspects == ["2"]
    for _ in range(2):
        fix_instrument(root, INSTRUMENTS["bb-trumpet"], suspect_measures=suspects)
    m2 = root.find("part/measure[@number='2']")
    assert child_tags(m2)[:2] == ["direction", "note"]
    assert len(m2.findall("direction")) == 1
    assert m2.find("direction").get("placement") == "above"
    assert m2.findtext("direction/direction-type/rehearsal") == "?"
    # Fixing twice gives the same document as fixing once.
    once = fixed("bb-trumpet", suspect_measures=suspects)
    assert ET.tostring(root) == ET.tostring(once)


def test_mark_goes_after_leading_attributes():
    root = fixed("tuba", suspect_measures=["1"], mark_text="check")
    assert child_tags(root.find("part/measure"))[:3] == ["attributes", "direction", "note"]
    assert root.findtext(".//rehearsal") == "check"


def test_missing_attributes_and_score_instrument_are_created():
    xml = score({"P1": [measure(1, note(4))]})
    root = parse_musicxml(xml)
    fix_instrument(root, INSTRUMENTS["alto-saxophone"])
    assert child_tags(root.find("part/measure")) == ["attributes", "note"]
    assert root.find("part-list/score-part/score-instrument").get("id") == "P1-I1"
    assert root.findtext(".//midi-channel") == "1"


@pytest.mark.parametrize(
    ("xml", "kwargs", "error", "match"),
    [
        (omr_score(), {"part_id": "P9"}, MusicXMLError, r"part 'P9' not found; parts are \['P1'\]"),
        (
            score({"P1": [measure(1, Q)], "P2": [measure(1, Q)]}),
            {},
            MusicXMLError,
            r"score has 2 parts \['P1', 'P2'\]; pass the part id",
        ),
        (omr_score(), {"suspect_measures": ["7"]}, FixupError, r"no measure numbered '7'"),
        (
            score({"P1": [measure(1, "<attributes><staves>2</staves></attributes>" + Q)]}),
            {},
            FixupError,
            r"has 2 staves; only single-staff parts",
        ),
        (
            score({"P1": [measure(1, Q)]}, score_parts=""),
            {},
            FixupError,
            r"part 'P1' has no <score-part>",
        ),
    ],
)
def test_fixup_fails_clearly(xml, kwargs, error, match):
    with pytest.raises(error, match=match):
        fix_instrument(parse_musicxml(xml), INSTRUMENTS["flute"], **kwargs)


def test_fix_instrument_file_round_trip(tmp_path):
    src = tmp_path / "omr.musicxml"
    src.write_text(omr_score(), encoding="utf-8")
    dst = tmp_path / "fixed.musicxml"
    fix_instrument_file(src, dst, INSTRUMENTS["bb-trumpet"], part_id="P1", suspect_measures=["2"])
    assert dst.read_bytes().startswith(b"<?xml version='1.0' encoding='UTF-8'?>")
    root = read_musicxml(dst)
    assert root.findtext(".//transpose/chromatic") == "-2"
    assert root.findtext(".//rehearsal") == "?"
