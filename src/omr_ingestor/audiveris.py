"""Pipeline stage 1: run Audiveris on a PDF and unzip the .mxl files it exports.

The Audiveris binary comes from an argument or from $OMR_AUDIVERIS (on macOS usually
/Applications/Audiveris.app/Contents/MacOS/Audiveris). Success is judged by the number
of .mxl files produced, not by the exit code. One PDF can yield several .mxl files
(one per movement), and each one is unzipped next to itself as `<stem>.musicxml`.

Each run happens in a fresh staging folder that replaces the output folder only when
it succeeds, so a failed run never destroys the result of a previous good one.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

AUDIVERIS_ENV_VAR = "OMR_AUDIVERIS"
LOG_NAME = "audiveris.log"
# Marks a folder as created by this module, so it is safe to replace it on a re-run.
MARKER_NAME = ".omr-ingestor-audiveris"

_CONTAINER = "META-INF/container.xml"
_MUSICXML_MEDIA_TYPE = "application/vnd.recordare.musicxml+xml"
_VERSION_RE = re.compile(r"Audiveris\D{0,20}?(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)


class AudiverisError(Exception):
    """Audiveris could not run, produced no .mxl, or produced an unreadable .mxl."""


@dataclass(frozen=True)
class AudiverisResult:
    pdf: Path
    output_dir: Path
    mxl_files: tuple[Path, ...]
    musicxml_files: tuple[Path, ...]
    log_path: Path
    version: str | None
    exit_code: int
    duration_s: float


def resolve_audiveris(audiveris: str | Path | None = None) -> Path:
    """The Audiveris executable: the explicit argument, else $OMR_AUDIVERIS."""
    if audiveris is None:
        audiveris = os.environ.get(AUDIVERIS_ENV_VAR, "").strip()
        if not audiveris:
            raise AudiverisError(
                f"no Audiveris configured; pass the binary path or set {AUDIVERIS_ENV_VAR}"
            )
    raw = str(audiveris)
    found = shutil.which(raw) if os.sep not in raw else raw
    # Absolute, so subprocess never searches PATH for a relative name like "Audiveris".
    exe = Path(found).expanduser().absolute() if found else None
    if exe is None or not exe.is_file() or not os.access(exe, os.X_OK):
        raise AudiverisError(f"Audiveris executable not found or not executable: {raw}")
    return exe


def _natural_key(path: Path) -> list:
    """Sort `x.mvt2.mxl` before `x.mvt10.mxl`."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(path))]


def _root_member(zf: zipfile.ZipFile, source: Path) -> str:
    """The MusicXML member named by META-INF/container.xml."""
    names = zf.namelist()
    if _CONTAINER not in names:
        candidates = [
            n for n in names if not n.startswith("META-INF/") and n.endswith((".xml", ".musicxml"))
        ]
        if len(candidates) == 1:
            return candidates[0]
        raise AudiverisError(f"{source}: no {_CONTAINER} and {len(candidates)} XML candidates")
    try:
        container = ET.fromstring(zf.read(_CONTAINER))
    except ET.ParseError as exc:
        raise AudiverisError(f"{source}: {_CONTAINER} is not well-formed XML") from exc
    # container.xml may or may not use a namespace; match on the local name.
    rootfiles = [el for el in container.iter() if el.tag.rpartition("}")[2] == "rootfile"]
    paths = [(el.get("full-path"), el.get("media-type")) for el in rootfiles if el.get("full-path")]
    if not paths:
        raise AudiverisError(f"{source}: {_CONTAINER} has no <rootfile full-path=...>")
    # The first rootfile is the MusicXML one unless a media-type says otherwise.
    chosen = next((p for p, mt in paths if mt == _MUSICXML_MEDIA_TYPE), paths[0][0])
    if chosen not in names:
        raise AudiverisError(f"{source}: root file {chosen!r} is missing from the archive")
    return chosen


def unzip_mxl(mxl: Path, dest: Path | None = None) -> Path:
    """Extract the root MusicXML of a .mxl to dest (default: `<stem>.musicxml` beside it)."""
    mxl = Path(mxl)
    dest = Path(dest) if dest is not None else mxl.with_suffix(".musicxml")
    try:
        with zipfile.ZipFile(mxl) as zf:
            data = zf.read(_root_member(zf, mxl))
    except (zipfile.BadZipFile, OSError) as exc:
        raise AudiverisError(f"{mxl}: not a readable .mxl (zip) file: {exc}") from exc
    dest.write_bytes(data)
    return dest


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _check_replaceable(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise AudiverisError(f"output path exists and is not a folder: {output_dir}")
    if any(output_dir.iterdir()) and not (output_dir / MARKER_NAME).is_file():
        raise AudiverisError(
            f"refusing to replace {output_dir}: it is not empty and was not created by "
            f"omr-ingestor (no {MARKER_NAME})"
        )


def _swap_in(staging: Path, output_dir: Path) -> None:
    """Replace output_dir with staging, removing the old folder only after the swap."""
    old = None
    if output_dir.exists():
        old = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.old-", dir=output_dir.parent))
        output_dir.rename(old / output_dir.name)
    staging.rename(output_dir)
    if old is not None:
        shutil.rmtree(old)


def run_audiveris(
    pdf: Path,
    output_dir: Path,
    *,
    audiveris: str | Path | None = None,
    timeout: float = 1800,
) -> AudiverisResult:
    """Run `Audiveris -batch -export -output <dir> -- <pdf>` and unzip every .mxl produced.

    output_dir is the per-item folder. It is created, or replaced when a previous run of
    this function created it. Raises AudiverisError when zero .mxl files are produced,
    whatever the exit code.
    """
    pdf = Path(pdf)
    output_dir = Path(output_dir)
    if not pdf.is_file():
        raise AudiverisError(f"PDF not found: {pdf}")
    exe = resolve_audiveris(audiveris)
    _check_replaceable(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        cmd = [str(exe), "-batch", "-export", "-output", str(staging), "--", str(pdf.resolve())]
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise AudiverisError(f"Audiveris timed out after {timeout}s on {pdf}") from None
        except OSError as exc:
            raise AudiverisError(f"cannot run Audiveris ({exe}): {exc}") from exc
        duration = time.monotonic() - start
        output = f"{proc.stdout}\n{proc.stderr}"

        mxl_files = sorted(staging.rglob("*.mxl"), key=_natural_key)
        if not mxl_files:
            raise AudiverisError(
                f"Audiveris produced no .mxl file for {pdf} (exit code {proc.returncode}); "
                f"last output:\n{_tail(output)}"
            )
        musicxml_files = [unzip_mxl(m) for m in mxl_files]

        (staging / LOG_NAME).write_text(
            f"$ {' '.join(cmd)}\nexit code: {proc.returncode}\n\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
            encoding="utf-8",
        )
        (staging / MARKER_NAME).touch()
        _swap_in(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    def moved(p: Path) -> Path:
        return output_dir / p.relative_to(staging)

    match = _VERSION_RE.search(output)
    return AudiverisResult(
        pdf=pdf,
        output_dir=output_dir,
        mxl_files=tuple(moved(p) for p in mxl_files),
        musicxml_files=tuple(moved(p) for p in musicxml_files),
        log_path=output_dir / LOG_NAME,
        version=match.group(1) if match else None,
        exit_code=proc.returncode,
        duration_s=duration,
    )
