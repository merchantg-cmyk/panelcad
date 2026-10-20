"""Update settings under the user config directory (``~/.panelcad`` by default).

The feed is an explicit override, then ``PANELCAD_UPDATE_FEED``, then the saved
setting, then the URL embedded at build time. That URL is not in the
repository. ``PANELCAD_CONFIG_DIR`` redirects the whole directory so tests do
not write into a real home folder.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from panelcad.update.manifest import UpdateError

SETTINGS_NAME = "update-settings.json"
NOTICE_NAME = "update-notice.json"
ROLLBACK_NAME = "rollback.json"


DEFAULT_KEEP_BACKUPS = 3


def keep_backup_count(value: Any) -> int:
    """How many install backups to retain. Missing or unreadable values use the default."""

    if value is None or value == "":
        return DEFAULT_KEEP_BACKUPS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_KEEP_BACKUPS
    if parsed < 1:
        return 1
    return parsed


@dataclass
class UpdateSettings:
    feed: str = ""
    check_on_startup: bool = True
    last_check: str = ""
    skip_version: str = ""
    keep_backups: int = DEFAULT_KEEP_BACKUPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "feed": self.feed,
            "check_on_startup": self.check_on_startup,
            "last_check": self.last_check,
            "skip_version": self.skip_version,
            "keep_backups": keep_backup_count(self.keep_backups),
        }


def config_dir() -> Path:
    raw = os.environ.get("PANELCAD_CONFIG_DIR", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".panelcad"


def _dir(path: Path | None) -> Path:
    return path if path is not None else config_dir()


def settings_path(directory: Path | None = None) -> Path:
    return _dir(directory) / SETTINGS_NAME


def load_settings(directory: Path | None = None) -> UpdateSettings:
    path = settings_path(directory)
    if not path.is_file():
        return UpdateSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Could not read update settings {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError(f"Update settings {path} must be a JSON object.")
    return UpdateSettings(
        feed=str(data.get("feed") or "").strip(),
        check_on_startup=bool(data.get("check_on_startup", True)),
        last_check=str(data.get("last_check") or "").strip(),
        skip_version=str(data.get("skip_version") or "").strip(),
        keep_backups=keep_backup_count(data.get("keep_backups", DEFAULT_KEEP_BACKUPS)),
    )


def save_settings(settings: UpdateSettings, directory: Path | None = None) -> Path:
    path = settings_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def effective_feed(
    settings: UpdateSettings | None = None,
    override: str | None = None,
    directory: Path | None = None,
) -> str:
    """Feed to use: explicit override, then env, then the saved setting, then the built-in URL.

    A public install with none of those uses ``github-releases`` (the public
    GitHub Releases option). A shop feed embedded at build time still wins.
    """

    if override is not None and str(override).strip():
        return str(override).strip()
    env = os.environ.get("PANELCAD_UPDATE_FEED", "").strip()
    if env:
        return env
    current = settings if settings is not None else load_settings(directory)
    saved = current.feed.strip()
    if saved:
        return saved
    from panelcad.update.builtin import default_feed_url
    from panelcad.update.github_releases import GITHUB_RELEASES_FEED, is_public_release

    embedded = default_feed_url()
    if embedded:
        return embedded
    if is_public_release():
        return GITHUB_RELEASES_FEED
    return ""


def builtin_feed_active(settings: UpdateSettings) -> bool:
    """True when an empty saved feed is the one that falls through to the embedded URL."""

    if os.environ.get("PANELCAD_UPDATE_FEED", "").strip():
        return False
    if settings.feed.strip():
        return False
    from panelcad.update.builtin import default_feed_url

    return bool(default_feed_url())


def github_releases_active(settings: UpdateSettings) -> bool:
    """True when a public install would check GitHub Releases instead of a shop feed."""

    if os.environ.get("PANELCAD_UPDATE_FEED", "").strip():
        return False
    if settings.feed.strip():
        return False
    from panelcad.update.builtin import default_feed_url
    from panelcad.update.github_releases import is_public_release

    if default_feed_url():
        return False
    return is_public_release()


def parse_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise UpdateError(f"Unreadable update timestamp {value!r}.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now_stamp(moment: datetime | None = None) -> str:
    current = moment or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def startup_check_due(
    settings: UpdateSettings | None = None,
    *,
    now: datetime | None = None,
    feed: str | None = None,
    directory: Path | None = None,
) -> bool:
    """True when a startup check is allowed: feed set, setting on, and not within 24 hours."""

    current = settings if settings is not None else load_settings(directory)
    if not current.check_on_startup:
        return False
    chosen = feed if feed is not None else effective_feed(current, directory=directory)
    if not chosen:
        return False
    from panelcad.update.github_releases import is_github_releases_feed, repository_configured

    if is_github_releases_feed(chosen) and not repository_configured():
        return False
    last = parse_time(current.last_check)
    if last is None:
        return True
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc) - last >= timedelta(hours=24)


def mark_checked(directory: Path | None = None, *, now: datetime | None = None) -> None:
    settings = load_settings(directory)
    settings.last_check = utc_now_stamp(now)
    save_settings(settings, directory)


def set_skip_version(version: str, directory: Path | None = None) -> UpdateSettings:
    settings = load_settings(directory)
    settings.skip_version = version.strip()
    save_settings(settings, directory)
    return settings


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError(f"{path} must be a JSON object.")
    return data


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def notice_path(directory: Path | None = None) -> Path:
    return _dir(directory) / NOTICE_NAME


def write_notice(
    version: str,
    directory: Path | None = None,
    *,
    kind: str = "update",
    message: str = "",
    log: str = "",
) -> Path:
    path = notice_path(directory)
    payload: dict[str, Any] = {"version": version, "kind": kind}
    if message:
        payload["message"] = message
    if log:
        payload["log"] = log
    write_json_file(path, payload)
    return path


def consume_notice(directory: Path | None = None) -> dict[str, str] | None:
    """Return the notice and delete the file. None when there is no notice."""

    path = notice_path(directory)
    data = read_json_file(path)
    if data is None:
        return None
    version = str(data.get("version") or "").strip()
    if not version:
        return None
    kind = str(data.get("kind") or "update").strip() or "update"
    try:
        path.unlink()
    except OSError as exc:
        raise UpdateError(f"Could not clear the update notice {path}: {exc}") from exc
    return {
        "version": version,
        "kind": kind,
        "message": str(data.get("message") or ""),
        "log": str(data.get("log") or ""),
    }


def prune_backups(directory: Path | None = None, keep: int | None = None) -> list[str]:
    """Delete older install backups, keeping the newest ``keep`` directories."""

    folder = _dir(directory)
    root = folder / "backups"
    if not root.is_dir():
        return []
    count = keep_backup_count(
        keep if keep is not None else load_settings(folder).keep_backups
    )
    kids = [path for path in root.iterdir() if path.is_dir() and not path.is_symlink()]
    kids.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    removed: list[str] = []
    for old in kids[count:]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(str(old))
    return removed


def rollback_path(directory: Path | None = None) -> Path:
    return _dir(directory) / ROLLBACK_NAME


def read_rollback(directory: Path | None = None) -> dict[str, Any] | None:
    return read_json_file(rollback_path(directory))


def write_rollback(
    *,
    previous_version: str,
    backup: str,
    install_root: str,
    directory: Path | None = None,
    created: str | None = None,
) -> Path:
    path = rollback_path(directory)
    write_json_file(
        path,
        {
            "previous_version": previous_version,
            "backup": backup,
            "install_root": install_root,
            "created": created or utc_now_stamp(),
        },
    )
    return path
