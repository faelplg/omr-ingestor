import re
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from omr_ingestor.instruments import Transpose, load_instruments
from omr_ingestor.mscx_check import MscxCheckError, check_mscz
from tests.mscx_builders import instrument_mscx, mscx_xml, part_xml, style_xml, two_trumpet_mscx

INSTRUMENTS = load_instruments()
CHECK_NAMES = ["part_count", "instrument", "part_name", "transposition", "clef", "page_size"]


def write_mscz(
    path: Path,
    mscx: str,
    *,
    style: str | None = None,
    container: bool = True,
    extra: dict[str, str] | None = None,
    name: str = "score.mscx",
) -> Path:
    files = {"score_style.mss": style_xml() if style is None else style, name: mscx}
    if style == "":
        del files["score_style.mss"]
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        for member, text in files.items():
            zf.writestr(member, text)
        if container:
            rootfiles = "".join(f'<rootfile full-path="{m}"/>' for m in files)
            zf.writestr(
                "META-INF/container.xml",
                f"<container><rootfiles>{rootfiles}</rootfiles></container>",
            )
    return path


def mscz_for(tmp_path: Path, inst_id: str, **kwargs) -> Path:
    """A .mscz as MuseScore 4.7.5 writes it for an instrument from instruments.json."""
    return write_mscz(tmp_path / f"{inst_id}.mscz", instrument_mscx(inst_id), **kwargs)


def by_name(result) -> dict:
    return {c.name: c for c in result.checks}


@pytest.mark.parametrize("inst_id", INSTRUMENTS.ids)
def test_matching_file_passes_every_check(tmp_path, inst_id):
    result = check_mscz(mscz_for(tmp_path, inst_id), INSTRUMENTS[inst_id])
    assert [c.name for c in result.checks] == CHECK_NAMES
    assert result.passed, [c.describe() for c in result.failures]
    assert result.mscx == "score.mscx"


def test_wrong_instrument_reports_found_and_expected(tmp_path):
    result = check_mscz(mscz_for(tmp_path, "tuba"), INSTRUMENTS["bb-trumpet"])
    checks = by_name(result)
    assert not result.passed
    assert [c.name for c in result.failures] == ["instrument", "part_name", "transposition", "clef"]
    assert (checks["instrument"].found, checks["instrument"].expected) == ("tuba", "bb-trumpet")
    assert checks["transposition"].found == "diatonic 0, chromatic 0"
    assert checks["transposition"].expected == "diatonic -1, chromatic -2"
    assert (checks["clef"].found, checks["clef"].expected) == ("F4", "G2")
    assert checks["page_size"].passed


def test_octave_change_is_folded_into_the_transposition(tmp_path):
    tenor = INSTRUMENTS["tenor-saxophone"]
    assert by_name(check_mscz(mscz_for(tmp_path, tenor.id), tenor))["transposition"].passed
    without_octave = replace(tenor, transpose=Transpose(-1, -2, 0))
    check = by_name(check_mscz(mscz_for(tmp_path, tenor.id), without_octave))["transposition"]
    assert not check.passed
    assert check.found == "diatonic -8, chromatic -14"


def test_written_clef_is_checked_not_the_concert_clef(tmp_path):
    # Tenor sax as MuseScore writes it: concert clef G8vb, written clef G.
    part = part_xml(
        "tenor-saxophone",
        "Sax tenor em Sib",
        -8,
        -14,
        "<concertClef>G8vb</concertClef><transposingClef>G</transposingClef>",
    )
    path = write_mscz(tmp_path / "t.mscz", mscx_xml(part, clef=None))
    assert check_mscz(path, INSTRUMENTS["tenor-saxophone"]).passed


def test_wrong_header_clef_fails(tmp_path):
    inst = INSTRUMENTS["trombone"]
    path = write_mscz(tmp_path / "t.mscz", mscx_xml(part_xml(inst.id, inst.name_pt), clef="G"))
    clef = by_name(check_mscz(path, inst))["clef"]
    assert (clef.passed, clef.found, clef.expected) == (False, "G2", "F4")


@pytest.mark.parametrize(
    ("style", "found"),
    [
        (style_xml("8.27", "11.69"), "210.1 x 296.9 mm"),  # A4 portrait
        (style_xml("5.83", "8.27"), "148.1 x 210.1 mm"),  # A5 portrait
        ("", "no score_style.mss"),
        (style_xml("abc"), "pageWidth 'abc'"),
    ],
)
def test_page_size_must_be_a5_landscape(tmp_path, style, found):
    path = mscz_for(tmp_path, "flute", style=style)
    page = by_name(check_mscz(path, INSTRUMENTS["flute"]))["page_size"]
    assert (page.passed, page.found) == (False, found)
    assert page.expected == "A5 landscape (210 x 148 mm)"


