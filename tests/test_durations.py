import xml.etree.ElementTree as ET
from fractions import Fraction

import pytest

from omr_ingestor.durations import check_durations, check_review_signals, measure_durations
from omr_ingestor.musicxml import MusicXMLError, parse_musicxml
from tests.musicxml_builders import DEFAULT_ATTRS, measure, multiple_rest, note, rest, score

Q = note(1)


def check(*measures: str):
    return check_durations(parse_musicxml(score({"P1": list(measures)})))


def check_measures(*measures: str):
    return check(*measures).measures


def suspects(*measures: str) -> list[str]:
    return [m.number for m in check(*measures).suspects]


def test_full_short_and_long_measures(tmp_path):
    path = tmp_path / "s.musicxml"
    path.write_text(
        score(
            {
                "P1": [
                    measure(1, DEFAULT_ATTRS + Q * 4),
                    measure(2, Q * 3),
                    measure(3, Q * 5),
                    measure(4, Q * 4),
                ]
            }
        ),
        encoding="utf-8",
    )
    report = measure_durations(path)
    assert [m.actual for m in report.measures] == [4, 3, 5, 4]
    assert [m.number for m in report.suspects] == ["2", "3"]
    assert report.suspects[0].describe() == "part P1 measure 2: 3 quarter(s), expected 4 (4/4)"


def test_divisions_change_between_and_inside_measures():
    assert not suspects(
        measure(1, DEFAULT_ATTRS + Q * 4),
        measure(2, "<attributes><divisions>2</divisions></attributes>" + note(2) * 2 + note(4)),
        measure(
            3,
            note(2) + "<attributes><divisions>8</divisions></attributes>" + note(8) + note(16),
        ),
    )


def test_chord_notes_do_not_advance():
    chord = note(2) + note(2, step="E", chord=True) + note(2, step="G", chord=True)
    assert not suspects(measure(1, DEFAULT_ATTRS + chord + note(2)))


def test_backup_and_forward_for_second_voice():
    voice2 = "<backup><duration>4</duration></backup><forward><duration>2</duration></forward>"
    assert not suspects(measure(1, DEFAULT_ATTRS + Q * 4 + voice2 + note(2)))
    # The second voice runs past the bar line.
    assert suspects(measure(1, DEFAULT_ATTRS + Q * 4 + voice2 + note(3))) == ["1"]


def test_backup_before_measure_start_is_a_problem():
    backup = "<backup><duration>2</duration></backup>"
    [m] = check_measures(measure(1, DEFAULT_ATTRS + Q + backup + Q))
    assert not m.ok
    assert m.problems == ("<backup> goes before the start of the measure",)


def test_pickup_and_closing_measure():
    report = check(
        measure(0, DEFAULT_ATTRS + Q, attrs=' implicit="yes"'),
        measure(1, Q * 4),
        measure(2, Q * 3),
    )
    assert [m.partial for m in report.measures] == [True, False, True]
    assert report.suspects == []


def test_first_measure_short_without_implicit_is_a_pickup():
    assert not suspects(measure(1, DEFAULT_ATTRS + Q), measure(2, Q * 4))


def test_closing_measure_must_complete_the_pickup():
    assert suspects(measure(0, DEFAULT_ATTRS + Q), measure(1, Q * 4), measure(2, Q * 2)) == ["2"]


def test_pickup_longer_than_the_bar_is_suspect():
    assert suspects(measure(0, DEFAULT_ATTRS + Q * 5, attrs=' implicit="yes"')) == ["0"]


def test_measure_split_by_an_implicit_measure():
    halves = [measure(2, Q * 2), measure("X1", Q * 2, attrs=' implicit="yes"')]
    assert not suspects(measure(1, DEFAULT_ATTRS + Q * 4), *halves, measure(3, Q * 4))
    # The first half is only accepted when both halves fill the bar.
    halves[0] = measure(2, Q)
    assert suspects(measure(1, DEFAULT_ATTRS + Q * 4), *halves, measure(3, Q * 4)) == ["2"]


