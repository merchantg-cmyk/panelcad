#!/usr/bin/env python3
"""Download the pinned Windows Tesseract 5 runtime used in the setup zip.

The shop PC does not run this script. ``scripts/build-release.py`` runs it
on the build machine, checks the sha256 values in
``packaging/tesseract-runtime.lock.json``, and packs the files into the
zip. Install then copies that tree. The first scan does not download
anything.

7-Zip is required on the build machine to unpack the official NSIS
installer. It is not required on the PC that runs PanelCAD.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

LOCK_NAME = "tesseract-runtime.lock.json"
NOTICE_REL = Path("packaging") / "tesseract" / "NOTICE.md"
RUNTIME_NOTICE = Path("licenses") / "NOTICE.md"


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def lock_path(root: Path) -> Path:
    return root / "packaging" / LOCK_NAME


def load_lock(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"Missing Tesseract runtime lock: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_file(path: Path, expected_sha: str, expected_bytes: int, label: str) -> None:
    if not path.is_file():
        raise SystemExit(f"Tesseract runtime is missing {label} ({path})")
    size = path.stat().st_size
    if size != expected_bytes:
        raise SystemExit(
            f"{label} size mismatch: expected {expected_bytes} bytes, got {size}"
        )
    actual = sha256_file(path)
    if actual != expected_sha:
        raise SystemExit(f"{label} sha256 mismatch: expected {expected_sha}, got {actual}")


def cache_dir() -> Path:
    override = os.environ.get("PANELCAD_TESSERACT_CACHE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".cache" / "panelcad" / "tesseract-runtime"


def find_7z() -> str:
    for name in ("7z", "7zz", "7za"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for candidate in (
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files\7-Zip\7za.exe",
        ):
            if Path(candidate).is_file():
                return candidate
    raise SystemExit(
        "7-Zip is required on the build machine to unpack the official "
        "Tesseract installer (the NSIS setup exe). Install 7z and rebuild. "
        "The PanelCAD installer does not run this step."
    )


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "panelcad-release"})
    with urllib.request.urlopen(request, timeout=180) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    partial.replace(dest)


def extract_installer(installer: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    subprocess.run(
        [find_7z(), "x", "-y", f"-o{dest}", str(installer)],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _source_for(item: dict, extracted: Path, eng_file: Path) -> Path:
    if item.get("source") == "tessdata_fast":
        return eng_file
    relative = str(item.get("installer_path") or item["path"])
    return extracted / relative


def assemble_runtime(
    lock: dict,
    *,
    extracted: Path,
    eng_file: Path,
    notice: Path,
    dest: Path,
) -> None:
    """Copy the locked files into ``dest`` and refuse a hash or size mismatch."""

    if not notice.is_file():
        raise SystemExit(f"Missing Tesseract notice: {notice}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for item in lock["files"]:
        relative = Path(item["path"])
        source = _source_for(item, extracted, eng_file)
        require_file(source, item["sha256"], int(item["bytes"]), item["path"])
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    notice_target = dest / RUNTIME_NOTICE
    notice_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(notice, notice_target)


def runtime_complete(dest: Path, lock: dict, notice: Path) -> bool:
    if not dest.is_dir() or not notice.is_file():
        return False
    shipped = dest / RUNTIME_NOTICE
    if not shipped.is_file() or shipped.read_bytes() != notice.read_bytes():
        return False
    for item in lock["files"]:
        path = dest / item["path"]
        if not path.is_file() or path.stat().st_size != int(item["bytes"]):
            return False
        if sha256_file(path) != item["sha256"]:
            return False
    return True


def ensure_runtime(root: Path | None = None) -> Path:
    """Return the cached runtime, downloading and checking it when needed."""

    root = (root or repo_root()).resolve()
    lock = load_lock(lock_path(root))
    notice = root / NOTICE_REL
    dest = cache_dir() / str(lock["id"])
    if runtime_complete(dest, lock, notice):
        return dest

    installer_meta = lock["installer"]
    downloads = cache_dir() / "downloads"
    installer = downloads / installer_meta["filename"]
    if not installer.is_file() or sha256_file(installer) != installer_meta["sha256"]:
        download(installer_meta["url"], installer)
        require_file(
            installer,
            installer_meta["sha256"],
            int(installer_meta["bytes"]),
            installer_meta["filename"],
        )
    trained = lock["tessdata_fast"]
    eng_file = downloads / "eng.traineddata"
    if not eng_file.is_file() or sha256_file(eng_file) != trained["sha256"]:
        download(trained["url"], eng_file)
        require_file(eng_file, trained["sha256"], int(trained["bytes"]), trained["path"])

    staging_parent = Path(tempfile.mkdtemp(prefix="panelcad-tesseract-"))
    try:
        extracted = staging_parent / "installer"
        extract_installer(installer, extracted)
        staging = staging_parent / "runtime"
        assemble_runtime(
            lock,
            extracted=extracted,
            eng_file=eng_file,
            notice=notice,
            dest=staging,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(staging), dest)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
    if not runtime_complete(dest, lock, notice):
        raise SystemExit(f"Tesseract runtime at {dest} failed the lock check after staging")
    return dest


def iter_runtime_files(runtime: Path) -> list[tuple[str, Path]]:
    """Repo-relative paths under ``resources/ocr/tesseract`` are added by the caller."""

    files: list[tuple[str, Path]] = []
    for path in sorted(runtime.rglob("*")):
        if path.is_file():
            files.append((path.relative_to(runtime).as_posix(), path))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="Copy the checked runtime here as well as the cache",
    )
    args = parser.parse_args(argv)
    runtime = ensure_runtime(args.root)
    print(f"tesseract runtime  {runtime}")
    if args.dest is not None:
        if args.dest.exists():
            shutil.rmtree(args.dest)
        shutil.copytree(runtime, args.dest)
        print(f"copied            {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
