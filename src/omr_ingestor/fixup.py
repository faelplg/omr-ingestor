"""Pipeline stage 4: apply an instruments.json entry to a MusicXML part.

The OMR result keeps its pitches; this stage only sets what the instrument decides
(transposition, clef, name, sound), drops the OMR page layout so the MuseScore style
can lay the score out, and marks suspect measures with a rehearsal mark.

It also repairs one known Audiveris mistake with volta endings (see fix_volta_endings);
each repair gets a rehearsal mark too, so the human review looks at it.
"""

import itertools
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from omr_ingestor.instruments import Instrument
from omr_ingestor.musicxml import (
    MusicXMLError,
    find_part,
    read_musicxml,
    write_musicxml,
)
from omr_ingestor.navigation import NAVIGATION_PATTERN, NAVIGATION_SOUND_ATTRIBUTES


class FixupError(MusicXMLError):
    """Raised when the instrument cannot be applied to the part."""


@dataclass(frozen=True, slots=True)
class Sound:
    instrument_sound: str  # MusicXML standard sound id (sounds.xml)
    midi_program: int  # General MIDI program, 1-based as in MusicXML


# instruments.json has no sound field yet, so the sound of each instrument lives here.
SOUNDS: dict[str, Sound] = {
    "bb-trumpet": Sound("brass.trumpet.bflat", 57),
    "trombone": Sound("brass.trombone", 58),
    "flute": Sound("wind.flutes.flute", 74),
    "alto-saxophone": Sound("wind.reed.saxophone.alto", 66),
    "tenor-saxophone": Sound("wind.reed.saxophone.tenor", 67),
    "tuba": Sound("brass.tuba", 59),
}

CLEFS: dict[str, tuple[str, str]] = {"G2": ("G", "2"), "F4": ("F", "4")}

DEFAULT_MARK = "?"

# Child order required by the MusicXML 4.0 XSD.
_SCORE_PART_ORDER = (
    "identification",
    "part-link",
    "part-name",
    "part-name-display",
    "part-abbreviation",
    "part-abbreviation-display",
    "group",
    "score-instrument",
    "player",
    "midi-device",
    "midi-instrument",
)
_ATTRIBUTES_ORDER = (
    "footnote",
    "level",
    "divisions",
    "key",
    "time",
    "staves",
    "part-symbol",
    "instruments",
    "clef",
    "staff-details",
    "transpose",
    "for-part",
    "directive",
    "measure-style",
)
_LAYOUT_IN_DEFAULTS = ("page-layout", "system-layout", "staff-layout")
_TIMED = ("note", "backup", "forward")
# <sound> children that refer to a score-instrument id.
_SOUND_INSTRUMENT_CHILDREN = ("instrument-change", "midi-device", "midi-instrument", "play")


def _insert_ordered(parent: ET.Element, child: ET.Element, order: tuple[str, ...]) -> None:
    rank = order.index(child.tag)
    for i, existing in enumerate(parent):
        if existing.tag in order and order.index(existing.tag) > rank:
            parent.insert(i, child)
            return
    parent.append(child)


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrib: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrib)
    el.text = text
    return el


def _remove_all(parent: ET.Element, tag: str) -> None:
    for el in parent.findall(tag):
        parent.remove(el)


def _score_part(root: ET.Element, part_id: str) -> ET.Element:
    for sp in root.iterfind("part-list/score-part"):
        if sp.get("id") == part_id:
            return sp
    raise FixupError(f"part {part_id!r} has no <score-part> in <part-list>")


