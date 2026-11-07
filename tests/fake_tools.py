"""Fake command-line tools for tests: a /bin/sh script that runs a small Python body."""

import stat
import sys
import textwrap
from pathlib import Path


def make_tool(directory: Path, name: str, body: str) -> Path:
    """Write an executable `directory/name` whose behavior is the Python `body`.

    The body sees `sys.argv` as the tool would (argv[1:] are the tool's arguments).
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"{name}.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    exe = directory / name
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


# Accepts any score-partwise; the real MusicXML XSD is not in the repository.
ANY_SCORE_XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
    '<xs:element name="score-partwise"><xs:complexType><xs:sequence>'
    '<xs:any processContents="skip" minOccurs="0" maxOccurs="unbounded"/>'
    '</xs:sequence><xs:anyAttribute processContents="skip"/></xs:complexType></xs:element>'
    "</xs:schema>"
)


CONTAINER = (
    '<container><rootfiles><rootfile full-path="score.xml" '
    'media-type="application/vnd.recordare.musicxml+xml"/></rootfiles></container>'
)


def fake_audiveris(bin_dir: Path, movements: list[str] | dict[str, list[str]]) -> Path:
    """Writes one .mxl per MusicXML text in movements (or in movements[pdf stem])."""
    return make_tool(
        bin_dir,
        "Audiveris",
        f"""
        import sys, zipfile
        from pathlib import Path
        args = sys.argv[1:]
        out = Path(args[args.index("-output") + 1])
        pdf = Path(args[args.index("--") + 1])
        print("Audiveris version 5.11.0")
        movements = {movements!r}
        if isinstance(movements, dict):
            movements = movements[pdf.stem]
        for i, text in enumerate(movements, 1):
            name = pdf.stem + (f".mvt{{i}}" if len(movements) > 1 else "") + ".mxl"
            with zipfile.ZipFile(out / name, "w") as zf:
                zf.writestr("META-INF/container.xml", {CONTAINER!r})
                zf.writestr("score.xml", text)
        """,
    )


def fake_musescore(bin_dir: Path, mscx: str | dict[str, str]) -> Path:
    """Writes mscx (or mscx[pdf stem]) plus the -S style; snapshots status.json as it was."""
    return make_tool(
        bin_dir,
        "mscore",
        f"""
        import json, shutil, sys, zipfile
        from pathlib import Path
        args = sys.argv[1:]
        if args == ["--version"]:
            print("MuseScore4 4.7.5")
            sys.exit(0)
        here = Path(sys.argv[0])
        here.with_suffix(".args").write_text(json.dumps(args))
        out = Path(args[args.index("-o") + 1])
        style = Path(args[args.index("-S") + 1])
        shutil.copy(out.parent.parent / "status.json", here.with_suffix(".status.json"))
        mscx = {mscx!r}
        if isinstance(mscx, dict):
            mscx = mscx[out.parent.parent.name]
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("score.mscx", mscx)
            zf.writestr("score_style.mss", style.read_text())
        sys.exit(134)
        """,
    )
