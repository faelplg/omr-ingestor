from fractions import Fraction

import pytest

from omr_ingestor.durations import check_durations, measure_durations
from omr_ingestor.musicxml import MusicXMLError, parse_musicxml
from tests.musicxml_builders import DEFAULT_ATTRS, measure, note, rest, score

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