def _set_name_and_sound(
    score_part: ET.Element,
    part: ET.Element,
    instrument: Instrument,
    sound: Sound,
    part_name: str | None = None,
) -> None:
    name = score_part.find("part-name")
    if name is None:
        name = ET.Element("part-name")
        _insert_ordered(score_part, name, _SCORE_PART_ORDER)
    name.text = part_name if part_name is not None else instrument.name_pt
    # OMR display names and abbreviations would show the old name; the table has no
    # abbreviation to put in their place.
    for tag in ("part-name-display", "part-abbreviation", "part-abbreviation-display"):
        _remove_all(score_part, tag)

    first = score_part.find("score-instrument")
    inst_id = first.get("id") if first is not None else f"{score_part.get('id')}-I1"
    old_midi = {m.get("id"): m for m in score_part.findall("midi-instrument")}
    old_channel = old_midi.get(inst_id)
    channel = old_channel.findtext("midi-channel") if old_channel is not None else None
    for tag in ("score-instrument", "midi-device", "midi-instrument"):
        _remove_all(score_part, tag)

    score_inst = ET.Element("score-instrument", id=inst_id)
    _sub(score_inst, "instrument-name", instrument.name_pt)
    _sub(score_inst, "instrument-sound", sound.instrument_sound)
    _insert_ordered(score_part, score_inst, _SCORE_PART_ORDER)
    midi = ET.Element("midi-instrument", id=inst_id)
    _sub(midi, "midi-channel", channel or "1")
    _sub(midi, "midi-program", str(sound.midi_program))
    _insert_ordered(score_part, midi, _SCORE_PART_ORDER)

    # Notes may point at score-instruments that were just removed.
    for ref in part.iterfind("measure/note/instrument"):
        ref.set("id", inst_id)
    _remove_instrument_changes(part)


def _remove_instrument_changes(part: ET.Element) -> None:
    """Drop mid-part instrument switches in <sound>; the part now has one instrument."""
    for measure in part.findall("measure"):
        for parent in (measure, *measure.findall("direction")):
            for snd in parent.findall("sound"):
                for tag in _SOUND_INSTRUMENT_CHILDREN:
                    _remove_all(snd, tag)
                if len(snd) == 0 and not snd.attrib:
                    parent.remove(snd)


def _set_clef_and_transpose(part: ET.Element, instrument: Instrument) -> None:
    for staves in part.iterfind("measure/attributes/staves"):
        if (staves.text or "").strip() not in ("", "1"):
            raise FixupError(
                f"part {part.get('id')!r} has {staves.text.strip()} staves; "
                "only single-staff parts are supported"
            )
    first_measure = part.find("measure")
    if first_measure is None:
        raise FixupError(f"part {part.get('id')!r} has no measures")
    first_attrs = first_measure.find("attributes")
    if first_attrs is None:
        first_attrs = ET.Element("attributes")
        first_measure.insert(0, first_attrs)

    # The instrument decides the clef for the whole part: later clef changes go too.
    for measure in part.findall("measure"):
        for attrs in measure.findall("attributes"):
            _remove_all(attrs, "clef")
            _remove_all(attrs, "transpose")
            if attrs is not first_attrs and len(attrs) == 0:
                measure.remove(attrs)

    sign, line = CLEFS[instrument.clef]
    clef = ET.Element("clef")
    _sub(clef, "sign", sign)
    _sub(clef, "line", line)
    _insert_ordered(first_attrs, clef, _ATTRIBUTES_ORDER)

    t = instrument.transpose
    if (t.diatonic, t.chromatic, t.octave_change) != (0, 0, 0):
        transpose = ET.Element("transpose")
        _sub(transpose, "diatonic", str(t.diatonic))
        _sub(transpose, "chromatic", str(t.chromatic))
        if t.octave_change:
            _sub(transpose, "octave-change", str(t.octave_change))
        _insert_ordered(first_attrs, transpose, _ATTRIBUTES_ORDER)


def remove_page_layout(root: ET.Element) -> None:
    """Drop the OMR layout of the whole score: <print>, layout defaults, measure widths."""
    defaults = root.find("defaults")
    if defaults is not None:
        for tag in _LAYOUT_IN_DEFAULTS:
            _remove_all(defaults, tag)
    for measure in root.iterfind("part/measure"):
        _remove_all(measure, "print")
        measure.attrib.pop("width", None)


# Counts refer to removed elements, not their descendant nodes or empty wrappers.
TEXT_REMOVAL_CATEGORIES = ("credits", "headers", "lyrics", "words", "rehearsals", "technical")
_TEXT_TECHNICAL = ("fingering", "pluck", "other-technical")


