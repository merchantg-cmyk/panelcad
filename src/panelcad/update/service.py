"""Check a feed, and install or roll back only after an explicit confirm."""

from __future__ import annotations

import os
import ssl
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from panelcad.update.feed import load_feed, resolve_download
from panelcad.update.fetch import (
    cleanup_stage,
    find_installer_script,
    prune_stage_dirs,
    safe_extract,
    stage_installer,
)
from panelcad.update.manifest import ReleaseManifest, UpdateError
from panelcad.update.settings import (
    config_dir as settings_config_dir,
)
from panelcad.update.settings import (
    effective_feed,
    load_settings,
    mark_checked,
    read_rollback,
)
from panelcad.update.version import compare_versions, is_newer, parse_version
from panelcad.version import __version__

__all__ = [
    "UpdateError",
    "check_for_update",
    "install_update",
    "rollback_update",
    "startup_should_prompt",
]


def _runtime_config(directory: Path | None) -> Path:
    """Data directory for this call. ``PANELCAD_CONFIG_DIR`` applies when none was passed."""

    if directory is not None:
        return directory
    return settings_config_dir()


def _cancelled(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise UpdateError("Update cancelled.")


def _log_update(action: str, path: object = None) -> None:
    """Record an updater path the way other file actions do. Never log file bodies."""

    try:
        from panelcad.diag.log import log_io

        log_io(action, path)
    except Exception:  # noqa: BLE001 — logging must not stop an update
        return


def startup_should_prompt(report: dict[str, Any]) -> bool:
    """Startup shows the dialog only for a newer feed release the user has not skipped."""

    return bool(
        report.get("enabled") and report.get("update_available") and not report.get("skipped")
    )


def _evaluate(manifest: ReleaseManifest, *, current: str, skip_version: str) -> dict[str, Any]:
    parse_version(manifest.version)
    parse_version(current)
    newer = is_newer(manifest.version, current)
    blocked = False
    block_reason = ""
    if manifest.min_version:
        parse_version(manifest.min_version)
        if compare_versions(current, manifest.min_version) < 0:
            blocked = True
            block_reason = (
                f"This release needs PanelCAD {manifest.min_version} or newer "
                f"(this copy is {current})."
            )
    skipped = bool(skip_version) and compare_versions(skip_version, manifest.version) == 0
    if not newer:
        if compare_versions(manifest.version, current) == 0:
            reason = f"PanelCAD {current} is the newest release on this feed."
        else:
            reason = f"The feed has {manifest.version}, which is older than {current}."
    elif blocked:
        reason = block_reason
    elif skipped:
        reason = f"Version {manifest.version} is marked skip this version."
    else:
        reason = f"PanelCAD {manifest.version} is available."
    return {
        "update_available": newer,
        "installable": newer and not blocked,
        "blocked": blocked,
        "block_reason": block_reason,
        "skipped": skipped,
        "reason": reason,
    }


def check_for_update(
    *,
    feed: str | None = None,
    current: str | None = None,
    config_dir: Path | None = None,
    now: datetime | None = None,
    record: bool = True,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Read the feed. Does not download the installer."""

    prune_stage_dirs()
    installed = current or __version__
    config_dir = _runtime_config(config_dir)
    settings = load_settings(config_dir)
    chosen = effective_feed(settings, feed, directory=config_dir)
    if not chosen:
        return {
            "enabled": False,
            "feed": "",
            "current": installed,
            "update_available": False,
            "installable": False,
            "blocked": False,
            "skipped": False,
            "reason": "no feed configured",
        }
    from panelcad.update.github_releases import (
        GITHUB_RELEASES_FEED,
        is_github_releases_feed,
        load_github_release,
        repository_configured,
    )

    if is_github_releases_feed(chosen):
        if not repository_configured():
            return {
                "enabled": False,
                "feed": GITHUB_RELEASES_FEED,
                "current": installed,
                "update_available": False,
                "installable": False,
                "blocked": False,
                "skipped": False,
                "reason": (
                    "PANELCAD_GITHUB_REPOSITORY must be owner/name. "
                    "Unset it to use merchantg-cmyk/panelcad."
                ),
            }
        try:
            manifest, resolved = load_github_release(timeout=timeout, ssl_context=ssl_context)
        except UpdateError:
            _log_update("error", "update_check github-releases")
            raise
    else:
        try:
            manifest, resolved = load_feed(chosen, timeout=timeout, ssl_context=ssl_context)
        except UpdateError:
            _log_update("error", f"update_check {chosen}")
            raise
    _log_update("update_check", resolved)
    report = _evaluate(manifest, current=installed, skip_version=settings.skip_version)
    if record:
        mark_checked(config_dir, now=now)
    download_resolved = resolve_download(resolved, manifest.download)
    shown_feed = resolved if is_github_releases_feed(chosen) else chosen
    report.update(
        {
            "enabled": True,
            "feed": shown_feed,
            "manifest": resolved,
            "current": installed,
            "version": manifest.version,
            "release_date": manifest.release_date,
            "notes": manifest.notes,
            "download": manifest.download,
            "download_resolved": download_resolved,
            "size": manifest.size,
            "sha256": manifest.sha256,
            "min_version": manifest.min_version,
        }
    )
    return report


def _require_expected_manifest(
    report: dict[str, Any],
    expected_version: str | None,
    expected_sha256: str | None,
) -> None:
    """Refuse when the feed no longer matches the version and sha256 the operator confirmed."""

    if not expected_version and not expected_sha256:
        return
    got_version = str(report.get("version") or "")
    got_sha = str(report.get("sha256") or "").lower()
    if expected_version and got_version != expected_version:
        raise UpdateError(
            f"The feed changed. The dialog showed {expected_version}, "
            f"but the feed now has {got_version}. Install was not started."
        )
    if expected_sha256 and got_sha != expected_sha256.strip().lower():
        raise UpdateError(
            "The feed changed. The published sha256 no longer matches the update "
            "you confirmed. Install was not started."
        )


def install_update(
    *,
    confirm: bool = False,
    feed: str | None = None,
    dry_run: bool = False,
    current: str | None = None,
    config_dir: Path | None = None,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float = 60.0,
    user_profile: str | None = None,
    python_exe: str | None = None,
    relaunch_project: str | None = None,
    wait_pid: int | None = None,
    stamp: str | None = None,
    runner: Callable[..., None] | None = None,
    launch: bool | None = None,
    expected_version: str | None = None,
    expected_sha256: str | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Download, verify, and hand off to the installer.

    ``confirm`` must be exactly True. A dry run still requires it, still verifies
    the sha256, and does not start the installer.
    """

    from panelcad.update.handoff import (
        apply_upgrade,
        default_user_profile,
        plan_upgrade,
        start_detached,
    )

    if confirm is not True:
        raise UpdateError(
            "update_install requires confirm=True. PanelCAD does not install updates by itself."
        )
    _cancelled(cancel)
    config_dir = _runtime_config(config_dir)
    report = check_for_update(
        feed=feed,
        current=current,
        config_dir=config_dir,
        record=True,
        ssl_context=ssl_context,
        timeout=timeout,
    )
    if not report.get("enabled"):
        raise UpdateError(
            "No update feed is configured. Set one in Help > Updates or PANELCAD_UPDATE_FEED."
        )
    _require_expected_manifest(report, expected_version, expected_sha256)
    _cancelled(cancel)
    if report.get("blocked"):
        raise UpdateError(
            str(report.get("block_reason") or "This release cannot be installed on this version.")
        )
    if not report.get("installable"):
        raise UpdateError(str(report.get("reason") or "No update is available."))
    manifest = ReleaseManifest(
        version=str(report["version"]),
        release_date=str(report.get("release_date") or ""),
        notes=str(report.get("notes") or ""),
        download=str(report["download"]),
        size=report.get("size"),
        sha256=str(report["sha256"]),
        min_version=str(report.get("min_version") or ""),
    )
    archive: Path | None = None
    launched = False
    try:
        _cancelled(cancel)
        archive = stage_installer(
            manifest,
            str(report["manifest"]),
            timeout=timeout,
            ssl_context=ssl_context,
            cancel=cancel,
        )
        extracted = safe_extract(archive, archive.parent / "extracted")
        script = find_installer_script(extracted)
        _cancelled(cancel)
        profile = user_profile or default_user_profile()
        plan = plan_upgrade(
            installer=str(script),
            new_version=manifest.version,
            previous_version=str(report["current"]),
            user_profile=profile,
            config_dir=str(config_dir),
            python_exe=python_exe,
            relaunch_project=relaunch_project,
            wait_pid=wait_pid,
            stamp=stamp,
        )
        _log_update("update_install", plan.install_root)
        _log_update("update_backup", plan.backup_dir)
        _log_update("update_download", report.get("download_resolved") or manifest.download)
        if dry_run:
            result = plan.to_dict()
            result.update(
                {
                    "dry_run": True,
                    "started": False,
                    "close_app": False,
                    "verified": True,
                    "archive": str(archive),
                    "sha256": manifest.sha256,
                }
            )
            return result
        should_launch = (os.name == "nt") if launch is None else launch
        if runner is not None or not should_launch:
            if runner is None and os.name != "nt":
                raise UpdateError(
                    "Installing an update runs on Windows after the download is verified. "
                    "Use dry_run to preview the handoff."
                )
            _cancelled(cancel)
            result = apply_upgrade(plan, dry_run=False, runner=runner, relaunch=False)
            result["verified"] = True
            result["archive"] = str(archive)
            return result
        _cancelled(cancel)
        start_detached(plan, kind="upgrade")
        launched = True
        return {
            "started": True,
            "close_app": True,
            "dry_run": False,
            "verified": True,
            "version": manifest.version,
            "command": list(plan.command),
            "backup_dir": plan.backup_dir,
            "install_root": plan.install_root,
            "note": "The installer starts after this process exits.",
        }
    finally:
        # The detached installer still needs the extracted script. Every other
        # path (dry run, refusal, in-process runner) can drop the temp folder.
        if archive is not None and not launched:
            cleanup_stage(archive)


def rollback_update(
    *,
    confirm: bool = False,
    dry_run: bool = False,
    config_dir: Path | None = None,
    user_profile: str | None = None,
    python_exe: str | None = None,
    relaunch_project: str | None = None,
    wait_pid: int | None = None,
    runner: Callable[..., None] | None = None,
    launch: bool | None = None,
) -> dict[str, Any]:
    """Restore program files from the saved backup. Requires confirm=True."""

    from panelcad.update.handoff import (
        apply_rollback,
        default_python,
        default_user_profile,
        plan_rollback,
        start_detached,
    )

    if confirm is not True:
        raise UpdateError("update_rollback requires confirm=True.")
    config_dir = _runtime_config(config_dir)
    info = read_rollback(config_dir)
    if not info:
        raise UpdateError("No previous install is saved.")
    backup = str(info.get("backup") or "")
    install_root = str(info.get("install_root") or "")
    previous = str(info.get("previous_version") or "")
    if not backup or not install_root or not previous:
        raise UpdateError("The rollback record is missing a backup path.")
    profile = user_profile or default_user_profile()
    config = str(config_dir)
    python = python_exe or default_python(profile)
    plan = plan_rollback(
        previous_version=previous,
        backup_dir=backup,
        install_root=install_root,
        user_profile=profile,
        config_dir=config,
        python_exe=python,
        relaunch_project=relaunch_project,
        wait_pid=wait_pid,
    )
    _log_update("update_rollback", plan.install_root)
    _log_update("update_backup", plan.backup_dir)
    if dry_run:
        result = plan.to_dict()
        result["dry_run"] = True
        result["started"] = False
        return result
    should_launch = (os.name == "nt") if launch is None else launch
    if runner is not None or not should_launch:
        if runner is None and os.name != "nt":
            raise UpdateError("Rolling back runs on Windows. Use dry_run to preview it.")
        return apply_rollback(plan, runner=runner, relaunch=False)
    start_detached(plan, kind="rollback")
    return {
        "started": True,
        "close_app": True,
        "dry_run": False,
        "previous_version": previous,
        "note": "Rollback starts after this process exits.",
    }
