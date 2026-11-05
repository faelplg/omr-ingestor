"""Builders for small synthetic MusicXML documents."""

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
