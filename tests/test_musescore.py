import json
import zipfile
from pathlib import Path

import pytest

from omr_ingestor.musescore import (
    MUSESCORE_ENV_VAR,
    MuseScoreError,
    check_mscz,
    convert_to_mscz,
    resolve_musescore,
)
from tests.fake_tools import make_tool


def fake_musescore(tmp_path: Path, mode: str = "ok", *, exit_code: int = 0) -> Path:
    """A fake mscore. mode: ok, none, empty, notzip, nomscx."""
    return make_tool(
        tmp_path / "bin",
        "mscore",
        f"""
        import json, sys, zipfile
        from pathlib import Path
        args = sys.argv[1:]
        if args == ["--version"]:
            print("MuseScore4 4.7.5")
            sys.exit(0)
        Path(sys.argv[0]).with_suffix(".args").write_text(json.dumps(args))
        out = Path(args[args.index("-o") + 1])
        mode = {mode!r}
        if mode == "ok":
            with zipfile.ZipFile(out, "w") as zf:
                zf.writestr("score.mscx", "<museScore>" + args[-1] + "</museScore>")
        elif mode == "empty":
            out.write_bytes(b"")
        elif mode == "notzip":
            out.write_bytes(b"garbage")
        elif mode == "nomscx":
            with zipfile.ZipFile(out, "w") as zf:
                zf.writestr("readme.txt", "x")
        print("musescore stderr", file=sys.stderr)
        sys.exit({exit_code})
        """,
    )


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    xml = tmp_path / "in" / "flute.musicxml"
    xml.parent.mkdir()
    xml.write_text("<score-partwise/>")
    style = tmp_path / "in" / "Estilo 2026.mss"
    style.write_text("<museScore/>")
    return xml, style


def mscx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read("score.mscx").decode()


def test_converts_with_style(tmp_path: Path, inputs):
    xml, style = inputs
    exe = fake_musescore(tmp_path)
    out = tmp_path / "out" / "flute.mscz"
    result = convert_to_mscz(xml, style, out, musescore=exe)

    assert result.output == out
    assert result.version == "4.7.5"
    assert result.exit_code == 0
    assert result.duration_s >= 0
    assert "musescore stderr" in result.stderr
    assert mscx_text(out) == f"<museScore>{xml.resolve()}</museScore>"
    args = json.loads(exe.with_suffix(".args").read_text())
    assert args[:2] == ["-S", str(style.resolve())]
    assert args[-1] == str(xml.resolve())
    assert [p.name for p in out.parent.iterdir()] == ["flute.mscz"]


def test_exit_134_with_valid_file_is_success(tmp_path: Path, inputs):
    xml, style = inputs
    out = tmp_path / "flute.mscz"
    result = convert_to_mscz(xml, style, out, musescore=fake_musescore(tmp_path, exit_code=134))
    assert result.exit_code == 134
    check_mscz(out)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("none", "was not created"),
        ("empty", "is empty"),
        ("notzip", "not a valid zip"),
        ("nomscx", r"no \.mscx inside"),
    ],
)
def test_bad_output_fails_even_with_exit_code_zero(tmp_path: Path, inputs, mode, message):
    xml, style = inputs
    out = tmp_path / "out" / "flute.mscz"
    with pytest.raises(MuseScoreError, match=rf"exit code 0\): .*{message}"):
        convert_to_mscz(xml, style, out, musescore=fake_musescore(tmp_path, mode))
    assert list(out.parent.iterdir()) == []


def test_rerun_overwrites_and_failure_keeps_previous(tmp_path: Path, inputs):
    xml, style = inputs
    out = tmp_path / "flute.mscz"
    out.write_bytes(b"stale")
    convert_to_mscz(xml, style, out, musescore=fake_musescore(tmp_path))
    good = out.read_bytes()
    check_mscz(out)

    with pytest.raises(MuseScoreError):
        convert_to_mscz(xml, style, out, musescore=fake_musescore(tmp_path, "notzip"))
    assert out.read_bytes() == good


def test_missing_inputs(tmp_path: Path, inputs):
    xml, style = inputs
    exe = fake_musescore(tmp_path)
    with pytest.raises(MuseScoreError, match="MusicXML not found"):
        convert_to_mscz(tmp_path / "nope.xml", style, tmp_path / "o.mscz", musescore=exe)
    with pytest.raises(MuseScoreError, match="style file not found"):
        convert_to_mscz(xml, tmp_path / "nope.mss", tmp_path / "o.mscz", musescore=exe)
    with pytest.raises(MuseScoreError, match=r"must end in \.mscz"):
        convert_to_mscz(xml, style, tmp_path / "o.mscx", musescore=exe)


def test_binary_from_env(tmp_path: Path, inputs, monkeypatch: pytest.MonkeyPatch):
    xml, style = inputs
    monkeypatch.setenv(MUSESCORE_ENV_VAR, str(fake_musescore(tmp_path)))
    assert convert_to_mscz(xml, style, tmp_path / "o.mscz").exit_code == 0


def test_binary_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MUSESCORE_ENV_VAR, raising=False)
    with pytest.raises(MuseScoreError, match=MUSESCORE_ENV_VAR):
        resolve_musescore()


def test_relative_binary_resolves_to_absolute_path(
    tmp_path: Path, inputs, monkeypatch: pytest.MonkeyPatch
):
    xml, style = inputs
    exe = fake_musescore(tmp_path)
    monkeypatch.chdir(exe.parent)
    assert resolve_musescore("./mscore") == exe
    assert resolve_musescore("./mscore").is_absolute()
    result = convert_to_mscz(xml, style, tmp_path / "o.mscz", musescore="./mscore")
    assert result.version == "4.7.5"
    check_mscz(result.output)