def test_main_mscx_found_next_to_excerpts(tmp_path):
    inst = INSTRUMENTS["flute"]
    excerpt = mscx_xml(part_xml("tuba", "Tuba"), "F")
    extra = {"Excerpts/0_Tuba/0_Tuba.mscx": excerpt, "Excerpts/0_Tuba/0_Tuba.mss": style_xml()}
    for container in (True, False):
        path = write_mscz(
            tmp_path / f"{container}.mscz",
            mscx_xml(part_xml(inst.id, inst.name_pt)),
            container=container,
            extra=extra,
            name="[01] Song.mscx",
        )
        result = check_mscz(path, inst)
        assert result.passed
        assert result.mscx == "[01] Song.mscx"


def test_more_than_one_part_fails_part_count(tmp_path):
    inst = INSTRUMENTS["flute"]
    two = part_xml(inst.id, inst.name_pt) * 2
    result = check_mscz(write_mscz(tmp_path / "t.mscz", mscx_xml(two)), inst)
    assert [c.name for c in result.failures] == ["part_count"]


def test_no_part_fails_every_instrument_check(tmp_path):
    result = check_mscz(write_mscz(tmp_path / "t.mscz", mscx_xml("")), INSTRUMENTS["flute"])
    assert {c.name for c in result.failures} == set(CHECK_NAMES) - {"page_size"}
    assert by_name(result)["instrument"].found == "no <Part>"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, r"cannot open as a \.mscz"),
        ("broken", "not well-formed XML"),
        ("<museScore/>", "no <Score>"),
    ],
)
def test_unreadable_files_raise(tmp_path, content, message):
    path = tmp_path / "bad.mscz"
    if content is None:
        path.write_text("not a zip", encoding="utf-8")
    else:
        write_mscz(path, content)
    with pytest.raises(MscxCheckError, match=message):
        check_mscz(path, INSTRUMENTS["flute"])


def test_zip_without_mscx_raises(tmp_path):
    path = tmp_path / "empty.mscz"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("score_style.mss", style_xml())
    with pytest.raises(MscxCheckError, match=r"expected one top-level \.mscx, found 0"):
        check_mscz(path, INSTRUMENTS["flute"])


@pytest.mark.parametrize("member", ["score.mscx", "score_style.mss", "META-INF/container.xml"])
def test_corrupt_member_raises(tmp_path, member):
    path = mscz_for(tmp_path, "flute")
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)
    data = bytearray(path.read_bytes())
    # Flip one byte of the stored (uncompressed) data; the zip directory stays intact.
    data_start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    data[data_start] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(MscxCheckError, match=f"{re.escape(member)}: cannot read zip member"):
        check_mscz(path, INSTRUMENTS["flute"])


@pytest.mark.parametrize(
    ("before", "after", "failure"),
    [
        ("Trumpet 2", "Wrong name", "part_name"),
        ('id="bb-trumpet"', 'id="flute"', "instrument"),
        ("<transposeChromatic>-2", "<transposeChromatic>0", "transposition"),
        ("<transposingClefType>G", "<transposingClefType>F", "clef"),
    ],
)
def test_each_multipart_property_is_checked(tmp_path, before, after, failure):
    xml = two_trumpet_mscx()
    # Change only the second occurrence for properties shared by both parts.
    at = xml.rfind(before)
    assert at >= 0
    xml = xml[:at] + xml[at:].replace(before, after, 1)
    result = check_mscz(
        write_mscz(tmp_path / "two.mscz", xml),
        INSTRUMENTS["bb-trumpet"],
        parts=("Trumpet 1", "Trumpet 2"),
    )
    assert [c.name for c in result.failures] == [f"part[2].{failure}"]


def test_multipart_final_count_mismatch(tmp_path):
    result = check_mscz(
        mscz_for(tmp_path, "bb-trumpet"),
        INSTRUMENTS["bb-trumpet"],
        parts=(INSTRUMENTS["bb-trumpet"].name_pt, "Trumpet 2"),
    )
    assert not result.passed
    assert by_name(result)["part_count"].found == "1"
    assert by_name(result)["part_count"].expected == "2"
    assert by_name(result)["part[2].instrument"].found == "no <Part>"
