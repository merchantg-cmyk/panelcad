"""Per-user folders for logs, crash records, and recovery.

Settings use this folder too, unless ``PANELCAD_CONFIG`` is set. The settings
file itself is ``panelcad.settings.settings_path``. ``PANELCAD_DATA_DIR``
overrides the folder for tests and portable runs. On Windows the default is
``%LOCALAPPDATA%\\PanelCAD``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "PanelCAD"


def app_data_dir() -> Path:
    override = os.environ.get("PANELCAD_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if not base:
            base = str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / APP_DIR_NAME
    return Path.home() / ".local" / "share" / APP_DIR_NAME


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def reports_dir() -> Path:
    return app_data_dir() / "reports"


def crashes_dir() -> Path:
    return logs_dir() / "crashes"


def recovery_dir() -> Path:
    return logs_dir() / "recovery"


def is_under_recovery(path: Path | str | None) -> bool:
    """True when ``path`` is the recovery folder or a file inside it."""

    if path is None or not str(path).strip():
        return False
    try:
        candidate = Path(path).resolve()
        root = recovery_dir().resolve()
    except OSError:
        return False
    return candidate == root or root in candidate.parents


def settings_path() -> Path:
    """The shared settings file. ``PANELCAD_CONFIG`` wins over the app-data folder."""

    from panelcad.settings import settings_path as canonical

    return canonical()


def fault_log_path() -> Path:
    return logs_dir() / "fault.log"


def sessions_dir() -> Path:
    """One lock file per process: ``sessions/<pid>-<start>.json``."""

    return logs_dir() / "sessions"


def session_marker_path() -> Path:
    """Legacy single lock. New sessions write under :func:`sessions_dir`."""

    return logs_dir() / "session.lock"


def log_file_path() -> Path:
    return logs_dir() / "panelcad.log"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
