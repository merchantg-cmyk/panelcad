"""H4 install and update hardening: constraints, Python bootstrap SHA-256, and rollback."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

from panelcad.update.handoff import (
    PROGRAM_DIRS,
    apply_upgrade,
    backup_install_tree,
    plan_upgrade,
    restore_install_tree,
)
from panelcad.update.manifest import UpdateError

ROOT = Path(__file__).resolve().parents[1]


def test_constraints_txt_pins_runtime_deps_and_matches_ocr_lock() -> None:
    constraints_path = ROOT / "packaging" / "constraints.txt"
    assert constraints_path.is_file()
    pins: dict[str, str] = {}
    for raw in constraints_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "==" in line:
            name, version = line.split("==", 1)
            pins[name.strip().lower()] = version.strip()
    lock = json.loads((ROOT / "packaging" / "electrical-ocr.lock.json").read_text(encoding="utf-8"))
    for key, pkg in (
        ("onnxruntime_version", "onnxruntime"),
        ("paddleocr_version", "paddleocr"),
        ("flatbuffers_version", "flatbuffers"),
        ("numpy_version", "numpy"),
        ("packaging_version", "packaging"),
        ("protobuf_version", "protobuf"),
    ):
        assert pins.get(pkg) == str(lock[key]), (pkg, pins.get(pkg), lock[key])
    for required in ("pydantic", "pyyaml", "pyside6", "typer", "ezdxf", "pypdf", "openpyxl", "pymupdf", "pillow"):
        assert required in pins, required


def test_windows_installer_verifies_python_sha256_uses_constraints_and_auto_rolls_back() -> None:
    ps1 = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    # Real 64-hex SHA-256 hashes for both amd64 and arm64 Python 3.12.10 installers.
    amd64 = re.search(r'"amd64"\s*=\s*"([0-9a-f]{64})"', ps1)
    arm64 = re.search(r'"arm64"\s*=\s*"([0-9a-f]{64})"', ps1)
    assert amd64 and arm64
    assert "Get-FileHash" in ps1
    assert "-Algorithm SHA256" in ps1
    assert r"packaging\constraints.txt" in ps1
    assert "pip install -e $InstallRoot -c $constraints" in ps1
    assert "Restore-BackupOnFailure" in ps1


def test_release_zip_ships_constraints_txt(tmp_path: Path) -> None:
    import importlib.util
    import sys

    path = ROOT / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("panelcad_build_release_h4", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    archive, _sha = mod.build_release(ROOT, tmp_path)
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert any(name.endswith("/packaging/constraints.txt") for name in names)


def test_rollback_restores_libraries_and_packaging_while_keeping_user_drawings(tmp_path: Path) -> None:
    assert {"src", "scripts", "libraries", "packaging"} <= set(PROGRAM_DIRS)
    install = tmp_path / "PanelCAD"
    backup = tmp_path / "backup"
    for rel, content in (
        ("src/panelcad/version.py", '__version__ = "0.5.27"\n'),
        ("scripts/Install-PanelCAD.ps1", "# old\n"),
        ("libraries/builtin/nema.yaml", "version: old\n"),
        ("packaging/constraints.txt", "pypdf==7.0.0\n"),
        ("pyproject.toml", '[project]\nname = "panelcad"\nversion = "0.5.27"\n'),
        ("shop-job.pcad.yaml", "name: Keep Me\n"),
    ):
        target = install / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    backup_install_tree(install, backup)

    # Simulate an upgrade that modified libraries/ and packaging/ and added extra files.
    (install / "src" / "panelcad" / "version.py").write_text('__version__ = "0.5.28"\n', encoding="utf-8")
    (install / "libraries" / "builtin" / "nema.yaml").write_text("version: new\n", encoding="utf-8")
    (install / "libraries" / "builtin" / "extra.yaml").write_text("extra: true\n", encoding="utf-8")
    (install / "packaging" / "constraints.txt").write_text("pypdf==7.1.1\n", encoding="utf-8")

    restore_install_tree(backup, install)

    assert (install / "src" / "panelcad" / "version.py").read_text(encoding="utf-8") == '__version__ = "0.5.27"\n'
    assert (install / "libraries" / "builtin" / "nema.yaml").read_text(encoding="utf-8") == "version: old\n"
    assert not (install / "libraries" / "builtin" / "extra.yaml").exists()
    assert (install / "packaging" / "constraints.txt").read_text(encoding="utf-8") == "pypdf==7.0.0\n"
    assert (install / "shop-job.pcad.yaml").read_text(encoding="utf-8") == "name: Keep Me\n"


def test_apply_upgrade_auto_rolls_back_when_installer_fails_after_copy(tmp_path: Path) -> None:
    profile = tmp_path / "user"
    install = profile / "PanelCAD"
    for rel, content in (
        ("src/panelcad/version.py", '__version__ = "0.5.27"\n'),
        ("scripts/Install-PanelCAD.ps1", "# old\n"),
        ("libraries/builtin/nema.yaml", "version: old\n"),
        ("packaging/constraints.txt", "pypdf==7.0.0\n"),
        ("pyproject.toml", '[project]\nname = "panelcad"\nversion = "0.5.27"\n'),
    ):
        target = install / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    plan = plan_upgrade(
        new_version="0.5.28",
        previous_version="0.5.27",
        installer=str(tmp_path / "stage" / "scripts" / "Install-PanelCAD.ps1"),
        user_profile=str(profile),
        config_dir=str(profile / ".panelcad"),
        python_exe=str(profile / ".venvs" / "panelcad" / "bin" / "python"),
        wait_pid=0,
    )

    def _failing_installer(_plan) -> None:
        # Simulate Copy-Source succeeding before pip install fails.
        (install / "src" / "panelcad" / "version.py").write_text('__version__ = "0.5.28"\n', encoding="utf-8")
        (install / "libraries" / "builtin" / "nema.yaml").write_text("version: broken\n", encoding="utf-8")
        raise UpdateError("pip install failed")

    with pytest.raises(UpdateError, match="pip install failed"):
        apply_upgrade(plan, runner=_failing_installer, relaunch=False)

    assert (install / "src" / "panelcad" / "version.py").read_text(encoding="utf-8") == '__version__ = "0.5.27"\n'
    assert (install / "libraries" / "builtin" / "nema.yaml").read_text(encoding="utf-8") == "version: old\n"
