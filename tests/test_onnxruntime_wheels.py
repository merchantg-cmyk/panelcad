"""Offline install of the shipped ONNX Runtime wheel.

The shop failure was ``pip install --no-index`` with only the onnxruntime
wheel in the folder. These tests fail when flatbuffers, numpy, packaging,
or protobuf is missing from that folder. ``--ignore-installed`` is the empty
environment: a machine that already has numpy or packaging must not hide a
missing wheel.
"""

from __future__ import annotations

import email
import importlib.util
import subprocess
import sys
import zipfile
from email import policy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_fetch():
    path = ROOT / "scripts" / "fetch_electrical_ocr.py"
    spec = importlib.util.spec_from_file_location("panelcad_fetch_electrical_ocr_wheels", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _requirement_name(spec: str) -> str:
    name = spec.split(";", 1)[0].strip().split("[", 1)[0]
    for sep in ("===", "==", "~=", "!=", ">=", "<=", ">", "<"):
        if sep in name:
            name = name.split(sep, 1)[0]
    return name.strip()


def _requires(path: Path) -> tuple[set[str], set[str]]:
    with zipfile.ZipFile(path) as archive:
        meta = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        message = email.message_from_bytes(archive.read(meta), policy=policy.default)
    base: set[str] = set()
    extras: set[str] = set()
    for spec in message.get_all("Requires-Dist") or []:
        name = _requirement_name(spec)
        if "; extra ==" in spec:
            extras.add(name)
        else:
            base.add(name)
    return base, extras


def _materialize(tmp_path: Path):
    fetch = _load_fetch()
    lock = fetch.load_lock(fetch.lock_path(ROOT))
    fetch.validate_lock(lock)
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    for wheel in lock["wheels"]:
        path = wheel_dir / wheel["filename"]
        fetch.download(wheel["url"], path)
        fetch.require_file(path, wheel["sha256"], int(wheel["bytes"]), wheel["filename"])
    return fetch, lock, wheel_dir


def _pip(
    wheel_dir: Path, wheel: Path, python: str, platform: str
) -> subprocess.CompletedProcess[str]:
    tag = "cp" + python.replace(".", "")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--dry-run",
        "--ignore-installed",
        "--no-index",
        "--only-binary=:all:",
        "--find-links",
        str(wheel_dir),
        "--platform",
        platform,
        "--python-version",
        python,
        "--implementation",
        "cp",
        "--abi",
        tag,
        str(wheel),
    ]
    return subprocess.run(command, check=False, capture_output=True, text=True)


def test_lock_rejects_a_missing_dependency_wheel() -> None:
    fetch = _load_fetch()
    lock = fetch.load_lock(fetch.lock_path(ROOT))
    lock["wheels"] = [
        wheel for wheel in lock["wheels"] if not str(wheel["filename"]).startswith("flatbuffers-")
    ]
    with pytest.raises(SystemExit, match="flatbuffers"):
        fetch.validate_lock(lock)


def test_lock_rejects_a_numpy_tag_the_runtime_ships() -> None:
    fetch = _load_fetch()
    lock = fetch.load_lock(fetch.lock_path(ROOT))
    removed = "numpy-2.3.5-cp312-cp312-win_arm64.whl"
    lock["wheels"] = [wheel for wheel in lock["wheels"] if wheel["filename"] != removed]
    with pytest.raises(SystemExit, match=r"numpy 2\.3\.5 wheel for Python 3\.12 \(win_arm64\)"):
        fetch.validate_lock(lock)


def test_lock_rejects_the_symbolic_extra() -> None:
    fetch = _load_fetch()
    lock = fetch.load_lock(fetch.lock_path(ROOT))
    lock["wheels"] = [
        *lock["wheels"],
        {
            "filename": "sympy-1.13.1-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/aa/sympy-1.13.1-py3-none-any.whl",
            "sha256": "a" * 64,
            "bytes": 10,
            "python": "py3",
            "platform": "any",
        },
    ]
    with pytest.raises(SystemExit, match="sympy"):
        fetch.validate_lock(lock)


def test_offline_pip_installs_onnxruntime_and_its_required_wheels(tmp_path: Path) -> None:
    fetch, lock, wheel_dir = _materialize(tmp_path)
    ort_wheels = [wheel for wheel in lock["wheels"] if wheel["filename"].startswith("onnxruntime-")]
    assert len(ort_wheels) == 8
    shop = next(
        wheel
        for wheel in ort_wheels
        if wheel["python"] == "3.12" and wheel["platform"] == "win_amd64"
    )
    base, extras = _requires(wheel_dir / shop["filename"])
    assert base == set(fetch.RUNTIME_REQUIREMENTS)
    assert extras == {"sympy", "ml_dtypes"}
    for wheel in lock["wheels"]:
        if wheel["filename"].startswith("onnxruntime-"):
            continue
        dep_base, dep_extras = _requires(wheel_dir / wheel["filename"])
        assert dep_base == set(), wheel["filename"]
        assert dep_extras == set(), wheel["filename"]

    universal = {
        "flatbuffers": f"flatbuffers-{lock['flatbuffers_version']}-py2.py3-none-any.whl",
        "packaging": f"packaging-{lock['packaging_version']}-py3-none-any.whl",
        "protobuf": f"protobuf-{lock['protobuf_version']}-py3-none-any.whl",
    }
    for wheel in ort_wheels:
        result = _pip(wheel_dir, wheel_dir / wheel["filename"], wheel["python"], wheel["platform"])
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        tag = "cp" + wheel["python"].replace(".", "")
        numpy_name = f"numpy-{lock['numpy_version']}-{tag}-{tag}-{wheel['platform']}.whl"
        assert numpy_name in combined
        for filename in universal.values():
            assert filename in combined
        assert "sympy" not in combined
        assert "ml_dtypes" not in combined

    for dist, filename in {
        **universal,
        "numpy": f"numpy-{lock['numpy_version']}-cp312-cp312-win_amd64.whl",
    }.items():
        path = wheel_dir / filename
        aside = tmp_path / "aside" / filename
        aside.parent.mkdir(parents=True, exist_ok=True)
        path.rename(aside)
        try:
            result = _pip(wheel_dir, wheel_dir / shop["filename"], "3.12", "win_amd64")
            combined = result.stdout + result.stderr
            assert result.returncode != 0, combined
            assert dist in combined
        finally:
            aside.rename(path)
