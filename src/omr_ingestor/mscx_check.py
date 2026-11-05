"""Pipeline stage 6: final check of the .mscx inside the .mscz written by MuseScore.

The mscore exit code is not reliable, so the result is read back from the file.
Layout of a MuseScore 4 .mscz (measured on 4.7.5 output): a zip with
META-INF/container.xml listing the root files, the main .mscx at the top level,
part excerpts under Excerpts/, and the score style in score_style.mss, not in the
.mscx. In the .mscx, <Part><Instrument id> is the MuseScore instrument id (the same
ids as instruments.json), the octave is folded into transposeDiatonic and
transposeChromatic, and the written clef is the header <Clef> of the first measure.
"""

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from omr_ingestor.instruments import Instrument

CONTAINER_PATH = "META-INF/container.xml"
STYLE_PATH = "score_style.mss"
MM_PER_INCH = 25.4

# instruments.json clef -> MuseScore ClefType name.
MUSESCORE_CLEFS: dict[str, str] = {"G2": "G", "F4": "F"}
_CLEFS_BACK = {v: k for k, v in MUSESCORE_CLEFS.items()}


class MscxCheckError(Exception):
    """Raised when the .mscz cannot be opened or has no readable main .mscx."""


@dataclass(frozen=True, slots=True)
class PageSize:
    width_mm: float
    height_mm: float
    name: str = ""
    tolerance_mm: float = 0.5  # MuseScore stores inches with 2 decimals

    def matches(self, width_mm: float, height_mm: float) -> bool:
        return (
            abs(width_mm - self.width_mm) <= self.tolerance_mm
            and abs(height_mm - self.height_mm) <= self.tolerance_mm
        )

    def describe(self) -> str:
        size = f"{self.width_mm:g} x {self.height_mm:g} mm"
        return f"{self.name} ({size})" if self.name else size


# The page of Estilo 2026.mss.
A5_LANDSCAPE = PageSize(210, 148, "A5 landscape")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    found: str
    expected: str

    def describe(self) -> str:
        verdict = "pass" if self.passed else "FAIL"
        return f"{self.name}: {verdict} (found {self.found}, expected {self.expected})"


@dataclass(frozen=True, slots=True)
class MscxCheckResult:
    mscz: Path
    mscx: str  # path of the main .mscx inside the zip
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


def _check(name: str, found: object, expected: object) -> Check:
    found_s, expected_s = str(found), str(expected)
    return Check(name, found_s == expected_s, found_s, expected_s)


