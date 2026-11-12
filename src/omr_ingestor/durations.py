"""Pipeline stage 3: duration per measure versus the time signature.

Durations are compared in quarter notes, as Fractions, so changes of <divisions>
(even in the middle of a measure) do not matter. The length of a measure is the
furthest point the cursor reaches, so chords, <backup> and <forward> (several
voices or staves) are handled the way a MusicXML reader places notes in time.

A measure that starts a multi-measure rest (<multiple-rest>) is suspect too, whatever
its duration: Audiveris reads every multi-measure rest as one bar, so the part loses
measures there. The length is reported as read, never guessed.
"""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from omr_ingestor.musicxml import MusicXMLError, has_safe_part_ids, read_musicxml
from omr_ingestor.navigation import NAVIGATION_PATTERN, NAVIGATION_SOUND_ATTRIBUTES


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
    multiple_rest: int | None = None  # bars of the <multiple-rest> that starts here, as read

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

    @property
    def suspect(self) -> bool:
        """The duration does not add up, or a multi-measure rest starts here."""
        return not self.ok or self.multiple_rest is not None

    def describe(self) -> str:
        """Every reason this measure is suspect, joined; the duration alone when it is not."""
        reasons = list(self.problems)
        if not reasons and (not self.ok or self.multiple_rest is None):
            time = self.time.text if self.time else "senza misura"
            reasons.append(f"{self.actual} quarter(s), expected {self.expected} ({time})")
        if self.multiple_rest is not None:
            reasons.append(
                f"multi-measure rest read as {self.multiple_rest} bar(s); "
                "check the real length in the PDF"
            )
        return f"part {self.part_id} measure {self.number}: " + "; ".join(reasons)


@dataclass(frozen=True, slots=True)
class DurationReport:
    measures: tuple[MeasureDuration, ...]

    @property
    def suspects(self) -> list[MeasureDuration]:
        return [m for m in self.measures if m.suspect]

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


def _multiple_rest(el: ET.Element, where: str) -> int:
    text = (el.text or "").strip()
    try:
        return int(text)
    except ValueError:
        raise MusicXMLError(f"{where}: <multiple-rest> {text!r} is not a whole number") from None


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
        multiple_rest: int | None = None

        for el in measure:
            if el.tag == "attributes":
                if (div := el.find("divisions")) is not None:
                    divisions = _number(div, where)
                    if divisions == 0:
                        raise MusicXMLError(f"{where}: <divisions> is zero")
                if (time_el := el.find("time")) is not None:
                    time, time_seen = _time_signature(time_el, where), True
                if (multi := el.find("measure-style/multiple-rest")) is not None:
                    multiple_rest = _multiple_rest(multi, where)
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

        results.append(
            MeasureDuration(
                part_id, number, index, end, time, False, tuple(problems), multiple_rest
            )
        )

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
    last = results[-1]
    completing_pickup = (
        len(results) > 1
        and _is_short(first)
        and _is_short(last)
        and first.time == last.time
        and first.actual + last.actual == first.time.quarters
    )
    partial = (
        implicit
        # A completing last measure is evidence for an unmarked pickup too.
        or (i in (0, len(results) - 1) and completing_pickup)
        # First half of a bar split by an implicit measure (e.g. at a repeat sign).
        or (
            nxt is not None
            and measures[i + 1].get("implicit") == "yes"
            and m.actual + nxt.actual == m.time.quarters
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

    A short first measure is accepted as a pickup only when marked implicit="yes"
    or paired with a completing last measure under the same time signature. Other
    short measures are accepted when marked implicit="yes", when they are the first
    half of a bar split by an implicit measure, or when they complete the pickup.
    """
    return check_durations(read_musicxml(path))


@dataclass(frozen=True, slots=True)
class ReviewSignal:
    """A source-review warning, independent of rhythmic validity."""

    code: str
    detail: str
    part_id: str = ""
    measure: str = ""


REVIEW_SIGNAL_CODES = frozenset(
    {
        "measure_count_mismatch",
        "part_count_mismatch",
        "part_identity",
        "part_identity_changed",
        "navigation_lyric",
    }
)


def check_review_signals(
    root: ET.Element, expected_measures: int, expected_parts: int = 1
) -> tuple[ReviewSignal, ...]:
    """Check completeness, structural identity and navigation without changing notes.

    Manifest part names are output labels, not evidence of recognized musical identity.
    Only structural identity can be checked without a source-backed comparison.
    """
    signals = []
    parts = root.findall("part")
    if len(parts) != expected_parts:
        signals.append(
            ReviewSignal(
                "part_count_mismatch", f"{len(parts)} part(s) read, {expected_parts} expected"
            )
        )
    if not has_safe_part_ids(root):
        signals.append(
            ReviewSignal(
                "part_identity", "part IDs must be unique and match the score-part declarations"
            )
        )
    for part in parts:
        pid = part.get("id", "")
        measures = part.findall("measure")
        if len(measures) != expected_measures:
            signals.append(
                ReviewSignal(
                    "measure_count_mismatch",
                    f"part {pid}: {len(measures)} measure(s) read, {expected_measures} expected; "
                    "compare all systems with the source; missing or extra bars may occur anywhere",
                    pid,
                )
            )
        has_navigation = (
            any(
                direction.find(".//coda") is not None
                or direction.find(".//segno") is not None
                or any(
                    NAVIGATION_PATTERN.search(words.text or "")
                    for words in direction.findall(".//words")
                )
                for direction in part.findall("measure/direction")
            )
            or any(
                NAVIGATION_SOUND_ATTRIBUTES.intersection(sound.attrib)
                for sound in part.findall("measure/sound") + part.findall("measure/direction/sound")
            )
            or any(
                barline.find(symbol) is not None
                for barline in part.findall("measure/barline")
                for symbol in ("segno", "coda")
            )
        )
        for measure in measures:
            for lyric in measure.findall("note/lyric"):
                text = " ".join(el.text or "" for el in lyric if el.tag in ("text", "elision"))
                if NAVIGATION_PATTERN.search(text):
                    detail = f"navigation-like lyric {text!r}; verify against the source"
                    if not has_navigation:
                        detail += "; no navigation direction found in this part"
                    signals.append(
                        ReviewSignal("navigation_lyric", detail, pid, measure.get("number", ""))
                    )
    return tuple(signals)