def test_time_signature_changes_and_composite_meters():
    three_four = "<attributes><time><beats>3</beats><beat-type>4</beat-type></time></attributes>"
    composite = "<attributes><time><beats>3+2</beats><beat-type>8</beat-type></time></attributes>"
    pairs = (
        "<attributes><time><beats>2</beats><beat-type>4</beat-type>"
        "<beats>3</beats><beat-type>8</beat-type></time></attributes>"
    )
    eighth = "<attributes><divisions>2</divisions></attributes>" + note(1) * 5
    report = check(
        measure(1, DEFAULT_ATTRS + Q * 4),
        measure(2, three_four + Q * 3),
        measure(3, composite + eighth),
        measure(4, pairs + note(1) * 7),
    )
    assert [m.time.text for m in report.measures] == ["4/4", "3/4", "3+2/8", "2/4+3/8"]
    assert [m.expected for m in report.measures] == [4, 3, Fraction(5, 2), Fraction(7, 2)]
    assert report.suspects == []


def test_senza_misura_is_not_checked():
    attrs = "<attributes><divisions>1</divisions><time><senza-misura/></time></attributes>"
    [m] = check_measures(measure(1, attrs + Q * 7))
    assert m.ok and m.expected is None


def test_whole_measure_rest_fills_any_meter():
    three_four = "<attributes><time><beats>3</beats><beat-type>4</beat-type></time></attributes>"
    assert not suspects(
        measure(1, DEFAULT_ATTRS + Q * 4),
        measure(2, three_four + rest(4, measure=True)),
        measure(3, rest(3)),
    )


def test_grace_notes_take_no_time():
    grace = "<note><grace/><pitch><step>D</step><octave>4</octave></pitch><type>16th</type></note>"
    assert not suspects(measure(1, DEFAULT_ATTRS + grace + Q * 4))


def test_empty_measure_is_suspect():
    assert suspects(measure(1, DEFAULT_ATTRS + Q * 4), measure(2, ""), measure(3, Q * 4)) == ["2"]


def test_missing_time_signature_is_suspect():
    [m] = check_measures(measure(1, "<attributes><divisions>1</divisions></attributes>" + Q * 4))
    assert m.problems == ("no time signature",)
    assert m.describe() == "part P1 measure 1: no time signature"


def test_suspects_are_reported_per_part():
    xml = score(
        {
            "P1": [measure(1, DEFAULT_ATTRS + Q * 4), measure(2, Q * 3)],
            "P2": [measure(1, DEFAULT_ATTRS + Q * 4), measure(2, Q * 4)],
        }
    )
    report = check_durations(parse_musicxml(xml))
    assert report.suspect_numbers("P1") == ["2"]
    assert report.suspect_numbers("P2") == []


MULTI_REST_REASON = "multi-measure rest read as {} bar(s); check the real length in the PDF"


def test_multi_measure_rests_are_suspect_whatever_their_length():
    whole = rest(4, measure=True)
    report = check(
        measure(1, DEFAULT_ATTRS + Q * 4),
        measure(2, multiple_rest(1) + whole),
        measure(3, Q * 4),
        measure(4, multiple_rest(4) + whole),
        measure(5, Q * 4),
    )
    assert [m.number for m in report.suspects] == ["2", "4"]
    assert [m.multiple_rest for m in report.suspects] == [1, 4]
    assert report.suspects[0].ok
    assert report.suspects[0].describe() == f"part P1 measure 2: {MULTI_REST_REASON.format(1)}"
    assert report.suspect_numbers("P1") == ["2", "4"]


def test_multi_measure_rest_on_a_bad_measure_merges_the_reasons():
    report = check(measure(1, DEFAULT_ATTRS + Q * 4), measure(2, multiple_rest(1) + Q * 3))
    [m] = report.suspects
    assert m.describe() == (
        f"part P1 measure 2: 3 quarter(s), expected 4 (4/4); {MULTI_REST_REASON.format(1)}"
    )


def test_part_without_multi_measure_rests_gets_no_new_suspects():
    xml = score(
        {
            "P1": [measure(1, DEFAULT_ATTRS + Q * 4), measure(2, multiple_rest(2) + rest(4))],
            "P2": [measure(1, DEFAULT_ATTRS + Q * 4), measure(2, rest(4, measure=True))],
        }
    )
    report = check_durations(parse_musicxml(xml))
    assert report.suspect_numbers("P1") == ["2"]
    assert report.suspect_numbers("P2") == []
    assert all(m.multiple_rest is None for m in report.measures if m.part_id == "P2")


