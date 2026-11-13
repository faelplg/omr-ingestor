"""Raw MusicXML metadata used to bind human reviews to parts."""

import xml.etree.ElementTree as ET

from omr_ingestor.status import PartIdentity


def raw_part_identities(root: ET.Element) -> dict[str, PartIdentity]:
    """Extract names before instrument fixup; callers must check structural safety."""
    return {
        part.get("id", ""): PartIdentity(
            part_name=part.findtext("part-name") or "",
            instruments=tuple(
                instrument.findtext("instrument-name") or ""
                for instrument in part.findall("score-instrument")
            ),
        )
        for part in root.findall("part-list/score-part")
    }
