import os
import shutil
from pathlib import Path

import pytest

from omr_ingestor.fixup import fix_instrument_file
from omr_ingestor.instruments import load_instruments
from omr_ingestor.xsd import (
    XSD_ENV_VAR,
    XsdSetupError,
    XsdValidationError,
    resolve_xsd_path,
    validate_xsd,
)
from tests.musicxml_builders import DEFAULT_ATTRS, measure, note, score
from tests.test_fixup import omr_score

needs_xmllint = pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint not installed")
needs_musicxml_xsd = pytest.mark.skipif(
    not os.environ.get(XSD_ENV_VAR), reason=f"set {XSD_ENV_VAR} to a local MusicXML 4.0 XSD"
)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_no_xsd_configured(monkeypatch):
    monkeypatch.delenv(XSD_ENV_VAR, raising=False)
    with pytest.raises(XsdSetupError, match=rf"no MusicXML XSD configured.*{XSD_ENV_VAR}"):
        resolve_xsd_path()


def test_xsd_path_from_env_must_exist(monkeypatch, tmp_path):
    monkeypatch.setenv(XSD_ENV_VAR, str(tmp_path / "missing.xsd"))
    with pytest.raises(XsdSetupError, match=r"XSD not found: .*missing\.xsd"):
        resolve_xsd_path()


# The wrapper itself (exit codes, error parsing) is checked against a tiny local XSD,
# so it runs even without the MusicXML XSD.
TINY_XSD = (
    '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"><xs:element name="a">'
    '<xs:complexType><xs:sequence><xs:element name="b" type="xs:int"/></xs:sequence>'
    "</xs:complexType></xs:element></xs:schema>"
)


@needs_xmllint
def test_wrapper_reports_each_error_with_its_line(tmp_path):
    xsd = write(tmp_path, "tiny.xsd", TINY_XSD)
    validate_xsd(write(tmp_path, "ok.xml", "<a><b>1</b></a>"), xsd)
    bad = write(tmp_path, "bad.xml", "<a>\n<b>x</b>\n<c/></a>\n")
    with pytest.raises(XsdValidationError) as info:
        validate_xsd(bad, xsd)
    assert info.value.problems == [
        "line 2: Element 'b': 'x' is not a valid value of the atomic type 'xs:int'.",
        "line 3: Element 'c': This element is not expected.",
    ]
    assert str(info.value).startswith(f"{bad}: not valid MusicXML (2 problem(s)):")


def test_missing_xmllint(tmp_path):
    xsd = write(tmp_path, "tiny.xsd", TINY_XSD)
    xml = write(tmp_path, "a.xml", "<a/>")
    with pytest.raises(XsdSetupError, match=r"xmllint not found \('no-such-xmllint'\)"):
        validate_xsd(xml, xsd, xmllint="no-such-xmllint")


@needs_xmllint
def test_wrapper_reports_malformed_xml_and_broken_schema(tmp_path):
    xsd = write(tmp_path, "tiny.xsd", TINY_XSD)
    with pytest.raises(XsdValidationError, match=r"line 1: parser error : Premature end"):
        validate_xsd(write(tmp_path, "broken.xml", "<a><b>"), xsd)
    with pytest.raises(XsdSetupError, match=r"XSD .*bad\.xsd failed to compile"):
        validate_xsd(write(tmp_path, "ok.xml", "<a/>"), write(tmp_path, "bad.xsd", "<xs:schema"))
    with pytest.raises(XsdSetupError, match=r"MusicXML file not found"):
        validate_xsd(tmp_path / "nope.xml", xsd)


@needs_xmllint
@needs_musicxml_xsd
def test_synthetic_score_is_valid_musicxml(tmp_path):
    xml = score({"P1": [measure(1, DEFAULT_ATTRS + note(1) * 4)]})
    validate_xsd(write(tmp_path, "s.musicxml", xml))


@needs_xmllint
@needs_musicxml_xsd
def test_invalid_musicxml_is_rejected(tmp_path):
    xml = score({"P1": [measure(1, DEFAULT_ATTRS + "<bogus/>")]})
    with pytest.raises(XsdValidationError, match=r"Element 'bogus'"):
        validate_xsd(write(tmp_path, "s.musicxml", xml))


@needs_xmllint
@needs_musicxml_xsd
@pytest.mark.parametrize("instrument_id", [i.id for i in load_instruments()])
def test_fixup_output_is_valid_musicxml(tmp_path, instrument_id):
    src = write(tmp_path, "omr.musicxml", omr_score())
    validate_xsd(src)
    dst = tmp_path / "fixed.musicxml"
    fix_instrument_file(src, dst, load_instruments()[instrument_id], suspect_measures=["2"])
    validate_xsd(dst)
