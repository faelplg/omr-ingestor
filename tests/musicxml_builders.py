"""Builders for small synthetic MusicXML documents."""

from omr_ingestor.musicxml import parse_musicxml

DEFAULT_ATTRS = (
    "<attributes><divisions>1</divisions>"
    "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
)


def note(duration: int | str, *, step: str = "C", chord: bool = False, extra: str = "") -> str:
    chord_el = "<chord/>" if chord else ""
    return (
        f"<note>{chord_el}<pitch><step>{step}</step><octave>4</octave></pitch>"
        f"<duration>{duration}</duration>{extra}<type>quarter</type></note>"
    )


def rest(duration: int | str, *, measure: bool = False) -> str:
    attr = ' measure="yes"' if measure else ""
    return f"<note><rest{attr}/><duration>{duration}</duration></note>"


def measure(number: int | str, body: str, *, attrs: str = "") -> str:
    return f'<measure number="{number}"{attrs}>{body}</measure>'


def score(parts: dict[str, list[str]], *, header: str = "", score_parts: str | None = None) -> str:
    if score_parts is None:
        score_parts = "".join(
            f'<score-part id="{pid}"><part-name>OMR {pid}</part-name></score-part>' for pid in parts
        )
    body = "".join(f'<part id="{pid}">{"".join(ms)}</part>' for pid, ms in parts.items())
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<score-partwise version="4.0">{header}<part-list>{score_parts}</part-list>'
        f"{body}</score-partwise>"
    )


def barline(
    location: str,
    *,
    ending: tuple[str, str] | None = None,
    repeat: str | None = None,
    end_length: str | None = None,
) -> str:
    """A <barline>; ending is (number, type), repeat is a direction."""
    body = ""
    if ending is not None:
        number, kind = ending
        length = f' end-length="{end_length}"' if end_length else ""
        body += f'<ending number="{number}" type="{kind}"{length}/>'
    if repeat is not None:
        body += f'<repeat direction="{repeat}"/>'
    return f'<barline location="{location}">{body}</barline>'


def multiple_rest(bars: int | str) -> str:
    """An <attributes> that starts a multi-measure rest of this many bars."""
    style = f"<measure-style><multiple-rest>{bars}</multiple-rest></measure-style>"
    return f"<attributes>{style}</attributes>"


HEADER = (
    "<work><work-number>1</work-number><work-title>Title</work-title></work>"
    "<movement-number>2</movement-number><movement-title>Movement</movement-title>"
    '<identification><creator type="composer">Composer</creator><rights>Rights</rights>'
    "<encoding><software>Synthetic</software></encoding><source>Source</source>"
    '<relation>Related work</relation><miscellaneous><miscellaneous-field name="x">'
    "Header</miscellaneous-field></miscellaneous></identification>"
    "<credit><credit-words>Credit</credit-words></credit>"
)
TECHNICAL = (
    "<fingering>1</fingering><pluck>p</pluck><fret>3</fret><string>1</string>"
    '<hammer-on type="start">H</hammer-on><pull-off type="stop">P</pull-off>'
    "<tap>T</tap><other-technical>Text</other-technical>"
)
NOTE = (
    "<note><pitch><step>C</step><alter>1</alter><octave>4</octave></pitch>"
    '<duration>4</duration><tie type="start"/><type>whole</type><accidental>sharp</accidental>'
    "<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes>"
    '</time-modification><notations><tied type="start"/><slur type="start"/>'
    '<tuplet type="start"/><articulations><staccato/></articulations>'
    "<technical><up-bow/>" + TECHNICAL + "</technical><dynamics><f/></dynamics></notations>"
    "<lyric><text>D.C. al Fine</text></lyric></note>"
)
DIRECTIONS = (
    "<direction><direction-type><words>Free text</words></direction-type></direction>"
    "<direction><direction-type><rehearsal>A</rehearsal></direction-type></direction>"
    "<direction><direction-type><words>D.C.</words></direction-type>"
    '<offset>2</offset><sound dacapo="yes"/></direction>'
    '<direction><direction-type><segno/></direction-type><sound segno="s"/></direction>'
    '<direction><direction-type><coda/></direction-type><sound coda="c"/></direction>'
    "<direction><direction-type><words>D.S. al Coda</words></direction-type>"
    '<sound dalsegno="s"/></direction>'
    "<direction><direction-type><words>To Coda</words></direction-type>"
    '<sound tocoda="c"/></direction>'
    "<direction><direction-type><words>solo</words><symbol>coda</symbol>"
    "</direction-type></direction>"
    "<direction><direction-type><words>Loud</words></direction-type>"
    "<direction-type><dynamics><ff/></dynamics></direction-type></direction>"
)


