"""Unhandled errors, per-process session locks, and a recovery copy that is not the user's file."""

from __future__ import annotations

import faulthandler
import hashlib
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from panelcad.diag.log import log_event, tail_log
from panelcad.diag.paths import (
    crashes_dir,
    ensure_dir,
    fault_log_path,
    recovery_dir,
    session_marker_path,
    sessions_dir,
)
from panelcad.diag.redact import redact_text
from panelcad.diag.report import local_time_text, system_info
from panelcad.version import __version__

RECOVERY_KEEP_DAYS = 14
RECOVERY_KEEP_NEWEST = 20
HEARTBEAT_SECONDS = 60
STILL_ACTIVE = 259
_RECOVERY_SIDECAR_GLOBS = (
    "*.ai.jsonl",
    "*.ai-in.jsonl",
    "*.ai-in.cursor",
    "*.ai-prompt.jsonl",
    "*.ai-prompt.cursor",
)

_started = False
_fatal = False
_handling = False
_hooks = False
_gui = False
_dialogs = 0
_recovery_session: Any = None
_prev_sys = None
_prev_thread = None
_prev_qt = None
_fault_handle = None
_original_invoke = None
_process_started = ""
_lock_path: Path | None = None
_clean_tokens: dict[int, str] = {}
_autosave_tokens: dict[int, str] = {}
_maintenance_stop = threading.Event()
_maintenance_thread: threading.Thread | None = None
_file_lock = threading.Lock()


@dataclass
class UncleanExit:
    reasons: list[str] = field(default_factory=list)
    session: dict[str, Any] | None = None
    fault_text: str = ""
    crash_path: str = ""
    crash_id: str = ""
    recovery_path: str = ""
    when: str = ""
    project_path: str = ""
    project_name: str = ""
    lock_paths: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reasons": list(self.reasons),
            "session": self.session,
            "fault_text": self.fault_text,
            "crash_path": self.crash_path,
            "crash_id": self.crash_id,
            "recovery_path": self.recovery_path,
            "when": self.when,
            "project_path": self.project_path,
            "project_name": self.project_name,
            "lock_paths": list(self.lock_paths),
        }


def dialogs_shown() -> int:
    return _dialogs


def crash_was_fatal() -> bool:
    return _fatal


def recovery_session() -> Any:
    return _recovery_session


def set_recovery_session(session: Any) -> None:
    """Bind the process's primary drawing. Bug-report helpers must not call this."""

    global _recovery_session
    _recovery_session = session
    if session is None or not _started:
        return
    path = getattr(session, "path", None)
    project = getattr(session, "project", None)
    name = str(getattr(project, "name", "") or "") if project is not None else ""
    _update_lock(
        {
            "project_path": "" if path is None else str(path),
            "project_name": name.strip(),
        }
    )


def current_lock_path() -> Path | None:
    return _lock_path


def remember_saved(session: Any) -> None:
    """The drawing matches the user's file. Autosave skips it until it changes."""

    if session is None:
        return
    _clean_tokens[id(session)] = _project_token(session)


def reset_state() -> None:
    """Restore hooks and forget this process. Does not delete files in the data dir."""

    global _started, _fatal, _handling, _hooks, _gui, _dialogs, _recovery_session
    global _prev_sys, _prev_thread, _prev_qt, _fault_handle, _original_invoke
    global _process_started, _lock_path
    _stop_maintenance()
    if _hooks:
        if _prev_sys is not None:
            sys.excepthook = _prev_sys
        if _prev_thread is not None:
            threading.excepthook = _prev_thread
        _hooks = False
    if getattr(_install_qt, "done", False):
        try:
            from PySide6.QtCore import qInstallMessageHandler

            qInstallMessageHandler(_prev_qt)
        except Exception:  # noqa: BLE001
            pass
        _install_qt.done = False  # type: ignore[attr-defined]
    _restore_click()
    _close_fault_handle()
    try:
        from panelcad.diag.runtime import reset_process_diagnostics

        reset_process_diagnostics()
    except Exception:  # noqa: BLE001
        pass
    _started = False
    _fatal = False
    _handling = False
    _gui = False
    _dialogs = 0
    _recovery_session = None
    _prev_sys = None
    _prev_thread = None
    _prev_qt = None
    _original_invoke = None
    _process_started = ""
    _lock_path = None
    _clean_tokens.clear()
    _autosave_tokens.clear()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _close_fault_handle() -> None:
    """Disable faulthandler before closing the file it writes to."""

    global _fault_handle
    try:
        faulthandler.disable()
    except Exception:  # noqa: BLE001
        pass
    handle = _fault_handle
    _fault_handle = None
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def _restore_click() -> None:
    global _original_invoke
    if _original_invoke is None:
        return
    try:
        import click

        click.core.Command.invoke = _original_invoke
    except Exception:  # noqa: BLE001
        pass
    _original_invoke = None


