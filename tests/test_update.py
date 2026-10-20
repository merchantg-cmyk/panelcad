"""Release feed, version order, sha256 refusal, and the Windows upgrade handoff."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
)

from panelcad.cli.agent import Agent
from panelcad.update.feed import classify_feed, resolve_download, resolve_feed_location
from panelcad.update.handoff import apply_rollback, apply_upgrade, plan_rollback, plan_upgrade
from panelcad.update.manifest import UpdateError, parse_manifest
from panelcad.update.publish import publish_feed
from panelcad.update.service import (
    check_for_update,
    install_update,
    rollback_update,
    startup_should_prompt,
)
from panelcad.update.settings import (
    UpdateSettings,
    consume_notice,
    load_settings,
    save_settings,
    startup_check_due,
    write_rollback,
)
from panelcad.update.version import compare_versions, is_newer, parse_version
from panelcad.version import __version__
from tests.artifacts import artifact_dir

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    config = tmp_path / "panelcad-config"
    config.mkdir()
    monkeypatch.setenv("PANELCAD_CONFIG_DIR", str(config))
    monkeypatch.delenv("PANELCAD_UPDATE_FEED", raising=False)
    return config


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup_zip(path: Path, version: str = "0.9.0") -> None:
    prefix = f"PanelCAD-{version}"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{prefix}/scripts/Install-PanelCAD.ps1", "Write-Host installer\r\n")
        zf.writestr(f"{prefix}/pyproject.toml", "[project]\nname = \"panelcad\"\n")
        zf.writestr(f"{prefix}/src/panelcad/version.py", f'__version__ = "{version}"\n')


def _manifest(zip_path: Path, **overrides: object) -> dict:
    data = {
        "version": "0.9.0",
        "release_date": "2026-10-08",
        "notes": "Shop update.\nKeeps drawings and the user part library.",
        "download": zip_path.name,
        "size": zip_path.stat().st_size,
        "sha256": _sha(zip_path),
        "min_version": "0.4.0",
    }
    data.update(overrides)
    return data


def _write_feed(folder: Path, zip_path: Path, **overrides: object) -> Path:
    feed = folder / "panelcad-feed.json"
    feed.write_text(json.dumps(_manifest(zip_path, **overrides), indent=2) + "\n", encoding="utf-8")
    return feed


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.4.0", "0.5.0"),
        ("0.5.9", "0.5.10"),
        ("0.9", "0.10.0"),
        ("1.0.0a1", "1.0.0b1"),
        ("1.0.0b1", "1.0.0rc1"),
        ("1.0.0rc1", "1.0.0"),
        ("1.0.0alpha1", "1.0.0"),
        ("1.0.0beta2", "1.0.0rc1"),
        ("1.0.0preview1", "1.0.0"),
        ("1.0.0", "1.0.0.post1"),
        ("1.0.0.dev1", "1.0.0a1"),
        ("1.0.0a1.dev1", "1.0.0a1"),
        ("1.0.0a1", "1.0.0a2"),
        ("v0.4.0", "0.4.1"),
        ("1!0.4.0", "1!0.4.1"),
    ],
)
def test_version_order_including_prerelease(older: str, newer: str) -> None:
    assert compare_versions(older, newer) < 0
    assert compare_versions(newer, older) > 0
    assert is_newer(newer, older)
    assert not is_newer(older, newer)


def test_patch_ten_is_newer_than_patch_nine() -> None:
    """0.5.10 is newer than 0.5.9. A character compare says the opposite."""

    assert compare_versions("0.5.10", "0.5.9") == 1
    assert is_newer("0.5.10", "0.5.9")
    assert not is_newer("0.5.9", "0.5.10")
    assert "0.5.10" < "0.5.9"


def test_version_equality_ignores_trailing_zeros() -> None:
    assert compare_versions("1.0", "1.0.0") == 0
    assert compare_versions("v1.2.3", "1.2.3") == 0
    assert parse_version("1.0.0+shop") > parse_version("1.0.0")


def test_version_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_version("not-a-version")
    with pytest.raises(ValueError):
        parse_version("")


def test_manifest_parses_aliases_and_requires_sha() -> None:
    manifest = parse_manifest(
        {
            "version": "0.9.0rc1",
            "date": "2026-10-08",
            "body": "Notes",
            "url": "PanelCAD-0.9.0rc1-setup.zip",
            "bytes": 10,
            "checksum": "ab" * 32,
            "minimum_supported_version": "0.4.0",
            "channel": "shop",
        }
    )
    assert manifest.version == "0.9.0rc1"
    assert manifest.release_date == "2026-10-08"
    assert manifest.notes == "Notes"
    assert manifest.download.endswith(".zip")
    assert manifest.size == 10
    assert manifest.min_version == "0.4.0"
    with pytest.raises(UpdateError, match="sha256"):
        parse_manifest({"version": "0.9.0", "download": "a.zip", "sha256": "abcd"})
    with pytest.raises(UpdateError, match="version"):
        parse_manifest({"download": "a.zip", "sha256": "ab" * 32})


def test_drive_and_unc_feeds_are_files_not_urls() -> None:
    drive_file = r"G:\My Drive\ECS\PanelCAD\panelcad-feed.json"
    drive_dir = r"G:\My Drive\ECS\PanelCAD"
    unc = r"\\fileserver\share\PanelCAD\panelcad-feed.json"
    unc_dir = r"\\fileserver\share\PanelCAD"
    assert classify_feed(drive_file) == "file"
    assert classify_feed(unc) == "unc"
    assert classify_feed("https://example.invalid/panelcad-feed.json") == "https"
    assert resolve_feed_location(drive_file) == drive_file
    assert resolve_feed_location(drive_dir) == drive_file
    assert resolve_feed_location(unc) == unc
    assert resolve_feed_location(unc_dir) == unc
    assert resolve_feed_location("G:/My Drive/ECS/PanelCAD") == drive_file
    resolved = resolve_feed_location("https://example.invalid/releases")
    assert resolved == "https://example.invalid/releases/panelcad-feed.json"
    download = resolve_download(drive_file, "PanelCAD-0.9.0-setup.zip")
    assert download == r"G:\My Drive\ECS\PanelCAD\PanelCAD-0.9.0-setup.zip"
    with pytest.raises(UpdateError, match="https"):
        resolve_feed_location("http://example.invalid/panelcad-feed.json")


def test_file_feed_directory_and_relative_zip(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    feed = _write_feed(tmp_path, archive)
    report = check_for_update(feed=str(tmp_path), current="0.4.0", record=False)
    assert report["enabled"] is True
    assert report["version"] == "0.9.0"
    assert report["update_available"] is True
    assert report["installable"] is True
    assert report["download_resolved"] == str(archive)
    again = check_for_update(feed=str(feed), current="0.9.0", record=False)
    assert again["update_available"] is False
    older = check_for_update(feed=str(feed), current="1.0.0", record=False)
    assert older["update_available"] is False


def test_prerelease_feed_is_newer_than_previous_tag_only(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0rc1-setup.zip"
    _setup_zip(archive, "0.9.0rc1")
    _write_feed(tmp_path, archive, version="0.9.0rc1", download=archive.name)
    assert check_for_update(feed=str(tmp_path), current="0.9.0a1", record=False)["installable"]
    final = check_for_update(feed=str(tmp_path), current="0.9.0", record=False)
    assert final["update_available"] is False


def test_min_version_blocks_install(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive, min_version="0.8.0")
    report = check_for_update(feed=str(tmp_path), current="0.4.0", record=False)
    assert report["update_available"] is True
    assert report["blocked"] is True
    assert report["installable"] is False
    with pytest.raises(UpdateError, match="0.8.0"):
        install_update(confirm=True, feed=str(tmp_path), current="0.4.0", dry_run=True)


@contextmanager
def _https_server(directory: Path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    cert = directory.parent / "cert.pem"
    key = directory.parent / "key.pem"
    # Windows openssl often has no ``-addext``. The SAN is written in a config
    # file so the same command works there.
    config = directory.parent / "openssl-san.cnf"
    config.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name = req_distinguished_name",
                "x509_extensions = v3_req",
                "prompt = no",
                "[req_distinguished_name]",
                "CN = 127.0.0.1",
                "[v3_req]",
                "subjectAltName = IP:127.0.0.1",
                "",
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-config",
            str(config),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace")[:400]
        pytest.skip(f"openssl could not make a SAN certificate: {detail}")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, fmt: str, *args) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    client = ssl.create_default_context(cafile=str(cert))
    try:
        yield f"https://127.0.0.1:{port}", client
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_https_feed_against_local_server(tmp_path: Path) -> None:
    public = tmp_path / "public"
    public.mkdir()
    archive = public / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(public, archive)
    with _https_server(public) as (base, client):
        report = check_for_update(
            feed=f"{base}/panelcad-feed.json",
            current="0.4.0",
            record=False,
            ssl_context=client,
        )
    assert report["enabled"] is True
    assert report["version"] == "0.9.0"
    assert report["sha256"] == _sha(archive)
    assert report["download_resolved"] == f"{base}/PanelCAD-0.9.0-setup.zip"


def test_sha_mismatch_refuses_and_does_not_call_installer(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive, sha256="ab" * 32)
    calls: list[object] = []
    with pytest.raises(UpdateError, match="sha256 mismatch"):
        install_update(
            confirm=True,
            feed=str(tmp_path),
            current="0.4.0",
            user_profile=str(tmp_path / "Ada"),
            runner=lambda plan: calls.append(plan),
        )
    assert calls == []
    assert archive.is_file()


def test_size_mismatch_refuses(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive, size=1)
    with pytest.raises(UpdateError, match="size mismatch"):
        install_update(confirm=True, feed=str(tmp_path), current="0.4.0", dry_run=True)


def test_no_feed_means_checker_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("panelcad.update.builtin.default_feed_url", lambda: "")
    report = check_for_update(current="0.4.0", record=False)
    assert report["enabled"] is False
    assert report["reason"] == "no feed configured"
    assert startup_check_due(UpdateSettings()) is False


def test_skip_version_suppresses_startup_prompt(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive)
    config = Path(os.environ["PANELCAD_CONFIG_DIR"])
    save_settings(UpdateSettings(feed=str(tmp_path), skip_version="0.9.0"), config)
    report = check_for_update(current="0.4.0", record=False, config_dir=config)
    assert report["skipped"] is True
    assert report["update_available"] is True
    assert report["installable"] is True
    assert startup_should_prompt(report) is False
    save_settings(UpdateSettings(feed=str(tmp_path), skip_version="0.8.0"), config)
    prompted = check_for_update(current="0.4.0", record=False, config_dir=config)
    assert prompted["skipped"] is False
    assert startup_should_prompt(prompted) is True


def test_startup_check_at_most_once_per_day() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    settings = UpdateSettings(feed=r"G:\My Drive\ECS\PanelCAD", check_on_startup=True)
    assert startup_check_due(settings, now=now, feed=settings.feed) is True
    settings.last_check = "2026-10-08T11:00:00Z"
    assert startup_check_due(settings, now=now, feed=settings.feed) is False
    settings.last_check = "2026-10-07T11:00:00Z"
    assert startup_check_due(settings, now=now, feed=settings.feed) is True
    settings.check_on_startup = False
    assert startup_check_due(settings, now=now, feed=settings.feed) is False
    stale = now - timedelta(hours=24)
    settings = UpdateSettings(
        feed=r"G:\My Drive\ECS\PanelCAD",
        last_check=stale.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    assert startup_check_due(settings, now=now, feed=settings.feed) is True


def test_env_feed_overrides_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive)
    other = tmp_path / "other"
    other.mkdir()
    save_settings(UpdateSettings(feed=str(other)))
    monkeypatch.setenv("PANELCAD_UPDATE_FEED", str(tmp_path))
    report = check_for_update(current="0.4.0", record=False)
    assert report["feed"] == str(tmp_path)
    assert report["version"] == "0.9.0"


def test_install_requires_confirm(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive)
    with pytest.raises(UpdateError, match="confirm=True"):
        install_update(confirm=False, feed=str(tmp_path), current="0.4.0", dry_run=True)
    agent = Agent()
    refused = agent.handle({"id": "u1", "op": "update_install", "confirm": "true", "dry_run": True})
    assert refused["ok"] is False
    assert "confirm" in refused["error"]


def test_windows_handoff_dry_run_does_not_call_installer() -> None:
    calls: list[object] = []
    installer = (
        r"C:\Users\Ada\AppData\Local\Temp\panelcad-update"
        r"\PanelCAD-0.5.0\scripts\Install-PanelCAD.ps1"
    )
    plan = plan_upgrade(
        installer=installer,
        new_version="0.5.0",
        previous_version="0.4.0",
        user_profile=r"C:\Users\Ada",
        python_exe=r"C:\Users\Ada\.venvs\panelcad\Scripts\python.exe",
        relaunch_project=r"C:\Users\Ada\Documents\Jobs\line1.pcad.yaml",
        wait_pid=4242,
        stamp="20261008-120000",
    )
    result = apply_upgrade(plan, dry_run=True, runner=lambda item: calls.append(item))
    assert calls == []
    assert result["dry_run"] is True
    assert result["started"] is False
    assert plan.install_root == r"C:\Users\Ada\PanelCAD"
    assert plan.backup_dir == r"C:\Users\Ada\.panelcad\backups\PanelCAD-0.4.0-20261008-120000"
    assert plan.command[0] == "powershell.exe"
    assert plan.command[-2:] == (installer, "-Quiet")
    assert "/MIR" not in plan.command
    assert plan.env == (("PANELCAD_INSTALL_QUIET", "1"), ("PANELCAD_BACKUP_DONE", "1"))
    assert r"C:\Users\Ada\.panelcad" in plan.preserved
    assert r"C:\Users\Ada\.panelcad\library" in plan.preserved
    assert r"C:\Users\Ada\.panelcad\logs" in plan.preserved
    assert r"C:\Users\Ada\.venvs\panelcad" in plan.preserved
    assert plan.relaunch[-1] == r"C:\Users\Ada\Documents\Jobs\line1.pcad.yaml"
    assert plan.notice_path == r"C:\Users\Ada\.panelcad\update-notice.json"
    assert "backup" in " ".join(plan.steps)
    assert plan.wait_pid == 4242


def test_upgrade_handoff_mocks_installer_and_keeps_user_data(tmp_path: Path) -> None:
    profile = tmp_path / "Ada"
    install = profile / "PanelCAD"
    install.mkdir(parents=True)
    (install / "version.py").write_text("0.4.0\n", encoding="utf-8")
    (install / "notes.txt").write_text("shop note\n", encoding="utf-8")
    library = profile / ".panelcad" / "library"
    library.mkdir(parents=True)
    (library / "user.yaml").write_text("id: shop.relay\n", encoding="utf-8")
    logs = profile / ".panelcad" / "logs"
    logs.mkdir()
    (logs / "app.log").write_text("log line\n", encoding="utf-8")
    venv = profile / ".venvs" / "panelcad"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = python\n", encoding="utf-8")
    drawing = profile / "Documents" / "Jobs"
    drawing.mkdir(parents=True)
    job = drawing / "line1.pcad.yaml"
    job.write_text("name: Line 1\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def runner(item) -> None:
        calls.append(item.command)
        (install / "version.py").write_text("0.5.0\n", encoding="utf-8")

    plan = plan_upgrade(
        installer=r"C:\Users\Ada\AppData\Local\Temp\panelcad-update\PanelCAD-0.5.0\scripts\Install-PanelCAD.ps1",
        new_version="0.5.0",
        previous_version="0.4.0",
        user_profile=str(profile),
        config_dir=str(profile / ".panelcad"),
        python_exe=str(venv / "bin" / "python"),
        relaunch_project=str(job),
        wait_pid=0,
        stamp="20261008-120000",
    )
    result = apply_upgrade(plan, runner=runner, relaunch=False)
    assert result["started"] is True
    assert calls and calls[0][-1] == "-Quiet"
    assert (install / "version.py").read_text(encoding="utf-8") == "0.5.0\n"
    backup = Path(plan.backup_dir)
    assert (backup / "version.py").read_text(encoding="utf-8") == "0.4.0\n"
    assert (backup / "notes.txt").read_text(encoding="utf-8") == "shop note\n"
    assert (library / "user.yaml").read_text(encoding="utf-8") == "id: shop.relay\n"
    assert (logs / "app.log").read_text(encoding="utf-8") == "log line\n"
    assert (venv / "pyvenv.cfg").is_file()
    assert job.read_text(encoding="utf-8") == "name: Line 1\n"
    notice = consume_notice(profile / ".panelcad")
    assert notice is not None
    assert notice["version"] == "0.5.0"
    assert notice["kind"] == "update"
    rollback = json.loads((profile / ".panelcad" / "rollback.json").read_text(encoding="utf-8"))
    assert rollback["previous_version"] == "0.4.0"
    assert Path(rollback["backup"]) == backup


def test_install_update_dry_run_verifies_then_returns_windows_plan(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive, notes="Ready for the floor.")
    calls: list[object] = []
    result = install_update(
        confirm=True,
        dry_run=True,
        feed=str(tmp_path),
        current="0.4.0",
        user_profile=r"C:\Users\Ada",
        python_exe=r"C:\Users\Ada\.venvs\panelcad\Scripts\python.exe",
        relaunch_project=r"C:\Users\Ada\Documents\Jobs\line1.pcad.yaml",
        stamp="20261008-120000",
        runner=lambda plan: calls.append(plan),
    )
    assert calls == []
    assert result["dry_run"] is True
    assert result["verified"] is True
    assert result["sha256"] == _sha(archive)
    assert result["install_root"] == r"C:\Users\Ada\PanelCAD"
    assert result["command"][-1] == "-Quiet"
    assert "Install-PanelCAD.ps1" in result["installer"]
    assert r"C:\Users\Ada\.venvs\panelcad" in result["preserved"]


def _write_program(root: Path, version: str) -> None:
    package = root / "src" / "panelcad"
    package.mkdir(parents=True, exist_ok=True)
    (package / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "Install-PanelCAD.ps1").write_text(f"# {version}\n", encoding="utf-8")


def test_rollback_dry_run_and_restore(tmp_path: Path) -> None:
    profile = tmp_path / "Ada"
    install = profile / "PanelCAD"
    _write_program(install, "0.5.0")
    (install / "src" / "panelcad" / "newmod.py").write_text("x = 1\n", encoding="utf-8")
    (install / "job.pcad.yaml").write_text("name: Line 1\n", encoding="utf-8")
    (install / "notes.txt").write_text("live note\n", encoding="utf-8")
    backup = profile / ".panelcad" / "backups" / "PanelCAD-0.4.0-20261008-120000"
    _write_program(backup, "0.4.0")
    (backup / "notes.txt").write_text("old note\n", encoding="utf-8")
    library = profile / ".panelcad" / "library"
    library.mkdir()
    (library / "user.yaml").write_text("kept\n", encoding="utf-8")
    write_rollback(
        previous_version="0.4.0",
        backup=str(backup),
        install_root=str(install),
        directory=profile / ".panelcad",
    )
    preview = rollback_update(
        confirm=True,
        dry_run=True,
        config_dir=profile / ".panelcad",
        user_profile=str(profile),
    )
    assert preview["dry_run"] is True
    assert preview["previous_version"] == "0.4.0"
    assert "pip" in preview["command"]
    assert '__version__ = "0.5.0"' in (install / "src" / "panelcad" / "version.py").read_text(
        encoding="utf-8"
    )
    calls: list[tuple[str, ...]] = []
    plan = plan_rollback(
        previous_version="0.4.0",
        backup_dir=str(backup),
        install_root=str(install),
        user_profile=str(profile),
        config_dir=str(profile / ".panelcad"),
        python_exe=str(profile / ".venvs" / "panelcad" / "bin" / "python"),
        wait_pid=0,
    )
    apply_rollback(plan, runner=lambda item: calls.append(item.command), relaunch=False)
    assert calls and calls[0][1:4] == ("-m", "pip", "install")
    restored = (install / "src" / "panelcad" / "version.py").read_text(encoding="utf-8")
    assert restored == '__version__ = "0.4.0"\n'
    assert (install / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8") == "# 0.4.0\n"
    assert not (install / "src" / "panelcad" / "newmod.py").exists()
    assert (install / "job.pcad.yaml").read_text(encoding="utf-8") == "name: Line 1\n"
    assert (install / "notes.txt").read_text(encoding="utf-8") == "live note\n"
    assert not list(profile.glob("PanelCAD.before-rollback-*"))
    assert (library / "user.yaml").read_text(encoding="utf-8") == "kept\n"
    assert backup.is_dir()
    notice = consume_notice(profile / ".panelcad")
    assert notice is not None and notice["kind"] == "rollback"
    assert notice["version"] == "0.4.0"


def _pyproject(version: str) -> str:
    return (
        "[project]\n"
        'name = "panelcad"\n'
        f'version = "{version}"\n'
        'requires-python = ">=3.11"\n'
        "\n"
        "[build-system]\n"
        'requires = ["setuptools>=69"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[tool.setuptools.packages.find]\n"
        'where = ["src"]\n'
    )


def _package_tree(root: Path, version: str) -> None:
    """A tiny install tree whose version lives in version.py and pyproject.toml."""

    _write_program(root, version)
    package = root / "src" / "panelcad"
    (package / "__init__.py").write_text(
        "from panelcad.version import __version__\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(_pyproject(version), encoding="utf-8")
    (root / "setup.cfg").write_text(
        f"[metadata]\nname = panelcad\ndescription = PanelCAD {version}\n",
        encoding="utf-8",
    )
    (root / "MANIFEST.in").write_text(f"# {version}\ninclude README.md\n", encoding="utf-8")


def _isolated_env() -> dict[str, str]:
    """Drop the repo and the outer venv so the temp install is the one on sys.path."""

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_BUILD_ISOLATION"] = "1"
    return env


def _venv_python(path: Path) -> Path:
    subprocess.check_call(
        [sys.executable, "-m", "venv", str(path)],
        timeout=180,
        env=_isolated_env(),
    )
    python = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.check_call(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "setuptools"],
        timeout=180,
        env=_isolated_env(),
    )
    return python


def _pip_editable(python: Path, command: tuple[str, ...]) -> None:
    completed = subprocess.run(
        list(command),
        check=False,
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _installed_versions(python: Path) -> tuple[str, str]:
    code = (
        "import importlib.metadata as m, panelcad; "
        "print(panelcad.__version__, m.version('panelcad'))"
    )
    completed = subprocess.run(
        [str(python), "-c", code],
        check=False,
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    printed = completed.stdout.strip().split()
    assert len(printed) == 2, completed.stdout
    return printed[0], printed[1]


def test_rollback_restores_packaging_so_metadata_matches_version(tmp_path: Path) -> None:
    """pip install -e must see the restored pyproject, not the version just replaced."""

    from panelcad.update.handoff import backup_install_tree

    profile = tmp_path / "Ada"
    install = profile / "PanelCAD"
    _package_tree(install, "0.5.0")
    (install / "job.pcad.yaml").write_text("name: Line 1\n", encoding="utf-8")
    (install / "notes.txt").write_text("old note\n", encoding="utf-8")
    backup = profile / ".panelcad" / "backups" / "PanelCAD-0.5.0-20261008-120000"
    backup_install_tree(install, backup)
    assert 'version = "0.5.0"' in (backup / "pyproject.toml").read_text(encoding="utf-8")
    assert "PanelCAD 0.5.0" in (backup / "setup.cfg").read_text(encoding="utf-8")
    assert (backup / "MANIFEST.in").read_text(encoding="utf-8").startswith("# 0.5.0\n")

    _package_tree(install, "0.5.99+test")
    (install / "setup.py").write_text(
        "# 0.5.99+test\nfrom setuptools import setup\nsetup()\n",
        encoding="utf-8",
    )
    (install / "job.pcad.yaml").write_text("name: After\n", encoding="utf-8")
    (install / "notes.txt").write_text("after upgrade\n", encoding="utf-8")
    python = _venv_python(tmp_path / "venv")
    _pip_editable(python, (str(python), "-m", "pip", "install", "-e", str(install)))
    assert _installed_versions(python) == ("0.5.99+test", "0.5.99+test")

    plan = plan_rollback(
        previous_version="0.5.0",
        backup_dir=str(backup),
        install_root=str(install),
        user_profile=str(profile),
        config_dir=str(profile / ".panelcad"),
        python_exe=str(python),
        wait_pid=0,
    )
    apply_rollback(plan, runner=lambda item: _pip_editable(python, item.command), relaunch=False)
    assert _installed_versions(python) == ("0.5.0", "0.5.0")
    text = (install / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.5.0"' in text
    assert "0.5.99" not in text
    assert not (install / "setup.py").exists()
    assert (install / "setup.cfg").read_text(encoding="utf-8") == (
        backup / "setup.cfg"
    ).read_text(encoding="utf-8")
    assert (install / "MANIFEST.in").read_text(encoding="utf-8") == (
        backup / "MANIFEST.in"
    ).read_text(encoding="utf-8")
    assert (install / "notes.txt").read_text(encoding="utf-8") == "after upgrade\n"
    assert (install / "job.pcad.yaml").read_text(encoding="utf-8") == "name: After\n"
    restored = (install / "src" / "panelcad" / "version.py").read_text(encoding="utf-8")
    assert restored == '__version__ = "0.5.0"\n'


def test_publish_feed_writes_sha_and_updates(tmp_path: Path) -> None:
    archive = tmp_path / "dist" / "PanelCAD-0.9.0-setup.zip"
    archive.parent.mkdir()
    _setup_zip(archive)
    feed_dir = tmp_path / "synced"
    path = publish_feed(
        zip_path=archive,
        feed=str(feed_dir),
        notes="First publish.\nSecond line.",
        release_date="2026-10-08",
        min_version="0.4.0",
    )
    assert path.name == "panelcad-feed.json"
    assert (feed_dir / archive.name).is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "0.9.0"
    assert data["sha256"] == _sha(archive)
    assert data["size"] == archive.stat().st_size
    assert data["download"] == archive.name
    assert data["notes"].startswith("First")
    assert data["min_version"] == "0.4.0"
    data["channel"] = "shop"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    publish_feed(
        zip_path=archive,
        feed=str(path),
        notes="Updated notes.",
        release_date="2026-10-09",
        copy_zip=False,
    )
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["notes"] == "Updated notes."
    assert updated["release_date"] == "2026-10-09"
    assert updated["channel"] == "shop"
    assert updated["sha256"] == _sha(archive)
    report = check_for_update(feed=str(feed_dir), current=__version__, record=False)
    assert report["version"] == "0.9.0"


def test_agent_and_session_check(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    feed = _write_feed(tmp_path, archive)
    session = Session.create("Untitled")
    report = session.update_check(feed=str(feed))
    assert report["installable"] is True
    agent = Agent()
    checked = agent.handle({"id": "c1", "op": "update_check", "feed": str(feed)})
    assert checked["ok"] is True
    assert checked["result"]["version"] == "0.9.0"
    dry = agent.handle(
        {"id": "c2", "op": "update_install", "confirm": True, "dry_run": True, "feed": str(feed)}
    )
    assert dry["ok"] is True, dry
    assert dry["result"]["verified"] is True
    assert dry["result"]["dry_run"] is True
    help_r = agent.handle({"id": "h", "op": "help"})
    assert "update_check" in help_r["result"]["ops"]
    assert "update_install" in help_r["result"]["ops"]
    assert "confirm" in help_r["result"]["schemas"]["update"]["update_install"]["args"]
    missing = agent.handle({"id": "c3", "op": "update_rollback", "confirm": True, "dry_run": True})
    assert missing["ok"] is False


def test_cli_check_install_and_publish(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    runner = CliRunner()
    published = runner.invoke(
        app,
        [
            "release",
            "publish-feed",
            "--zip",
            str(archive),
            "--feed",
            str(tmp_path / "feed"),
            "--notes",
            "CLI notes",
            "--date",
            "2026-10-08",
            "--min-version",
            "0.4.0",
        ],
    )
    assert published.exit_code == 0, published.stdout + published.stderr
    assert "panelcad-feed.json" in published.stdout
    checked = runner.invoke(
        app,
        ["update", "check", "--feed", str(tmp_path / "feed"), "--json"],
    )
    assert checked.exit_code == 0, checked.stdout + checked.stderr
    payload = json.loads(checked.stdout)
    assert payload["version"] == "0.9.0"
    refused = runner.invoke(
        app,
        ["update", "install", "--feed", str(tmp_path / "feed"), "--dry-run"],
    )
    assert refused.exit_code == 1
    assert "confirm" in refused.stderr
    dry = runner.invoke(
        app,
        [
            "update",
            "install",
            "--confirm",
            "--dry-run",
            "--feed",
            str(tmp_path / "feed"),
        ],
    )
    assert dry.exit_code == 0, dry.stdout + dry.stderr
    body = json.loads(dry.stdout)
    assert body["verified"] is True
    assert body["dry_run"] is True


def test_zip_slip_is_refused(tmp_path: Path) -> None:
    from panelcad.update.fetch import safe_extract

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../outside.txt", "nope")
    with pytest.raises(UpdateError, match="escapes"):
        safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "outside.txt").exists()


def test_installer_quiet_switch_and_backup_are_not_a_mirror() -> None:
    text = (ROOT / "scripts" / "Install-PanelCAD.ps1").read_text(encoding="utf-8")
    cmd = (ROOT / "Install-PanelCAD.cmd").read_text(encoding="utf-8")
    assert "param(" in text
    assert "[switch]$Quiet" in text
    assert "PANELCAD_INSTALL_QUIET" in text
    assert "PANELCAD_BACKUP_DONE" in text
    assert "Backup-ExistingInstall" in text
    assert "install_tree.py" in text
    assert "/MIR" not in text
    assert "robocopy.exe" not in text
    backup = text[text.index("function Backup-ExistingInstall") : text.index("function Write-UpdateNotice")]
    assert '$env:PANELCAD_BACKUP_DONE -eq "1"' in backup
    assert backup.index("PANELCAD_BACKUP_DONE") < backup.index("--source")
    assert "pip install -e $InstallRoot" in text
    assert ".panelcad" in text
    assert "/quiet" in cmd
    assert "-Quiet" in cmd
    assert "shortcut_action" in text
    assert "--shortcut-exists" in text
    assert "IsNullOrWhiteSpace($existingWork)" in text
    assert 'throw "Could not decide whether to update the desktop shortcut."' not in text
    assert "Leaving it unchanged." in text
    assert text.index("pip install -e $InstallRoot") < text.index("Leaving it unchanged.")
    assert "Not changing PanelCAD.lnk" in text
    assert "it points at" in text
    assert text.index("it points at") < text.rindex("$link.Save()")
    assert "keep_backups" in text
    assert "Select-Object -Skip $keep" in text


def _save_shot(widget, directory: Path, name: str) -> Path:
    path = directory / name
    pixmap = widget.grab()
    assert pixmap.width() > 400
    assert pixmap.height() > 120
    assert pixmap.save(str(path))
    return path


def _assert_separated(dialog, names: list[str]) -> None:
    boxes = []
    for name in names:
        widget = dialog.findChild(QPushButton, name)
        assert widget is not None and widget.isVisible()
        rect = widget.geometry()
        assert rect.width() > 40 and rect.height() > 16
        assert rect.right() <= dialog.width() + 1
        assert rect.bottom() <= dialog.height() + 1
        for other in boxes:
            assert not rect.intersects(other)
        boxes.append(rect)


def test_update_dialogs_render_notes_and_actions(tmp_path: Path) -> None:
    from panelcad.gui.update_dialog import (
        RollbackDialog,
        UpdateAvailableDialog,
        UpdateNoticeDialog,
        UpdateSettingsDialog,
    )

    app = _app()
    shots = artifact_dir(tmp_path)
    report = {
        "version": "0.9.0",
        "current": __version__,
        "release_date": "2026-10-08",
        "installable": True,
        "blocked": False,
        "size": 1200,
        "feed": r"G:\My Drive\ECS\PanelCAD\panelcad-feed.json",
        "notes": (
            "Shop release.\n"
            "Upgrade keeps drawings, settings, logs, and the user part library.\n"
            "The previous program folder is saved for Help → Roll back to previous version.\n"
            "The download is refused if the SHA-256 does not match the feed."
        ),
    }
    available = UpdateAvailableDialog(report)
    available.resize(760, available.sizeHint().height())
    available.show()
    app.processEvents()
    available.adjustSize()
    available.resize(760, available.sizeHint().height())
    app.processEvents()
    notes = available.findChild(QPlainTextEdit)
    assert notes is not None
    assert "SHA-256" in notes.toPlainText()
    line_count = notes.toPlainText().count("\n") + 1
    assert notes.viewport().height() >= line_count * notes.fontMetrics().lineSpacing()
    released = available.findChild(QLabel, "updateReleaseDate")
    assert released is not None
    assert released.text() == "2026-10-08"
    assert released.textFormat() == Qt.TextFormat.PlainText
    meta = available.findChild(QLabel, "updateMeta")
    assert meta is not None and "1200" in meta.text()
    _assert_separated(available, ["updateInstall", "updateLater", "updateSkip"])
    _save_shot(available, shots, "update-available-dialog.png")
    available.findChild(QPushButton, "updateSkip").click()
    app.processEvents()
    assert available.choice == "skip"

    settings = UpdateSettingsDialog(
        UpdateSettings(feed=r"G:\My Drive\ECS\PanelCAD\panelcad-feed.json")
    )
    settings.resize(760, settings.sizeHint().height())
    settings.show()
    app.processEvents()
    assert settings.feed.text().startswith("G:\\My Drive")
    _save_shot(settings, shots, "update-settings-dialog.png")
    settings.close()

    notice = UpdateNoticeDialog("0.9.0")
    notice.adjustSize()
    notice.resize(640, notice.sizeHint().height())
    notice.show()
    app.processEvents()
    headline = next(
        label.text() for label in notice.findChildren(QLabel) if "Updated to" in label.text()
    )
    assert headline == "Updated to 0.9.0"
    _save_shot(notice, shots, "update-installed-notice.png")
    notice.close()

    rollback = RollbackDialog(
        "0.4.0",
        r"C:\Users\Ada\.panelcad\backups\PanelCAD-0.4.0-20261008-120000",
    )
    rollback.adjustSize()
    rollback.resize(720, rollback.sizeHint().height())
    rollback.show()
    app.processEvents()
    path_label = rollback.findChild(QLabel, "updateBackupPath")
    assert path_label is not None
    assert "\n" not in path_label.text()
    assert path_label.toolTip().endswith("20261008-120000")
    _save_shot(rollback, shots, "update-rollback-dialog.png")
    rollback.close()

    failed = UpdateNoticeDialog(
        "rollback/update failed: see log",
        kind="failed",
        message="WinError 32",
        log=r"C:\Users\Ada\.panelcad\logs\rollback-20261008-120000.log",
    )
    failed.adjustSize()
    failed.resize(720, failed.sizeHint().height())
    failed.show()
    app.processEvents()
    failed_title = next(
        label.text()
        for label in failed.findChildren(QLabel)
        if "failed" in label.text()
    )
    assert failed_title == "rollback/update failed: see log"
    _save_shot(failed, shots, "update-failed-notice.png")
    failed.close()

    sample = {
        "version": "0.9.0",
        "release_date": "2026-10-08",
        "notes": report["notes"],
        "download": "PanelCAD-0.9.0-setup.zip",
        "size": 1200,
        "sha256": "ab" * 32,
        "min_version": "0.4.0",
    }
    (shots / "panelcad-feed.sample.json").write_text(
        json.dumps(sample, indent=2) + "\n",
        encoding="utf-8",
    )
    assert load_settings().feed == ""


def test_manifest_and_publish_reject_bad_versions(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    sha = "ab" * 32
    with pytest.raises(UpdateError, match="PEP 440"):
        parse_manifest(
            {
                "version": "banana",
                "download": "PanelCAD-setup.zip",
                "sha256": sha,
            }
        )
    with pytest.raises(UpdateError, match="min_version"):
        parse_manifest(
            {
                "version": "0.5.0",
                "min_version": "0.4.99-test",
                "download": "PanelCAD-setup.zip",
                "sha256": sha,
            }
        )
    with pytest.raises(UpdateError, match="YYYY-MM-DD"):
        parse_manifest(
            {
                "version": "0.5.0",
                "release_date": "October 8",
                "download": "PanelCAD-setup.zip",
                "sha256": sha,
            }
        )
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    runner = CliRunner()
    published = runner.invoke(
        app,
        [
            "release",
            "publish-feed",
            "--zip",
            str(archive),
            "--feed",
            str(tmp_path / "feed"),
            "--version",
            "0.4.99-test",
        ],
    )
    assert published.exit_code == 1
    assert "PEP 440" in published.stderr
    assert "Traceback" not in published.stderr + published.stdout


def test_install_refuses_when_the_shown_manifest_changes(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    feed = _write_feed(tmp_path, archive)
    report = check_for_update(feed=str(tmp_path), current="0.4.0", record=False)
    data = json.loads(feed.read_text(encoding="utf-8"))
    data["sha256"] = "cd" * 32
    feed.write_text(json.dumps(data) + "\n", encoding="utf-8")
    calls: list[object] = []
    with pytest.raises(UpdateError, match="feed changed"):
        install_update(
            confirm=True,
            feed=str(tmp_path),
            current="0.4.0",
            expected_version=str(report["version"]),
            expected_sha256=str(report["sha256"]),
            runner=lambda plan: calls.append(plan),
        )
    assert calls == []
    assert archive.is_file()


def test_install_uses_panelcad_config_dir(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive)
    result = install_update(
        confirm=True,
        dry_run=True,
        feed=str(tmp_path),
        current="0.4.0",
        user_profile=str(tmp_path / "Ada"),
    )
    assert result["config_dir"] == os.environ["PANELCAD_CONFIG_DIR"]
    assert not Path(result["archive"]).exists()


def test_update_check_survives_a_cp1252_pipe(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    env["PANELCAD_CONFIG_DIR"] = str(tmp_path / "cfg")
    env.pop("PANELCAD_UPDATE_FEED", None)
    env["PYDANTIC_AI_NO_BANNER"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "panelcad", "update", "check"],
        capture_output=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert b"UnicodeEncodeError" not in proc.stderr
    assert b"Help > Updates" in proc.stdout


def test_handoff_module_has_no_runpy_warning() -> None:
    proc = subprocess.run(
        [sys.executable, "-W", "always", "-m", "panelcad.update.handoff", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "found in sys.modules" not in proc.stderr


def test_windows_pid_uses_exit_code_not_ctrl_c() -> None:
    from panelcad.update.handoff import windows_pid_alive

    class FakeKernel:
        def __init__(self, handle: int, code: int = 259, error: int = 0, ok: bool = True) -> None:
            self.handle = handle
            self.code = code
            self.error = error
            self.ok = ok
            self.closed: list[int] = []

        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            assert access == 0x1000
            assert inherit is False
            assert pid > 0
            return self.handle

        def GetLastError(self) -> int:
            return self.error

        def GetExitCodeProcess(self, handle: int, ref: object) -> int:
            if not self.ok:
                return 0
            ref._obj.value = self.code  # type: ignore[attr-defined]
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.closed.append(handle)
            return 1

    alive = FakeKernel(1, code=259)
    assert windows_pid_alive(10, alive) is True
    assert alive.closed == [1]
    assert windows_pid_alive(10, FakeKernel(1, code=0)) is False
    assert windows_pid_alive(10, FakeKernel(0, error=5)) is True
    assert windows_pid_alive(10, FakeKernel(0, error=87)) is False


def test_handoff_helper_leaves_the_install_folder_and_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.update.handoff import main as handoff_main

    relaunched: list[tuple[tuple[str, ...], str]] = []

    def record_relaunch(command: tuple[str, ...], cwd: str) -> None:
        relaunched.append((tuple(command), cwd))

    # On Windows the real helper Popen's a detached GUI and leaves it running.
    monkeypatch.setattr("panelcad.update.handoff._relaunch", record_relaunch)
    install = tmp_path / "PanelCAD"
    install.mkdir()
    (install / "job.pcad.yaml").write_text("name: Line 1\n", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    plan = plan_rollback(
        previous_version="0.4.0",
        backup_dir=str(tmp_path / "missing-backup"),
        install_root=str(install),
        user_profile=str(tmp_path),
        config_dir=str(config),
        wait_pid=0,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"kind": "rollback", "plan": plan.to_dict()}) + "\n",
        encoding="utf-8",
    )
    previous = Path.cwd()
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    try:
        os.chdir(install)
        code = handoff_main([str(plan_path)])
        assert Path.cwd().resolve() != install.resolve()
        assert install.resolve() not in Path.cwd().resolve().parents
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        os.chdir(previous)
    assert code == 1
    assert (install / "job.pcad.yaml").read_text(encoding="utf-8") == "name: Line 1\n"
    assert not list(tmp_path.glob("PanelCAD.before-rollback-*"))
    notice = json.loads((config / "update-notice.json").read_text(encoding="utf-8"))
    assert notice["kind"] == "failed"
    assert notice["version"] == "rollback/update failed: see log"
    logs = list((config / "logs").glob("*.log"))
    assert len(logs) == 1
    body = logs[0].read_text(encoding="utf-8")
    assert "working folder" in body
    assert "Backup not found" in body
    assert body.index("working folder") < body.index("Backup not found")
    assert len(relaunched) == 1
    command, cwd = relaunched[0]
    assert command[-3:] == ("-m", "panelcad", "gui")
    assert len(command) == 4
    assert Path(cwd).resolve() == install.resolve()


def test_start_detached_uses_a_neutral_folder_and_a_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from panelcad.update.handoff import start_detached

    install = tmp_path / "PanelCAD"
    install.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    captured: dict[str, object] = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs

        class Proc:
            pid = 1

        return Proc()

    monkeypatch.setattr("panelcad.update.handoff.subprocess.Popen", fake_popen)
    plan = plan_rollback(
        previous_version="0.4.0",
        backup_dir=str(config / "backups" / "old"),
        install_root=str(install),
        user_profile=str(tmp_path),
        config_dir=str(config),
        wait_pid=0,
    )
    previous = Path.cwd()
    try:
        os.chdir(install)
        start_detached(plan, kind="rollback")
    finally:
        os.chdir(previous)
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    cwd = Path(str(kwargs["cwd"])).resolve()
    assert cwd != install.resolve()
    assert install.resolve() not in cwd.parents
    assert kwargs["stdout"] is not subprocess.DEVNULL
    logs = list((config / "logs").glob("rollback-*.log"))
    assert len(logs) == 1


def _load_install_tree():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "install_tree_shortcut", ROOT / "scripts" / "install_tree.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _powershell_drops_empty(args: list[str]) -> list[str]:
    """Windows PowerShell omits empty strings when splatting a native program."""

    return [item for item in args if item != ""]


def test_shortcut_is_not_rewritten_for_a_different_install() -> None:
    shortcut_action = _load_install_tree().shortcut_action

    real = r"C:\Users\Ada\PanelCAD"
    side = r"D:\side\PanelCAD"
    desktop = r"C:\Users\Ada\Desktop"
    profile = r"C:\Users\Ada"
    assert (
        shortcut_action(
            existing_workdir=real,
            install_root=side,
            desktop=desktop,
            user_profile=r"D:\side",
            shortcut_exists=True,
        )
        == "leave"
    )
    assert (
        shortcut_action(
            existing_workdir=real.replace("\\", "/"),
            install_root=real,
            desktop=desktop,
            user_profile=profile,
            shortcut_exists=True,
        )
        == "update"
    )
    assert (
        shortcut_action(
            existing_workdir="",
            install_root=real,
            desktop=desktop,
            user_profile=profile,
            shortcut_exists=False,
        )
        == "create"
    )
    assert (
        shortcut_action(
            existing_workdir="",
            install_root=side,
            desktop=desktop,
            user_profile=r"D:\side",
            shortcut_exists=False,
        )
        == "leave"
    )
    assert (
        shortcut_action(
            existing_workdir="",
            install_root=real,
            desktop=desktop,
            user_profile=profile,
            shortcut_exists=True,
        )
        == "leave"
    )


def test_shortcut_args_for_missing_empty_same_and_other(capsys: pytest.CaptureFixture[str]) -> None:
    """Missing creates. Empty or another folder leaves. The same folder updates.

    The argument list never has a bare ``--existing``. PowerShell would drop
    the empty value and argparse would exit 2 after the copy.
    """

    module = _load_install_tree()
    real = r"C:\Users\Ada\PanelCAD"
    side = r"D:\side\PanelCAD"
    desktop = r"C:\Users\Ada\Desktop"
    profile = r"C:\Users\Ada"
    cases = (
        ("", False, real, profile, "create"),
        ("", True, real, profile, "leave"),
        (real, True, real, profile, "update"),
        (real, True, side, r"D:\side", "leave"),
    )
    for existing, exists, install, user, expect in cases:
        args = module.shortcut_cli_args(
            existing_workdir=existing,
            install_root=install,
            desktop=desktop,
            user_profile=user,
            shortcut_exists=exists,
        )
        native = _powershell_drops_empty(args)
        if "--existing" in native:
            index = native.index("--existing")
            assert index + 1 < len(native)
            assert native[index + 1] and not native[index + 1].startswith("-")
        else:
            assert existing == ""
        assert "" not in native
        code = module.main(native)
        assert code == 0
        assert capsys.readouterr().out.strip() == expect
    bare = module.main(
        [
            "--shortcut-action",
            "--existing",
            "--install-root",
            real,
            "--desktop",
            desktop,
            "--user-profile",
            profile,
            "--shortcut-exists",
        ]
    )
    assert bare == 0
    assert capsys.readouterr().out.strip() == "leave"


def test_handoff_log_keeps_the_working_folder_line(tmp_path: Path) -> None:
    from panelcad.update.handoff import attach_handoff_log

    log = tmp_path / "logs" / "update.log"
    log.parent.mkdir()
    log.write_bytes(b"")
    inherited = open(log, "r+b")
    inherited.seek(0)
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    try:
        os.dup2(inherited.fileno(), 1)
        attach_handoff_log(log, r"working folder C:\Users\Ada\.panelcad")
        os.write(1, b"pip install ok\n")
        sys.stdout.flush()
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        inherited.close()
    text = log.read_text(encoding="utf-8")
    assert "working folder" in text
    assert "pip install ok" in text
    assert text.index("working folder") < text.index("pip install ok")


def test_update_actions_log_paths(tmp_path: Path) -> None:
    from panelcad.diag.log import configure_logging, read_log_text
    from panelcad.update.settings import write_rollback

    configure_logging()
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    feed = _write_feed(tmp_path, archive)
    check_for_update(feed=str(feed), current="0.4.0", record=False)
    install_update(confirm=True, feed=str(feed), dry_run=True, current="0.4.0")
    config = Path(os.environ["PANELCAD_CONFIG_DIR"])
    backup = config / "backups" / "PanelCAD-0.4.0"
    backup.mkdir(parents=True)
    install = tmp_path / "PanelCAD"
    install.mkdir()
    write_rollback(
        previous_version="0.4.0",
        backup=str(backup),
        install_root=str(install),
        directory=config,
    )
    rollback_update(confirm=True, dry_run=True, config_dir=config)
    text = read_log_text()
    assert "update_check" in text
    assert "panelcad-feed.json" in text
    assert "update_install" in text
    assert "update_rollback" in text
    assert "update_backup" in text
    assert "name: Line 1" not in text


def test_save_as_starts_in_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow, save_as_start

    home = Path.home()
    assert save_as_start(None).replace("\\", "/").endswith("Documents/PanelCAD")
    program = home / "PanelCAD" / "job.pcad.yaml"
    assert save_as_start(program).replace("\\", "/").endswith("Documents/PanelCAD/job.pcad.yaml")
    _app()
    window = MainWindow(Session.create("Save As"))
    captured: dict[str, str] = {}

    def fake_save(_parent, _title, start, _filt):
        captured["start"] = start
        return "", ""

    monkeypatch.setattr(
        "panelcad.gui.main_window.QFileDialog.getSaveFileName",
        fake_save,
    )
    assert window.save_project_as() is False
    assert captured["start"].replace("\\", "/").endswith("Documents/PanelCAD")
    window.close()


def test_unsaved_changes_cancel_blocks_install_and_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from PySide6.QtCore import QTimer

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    _app()
    window = MainWindow(Session.create("Dirty"))
    window.session.project.name = "Changed"
    assert window._project_dirty()
    labels: list[str] = []

    def click_cancel() -> None:
        boxes = [
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
        ]
        assert boxes
        box = boxes[-1]
        labels.extend(button.text().replace("&", "") for button in box.buttons())
        for button in box.buttons():
            if button.text().replace("&", "") == "Cancel":
                button.click()
                return

    QTimer.singleShot(0, click_cancel)
    assert window._ask_save_changes() == "cancel"
    assert "Save" in labels
    assert "Don't save" in labels
    assert "Cancel" in labels

    started: list[object] = []
    monkeypatch.setattr(
        "panelcad.update.service.install_update",
        lambda **kwargs: started.append(kwargs),
    )
    monkeypatch.setattr(
        "panelcad.update.service.rollback_update",
        lambda **kwargs: started.append(kwargs),
    )
    monkeypatch.setattr(window, "_confirm_unsaved", lambda: False)

    class StubRollback:
        def __init__(self, *_args, **_kwargs) -> None:
            self.confirmed = True

        def exec(self) -> QDialog.DialogCode:
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr("panelcad.gui.update_dialog.RollbackDialog", StubRollback)
    write_rollback(
        previous_version="0.4.0",
        backup=str(tmp_path / "backup"),
        install_root=str(tmp_path / "PanelCAD"),
        directory=Path(os.environ["PANELCAD_CONFIG_DIR"]),
    )
    assert window.install_update_now({"version": "0.9.0", "sha256": "ab" * 32}) is False
    assert window.roll_back_update() is False
    assert started == []
    window.close()


def test_gui_inbox_waits_for_the_update_dialog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow

    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    _write_feed(tmp_path, archive)
    app = _app()
    assert app is not None
    window = MainWindow(Session.create("Inbox"))
    seen: dict[str, object] = {}

    class Later:
        def __init__(self, report: dict, parent=None) -> None:
            seen["report"] = report
            self.choice = "later"

        def exec(self) -> int:
            return 0

    monkeypatch.setattr("panelcad.gui.update_dialog.UpdateAvailableDialog", Later)
    refused = window._inbox_handle(
        {"id": "in1", "op": "update_install", "confirm": True, "feed": str(tmp_path)}
    )
    assert refused["ok"] is False
    assert "operator" in refused["error"]
    assert seen["report"]["version"] == "0.9.0"

    class Install(Later):
        def __init__(self, report: dict, parent=None) -> None:
            super().__init__(report, parent)
            self.choice = "install"

    monkeypatch.setattr("panelcad.gui.update_dialog.UpdateAvailableDialog", Install)
    clicked: list[dict] = []

    def fake_install(self, report=None) -> bool:
        clicked.append(report or {})
        return True

    monkeypatch.setattr(MainWindow, "install_update_now", fake_install)
    accepted = window._inbox_handle(
        {"id": "in2", "op": "update_install", "confirm": True, "feed": str(tmp_path)}
    )
    assert accepted["ok"] is True
    assert clicked and clicked[0]["sha256"] == _sha(archive)
    window.close()


def _drive_update_dialogs(action, *, cancel_save: bool):
    """Click through rollback, and Cancel any unsaved-changes box, while ``action`` runs."""

    from PySide6.QtCore import QTimer

    seen: list[tuple[str, ...]] = []

    def tick() -> None:
        app = QApplication.instance()
        if app is None:
            return
        for widget in app.topLevelWidgets():
            if isinstance(widget, QMessageBox) and widget.isVisible():
                texts = tuple(button.text().replace("&", "") for button in widget.buttons())
                if texts and texts not in seen:
                    seen.append(texts)
                for button in widget.buttons():
                    if button.text().replace("&", "") == "Cancel":
                        button.click()
                        break
            if widget.__class__.__name__ == "RollbackDialog" and widget.isVisible():
                go = widget.findChild(QPushButton, "updateRollback")
                if go is not None:
                    go.click()
            if widget.__class__.__name__ == "UpdateAvailableDialog" and widget.isVisible():
                later = widget.findChild(QPushButton, "updateLater")
                if later is not None:
                    later.click()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(15)
    try:
        result = action()
    finally:
        timer.stop()
    return result, seen


def test_check_for_updates_returns_after_closed_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Check for updates returns after other main windows have been built and closed.

    The progress dialog accepts itself from a timer inside exec(). That used to
    let the dock-tab filter walk the widget tree and abort the process.
    """

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.update.settings import UpdateSettings, save_settings

    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    feed = _write_feed(tmp_path, archive)
    save_settings(UpdateSettings(feed=str(feed), check_on_startup=False))
    app = _app()
    for index in range(3):
        closed = MainWindow(Session.create(f"Closed {index}"))
        closed.resize(1495, 860)
        closed.show()
        app.processEvents()
        closed.close()
        app.processEvents()

    job = tmp_path / "motor.pcad.yaml"
    Session.create("Motor job").save(job)
    window = MainWindow(Session.create("Untitled"))
    window.resize(1495, 860)
    window.show()
    app.processEvents()
    window.open_path(str(job))
    app.processEvents()
    window.session.project.name = "Edited"
    assert window._project_dirty() is True
    assert window.save_project() is True
    assert window._project_dirty() is False

    installs: list[object] = []

    def fake_install(**kwargs):
        installs.append(kwargs)
        return {"close_app": False}

    monkeypatch.setattr("panelcad.update.service.install_update", fake_install)
    offered: list[str] = []

    class Later:
        def __init__(self, report: dict, parent=None) -> None:
            offered.append(str(report.get("version") or ""))
            self.choice = "later"

        def exec(self) -> int:
            return 0

    monkeypatch.setattr("panelcad.gui.update_dialog.UpdateAvailableDialog", Later)
    window.check_for_updates()
    app.processEvents()
    assert offered == ["0.9.0"]
    assert installs == []
    assert window.isVisible()
    assert QApplication.instance() is app
    window.close()
    app.processEvents()


