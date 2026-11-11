"""Bundled Tesseract lookup. No Windows executable is required."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_fetch():
    path = ROOT / "scripts" / "fetch_tesseract_runtime.py"
    spec = importlib.util.spec_from_file_location("panelcad_fetch_tesseract_runtime", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bundle(root: Path) -> tuple[Path, Path]:
    runtime = root / "resources" / "ocr" / "tesseract"
    tessdata = runtime / "tessdata"
    tessdata.mkdir(parents=True)
    executable = runtime / "tesseract.exe"
    executable.write_bytes(b"bundle")
    (tessdata / "eng.traineddata").write_bytes(b"eng")
    return executable, tessdata


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.delenv("PANELCAD_TESSERACT", raising=False)


def test_bundled_binary_beats_a_system_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.importer import tesseract_runtime as runtime

    executable, _tessdata = _bundle(tmp_path)
    system = tmp_path / "Program Files" / "Tesseract-OCR" / "tesseract.exe"
    system.parent.mkdir(parents=True)
    system.write_bytes(b"system")
    monkeypatch.setattr(runtime, "install_root", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_system_tesseract_candidates", lambda: [str(system)])
    _clear_env(monkeypatch)

    assert runtime.find_tesseract() == str(executable)


def test_env_override_beats_the_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.importer import tesseract_runtime as runtime

    _bundle(tmp_path)
    override = tmp_path / "override-tesseract.exe"
    override.write_bytes(b"override")
    monkeypatch.setattr(runtime, "install_root", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_system_tesseract_candidates", lambda: [])
    _clear_env(monkeypatch)
    monkeypatch.setenv("PANELCAD_TESSERACT", f'"{override}"')

    assert runtime.find_tesseract() == str(override)

    command = tmp_path / "command-tesseract.exe"
    command.write_bytes(b"command")
    monkeypatch.setenv("TESSERACT_CMD", str(command))
    assert runtime.find_tesseract() == str(command)


def test_missing_bundle_and_system_binary_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.importer import tesseract_runtime as runtime
    from panelcad.importer.raster import TESSERACT_MESSAGE, TesseractMissing, require_tesseract

    monkeypatch.setattr(runtime, "install_root", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_system_tesseract_candidates", lambda: [])
    _clear_env(monkeypatch)

    assert runtime.find_tesseract() == ""
    with pytest.raises(TesseractMissing, match="github.com/tesseract-ocr/tesseract") as raised:
        require_tesseract()
    assert TESSERACT_MESSAGE in str(raised.value)
    assert "https://tesseract-ocr.github.io/tessdoc/Downloads.html" in str(raised.value)


def test_bundled_tessdata_dir_is_passed_as_its_own_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pytesseract

    from panelcad.importer import tesseract_runtime as runtime

    root = tmp_path / "Ada Lovelace" / "PanelCAD"
    executable, tessdata = _bundle(root)
    system = tmp_path / "system-tesseract.exe"
    system.write_bytes(b"system")
    monkeypatch.setattr(runtime, "install_root", lambda: root)
    monkeypatch.setattr(runtime, "_system_tesseract_candidates", lambda: [str(system)])
    _clear_env(monkeypatch)

    assert runtime.configure_tesseract() == str(executable)
    captured: list[list[str]] = []

    def fake_popen(cmd, *_args, **_kwargs):
        captured.append(list(cmd))
        raise RuntimeError("stop-ocr")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with pytest.raises(RuntimeError, match="stop-ocr"):
        pytesseract.pytesseract.run_tesseract("in.png", "out", "txt", "eng", config="--psm 11")

    command = captured[0]
    flag = command.index("--tessdata-dir")
    assert command[flag + 1] == str(tessdata)
    assert '"' not in command[flag + 1]
    assert " " in command[flag + 1]
    assert command[-2:] == ["--tessdata-dir", str(tessdata)]


def test_system_binary_does_not_force_bundled_tessdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pytesseract

    from panelcad.importer import tesseract_runtime as runtime

    _bundle(tmp_path)
    system = tmp_path / "system-tesseract.exe"
    system.write_bytes(b"system")
    monkeypatch.setattr(runtime, "install_root", lambda: tmp_path)
    monkeypatch.setattr(runtime, "_system_tesseract_candidates", lambda: [str(system)])
    _clear_env(monkeypatch)
    monkeypatch.setenv("TESSERACT_CMD", str(system))

    assert runtime.configure_tesseract() == str(system)
    captured: list[list[str]] = []

    def fake_popen(cmd, *_args, **_kwargs):
        captured.append(list(cmd))
        raise RuntimeError("stop-ocr")

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    with pytest.raises(RuntimeError, match="stop-ocr"):
        pytesseract.pytesseract.run_tesseract("in.png", "out", "txt", "eng", config="--psm 6")

    assert "--tessdata-dir" not in captured[0]


def test_notice_names_every_locked_binary() -> None:
    lock_path = ROOT / "packaging" / "tesseract-runtime.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    notice = (ROOT / "packaging" / "tesseract" / "NOTICE.md").read_text(encoding="utf-8")
    assert lock["tesseract_version"] == "5.5.3"
    installer_sha = "bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4"
    assert lock["installer"]["sha256"] == installer_sha
    assert lock["tessdata_fast"]["bytes"] == 4_113_088
    assert "tessdata_best" in notice
    assert "osd.traineddata" in notice
    names = [Path(item["path"]).name for item in lock["files"]]
    assert "osd.traineddata" not in names
    assert "eng.traineddata" in names
    assert "tsv" in names
    for item in lock["files"]:
        name = Path(item["path"]).name
        if name.endswith(".dll") or name == "tesseract.exe":
            assert name in notice, name
    assert "Apache-2.0" in notice
    assert "Leptonica" in notice
    assert "tessdata_fast" in notice


def test_assemble_runtime_checks_hashes(tmp_path: Path) -> None:
    fetch = _load_fetch()
    payload = b"tsv\n"
    extracted = tmp_path / "extracted"
    (extracted / "tessdata" / "configs").mkdir(parents=True)
    (extracted / "tessdata" / "configs" / "tsv").write_bytes(payload)
    eng = tmp_path / "eng.traineddata"
    eng.write_bytes(b"eng-model")
    notice = tmp_path / "NOTICE.md"
    notice.write_text("notice\n", encoding="utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    eng_digest = hashlib.sha256(b"eng-model").hexdigest()
    lock = {
        "files": [
            {
                "path": "tessdata/configs/tsv",
                "sha256": digest,
                "bytes": len(payload),
                "source": "installer",
                "installer_path": "tessdata/configs/tsv",
            },
            {
                "path": "tessdata/eng.traineddata",
                "sha256": eng_digest,
                "bytes": len(b"eng-model"),
                "source": "tessdata_fast",
            },
        ]
    }
    dest = tmp_path / "runtime"
    fetch.assemble_runtime(lock, extracted=extracted, eng_file=eng, notice=notice, dest=dest)
    assert (dest / "tessdata" / "configs" / "tsv").read_bytes() == payload
    assert (dest / "tessdata" / "eng.traineddata").read_bytes() == b"eng-model"
    assert (dest / "licenses" / "NOTICE.md").read_text(encoding="utf-8") == "notice\n"

    bad = dict(lock)
    bad["files"] = [dict(lock["files"][0], sha256="0" * 64)]
    with pytest.raises(SystemExit, match="sha256"):
        fetch.assemble_runtime(
            bad,
            extracted=extracted,
            eng_file=eng,
            notice=notice,
            dest=tmp_path / "rejected",
        )