def _parse(data: bytes, where: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        line, col = exc.position
        raise MscxCheckError(f"{where}: not well-formed XML at line {line}, column {col}") from exc


def _is_main_mscx(name: str) -> bool:
    path = PurePosixPath(name)
    return path.suffix == ".mscx" and len(path.parts) == 1


def _main_mscx_name(zf: zipfile.ZipFile, source: str) -> str:
    names = zf.namelist()
    if CONTAINER_PATH in names:
        container = _parse(zf.read(CONTAINER_PATH), f"{source}:{CONTAINER_PATH}")
        for rootfile in container.iterfind("rootfiles/rootfile"):
            path = rootfile.get("full-path", "")
            if _is_main_mscx(path) and path in names:
                return path
    candidates = [n for n in names if _is_main_mscx(n)]
    if len(candidates) != 1:
        raise MscxCheckError(
            f"{source}: expected one top-level .mscx, found {len(candidates)} {candidates}"
        )
    return candidates[0]


def _int_text(parent: ET.Element, tag: str) -> str:
    text = (parent.findtext(tag) or "0").strip()
    try:
        return str(int(text))
    except ValueError:
        return text


def _transposition(instrument: Instrument) -> str:
    t = instrument.transpose
    return _format_transposition(
        str(t.diatonic + 7 * t.octave_change), str(t.chromatic + 12 * t.octave_change)
    )


def _format_transposition(diatonic: str, chromatic: str) -> str:
    return f"diatonic {diatonic}, chromatic {chromatic}"


def _written_clef(score: ET.Element, part: ET.Element, inst: ET.Element) -> str:
    """The clef the part starts with, as written (transposing), as a MuseScore ClefType."""
    staff = score.find("Staff")
    measure = staff.find("Measure") if staff is not None else None
    clef = measure.find("voice/Clef") if measure is not None else None
    if clef is not None:
        for tag in ("transposingClefType", "concertClefType"):
            if (text := clef.findtext(tag)) is not None:
                return text.strip()
    part_staff = part.find("Staff")
    sources = []
    if part_staff is not None:
        sources.append((part_staff, ("defaultTransposingClef", "defaultClef")))
    sources.append((inst, ("transposingClef", "clef")))
    for el, tags in sources:
        for tag in tags:
            if (text := el.findtext(tag)) is not None:
                return text.strip()
    return "G"  # MuseScore's default when nothing is written


def _page_size(zf: zipfile.ZipFile, source: str) -> tuple[float, float] | str:
    """Page width and height in mm, or a description of why they cannot be read."""
    if STYLE_PATH not in zf.namelist():
        return f"no {STYLE_PATH}"
    style = _parse(zf.read(STYLE_PATH), f"{source}:{STYLE_PATH}").find("Style")
    if style is None:
        return f"no <Style> in {STYLE_PATH}"
    values = []
    for tag in ("pageWidth", "pageHeight"):
        text = style.findtext(tag)
        try:
            values.append(float(text) * MM_PER_INCH)
        except (TypeError, ValueError):
            return f"{tag} {text!r}"
    return values[0], values[1]


def _page_check(zf: zipfile.ZipFile, source: str, expected: PageSize) -> Check:
    size = _page_size(zf, source)
    if isinstance(size, str):
        return Check("page_size", False, size, expected.describe())
    width, height = size
    found = f"{width:.1f} x {height:.1f} mm"
    return Check("page_size", expected.matches(width, height), found, expected.describe())


def check_mscz(
    path: Path, instrument: Instrument, *, page_size: PageSize = A5_LANDSCAPE
) -> MscxCheckResult:
    """Check a single-part .mscz against an instruments.json entry and the style page size.

    Every check is reported, pass or fail. Raises MscxCheckError only when the file
    cannot be read at all.
    """
    source = str(path)
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise MscxCheckError(f"{source}: cannot open as a .mscz (zip): {exc}") from exc
    with zf:
        mscx = _main_mscx_name(zf, source)
        root = _parse(zf.read(mscx), f"{source}:{mscx}")
        score = root.find("Score")
        if score is None:
            raise MscxCheckError(f"{source}:{mscx}: no <Score> under <{root.tag}>")
        parts = score.findall("Part")
        checks = [_check("part_count", len(parts), 1)]
        part = parts[0] if parts else None
        inst = part.find("Instrument") if part is not None else None
        if part is None or inst is None:
            missing = "no <Part>" if part is None else "no <Instrument>"
            checks += [
                Check("instrument", False, missing, instrument.id),
                Check("part_name", False, missing, instrument.name_pt),
                Check("transposition", False, missing, _transposition(instrument)),
                Check("clef", False, missing, instrument.clef),
            ]
        else:
            clef = _written_clef(score, part, inst)
            found_transposition = _format_transposition(
                _int_text(inst, "transposeDiatonic"), _int_text(inst, "transposeChromatic")
            )
            checks += [
                _check("instrument", inst.get("id", ""), instrument.id),
                _check("part_name", (inst.findtext("longName") or "").strip(), instrument.name_pt),
                _check("transposition", found_transposition, _transposition(instrument)),
                _check("clef", _CLEFS_BACK.get(clef, clef), instrument.clef),
            ]
        checks.append(_page_check(zf, source, page_size))
    return MscxCheckResult(Path(path), mscx, tuple(checks))
