"""Reading and writing partwise MusicXML with xml.etree.

MusicXML has no XML namespace, so element tags are plain names such as "measure".
The DOCTYPE of the source file is not preserved on write; it is optional for both
the XSD and MuseScore.
"""

import xml.etree.ElementTree as ET
from pathlib import Path


class MusicXMLError(Exception):
    """Raised when a file is not usable partwise MusicXML."""


def _check_root(root: ET.Element, source: str) -> ET.Element:
    if root.tag == "score-timewise":
        raise MusicXMLError(f"{source}: score-timewise is not supported, only score-partwise")
    if root.tag != "score-partwise":
        raise MusicXMLError(f"{source}: root element is <{root.tag}>, expected <score-partwise>")
    return root


def parse_musicxml(text: str | bytes, *, source: str = "<string>") -> ET.Element:
    """Parse MusicXML text and return the <score-partwise> root."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        line, col = exc.position
        raise MusicXMLError(f"{source}: not well-formed XML at line {line}, column {col}") from exc
    return _check_root(root, source)


def read_musicxml(path: Path) -> ET.Element:
    """Read an uncompressed MusicXML file and return the <score-partwise> root."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise MusicXMLError(f"{path}: cannot read file: {exc.strerror}") from exc
    return parse_musicxml(data, source=str(path))


def write_musicxml(root: ET.Element, path: Path) -> None:
    """Write the score as UTF-8 with an XML declaration."""
    ET.ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)


def part_ids(root: ET.Element) -> list[str]:
    return [part.get("id", "") for part in root.findall("part")]


def has_safe_part_ids(root: ET.Element) -> bool:
    """Require each ID to be nonempty, unique and declared exactly once."""
    ids = part_ids(root)
    declared = [part.get("id", "") for part in root.findall("part-list/score-part")]
    return (
        all(pid.strip() for pid in ids)
        and len(set(ids)) == len(ids)
        and len(set(declared)) == len(declared)
        and set(ids) == set(declared)
    )


def find_part(root: ET.Element, part_id: str | None) -> ET.Element:
    """Return the <part> with this id; with part_id=None the score must have exactly one part."""
    parts = root.findall("part")
    if part_id is None:
        if len(parts) != 1:
            raise MusicXMLError(
                f"score has {len(parts)} parts {part_ids(root)}; pass the part id explicitly"
            )
        return parts[0]
    for part in parts:
        if part.get("id") == part_id:
            return part
    raise MusicXMLError(f"part {part_id!r} not found; parts are {part_ids(root)}")