def abandon_session() -> None:
    """Pretend this process died. Its lock file stays for the next start."""

    global _started
    _stop_maintenance()
    _started = False
    _close_fault_handle()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _consume_fault_text() -> str:
    path = fault_log_path()
    if not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not data.strip():
        return ""
    try:
        path.write_bytes(b"")
    except OSError:
        pass
    return redact_text(data.decode("utf-8", errors="replace"))


def _iter_lock_files() -> list[Path]:
    found: list[Path] = []
    folder = sessions_dir()
    if folder.is_dir():
        found.extend(path for path in folder.glob("*.json") if path.is_file())
    legacy = session_marker_path()
    if legacy.is_file():
        found.append(legacy)
    return found


def _safe_token(token: str) -> str:
    safe = "".join(ch if ch.isalnum() else "-" for ch in token).strip("-")
    return (safe[:48] or "session")


def process_start_token(pid: int) -> str | None:
    """Start id for ``pid``. ``None`` when that process is not running.

    A recycled pid has a different start id, so an old lock is not treated as live.
    """

    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_start(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start(pid)
    return _ps_process_start(pid)


def _linux_process_start(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = text.rfind(")")
    if end < 0:
        return None
    fields = text[end + 2 :].split()
    # starttime is field 22. After the comm field, index 0 is field 3.
    if len(fields) <= 19:
        return None
    token = fields[19]
    return token or None


def _windows_process_start(pid: int, kernel32: Any = None) -> str | None:
    """Creation time of a still-running process, or None.

    ``OpenProcess`` and ``GetProcessTimes`` succeed for an exited process while
    any handle to it is open. ``GetExitCodeProcess`` must be ``STILL_ACTIVE``.
    ``kernel32`` is injectable so this check can run without Windows.
    """

    import ctypes
    from ctypes import wintypes

    process_query = 0x1000
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if int(code.value) != STILL_ACTIVE:
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        low = int(created.dwLowDateTime) & 0xFFFFFFFF
        high = int(created.dwHighDateTime) & 0xFFFFFFFF
        return str((high << 32) | low)
    finally:
        kernel32.CloseHandle(handle)


def _ps_process_start(pid: int) -> str | None:
    import subprocess

    try:
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(out.split())
    return text or None


def _looks_like_legacy_stamp(value: str) -> bool:
    """Old locks stored a UTC timestamp in ``started``, not a process start id."""

    return "T" in value and (value.endswith("Z") or "+" in value[10:])


def _lock_is_live(data: dict[str, Any]) -> bool:
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return False
    token = process_start_token(pid)
    if token is None:
        return False
    started = str(data.get("started") or "")
    if started == token:
        return True
    if _looks_like_legacy_stamp(started):
        return True
    return False


def _is_our_lock(path: Path, data: dict[str, Any]) -> bool:
    if _lock_path is not None:
        try:
            if path.resolve() == _lock_path.resolve():
                return True
        except OSError:
            if path == _lock_path:
                return True
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return False
    return pid == os.getpid() and str(data.get("started") or "") == _process_started


def _mtime_iso(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _update_lock(updates: dict[str, Any] | None = None) -> None:
    if _lock_path is None or not _process_started:
        return
    with _file_lock:
        current = _read_json(_lock_path) if _lock_path.is_file() else {}
        payload: dict[str, Any] = {
            "pid": os.getpid(),
            "started": _process_started,
            "active": _now(),
            "version": __version__,
            "gui": bool(_gui),
            "project_path": str(current.get("project_path") or ""),
            "project_name": str(current.get("project_name") or ""),
            "recovery_path": str(current.get("recovery_path") or ""),
        }
        if updates:
            for key, value in updates.items():
                if key in {"pid", "started"}:
                    continue
                payload[key] = value
        payload["pid"] = os.getpid()
        payload["started"] = _process_started
        ensure_dir(_lock_path.parent)
        _lock_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _remove_our_lock() -> None:
    path = _lock_path
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def note_open_project(path: Path | str | None, name: str = "", *, session: Any = None) -> None:
    """Remember the open drawing on this process's lock."""

    if not _started:
        return
    if session is not None and session is not _recovery_session:
        return
    _update_lock(
        {
            "project_path": "" if path is None else str(path),
            "project_name": str(name or "").strip(),
        }
    )


def plain_when(stamp: str, *, tz: tzinfo | None = None) -> str:
    """Local time on this PC, with a short zone abbreviation."""

    return local_time_text(stamp, tz=tz)


def recovery_sentence(
    *,
    project_path: str = "",
    project_name: str = "",
    recovery_path: str = "",
    recovery_exists: bool = False,
) -> str:
    """One or two plain sentences for the crash dialog."""

    project_path = str(project_path or "").strip()
    project_name = str(project_name or "").strip()
    recovery_path = str(recovery_path or "").strip()
    open_project = bool(project_path or project_name)
    if recovery_exists and recovery_path:
        if project_name and project_path:
            return (
                f"A recovery copy of {project_name} was saved at {recovery_path}.\n"
                f"Project file: {project_path}"
            )
        if project_name:
            return f"A recovery copy of {project_name} was saved at {recovery_path}."
        if project_path:
            return (
                f"A recovery copy of the open project was saved at {recovery_path}.\n"
                f"Project file: {project_path}"
            )
        return f"A recovery copy was saved at {recovery_path}."
    if not open_project:
        return "No project was open."
    shown = project_name or "The project"
    if project_path:
        return f"{shown} was open ({project_path}). A recovery copy was not saved."
    return f"{shown} was open. A recovery copy was not saved."


def enable_fault_log() -> None:
    global _fault_handle
    _close_fault_handle()
    path = fault_log_path()
    ensure_dir(path.parent)
    handle = open(path, "ab", buffering=0)  # noqa: SIM115 — faulthandler keeps it
    _fault_handle = handle
    try:
        faulthandler.enable(file=handle, all_threads=True)
    except Exception:  # noqa: BLE001
        log_event("error", "faulthandler enable failed")


def begin_session(*, gui: bool = False) -> UncleanExit | None:
    """Take this process's lock. A GUI start reports locks whose process is gone.

    The CLI and the agent write their own lock and do not read or delete a GUI lock.
    Dead locks stay on disk until :func:`acknowledge_unclean`.
    """

    global _started, _gui, _process_started, _lock_path
    _gui = gui
    token = process_start_token(os.getpid()) or _now()
    _process_started = token
    _lock_path = ensure_dir(sessions_dir()) / f"{os.getpid()}-{_safe_token(token)}.json"
    unclean = _collect_unclean() if gui else None
    _update_lock()
    enable_fault_log()
    install_hooks(gui=gui)
    _started = True
    _start_maintenance()
    log_event("session", "started")
    prune_recovery_files()
    return unclean


def mark_clean_exit(*, force: bool = False) -> None:
    """Clear this process's lock after a normal exit. A fatal crash keeps it unless ``force``.

    ``force`` is the window closing or a CLI success path. It does not invent a
    session that never started, and it does not delete another process's lock.
    """

    global _started, _fatal
    if not _started:
        return
    if _fatal and not force:
        return
    _stop_maintenance()
    _remove_our_lock()
    _close_fault_handle()
    path = fault_log_path()
    try:
        if path.is_file() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass
    _started = False
    if force:
        _fatal = False
    log_event("session", "clean exit")


def acknowledge_unclean(info: UncleanExit | None = None) -> None:
    """Delete the dead locks the user was just shown. Leave live locks alone."""

    if info is None:
        return
    for raw in list(info.lock_paths):
        path = Path(raw)
        if _lock_path is not None:
            try:
                if path.resolve() == _lock_path.resolve():
                    continue
            except OSError:
                if path == _lock_path:
                    continue
        data = _read_json(path)
        if data and _lock_is_live(data):
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _collect_unclean() -> UncleanExit | None:
    dead: list[tuple[Path, dict[str, Any]]] = []
    for path in _iter_lock_files():
        data = _read_json(path)
        if _is_our_lock(path, data):
            continue
        if data and _lock_is_live(data):
            continue
        dead.append((path, data))
    fault = _consume_fault_text()
    if not dead and not fault.strip():
        return None
    chosen_path: Path | None = None
    chosen: dict[str, Any] = {}
    if dead:
        def _rank(item: tuple[Path, dict[str, Any]]) -> tuple[str, float]:
            path, data = item
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            return str(data.get("active") or ""), mtime

        chosen_path, chosen = max(dead, key=_rank)
    recovery = str(chosen.get("recovery_path") or "").strip()
    if recovery and not Path(recovery).is_file():
        recovery = ""
    when = str(chosen.get("active") or "").strip()
    if not when and chosen_path is not None:
        when = _mtime_iso(chosen_path)
    reasons: list[str] = []
    if dead:
        reasons.append("session")
    if fault.strip():
        reasons.append("faulthandler")
    info = UncleanExit(
        reasons=reasons,
        session=chosen or None,
        fault_text=fault,
        recovery_path=recovery,
        when=when,
        project_path=str(chosen.get("project_path") or ""),
        project_name=str(chosen.get("project_name") or ""),
        lock_paths=[str(path) for path, _data in dead],
    )
    if fault.strip():
        recovery_file = Path(recovery) if recovery else None
        record = _write_crash_file(
            source="faulthandler",
            exception="faulthandler",
            tb=fault,
            fatal=False,
            recovery=recovery_file,
        )
        info.crash_id = str(record.get("id") or "")
        info.crash_path = str(record.get("path") or "")
    return info


def _referenced_recovery_paths() -> set[str]:
    found: set[str] = set()
    for path in _iter_lock_files():
        raw = str(_read_json(path).get("recovery_path") or "").strip()
        if not raw:
            continue
        found.add(raw)
        try:
            found.add(str(Path(raw).resolve()))
        except OSError:
            pass
    return found


def _prune_recovery_sidecars(folder: Path) -> list[Path]:
    """Remove AI journals that landed next to a recovery copy.

    Those files are not the user's project journal. Prune used to delete only
    ``*.pcad.yaml``, so a ``.pcad.ai.jsonl`` stayed behind forever.
    """

    removed: list[Path] = []
    for pattern in _RECOVERY_SIDECAR_GLOBS:
        for path in folder.glob(pattern):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    return removed


def prune_recovery_files(
    *,
    keep_days: int = RECOVERY_KEEP_DAYS,
    keep_newest: int = RECOVERY_KEEP_NEWEST,
    protect: Path | None = None,
) -> list[Path]:
    """Drop recovery copies older than 14 days or outside the newest 20.

    A copy named by any session lock is kept so the next start can still offer it.
    Sidecars in this folder are always removed: recovery copies are not projects.
    """

    folder = recovery_dir()
    if not folder.is_dir():
        return []
    files = [path for path in folder.glob("*.pcad.yaml") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    referenced = _referenced_recovery_paths()
    protected = ""
    if protect is not None:
        protected = str(protect)
        try:
            protected = str(protect.resolve())
        except OSError:
            pass
    now = time.time()
    removed: list[Path] = []
    for index, path in enumerate(files):
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in referenced or str(path) in referenced or resolved == protected:
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if index >= keep_newest or age > keep_days * 86400:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                pass
    removed.extend(_prune_recovery_sidecars(folder))
    return removed


def _project_token(session: Any) -> str:
    project = getattr(session, "project", None)
    if project is None or not hasattr(project, "model_dump"):
        return ""
    try:
        payload = project.model_dump(mode="json")
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _autosave_path() -> Path | None:
    if not _process_started:
        return None
    name = f"autosave-{os.getpid()}-{_safe_token(_process_started)}.pcad.yaml"
    return ensure_dir(recovery_dir()) / name


def heartbeat_now() -> None:
    """Refresh the lock's last-active time. Does not write the drawing."""

    if not _started:
        return
    _update_lock({})


def autosave_now() -> Path | None:
    """Copy the primary drawing into the recovery folder when it has unsaved changes.

    Interval 0 skips the write. The user's own file is never the destination.
    """

    from panelcad.diag.settings import autosave_seconds

    if autosave_seconds() <= 0:
        return None
    session = _recovery_session
    if session is None or not hasattr(session, "project"):
        return None
    token = _project_token(session)
    if not token:
        return None
    if _clean_tokens.get(id(session)) == token:
        return None
    if _autosave_tokens.get(id(session)) == token:
        return None
    dest = _autosave_path()
    if dest is None:
        return None
    user_path = getattr(session, "path", None)
    try:
        if user_path is not None and dest.resolve() == Path(user_path).resolve():
            return None
    except OSError:
        return None
    try:
        from panelcad.io.yaml_format import save_project

        save_project(session.project, dest)
    except Exception as exc:  # noqa: BLE001
        log_event("error", f"autosave {type(exc).__name__}")
        return None
    try:
        if user_path is not None and dest.resolve() == Path(user_path).resolve():
            return None
    except OSError:
        return None
    _autosave_tokens[id(session)] = token
    _remember_recovery(dest)
    log_event("autosave", str(dest))
    return dest


def _stop_maintenance() -> None:
    global _maintenance_thread
    _maintenance_stop.set()
    thread = _maintenance_thread
    _maintenance_thread = None
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2.0)
    _maintenance_stop.clear()


def _start_maintenance() -> None:
    global _maintenance_thread
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    if _maintenance_thread is not None and _maintenance_thread.is_alive():
        return
    _maintenance_stop.clear()
    thread = threading.Thread(target=_maintenance_loop, name="panelcad-autosave", daemon=True)
    _maintenance_thread = thread
    thread.start()


def _maintenance_loop() -> None:
    from panelcad.diag.settings import autosave_seconds

    last_beat = time.monotonic()
    last_save = time.monotonic()
    while not _maintenance_stop.wait(1.0):
        now = time.monotonic()
        if now - last_beat >= HEARTBEAT_SECONDS:
            try:
                heartbeat_now()
            except Exception:  # noqa: BLE001
                pass
            last_beat = now
        interval = autosave_seconds()
        if interval > 0 and now - last_save >= interval:
            try:
                autosave_now()
            except Exception:  # noqa: BLE001
                pass
            last_save = now


def record_exception(
    exc: BaseException,
    *,
    source: str,
    tb: str | None = None,
    fatal: bool = False,
) -> dict[str, str]:
    if tb is None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    text = _safe_error(exc)
    log_event("error", f"{source} {text}")
    recovery = _write_recovery()
    _remember_recovery(recovery)
    return _write_crash_file(
        source=source,
        exception=text,
        tb=tb,
        fatal=fatal,
        recovery=recovery,
    )


def _safe_error(exc: BaseException) -> str:
    text = redact_text(f"{type(exc).__name__}: {exc}")
    if "\n" in text or len(text) > 180:
        return type(exc).__name__
    return text


def _write_crash_file(
    *,
    source: str,
    exception: str,
    tb: str,
    fatal: bool,
    recovery: Path | None,
) -> dict[str, str]:
    global _fatal
    if fatal:
        _fatal = True
    crash_id = _new_id()
    project_path = ""
    project_name = ""
    session = _recovery_session
    if session is not None and getattr(session, "path", None):
        project_path = str(session.path)
    project = getattr(session, "project", None)
    if project is not None:
        project_name = str(getattr(project, "name", "") or "")
    record = {
        "id": crash_id,
        "source": source,
        "when": _now(),
        "version": __version__,
        "system": system_info(),
        "exception": redact_text(exception),
        "traceback": redact_text(tb),
        "recent_log": redact_text(tail_log()),
        "recovery_path": str(recovery) if recovery else "",
        "project_path": project_path,
        "project_name": project_name,
    }
    dest = ensure_dir(crashes_dir()) / f"{crash_id}.json"
    dest.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {
        "id": crash_id,
        "path": str(dest),
        "recovery_path": record["recovery_path"],
        "project_path": project_path,
        "project_name": project_name,
    }


def _remember_recovery(path: Path | None) -> None:
    if path is None:
        return
    _update_lock({"recovery_path": str(path)})
    prune_recovery_files(protect=path)


def _write_recovery() -> Path | None:
    session = _recovery_session
    if session is None or not hasattr(session, "project"):
        return None
    try:
        from panelcad.io.yaml_format import save_project
    except Exception:  # noqa: BLE001
        return None
    dest = ensure_dir(recovery_dir()) / f"recovery-{_new_id()}.pcad.yaml"
    user_path = getattr(session, "path", None)
    try:
        if user_path is not None and dest.resolve() == Path(user_path).resolve():
            return None
    except OSError:
        return None
    try:
        save_project(session.project, dest)
    except Exception as exc:  # noqa: BLE001
        log_event("error", f"recovery {type(exc).__name__}")
        return None
    try:
        if user_path is not None and dest.resolve() == Path(user_path).resolve():
            return None
    except OSError:
        return None
    log_event("recovery", str(dest))
    return dest


def install_hooks(*, gui: bool) -> None:
    global _prev_sys, _prev_thread, _hooks, _gui
    _gui = gui
    if not _hooks:
        _prev_sys = sys.excepthook
        sys.excepthook = _sys_hook
        _prev_thread = threading.excepthook
        threading.excepthook = _thread_hook
        _hooks = True
    if gui:
        _install_qt()


def install_cli_exception_hook() -> None:
    """Record an exception Click would otherwise print and turn into SystemExit."""

    global _original_invoke
    import click

    current = click.core.Command.invoke
    if getattr(current, "_panelcad_guard", False):
        return
    _original_invoke = current

    def invoke(self, ctx):  # type: ignore[no-untyped-def]
        try:
            return _original_invoke(self, ctx)
        except Exception as exc:
            if isinstance(exc, (click.ClickException, click.exceptions.Exit)):
                raise
            record_exception(exc, source="cli", fatal=True)
            raise

    invoke._panelcad_guard = True  # type: ignore[attr-defined]
    click.core.Command.invoke = invoke


def _sys_hook(etype, value, tb) -> None:  # type: ignore[no-untyped-def]
    global _handling
    if _handling:
        if _prev_sys is not None:
            _prev_sys(etype, value, tb)
        return
    _handling = True
    try:
        text = "".join(traceback.format_exception(etype, value, tb))
        exc = value if isinstance(value, BaseException) else RuntimeError(str(value))
        record = record_exception(exc, source="sys", tb=text, fatal=True)
        before = _dialogs
        if _gui:
            _present_crash(record)
        if _dialogs == before and _prev_sys is not None:
            _prev_sys(etype, value, tb)
    finally:
        _handling = False


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    try:
        text = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        exc = args.exc_value if isinstance(args.exc_value, BaseException) else RuntimeError("thread")
        record_exception(exc, source="thread", tb=text, fatal=False)
    except Exception:  # noqa: BLE001
        pass
    if _prev_thread is not None:
        _prev_thread(args)


_QT_UNSET = object()
_qt_depth = 0


def _install_qt() -> None:
    global _prev_qt
    if _prev_qt is not _QT_UNSET and _prev_qt is not None:
        return
    if getattr(_install_qt, "done", False):
        return
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except Exception:  # noqa: BLE001
        return
    _prev_qt = qInstallMessageHandler(_qt_handler)
    _install_qt.done = True  # type: ignore[attr-defined]


def _qt_handler(mode, context, message) -> None:  # type: ignore[no-untyped-def]
    global _qt_depth
    if _qt_depth:
        return
    _qt_depth += 1
    try:
        from PySide6.QtCore import QtMsgType

        text = redact_text(str(message))
        if mode == QtMsgType.QtWarningMsg:
            log_event("qt", f"warning {text}", level=logging.WARNING)
        elif mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            log_event("qt", f"error {text}", level=logging.ERROR)
            record_exception(
                RuntimeError(text),
                source="qt",
                tb=f"Qt message: {text}",
                fatal=mode == QtMsgType.QtFatalMsg,
            )
    except Exception:  # noqa: BLE001
        pass
    finally:
        _qt_depth -= 1
    if _prev_qt is not None and _prev_qt is not _QT_UNSET:
        _prev_qt(mode, context, message)


def crash_dialog_parent() -> Any:
    """The open main window, so the crash dialog does not build its own project."""

    try:
        from PySide6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        return None
    app = QApplication.instance()
    if app is None:
        return None
    try:
        from panelcad.gui.main_window import MainWindow
    except Exception:  # noqa: BLE001
        return None
    widgets = list(app.topLevelWidgets())
    for widget in widgets:
        if isinstance(widget, MainWindow) and widget.isVisible():
            return widget
    for widget in widgets:
        if isinstance(widget, MainWindow):
            return widget
    return None


def _present_crash(record: dict[str, str]) -> None:
    global _dialogs
    try:
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is None:
            return
        from panelcad.gui.bug_report_dialog import CrashDialog
        from panelcad.gui.splash import close_splash

        close_splash()
        _dialogs += 1
        dlg = CrashDialog(crash_dialog_parent(), record)
        dlg.exec()
    except Exception as exc:  # noqa: BLE001
        log_event("error", f"crash dialog {type(exc).__name__}")


def load_crash(path: Path | str) -> dict[str, Any]:
    dest = Path(path)
    data = json.loads(dest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return data
