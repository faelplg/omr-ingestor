"""Pipeline stage 5: convert MusicXML to .mscz with MuseScore and a style file (-S).

The MuseScore binary comes from an argument or from $OMR_MUSESCORE (on macOS usually
/Applications/MuseScore 4.app/Contents/MacOS/mscore). Its exit code is not reliable (it
once exited 134 with the file generated), so success is judged by the output: it must
exist, be non-empty and be a valid zip holding a .mscx. The exit code is only reported.

MuseScore writes into a temporary folder next to the target; the file replaces the target
only when it passes those checks, so a failed run never leaves a stale or partial .mscz.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

MUSESCORE_ENV_VAR = "OMR_MUSESCORE"

_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")


class MuseScoreError(Exception):
    """MuseScore could not run or did not produce a usable .mscz."""


@dataclass(frozen=True)
class MuseScoreResult:
    musicxml: Path
    style: Path
    output: Path
    version: str | None
    exit_code: int
    duration_s: float
    stdout: str
    stderr: str


def resolve_musescore(musescore: str | Path | None = None) -> Path:
    """The MuseScore executable: the explicit argument, else $OMR_MUSESCORE."""
    if musescore is None:
        musescore = os.environ.get(MUSESCORE_ENV_VAR, "").strip()
        if not musescore:
            raise MuseScoreError(
                f"no MuseScore configured; pass the binary path or set {MUSESCORE_ENV_VAR}"
            )
    raw = str(musescore)
    found = shutil.which(raw) if os.sep not in raw else raw
    exe = Path(found).expanduser() if found else None
    if exe is None or not exe.is_file() or not os.access(exe, os.X_OK):
        raise MuseScoreError(f"MuseScore executable not found or not executable: {raw}")
    return exe


def musescore_version(exe: Path, *, timeout: float = 60) -> str | None:
    """The version printed by `mscore --version` (e.g. "4.7.5"), or None if unknown."""
    try:
        proc = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else None


def check_mscz(path: Path) -> None:
    """Raise MuseScoreError unless path is a non-empty, valid zip holding a .mscx."""
    path = Path(path)
    if not path.is_file():
        raise MuseScoreError(f"{path}: file was not created")
    if path.stat().st_size == 0:
        raise MuseScoreError(f"{path}: file is empty")
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise MuseScoreError(f"{path}: not a valid zip: {exc}") from exc
    if bad is not None:
        raise MuseScoreError(f"{path}: corrupt member {bad!r}")
    if not any(n.endswith(".mscx") for n in names):
        raise MuseScoreError(f"{path}: no .mscx inside")


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def convert_to_mscz(
    musicxml: Path,
    style: Path,
    output: Path,
    *,
    musescore: str | Path | None = None,
    timeout: float = 600,
) -> MuseScoreResult:
    """Run `mscore -S <style> -o <output> <musicxml>`; an existing output is overwritten.

    Raises MuseScoreError when the output is missing, empty or not a valid .mscz,
    whatever the exit code.
    """
    musicxml, style, output = Path(musicxml), Path(style), Path(output)
    if not musicxml.is_file():
        raise MuseScoreError(f"MusicXML not found: {musicxml}")
    if not style.is_file():
        raise MuseScoreError(f"style file not found: {style}")
    if output.suffix != ".mscz":
        raise MuseScoreError(f"output must end in .mscz: {output}")
    exe = resolve_musescore(musescore)
    version = musescore_version(exe)
    output.parent.mkdir(parents=True, exist_ok=True)

    # A private folder on the same filesystem, so os.replace is atomic. The temp file
    # keeps the target name because MuseScore picks the format from the extension.
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{output.stem}.tmp-", dir=output.parent))
    tmp = tmp_dir / output.name
    try:
        cmd = [str(exe), "-S", str(style.resolve()), "-o", str(tmp), str(musicxml.resolve())]
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise MuseScoreError(f"MuseScore timed out after {timeout}s on {musicxml}") from None
        except OSError as exc:
            raise MuseScoreError(f"cannot run MuseScore ({exe}): {exc}") from exc
        duration = time.monotonic() - start
        try:
            check_mscz(tmp)
        except MuseScoreError as exc:
            last = _tail(f"{proc.stdout}\n{proc.stderr}")
            raise MuseScoreError(
                f"MuseScore did not produce a usable .mscz for {musicxml} "
                f"(exit code {proc.returncode}): {exc}\nlast output:\n{last}"
            ) from None
        os.replace(tmp, output)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return MuseScoreResult(
        musicxml=musicxml,
        style=style,
        output=output,
        version=version,
        exit_code=proc.returncode,
        duration_s=duration,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
