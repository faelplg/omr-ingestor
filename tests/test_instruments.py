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
        source="OMR study",
    )


def test_unknown_instrument_id():
    with pytest.raises(InstrumentsError, match=r"unknown instrument 'piano'; known instruments"):
        load_instruments()["piano"]


def entry(**changes):
    item = json.loads(json.dumps(RAW[0]))
    item.update(changes)
    return item


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
