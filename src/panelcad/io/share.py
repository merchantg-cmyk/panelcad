"""Shared-folder locks, conflict checks, and sync-safe saves.

No server and no account. A synced directory is the only shared state.
See ``docs/SHARED_FOLDERS.md``.
"""

from __future__ import annotations

import difflib
import getpass
import hashlib
import json
import logging
import os
import platform
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from panelcad.diag.paths import is_under_recovery
from panelcad.io.winpath import io_path

ACTIVITY_KEEP = 100
BACKUP_KEEP = 30
LOCK_STALE_AFTER = timedelta(minutes=20)
SHARE_MARKER = ".panelcad-share"
# Sleeps between retries when a sync client or antivirus still holds the file.
# They add up to a little over 8 seconds. NTFS returns WinError 5 for the whole
# time another handle is open, even a share-read/write handle, so the old
# ~1.5 second budget gave up while the file was still busy.
REPLACE_PAUSES = (0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.5, 2.0, 2.0)
# The lock file is rewritten from the heartbeat timer. A full save-sized wait
# would freeze the window, so a lock replace stops after this shorter budget.
LOCK_PAUSES = (0.05, 0.1, 0.2)
# The disk watcher runs on the GUI timer (about every 400 ms). One attempt and
# no sleep. A save still uses REPLACE_PAUSES. The sum must stay at or under 50 ms.
WATCH_PAUSES: tuple[float, ...] = ()

log = logging.getLogger("panelcad.io.share")

_guard = threading.Lock()
_file_guard = threading.Lock()
_counts: dict[str, int] = {}


class ShareError(Exception):
    """A save was refused so two editors would not destroy one file."""


@dataclass
class LockInfo:
    """One project lock. ``unreadable`` means the file was not valid JSON."""

    user: str = ""
    host: str = ""
    pid: int = 0
    started: str = ""
    token: str = ""
    acquired: str = ""
    heartbeat: str = ""
    unreadable: bool = False

    def label(self) -> str:
        if self.unreadable:
            return "an unreadable lock"
        who = self.user or "someone"
        host = self.host or "another PC"
        when = self.heartbeat or self.acquired
        if when:
            return f"{who} on {host} at {when}"
        return f"{who} on {host}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "user": self.user,
            "host": self.host,
            "pid": self.pid,
            "started": self.started,
            "token": self.token,
            "acquired": self.acquired,
            "heartbeat": self.heartbeat,
        }


@dataclass
class LockClaim:
    """Result of trying to hold a project lock."""

    mode: str
    token: str = ""
    lock: LockInfo | None = None
    holder: LockInfo | None = None


class ProjectChanged(ShareError):
    """The file on disk is not the one this session opened."""

    def __init__(self, path: Path | str, copies: list[Path] | None = None) -> None:
        self.path = Path(path)
        self.copies = list(copies or [])
        super().__init__(f"{self.path.name} changed on disk since it was opened; not overwritten")


class FileBusy(OSError):
    """The retried save steps gave up, or the project file could not be read.

    Raised only when ``atomic_write_text`` runs out of replace retries, or
    when ``file_stamp(retry=True)`` still cannot read the file. The original
    error is ``__cause__``. A Windows sharing or lock violation (WinError 32
    or 33) is busy when it comes from those retries. A permission error from
    creating the temp file, or from making the backup folder, is not.
    """

    def __init__(self, message: str = "file is busy") -> None:
        super().__init__(message)


