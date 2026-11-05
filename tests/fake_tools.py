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
