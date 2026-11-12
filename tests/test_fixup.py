import hashlib
import json
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
from omr_ingestor.paths import INSTRUMENTS_PATH
from tests.musicxml_builders import (
    Q,
    measure,
    note,
    omr_score,
    score,
    start,
    stop,
    volta_score,
)

INSTRUMENTS = load_instruments()


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


@pytest.mark.parametrize(
    ("instrument_id", "marked_sha256", "unmarked_sha256"),
    [
        (
            "bb-trumpet",
            "43c12b66e3457ee1b7fd212d10dbfbfc0a206e52078661ce93d2d93dabaaec33",
            "c3a6322ba68287f89c267532e5fc1f013895d9ef394aa4dcdb30a29bfa913cbf",
        ),
        (
            "trombone",
            "dfb03e3881532bdd393107d15c02a588a3e97808db5d72b9096f9071c19b5633",
            "a39270e628a45ad41bce91b44444ac7383c8610aa6122c341fecfa639792a592",
        ),
        (
            "flute",
            "628dcfebc95e723124c61747475d1c949359713f41bb462fa3e8c6a0cefad2a4",
            "dea00593a8fd9f55372b285849860b57ad7be0380606311c71d0d93d3ec06bd5",
        ),
        (
            "alto-saxophone",
            "7d68715da37acb553e9c4173273350c13eee5af3a37212b6c36592a56cf92c8c",
            "9d5bf3ba6c28469be4e8f6fc86d5625571d2a7a3c4d3378a630df67800324ef1",
        ),
        (
            "tenor-saxophone",
            "0eeb9a6622517eeaffc9d968132c35958c1b4b735a0bde0dd599b024ef3628b1",
            "4795ab2718b2efca92af7b292ded135e56fdd44e3137815ecf710904ba7b7463",
        ),
        (
            "tuba",
            "2d1fa39d3fb5b6c5f1eaeeb04624104a0aca6ab73a50eb706a7cc01c9ee34be4",
            "88f75805ffa0140e1f8654502f28e6b66ec0f6fedb799c536a28b50ddce637c4",
        ),
    ],
)
@pytest.mark.parametrize(
    "suspect_measures",
    [pytest.param(["2"], id="marked"), pytest.param([], id="no-suspect-measures")],
)
def test_fixup_output_snapshot(
    tmp_path, instrument_id, marked_sha256, unmarked_sha256, suspect_measures
):
    # Byte snapshots: marked hashes captured at e830fb8 before moving SOUNDS into
    # instruments.json; unmarked hashes captured at d3c0a56 before ID validation.
    # Change these only after reviewing the serialized MusicXML output.
    expected_sha256 = marked_sha256 if suspect_measures else unmarked_sha256
    src = tmp_path / "omr.musicxml"
    src.write_text(omr_score(), encoding="utf-8")
    dst = tmp_path / "fixed.musicxml"
    fix_instrument_file(src, dst, INSTRUMENTS[instrument_id], suspect_measures=suspect_measures)
    assert hashlib.sha256(dst.read_bytes()).hexdigest() == expected_sha256


def test_fixup_reads_sound_from_custom_instrument_table(write_json):
    entry = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8"))[0]
    entry.update(id="custom-trumpet", instrument_sound="brass.trumpet.c", midi_program=60)
    instrument = load_instruments(write_json([entry]))["custom-trumpet"]
    root = parse_musicxml(omr_score())
    fix_instrument(root, instrument)
    sp = root.find("part-list/score-part")
    assert sp.findtext("score-instrument/instrument-sound") == "brass.trumpet.c"
    assert sp.findtext("midi-instrument/midi-program") == "60"


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