class ReadOnlyProject(ShareError):
    """This session must not write the project file."""

    def __init__(self, holder: LockInfo | None = None) -> None:
        self.holder = holder
        if holder is not None and holder.unreadable:
            text = "project lock file is unreadable"
        elif holder is not None:
            text = f"project is locked by {holder.label()}"
        else:
            text = "project is read-only"
        super().__init__(text)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_user() -> str:
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001 — a locked password database still saves
        name = ""
    name = str(name or os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()
    return name or "unknown"


def current_host() -> str:
    return platform.node().strip() or "unknown"


def last_saved_text(project: Any) -> str:
    """One status-bar line. Empty when the file has never recorded a save."""

    stamp = getattr(project, "last_saved", None)
    if stamp is None:
        return ""
    user = str(getattr(stamp, "user", "") or "").strip()
    host = str(getattr(stamp, "host", "") or "").strip()
    when = str(getattr(stamp, "time", "") or "").strip()
    if not (user or host or when):
        return ""
    who = user or "someone"
    where = f" on {host}" if host else ""
    clock = f" at {when}" if when else ""
    return f"Last saved by {who}{where}{clock}"


def copy_suggestion(path: Path | str) -> str:
    """A new filename for 'save a copy' that will not collide in the same second."""

    src = Path(path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = src.name
    if name.lower().endswith(".pcad.yaml"):
        base = name[: -len(".pcad.yaml")]
        return str(src.with_name(f"{base}-copy-{stamp}.pcad.yaml"))
    return str(src.with_name(f"{src.stem}-copy-{stamp}{src.suffix}"))


def _is_windows_g(raw: str) -> bool:
    text = raw.replace("\\", "/")
    return len(text) >= 2 and text[0] in "Gg" and text[1] == ":"


def _part_is_sync_root(part: str) -> bool:
    low = part.strip().lower()
    if low in {"google drive", "my drive"}:
        return True
    if low.startswith(("onedrive", "dropbox")):
        return True
    return low.replace(" ", "").startswith("googledrive")


def folder_is_shared(path: Path | str) -> bool:
    """True for a desktop sync folder, ``G:``, or a ``.panelcad-share`` marker.

    The marker file may sit in the project directory or an ancestor. Detection
    does not call a cloud API.
    """

    raw = str(path).strip()
    if not raw:
        return False
    if _is_windows_g(raw):
        return True
    candidate = Path(raw)
    folder = candidate.parent if candidate.suffix else candidate
    if any(_part_is_sync_root(part) for part in folder.parts):
        return True
    here = folder
    for _ in range(16):
        try:
            if (here / SHARE_MARKER).is_file():
                return True
        except OSError:
            return False
        parent = here.parent
        if parent == here:
            break
        here = parent
    return False


def project_lock_path(project: Path | str) -> Path:
    path = Path(project)
    name = path.name.removesuffix(".yaml")
    return path.with_name(name + ".lock.json")


def backup_dir_for(project: Path | str) -> Path:
    path = Path(project)
    return path.with_name(f".{path.name}.backups")


def file_stamp(
    path: Path | str,
    *,
    retry: bool = False,
    pauses: tuple[float, ...] | None = None,
) -> str:
    """SHA-256 of the file. Empty when the file is missing.

    ``retry=True`` reads through ``REPLACE_PAUSES``. That is the save path.
    If the file still cannot be read, it raises ``FileBusy`` (the original
    error is ``__cause__``). ``pauses`` is a shorter budget; an empty tuple is
    one attempt and no sleep, for the disk watcher, and still raises the
    original ``OSError``. A missing file stays empty. An unreadable file is
    not reported as unchanged.
    """

    target = Path(io_path(path))
    holder: dict[str, str] = {}

    def read() -> None:
        try:
            os.stat(target)
        except FileNotFoundError:
            holder.clear()
            holder["missing"] = "1"
            return
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        holder.clear()
        holder["stamp"] = digest.hexdigest()

    if retry:
        try:
            _retry(read)
        except OSError as exc:
            raise FileBusy() from exc
    elif pauses is not None:
        _retry(read, pauses)
    else:
        if not target.is_file():
            return ""
        read()
        return "" if holder.get("missing") else holder["stamp"]
    if holder.get("missing"):
        return ""
    return holder["stamp"]


def _parse_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    text = text.removesuffix("Z")
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _heartbeat_old(info: LockInfo) -> bool:
    when = _parse_time(info.heartbeat or info.acquired)
    if when is None:
        return True
    return datetime.now() - when > LOCK_STALE_AFTER


def _same_machine(info: LockInfo) -> bool:
    host = info.host.strip().lower()
    return bool(host) and host == current_host().lower()


def _process_matches(info: LockInfo) -> bool:
    if not _same_machine(info) or info.pid <= 0 or not info.started:
        return False
    from panelcad.diag.crash import process_start_token

    token = process_start_token(info.pid)
    return bool(token) and token == info.started


def _hold(token: str) -> None:
    if not token:
        return
    with _guard:
        _counts[token] = _counts.get(token, 0) + 1


def _drop(token: str) -> bool:
    if not token:
        return False
    with _guard:
        count = _counts.get(token, 0) - 1
        if count <= 0:
            _counts.pop(token, None)
            return True
        _counts[token] = count
        return False


def lock_is_ours(info: LockInfo) -> bool:
    if info.unreadable or not info.token:
        return False
    with _guard:
        if _counts.get(info.token, 0) > 0:
            return True
    if info.pid != os.getpid():
        return False
    return _process_matches(info)


def lock_is_stale(info: LockInfo) -> bool:
    """True when the holder is gone or has not refreshed the lock in time."""

    if info.unreadable or lock_is_ours(info):
        return False
    if _same_machine(info) and not _process_matches(info):
        return True
    return _heartbeat_old(info)


def read_project_lock(project: Path | str) -> LockInfo | None:
    dest = project_lock_path(project)
    if not dest.is_file():
        return None
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return LockInfo(unreadable=True)
    if not isinstance(data, dict):
        return LockInfo(unreadable=True)
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return LockInfo(
        user=str(data.get("user") or ""),
        host=str(data.get("host") or ""),
        pid=pid,
        started=str(data.get("started") or ""),
        token=str(data.get("token") or ""),
        acquired=str(data.get("acquired") or ""),
        heartbeat=str(data.get("heartbeat") or ""),
    )


def store_project_lock(project: Path | str, info: LockInfo) -> None:
    payload = json.dumps(info.as_dict(), indent=2) + "\n"
    atomic_write_text(
        project_lock_path(project),
        payload,
        backup=False,
        pauses=LOCK_PAUSES,
    )


def _our_lock() -> LockInfo:
    from panelcad.diag.crash import process_start_token

    pid = os.getpid()
    now = now_iso()
    return LockInfo(
        user=current_user(),
        host=current_host(),
        pid=pid,
        started=process_start_token(pid) or "",
        token=uuid.uuid4().hex,
        acquired=now,
        heartbeat=now,
    )


def _touch_lock(path: Path, info: LockInfo) -> None:
    info.heartbeat = now_iso()
    store_project_lock(path, info)


def claim_project_lock(
    project: Path | str,
    *,
    take_over: bool = False,
    current_token: str = "",
) -> LockClaim:
    """Hold the lock, or report that someone else still has it.

    ``local`` means this folder is not shared, so no lock file is written.
    ``readonly`` and ``unreadable`` do not replace the file.
    """

    path = Path(project)
    if is_under_recovery(path) or not folder_is_shared(path):
        return LockClaim(mode="local")
    with _file_guard:
        info = read_project_lock(path)
        same_token = bool(current_token) and info is not None and info.token == current_token
        if same_token and info is not None and not info.unreadable:
            _touch_lock(path, info)
            return LockClaim(mode="ours", token=info.token, lock=info)
        if info is not None and not info.unreadable and lock_is_ours(info) and not take_over:
            _hold(info.token)
            _touch_lock(path, info)
            return LockClaim(mode="ours", token=info.token, lock=info)
        if info is None:
            created = _our_lock()
            store_project_lock(path, created)
            _hold(created.token)
            return LockClaim(mode="acquired", token=created.token, lock=created)
        if info.unreadable and not take_over:
            return LockClaim(mode="unreadable", holder=info)
        stale = lock_is_stale(info)
        if stale or take_over:
            created = _our_lock()
            store_project_lock(path, created)
            _hold(created.token)
            mode = "takeover" if take_over and not stale else "stale"
            return LockClaim(mode=mode, token=created.token, lock=created, holder=info)
        return LockClaim(mode="readonly", holder=info)


def release_project_lock(project: Path | str | None, token: str) -> None:
    """Drop one holder. Delete the file only when we were the last holder and still own it."""

    if not token:
        return
    last = _drop(token)
    if not last or project is None:
        return
    path = Path(project)
    with _file_guard:
        info = read_project_lock(path)
        if info is None or info.token != token:
            return
        try:
            project_lock_path(path).unlink()
        except OSError:
            return


def heartbeat_project_lock(project: Path | str, token: str) -> bool:
    """Refresh our heartbeat. False when the file is no longer the lock we hold."""

    if not token:
        return False
    path = Path(project)
    with _file_guard:
        info = read_project_lock(path)
        if info is None or info.unreadable or info.token != token:
            return False
        info.heartbeat = now_iso()
        store_project_lock(path, info)
        return True


_CONFLICT_MARK = None


def _conflict_mark():
    global _CONFLICT_MARK
    if _CONFLICT_MARK is None:
        import re

        _CONFLICT_MARK = re.compile(
            r"\s+\([^)]*conflict[^)]*\)|\s+\[[^\]]*conflict[^\]]*\]|\s+\(\d+\)",
            re.IGNORECASE,
        )
    return _CONFLICT_MARK


def _strip_conflict_marks(name: str) -> str:
    import re

    previous = None
    current = name
    while current != previous:
        previous = current
        current = _conflict_mark().sub("", current).strip()
    return re.sub(r"^copy of\s+", "", current, flags=re.IGNORECASE).strip()


def is_conflict_copy(project: Path, candidate: Path) -> bool:
    """True for a Drive/Dropbox/OneDrive sibling of ``project``."""

    if not candidate.is_file() or candidate.name.startswith("."):
        return False
    low = candidate.name.lower()
    if low.endswith(".lock.json") or ".partial-" in low:
        return False
    try:
        if candidate.resolve() == project.resolve():
            return False
    except OSError:
        if candidate == project:
            return False
    return _strip_conflict_marks(candidate.name) == project.name


def find_conflict_copies(project: Path | str) -> list[Path]:
    path = Path(project)
    folder = path.parent
    if not folder.is_dir():
        return []
    found: list[Path] = []
    try:
        children = list(folder.iterdir())
    except OSError:
        return []
    for item in children:
        try:
            if is_conflict_copy(path, item):
                found.append(item)
        except OSError:
            continue
    return sorted(found, key=lambda item: item.name.lower())


def unified_diff_text(disk: str, current: str, name: str) -> str:
    rows = difflib.unified_diff(
        disk.splitlines(),
        current.splitlines(),
        fromfile=f"{name} (on disk)",
        tofile=f"{name} (this session)",
        lineterm="",
    )
    text = "\n".join(rows)
    if not text.strip():
        return "No text difference."
    if len(text) > 200_000:
        return text[:200_000] + "\n… truncated"
    return text


def _backup_rank(path: Path) -> tuple[str, int]:
    """Oldest first. A bare timestamp is suffix 0; ``-10`` is newer than ``-2``."""

    import re

    match = re.fullmatch(
        r"(\d{8}-\d{6})(?:-(\d+))?\.pcad\.yaml",
        path.name,
        flags=re.IGNORECASE,
    )
    if match is None:
        return ("", 0)
    return (match.group(1), int(match.group(2) or 0))


def prune_backups(folder: Path, keep: int = BACKUP_KEEP) -> None:
    files = [item for item in folder.glob("*.pcad.yaml") if item.is_file()]
    files.sort(key=_backup_rank)
    if keep > 0 and len(files) <= keep:
        return
    extra = files if keep <= 0 else files[:-keep]
    for item in extra:
        try:
            item.unlink()
        except OSError:
            continue


def _retry(action, pauses: tuple[float, ...] | None = None) -> None:
    """Run ``action`` until it works, pausing on ``OSError``.

    The pauses are ``REPLACE_PAUSES`` unless ``pauses`` is passed. The last
    error is raised when every attempt fails.
    """

    steps = REPLACE_PAUSES if pauses is None else pauses
    last: OSError | None = None
    for attempt in range(len(steps) + 1):
        try:
            action()
            return
        except OSError as exc:
            last = exc
            if attempt >= len(steps):
                break
            time.sleep(steps[attempt])
    if last is not None:
        raise last
    raise OSError("retry failed")


def _log_skipped_backup(project: Path, exc: OSError) -> None:
    message = f"skipped backup of {project.name}: {exc}"
    log.warning(message)
    from panelcad.diag.log import log_event

    log_event("warning", message)


def _backup_previous(project: Path) -> Path | None:
    """Copy ``project`` into the backup folder. ``None`` when the copy cannot be read.

    The copy is retried with ``REPLACE_PAUSES``. A sync client can hold the
    file without share-read for a moment; that used to fail the whole save.
    If the copy still fails, the backup is skipped and the save continues.
    The project file itself is not removed until a later replace succeeds.
    """

    folder = backup_dir_for(project)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"{stamp}.pcad.yaml"
    index = 1
    while dest.exists():
        dest = folder / f"{stamp}-{index}.pcad.yaml"
        index += 1
    try:
        _retry(lambda: shutil.copy2(project, dest))
    except OSError as exc:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        _log_skipped_backup(project, exc)
        return None
    prune_backups(folder, BACKUP_KEEP)
    return dest


def _replace_retry(src: Path, dest: Path, pauses: tuple[float, ...] | None = None) -> None:
    try:
        _retry(lambda: os.replace(src, dest), pauses)
    except OSError as exc:
        raise FileBusy() from exc


def _fsync_dir(folder: Path) -> None:
    if os.name != "posix":
        return
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        return
    finally:
        os.close(handle)


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    backup: bool = True,
    pauses: tuple[float, ...] | None = None,
) -> Path | None:
    """Write ``text`` via a temp file in the same folder, then replace.

    A previous file is copied into ``.<name>.backups`` first when ``backup``
    is set and the destination is not a crash-recovery copy. That copy is
    retried with ``REPLACE_PAUSES``. If it still fails, the backup is skipped
    (and logged) and the save continues. The original stays in place when the
    replace fails. Replace uses the same pauses, a little over eight seconds,
    so a sync client can let go of the file. If those retries run out, this
    raises ``FileBusy``. An error before the replace (the folder cannot be
    written, the path is not a folder) propagates unchanged.
    """

    dest = Path(io_path(path))
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if backup and dest.is_file() and not is_under_recovery(dest):
        backup_path = _backup_previous(dest)
    tmp = dest.with_name(f".{dest.name}.partial-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        _replace_retry(tmp, dest, pauses)
        _fsync_dir(dest.parent)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.unlink(missing_ok=True)
    return backup_path
