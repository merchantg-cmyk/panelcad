"""One settings file for every PanelCAD feature.

The directory is ``PANELCAD_CONFIG`` when that variable is set. Otherwise it
is the app-data folder (``PANELCAD_DATA_DIR``, or ``%LOCALAPPDATA%\\PanelCAD``
on Windows). The file is ``settings.json``. Logs stay in the app-data folder
even when ``PANELCAD_CONFIG`` points somewhere else.

Writes re-read the file, merge the patch, and replace it. A reader that holds
the file on Windows (``PermissionError``) is retried. A lock that hides the
bytes is not an empty document: the read fails, the cache stays, and nothing
is written. Keys this module does not understand stay on disk.

Shared keys:

- ``theme``: ``light``, ``dark``, or ``system``
- ``canvas``: ``paper``, ``white``, ``dark``, or ``auto``
- ``recent``: project paths, newest first, at most 10. File > Open Recent
  reads this list. It is not written under ``.panelcad`` or ``.grok``.
- ``phase_colors``: draw phase colours on the sheet (print stays unchanged)
- ``layout``: base64 ``QMainWindow.saveState``
- ``autosave_seconds``: recovery copies of unsaved work. ``0`` is off.
  ``PANELCAD_AUTOSAVE_SECONDS`` overrides the file at runtime.
- ``bug_reports``: ``webhook_url``, ``slack_bot_token``, ``slack_channel``,
  ``relay_url``, ``disabled``. A release may also embed an incoming webhook.
  Environment variables override the file. ``disabled`` or
  ``PANELCAD_BUG_REPORTS=off`` turns sending off.
- ``guidance``: ``show_hints`` (one-time tips), ``hints_seen``,
  ``next_steps_open``, plus welcome, tour, task checklists, and dismissed
  dialog notes. Progress stays in this file. It is not written into a project.
  Other keys in the file are left alone.
- ``ai``: datasheet lookup. ``datasheet_lookup``, ``provider``
  (``xai``, ``openai``, ``compatible``, ``ollama``), ``endpoint``,
  ``api_key``, and ``model``. ``PANELCAD_AI_LOOKUP``,
  ``PANELCAD_AI_PROVIDER``, ``PANELCAD_AI_ENDPOINT``,
  ``PANELCAD_AI_API_KEY``, and ``PANELCAD_AI_MODEL`` override the file.
  No API key is built in.
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path

_cache: dict | None = None
_cache_path: str | None = None
_seen: str | None = None
_seen_path: str | None = None
_generation = 0
_appearance_changed = False

_REPLACE_ATTEMPTS = 20
_REPLACE_WAIT_S = 0.025


def config_dir() -> Path:
    override = os.environ.get("PANELCAD_CONFIG", "").strip()
    if override:
        return Path(override)
    from panelcad.diag.paths import app_data_dir

    return app_data_dir()


def settings_path() -> Path:
    return config_dir() / "settings.json"


def generation() -> int:
    """Bumps when the file is written or the cache is dropped."""

    return _generation


def invalidate() -> None:
    """Drop the in-memory copy. The next read loads the file once.

    Does not forget the last bytes this process observed. A watcher event for
    those same bytes is not a new change.
    """

    global _cache, _cache_path, _generation
    _cache = None
    _cache_path = None
    _generation += 1


def peek() -> dict:
    """Cached document. Callers must not mutate it.

    A failed read keeps the last good copy for this path. It does not cache
    an empty document, which would turn a locked dark theme into light.
    """

    global _cache, _cache_path
    path = settings_path()
    key = str(path)
    if _cache is not None and _cache_path == key:
        return _cache
    data = read_disk()
    _cache = data
    _cache_path = key
    observe_file()
    return _cache


def load() -> dict:
    """Full settings document. A copy, so a caller can edit it."""

    return copy.deepcopy(peek())


def get(key: str, default=None):
    return peek().get(key, default)


def read_disk() -> dict:
    """The file as it is now. Does not use or update the cache."""

    return _read(settings_path())


def update(patch: dict) -> dict:
    """Merge ``patch`` into a fresh read of the file.

    Unknown keys already on disk are kept. The cache is not the source of
    the merge: another writer may have saved since this process last read.
    """

    global _cache, _cache_path, _seen, _seen_path, _generation
    path = settings_path()
    data = _read(path)
    data.update(patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2) + "\n"
    _replace(path, payload)
    _seen = payload
    _seen_path = str(path)
    _cache = data
    _cache_path = str(path)
    _generation += 1
    return copy.deepcopy(data)


def external_appearance_changed() -> bool:
    """True when the last consumed edit changed theme, canvas, recent, or phase."""

    return _appearance_changed


def observe_file() -> None:
    """Remember ``settings.json`` the first time this path exists.

    A missing file is not a baseline. Startup reads the file that is already
    there (the window's first ``peek``, or the settings watcher arming). A
    later edit is compared with that text. An autosave-only edit, including
    the first one after startup, does not look like a new theme.
    """

    global _seen, _seen_path
    path = settings_path()
    key = str(path)
    if _seen_path == key and _seen is not None:
        return
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    _seen = text
    _seen_path = key


def consume_external_change() -> bool:
    """True once when ``settings.json`` differs from the last text this process saw.

    Compares the file with the last observed bytes, not with the last bytes
    this process wrote. An outside edit that restores an older theme is a
    change. A second watcher event for the same bytes is not.

    The cache is dropped on every new text. ``external_appearance_changed``
    is true only when theme, canvas, recent paths, or phase colours differ.
    Until this path has been observed, the cached startup document is that
    baseline. A missing file is not recorded, so the first real read still is.
    """

    global _cache, _cache_path, _seen, _seen_path, _generation, _appearance_changed
    path = settings_path()
    key = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
        exists = False
    except OSError:
        # Sharing violation or another lock. The bytes are still the last
        # good document. Treating them as missing applies the default theme
        # and the next successful write stores that default.
        _appearance_changed = False
        return False
    else:
        exists = True
    if not exists and not (_seen_path == key and _seen):
        _appearance_changed = False
        return False
    if _seen_path == key and text == _seen:
        _appearance_changed = False
        return False
    if _seen_path == key and _seen is not None:
        previous = _appearance_signature(_seen)
    elif _cache_path == key and isinstance(_cache, dict):
        previous = _appearance_from_mapping(_cache)
    else:
        previous = _appearance_signature("")
    _appearance_changed = previous != _appearance_signature(text)
    _seen = text
    _seen_path = key
    _cache = None
    _cache_path = None
    _generation += 1
    return True


def _appearance_from_mapping(raw: dict) -> tuple:
    """Theme, canvas, phase, and recent paths. Other keys do not repaint the sheet."""

    recent = raw.get("recent")
    paths = tuple(str(item) for item in recent) if isinstance(recent, list) else ()
    return (raw.get("theme"), raw.get("canvas"), raw.get("phase_colors", True), paths)


def _appearance_signature(text: str) -> tuple:
    """Theme, canvas, phase, and recent paths. Other keys do not repaint the sheet."""

    try:
        raw = json.loads(text) if text else {}
    except (json.JSONDecodeError, UnicodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return _appearance_from_mapping(raw)


def _read(path: Path) -> dict:
    """Load ``path``. Missing or invalid JSON is an empty document.

    ``OSError`` (a Windows share-none lock, a sharing violation) propagates.
    ``Path.is_file`` hides that error and looks like a missing file, so this
    does not use it.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeError:
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _replace(path: Path, payload: str) -> None:
    """Write ``payload`` over ``path``. Retry when Windows denies the swap."""

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    last: PermissionError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            last = None
            break
        except PermissionError as exc:
            last = exc
            if attempt + 1 < _REPLACE_ATTEMPTS:
                time.sleep(_REPLACE_WAIT_S)
    if last is not None:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise last
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