def synthetic_score():
    attrs = DEFAULT_ATTRS.replace("<time>", "<key><fifths>2</fifths></key><time>").replace(
        "</attributes>", "<clef><sign>G</sign><line>2</line></clef></attributes>"
    )
    score_parts = "".join(
        f'<score-part id="{p}"><part-name>Trumpet {p}</part-name>'
        f'<score-instrument id="{p}-I1"><instrument-name>Trumpet</instrument-name>'
        "<instrument-sound>brass.trumpet</instrument-sound></score-instrument>"
        f'<midi-instrument id="{p}-I1"><midi-channel>1</midi-channel>'
        "<midi-program>57</midi-program></midi-instrument></score-part>"
        for p in ("P1", "P2")
    )
    return parse_musicxml(
        score(
            {
                p: [
                    measure(
                        1,
                        attrs
                        + DIRECTIONS
                        + NOTE
                        + (
                            "<note><rest/><duration>1</duration></note>"
                            '<barline location="right"><ending number="1" type="start"/>'
                            '<repeat direction="backward"/></barline>'
                        ),
                    )
                ]
                for p in ("P1", "P2")
            },
            header=HEADER,
            score_parts=score_parts,
        )
    )


OMR_HEADER = (
    "<defaults><scaling><millimeters>7</millimeters><tenths>40</tenths></scaling>"
    "<page-layout><page-height>1700</page-height><page-width>1200</page-width></page-layout>"
    "<system-layout><system-distance>120</system-distance></system-layout>"
    "<staff-layout><staff-distance>60</staff-distance></staff-layout></defaults>"
)


OMR_SCORE_PART = (
    '<score-part id="P1"><part-name>Voice</part-name>'
    "<part-name-display><display-text>Voice</display-text></part-name-display>"
    "<part-abbreviation>Pno.</part-abbreviation>"
    "<part-abbreviation-display><display-text>Pno.</display-text></part-abbreviation-display>"
    '<score-instrument id="P1-I1"><instrument-name>Piano</instrument-name></score-instrument>'
    '<score-instrument id="P1-I2"><instrument-name>Organ</instrument-name></score-instrument>'
    '<midi-instrument id="P1-I1"><midi-channel>3</midi-channel>'
    "<midi-program>1</midi-program><volume>80</volume></midi-instrument>"
    "</score-part>"
)


FIRST_ATTRS = (
    "<attributes><divisions>1</divisions><key><fifths>0</fifths></key>"
    "<time><beats>4</beats><beat-type>4</beat-type></time>"
    "<clef><sign>F</sign><line>4</line></clef>"
    "<transpose><diatonic>0</diatonic><chromatic>-12</chromatic></transpose></attributes>"
)


PRINT = (
    '<print new-system="yes"><system-layout>'
    "<system-distance>90</system-distance></system-layout></print>"
)


C_CLEF = "<attributes><clef><sign>C</sign><line>4</line></clef></attributes>"


Q = note(1)


I2_NOTE = note(1, extra='<instrument id="P1-I2"/>')


# Mid-part switches to P1-I2: a standalone <sound> and one inside a <direction>.
SWITCH_TO_I2 = (
    '<sound><midi-instrument id="P1-I2"><midi-program>20</midi-program></midi-instrument></sound>'
    '<direction><direction-type><words>Organ</words></direction-type><sound tempo="90">'
    '<instrument-change id="P1-I2"><instrument-sound>keyboard.organ</instrument-sound>'
    '</instrument-change><midi-instrument id="P1-I2"><midi-program>20</midi-program>'
    "</midi-instrument></sound></direction>"
)


def omr_score() -> str:
    return score(
        {
            "P1": [
                measure(1, PRINT + FIRST_ATTRS + Q * 4, attrs=' width="300"'),
                measure(2, Q * 3, attrs=' width="250"'),
                measure(3, PRINT + C_CLEF + SWITCH_TO_I2 + I2_NOTE * 4),
            ]
        },
        header=OMR_HEADER,
        score_parts=OMR_SCORE_PART,
    )


def start(number: str, **kwargs) -> str:
    return barline("left", ending=(number, "start"), **kwargs)


def stop(number: str, kind: str = "stop", *, repeat: bool = False, **kwargs) -> str:
    return barline("right", ending=(number, kind), repeat="backward" if repeat else None, **kwargs)


def volta_score(*bodies: str) -> str:
    """Measure 1, then measure i + 2 with four quarters and the barlines in bodies[i]."""
    ms = [measure(1, FIRST_ATTRS + Q * 4)]
    ms += [measure(i, Q * 4 + body) for i, body in enumerate(bodies, 2)]
    return score({"P1": ms}, score_parts=OMR_SCORE_PART)
