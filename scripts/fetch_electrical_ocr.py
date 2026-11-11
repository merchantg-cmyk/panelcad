#!/usr/bin/env python3
"""Download the pinned electrical OCR weights and ONNX Runtime wheels.

The wheel list is ONNX Runtime plus the distributions that wheel requires:
flatbuffers, numpy, packaging, and protobuf. The symbolic and quantization
extras are not in the lock. The shop PC does not run this script.
``scripts/build-release.py`` runs it on the build machine, checks the sha256
values in ``packaging/electrical-ocr.lock.json``, and packs the tree into
the zip under ``resources/ocr/electrical``. Install then copies that tree.
The first scan does not download anything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

LOCK_NAME = "electrical-ocr.lock.json"
NOTICE_SRC = Path("packaging") / "electrical" / "NOTICE.md"
APACHE_SRC = Path("packaging") / "electrical" / "LICENSE"
MIT_SRC = Path("packaging") / "electrical" / "onnxruntime-LICENSE"
# Required-Dist of the pinned onnxruntime wheel, ignoring extras.
RUNTIME_REQUIREMENTS = ("flatbuffers", "numpy", "packaging", "protobuf")
EXCLUDED_EXTRAS = ("sympy", "ml_dtypes")
ORT_PYTHONS = ("3.11", "3.12", "3.13", "3.14")
ORT_PLATFORMS = ("win_amd64", "win_arm64")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def lock_path(root: Path) -> Path:
    return root / "packaging" / LOCK_NAME


def load_lock(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Missing electrical OCR lock: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_wheel_filename(filename: str) -> tuple[str, str, str, str, str]:
    """Return distribution, version, python tag, abi tag, and platform tag."""

    if not filename.endswith(".whl"):
        raise SystemExit(f"Electrical OCR lock wheel is not a wheel: {filename}")
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        raise SystemExit(f"Electrical OCR lock wheel name is not understood: {filename}")
    platform, abi, python_tag = parts[-1], parts[-2], parts[-3]
    version = parts[-4]
    name = "-".join(parts[:-4])
    if not name or not version:
        raise SystemExit(f"Electrical OCR lock wheel name is not understood: {filename}")
    return name, version, python_tag, abi, platform


def wheel_covers(filename: str, python: str, platform: str) -> bool:
    """True when this wheel can install on that CPython/Windows pair."""

    _name, _version, python_tag, abi, wheel_platform = parse_wheel_filename(filename)
    if wheel_platform not in {platform, "any"}:
        return False
    if python_tag in {"py3", "py2.py3"} and abi == "none":
        return True
    tag = "cp" + python.replace(".", "")
    if python_tag == tag and abi == tag:
        return True
    if abi == "abi3" and python_tag.startswith("cp") and python_tag[2:].isdigit():
        major = int(python_tag[2])
        minor = int(python_tag[3:])
        want_major, want_minor = (int(part) for part in python.split("."))
        return (want_major, want_minor) >= (major, minor)
    return False


def validate_lock(lock: dict) -> None:
    """Fail when the lock cannot satisfy an offline ONNX Runtime install."""

    wheels = lock.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        raise SystemExit("Electrical OCR lock has no wheels")
    seen: list[tuple[str, str, str]] = []
    for wheel in wheels:
        filename = str(wheel.get("filename") or "")
        name, version, python_tag, _abi, platform = parse_wheel_filename(filename)
        if name in EXCLUDED_EXTRAS:
            raise SystemExit(f"Electrical OCR lock must not ship the {name} extra")
        if python_tag.startswith("cp") and "t" in python_tag:
            raise SystemExit(
                f"{filename} is a free-threaded wheel. The ONNX Runtime wheels are not."
            )
        digest = str(wheel.get("sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise SystemExit(f"{filename} is missing sha256")
        if int(wheel.get("bytes") or 0) <= 0:
            raise SystemExit(f"{filename} is missing its size")
        url = str(wheel.get("url") or "")
        pinned = url.startswith("https://files.pythonhosted.org/") and url.endswith("/" + filename)
        if not pinned:
            raise SystemExit(f"{filename} URL is not the pinned PyPI file")
        recorded_python = str(wheel.get("python") or "")
        recorded_platform = str(wheel.get("platform") or "")
        if platform == "any":
            tags_match = recorded_platform == "any" and recorded_python == python_tag
        else:
            pretty = python_tag
            if python_tag.startswith("cp"):
                pretty = f"{python_tag[2]}.{python_tag[3:]}"
            tags_match = recorded_platform == platform and recorded_python == pretty
        if not tags_match:
            raise SystemExit(f"{filename} python/platform tags do not match the file name")
        seen.append((name, version, filename))

    ort_version = str(lock.get("onnxruntime_version") or "")
    expected = set()
    for python in ORT_PYTHONS:
        tag = "cp" + python.replace(".", "")
        for platform in ORT_PLATFORMS:
            expected.add(f"onnxruntime-{ort_version}-{tag}-{tag}-{platform}.whl")
    got = {filename for name, _version, filename in seen if name == "onnxruntime"}
    if got != expected:
        missing = ", ".join(sorted(expected - got))
        extra = ", ".join(sorted(got - expected))
        detail = ""
        if missing:
            detail += f" Missing: {missing}."
        if extra:
            detail += f" Unexpected: {extra}."
        raise SystemExit(
            "Electrical OCR lock ONNX Runtime wheels must be "
            f"{ort_version} for Python {', '.join(ORT_PYTHONS)} "
            f"on {', '.join(ORT_PLATFORMS)}.{detail}"
        )

    for dist in RUNTIME_REQUIREMENTS:
        version = str(lock.get(f"{dist}_version") or "")
        if not version:
            raise SystemExit(f"Electrical OCR lock is missing {dist}_version")
        chosen = [item for item in seen if item[0] == dist and item[1] == version]
        if not chosen:
            raise SystemExit(f"Electrical OCR lock has no {dist} {version} wheel")
        for python in ORT_PYTHONS:
            for platform in ORT_PLATFORMS:
                covered = any(
                    wheel_covers(filename, python, platform)
                    for _name, _version, filename in chosen
                )
                if covered:
                    continue
                raise SystemExit(
                    f"Electrical OCR lock has no {dist} {version} wheel "
                    f"for Python {python} ({platform})"
                )

    known = {"onnxruntime", *RUNTIME_REQUIREMENTS}
    unknown = sorted({name for name, _version, _filename in seen} - known)
    if unknown:
        raise SystemExit("Electrical OCR lock has unexpected wheels: " + ", ".join(unknown))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(path: Path, expected_sha: str, expected_bytes: int, label: str) -> None:
    if not path.is_file():
        raise SystemExit(f"Electrical OCR runtime is missing {label} ({path})")
    size = path.stat().st_size
    if size != expected_bytes:
        raise SystemExit(f"{label} size mismatch: expected {expected_bytes} bytes, got {size}")
    actual = sha256_file(path)
    if actual != expected_sha:
        raise SystemExit(f"{label} sha256 mismatch: expected {expected_sha}, got {actual}")


def cache_dir() -> Path:
    override = os.environ.get("PANELCAD_ELECTRICAL_OCR_CACHE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".cache" / "panelcad" / "electrical-ocr"


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "panelcad-release"})
    with urllib.request.urlopen(request, timeout=180) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    partial.replace(dest)


def _notices(root: Path) -> list[tuple[str, Path]]:
    pairs = (
        (NOTICE_SRC, Path("licenses") / "NOTICE.md"),
        (APACHE_SRC, Path("licenses") / "paddleocr-Apache-2.0.txt"),
        (MIT_SRC, Path("licenses") / "onnxruntime-MIT.txt"),
    )
    found = []
    for src_rel, dest_rel in pairs:
        src = root / src_rel
        if not src.is_file():
            raise SystemExit(f"Missing electrical OCR notice: {src}")
        found.append((dest_rel.as_posix(), src))
    return found


def _place_notices(root: Path, dest: Path) -> None:
    for rel, src in _notices(root):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)


def verify_tree(root: Path, dest: Path, lock: dict) -> None:
    for wheel in lock["wheels"]:
        require_file(
            dest / "wheels" / wheel["filename"],
            wheel["sha256"],
            int(wheel["bytes"]),
            wheel["filename"],
        )
    for model in lock["models"]:
        folder = dest / "models" / model["directory"]
        for item in model["files"]:
            require_file(
                folder / item["name"],
                item["sha256"],
                int(item["bytes"]),
                f"{model['directory']}/{item['name']}",
            )
    for rel, _src in _notices(root):
        if not (dest / rel).is_file():
            raise SystemExit(f"Electrical OCR runtime is missing {rel}")


def _complete(root: Path, dest: Path, lock: dict) -> bool:
    try:
        verify_tree(root, dest, lock)
    except SystemExit:
        return False
    return True


def ensure_runtime(root: Path, dest: Path | None = None) -> Path:
    """Return a directory whose hashes match the lock. Download on a miss."""

    lock = load_lock(lock_path(root))
    validate_lock(lock)
    target = dest or (cache_dir() / "runtime")
    if _complete(root, target, lock):
        return target
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for wheel in lock["wheels"]:
        path = target / "wheels" / wheel["filename"]
        download(wheel["url"], path)
        require_file(path, wheel["sha256"], int(wheel["bytes"]), wheel["filename"])
    for model in lock["models"]:
        folder = target / "models" / model["directory"]
        for item in model["files"]:
            path = folder / item["name"]
            download(item["url"], path)
            require_file(
                path,
                item["sha256"],
                int(item["bytes"]),
                f"{model['directory']}/{item['name']}",
            )
    _place_notices(root, target)
    verify_tree(root, target, lock)
    return target


def iter_runtime_files(runtime: Path) -> list[tuple[str, Path]]:
    """Repo-relative paths under ``resources/ocr/electrical`` are added by the caller."""

    files = []
    for path in sorted(runtime.rglob("*")):
        if path.is_file():
            files.append((path.relative_to(runtime).as_posix(), path))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--dest", type=Path, default=None, help="Stage here instead of the cache")
    args = parser.parse_args(argv)
    root = (args.root or repo_root()).resolve()
    runtime = ensure_runtime(root, args.dest.resolve() if args.dest else None)
    print(f"electrical ocr  {runtime}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
