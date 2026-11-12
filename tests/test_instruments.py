import json

import pytest

from omr_ingestor.instruments import (
    Instrument,
    InstrumentsError,
    Transpose,
    load_instruments,
)
from omr_ingestor.paths import INSTRUMENTS_PATH

RAW = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8"))


def test_loads_all_six_as_dataclasses():
    table = load_instruments()
    assert table.ids == [
        "bb-trumpet",
        "trombone",
        "flute",
        "alto-saxophone",
        "tenor-saxophone",
        "tuba",
    ]
    tenor = table["tenor-saxophone"]
    assert tenor == Instrument(
        id="tenor-saxophone",
        name_pt="Sax tenor em Sib",
        clef="G2",
        transpose=Transpose(diatonic=-1, chromatic=-2, octave_change=-1),
        instrument_sound="wind.reed.saxophone.tenor",
        midi_program=67,
        source="OMR study",
    )


def test_unknown_instrument_id():
    with pytest.raises(InstrumentsError, match=r"unknown instrument 'piano'; known instruments"):
        load_instruments()["piano"]


@pytest.mark.parametrize(
    ("instrument_id", "sound", "program"),
    [
        ("bb-trumpet", "brass.trumpet.bflat", 57),
        ("trombone", "brass.trombone", 58),
        ("flute", "wind.flutes.flute", 74),
        ("alto-saxophone", "wind.reed.saxophone.alto", 66),
        ("tenor-saxophone", "wind.reed.saxophone.tenor", 67),
        ("tuba", "brass.tuba", 59),
    ],
)
def test_sound_values_preserve_original_table(instrument_id, sound, program):
    instrument = load_instruments()[instrument_id]
    assert (instrument.instrument_sound, instrument.midi_program) == (sound, program)


def entry(**changes):
    item = json.loads(json.dumps(RAW[0]))
    item.update(changes)
    return item


@pytest.mark.parametrize("field", ["instrument_sound", "midi_program"])
def test_sound_fields_are_required(write_json, field):
    data = entry()
    del data[field]
    with pytest.raises(InstrumentsError, match=rf"missing key\(s\) \['{field}'\]"):
        load_instruments(write_json([data]))


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"instrument_sound": None}, r"instrument_sound: expected str"),
        ({"instrument_sound": 57}, r"instrument_sound: expected str"),
        ({"instrument_sound": ""}, r"instrument_sound: expected a non-empty sound id"),
        ({"instrument_sound": "  "}, r"instrument_sound: expected a non-empty sound id"),
        ({"midi_program": "57"}, r"midi_program: expected int"),
        ({"midi_program": 57.0}, r"midi_program: expected int"),
        ({"midi_program": True}, r"midi_program: expected int"),
        ({"midi_program": None}, r"midi_program: expected int"),
        ({"midi_program": 0}, r"midi_program: expected an integer from 1 to 128"),
        ({"midi_program": 129}, r"midi_program: expected an integer from 1 to 128"),
    ],
)
def test_invalid_sound_fields_fail_clearly(write_json, changes, match):
    with pytest.raises(InstrumentsError, match=match):
        load_instruments(write_json([entry(**changes)]))


@pytest.mark.parametrize("program", [1, 128])
def test_midi_program_boundaries_are_valid(write_json, program):
    table = load_instruments(write_json([entry(midi_program=program)]))
    assert table["bb-trumpet"].midi_program == program


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ([entry(color="red")], r"\[0\]: unknown key\(s\) \['color'\]"),
        (
            [entry(transpose={"diatonic": 0, "chromatic": 0, "octave_change": 0, "cents": 0})],
            r"\[0\]\.transpose: unknown key\(s\) \['cents'\]",
        ),
        ([{k: v for k, v in RAW[0].items() if k != "clef"}], r"missing key\(s\) \['clef'\]"),
        ([entry(clef="C3")], r"\.clef: 'C3' is not one of"),
        (
            [entry(transpose={"diatonic": "1", "chromatic": 0, "octave_change": 0})],
            r"transpose\.diatonic: expected int",
        ),
        ([entry(), entry()], r"duplicate instrument id 'bb-trumpet'"),
        ({"bb-trumpet": {}}, r"expected a JSON array"),
    ],
)
def test_malformed_file_fails_clearly(write_json, data, match):
    with pytest.raises(InstrumentsError, match=match):
        load_instruments(write_json(data, "instruments.json"))