def strip_text(root: ET.Element, *, keep_text: bool = False) -> dict[str, int]:
    """Clean a raw export before adding our review annotations or applying instruments.

    Apply once to the whole score, including every part. Directions tied to a
    navigation sound or navigation-like words remain intact, including positioning.
    """
    counts = dict.fromkeys(TEXT_REMOVAL_CATEGORIES, 0)
    if keep_text:
        return counts

    def remove(parent: ET.Element, tag: str, category: str) -> None:
        for child in parent.findall(tag):
            parent.remove(child)
            counts[category] += 1

    remove(root, "credit", "credits")
    for tag in ("work", "movement-number", "movement-title"):
        remove(root, tag, "headers")
    for identification in root.findall("identification"):
        for tag in ("creator", "rights", "source", "relation", "miscellaneous"):
            remove(identification, tag, "headers")
        if not len(identification):
            root.remove(identification)
    for note in root.iter("note"):
        remove(note, "lyric", "lyrics")
        for notations in note.findall("notations"):
            removed_before = counts["technical"]
            for technical in notations.findall("technical"):
                had_children = bool(len(technical))
                for tag in _TEXT_TECHNICAL:
                    remove(technical, tag, "technical")
                if had_children and not len(technical):
                    notations.remove(technical)
            if counts["technical"] > removed_before and not len(notations):
                note.remove(notations)
    for measure in root.iterfind("part/measure"):
        for direction in measure.findall("direction"):
            if any(
                NAVIGATION_SOUND_ATTRIBUTES.intersection(sound.attrib)
                for sound in direction.findall("sound")
            ) or any(
                NAVIGATION_PATTERN.search(words.text or "")
                for words in direction.findall(".//words")
            ):
                continue
            for kind in direction.findall("direction-type"):
                remove(kind, "words", "words")
                remove(kind, "rehearsal", "rehearsals")
                if not len(kind):
                    direction.remove(kind)
            if direction.find("direction-type") is None:
                measure.remove(direction)
    return counts


def _has_mark(measure: ET.Element, text: str) -> bool:
    return any(r.text == text for r in measure.iterfind("direction/direction-type/rehearsal"))


def _add_marks(part: ET.Element, numbers: Iterable[str], text: str) -> None:
    by_number: dict[str, list[ET.Element]] = {}
    for measure in part.findall("measure"):
        by_number.setdefault(measure.get("number", ""), []).append(measure)
    for number in dict.fromkeys(numbers):
        if number not in by_number:
            raise FixupError(f"part {part.get('id')!r} has no measure numbered {number!r}")
        for measure in by_number[number]:
            if _has_mark(measure, text):
                continue
            direction = ET.Element("direction", placement="above")
            _sub(_sub(direction, "direction-type"), "rehearsal", text)
            at = next((i for i, el in enumerate(measure) if el.tag in _TIMED), len(measure))
            measure.insert(at, direction)


@dataclass(frozen=True, slots=True)
class VoltaRewrite:
    """One ending rebuilt by fix_volta_endings; first and last are measure numbers."""

    number: str
    first: str
    last: str

    def describe(self) -> str:
        return f"ending {self.number} rejoined over measures {self.first}-{self.last}"


@dataclass(frozen=True, slots=True)
class VoltaSuspect:
    measure: str
    reason: str


@dataclass(frozen=True, slots=True)
class FixupResult:
    rewrites: tuple[VoltaRewrite, ...] = ()
    suspects: tuple[VoltaSuspect, ...] = ()
    # Measure numbers that carry a rehearsal mark, in the order they were given.
    marked: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Span:
    """An ending bracket: measures[first..last], opened by `start`, closed by `end`."""

    number: str
    first: int
    last: int
    start: ET.Element
    start_barline: ET.Element
    end: ET.Element
    end_barline: ET.Element


_BARLINE_ORDER = {"left": 0, "middle": 1, "right": 2}
_ENDING_STOPS = ("stop", "discontinue")
DEFAULT_END_LENGTH = "30"


