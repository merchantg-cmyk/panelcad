"""Write a release-feed manifest for a setup zip."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from panelcad.update.manifest import (
    FEED_FILENAME,
    ReleaseManifest,
    UpdateError,
    dump_manifest,
    parse_manifest,
    require_release_date,
    require_version,
)

_ZIP_VERSION = re.compile(r"^PanelCAD-(?P<version>.+)-setup\.zip$", re.IGNORECASE)
_VERSION_PY = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_from_zip(path: Path) -> str:
    match = _ZIP_VERSION.match(path.name)
    if match:
        return require_version(match.group("version"), label="version")
    try:
        with zipfile.ZipFile(path) as zf:
            names = [name for name in zf.namelist() if name.endswith("src/panelcad/version.py")]
            if not names:
                raise UpdateError(f"Could not find a version in {path.name}. Pass --version.")
            text = zf.read(names[0]).decode("utf-8")
    except UpdateError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError(f"Could not read {path}: {exc}") from exc
    found = _VERSION_PY.search(text)
    if not found:
        raise UpdateError(f"{path.name} has no __version__. Pass --version.")
    return require_version(found.group(1), label="version")


def feed_manifest_path(feed: str) -> tuple[Path, bool]:
    """Return ``(manifest_path, feed_is_directory)``."""

    path = Path(feed)
    if path.exists() and path.is_dir():
        return path / FEED_FILENAME, True
    if path.suffix.lower() == ".json":
        return path, False
    return path / FEED_FILENAME, True


def _relative_download(zip_path: Path, manifest_path: Path) -> str:
    try:
        relative = zip_path.resolve().relative_to(manifest_path.parent.resolve())
    except ValueError:
        return str(zip_path.resolve())
    return relative.as_posix()


def publish_feed(
    *,
    zip_path: Path,
    feed: str,
    notes: str = "",
    release_date: str | None = None,
    min_version: str | None = None,
    download: str | None = None,
    version: str | None = None,
    copy_zip: bool = True,
) -> Path:
    """Compute sha256 and write or update the feed manifest. Returns the manifest path."""

    archive = Path(zip_path)
    if not archive.is_file():
        raise UpdateError(f"Installer zip not found: {archive}")
    if archive.suffix.lower() != ".zip":
        raise UpdateError(f"Expected a .zip installer, got {archive.name}.")
    resolved_version = require_version(
        (version or "").strip() or version_from_zip(archive),
        label="version",
    )
    if min_version:
        min_version = require_version(min_version, label="min_version")
    when = require_release_date(
        (release_date or "").strip() or datetime.now(timezone.utc).date().isoformat()
    )
    manifest_path, feed_is_dir = feed_manifest_path(feed)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if feed_is_dir and copy_zip:
        dest_zip = manifest_path.parent / archive.name
        if archive.resolve() != dest_zip.resolve():
            shutil.copy2(archive, dest_zip)
        archive_for_link = dest_zip
    else:
        archive_for_link = archive
    download_value = (download or "").strip() or _relative_download(archive_for_link, manifest_path)
    extra: dict = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UpdateError(f"Could not read existing feed {manifest_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise UpdateError(f"Existing feed {manifest_path} must be a JSON object.")
        extra = loaded
        if min_version is None and str(extra.get("min_version") or "").strip():
            min_version = str(extra.get("min_version"))
    manifest = ReleaseManifest(
        version=resolved_version,
        release_date=when,
        notes=notes,
        download=download_value,
        size=archive_for_link.stat().st_size,
        sha256=sha256_file(archive_for_link),
        min_version=min_version or "",
    )
    payload = dump_manifest(manifest, extra)
    # Validate the document we are about to publish.
    parse_manifest(payload)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return manifest_path
