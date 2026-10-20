from __future__ import annotations

import hashlib
import re
import sys
import zipfile
from pathlib import Path

import pytest

from panelcad.version import __version__

ROOT = Path(__file__).resolve().parents[1]


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


def test_installer_excludes_setup_archives():
    text = (ROOT / "scripts" / "install_tree.py").read_text(encoding="utf-8")
    assert ".zip" in text
    assert "PanelCAD-Setup.tar.gz" in text
    assert ".venvs" in text
    ps1 = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    assert "/MIR" not in ps1
    assert "install_tree.py" in ps1
    assert "robocopy /E /IS /IT" in ps1


def test_installer_points_at_the_official_tesseract_source():
    text = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    assert "Test-Tesseract" in text
    assert "Tesseract-OCR" in text
    assert "https://github.com/tesseract-ocr/tesseract" in text
    assert "https://tesseract-ocr.github.io/tessdoc/Downloads.html" in text


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