def _ending_events(measure: ET.Element) -> list[tuple[ET.Element, ET.Element]]:
    """(barline, ending) pairs of a measure, left barline first."""
    barlines = sorted(
        measure.findall("barline"),
        key=lambda b: _BARLINE_ORDER.get(b.get("location", "right"), 2),
    )
    return [(b, e) for b in barlines for e in b.findall("ending")]


def _has_backward_repeat(measure: ET.Element) -> bool:
    return any(r.get("direction") == "backward" for r in measure.iterfind("barline/repeat"))


def _ending_spans(measures: list[ET.Element]) -> tuple[list[_Span], VoltaSuspect | None]:
    """Pair every ending start with its stop; a VoltaSuspect if they do not pair up."""
    spans: list[_Span] = []
    opened: tuple[str, int, ET.Element, ET.Element] | None = None
    for i, measure in enumerate(measures):
        for barline, ending in _ending_events(measure):
            number, kind = ending.get("number", ""), ending.get("type")
            problem = None
            if kind == "start":
                if opened is None:
                    opened = (number, i, ending, barline)
                else:
                    problem = f"ending {number} starts before ending {opened[0]} ends"
            elif kind in _ENDING_STOPS and opened is not None and opened[0] == number:
                spans.append(_Span(number, opened[1], i, opened[2], opened[3], ending, barline))
                opened = None
            else:
                problem = f"ending {number} {kind} does not close an open ending"
            if problem:
                return [], VoltaSuspect(measure.get("number", ""), f"{problem}; left as is")
    if opened is not None:
        measure = measures[opened[1]]
        return [], VoltaSuspect(
            measure.get("number", ""), f"ending {opened[0]} never ends; left as is"
        )
    return spans, None


def _int_number(span: _Span) -> int | None:
    return int(span.number) if span.number.isdigit() else None


def _is_split_ending(p: _Span, r: _Span, n: _Span, measures: list[ET.Element]) -> bool:
    """Audiveris cut ending p short and gave its rest (r, up to the repeat) a span of its own.

    Unambiguous only when p, r and n touch, the only backward repeat from p to r closes
    r, and the numbers read p=k, r=k or k+1, n=k+1.
    """
    if not (p.last + 1 == r.first and r.last + 1 == n.first):
        return False
    if not _has_backward_repeat(measures[r.last]):
        return False
    if any(_has_backward_repeat(measures[i]) for i in range(p.first, r.last)):
        return False
    k, rk, nk = _int_number(p), _int_number(r), _int_number(n)
    return k is not None and rk in (k, k + 1) and nk == k + 1


def _remove_ending(barline: ET.Element, ending: ET.Element, measure: ET.Element) -> None:
    barline.remove(ending)
    if len(barline) == 0:
        measure.remove(barline)


def _merge(p: _Span, r: _Span, measures: list[ET.Element]) -> VoltaRewrite:
    """Make p run from its start to the end of r, closed with a stop on the repeat."""
    _remove_ending(p.end_barline, p.end, measures[p.last])
    _remove_ending(r.start_barline, r.start, measures[r.first])
    r.end.set("number", p.number)
    r.end.set("type", "stop")
    if r.end.get("end-length") is None:
        r.end.set("end-length", p.start.get("end-length") or DEFAULT_END_LENGTH)
    return VoltaRewrite(
        p.number, measures[p.first].get("number", ""), measures[r.last].get("number", "")
    )


def _span_label(span: _Span, measures: list[ET.Element]) -> str:
    first, last = measures[span.first].get("number"), measures[span.last].get("number")
    where = f"measure {first}" if span.first == span.last else f"measures {first}-{last}"
    return f"ending {span.number} ({where})"


