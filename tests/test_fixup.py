import xml.etree.ElementTree as ET

import pytest

from omr_ingestor.durations import check_durations
from omr_ingestor.fixup import (
    FixupError,
    VoltaRewrite,
    fix_instrument,
    fix_instrument_file,
    fix_volta_endings,
)
from omr_ingestor.instruments import load_instruments
from omr_ingestor.musicxml import MusicXMLError, parse_musicxml, read_musicxml
from tests.musicxml_builders import barline, measure, note, score

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
    "<part-abbreviation>Pno.</part-abbreviation>"
    "<part-abbreviation-display><display-text>Pno.</display-text></part-abbreviation-display>"
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
# Mid-part switches to P1-I2: a standalone <sound> and one inside a <direction>.
SWITCH_TO_I2 = (
    '<sound><midi-instrument id="P1-I2"><midi-program>20</midi-program></midi-instrument></sound>'
    '<direction><direction-type><words>Organ</words></direction-type><sound tempo="90">'
    '<instrument-change id="P1-I2"><instrument-sound>keyboard.organ</instrument-sound>'
    '</instrument-change><midi-instrument id="P1-I2"><midi-program>20</midi-program>'
    "</midi-instrument></sound></direction>"
)


def omr_score() -> str:
    return score(
        {
            "P1": [
                measure(1, PRINT + FIRST_ATTRS + Q * 4, attrs=' width="300"'),
                measure(2, Q * 3, attrs=' width="250"'),
                measure(3, PRINT + C_CLEF + SWITCH_TO_I2 + I2_NOTE * 4),
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


def test_no_reference_to_a_removed_instrument_remains():
    root = fixed("flute")
    kept = {i.get("id") for i in root.iterfind("part-list/score-part/score-instrument")}
    assert kept == {"P1-I1"}
    refs = {el.get("id") for el in root.find("part").iterfind("measure//*")} - {None}
    assert refs <= kept
    m3 = root.find("part/measure[@number='3']")
    # The empty standalone <sound> goes; the one with a tempo stays, without the switch.
    assert m3.find("sound") is None
    assert ET.tostring(m3.find("direction/sound"), encoding="unicode") == '<sound tempo="90" />'


def test_old_abbreviations_and_display_names_are_removed():
    sp = fixed("flute").find("part-list/score-part")
    for tag in ("part-name-display", "part-abbreviation", "part-abbreviation-display"):
        assert sp.find(tag) is None
    assert "Pno." not in ET.tostring(sp, encoding="unicode")


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


def start(number: str, **kwargs) -> str:
    return barline("left", ending=(number, "start"), **kwargs)


def stop(number: str, kind: str = "stop", *, repeat: bool = False, **kwargs) -> str:
    return barline("right", ending=(number, kind), repeat="backward" if repeat else None, **kwargs)


def volta_score(*bodies: str) -> str:
    """Measure 1, then measure i + 2 with four quarters and the barlines in bodies[i]."""
    ms = [measure(1, FIRST_ATTRS + Q * 4)]
    ms += [measure(i, Q * 4 + body) for i, body in enumerate(bodies, 2)]
    return score({"P1": ms}, score_parts=OMR_SCORE_PART)


def volta_part(*bodies: str) -> ET.Element:
    return parse_musicxml(volta_score(*bodies))


def endings(root: ET.Element) -> list[tuple[str, str, str, str | None]]:
    """(measure, number, type, end-length) of every ending, in document order."""
    return [
        (m.get("number"), e.get("number"), e.get("type"), e.get("end-length"))
        for m in root.iterfind("part/measure")
        for e in m.iterfind("barline/ending")
    ]


@pytest.mark.parametrize("second", ["2", "1"])
def test_ending_split_by_audiveris_is_rejoined_up_to_the_repeat(second):
    # Audiveris 5.11: ending 1 on one measure, then ending 2 (or 1 again) up to the
    # repeat, then the real ending 2.
    root = volta_part(
        start("1", end_length="34") + stop("1", "discontinue"),
        start(second),
        "",
        stop(second, repeat=True),
        start("2") + stop("2", "discontinue"),
    )
    result = fix_instrument(root, INSTRUMENTS["bb-trumpet"])

    assert endings(root) == [
        ("2", "1", "start", "34"),
        ("5", "1", "stop", "34"),
        ("6", "2", "start", None),
        ("6", "2", "discontinue", None),
    ]
    # The barlines that only held the removed endings are gone.
    assert root.find("part/measure[@number='3']/barline") is None
    assert [b.get("location") for b in root.iterfind("part/measure[@number='2']/barline")] == [
        "left"
    ]
    assert result.rewrites == (VoltaRewrite("1", "2", "5"),)
    assert result.suspects == ()
    assert result.marked == ("2",)
    assert root.findtext("part/measure[@number='2']/direction/direction-type/rehearsal") == "?"
    # A second run finds nothing left to fix.
    assert fix_volta_endings(root.find("part")) == ([], [])


def test_rejoined_ending_keeps_its_own_end_length():
    root = volta_part(
        start("1") + stop("1", "discontinue"),
        start("2"),
        stop("2", repeat=True, end_length="37"),
        start("2") + stop("2"),
    )
    fix_volta_endings(root.find("part"))
    assert endings(root)[:2] == [("2", "1", "start", None), ("4", "1", "stop", "37")]


@pytest.mark.parametrize(
    "bodies",
    [
        # A plain first and second ending.
        (start("1"), stop("1", repeat=True), start("2") + stop("2", "discontinue")),
        # Three endings, each but the last closed by a repeat.
        (
            start("1") + stop("1", repeat=True),
            start("2") + stop("2", repeat=True),
            start("3") + stop("3", "discontinue"),
        ),
        # The same numbers in two separate volta sections.
        (
            start("1") + stop("1", repeat=True),
            start("2") + stop("2"),
            "",
            start("1") + stop("1", repeat=True),
            start("2") + stop("2"),
        ),
    ],
)
def test_correct_endings_are_left_alone(bodies):
    root = volta_part(*bodies)
    before = ET.tostring(root.find("part"))
    assert fix_volta_endings(root.find("part")) == ([], [])
    assert ET.tostring(root.find("part")) == before


@pytest.mark.parametrize(
    ("bodies", "measures", "reason"),
    [
        (
            # Same number twice, but no repeat to say where ending 1 stops.
            (
                start("1") + stop("1", "discontinue"),
                start("1") + stop("1"),
                start("2") + stop("2"),
            ),
            ["2", "3"],
            "ending 1 (measure 3) has the same number as the ending right before it",
        ),
        (
            # The span after the repeat is numbered 3: ending 2 or a third ending?
            (
                start("1") + stop("1", "discontinue"),
                start("2") + stop("2", repeat=True),
                start("3") + stop("3"),
            ),
            ["2", "3"],
            "ending 2 (measure 3) ends with the repeat although the ending before it has none",
        ),
        (
            (start("1"), start("2") + stop("2")),
            ["3"],
            "ending 2 starts before ending 1 ends",
        ),
        ((start("1"),), ["2"], "ending 1 never ends"),
    ],
)
def test_doubtful_endings_become_suspects(bodies, measures, reason):
    root = volta_part(*bodies)
    before = endings(root)
    result = fix_instrument(root, INSTRUMENTS["bb-trumpet"])

    assert endings(root) == before
    assert result.rewrites == ()
    assert [s.measure for s in result.suspects] == measures
    assert all(s.reason == f"{reason}; left as is" for s in result.suspects)
    assert result.marked == tuple(measures)
    for number in measures:
        m = root.find(f"part/measure[@number='{number}']")
        assert m.findtext("direction/direction-type/rehearsal") == "?"
