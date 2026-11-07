"""Synthetic MuseScore XML builders shared by tests."""

from omr_ingestor.instruments import load_instruments

INSTRUMENTS = load_instruments()


def part_xml(
    inst_id: str,
    name: str,
    diatonic: int = 0,
    chromatic: int = 0,
    extra: str = "",
) -> str:
    transpose = ""
    if (diatonic, chromatic) != (0, 0):
        transpose = (
            f"<transposeDiatonic>{diatonic}</transposeDiatonic>"
            f"<transposeChromatic>{chromatic}</transposeChromatic>"
        )
    return (
        '<Part id="1"><Staff id="1"><StaffType group="pitched"><name>stdNormal</name>'
        f"</StaffType></Staff><trackName>{name}</trackName>"
        f'<Instrument id="{inst_id}"><longName>{name}</longName>{transpose}{extra}'
        "</Instrument></Part>"
    )


def mscx_xml(parts: str, clef: str | None = "G") -> str:
    header = ""
    if clef is not None:
        header = (
            f"<Clef><concertClefType>{clef}</concertClefType>"
            f"<transposingClefType>{clef}</transposingClefType><isHeader>1</isHeader></Clef>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><museScore version="4.70">'
        "<programVersion>4.7.5</programVersion><Score><Division>480</Division>"
        f'{parts}<Staff id="1"><Measure><voice>{header}'
        "<TimeSig><sigN>4</sigN><sigD>4</sigD></TimeSig></voice></Measure></Staff>"
        "</Score></museScore>"
    )


def style_xml(width: str = "8.27", height: str = "5.83") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><museScore version="4.70"><Style>'
        f"<pageWidth>{width}</pageWidth><pageHeight>{height}</pageHeight>"
        "<spatium>1.75</spatium></Style></museScore>"
    )


def instrument_mscx(inst_id: str) -> str:
    """The .mscx MuseScore 4.7.5 writes for an instrument from instruments.json."""
    inst = INSTRUMENTS[inst_id]
    t = inst.transpose
    part = part_xml(
        inst.id,
        inst.name_pt,
        t.diatonic + 7 * t.octave_change,
        t.chromatic + 12 * t.octave_change,
    )
    return mscx_xml(part, {"G2": "G", "F4": "F"}[inst.clef])


def two_trumpet_mscx():
    first = part_xml("bb-trumpet", "Trumpet 1", -1, -2)
    second = part_xml("bb-trumpet", "Trumpet 2", -1, -2).replace('id="1"', 'id="2"')
    xml = mscx_xml(first + second)
    staff = xml[xml.index('<Staff id="1"><Measure>') : xml.index("</Score>")]
    return xml.replace("</Score>", staff.replace('id="1"', 'id="2"') + "</Score>")
