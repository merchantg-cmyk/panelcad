from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from panelcad.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def _load_electrical_fetch():
    import importlib.util

    path = ROOT / "scripts" / "fetch_electrical_ocr.py"
    spec = importlib.util.spec_from_file_location("panelcad_fetch_electrical_ocr_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_build_release():
    import importlib.util

    path = ROOT / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("panelcad_build_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pyproject_version_matches_version_py():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.M)
    assert match, "pyproject.toml must declare version"
    assert match.group(1) == __version__


def test_build_release_writes_one_versioned_zip_and_sha256(tmp_path: Path):
    build = _load_build_release()
    archive, checksum = build.build_release(ROOT, tmp_path)

    assert archive.name == f"PanelCAD-{__version__}-setup.zip"
    assert checksum.name == f"PanelCAD-{__version__}-setup.zip.sha256"
    assert archive.is_file()
    assert checksum.is_file()
    assert archive.stat().st_size > 10_000

    recorded, filename = checksum.read_text(encoding="ascii").strip().split()
    assert filename == archive.name
    assert recorded == hashlib.sha256(archive.read_bytes()).hexdigest()

    prefix = f"PanelCAD-{__version__}/"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert f"{prefix}INSTALL.txt" in names
    assert f"{prefix}Install-PanelCAD.cmd" in names
    assert f"{prefix}scripts/Install-PanelCAD.ps1" in names
    assert f"{prefix}src/panelcad/version.py" in names
    assert f"{prefix}pyproject.toml" in names
    assert f"{prefix}scripts/check" in names
    assert f"{prefix}scripts/build-release.py" in names
    assert f"{prefix}scripts/install_tree.py" in names
    assert f"{prefix}CHANGELOG.md" in names
    for rel in build.BRAND_ASSETS:
        assert f"{prefix}{rel}" in names

    banned = [
        name
        for name in names
        if name.endswith("PanelCAD-Setup.zip")
        or name.endswith("PanelCAD-Setup.tar.gz")
        or "/.git/" in name
        or name.endswith(".git")
        or "/dist/" in name
    ]
    assert banned == []

    with zipfile.ZipFile(archive) as zf:
        cmd = zf.read(f"{prefix}Install-PanelCAD.cmd")
        ps1 = zf.read(f"{prefix}scripts/Install-PanelCAD.ps1")
        install = zf.read(f"{prefix}INSTALL.txt").decode("utf-8")
        version_py = zf.read(f"{prefix}src/panelcad/version.py").decode("utf-8")
        pyproject = zf.read(f"{prefix}pyproject.toml").decode("utf-8")
    assert b"\r\n" in cmd
    assert b"\r\n" in ps1
    cmd_text = cmd.decode("utf-8")
    ps1_text = ps1.decode("utf-8")
    assert f"PanelCAD {__version__}" in install
    assert f'__version__ = "{__version__}"' in version_py
    assert "Do not use Drive split archives" in install
    assert "Install-PanelCAD.cmd" in install
    assert "/quiet" in install
    assert "PANELCAD_INSTALL_QUIET" in install
    assert "__pycache__" in install
    assert "pcad.ai.jsonl" in install
    assert 'import panelcad; print(panelcad.__version__)' in install
    assert "/quiet" in cmd_text.lower()
    assert "PANELCAD_INSTALL_QUIET" in cmd_text
    assert "if not defined QUIET pause" in cmd_text
    assert "install_tree.py" in ps1_text
    assert 'import panelcad; print(panelcad.__version__)' in ps1_text
    assert "if (-not $Quiet)" in ps1_text
    assert "pypdf" in pyproject
    assert "pip install -e $InstallRoot" in ps1_text
    assert r"resources\ocr\tesseract" in install
    assert r"resources\ocr\electrical" in install
    assert "does not change PATH" in install


def test_installer_excludes_setup_archives():
    text = (ROOT / "scripts" / "install_tree.py").read_text(encoding="utf-8")
    assert ".zip" in text
    assert "PanelCAD-Setup.tar.gz" in text
    assert ".venvs" in text
    ps1 = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    assert "/MIR" not in ps1
    assert "install_tree.py" in ps1
    assert "robocopy /E /IS /IT" in ps1


def test_installer_ships_electrical_ocr_and_keeps_tesseract_as_fallback():
    text = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    assert "Test-BundledElectrical" in text
    assert "Get-ElectricalLockWheelNames" in text
    assert "electrical-ocr.lock.json" in text
    assert r"resources\ocr\electrical" in text
    assert "Install-BundledOnnxRuntime" in text
    assert "--no-index" in text
    assert "--find-links" in text
    assert "--only-binary=:all:" in text
    assert "flatbuffers, numpy, packaging, and protobuf" in text
    assert "does not download OCR models" in text
    assert "return ,@()" not in text
    assert "Test-BundledTesseract" in text
    assert "only as a fallback" in text
    assert "https://tesseract-ocr.github.io/tessdoc/Downloads.html" not in text


def _tesseract_fixture(tmp_path: Path) -> Path:
    runtime = tmp_path / "tesseract-runtime"
    (runtime / "tessdata" / "configs").mkdir(parents=True)
    (runtime / "licenses").mkdir()
    (runtime / "tesseract.exe").write_bytes(b"MZ\x00fixture")
    (runtime / "tessdata" / "eng.traineddata").write_bytes(b"eng\x00")
    (runtime / "tessdata" / "configs" / "tsv").write_bytes(b"tessedit_create_tsv 1\n")
    (runtime / "licenses" / "NOTICE.md").write_text("Tesseract notice\n", encoding="utf-8")
    return runtime


def test_build_release_packs_the_tesseract_runtime(tmp_path: Path):
    build = _load_build_release()
    runtime = _tesseract_fixture(tmp_path)
    archive, _checksum = build.build_release(ROOT, tmp_path / "zip", tesseract_runtime=runtime)
    prefix = f"PanelCAD-{__version__}/resources/ocr/tesseract/"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        tsv = zf.read(prefix + "tessdata/configs/tsv")
    assert prefix + "tesseract.exe" in names
    assert prefix + "tessdata/eng.traineddata" in names
    assert prefix + "licenses/NOTICE.md" in names
    assert tsv == b"tessedit_create_tsv 1\n"


def test_build_release_asks_the_fetcher_for_the_cli_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    build = _load_build_release()
    runtime = _tesseract_fixture(tmp_path)
    calls: list[Path] = []

    class Tools:
        @staticmethod
        def ensure_runtime(root: Path) -> Path:
            calls.append(root)
            return runtime

        @staticmethod
        def iter_runtime_files(path: Path) -> list[tuple[str, Path]]:
            return [
                (item.relative_to(path).as_posix(), item)
                for item in sorted(path.rglob("*"))
                if item.is_file()
            ]

    monkeypatch.setattr(build, "_tesseract_fetch_tools", lambda: Tools)
    archive, _checksum = build.build_release(ROOT, tmp_path / "zip", include_tesseract=True)
    assert calls == [ROOT]
    prefix = f"PanelCAD-{__version__}/resources/ocr/tesseract/tesseract.exe"
    with zipfile.ZipFile(archive) as zf:
        assert prefix in zf.namelist()
    source = (ROOT / "scripts" / "build-release.py").read_text(encoding="utf-8")
    assert "include_tesseract=True" in source


def _electrical_fixture(tmp_path: Path) -> Path:
    import json

    runtime = tmp_path / "electrical-ocr"
    folders = (
        "models/PP-OCRv5_mobile_det_onnx",
        "models/en_PP-OCRv5_mobile_rec_onnx",
        "models/PP-OCRv6_small_det_onnx",
        "models/PP-OCRv6_small_rec_onnx",
        "models/PP-LCNet_x1_0_textline_ori_onnx",
        "wheels",
        "licenses",
    )
    for folder in folders:
        (runtime / folder).mkdir(parents=True)
    for folder in folders:
        if not folder.startswith("models/"):
            continue
        (runtime / folder / "inference.onnx").write_bytes(b"onnx")
        (runtime / folder / "inference.yml").write_bytes(b"yml")
    lock = json.loads((ROOT / "packaging" / "electrical-ocr.lock.json").read_text(encoding="utf-8"))
    for wheel in lock["wheels"]:
        (runtime / "wheels" / wheel["filename"]).write_bytes(b"wheel")
    (runtime / "licenses" / "NOTICE.md").write_text("Apache-2.0\n", encoding="utf-8")
    (runtime / "licenses" / "paddleocr-Apache-2.0.txt").write_text("Apache\n", encoding="utf-8")
    (runtime / "licenses" / "onnxruntime-MIT.txt").write_text("MIT\n", encoding="utf-8")
    return runtime


def test_build_release_packs_the_electrical_reader(tmp_path: Path):
    build = _load_build_release()
    runtime = _electrical_fixture(tmp_path)
    archive, _checksum = build.build_release(ROOT, tmp_path / "zip", electrical_runtime=runtime)
    prefix = f"PanelCAD-{__version__}/resources/ocr/electrical/"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert prefix + "models/PP-OCRv5_mobile_det_onnx/inference.onnx" in names
    assert prefix + "models/PP-OCRv6_small_rec_onnx/inference.onnx" in names
    assert prefix + "models/PP-LCNet_x1_0_textline_ori_onnx/inference.yml" in names
    assert prefix + "wheels/onnxruntime-1.31.1-cp312-cp312-win_amd64.whl" in names
    assert prefix + "wheels/flatbuffers-25.12.19-py2.py3-none-any.whl" in names
    assert prefix + "wheels/numpy-2.3.5-cp312-cp312-win_amd64.whl" in names
    assert prefix + "wheels/packaging-26.3-py3-none-any.whl" in names
    assert prefix + "wheels/protobuf-5.29.5-py3-none-any.whl" in names
    assert prefix + "licenses/NOTICE.md" in names
    assert prefix + "licenses/paddleocr-Apache-2.0.txt" in names
    source = (ROOT / "scripts" / "build-release.py").read_text(encoding="utf-8")
    assert "include_electrical=True" in source


def test_electrical_ocr_lock_pins_weights_and_wheels():
    import json

    lock = json.loads((ROOT / "packaging" / "electrical-ocr.lock.json").read_text(encoding="utf-8"))
    notice = (ROOT / "packaging" / "electrical" / "NOTICE.md").read_text(encoding="utf-8")
    assert lock["onnxruntime_version"] == "1.31.1"
    names = {model["directory"] for model in lock["models"]}
    assert names == {
        "PP-OCRv5_mobile_det_onnx",
        "en_PP-OCRv5_mobile_rec_onnx",
        "PP-OCRv6_small_det_onnx",
        "PP-OCRv6_small_rec_onnx",
        "PP-LCNet_x1_0_textline_ori_onnx",
    }
    for model in lock["models"]:
        files = {item["name"] for item in model["files"]}
        assert {"inference.onnx", "inference.yml"} <= files
        for item in model["files"]:
            assert len(item["sha256"]) == 64
            assert item["url"].endswith("/" + item["name"])
            assert model["revision"] in item["url"]
    fetch = _load_electrical_fetch()
    fetch.validate_lock(lock)
    assert lock["flatbuffers_version"] == "25.12.19"
    assert lock["numpy_version"] == "2.3.5"
    assert lock["packaging_version"] == "26.3"
    assert lock["protobuf_version"] == "5.29.5"
    names = {wheel["filename"] for wheel in lock["wheels"]}
    assert "flatbuffers-25.12.19-py2.py3-none-any.whl" in names
    assert "numpy-2.3.5-cp312-cp312-win_amd64.whl" in names
    assert "packaging-26.3-py3-none-any.whl" in names
    assert "protobuf-5.29.5-py3-none-any.whl" in names
    assert not any("sympy" in name or "ml_dtypes" in name for name in names)
    assert "Apache-2.0" in notice
    assert "flatbuffers" in notice
    assert "protobuf" in notice
    assert "~/.paddlex" in notice


def _setup_archive(tmp_path: Path) -> Path:
    build = _load_build_release()
    return build.build_release(
        ROOT,
        tmp_path / "zip",
        tesseract_runtime=_tesseract_fixture(tmp_path),
        electrical_runtime=_electrical_fixture(tmp_path),
    )[0]


def test_setup_zip_contains_every_path_the_installer_reads(tmp_path: Path):
    archive = _setup_archive(tmp_path)
    prefix = f"PanelCAD-{__version__}/"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    ps1 = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    required = [
        "pyproject.toml",
        "src/panelcad/version.py",
        "packaging/electrical-ocr.lock.json",
        "packaging/tesseract-runtime.lock.json",
        "packaging/electrical/NOTICE.md",
        "packaging/tesseract/NOTICE.md",
        "scripts/install_tree.py",
        "scripts/Install-PanelCAD.ps1",
        "Install-PanelCAD.cmd",
        "examples/dol_starter.pcad.yaml",
        "src/panelcad/assets/panelcad.ico",
        "resources/ocr/tesseract/tesseract.exe",
        "resources/ocr/tesseract/tessdata/eng.traineddata",
    ]
    for raw in re.findall(r'Join-Path \$Source "([^"]+)"', ps1):
        rel = raw.replace("\\", "/")
        if rel.endswith((".json", ".exe", ".traineddata")):
            required.append(rel)
    for raw in re.findall(r'"((?:models|licenses)\\[^"]+)"', ps1):
        required.append("resources/ocr/electrical/" + raw.replace("\\", "/"))
    lock = json.loads((ROOT / "packaging" / "electrical-ocr.lock.json").read_text(encoding="utf-8"))
    for wheel in lock["wheels"]:
        required.append(f"resources/ocr/electrical/wheels/{wheel['filename']}")
    missing = sorted({rel for rel in required if prefix + rel not in names})
    assert missing == [], missing
    for path in (ROOT / "packaging").rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        assert prefix + rel in names, rel


def test_missing_electrical_lock_is_an_empty_list(tmp_path: Path):
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    archive = _setup_archive(tmp_path)
    setup = tmp_path / "setup"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(setup)
    root = setup / f"PanelCAD-{__version__}"
    empty = tmp_path / "empty"
    empty.mkdir()
    script = tmp_path / "check-lock.ps1"
    script.write_text(
        "\n".join(
            [
                'param([string]$Root, [string]$Empty)',
                '$ErrorActionPreference = "Stop"',
                '. (Join-Path $Root "scripts\\Install-PanelCAD.ps1")',
                "$names = @(Get-ElectricalLockWheelNames $Empty)",
                'if ($names.Count -ne 0) { throw "empty lock count=$($names.Count)" }',
                "foreach ($name in $names) { Join-Path $Empty $name | Out-Null }",
                "if (Test-BundledElectrical $Empty) { throw 'empty setup looked complete' }",
                "if (-not (Test-BundledElectrical $Root)) { throw 'setup zip failed the electrical check' }",
                "$locked = @(Get-ElectricalLockWheelNames $Root)",
                'if ($locked.Count -lt 1) { throw "lock produced no wheel names" }',
                "foreach ($name in $locked) {",
                '    if ($name -isnot [string]) { throw "wheel name is $($name.GetType().FullName)" }',
                '    $wheel = Join-Path (Join-Path $Root "resources\\ocr\\electrical\\wheels") $name',
                '    if (-not (Test-Path -LiteralPath $wheel)) { throw "missing $name" }',
                "}",
                'Remove-Item -LiteralPath (Join-Path $Root "packaging\\electrical-ocr.lock.json")',
                "$gone = @(Get-ElectricalLockWheelNames $Root)",
                'if ($gone.Count -ne 0) { throw "removed lock count=$($gone.Count)" }',
                "if (Test-BundledElectrical $Root) { throw 'removed lock still passed' }",
                'Write-Output "ok $($locked.Count)"',
            ]
        ),
        encoding="utf-8",
    )
    env = dict(**os.environ)
    env["USERPROFILE"] = str(tmp_path / "profile")
    env["LOCALAPPDATA"] = str(tmp_path / "local")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(script), str(root), str(empty)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_build_release_refuses_a_runtime_missing_an_onnx_dependency(tmp_path: Path):
    build = _load_build_release()
    runtime = _electrical_fixture(tmp_path)
    missing = next((runtime / "wheels").glob("flatbuffers-*.whl"))
    missing.unlink()
    with pytest.raises(SystemExit, match="flatbuffers"):
        build.build_release(ROOT, tmp_path / "out", electrical_runtime=runtime)


def test_build_release_refuses_a_runtime_without_traineddata(tmp_path: Path):
    build = _load_build_release()
    runtime = tmp_path / "incomplete"
    runtime.mkdir()
    (runtime / "tesseract.exe").write_bytes(b"MZ\x00")
    with pytest.raises(SystemExit, match="eng.traineddata"):
        build.build_release(ROOT, tmp_path / "out", tesseract_runtime=runtime)


def test_installer_uses_the_shipped_tesseract_without_touching_path():
    text = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    assert "Test-BundledTesseract" in text
    assert r"resources\ocr\tesseract\tesseract.exe" in text
    assert "does not change PATH" in text
    assert "tesseract-ocr-w64-setup" not in text


def test_release_lists_brand_assets():
    build = _load_build_release()
    assert build.missing_brand_assets(ROOT) == []
    rels = {rel for rel, _path in build.iter_release_files(ROOT)}
    for rel in build.BRAND_ASSETS:
        assert rel in rels
        assert (ROOT / rel).stat().st_size > 50


def test_build_release_refuses_a_missing_icon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    build = _load_build_release()
    monkeypatch.setattr(build, "BRAND_ASSETS", ("src/panelcad/assets/missing-panelcad.ico",))
    with pytest.raises(SystemExit, match="icon assets"):
        build.build_release(ROOT, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_public_zip_uses_the_snapshot_exclude_list(tmp_path: Path):
    """A public zip omits the private paths and passes the snapshot self-check."""

    import importlib.util

    build = _load_build_release()
    snap_path = ROOT / "scripts" / "public-snapshot.py"
    spec = importlib.util.spec_from_file_location("panelcad_public_snapshot_test", snap_path)
    assert spec and spec.loader
    snap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snap)

    public_zip, _checksum = build.build_release(ROOT, tmp_path / "public", public=True)
    prefix = f"PanelCAD-{__version__}/"
    with zipfile.ZipFile(public_zip) as zf:
        public_names = [name[len(prefix) :] if name.startswith(prefix) else name for name in zf.namelist()]
    for item in snap.EXCLUDE:
        leaked = [name for name in public_names if name == item or name.startswith(item.rstrip("/") + "/")]
        assert leaked == [], leaked
    assert build.public_zip_problems(public_zip, ROOT) == []

    shop_zip, _shop_sha = build.build_release(ROOT, tmp_path / "shop")
    with zipfile.ZipFile(shop_zip) as zf:
        shop_names = [name.replace("\\", "/") for name in zf.namelist()]

    def _zip_has(names: list[str], item: str) -> bool:
        needle = item.rstrip("/")
        for name in names:
            rel = name
            if rel.startswith("PanelCAD-") and "/" in rel:
                rel = rel.split("/", 1)[1]
            if rel == needle or rel.startswith(needle + "/"):
                return True
        return False

    # Shop zips still ship the paths a public zip leaves out.
    for item in snap.EXCLUDE:
        if not (ROOT / item).exists():
            continue
        assert _zip_has(shop_names, item), item
        assert not _zip_has(public_names, item), item


def test_pyproject_and_installer_include_pypdf():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"pypdf>=", pyproject)
    installer = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    assert "pip install -e $InstallRoot" in installer