@pytest.mark.parametrize(
    ("xml", "match"),
    [
        (score({"P1": [measure(1, Q)]}), r"measure 1: <duration> before any <divisions>"),
        ('<score-timewise version="4.0"/>', r"score-timewise is not supported"),
        ("<score-partwise><part>", r"not well-formed XML at line 1"),
        (
            score({"P1": [measure(1, DEFAULT_ATTRS.replace("<beats>4", "<beats>x") + Q)]}),
            r"invalid time signature x/4",
        ),
    ],
)
def test_unusable_input_fails_clearly(xml, match):
    with pytest.raises(MusicXMLError, match=match):
        check_durations(parse_musicxml(xml))


@pytest.mark.parametrize("expected", [1, 3])
def test_count_review_is_independent_of_valid_durations(expected):
    root = ET.fromstring(score({"P1": [measure(1, DEFAULT_ATTRS + note(4)), measure(2, note(4))]}))
    assert not check_durations(root).suspects
    signals = check_review_signals(root, expected)
    assert [s.code for s in signals] == ["measure_count_mismatch"]
    assert signals[0].measure == ""
    assert "anywhere" in signals[0].detail
    assert not check_review_signals(root, 2)


def test_part_count_and_structural_identity_review():
    root = ET.fromstring(
        score({f"P{i}": [measure(1, DEFAULT_ATTRS + rest(4, measure=True))] for i in range(6)})
    )
    assert not check_durations(root).suspects
    assert [s.code for s in check_review_signals(root, 1, 2)] == ["part_count_mismatch"]
    assert not check_review_signals(root, 1, 6)
    root.findall("part")[1].set("id", "P0")
    assert "part_identity" in [s.code for s in check_review_signals(root, 1, 6)]


@pytest.mark.parametrize(
    "text", ["D.C.", "D.S.", "Fine", "Coda", "al Fine", "D. C. al Fine", "Fim"]
)
@pytest.mark.parametrize("direction", [False, True])
def test_navigation_lyrics_require_review_even_with_directions(text, direction):
    nav = '<direction><sound dacapo="yes"/></direction>' if direction else ""
    lyric = f"<lyric><text>{text}</text></lyric>"
    root = ET.fromstring(score({"P1": [measure(1, DEFAULT_ATTRS + nav + note(4, extra=lyric))]}))
    before = ET.tostring(root)
    signals = check_review_signals(root, 1)
    assert len(signals) == 1
    assert signals[0].code == "navigation_lyric"
    assert signals[0].part_id == "P1" and signals[0].measure == "1"
    assert ("no navigation direction" in signals[0].detail) is not direction
    assert ET.tostring(root) == before


def test_navigation_matching_avoids_word_fragments_and_joins_lyric_text():
    root = ET.fromstring(
        score(
            {
                "P1": [
                    measure(
                        1,
                        DEFAULT_ATTRS
                        + note(4, extra="<lyric><text>finally codas decode</text></lyric>"),
                    )
                ]
            }
        )
    )
    assert not check_review_signals(root, 1)
    lyric = root.find("part/measure/note/lyric")
    lyric.clear()
    ET.SubElement(lyric, "text").text = "D."
    ET.SubElement(lyric, "text").text = "C."
    assert check_review_signals(root, 1)[0].code == "navigation_lyric"


@pytest.mark.parametrize("attribute", ["fine", "dacapo", "dalsegno", "coda", "tocoda", "segno"])
def test_measure_sound_counts_as_navigation(attribute):
    root = ET.fromstring(
        score(
            {
                "P1": [
                    measure(
                        1,
                        DEFAULT_ATTRS
                        + f'<sound {attribute}="yes"/>'
                        + note(4, extra="<lyric><text>Fine</text></lyric>"),
                    )
                ]
            }
        )
    )
    signals = check_review_signals(root, 1)
    assert len(signals) == 1
    assert "no navigation direction" not in signals[0].detail


@pytest.mark.parametrize("symbol", ["segno", "coda"])
def test_barline_symbol_counts_as_navigation(symbol):
    root = ET.fromstring(
        score(
            {
                "P1": [
                    measure(
                        1,
                        DEFAULT_ATTRS
                        + note(4, extra="<lyric><text>Fine</text></lyric>")
                        + f"<barline><{symbol}/></barline>",
                    )
                ]
            }
        )
    )
    signals = check_review_signals(root, 1)
    assert len(signals) == 1
    assert "no navigation direction" not in signals[0].detail
