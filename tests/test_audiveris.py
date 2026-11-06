import json
import zipfile
from pathlib import Path

import pytest

from omr_ingestor.audiveris import (
    AUDIVERIS_ENV_VAR,
    LOG_NAME,
    MARKER_NAME,
    AudiverisError,
    resolve_audiveris,
    run_audiveris,
    unzip_mxl,
)
from tests.fake_tools import make_tool

CONTAINER = (
    '<?xml version="1.0" encoding="UTF-8"?><container><rootfiles>'
    '<rootfile full-path="{root}" media-type="application/vnd.recordare.musicxml+xml"/>'
    "</rootfiles></container>"
)


def write_mxl(path: Path, files: dict[str, str], *, root: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        if root is not None:
            zf.writestr("META-INF/container.xml", CONTAINER.format(root=root))
        for name, content in files.items():
            zf.writestr(name, content)
    return path


def fake_audiveris(
    tmp_path: Path,
    mxl_names: list[str],
    *,
    exit_code: int = 0,
    subfolder: bool = False,
    corrupt: bool = False,
    print_version: bool = True,
    encoding: str = "",
) -> Path:
    """A fake Audiveris that writes one .mxl per name ({stem} is the PDF stem)."""
    plan = {
        "names": mxl_names,
        "exit": exit_code,
        "subfolder": subfolder,
        "corrupt": corrupt,
        "print_version": print_version,
        "encoding": encoding,
    }
    return make_tool(
        tmp_path / "bin",
        "Audiveris",
        f"""
        import json, sys, zipfile
        from pathlib import Path
        plan = json.loads({json.dumps(plan)!r})
        args = sys.argv[1:]
        Path(sys.argv[0]).with_suffix(".args").write_text(json.dumps(args))
        out = Path(args[args.index("-output") + 1])
        pdf = Path(args[args.index("--") + 1])
        if plan["print_version"]:
            print("INFO  Audiveris version 5.11.0")
        print("some error", file=sys.stderr)
        book = out / pdf.stem if plan["subfolder"] else out
        book.mkdir(parents=True, exist_ok=True)
        (book / (pdf.stem + ".omr")).write_bytes(b"omr")
        for name in plan["names"]:
            name = name.format(stem=pdf.stem)
            if plan["corrupt"]:
                (book / name).write_bytes(b"not a zip")
                continue
            with zipfile.ZipFile(book / name, "w") as zf:
                zf.writestr("META-INF/container.xml", {CONTAINER!r}.format(root="score.xml"))
                zf.writestr(
                    "score.xml", "<score-partwise>" + plan["encoding"] + name + "</score-partwise>"
                )
        sys.exit(plan["exit"])
        """,
    )


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "in" / "flute part.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.4")
    return path


def test_single_movement(tmp_path: Path, pdf: Path):
    exe = fake_audiveris(tmp_path, ["{stem}.mxl"])
    out = tmp_path / "items" / "flute"
    result = run_audiveris(pdf, out, audiveris=exe)

    assert result.output_dir == out
    assert result.mxl_files == (out / "flute part.mxl",)
    assert result.musicxml_files == (out / "flute part.musicxml",)
    assert result.musicxml_files[0].read_text() == "<score-partwise>flute part.mxl</score-partwise>"
    assert result.version == "5.11.0"
    assert result.exit_code == 0
    assert result.duration_s >= 0
    assert "some error" in result.log_path.read_text()
    assert result.log_path == out / LOG_NAME
    args = json.loads(exe.with_suffix(".args").read_text())
    assert args[:3] == ["-batch", "-export", "-output"]
    assert args[4:-2] == []
    assert args[-2:] == ["--", str(pdf.resolve())]
    # Staging folders are gone.
    assert sorted(p.name for p in out.parent.iterdir()) == ["flute"]


def test_constant_options_are_separate_arguments_and_logged(tmp_path: Path, pdf: Path):
    exe = fake_audiveris(tmp_path, ["{stem}.mxl"])
    options = {
        "org.audiveris.omr.sheet.grid.StaffProjector.staffAbscissaMargin": "1.0",
        "org.audiveris.omr.image.ImageLoading.pdfResolution": "600",
    }
    result = run_audiveris(pdf, tmp_path / "out", audiveris=exe, options=options)
    args = json.loads(exe.with_suffix(".args").read_text())
    assert args[4:-2] == [
        "-option", "org.audiveris.omr.image.ImageLoading.pdfResolution=600",
        "-option", "org.audiveris.omr.sheet.grid.StaffProjector.staffAbscissaMargin=1.0",
    ]
    for key, value in options.items():
        assert f"{key}={value}" in result.log_path.read_text()


