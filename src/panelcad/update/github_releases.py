"""Public-build updates from GitHub Releases.

The public repository is ``merchantg-cmyk/panelcad``.
``PANELCAD_GITHUB_REPOSITORY=owner/name`` overrides that default.
A shop release keeps the feed embedded at build time and does not use this
until that feed is absent.

A public zip (``scripts/build-release.py --public``) contains
``_release_public.txt``. With no environment feed, no saved feed, and no
shop feed, Check for updates reads the latest GitHub release, takes the
``PanelCAD-*-setup.zip`` asset and its ``.sha256`` sidecar, and the existing
prompt verifies that sha256 before install.
"""

from __future__ import annotations

import json
import os
import re
import ssl

from panelcad.update.feed import _allowed_url, http_read
from panelcad.update.manifest import ReleaseManifest, UpdateError, require_version

# Public source repository. PANELCAD_GITHUB_REPOSITORY overrides this.
PUBLIC_GITHUB_REPOSITORY = "merchantg-cmyk/panelcad"
GITHUB_RELEASES_FEED = "github-releases"
PUBLIC_MARKER_FILENAME = "_release_public.txt"
_REPOSITORY_ENV = "PANELCAD_GITHUB_REPOSITORY"
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_ZIP_NAME = re.compile(r"^PanelCAD-(.+)-setup\.zip$", re.IGNORECASE)
_SHA_LINE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def public_marker_path():
    """Marker file next to this module. Present only inside a public release zip."""

    from pathlib import Path

    return Path(__file__).resolve().with_name(PUBLIC_MARKER_FILENAME)


def is_public_release() -> bool:
    """True when this install was built with ``--public``."""

    path = public_marker_path()
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    for line in raw.splitlines():
        if line.strip():
            return line.strip().lower() == "public"
    return False


def is_github_releases_feed(value: str) -> bool:
    return value.strip().lower() == GITHUB_RELEASES_FEED


def public_github_repository() -> str:
    """``owner/name`` from the environment, otherwise ``PUBLIC_GITHUB_REPOSITORY``."""

    env = os.environ.get(_REPOSITORY_ENV, "").strip()
    if env:
        return env
    return PUBLIC_GITHUB_REPOSITORY


def repository_configured(value: str | None = None) -> bool:
    """True when the value is ``owner/name``. The default repository counts."""

    text = public_github_repository() if value is None else value
    try:
        repository_parts(text)
    except UpdateError:
        return False
    return True


def repository_parts(value: str) -> tuple[str, str]:
    """Split ``owner/name``. A full github.com URL is accepted and not echoed on failure."""

    text = value.strip().strip("/")
    lowered = text.lower()
    prefix = "https://github.com/"
    if lowered.startswith(prefix):
        text = text[len(prefix) :]
        text = text.split("?", 1)[0].split("#", 1)[0].strip("/")
    parts = [part for part in text.split("/") if part]
    if len(parts) != 2 or not _NAME.fullmatch(parts[0]) or not _NAME.fullmatch(parts[1]):
        raise UpdateError(
            "The public GitHub repository must be owner/name. "
            f"Set {_REPOSITORY_ENV}, or leave it unset to use {PUBLIC_GITHUB_REPOSITORY}."
        )
    return parts[0], parts[1]


def github_api_base() -> str:
    """Production API. Tests replace this with a loopback server."""

    return "https://api.github.com"


def releases_page(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}/releases/latest"


def _unconfigured() -> UpdateError:
    return UpdateError(
        "The public GitHub repository must be owner/name. "
        f"Unset {_REPOSITORY_ENV} to use {PUBLIC_GITHUB_REPOSITORY}."
    )


def load_github_release(
    *,
    timeout: float = 20.0,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[ReleaseManifest, str]:
    """Latest release as a manifest. The download URL is the setup zip asset."""

    if not repository_configured():
        raise _unconfigured()
    owner, repo = repository_parts(public_github_repository())
    api = f"{github_api_base().rstrip('/')}/repos/{owner}/{repo}/releases/latest"
    if not _allowed_url(api):
        raise UpdateError("The GitHub releases check only talks to https://api.github.com.")
    raw = http_read(api, timeout=timeout, ssl_context=ssl_context)
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("The GitHub release response was not JSON.") from exc
    if not isinstance(payload, dict):
        raise UpdateError("The GitHub release response was not a JSON object.")
    manifest = manifest_from_release(payload, timeout=timeout, ssl_context=ssl_context)
    return manifest, releases_page(owner, repo)


def manifest_from_release(
    payload: dict,
    *,
    timeout: float = 20.0,
    ssl_context: ssl.SSLContext | None = None,
) -> ReleaseManifest:
    """Pick the setup zip and read its sha256 sidecar. Does not download the zip."""

    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("The GitHub release has no files.")
    zips = []
    sidecars: dict[str, dict] = {}
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if _ZIP_NAME.match(name):
            zips.append(item)
        elif name.lower().endswith(".sha256"):
            sidecars[name.lower()] = item
    if not zips:
        raise UpdateError(
            "The GitHub release has no PanelCAD setup zip. "
            "Publish PanelCAD-<version>-setup.zip and its .sha256 file."
        )
    tag = str(payload.get("tag_name") or "").strip()
    tag_version = tag[1:] if tag.lower().startswith("v") else tag
    chosen = _choose_zip(zips, tag_version)
    name = str(chosen.get("name") or "")
    match = _ZIP_NAME.match(name)
    version = require_version(match.group(1) if match else tag_version, label="version")
    download = str(chosen.get("browser_download_url") or "").strip()
    if not download or not _allowed_url(download):
        raise UpdateError("The GitHub setup zip URL must be https.")
    sidecar = sidecars.get(f"{name.lower()}.sha256")
    if sidecar is None:
        raise UpdateError(
            "The GitHub release has no sha256 file next to the setup zip. "
            "The zip was not downloaded."
        )
    side_url = str(sidecar.get("browser_download_url") or "").strip()
    if not side_url or not _allowed_url(side_url):
        raise UpdateError("The GitHub sha256 file URL must be https.")
    side_raw = http_read(side_url, timeout=timeout, ssl_context=ssl_context)
    sha = _sha_text(side_raw)
    if not sha:
        raise UpdateError("The GitHub sha256 file does not contain 64 hex characters.")
    published = str(payload.get("published_at") or "")
    release_date = published[:10] if len(published) >= 10 and published[4] == "-" else ""
    notes = str(payload.get("body") or "").strip()
    if len(notes) > 8000:
        notes = notes[:7999] + "…"
    size = chosen.get("size")
    try:
        size_int = int(size) if size not in (None, "") else None
    except (TypeError, ValueError):
        size_int = None
    if size_int is not None and size_int < 0:
        size_int = None
    return ReleaseManifest(
        version=version,
        release_date=release_date,
        notes=notes,
        download=download,
        size=size_int,
        sha256=sha,
        min_version="",
    )


def _choose_zip(zips: list[dict], tag_version: str) -> dict:
    for item in zips:
        match = _ZIP_NAME.match(str(item.get("name") or ""))
        if match and match.group(1) == tag_version:
            return item
    return zips[0]


def _sha_text(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ""
    found = _SHA_LINE.search(text)
    return found.group(1).lower() if found else ""
