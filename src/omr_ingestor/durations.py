"""Pipeline stage 3: duration per measure versus the time signature.

Durations are compared in quarter notes, as Fractions, so changes of <divisions>
(even in the middle of a measure) do not matter. The length of a measure is the
furthest point the cursor reaches, so chords, <backup> and <forward> (several
voices or staves) are handled the way a MusicXML reader places notes in time.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from omr_ingestor.musicxml import MusicXMLError, read_musicxml


@dataclass(frozen=True, slots=True)
class TimeSignature:
    text: str  # as written, e.g. "4/4" or "3+2/8"
    quarters: Fraction  # expected measure length in quarter notes


@dataclass(frozen=True, slots=True)
class MeasureDuration:
    """Duration check of one measure of one part. Lengths are in quarter notes."""

    part_id: str
    number: str  # the measure's number attribute, as written
    index: int  # 0-based position of the measure in its part
    actual: Fraction
    time: TimeSignature | None  # None under senza-misura
    partial: bool = False  # accepted as shorter: pickup, implicit, or closing the pickup
    problems: tuple[str, ...] = ()

    @property
    def expected(self) -> Fraction | None:
        return self.time.quarters if self.time else None

    @property
    def ok(self) -> bool:
        if self.problems:
            return False
        if self.time is None:
            return True
        if self.partial:
            return 0 < self.actual < self.time.quarters
        return self.actual == self.time.quarters

    def describe(self) -> str:
        where = f"part {self.part_id} measure {self.number}"
        if self.problems:
            return f"{where}: " + "; ".join(self.problems)
        return f"{where}: {self.actual} quarter(s), expected {self.expected} ({self.time.text})"


@dataclass(frozen=True, slots=True)
class DurationReport:
    measures: tuple[MeasureDuration, ...]

    @property
    def suspects(self) -> list[MeasureDuration]:
        return [m for m in self.measures if not m.ok]

    def suspect_numbers(self, part_id: str) -> list[str]:
        """Measure numbers of the suspect measures of one part, ready for fix_instrument."""
        return [m.number for m in self.suspects if m.part_id == part_id]


def _number(el: ET.Element, where: str) -> Fraction:
    text = (el.text or "").strip()
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise MusicXMLError(f"{where}: <{el.tag}> {text!r} is not a number") from None
    if value < 0:
        raise MusicXMLError(f"{where}: <{el.tag}> {text!r} is negative")
    return value


def _time_signature(time_el: ET.Element, where: str) -> TimeSignature | None:
    """Parse <time>. Composite signatures (3+2/8, or several beats/beat-type pairs) add up."""
    if time_el.find("senza-misura") is not None:
        return None
    beats = time_el.findall("beats")
    beat_types = time_el.findall("beat-type")
    if not beats or len(beats) != len(beat_types):
        raise MusicXMLError(f"{where}: <time> needs matching <beats> and <beat-type>")
    total = Fraction(0)
    parts = []
    for b, bt in zip(beats, beat_types, strict=True):
        b_text, bt_text = (b.text or "").strip(), (bt.text or "").strip()
        try:
            count = sum(int(x) for x in b_text.split("+"))
            beat_type = int(bt_text)
            total += Fraction(4 * count, beat_type)
        except (ValueError, ZeroDivisionError):
            raise MusicXMLError(f"{where}: invalid time signature {b_text}/{bt_text}") from None
        parts.append(f"{b_text}/{bt_text}")
    return TimeSignature("+".join(parts), total)


def _is_measure_rest(note: ET.Element) -> bool:
    rest = note.find("rest")
    return rest is not None and rest.get("measure") == "yes"


def check_part(part: ET.Element) -> list[MeasureDuration]:
    part_id = part.get("id", "")
    divisions: Fraction | None = None
    time: TimeSignature | None = None
    time_seen = False
    results: list[MeasureDuration] = []
    measures = part.findall("measure")

    for index, measure in enumerate(measures):
        number = measure.get("number", "")
        where = f"part {part_id} measure {number}"
        problems: list[str] = []
        pos = end = chord_start = Fraction(0)
        notes = 0
        measure_rests = 0

        for el in measure:
            if el.tag == "attributes":
                if (div := el.find("divisions")) is not None:
                    divisions = _number(div, where)
                    if divisions == 0:
                        raise MusicXMLError(f"{where}: <divisions> is zero")
                if (time_el := el.find("time")) is not None:
                    time, time_seen = _time_signature(time_el, where), True
                continue
            if el.tag not in ("note", "backup", "forward"):
                continue
            if el.tag == "note" and el.find("grace") is not None:
                continue
            dur_el = el.find("duration")
            if dur_el is None:
                problems.append(f"<{el.tag}> without <duration>")
                continue
            if divisions is None:
                raise MusicXMLError(f"{where}: <duration> before any <divisions>")
            length = _number(dur_el, where) / divisions

            if el.tag == "note":
                notes += 1
                measure_rests += _is_measure_rest(el)
                if el.find("chord") is not None:
                    end = max(end, chord_start + length)
                else:
                    chord_start = pos
                    pos += length
            elif el.tag == "forward":
                pos += length
            else:
                pos -= length
                if pos < 0:
                    problems.append("<backup> goes before the start of the measure")
                    pos = Fraction(0)
            end = max(end, pos)

        if not time_seen:
            problems.append("no time signature")
        # A whole-measure rest fills the measure whatever duration the writer gave it.
        if time is not None and notes and measure_rests == notes:
            end = time.quarters

        results.append(MeasureDuration(part_id, number, index, end, time, False, tuple(problems)))

    return [_mark_partial(results, measures, i) for i in range(len(results))]


def _is_short(m: MeasureDuration) -> bool:
    return m.time is not None and 0 < m.actual < m.time.quarters


def _mark_partial(
    results: list[MeasureDuration], measures: list[ET.Element], i: int
) -> MeasureDuration:
    """Accept a short measure that is a pickup, an implicit measure, or completes one."""
    m = results[i]
    if not _is_short(m):
        return m
    implicit = measures[i].get("implicit") == "yes"
    nxt = results[i + 1] if i + 1 < len(results) else None
    first = results[0]
    partial = (
        i == 0
        or implicit
        # First half of a bar split by an implicit measure (e.g. at a repeat sign).
        or (
            nxt is not None
            and measures[i + 1].get("implicit") == "yes"
            and m.actual + nxt.actual == m.time.quarters
        )
        # Last measure completing the pickup.
        or (
            nxt is None
            and i > 0
            and _is_short(first)
            and first.time == m.time
            and m.actual + first.actual == m.time.quarters
        )
    )
    return replace(m, partial=partial) if partial else m


def check_durations(root: ET.Element) -> DurationReport:
    measures: list[MeasureDuration] = []
    for part in root.findall("part"):
        measures.extend(check_part(part))
    return DurationReport(tuple(measures))


def measure_durations(path: Path) -> DurationReport:
    """Check every measure of every part of a MusicXML file against its time signature.

    A measure shorter than the time signature is accepted when it is the first measure
    (pickup), is marked implicit="yes", is the first half of a bar split by an implicit
    measure, or is the last measure and completes the pickup.
    """
    return check_durations(read_musicxml(path))