def test_update_unsaved_prompt_only_when_the_drawing_is_dirty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clean drawing installs without asking. Cancel on a dirty drawing does not."""

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.update.settings import UpdateSettings, save_settings

    calls: list[str] = []

    def fake_install(**_kwargs):
        calls.append("install")
        return {"close_app": False}

    def fake_rollback(**_kwargs):
        calls.append("rollback")
        return {"close_app": False}

    monkeypatch.setattr("panelcad.update.service.install_update", fake_install)
    monkeypatch.setattr("panelcad.update.service.rollback_update", fake_rollback)
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    feed = _write_feed(tmp_path, archive)
    save_settings(UpdateSettings(feed=str(feed), check_on_startup=False))
    write_rollback(
        previous_version="0.4.0",
        backup=str(tmp_path / "backup"),
        install_root=str(tmp_path / "PanelCAD"),
        directory=Path(os.environ["PANELCAD_CONFIG_DIR"]),
    )
    app = _app()
    window = MainWindow(Session.create("Clean"))
    window.resize(1200, 800)
    window.show()
    app.processEvents()
    assert window._project_dirty() is False
    report = {"version": "0.9.0", "sha256": "ab" * 32}
    installed, clean_boxes = _drive_update_dialogs(
        lambda: window.install_update_now(report),
        cancel_save=False,
    )
    rolled, rollback_boxes = _drive_update_dialogs(
        window.roll_back_update,
        cancel_save=False,
    )
    assert installed is True
    assert rolled is True
    assert calls == ["install", "rollback"]
    assert clean_boxes == []
    assert rollback_boxes == []

    window.session.project.name = "Dirty"
    assert window._project_dirty() is True
    before = list(calls)
    blocked, dirty_boxes = _drive_update_dialogs(
        lambda: window.install_update_now(report),
        cancel_save=True,
    )
    blocked_rollback, rollback_dirty = _drive_update_dialogs(
        window.roll_back_update,
        cancel_save=True,
    )
    assert blocked is False
    assert blocked_rollback is False
    assert calls == before
    wanted = {"Save", "Don't save", "Cancel"}
    assert any(set(box) == wanted for box in dirty_boxes)
    assert any(set(box) == wanted for box in rollback_dirty)
    window.close()
    app.processEvents()


def test_startup_update_offer_follows_the_setting(tmp_path: Path) -> None:
    """Startup shows the offer when the check is on, and stays quiet when it is off."""

    from PySide6.QtCore import QEventLoop, QTimer

    from panelcad.api.session import Session
    from panelcad.gui.main_window import MainWindow
    from panelcad.update.settings import UpdateSettings, save_settings

    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    feed = _write_feed(tmp_path, archive)
    save_settings(UpdateSettings(feed=str(feed), check_on_startup=True))
    app = _app()
    seen: list[str] = []

    def dismiss() -> None:
        for widget in app.topLevelWidgets():
            if not widget.isVisible():
                continue
            if widget.__class__.__name__ != "UpdateAvailableDialog":
                continue
            for label in widget.findChildren(QLabel):
                text = label.text()
                if "is available" in text and text not in seen:
                    seen.append(text)
            later = widget.findChild(QPushButton, "updateLater")
            if later is not None:
                later.click()

    def pump(milliseconds: int) -> None:
        timer = QTimer()
        timer.timeout.connect(dismiss)
        timer.start(20)
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()
        timer.stop()
        app.processEvents()

    window = MainWindow(Session.create("Startup"))
    window.resize(1200, 800)
    window.show()
    assert window.isVisible()
    pump(1600)
    assert window.isVisible()
    assert "PanelCAD 0.9.0 is available" in seen
    window.close()
    app.processEvents()

    save_settings(UpdateSettings(feed=str(feed), check_on_startup=False))
    seen.clear()
    quiet = MainWindow(Session.create("Quiet"))
    quiet.resize(1200, 800)
    quiet.show()
    pump(900)
    assert seen == []
    quiet.close()
    app.processEvents()


def test_prune_keeps_newest_backups(tmp_path: Path) -> None:
    from panelcad.update.settings import UpdateSettings, prune_backups, save_settings

    config = Path(os.environ["PANELCAD_CONFIG_DIR"])
    save_settings(UpdateSettings(keep_backups=2), config)
    root = config / "backups"
    root.mkdir()
    for index, name in enumerate(("old", "mid", "new")):
        folder = root / name
        folder.mkdir()
        os.utime(folder, (index + 10, index + 10))
    removed = prune_backups(config)
    assert Path(removed[0]).name == "old"
    assert sorted(path.name for path in root.iterdir()) == ["mid", "new"]


def test_drive_confirm_form_points_at_usercontent() -> None:
    import http.cookiejar

    from panelcad.update.feed import _confirm_url

    page = """<!DOCTYPE html><html><body>
    <form id="download-form" action="https://drive.usercontent.google.com/download" method="get">
    <input type="hidden" name="id" value="FILEID">
    <input type="hidden" name="export" value="download">
    <input type="hidden" name="confirm" value="t">
    <input type="hidden" name="uuid" value="uuid-1">
    </form>
    <a href="/uc?export=download&amp;confirm=AbCd&amp;id=FILEID">Download anyway</a>
    </body></html>"""
    found = _confirm_url(
        page,
        "https://drive.google.com/uc?export=download&id=FILEID",
        http.cookiejar.CookieJar(),
    )
    assert found is not None
    assert found.startswith("https://drive.usercontent.google.com/download?")
    assert "confirm=t" in found
    assert "id=FILEID" in found
    assert "export=download" in found
    assert "uuid=uuid-1" in found


def test_google_drive_uc_url_is_the_file_itself() -> None:
    url = "https://drive.google.com/uc?export=download&id=file-id"
    assert resolve_feed_location(url) == url
    usercontent = (
        "https://drive.usercontent.google.com/download?id=file-id&export=download&confirm=t"
    )
    assert resolve_feed_location(usercontent) == usercontent
    local = "http://127.0.0.1:9/uc?export=download&id=file-id"
    assert resolve_feed_location(local) == local
    assert (
        resolve_feed_location("https://example.invalid/releases")
        == "https://example.invalid/releases/panelcad-feed.json"
    )
    assert resolve_download(url, url) == url


def test_builtin_feed_order_is_env_then_saved_then_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from panelcad.update.settings import effective_feed

    def _tree(version: str) -> Path:
        folder = tmp_path / version
        folder.mkdir()
        archive = folder / f"PanelCAD-{version}-setup.zip"
        _setup_zip(archive, version)
        _write_feed(folder, archive, version=version)
        return folder

    builtin_dir = _tree("0.9.0")
    saved_dir = _tree("0.9.1")
    env_dir = _tree("0.9.2")
    monkeypatch.setattr(
        "panelcad.update.builtin.default_feed_url", lambda: str(builtin_dir)
    )
    assert effective_feed(UpdateSettings()) == str(builtin_dir)
    report = check_for_update(current="0.4.0", record=False)
    assert report["enabled"] is True
    assert report["version"] == "0.9.0"
    assert report["feed"] == str(builtin_dir)

    save_settings(UpdateSettings(feed=str(saved_dir)))
    assert check_for_update(current="0.4.0", record=False)["version"] == "0.9.1"

    monkeypatch.setenv("PANELCAD_UPDATE_FEED", str(env_dir))
    assert check_for_update(current="0.4.0", record=False)["version"] == "0.9.2"
    explicit = check_for_update(feed=str(builtin_dir), current="0.4.0", record=False)
    assert explicit["version"] == "0.9.0"


def test_default_feed_url_reads_https_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from panelcad.update.builtin import default_feed_url

    path = tmp_path / "_release_feed.txt"
    monkeypatch.setattr("panelcad.update.builtin.feed_path", lambda: path)
    assert default_feed_url() == ""
    path.write_text("http://127.0.0.1/panelcad-feed.json\n", encoding="utf-8")
    assert default_feed_url() == ""
    path.write_text("https://user:secret@updates.example/feed.json\n", encoding="utf-8")
    assert default_feed_url() == ""
    path.write_text("https://updates.example/panelcad-feed.json\n", encoding="utf-8")
    assert default_feed_url() == "https://updates.example/panelcad-feed.json"
    assert UpdateSettings().to_dict()["feed"] == ""
    from panelcad.update.settings import effective_feed

    assert effective_feed(UpdateSettings()) == "https://updates.example/panelcad-feed.json"


def test_settings_dialog_names_the_builtin_feed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from panelcad.gui.update_dialog import UpdateSettingsDialog

    path = tmp_path / "_release_feed.txt"
    path.write_text("https://updates.example/panelcad-feed.json\n", encoding="utf-8")
    monkeypatch.setattr("panelcad.update.builtin.feed_path", lambda: path)
    app = _app()
    dialog = UpdateSettingsDialog(UpdateSettings())
    dialog.show()
    app.processEvents()
    label = dialog.findChild(QLabel, "builtinUpdateFeed")
    assert label is not None and label.isVisible()
    assert label.text() == "Using the built-in PanelCAD update feed"
    blob = " ".join(item.text() for item in dialog.findChildren(QLabel))
    assert "updates.example" not in blob
    assert dialog.feed.text() == ""
    dialog.close()

    filled = UpdateSettingsDialog(UpdateSettings(feed=r"G:\My Drive\PanelCAD"))
    filled.show()
    app.processEvents()
    again = filled.findChild(QLabel, "builtinUpdateFeed")
    assert again is not None and not again.isVisible()
    filled.close()


@contextmanager
def _drive_server(holder: dict[str, bytes]):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, body: bytes, content_type: str, *, cookie: str | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            if cookie:
                self.send_header("Set-Cookie", f"{cookie}; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def _form(self, file_id: str) -> None:
            html = (
                "<!DOCTYPE html><html><body>"
                '<form id="download-form" action="/uc" method="get">'
                f'<input type="hidden" name="id" value="{file_id}">'
                '<input type="hidden" name="export" value="download">'
                '<input type="hidden" name="confirm" value="t">'
                '<input type="hidden" name="uuid" value="uuid-test">'
                "</form></body></html>"
            )
            self._send(html.encode(), "text/html; charset=utf-8")

        def do_GET(self) -> None:
            import urllib.parse

            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            file_id = (query.get("id") or [""])[0]
            confirm = (query.get("confirm") or [""])[0]
            via = (query.get("via") or [""])[0]
            holder["hits"] = holder.get("hits", 0) + 1
            if parsed.path == "/uc" and file_id == "html":
                page = (
                    b"<!DOCTYPE html><html><head><title>Google Drive</title></head>"
                    b"<body>interstitial</body></html>"
                )
                self._send(page, "text/html; charset=utf-8")
                return
            if parsed.path == "/uc" and via == "redirect" and not confirm:
                self._redirect(f"/landing?id={file_id}")
                return
            if parsed.path == "/uc" and via == "file" and not confirm:
                self._redirect(f"/file/{file_id}")
                return
            if parsed.path == "/scan":
                host = self.headers.get("Host", "127.0.0.1")
                html = (
                    "<!DOCTYPE html><html><head><title>Google Drive - Virus scan warning</title></head><body>"
                    f'<form id="download-form" action="http://{host}/uc" method="get">'
                    f'<input type="hidden" name="id" value="{file_id}">'
                    '<input type="hidden" name="export" value="download">'
                    '<input type="hidden" value="t" name="confirm">'
                    '<input type="hidden" name="uuid" value="uuid-test">'
                    "</form></body></html>"
                )
                self._send(html.encode(), "text/html; charset=utf-8")
                return
            if parsed.path == "/warn":
                page = b"<!DOCTYPE html><html><body>virus scan warning</body></html>"
                self._send(
                    page,
                    "text/html; charset=utf-8",
                    cookie=f"download_warning_{file_id}=cookie-token",
                )
                return
            if parsed.path.startswith("/file/"):
                file_id = parsed.path.rsplit("/", 1)[-1]
                confirm = "t"
            if confirm in {"t", "cookie-token"} and file_id in {"feed", "zip"}:
                body = holder[file_id]
                kind = "application/json" if file_id == "feed" else "application/zip"
                self._send(body, kind)
                return
            if parsed.path in {"/uc", "/landing"}:
                self._form(file_id)
                return
            self.send_error(404)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_drive_confirm_redirect_and_html_refusal(tmp_path: Path) -> None:
    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    holder: dict[str, bytes] = {"zip": archive.read_bytes(), "hits": 0}
    with _drive_server(holder) as base:
        download = f"{base}/uc?export=download&id=zip"
        manifest = _manifest(archive, download=download)
        holder["feed"] = json.dumps(manifest).encode()
        feed_url = f"{base}/uc?export=download&id=feed"
        before = holder["hits"]
        with pytest.raises(UpdateError, match="confirm=True"):
            install_update(confirm=False, feed=feed_url, current="0.4.0", dry_run=True)
        assert holder["hits"] == before

        report = check_for_update(feed=feed_url, current="0.4.0", record=False)
        assert report["version"] == "0.9.0"
        assert report["download_resolved"] == download
        assert report["sha256"] == _sha(archive)
        dry = install_update(confirm=True, feed=feed_url, current="0.4.0", dry_run=True)
        assert dry["verified"] is True
        assert dry["dry_run"] is True
        assert dry["sha256"] == _sha(archive)
        assert not Path(dry["archive"]).exists()

        redirected = check_for_update(
            feed=f"{base}/uc?export=download&id=feed&via=redirect",
            current="0.4.0",
            record=False,
        )
        assert redirected["version"] == "0.9.0"
        straight = check_for_update(
            feed=f"{base}/uc?export=download&id=feed&via=file",
            current="0.4.0",
            record=False,
        )
        assert straight["version"] == "0.9.0"
        cookie = check_for_update(
            feed=f"{base}/warn?export=download&id=feed",
            current="0.4.0",
            record=False,
        )
        assert cookie["version"] == "0.9.0"
        absolute = check_for_update(
            feed=f"{base}/scan?export=download&id=feed",
            current="0.4.0",
            record=False,
        )
        assert absolute["version"] == "0.9.0"

        with pytest.raises(UpdateError, match="HTML") as html_feed:
            check_for_update(
                feed=f"{base}/uc?export=download&id=html",
                current="0.4.0",
                record=False,
            )
        assert "sha256" not in str(html_feed.value).lower()
        assert "not kept" in str(html_feed.value)

        local = tmp_path / "panelcad-feed.json"
        bad = _manifest(archive, download=f"{base}/uc?export=download&id=html")
        local.write_text(json.dumps(bad) + "\n", encoding="utf-8")
        with pytest.raises(UpdateError, match="HTML") as html_zip:
            install_update(confirm=True, feed=str(local), current="0.4.0", dry_run=True)
        assert "sha256" not in str(html_zip.value).lower()
        assert "not kept" in str(html_zip.value)

        holder["feed"] = json.dumps(_manifest(archive, download=download, sha256="ab" * 32)).encode()
        with pytest.raises(UpdateError, match="sha256 mismatch"):
            install_update(confirm=True, feed=feed_url, current="0.4.0", dry_run=True)


def test_publish_feed_download_url(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.update.feed import require_remote_download_url

    archive = tmp_path / "PanelCAD-0.9.0-setup.zip"
    _setup_zip(archive)
    url = "https://drive.google.com/uc?export=download&id=testfile"
    path = publish_feed(
        zip_path=archive,
        feed=str(tmp_path / "synced"),
        notes="Drive copy.",
        release_date="2026-10-08",
        download=url,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["download"] == url
    assert data["sha256"] == _sha(archive)
    assert data["size"] == archive.stat().st_size
    assert require_remote_download_url(url) == url
    with pytest.raises(UpdateError, match="https"):
        require_remote_download_url("http://files.example/setup.zip?token=SUPERSECRETTOKEN")

    runner = CliRunner()
    published = runner.invoke(
        app,
        [
            "release",
            "publish-feed",
            "--zip",
            str(archive),
            "--feed",
            str(tmp_path / "cli-feed"),
            "--download-url",
            url,
            "--notes",
            "CLI drive",
            "--date",
            "2026-10-08",
        ],
    )
    assert published.exit_code == 0, published.stdout + published.stderr
    written = json.loads((tmp_path / "cli-feed" / "panelcad-feed.json").read_text(encoding="utf-8"))
    assert written["download"] == url
    assert written["sha256"] == _sha(archive)

    refused = runner.invoke(
        app,
        [
            "release",
            "publish-feed",
            "--zip",
            str(archive),
            "--feed",
            str(tmp_path / "bad"),
            "--download-url",
            "http://files.example/setup.zip?token=SUPERSECRETTOKEN",
        ],
    )
    assert refused.exit_code == 1
    assert "https" in refused.stderr
    assert "SUPERSECRETTOKEN" not in refused.stderr + refused.stdout
    assert not (tmp_path / "bad").exists()

    clash = runner.invoke(
        app,
        [
            "release",
            "publish-feed",
            "--zip",
            str(archive),
            "--feed",
            str(tmp_path / "clash"),
            "--download",
            "local.zip",
            "--download-url",
            url,
        ],
    )
    assert clash.exit_code == 1
    assert "disagree" in clash.stderr


def test_release_update_feed_is_embedded_only_from_the_env(tmp_path: Path, monkeypatch, capsys) -> None:
    import importlib.util

    path = ROOT / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("panelcad_build_release_feed", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    monkeypatch.delenv("PANELCAD_RELEASE_UPDATE_FEED", raising=False)
    monkeypatch.delenv("PANELCAD_RELEASE_BUG_WEBHOOK", raising=False)
    plain, _checksum = module.build_release(ROOT, tmp_path / "plain")
    warning = capsys.readouterr().err
    assert "PANELCAD_RELEASE_UPDATE_FEED" in warning
    with zipfile.ZipFile(plain) as zf:
        assert not any(name.endswith("_release_feed.txt") for name in zf.namelist())

    leftover = ROOT / "src" / "panelcad" / "update" / "_release_feed.txt"
    leftover.write_text("https://updates.example/leftover.json\n", encoding="utf-8")
    try:
        skipped, _ = module.build_release(ROOT, tmp_path / "skip")
        capsys.readouterr()
        with zipfile.ZipFile(skipped) as zf:
            assert not any(name.endswith("_release_feed.txt") for name in zf.namelist())
    finally:
        leftover.unlink(missing_ok=True)
    assert not leftover.exists()

    url = "https://drive.google.com/uc?export=download&id=testfile"
    monkeypatch.setenv("PANELCAD_RELEASE_UPDATE_FEED", url)
    embedded, _ = module.build_release(ROOT, tmp_path / "with")
    capsys.readouterr()
    assert not leftover.exists()
    rel = f"PanelCAD-{__version__}/src/panelcad/update/_release_feed.txt"
    with zipfile.ZipFile(embedded) as zf:
        assert zf.read(rel).decode("utf-8").strip() == url

    monkeypatch.setenv(
        "PANELCAD_RELEASE_UPDATE_FEED",
        "http://updates.example/feed.json?token=SUPERSECRETTOKEN",
    )
    with pytest.raises(SystemExit) as refused:
        module.build_release(ROOT, tmp_path / "http")
    assert "SUPERSECRETTOKEN" not in str(refused.value)
    assert "not an https URL" in str(refused.value)

    monkeypatch.setenv("PANELCAD_RELEASE_UPDATE_FEED", r"G:\My Drive\secret-feed.json")
    with pytest.raises(SystemExit) as drive_path:
        module.build_release(ROOT, tmp_path / "path")
    assert "secret-feed" not in str(drive_path.value)
    assert "not an https URL" in str(drive_path.value)


def test_public_github_default_is_merchantg_cmyk_panelcad(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.update.github_releases import PUBLIC_GITHUB_REPOSITORY, repository_configured
    from panelcad.update.settings import effective_feed, github_releases_active, startup_check_due

    assert PUBLIC_GITHUB_REPOSITORY == "merchantg-cmyk/panelcad"
    assert repository_configured() is True
    monkeypatch.setattr("panelcad.update.builtin.default_feed_url", lambda: "")
    monkeypatch.setattr("panelcad.update.github_releases.is_public_release", lambda: False)
    assert effective_feed(UpdateSettings()) == ""
    assert github_releases_active(UpdateSettings()) is False

    monkeypatch.setattr("panelcad.update.github_releases.is_public_release", lambda: True)
    assert effective_feed(UpdateSettings()) == "github-releases"
    assert github_releases_active(UpdateSettings()) is True
    assert startup_check_due(UpdateSettings()) is True

    monkeypatch.setenv("PANELCAD_GITHUB_REPOSITORY", "not a repo")
    assert repository_configured() is False
    assert startup_check_due(UpdateSettings()) is False
    report = check_for_update(current="0.5.10", record=False)
    assert report["enabled"] is False
    assert "owner/name" in report["reason"]
    monkeypatch.delenv("PANELCAD_GITHUB_REPOSITORY", raising=False)

    monkeypatch.setattr(
        "panelcad.update.builtin.default_feed_url",
        lambda: "https://updates.example/panelcad-feed.json",
    )
    assert effective_feed(UpdateSettings()) == "https://updates.example/panelcad-feed.json"
    assert github_releases_active(UpdateSettings()) is False


def test_public_build_dialog_offers_github_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.gui.update_dialog import UpdateSettingsDialog

    monkeypatch.setattr("panelcad.update.builtin.default_feed_url", lambda: "")
    monkeypatch.setattr("panelcad.update.github_releases.is_public_release", lambda: True)
    app = _app()
    dialog = UpdateSettingsDialog(UpdateSettings())
    dialog.show()
    app.processEvents()
    label = dialog.findChild(QLabel, "githubReleasesUpdate")
    assert label is not None and label.isVisible()
    assert label.text() == "Download update from the public GitHub Releases"
    named = dialog.findChild(QLabel, "githubReleasesPlaceholder")
    assert named is not None and named.isVisible()
    assert "merchantg-cmyk/panelcad" in named.text()
    shop = dialog.findChild(QLabel, "builtinUpdateFeed")
    assert shop is not None and not shop.isVisible()
    dialog.close()


def test_github_release_check_verifies_sha256_before_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from http.server import BaseHTTPRequestHandler

    from panelcad.update.github_releases import github_api_base

    version = "0.9.0"
    archive = tmp_path / f"PanelCAD-{version}-setup.zip"
    _setup_zip(archive, version)
    digest = _sha(archive)
    zip_bytes = archive.read_bytes()
    sha_name = archive.name + ".sha256"
    sha_bytes = f"{digest}  {archive.name}\n".encode()
    bad_sha = f"{'ab' * 32}  {archive.name}\n".encode()
    holder = {"sha": sha_bytes}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            host = self.headers.get("Host", "127.0.0.1")
            if self.path.startswith("/repos/") and self.path.endswith("/releases/latest"):
                body = json.dumps(
                    {
                        "tag_name": f"v{version}",
                        "body": "Public release notes.",
                        "published_at": "2026-10-20T12:00:00Z",
                        "assets": [
                            {
                                "name": archive.name,
                                "browser_download_url": f"http://{host}/{archive.name}",
                                "size": len(zip_bytes),
                            },
                            {
                                "name": sha_name,
                                "browser_download_url": f"http://{host}/{sha_name}",
                                "size": len(holder["sha"]),
                            },
                        ],
                    }
                ).encode()
                kind = "application/json"
            elif self.path == "/" + archive.name:
                body, kind = zip_bytes, "application/zip"
            elif self.path == "/" + sha_name:
                body, kind = holder["sha"], "text/plain"
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        monkeypatch.setenv("PANELCAD_GITHUB_REPOSITORY", "example-org/panelcad")
        monkeypatch.setattr(
            "panelcad.update.github_releases.github_api_base",
            lambda: f"http://127.0.0.1:{port}",
        )
        monkeypatch.setattr("panelcad.update.github_releases.is_public_release", lambda: True)
        monkeypatch.setattr("panelcad.update.builtin.default_feed_url", lambda: "")
        assert github_api_base() == "https://api.github.com"
        report = check_for_update(current="0.5.10", record=False, config_dir=tmp_path / "cfg")
        assert report["enabled"] is True
        assert report["update_available"] is True
        assert report["version"] == version
        assert report["sha256"] == digest
        assert report["feed"] == "https://github.com/example-org/panelcad/releases/latest"
        assert report["release_date"] == "2026-10-20"
        assert "Public release notes." in report["notes"]
        installed = install_update(
            confirm=True,
            dry_run=True,
            current="0.5.10",
            config_dir=tmp_path / "cfg",
        )
        assert installed["verified"] is True
        assert installed["sha256"] == digest

        holder["sha"] = bad_sha
        with pytest.raises(UpdateError) as mismatch:
            install_update(
                confirm=True,
                dry_run=True,
                current="0.5.10",
                config_dir=tmp_path / "cfg2",
            )
        assert "sha256 mismatch" in str(mismatch.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
