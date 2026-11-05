"""Typed loader for instruments.json."""

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from omr_ingestor.paths import INSTRUMENTS_PATH

VALID_CLEFS = frozenset({"G2", "F4"})


class InstrumentsError(Exception):
    """Raised when instruments.json is malformed or an instrument id is unknown."""


@dataclass(frozen=True, slots=True)
class Transpose:
    """MusicXML <transpose> values, from written pitch to sounding pitch."""

    diatonic: int
    chromatic: int
    octave_change: int


@dataclass(frozen=True, slots=True)
class Instrument:
    id: str
    name_pt: str
    clef: str
    transpose: Transpose
    source: str


def _check_keys(obj: Any, cls: type, where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise InstrumentsError(f"{where}: expected an object, got {type(obj).__name__}")
    expected = {f.name for f in fields(cls)}
    unknown = sorted(obj.keys() - expected)
    missing = sorted(expected - obj.keys())
    if unknown:
        raise InstrumentsError(
            f"{where}: unknown key(s) {unknown}; allowed keys are {sorted(expected)}"
        )
    if missing:
        raise InstrumentsError(f"{where}: missing key(s) {missing}")
    return obj


def _expect(value: Any, typ: type, where: str) -> Any:
    # bool is a subclass of int; reject it explicitly for integer fields.
    if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
        raise InstrumentsError(f"{where}: expected {typ.__name__}, got {value!r}")
    return value


def _parse_instrument(raw: Any, where: str) -> Instrument:
    obj = _check_keys(raw, Instrument, where)
    tr = _check_keys(obj["transpose"], Transpose, f"{where}.transpose")
    clef = _expect(obj["clef"], str, f"{where}.clef")
    if clef not in VALID_CLEFS:
        raise InstrumentsError(f"{where}.clef: {clef!r} is not one of {sorted(VALID_CLEFS)}")
    return Instrument(
        id=_expect(obj["id"], str, f"{where}.id"),
        name_pt=_expect(obj["name_pt"], str, f"{where}.name_pt"),
        clef=clef,
        transpose=Transpose(**{k: _expect(tr[k], int, f"{where}.transpose.{k}") for k in tr}),
        source=_expect(obj["source"], str, f"{where}.source"),
    )


class InstrumentTable:
    """The instruments from instruments.json, looked up by id."""

    def __init__(self, instruments: list[Instrument]) -> None:
        self._by_id: dict[str, Instrument] = {}
        for inst in instruments:
            if inst.id in self._by_id:
                raise InstrumentsError(f"duplicate instrument id {inst.id!r}")
            self._by_id[inst.id] = inst

    def __getitem__(self, instrument_id: str) -> Instrument:
        try:
            return self._by_id[instrument_id]
        except KeyError:
            raise InstrumentsError(
                f"unknown instrument {instrument_id!r}; known instruments are {self.ids}"
            ) from None

    def __contains__(self, instrument_id: object) -> bool:
        return instrument_id in self._by_id

    def __iter__(self):
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def ids(self) -> list[str]:
        return list(self._by_id)


def load_instruments(path: Path = INSTRUMENTS_PATH) -> InstrumentTable:
    """Load and type-check instruments.json. Raises InstrumentsError on any problem."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise InstrumentsError(f"{path}: cannot read file: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise InstrumentsError(
            f"{path}: not valid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(raw, list):
        raise InstrumentsError(f"{path}: expected a JSON array of instruments")
    return InstrumentTable([_parse_instrument(item, f"{path}[{i}]") for i, item in enumerate(raw)])
