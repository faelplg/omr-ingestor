"""Pipeline stages 2 and 4: validate MusicXML against a local XSD with xmllint.

The XSD is not shipped with this repository. Pass its path, or set OMR_MUSICXML_XSD.
xmllint runs with --nonet, so a schema that imports remote files (xml.xsd, xlink.xsd)
needs local copies next to it or an XML catalog (XML_CATALOG_FILES).
"""

import os
import shutil
import subprocess
from pathlib import Path

XSD_ENV_VAR = "OMR_MUSICXML_XSD"

# xmllint exit code for "schema failed to compile".
_SCHEMA_COMPILE_ERROR = 5


class XsdError(Exception):
    """Base class for XSD validation errors."""


class XsdSetupError(XsdError):
    """The validation could not run: no XSD, no xmllint, missing file, broken schema."""


class XsdValidationError(XsdError):
    """The file does not match the schema. `problems` holds one line per error."""

    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"{source}: not valid MusicXML ({len(problems)} problem(s)):\n{lines}")


def resolve_xsd_path(xsd_path: Path | None = None) -> Path:
    """The XSD to use: the explicit path, else $OMR_MUSICXML_XSD."""
    if xsd_path is None:
        env = os.environ.get(XSD_ENV_VAR, "").strip()
        if not env:
            raise XsdSetupError(
                f"no MusicXML XSD configured; pass xsd_path or set {XSD_ENV_VAR} "
                "to a local musicxml.xsd"
            )
        xsd_path = Path(env)
    xsd_path = Path(xsd_path).expanduser()
    if not xsd_path.is_file():
        raise XsdSetupError(f"XSD not found: {xsd_path}")
    return xsd_path


def _problems(stderr: str, source: str) -> list[str]:
    """Keep xmllint's `file:LINE: message` lines, rendered as `line LINE: message`."""
    prefix = f"{source}:"
    out = []
    for line in stderr.splitlines():
        if not line.startswith(prefix):
            continue
        lineno, _, message = line[len(prefix) :].partition(":")
        message = message.strip()
        # "element x: Schemas validity error : Element 'x': ..." -> keep the last part.
        if "Schemas validity error :" in message:
            message = message.split("Schemas validity error :", 1)[1].strip()
        out.append(f"line {lineno}: {message}")
    return out


def validate_xsd(
    xml_path: Path,
    xsd_path: Path | None = None,
    *,
    xmllint: str = "xmllint",
    timeout: float = 120,
) -> None:
    """Validate xml_path with `xmllint --schema`. Returns None when valid.

    Raises XsdValidationError when the file is invalid or not well-formed, and
    XsdSetupError when the validation itself cannot run.
    """
    xsd = resolve_xsd_path(xsd_path)
    if not Path(xml_path).is_file():
        raise XsdSetupError(f"MusicXML file not found: {xml_path}")
    exe = shutil.which(xmllint)
    if exe is None:
        raise XsdSetupError(f"xmllint not found ({xmllint!r}); it ships with libxml2")
    source = str(xml_path)
    try:
        proc = subprocess.run(
            [exe, "--noout", "--nonet", "--schema", str(xsd), source],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise XsdSetupError(f"xmllint timed out after {timeout}s on {source}") from None
    if proc.returncode == 0:
        return
    stderr = proc.stderr.strip()
    if proc.returncode == _SCHEMA_COMPILE_ERROR:
        raise XsdSetupError(f"XSD {xsd} failed to compile:\n{stderr}")
    problems = _problems(stderr, source) or [stderr or f"xmllint exited {proc.returncode}"]
    raise XsdValidationError(source, problems)