def _doubtful_pairs(spans: list[_Span], measures: list[ET.Element]) -> list[VoltaSuspect]:
    """Touching endings that look like the split-ending mistake but could not be fixed."""
    suspects = []
    for i, (a, b) in enumerate(itertools.pairwise(spans)):
        if a.last + 1 != b.first:
            continue
        followed = i + 2 < len(spans) and spans[i + 2].first == b.last + 1
        if a.number == b.number:
            why = "has the same number as the ending right before it"
        elif (
            followed
            and _has_backward_repeat(measures[b.last])
            and not _has_backward_repeat(measures[a.last])
        ):
            why = "ends with the repeat although the ending before it has none"
        else:
            continue
        reason = f"{_span_label(b, measures)} {why}; left as is"
        suspects.extend(VoltaSuspect(measures[s.first].get("number", ""), reason) for s in (a, b))
    return suspects


def fix_volta_endings(part: ET.Element) -> tuple[list[VoltaRewrite], list[VoltaSuspect]]:
    """Rejoin an ending that Audiveris split in two, in place. Safe to run twice.

    Audiveris 5.11 can read a first ending that runs up to a backward repeat as a short
    ending 1 (start, then discontinue) plus a second span numbered 1 or 2 that ends at
    the repeat, followed by the real next ending. When that reading is unambiguous, the
    two spans become one ending 1 that stops (with end-length) on the repeat measure.
    Anything that looks like the mistake but does not fit exactly is left alone and
    returned as suspect, as are endings whose starts and stops do not pair up.
    """
    measures = part.findall("measure")
    rewrites: list[VoltaRewrite] = []
    i = 0
    while True:
        spans, broken = _ending_spans(measures)
        if broken is not None:
            return rewrites, [broken]
        hit = next(
            (
                j
                for j in range(i, len(spans) - 2)
                if _is_split_ending(spans[j], spans[j + 1], spans[j + 2], measures)
            ),
            None,
        )
        if hit is None:
            return rewrites, _doubtful_pairs(spans, measures)
        rewrites.append(_merge(spans[hit], spans[hit + 1], measures))
        # After the merge the next ending sits at hit + 1.
        i = hit + 1


def fix_instrument(
    root: ET.Element,
    instrument: Instrument,
    *,
    part_id: str | None = None,
    part_name: str | None = None,
    suspect_measures: Iterable[str] = (),
    mark_text: str = DEFAULT_MARK,
) -> FixupResult:
    """Apply an instrument to one part of the score, in place. Safe to run twice.

    part_id=None works on single-part scores. suspect_measures are measure numbers
    (as in DurationReport.suspect_numbers); each gets a rehearsal mark with mark_text,
    as do the volta endings fix_volta_endings rewrote or found suspect.
    part_name overrides the displayed part name, keeping the instrument sound/name.
    The page layout is removed from the whole score, since it is shared by all parts.
    """
    try:
        sound = SOUNDS[instrument.id]
    except KeyError:
        raise FixupError(f"no sound known for instrument {instrument.id!r}") from None
    if instrument.clef not in CLEFS:
        raise FixupError(f"clef {instrument.clef!r} is not one of {sorted(CLEFS)}")
    part = find_part(root, part_id)
    score_part = _score_part(root, part.get("id", ""))

    rewrites, volta_suspects = fix_volta_endings(part)
    volta_suspects = list(dict.fromkeys(volta_suspects))
    marked = tuple(
        dict.fromkeys(
            [
                *suspect_measures,
                *(s.measure for s in volta_suspects),
                *(r.first for r in rewrites),
            ]
        )
    )
    _add_marks(part, marked, mark_text)
    _set_name_and_sound(score_part, part, instrument, sound, part_name)
    _set_clef_and_transpose(part, instrument)
    remove_page_layout(root)
    return FixupResult(tuple(rewrites), tuple(volta_suspects), marked)


def fix_instrument_file(
    src: Path,
    dst: Path,
    instrument: Instrument,
    *,
    part_id: str | None = None,
    part_name: str | None = None,
    suspect_measures: Iterable[str] = (),
    mark_text: str = DEFAULT_MARK,
) -> FixupResult:
    """Read src, apply fix_instrument, write dst (which may be src)."""
    root = read_musicxml(src)
    result = fix_instrument(
        root,
        instrument,
        part_id=part_id,
        part_name=part_name,
        suspect_measures=suspect_measures,
        mark_text=mark_text,
    )
    write_musicxml(root, dst)
    return result
