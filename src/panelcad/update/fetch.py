"""Copy or download an installer zip and refuse it when the sha256 does not match."""

from __future__ import annotations

import hashlib
import re
import shutil
import ssl
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from pathlib import Path

from panelcad.update.feed import http_read, is_http_url, resolve_download
from panelcad.update.manifest import ReleaseManifest, UpdateError

_STAGE_PREFIX = "panelcad-update-"
_DRIVE = re.compile(r"^[A-Za-z]:")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, *, expected_sha256: str, expected_size: int | None) -> str:
    """Hash ``path``. On mismatch, delete it and raise. Returns the hex digest."""

    try:
        actual_size = path.stat().st_size
        digest = sha256_file(path)
    except OSError as exc:
        raise UpdateError(f"Could not read {path}: {exc}") from exc
    if expected_size is not None and actual_size != expected_size:
        path.unlink(missing_ok=True)
        raise UpdateError(
            "Refusing to install: size mismatch "
            f"(expected {expected_size} bytes, got {actual_size}). "
            "The file was not kept."
        )
    if digest.lower() != expected_sha256.lower():
        path.unlink(missing_ok=True)
        raise UpdateError(
            "Refusing to install: sha256 mismatch "
            f"(expected {expected_sha256.lower()}, got {digest}). The file was not kept."
        )
    return digest


def prune_stage_dirs(*, older_than: float = 24 * 3600) -> None:
    """Remove leftover ``panelcad-update-*`` folders. A failed install used to leave them."""

    root = Path(tempfile.gettempdir())
    cutoff = time.time() - older_than
    try:
        children = list(root.glob(f"{_STAGE_PREFIX}*"))
    except OSError:
        return
    for child in children:
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def cleanup_stage(archive: Path) -> None:
    """Delete a temp stage folder created by :func:`stage_installer`."""

    folder = archive.parent
    if folder.is_dir() and folder.name.startswith(_STAGE_PREFIX):
        shutil.rmtree(folder, ignore_errors=True)


def _download_name(source: str) -> str:
    """Last path segment. A Drive ``/uc?export=download&id=`` link is named ``uc``, not the query."""

    if is_http_url(source):
        path = urllib.parse.urlparse(source).path.rstrip("/")
        name = path.rsplit("/", 1)[-1]
    else:
        name = Path(source.replace("\\", "/")).name
    return name or "PanelCAD-setup.zip"


def stage_installer(
    manifest: ReleaseManifest,
    manifest_location: str,
    *,
    timeout: float = 60.0,
    ssl_context: ssl.SSLContext | None = None,
    dest_dir: Path | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Copy or download the zip into a temp folder and verify size and sha256."""

    source = resolve_download(manifest_location, manifest.download)
    owned = dest_dir is None
    folder = dest_dir or Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX))
    folder.mkdir(parents=True, exist_ok=True)
    filename = _download_name(source)
    dest = folder / filename
    try:
        if cancel is not None and cancel():
            raise UpdateError("Update cancelled.")
        if is_http_url(source):
            http_read(source, timeout=timeout, ssl_context=ssl_context, dest=dest, cancel=cancel)
        else:
            src = Path(source)
            if not src.is_file():
                raise UpdateError(f"Installer not found: {source}")
            try:
                shutil.copyfile(src, dest)
            except OSError as exc:
                raise UpdateError(f"Could not copy {source}: {exc}") from exc
        verify_file(dest, expected_sha256=manifest.sha256, expected_size=manifest.size)
    except Exception:
        if owned:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    return dest


def _zip_entry_escapes(name: str) -> bool:
    text = name.replace("\\", "/")
    if not text or text.startswith("/"):
        return True
    if _DRIVE.match(text):
        return True
    parts = [part for part in text.split("/") if part not in ("", ".")]
    return any(part == ".." for part in parts)


def safe_extract(archive: Path, dest: Path) -> Path:
    """Extract a setup zip. Refuse entries that would write outside ``dest``."""

    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename
                if _zip_entry_escapes(name):
                    raise UpdateError(f"Refusing zip entry that escapes the temp folder: {name}")
                target = (root / name.replace("\\", "/")).resolve()
                if root != target and root not in target.parents:
                    raise UpdateError(f"Refusing zip entry that escapes the temp folder: {name}")
            zf.extractall(root)
    except UpdateError:
        raise
    except zipfile.BadZipFile as exc:
        raise UpdateError(f"The download is not a zip file: {archive.name}") from exc
    except OSError as exc:
        raise UpdateError(f"Could not extract {archive}: {exc}") from exc
    return root


def find_installer_script(extracted: Path) -> Path:
    direct = extracted / "scripts" / "Install-PanelCAD.ps1"
    if direct.is_file():
        return direct
    matches = sorted(extracted.glob("*/scripts/Install-PanelCAD.ps1"))
    if len(matches) == 1:
        return matches[0]
    raise UpdateError(
        "The setup zip does not contain scripts/Install-PanelCAD.ps1. "
        "Publish the zip from scripts/build-release.py."
    )
