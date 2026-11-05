"""Pipeline stage 4: apply an instruments.json entry to a MusicXML part.

The OMR result keeps its pitches; this stage only sets what the instrument decides
(transposition, clef, name, sound), drops the OMR page layout so the MuseScore style
can lay the score out, and marks suspect measures with a rehearsal mark.
"""

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
    score_part: ET.Element, part: ET.Element, instrument: Instrument, sound: Sound
) -> None:
    name = score_part.find("part-name")
    if name is None:
        name = ET.Element("part-name")
        _insert_ordered(score_part, name, _SCORE_PART_ORDER)
    name.text = instrument.name_pt
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


def _has_mark(measure: ET.Element, text: str) -> bool:
    return any(
        r.text == text for r in measure.iterfind("direction/direction-type/rehearsal")
    )


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


def fix_instrument(
    root: ET.Element,
    instrument: Instrument,
    *,
    part_id: str | None = None,
    suspect_measures: Iterable[str] = (),
    mark_text: str = DEFAULT_MARK,
) -> None:
    """Apply an instrument to one part of the score, in place. Safe to run twice.

    part_id=None works on single-part scores. suspect_measures are measure numbers
    (as in DurationReport.suspect_numbers); each gets a rehearsal mark with mark_text.
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

    _add_marks(part, suspect_measures, mark_text)
    _set_name_and_sound(score_part, part, instrument, sound)
    _set_clef_and_transpose(part, instrument)
    remove_page_layout(root)


def fix_instrument_file(
    src: Path,
    dst: Path,
    instrument: Instrument,
    *,
    part_id: str | None = None,
    suspect_measures: Iterable[str] = (),
    mark_text: str = DEFAULT_MARK,
) -> None:
    """Read src, apply fix_instrument, write dst (which may be src)."""
    root = read_musicxml(src)
    fix_instrument(
        root,
        instrument,
        part_id=part_id,
        suspect_measures=suspect_measures,
        mark_text=mark_text,
    )
    write_musicxml(root, dst)