@pytest.mark.parametrize("options", [
    {"-run": "SomeClass"},
    {"org.audiveris.omr.image.ImageLoading.pdfResolution": "600\nother=true"},
    {"org.audiveris.omr.image.ImageLoading.pdfResolution": r"600\nother=true"},
])
def test_invalid_constant_options_fail_before_output_creation(tmp_path: Path, pdf: Path, options):
    out = tmp_path / "out"
    with pytest.raises(AudiverisError, match="invalid Audiveris constant"):
        run_audiveris(pdf, out, audiveris=tmp_path / "missing", options=options)
    assert not out.exists()


def test_version_comes_from_the_musicxml_signature(tmp_path: Path, pdf: Path):
    # Audiveris 5.11 prints no version in batch mode; the export names it.
    encoding = (
        "<identification><encoding><software>Audiveris 5.11.0</software>"
        "<software>ProxyMusic 4.0.3</software></encoding></identification>"
    )
    exe = fake_audiveris(tmp_path, ["{stem}.mxl"], print_version=False, encoding=encoding)
    assert run_audiveris(pdf, tmp_path / "out", audiveris=exe).version == "5.11.0"

    exe = fake_audiveris(tmp_path, ["{stem}.mxl"], print_version=False)
    assert run_audiveris(pdf, tmp_path / "out", audiveris=exe).version is None


def test_several_movements_in_subfolder_are_sorted_naturally(tmp_path: Path, pdf: Path):
    names = ["{stem}.mvt10.mxl", "{stem}.mvt2.mxl", "{stem}.mvt1.mxl"]
    exe = fake_audiveris(tmp_path, names, subfolder=True)
    out = tmp_path / "out"
    result = run_audiveris(pdf, out, audiveris=exe)

    assert [p.name for p in result.mxl_files] == [
        "flute part.mvt1.mxl",
        "flute part.mvt2.mxl",
        "flute part.mvt10.mxl",
    ]
    assert [p.parent for p in result.musicxml_files] == [out / "flute part"] * 3
    for mxl, xml in zip(result.mxl_files, result.musicxml_files, strict=True):
        assert xml.read_text() == f"<score-partwise>{mxl.name}</score-partwise>"


def test_zero_mxl_fails_even_with_exit_code_zero(tmp_path: Path, pdf: Path):
    exe = fake_audiveris(tmp_path, [], exit_code=0)
    out = tmp_path / "out"
    with pytest.raises(AudiverisError, match=r"(?s)no \.mxl file .*exit code 0.*some error"):
        run_audiveris(pdf, out, audiveris=exe)
    assert list(tmp_path.glob("*out*")) == []


def test_nonzero_exit_with_mxl_succeeds_and_reports_exit_code(tmp_path: Path, pdf: Path):
    exe = fake_audiveris(tmp_path, ["{stem}.mxl"], exit_code=1)
    result = run_audiveris(pdf, tmp_path / "out", audiveris=exe)
    assert result.exit_code == 1
    assert len(result.musicxml_files) == 1


def test_corrupt_mxl_fails_and_cleans_staging(tmp_path: Path, pdf: Path):
    exe = fake_audiveris(tmp_path, ["{stem}.mxl"], corrupt=True)
    with pytest.raises(AudiverisError, match=r"not a readable \.mxl"):
        run_audiveris(pdf, tmp_path / "out", audiveris=exe)
    assert list(tmp_path.glob("*out*")) == []


def test_rerun_replaces_previous_output(tmp_path: Path, pdf: Path):
    out = tmp_path / "out"
    run_audiveris(pdf, out, audiveris=fake_audiveris(tmp_path, ["{stem}.mvt1.mxl", "x.mxl"]))
    assert (out / "x.musicxml").exists()
    assert (out / MARKER_NAME).exists()

    result = run_audiveris(pdf, out, audiveris=fake_audiveris(tmp_path, ["{stem}.mvt1.mxl"]))
    assert result.mxl_files == (out / "flute part.mvt1.mxl",)
    assert not (out / "x.musicxml").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin", "in", "out"]


