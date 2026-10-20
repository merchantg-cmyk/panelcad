"""Plan the Windows upgrade handoff. The installer is not started on a dry run.

The plan keeps projects, ``%USERPROFILE%\\.panelcad`` (settings, logs, and the
user part library), and ``%USERPROFILE%\\.venvs\\panelcad``. It backs up the
program folder, then runs ``Install-PanelCAD.ps1 -Quiet`` (the same unattended
switch as a manual ``Install-PanelCAD.cmd /quiet``). PanelCAD does not install
anything until the caller has confirmed and this process is ready to exit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from panelcad.update.manifest import UpdateError
from panelcad.update.settings import prune_backups, write_notice, write_rollback

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
SKIP_DIR_NAMES = frozenset(
    {".git", ".venv", ".venvs", "__pycache__", ".pytest_cache", ".ruff_cache", ".sharpen", ".grok"}
)
# Rollback replaces these trees in place. Drawings and other files next to them stay.
PROGRAM_DIRS = ("src", "scripts")
# pip install -e reads these from the install root. The backup already holds them
# because the whole program folder is copied. Restore must put them back too, or
# the reinstall keeps the new version's metadata while version.py is the old one.
PACKAGING_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in")
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
FAILED_NOTICE = "rollback/update failed: see log"
_HANDOFF_LOG: Any = None


def is_windows_profile(user_profile: str) -> bool:
    text = user_profile.strip().replace("/", "\\")
    return bool(_DRIVE.match(text)) or text.startswith("\\\\")


def join_profile(user_profile: str, *parts: str) -> str:
    """Join onto a profile path. Drive and UNC profiles stay Windows paths on any OS."""

    if is_windows_profile(user_profile):
        head = user_profile.strip().replace("/", "\\").rstrip("\\")
        bits = [head]
        for part in parts:
            cleaned = part.replace("/", "\\").strip("\\")
            if cleaned:
                bits.append(cleaned)
        return "\\".join(bits)
    path = Path(user_profile)
    for part in parts:
        path = path / part
    return str(path)


def preserved_locations(user_profile: str) -> tuple[str, ...]:
    """Folders the upgrade must not delete. Drawings live outside the program folder."""

    return (
        join_profile(user_profile, ".panelcad"),
        join_profile(user_profile, ".panelcad", "library"),
        join_profile(user_profile, ".panelcad", "logs"),
        join_profile(user_profile, ".venvs", "panelcad"),
    )


def default_user_profile() -> str:
    if os.name == "nt":
        return os.environ.get("USERPROFILE") or str(Path.home())
    return str(Path.home())


def default_python(user_profile: str) -> str:
    if is_windows_profile(user_profile):
        venv = join_profile(user_profile, ".venvs", "panelcad", "Scripts", "python.exe")
        if os.name != "nt" or Path(venv).is_file():
            return venv
        return sys.executable
    posix_venv = join_profile(user_profile, ".venvs", "panelcad", "bin", "python")
    if Path(posix_venv).is_file():
        return posix_venv
    return sys.executable


@dataclass(frozen=True)
class UpgradePlan:
    """What the detached handoff will do. ``command`` is the installer invocation."""

    new_version: str
    previous_version: str
    install_root: str
    backup_dir: str
    installer: str
    command: tuple[str, ...]
    python_exe: str
    relaunch: tuple[str, ...]
    relaunch_cwd: str
    wait_pid: int
    preserved: tuple[str, ...]
    notice_path: str
    rollback_path: str
    config_dir: str
    user_profile: str
    env: tuple[tuple[str, str], ...]
    steps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["command"] = list(self.command)
        data["relaunch"] = list(self.relaunch)
        data["preserved"] = list(self.preserved)
        data["env"] = {key: value for key, value in self.env}
        data["steps"] = list(self.steps)
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpgradePlan:
        env = data.get("env") or {}
        if isinstance(env, dict):
            env_pairs = tuple((str(key), str(value)) for key, value in env.items())
        else:
            env_pairs = tuple((str(key), str(value)) for key, value in env)
        return cls(
            new_version=str(data["new_version"]),
            previous_version=str(data["previous_version"]),
            install_root=str(data["install_root"]),
            backup_dir=str(data["backup_dir"]),
            installer=str(data["installer"]),
            command=tuple(str(part) for part in data["command"]),
            python_exe=str(data["python_exe"]),
            relaunch=tuple(str(part) for part in data["relaunch"]),
            relaunch_cwd=str(data["relaunch_cwd"]),
            wait_pid=int(data["wait_pid"]),
            preserved=tuple(str(part) for part in data["preserved"]),
            notice_path=str(data["notice_path"]),
            rollback_path=str(data["rollback_path"]),
            config_dir=str(data["config_dir"]),
            user_profile=str(data["user_profile"]),
            env=env_pairs,
            steps=tuple(str(part) for part in data["steps"]),
        )


@dataclass(frozen=True)
class RollbackPlan:
    previous_version: str
    install_root: str
    backup_dir: str
    python_exe: str
    command: tuple[str, ...]
    relaunch: tuple[str, ...]
    relaunch_cwd: str
    wait_pid: int
    preserved: tuple[str, ...]
    notice_path: str
    config_dir: str
    steps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["command"] = list(self.command)
        data["relaunch"] = list(self.relaunch)
        data["preserved"] = list(self.preserved)
        data["steps"] = list(self.steps)
        return data


def installer_command(installer: str) -> tuple[str, ...]:
    """Unattended upgrade. ``-Quiet`` matches ``Install-PanelCAD.cmd /quiet``."""

    return (
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        installer,
        "-Quiet",
    )


def plan_upgrade(
    *,
    installer: str,
    new_version: str,
    previous_version: str,
    user_profile: str | None = None,
    config_dir: str | None = None,
    python_exe: str | None = None,
    relaunch_project: str | None = None,
    wait_pid: int | None = None,
    stamp: str | None = None,
) -> UpgradePlan:
    profile = user_profile or default_user_profile()
    install_root = join_profile(profile, "PanelCAD")
    config = config_dir or join_profile(profile, ".panelcad")
    stamp_text = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    backup_dir = join_profile(config, "backups", f"PanelCAD-{previous_version}-{stamp_text}")
    python = python_exe or default_python(profile)
    relaunch_bits = [python, "-m", "panelcad", "gui"]
    if relaunch_project:
        relaunch_bits.append(relaunch_project)
    command = installer_command(installer)
    preserved = preserved_locations(profile)
    if _use_windows_paths(config):
        notice = join_profile(config, "update-notice.json")
        rollback = join_profile(config, "rollback.json")
    else:
        notice = str(Path(config) / "update-notice.json")
        rollback = str(Path(config) / "rollback.json")
        backup_dir = str(Path(config) / "backups" / f"PanelCAD-{previous_version}-{stamp_text}")
    env = (
        ("PANELCAD_INSTALL_QUIET", "1"),
        ("PANELCAD_BACKUP_DONE", "1"),
    )
    steps = (
        f"wait for pid {wait_pid if wait_pid is not None else os.getpid()} to exit",
        f"backup {install_root} -> {backup_dir}",
        "keep " + ", ".join(preserved),
        "run " + " ".join(command),
        f"write notice Updated to {new_version} at {notice}",
        "relaunch " + " ".join(relaunch_bits),
    )
    return UpgradePlan(
        new_version=new_version,
        previous_version=previous_version,
        install_root=install_root,
        backup_dir=backup_dir,
        installer=installer,
        command=command,
        python_exe=python,
        relaunch=tuple(relaunch_bits),
        relaunch_cwd=install_root,
        wait_pid=os.getpid() if wait_pid is None else wait_pid,
        preserved=preserved,
        notice_path=notice,
        rollback_path=rollback,
        config_dir=config,
        user_profile=profile,
        env=env,
        steps=steps,
    )


def _use_windows_paths(config: str) -> bool:
    return is_windows_profile(config)


def plan_rollback(
    *,
    previous_version: str,
    backup_dir: str,
    install_root: str,
    user_profile: str,
    config_dir: str,
    python_exe: str | None = None,
    relaunch_project: str | None = None,
    wait_pid: int | None = None,
) -> RollbackPlan:
    python = python_exe or default_python(user_profile)
    command = (python, "-m", "pip", "install", "-e", install_root)
    relaunch_bits = [python, "-m", "panelcad", "gui"]
    if relaunch_project:
        relaunch_bits.append(relaunch_project)
    preserved = preserved_locations(user_profile)
    notice = str(Path(config_dir) / "update-notice.json")
    if is_windows_profile(config_dir):
        notice = join_profile(config_dir, "update-notice.json")
    steps = (
        f"wait for pid {wait_pid if wait_pid is not None else os.getpid()} to exit",
        f"restore {backup_dir} -> {install_root}",
        "keep " + ", ".join(preserved),
        "run " + " ".join(command),
        f"write notice Rolled back to {previous_version}",
        "relaunch " + " ".join(relaunch_bits),
    )
    return RollbackPlan(
        previous_version=previous_version,
        install_root=install_root,
        backup_dir=backup_dir,
        python_exe=python,
        command=command,
        relaunch=tuple(relaunch_bits),
        relaunch_cwd=install_root,
        wait_pid=os.getpid() if wait_pid is None else wait_pid,
        preserved=preserved,
        notice_path=notice,
        config_dir=config_dir,
        steps=steps,
    )


def backup_install_tree(source: Path, dest: Path) -> int:
    """Copy the program folder. Does not delete the source or any preserved directory."""

    if not source.is_dir():
        raise UpdateError(f"Install folder not found: {source}")
    if source.is_symlink():
        raise UpdateError(f"Refusing to back up a symlink: {source}")
    if dest.exists():
        raise UpdateError(f"Backup already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    dest.mkdir()
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not (current / name).is_symlink()
        ]
        target_dir = dest / current.relative_to(source)
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            src = current / name
            if src.is_symlink():
                continue
            shutil.copy2(src, target_dir / name)
            count += 1
    # The walk copies these with the rest of the tree. If a skip ever drops one,
    # put it back so rollback still has the file pip install -e reads.
    count += _copy_packaging_files(source, dest, only_missing=True)
    return count


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _replace_program_dir(source: Path, dest: Path) -> None:
    """Overwrite ``dest`` with the files in ``source``. Extra files under ``dest`` are removed."""

    if source.is_symlink():
        raise UpdateError(f"Refusing to restore a symlink: {source}")
    if dest.is_symlink():
        raise UpdateError(f"Refusing to replace a symlink: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    copied: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not (current / name).is_symlink()
        ]
        target_dir = dest / current.relative_to(source)
        if target_dir.is_symlink():
            raise UpdateError(f"Refusing to replace a symlink: {target_dir}")
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            src = current / name
            if src.is_symlink():
                continue
            target = target_dir / name
            if target.is_symlink():
                target.unlink()
            shutil.copy2(src, target)
            copied.add(target.resolve())
    for dirpath, dirnames, filenames in os.walk(dest, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink():
                continue
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in copied or not _is_within(resolved, dest_root):
                continue
            path.unlink()
    for dirpath, dirnames, filenames in os.walk(dest, topdown=False, followlinks=False):
        current = Path(dirpath)
        if current == dest:
            continue
        rel = current.relative_to(dest)
        if (source / rel).is_dir():
            continue
        try:
            current.rmdir()
        except OSError:
            pass


def _packaging_source(root: Path, name: str) -> Path | None:
    """A real top-level packaging file, or None. A symlink is refused."""

    path = root / name
    if path.is_symlink():
        raise UpdateError(f"Refusing to copy a packaging symlink: {path}")
    if path.is_file():
        return path
    return None


def _copy_packaging_files(source: Path, dest: Path, *, only_missing: bool) -> int:
    """Copy top-level packaging files ``source`` holds. Returns how many were written."""

    written = 0
    dest.mkdir(parents=True, exist_ok=True)
    for name in PACKAGING_FILES:
        src = _packaging_source(source, name)
        if src is None:
            continue
        target = dest / name
        if target.is_symlink():
            raise UpdateError(f"Refusing to replace a symlink: {target}")
        if only_missing and target.is_file():
            continue
        shutil.copy2(src, target)
        written += 1
    return written


def _restore_packaging_files(backup: Path, install_root: Path) -> None:
    """Put packaging files back before ``pip install -e`` reads them.

    A file the backup holds replaces the install's copy. A packaging file only
    the new install has is removed, so a leftover ``setup.py`` cannot keep the
    new version. Drawings and other names are left alone.
    """

    for name in PACKAGING_FILES:
        source = _packaging_source(backup, name)
        target = install_root / name
        if target.is_symlink():
            raise UpdateError(f"Refusing to replace a symlink: {target}")
        if source is not None:
            shutil.copy2(source, target)
            continue
        if target.is_file():
            target.unlink()


def restore_install_tree(backup: Path, install_root: Path) -> None:
    """Replace ``src``, ``scripts``, and top-level packaging files from the backup.

    User files in the install stay put. The install folder is not renamed. A
    drawing saved inside it is not moved aside. The venv is outside this folder;
    the rollback plan reinstalls it with pip, which reads ``pyproject.toml``.
    """

    if not backup.is_dir():
        raise UpdateError(f"Backup not found: {backup}")
    if install_root.is_symlink():
        raise UpdateError(f"Refusing to restore through a symlink: {install_root}")
    install_root.mkdir(parents=True, exist_ok=True)
    found = False
    for name in PROGRAM_DIRS:
        source = backup / name
        if not source.is_dir():
            continue
        _replace_program_dir(source, install_root / name)
        found = True
    if not found:
        raise UpdateError(
            f"Backup {backup} has no src or scripts directory. "
            "Refusing to move the install folder."
        )
    _restore_packaging_files(backup, install_root)


def windows_pid_alive(pid: int, kernel32: Any) -> bool:
    """True when ``pid`` is still running.

    ``os.kill(pid, 0)`` on Windows is ``GenerateConsoleCtrlEvent(CTRL_C_EVENT)`` and
    raises ``OSError`` with winerror 87 for a live process. Query the process exit
    code instead. Access denied means the process exists and we cannot read it.
    """

    import ctypes

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        try:
            error = int(kernel32.GetLastError())
        except (OSError, TypeError, ValueError):
            error = 0
        return error == _ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if not ok:
            return True
        return int(code.value) == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        return windows_pid_alive(pid, ctypes.windll.kernel32)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for_exit(
    pid: int,
    *,
    timeout: float = 180.0,
    pause: Callable[[float], None] | None = None,
) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    sleep = pause or time.sleep
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        sleep(0.2)
    raise UpdateError(f"PanelCAD (pid {pid}) did not exit.")


def _run_installer(plan: UpgradePlan) -> None:
    if os.name != "nt":
        raise UpdateError(
            "The installer handoff runs on Windows. Pass dry_run to preview the command."
        )
    env = os.environ.copy()
    for key, value in plan.env:
        env[key] = value
    completed = subprocess.run(list(plan.command), env=env, check=False)
    if completed.returncode != 0:
        raise UpdateError(f"The installer exited with code {completed.returncode}.")


def _relaunch(command: tuple[str, ...], cwd: str) -> None:
    if os.name != "nt":
        return
    subprocess.Popen(
        list(command),
        cwd=cwd if cwd and Path(cwd).is_dir() else None,
        close_fds=True,
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    )


def apply_upgrade(
    plan: UpgradePlan,
    *,
    dry_run: bool = False,
    runner: Callable[[UpgradePlan], None] | None = None,
    relaunch: bool = True,
    wait: bool = False,
) -> dict[str, Any]:
    """Back up, run the installer, and write the "Updated to X" notice.

    ``dry_run`` returns the plan and does not copy files or call ``runner``.
    """

    payload = plan.to_dict()
    payload["dry_run"] = dry_run
    payload["started"] = False
    if dry_run:
        return payload
    _log_update("update_install", plan.install_root)
    _log_update("update_backup", plan.backup_dir)
    if "/MIR" in plan.command or any(part.upper() == "/MIR" for part in plan.command):
        raise UpdateError("Refusing an installer command that mirror-deletes the program folder.")
    if wait:
        wait_for_exit(plan.wait_pid)
    root = Path(plan.install_root)
    if root.exists():
        backup_install_tree(root, Path(plan.backup_dir))
        write_rollback(
            previous_version=plan.previous_version,
            backup=plan.backup_dir,
            install_root=plan.install_root,
            directory=Path(plan.config_dir),
        )
        prune_backups(Path(plan.config_dir))
    try:
        if runner is not None:
            runner(plan)
        else:
            _run_installer(plan)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"The installer did not run: {exc}") from exc
    write_notice(plan.new_version, Path(plan.config_dir), kind="update")
    if relaunch:
        _relaunch(plan.relaunch, plan.relaunch_cwd)
    payload["started"] = True
    payload["close_app"] = True
    return payload


def apply_rollback(
    plan: RollbackPlan,
    *,
    dry_run: bool = False,
    runner: Callable[[RollbackPlan], None] | None = None,
    relaunch: bool = True,
    wait: bool = False,
) -> dict[str, Any]:
    payload = plan.to_dict()
    payload["dry_run"] = dry_run
    payload["started"] = False
    if dry_run:
        return payload
    _log_update("update_rollback", plan.install_root)
    _log_update("update_backup", plan.backup_dir)
    if wait:
        wait_for_exit(plan.wait_pid)
    restore_install_tree(Path(plan.backup_dir), Path(plan.install_root))
    try:
        if runner is not None:
            runner(plan)
        elif os.name == "nt":
            completed = subprocess.run(list(plan.command), check=False)
            if completed.returncode != 0:
                raise UpdateError(f"pip install exited with code {completed.returncode}.")
        else:
            raise UpdateError(
                "Rollback's pip reinstall runs on Windows. Pass dry_run to preview it."
            )
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"Could not reinstall the restored copy: {exc}") from exc
    write_notice(plan.previous_version, Path(plan.config_dir), kind="rollback")
    if relaunch:
        _relaunch(plan.relaunch, plan.relaunch_cwd)
    payload["started"] = True
    payload["close_app"] = True
    return payload


def neutral_workdir(config_dir: str, install_root: str) -> Path:
    """A folder that is not the program directory. The helper must not run from inside it."""

    import tempfile

    candidates: list[Path] = []
    if config_dir:
        candidates.append(Path(config_dir))
    candidates.append(Path(tempfile.gettempdir()))
    root = Path(install_root) if install_root else None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if root is not None and _is_within(candidate, root):
            continue
        return candidate
    fallback = Path(tempfile.gettempdir())
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def enter_neutral_workdir(config_dir: str, install_root: str) -> Path:
    """Leave the program folder before any copy or rename. Returns the new working folder."""

    work = neutral_workdir(config_dir, install_root)
    os.chdir(work)
    if install_root and _is_within(Path.cwd(), Path(install_root)):
        raise UpdateError(
            f"Refusing to update while the working folder is inside {install_root}."
        )
    return work


def handoff_log_path(config_dir: str, kind: str, stamp: str | None = None) -> Path:
    """Log file under the data directory, outside the program folder."""

    import tempfile

    if config_dir:
        logs = Path(config_dir) / "logs"
    else:
        logs = Path(tempfile.gettempdir())
    logs.mkdir(parents=True, exist_ok=True)
    stamp_text = stamp or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    safe_kind = "rollback" if kind == "rollback" else "update"
    return logs / f"{safe_kind}-{stamp_text}.log"


def _log_update(action: str, path: object = None) -> None:
    """Path line in the rotating log. The home directory is redacted. No file bodies."""

    try:
        from panelcad.diag.log import log_io

        log_io(action, path)
    except Exception:  # noqa: BLE001 — a log failure must not hide the update result
        return


def _configure_update_log() -> None:
    """Open the rotating log in this process. The handoff child is not the GUI."""

    try:
        from panelcad.diag.log import configure_logging

        configure_logging()
    except Exception:  # noqa: BLE001
        return


def attach_handoff_log(log_path: Path, banner: str) -> None:
    """Append ``banner`` on the handle the installer will inherit.

    The parent passes a log handle as this process's stdout. On Windows that
    inherited handle stays at offset 0, so installer and pip output overwrite a
    banner written through a second handle. Open the log for append, write the
    banner there, and point stdout and stderr at that handle.
    """

    global _HANDOFF_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(log_path, "a", encoding="utf-8", buffering=1)
    try:
        stream.seek(0, os.SEEK_END)
    except OSError:
        pass
    text = banner if banner.endswith("\n") else banner + "\n"
    stream.write(text)
    stream.flush()
    try:
        os.dup2(stream.fileno(), 1)
        os.dup2(stream.fileno(), 2)
    except OSError:
        pass
    sys.stdout = stream
    sys.stderr = stream
    _HANDOFF_LOG = stream


def record_handoff_failure(
    config_dir: str,
    log_path: Path,
    exc: BaseException,
    *,
    relaunch: tuple[str, ...] = (),
    relaunch_cwd: str = "",
) -> None:
    """Write the failure to the log and leave a notice for the next launch."""

    _log_update("error", f"update_failed {log_path}")
    detail = f"{FAILED_NOTICE}: {exc}\n{traceback.format_exc()}"
    if not detail.endswith("\n"):
        detail += "\n"
    try:
        sys.stdout.write(detail)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — fall back to a fresh append handle
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(detail)
        except OSError:
            pass
    try:
        if config_dir:
            write_notice(
                FAILED_NOTICE,
                Path(config_dir),
                kind="failed",
                message=str(exc),
                log=str(log_path),
            )
    except (OSError, UpdateError):
        pass
    if relaunch:
        _relaunch(relaunch, relaunch_cwd)


def start_detached(plan: UpgradePlan | RollbackPlan, *, kind: str) -> Path:
    """Write the plan and start a child that waits for this process to exit.

    The child starts with its working folder set to the data directory (or temp),
    never the program folder. The desktop shortcut sets that folder as the app's
    working directory, and a helper that inherits it cannot rename or replace files
    there (WinError 32). Stdout and stderr go to ``.panelcad/logs``.
    """

    work = neutral_workdir(plan.config_dir, plan.install_root)
    log_path = handoff_log_path(plan.config_dir, kind)
    # The plan file stays in TEMP. The install folder is updated while the child runs.
    base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or tempfile_dir())
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"panelcad-{kind}-plan.json"
    path.write_text(
        json.dumps(
            {"kind": kind, "plan": plan.to_dict(), "log": str(log_path)},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-m", "panelcad.update.handoff", str(path)]
    log_handle = log_path.open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "cwd": str(work),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()
    return path


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Finish a PanelCAD upgrade after the app exits.")
    parser.add_argument("plan", type=Path)
    args = parser.parse_args(argv)
    payload = json.loads(args.plan.read_text(encoding="utf-8"))
    kind = str(payload.get("kind") or "upgrade")
    data = payload["plan"]
    if kind == "rollback":
        plan: UpgradePlan | RollbackPlan = RollbackPlan(**_plan_kwargs(RollbackPlan, data))
    else:
        plan = UpgradePlan.from_dict(data)
    log_path = Path(str(payload["log"])) if payload.get("log") else handoff_log_path(
        plan.config_dir, kind
    )
    _configure_update_log()
    try:
        work = enter_neutral_workdir(plan.config_dir, plan.install_root)
        attach_handoff_log(log_path, f"working folder {work}")
        if isinstance(plan, RollbackPlan):
            apply_rollback(plan, wait=True)
        else:
            apply_upgrade(plan, wait=True)
    except Exception as exc:  # noqa: BLE001 — any failure must become a notice, not a silent exit
        record_handoff_failure(
            plan.config_dir,
            log_path,
            exc,
            relaunch=plan.relaunch,
            relaunch_cwd=plan.relaunch_cwd,
        )
        return 1
    return 0


def _plan_kwargs(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    fields = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
    for key in ("command", "relaunch", "preserved", "steps"):
        if key in fields and not isinstance(fields[key], tuple):
            fields[key] = tuple(fields[key])
    return fields


if __name__ == "__main__":
    raise SystemExit(main())
