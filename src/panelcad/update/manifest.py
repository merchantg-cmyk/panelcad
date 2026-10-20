"""JSON release manifest for a PanelCAD update feed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from panelcad.update.version import parse_version

FEED_FILENAME = "panelcad-feed.json"
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UpdateError(Exception):
    """A feed, download, or upgrade step that the user can be shown."""


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    release_date: str
    notes: str
    download: str
    size: int | None
    sha256: str
    min_version: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": self.version,
            "release_date": self.release_date,
            "notes": self.notes,
            "download": self.download,
            "sha256": self.sha256.lower(),
            "min_version": self.min_version,
        }
        if self.size is not None:
            data["size"] = self.size
        return data


def _text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in data and data[key] is not None:
            return str(data[key]).strip()
    return ""


def _size(data: dict[str, Any]) -> int | None:
    for key in ("size", "bytes", "length"):
        if key not in data or data[key] is None or data[key] == "":
            continue
        try:
            value = int(data[key])
        except (TypeError, ValueError) as exc:
            raise UpdateError(f"Manifest size must be an integer byte count ({key}).") from exc
        if value < 0:
            raise UpdateError("Manifest size cannot be negative.")
        return value
    return None


def require_version(text: str, *, label: str = "version") -> str:
    """Reject a value that is not a PEP 440 version, with a message instead of a traceback."""

    raw = str(text or "").strip()
    try:
        parse_version(raw)
    except ValueError as exc:
        raise UpdateError(
            f"{label} must be a PEP 440 version such as 0.5.0, not {raw!r}."
        ) from exc
    return raw


def require_release_date(text: str) -> str:
    """Empty is allowed. Anything else must be a real YYYY-MM-DD date."""

    raw = str(text or "").strip()
    if not raw:
        return ""
    if not _DATE.fullmatch(raw):
        raise UpdateError(f"release_date must be YYYY-MM-DD, not {raw!r}.")
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise UpdateError(f"release_date must be YYYY-MM-DD, not {raw!r}.") from exc
    return raw


def parse_manifest(data: dict[str, Any]) -> ReleaseManifest:
    """Parse one release object. Unknown keys are ignored here."""

    if not isinstance(data, dict):
        raise UpdateError("The release feed must be a JSON object.")
    version = _text(data, "version")
    download = _text(data, "download", "url", "file", "location")
    sha = _text(data, "sha256", "sha", "checksum").lower()
    if not version:
        raise UpdateError("The release feed is missing version.")
    if not download:
        raise UpdateError("The release feed is missing download.")
    if not _SHA.fullmatch(sha):
        raise UpdateError("The release feed sha256 must be 64 hex characters.")
    version = require_version(version, label="version")
    min_version = _text(data, "min_version", "minimum_supported_version", "min_supported_version")
    if min_version:
        min_version = require_version(min_version, label="min_version")
    return ReleaseManifest(
        version=version,
        release_date=require_release_date(_text(data, "release_date", "date", "released")),
        notes=_text(data, "notes", "body", "changelog"),
        download=download,
        size=_size(data),
        sha256=sha,
        min_version=min_version,
    )


def dump_manifest(manifest: ReleaseManifest, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge a manifest onto existing feed keys so extra fields survive an update."""

    merged: dict[str, Any] = dict(extra or {})
    merged.update(manifest.to_dict())
    return merged