def test_failed_rerun_keeps_previous_output(tmp_path: Path, pdf: Path):
    out = tmp_path / "out"
    first = run_audiveris(pdf, out, audiveris=fake_audiveris(tmp_path, ["{stem}.mxl"]))
    with pytest.raises(AudiverisError):
        run_audiveris(pdf, out, audiveris=fake_audiveris(tmp_path, []))
    assert first.musicxml_files[0].exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin", "in", "out"]


def test_existing_empty_folder_is_reused(tmp_path: Path, pdf: Path):
    out = tmp_path / "out"
    out.mkdir()
    result = run_audiveris(pdf, out, audiveris=fake_audiveris(tmp_path, ["{stem}.mxl"]))
    assert result.musicxml_files[0].exists()


def test_refuses_to_replace_foreign_folder(tmp_path: Path, pdf: Path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    with pytest.raises(AudiverisError, match="refusing to replace"):
        run_audiveris(pdf, out, audiveris=fake_audiveris(tmp_path, ["{stem}.mxl"]))
    assert (out / "notes.txt").read_text() == "mine"


def test_missing_pdf(tmp_path: Path):
    with pytest.raises(AudiverisError, match="PDF not found"):
        run_audiveris(tmp_path / "nope.pdf", tmp_path / "out", audiveris=tmp_path)


def test_binary_from_env(tmp_path: Path, pdf: Path, monkeypatch: pytest.MonkeyPatch):
    exe = fake_audiveris(tmp_path, ["{stem}.mxl"])
    monkeypatch.setenv(AUDIVERIS_ENV_VAR, str(exe))
    assert resolve_audiveris() == exe
    assert run_audiveris(pdf, tmp_path / "out").exit_code == 0


def test_binary_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(AUDIVERIS_ENV_VAR, raising=False)
    with pytest.raises(AudiverisError, match=AUDIVERIS_ENV_VAR):
        resolve_audiveris()


def test_binary_not_executable(tmp_path: Path):
    path = tmp_path / "Audiveris"
    path.write_text("")
    with pytest.raises(AudiverisError, match="not executable"):
        resolve_audiveris(path)


def test_unzip_uses_rootfile_from_container(tmp_path: Path):
    mxl = write_mxl(
        tmp_path / "a.mxl",
        {"other.xml": "<other/>", "dir/real.xml": "<score-partwise/>"},
        root="dir/real.xml",
    )
    assert unzip_mxl(mxl).read_text() == "<score-partwise/>"
    assert (tmp_path / "a.musicxml").exists()


def test_unzip_without_container_takes_single_xml(tmp_path: Path):
    mxl = write_mxl(tmp_path / "a.mxl", {"score.musicxml": "<score-partwise/>"})
    assert unzip_mxl(mxl, tmp_path / "b.xml").read_text() == "<score-partwise/>"


def test_unzip_without_container_and_several_xml_fails(tmp_path: Path):
    mxl = write_mxl(tmp_path / "a.mxl", {"a.xml": "<a/>", "b.xml": "<b/>"})
    with pytest.raises(AudiverisError, match="2 XML candidates"):
        unzip_mxl(mxl)


def test_unzip_missing_root_member_fails(tmp_path: Path):
    mxl = write_mxl(tmp_path / "a.mxl", {"a.xml": "<a/>"}, root="missing.xml")
    with pytest.raises(AudiverisError, match="missing from the archive"):
        unzip_mxl(mxl)


def test_relative_binary_resolves_to_absolute_path(
    tmp_path: Path, pdf: Path, monkeypatch: pytest.MonkeyPatch
):
    exe = fake_audiveris(tmp_path, ["{stem}.mxl"])
    monkeypatch.chdir(exe.parent)
    assert resolve_audiveris("./Audiveris") == exe
    assert resolve_audiveris("./Audiveris").is_absolute()
    result = run_audiveris(pdf, tmp_path / "out", audiveris="./Audiveris")
    assert result.musicxml_files[0].exists()
