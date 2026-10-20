"""Logs, crash records, bug-report zips, and the Slack reporters. Temp dirs only."""

from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, QtMsgType
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from panelcad.api.session import Session
from panelcad.cli.agent import REPORT_BUG_SCHEMA, Agent
from panelcad.cli.main import app
from panelcad.diag.crash import (
    UncleanExit,
    abandon_session,
    acknowledge_unclean,
    autosave_now,
    begin_session,
    crash_dialog_parent,
    current_lock_path,
    dialogs_shown,
    heartbeat_now,
    install_hooks,
    mark_clean_exit,
    plain_when,
    process_start_token,
    prune_recovery_files,
    record_exception,
    recovery_session,
    set_recovery_session,
)
from panelcad.diag.log import (
    LOG_BACKUP_COUNT,
    MAX_LOG_BYTES,
    configure_logging,
    log_file_path,
)
from panelcad.diag.paths import (
    app_data_dir,
    crashes_dir,
    fault_log_path,
    logs_dir,
    recovery_dir,
    sessions_dir,
    settings_path,
)
from panelcad.diag.redact import redact_text
from panelcad.diag.reporters import SendResult, register_reporter, set_slack_api_base
from panelcad.diag.settings import BugSettings
from panelcad.gui.bug_report_dialog import BugReportDialog, CrashDialog, RecoveryDialog, SettingsDialog
from panelcad.gui.main_window import MainWindow
from panelcad.version import __version__


class _Server(ThreadingHTTPServer):
    def __init__(self, address, handler) -> None:
        super().__init__(address, handler)
        self.hits: list[dict] = []
        self.fail = False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0]
        server: _Server = self.server  # type: ignore[assignment]
        server.hits.append(
            {
                "path": path,
                "headers": {key: value for key, value in self.headers.items()},
                "body": body,
            }
        )
        if server.fail:
            raw = json.dumps({"ok": False, "error": "invalid_auth"}).encode("utf-8")
            self._send(200 if path.startswith("/api/") else 500, raw)
            return
        if getattr(server, "fail_uploads", False) and (path.startswith("/api/") or path == "/upload"):
            raw = json.dumps({"ok": False, "error": "missing_scope"}).encode("utf-8")
            self._send(200 if path.startswith("/api/") else 500, raw)
            return
        if path == "/api/files.getUploadURLExternal":
            port = server.server_address[1]
            server.upload_count = getattr(server, "upload_count", 0) + 1
            file_id = "FTEST123" if server.upload_count == 1 else f"FTEST{server.upload_count}"
            payload = {
                "ok": True,
                "upload_url": f"http://127.0.0.1:{port}/upload",
                "file_id": file_id,
            }
            self._send(200, json.dumps(payload).encode("utf-8"))
            return
        if path == "/upload":
            if getattr(server, "fail_zip_put", False) and getattr(server, "upload_count", 0) >= 2:
                self._send(500, b"zip-put-failed")
                return
            self._send(200, b"ok")
            return
        if path == "/api/files.completeUploadExternal":
            incoming = json.loads(body.decode("utf-8"))
            files = []
            for item in incoming.get("files") or []:
                if isinstance(item, dict):
                    files.append(
                        {
                            "id": item.get("id"),
                            "title": item.get("title"),
                            "shares": {"public": {"C0TESTBUGS01": [{"ts": "1700000000.000100"}]}},
                        }
                    )
            self._send(200, json.dumps({"ok": True, "files": files}).encode("utf-8"))
            return
        if path == "/api/chat.postMessage":
            self._send(
                200,
                json.dumps({"ok": True, "channel": "C0TESTBUGS01", "ts": "1700000000.000200"}).encode(
                    "utf-8"
                ),
            )
            return
        if path.startswith("/services/"):
            self._send(200, b"ok")
            return
        self._send(404, b"missing")

    def _send(self, status: int, raw: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def http_server():
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_app_data_dirs(monkeypatch, tmp_path):
    monkeypatch.delenv("PANELCAD_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Ada\AppData\Local")
    assert app_data_dir() == Path(r"C:\Users\Ada\AppData\Local") / "PanelCAD"
    assert logs_dir() == app_data_dir() / "logs"

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/ada"))
    assert app_data_dir() == Path("/Users/ada/Library/Application Support/PanelCAD")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert app_data_dir() == Path("/Users/ada/.local/share/PanelCAD")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert app_data_dir() == tmp_path / "xdg" / "PanelCAD"
    monkeypatch.setenv("PANELCAD_DATA_DIR", str(tmp_path / "override"))
    assert app_data_dir() == tmp_path / "override"
    assert logs_dir() == tmp_path / "override" / "logs"


def test_log_rotation_startup_and_home(tmp_path):
    assert MAX_LOG_BYTES == 1_000_000
    assert LOG_BACKUP_COUNT == 5
    configure_logging(max_bytes=500, backup_count=5)
    home = Path.home()
    token = "xoxb-FAKE-ROTATE-TOKEN-123456789"
    logger = logging.getLogger("panelcad")
    logger.info("open %s secret %s", home / "drawings" / "job.pcad.yaml", token)
    text = log_file_path().read_text(encoding="utf-8")
    assert f"version={__version__}" in text
    assert "pyside6=" in text
    assert "python=" in text
    assert "os=" in text
    assert "install=" in text
    assert token not in text
    assert str(home) not in text
    assert "~" in text
    for index in range(40):
        logger.info("pad %s %s", index, "x" * 80)
    names = [item.name for item in logs_dir().glob("panelcad.log*")]
    assert "panelcad.log" in names
    backups = [name for name in names if name != "panelcad.log"]
    assert backups
    assert len(backups) <= 5
    combined = "".join(item.read_text(encoding="utf-8") for item in logs_dir().glob("panelcad.log*"))
    assert token not in combined
    assert str(home) not in combined


def test_ai_api_key_setting_is_redacted(tmp_path, monkeypatch):
    from panelcad.ai.config import AiLookupConfig, save_ai_lookup

    key = "sk-panelcad-user-key"
    save_ai_lookup(AiLookupConfig(enabled=True, provider="openai", api_key=key, model="gpt-test"))
    stored = settings_path().read_text(encoding="utf-8")
    assert key in stored
    redacted = redact_text(f"Authorization Bearer {key}\n{stored}")
    assert key not in redacted
    assert "[redacted]" in redacted
    configure_logging()
    logging.getLogger("panelcad").info("datasheet lookup failed for %s", key)
    assert key not in log_file_path().read_text(encoding="utf-8")
    session = Session.create("Key job")
    result = session.report_bug(
        "Lookup failed",
        f"The provider answered with {key}",
        steps="Look up a datasheet",
        send=False,
    )
    blob = Path(result["path"]).read_bytes()
    assert key.encode() not in blob
    env_key = "sk-env-panelcad-key"
    monkeypatch.setenv("PANELCAD_AI_API_KEY", env_key)
    assert env_key not in redact_text(f"env key {env_key} and file key {key}")


def test_redact_home_and_secrets():
    windows = redact_text(r"saved C:\Users\Ada\drawings\job.pcad.yaml", home=Path(r"C:\Users\Ada"))
    assert windows == r"saved ~\drawings\job.pcad.yaml"
    posix = redact_text("/home/ada/drawings/job.pcad.yaml", home=Path("/home/ada"))
    assert posix == "~/drawings/job.pcad.yaml"
    folded = redact_text(r"c:\users\ada\file.txt", home=Path(r"C:\Users\Ada"))
    assert folded.startswith("~")
    token = "xoxb-FAKE-ABCDEFGHIJKLMNOP"
    assert token not in redact_text(f"token {token} leftover")
    hook = "https://hooks.slack.com/services/T00/B00/secret"
    assert "hooks.slack.com" not in redact_text(f"see {hook}")
    ai_key = "sk-live-panelcad-secret-key"
    xai_key = "xai-1234567890abcdef"
    assert ai_key not in redact_text(f"lookup {ai_key}")
    assert xai_key not in redact_text(f"key {xai_key}")
    assert "Bearer [redacted]" in redact_text("Authorization: Bearer supersecrettokenvalue")
    assert "api_key=[redacted]" in redact_text("note api_key=abcd1234wxyz leftover")


def test_session_io_logs_paths_not_bodies(tmp_path):
    configure_logging()
    session = Session.create("Logged job")
    session.set_title_block(company="UNIQUE_COMPANY_BODY_9f3a")
    dest = tmp_path / "job.pcad.yaml"
    session.save(dest)
    opened = Session.open(dest)
    bom = tmp_path / "bom.csv"
    opened.export_bom(bom)
    text = log_file_path().read_text(encoding="utf-8")
    assert "save " in text
    assert "open " in text
    assert "export_bom " in text
    assert "UNIQUE_COMPANY_BODY_9f3a" not in text
    assert str(Path.home()) not in text
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Agent log"})["ok"]
    text = log_file_path().read_text(encoding="utf-8")
    assert "agent op=create_project ok=true" in text


def test_zip_contents_and_redaction(tmp_path, monkeypatch):
    configure_logging()
    token = "xoxb-FAKE-ZIP-SECRET-1234567890"
    webhook = "https://hooks.slack.com/services/T11/B22/abcdef"
    monkeypatch.setenv("PANELCAD_SLACK_BOT_TOKEN", token)
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", webhook)
    logging.getLogger("panelcad").info("note %s %s %s", Path.home() / "secret.yaml", token, webhook)
    session = Session.create("Zip job")
    session.set_title_block(company=str(Path.home() / "Acme"))
    original = tmp_path / "job.pcad.yaml"
    session.save(original)
    before = original.read_bytes()
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    result = session.report_bug(
        "Coil vanished",
        f"What happened near {Path.home()} with {token}",
        steps="Place a coil",
        contact="George",
        include_project=True,
        include_screenshot=True,
        screenshot_png=png,
        send=False,
    )
    assert result["sent"] is False
    assert result["send"]["attempted"] is False
    zpath = Path(result["path"])
    assert zpath.is_file()
    assert original.read_bytes() == before
    blob = zpath.read_bytes()
    assert token.encode() not in blob
    assert webhook.encode() not in blob
    assert str(Path.home()).encode() not in blob
    with zipfile.ZipFile(zpath) as archive:
        assert set(archive.namelist()) >= {"report.json", "log.txt", "screenshot.png", "project.pcad.yaml"}
        raw_report = archive.read("report.json").decode("utf-8")
        report = json.loads(raw_report)
        assert token not in raw_report
        assert webhook not in raw_report
        assert report["title"] == "Coil vanished"
        assert report["contact"] == "George"
        assert report["steps"] == "Place a coil"
        assert report["version"] == __version__
        assert report["system"]["version"] == __version__
        assert "log.txt" in report["files"]
        log_text = archive.read("log.txt").decode("utf-8")
        assert token not in log_text
        assert "~" in log_text
        project = archive.read("project.pcad.yaml").decode("utf-8")
        assert "Zip job" in project
        assert str(Path.home()) not in project


def test_excepthook_writes_crash_and_recovery(tmp_path):
    configure_logging()
    session = Session.create("Keep")
    original = tmp_path / "job.pcad.yaml"
    session.save(original)
    before = original.read_bytes()
    session.project.name = "Unsaved crash"
    set_recovery_session(session)
    install_hooks(gui=False)
    try:
        raise RuntimeError("boom-hook")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    assert dialogs_shown() == 0
    assert original.read_bytes() == before
    crashes = list(crashes_dir().glob("*.json"))
    assert crashes
    body = crashes[-1].read_text(encoding="utf-8")
    assert "boom-hook" in body
    assert __version__ in body
    copies = list(recovery_dir().glob("*.pcad.yaml"))
    assert copies
    assert "Unsaved crash" in copies[-1].read_text(encoding="utf-8")
    assert original.read_bytes() == before


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_and_qt_hooks(tmp_path):
    del tmp_path
    configure_logging()
    install_hooks(gui=True)

    def boom() -> None:
        raise RuntimeError("thread-boom-marker")

    worker = threading.Thread(target=boom)
    worker.start()
    worker.join()
    assert any("thread-boom-marker" in item.read_text(encoding="utf-8") for item in crashes_dir().glob("*.json"))

    from panelcad.diag import crash as crashmod

    crashmod._qt_handler(QtMsgType.QtWarningMsg, None, "qt warn xoxb-FAKE-QTTOKEN123456789")
    log_text = log_file_path().read_text(encoding="utf-8")
    assert "qt warn" in log_text
    assert "xoxb-FAKE-QTTOKEN123456789" not in log_text
    warn_line = next(line for line in log_text.splitlines() if "qt warn" in line)
    assert " WARNING " in warn_line
    crashmod._qt_handler(QtMsgType.QtCriticalMsg, None, "qt critical sample")
    crit_line = next(
        line
        for line in log_file_path().read_text(encoding="utf-8").splitlines()
        if "qt error qt critical sample" in line
    )
    assert " ERROR " in crit_line
    assert any("qt critical sample" in item.read_text(encoding="utf-8") for item in crashes_dir().glob("*.json"))
    assert dialogs_shown() == 0


def _leave_dead_lock() -> Path:
    """Copy this process's lock to a dead pid and drop our own file."""

    path = current_lock_path()
    assert path is not None and path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = 2147483647
    data["started"] = "dead-start"
    dead = path.parent / "2147483647-dead-start.json"
    dead.write_text(json.dumps(data), encoding="utf-8")
    abandon_session()
    path.unlink()
    return dead


def test_unclean_exit_detection():
    configure_logging()
    assert begin_session(gui=True) is None
    dead = _leave_dead_lock()
    again = begin_session(gui=True)
    assert again is not None
    assert "session" in again.reasons
    assert dead.is_file()
    acknowledge_unclean(again)
    assert not dead.is_file()
    assert current_lock_path() is not None and current_lock_path().is_file()
    mark_clean_exit(force=True)
    fault_log_path().write_text("Fatal Python error: segfault test\n", encoding="utf-8")
    found = begin_session(gui=True)
    assert found is not None
    assert "faulthandler" in found.reasons
    assert found.crash_id
    assert "Fatal Python error" in Path(found.crash_path).read_text(encoding="utf-8")


def test_webhook_and_bot_against_local_server(http_server, monkeypatch):
    configure_logging()
    port = http_server.server_address[1]
    token = "xoxb-FAKE-BOT-SECRET-123456789"
    channel = "C0TESTBUGS01"
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", f"http://127.0.0.1:{port}/services/hook")
    session = Session.create("Send")
    traceback = "Traceback (most recent call last):\n  File \"job.py\", line 3\nRuntimeError: boom-line"
    sent = session.report_bug(
        "Webhook title",
        "Webhook description",
        send=True,
        traceback=traceback,
    )
    assert sent["sent"] is True
    hook = next(hit for hit in http_server.hits if hit["path"] == "/services/hook")
    posted = json.loads(hook["body"])
    assert posted["text"].splitlines()[0] == "*Webhook title*"
    assert f"version: {__version__}" in posted["text"]
    assert "os:" in posted["text"]
    assert "What happened\nWebhook description" in posted["text"]
    assert "Traceback (most recent call last)" in posted["text"]
    assert "where: job.py:3" in posted["text"]
    assert "exception: RuntimeError: boom-line" in posted["text"]
    assert "computer:" in posted["text"]
    assert token not in posted["text"]
    assert sent["send"]["message"].startswith("Sent")
    assert "Not sent" not in sent["send"]["message"]
    assert sent["path"] in sent["send"]["message"]
    assert "hooks.slack.com" not in sent["send"]["message"]
    assert posted["version"] == __version__
    assert posted["zip"] == Path(sent["path"]).name
    assert posted["git"]
    assert posted["machine"]
    side = json.loads(Path(str(sent["path"]) + ".status.json").read_text(encoding="utf-8"))
    assert side["status"] == "sent"
    assert side["reporter"] == "webhook"
    assert side["error"] == ""
    assert side["time"].endswith("Z")
    with zipfile.ZipFile(sent["path"]) as archive:
        saved = json.loads(archive.read("report.json"))
    assert saved["description"] == "Webhook description"
    assert saved["title"] == "Webhook title"

    http_server.hits.clear()
    monkeypatch.delenv("PANELCAD_BUG_WEBHOOK", raising=False)
    set_slack_api_base(f"http://127.0.0.1:{port}/api")
    bot = session.report_bug(
        "Bot title",
        "Bot description",
        send=True,
        traceback=traceback,
        settings=BugSettings(slack_bot_token=token, slack_channel=channel),
    )
    assert bot["sent"] is True, bot
    paths = [hit["path"] for hit in http_server.hits]
    assert paths == [
        "/api/files.getUploadURLExternal",
        "/upload",
        "/api/files.completeUploadExternal",
    ]
    assert not any("files.upload" in item for item in paths)
    opened = http_server.hits[0]
    assert opened["headers"]["Authorization"] == f"Bearer {token}"
    assert b"filename=" in opened["body"]
    assert b"length=" in opened["body"]
    uploaded = http_server.hits[1]["body"]
    zpath = Path(bot["path"])
    assert uploaded == zpath.read_bytes()
    finished = json.loads(http_server.hits[2]["body"])
    assert finished["channel_id"] == channel
    assert finished["files"][0]["id"] == "FTEST123"
    assert finished["initial_comment"].splitlines()[0] == "*Bot title*"
    assert "What happened\nBot description" in finished["initial_comment"]
    assert bot["send"]["message"].startswith("Sent")
    assert bot["path"] in bot["send"]["message"]
    bot_side = json.loads(Path(str(bot["path"]) + ".status.json").read_text(encoding="utf-8"))
    assert bot_side["status"] == "sent"
    assert bot_side["reporter"] == "slack-bot"
    assert token.encode() not in zpath.read_bytes()
    log_text = log_file_path().read_text(encoding="utf-8")
    assert token not in log_text


def test_report_text_and_zip_keep_context_and_drop_secrets(http_server, monkeypatch):
    """Shop-style webhook post: full text, redacted secrets, no bot required."""

    configure_logging()
    port = http_server.server_address[1]
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", f"http://127.0.0.1:{port}/services/hook")
    token = "xoxb-FAKE-LEAKED-BOT-123456789"
    hook = "https://hooks.slack.com/services/T99/B99/supersecret"
    ai_key = "sk-live-panelcad-should-not-leak"
    env_key = "sk-env-panelcad-also-secret"
    monkeypatch.setenv("PANELCAD_AI_API_KEY", env_key)
    logging.getLogger("panelcad").info("lookup failed %s %s %s %s", token, hook, ai_key, env_key)
    session = Session.create("Context")
    session.set_title_block(company="Acme Controls")
    traceback = "Traceback (most recent call last):\n  File \"coil.py\", line 9, in run\nRuntimeError: coil vanished"
    sent = session.report_bug(
        "Coil vanished",
        f"The coil tag disappeared after numbering. token {token} key {ai_key} env {env_key} {hook}",
        steps="1. Open the job\n2. Number the wires",
        contact="George at the shop",
        include_log=True,
        traceback=traceback,
        send=True,
    )
    assert sent["sent"] is True, sent
    assert sent["send"]["uploaded"] is False
    posted = json.loads(http_server.hits[-1]["body"])["text"]
    assert posted.splitlines()[0] == "*Coil vanished*"
    assert f"version: {__version__}" in posted
    assert "contact: George at the shop" in posted
    assert "What happened\nThe coil tag disappeared after numbering." in posted
    assert "Steps to reproduce\n1. Open the job\n2. Number the wires" in posted
    assert "Recent actions\n" in posted
    assert "undo steps:" in posted
    assert "set_title_block" in posted
    assert "Traceback\nTraceback (most recent call last):" in posted
    assert "RuntimeError: coil vanished" in posted
    assert "Recent log\n" in posted
    assert "lookup failed" in posted
    for secret in (token, hook, ai_key, env_key, "hooks.slack.com"):
        assert secret not in posted
    blob = Path(sent["path"]).read_bytes()
    for secret in (token, hook, ai_key, env_key):
        assert secret.encode() not in blob
    with zipfile.ZipFile(sent["path"]) as archive:
        report = json.loads(archive.read("report.json"))
    assert report["contact"] == "George at the shop"
    assert "Number the wires" in report["steps"]
    assert "set_title_block" in report["recent_actions"]
    assert report["version"] == __version__
    assert "coil vanished" in report["traceback"]
    assert token not in report["description"]
    assert "[redacted]" in report["description"]


def test_bot_uploads_screenshot_and_zip(http_server, monkeypatch):
    """files:write upload posts the image and the zip. A working upload does not also webhook."""

    configure_logging()
    port = http_server.server_address[1]
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", f"http://127.0.0.1:{port}/services/hook")
    token = "xoxb-FAKE-BOT-UPLOAD-123456789"
    set_slack_api_base(f"http://127.0.0.1:{port}/api")
    logging.getLogger("panelcad").info("recent-log-marker before the report")
    session = Session.create("Upload")
    session.set_title_block(drawn_by="George")
    png = b"\x89PNG\r\n\x1a\n" + b"screenshot-bytes"
    traceback = "Traceback (most recent call last):\nRuntimeError: screen froze"
    sent = session.report_bug(
        "Screen froze",
        "The sheet stopped painting.",
        steps="Pan the layout",
        contact="crew desk",
        include_screenshot=True,
        screenshot_png=png,
        traceback=traceback,
        send=True,
        settings=BugSettings(slack_bot_token=token, slack_channel="C0TESTBUGS01"),
    )
    assert sent["sent"] is True, sent
    assert sent["send"]["uploaded"] is True
    assert [row["reporter"] for row in sent["send"]["results"]] == ["slack-bot"]
    paths = [hit["path"] for hit in http_server.hits]
    assert paths == [
        "/api/files.getUploadURLExternal",
        "/upload",
        "/api/files.getUploadURLExternal",
        "/upload",
        "/api/files.completeUploadExternal",
    ]
    assert not any("files.upload" in item for item in paths)
    assert not any(hit["path"].startswith("/services/") for hit in http_server.hits)
    assert http_server.hits[0]["headers"]["Authorization"] == f"Bearer {token}"
    assert b"filename=screenshot.png" in http_server.hits[0]["body"]
    assert http_server.hits[1]["body"] == png
    assert b"filename=" in http_server.hits[2]["body"]
    assert Path(sent["path"]).name.encode() in http_server.hits[2]["body"]
    assert http_server.hits[3]["body"] == Path(sent["path"]).read_bytes()
    put_headers = http_server.hits[1]["headers"]
    assert token not in " ".join(put_headers.values())
    finished = json.loads(http_server.hits[4]["body"])
    assert finished["channel_id"] == "C0TESTBUGS01"
    assert [item["title"] for item in finished["files"]] == ["screenshot.png", Path(sent["path"]).name]
    assert [item["id"] for item in finished["files"]] == ["FTEST123", "FTEST2"]
    comment = finished["initial_comment"]
    assert "What happened\nThe sheet stopped painting." in comment
    assert "Steps to reproduce\nPan the layout" in comment
    assert "contact: crew desk" in comment
    assert f"version: {__version__}" in comment
    assert "screenshot: screenshot.png" in comment
    assert "Recent log\n" in comment
    assert "recent-log-marker" in comment
    assert "Traceback\nTraceback (most recent call last):" in comment
    assert "RuntimeError: screen froze" in comment
    assert "undo steps:" in comment
    assert "set_title_block" in comment
    assert token not in comment
    assert token.encode() not in Path(sent["path"]).read_bytes()


def test_webhook_text_when_upload_fails(http_server, monkeypatch):
    """A failed bot upload still posts the full text through the webhook."""

    configure_logging()
    http_server.fail_uploads = True
    port = http_server.server_address[1]
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", f"http://127.0.0.1:{port}/services/hook")
    token = "xoxb-FAKE-BOT-FALLBACK-123456789"
    hook = "https://hooks.slack.com/services/T77/B77/fallbacksecret"
    set_slack_api_base(f"http://127.0.0.1:{port}/api")
    logging.getLogger("panelcad").info("fallback-log-marker %s", hook)
    session = Session.create("Fallback")
    session.set_title_block(company="Crew")
    sent = session.report_bug(
        "Upload failed",
        f"Still tell the channel. {token}",
        steps="Click Send",
        contact="George",
        traceback="RuntimeError: upload broke",
        send=True,
        settings=BugSettings(slack_bot_token=token, slack_channel="C0TESTBUGS01"),
    )
    assert sent["sent"] is True, sent
    assert sent["send"]["uploaded"] is False
    reporters = [row["reporter"] for row in sent["send"]["results"]]
    assert reporters == ["slack-bot", "webhook"]
    assert sent["send"]["results"][0]["ok"] is False
    assert sent["send"]["results"][1]["ok"] is True
    paths = [hit["path"] for hit in http_server.hits]
    assert "/api/files.getUploadURLExternal" in paths
    assert "/api/files.completeUploadExternal" not in paths
    assert "/services/hook" in paths
    posted = json.loads(next(hit["body"] for hit in http_server.hits if hit["path"] == "/services/hook"))
    text = posted["text"]
    assert "What happened\nStill tell the channel." in text
    assert "Steps to reproduce\nClick Send" in text
    assert "contact: George" in text
    assert f"version: {__version__}" in text
    assert "RuntimeError: upload broke" in text
    assert "fallback-log-marker" in text
    assert "undo steps:" in text
    assert token not in text
    assert hook not in text
    assert "hooks.slack.com" not in text
    assert token.encode() not in Path(sent["path"]).read_bytes()
    side = json.loads(Path(sent["status_path"]).read_text(encoding="utf-8"))
    assert side["status"] == "sent"
    assert side["reporter"] == "webhook"


def test_send_failure_keeps_zip(http_server, monkeypatch):
    http_server.fail = True
    port = http_server.server_address[1]
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", f"http://127.0.0.1:{port}/services/hook")
    session = Session.create("Fail")
    result = session.report_bug("Nope", "Did not send", send=True)
    assert result["sent"] is False
    assert Path(result["path"]).is_file()
    assert str(result["path"]) in str(result["send"]["error"]) or "Report saved" in str(result["send"]["error"])
    assert result["send"]["message"].startswith("Not sent:")
    assert result["path"] in result["send"]["message"]
    assert "Saved only at" in result["send"]["message"]
    failed_side = json.loads(Path(str(result["path"]) + ".status.json").read_text(encoding="utf-8"))
    assert failed_side["status"] == "saved_only"
    assert "http://" not in failed_side["error"]


def test_session_jsonl_and_cli(tmp_path):
    from typer.testing import CliRunner

    session = Session.create("API")
    project = tmp_path / "api.pcad.yaml"
    session.save(project)
    before = project.read_bytes()
    result = session.report_bug("API title", "API description", include_project=True, send=False)
    assert Path(result["path"]).is_file()
    assert project.read_bytes() == before
    with zipfile.ZipFile(result["path"]) as archive:
        assert "project.pcad.yaml" in archive.namelist()
        assert "API" in archive.read("project.pcad.yaml").decode("utf-8")

    agent = Agent()
    created = agent.handle({"id": "c1", "op": "create_project", "name": "JSONL"})
    assert created["ok"]
    reported = agent.handle(
        {
            "id": "corr-9",
            "op": "report_bug",
            "title": "JSONL title",
            "description": "JSONL description",
            "include_project": False,
            "send": False,
        }
    )
    assert reported["ok"] is True
    assert reported["id"] == "corr-9"
    assert Path(reported["result"]["path"]).is_file()
    help_row = agent.handle({"id": "h", "op": "help"})
    assert "report_bug" in help_row["result"]["ops"]
    assert help_row["result"]["schemas"]["report_bug"] == REPORT_BUG_SCHEMA
    assert "report_bug" in help_row["result"]["help"]

    failed = agent.handle({"id": "bad", "op": "not_a_real_op"})
    assert failed["ok"] is False
    assert failed["crash"]["id"]
    assert Path(failed["crash"]["path"]).is_file()
    assert project.read_bytes() == before

    runner = CliRunner()
    logs = runner.invoke(app, ["logs", "--path"])
    assert logs.exit_code == 0
    assert Path(logs.stdout.strip()) == logs_dir()
    report = runner.invoke(
        app,
        [
            "bug-report",
            "--title",
            "CLI title",
            "--description",
            "CLI description",
            "--project",
            str(project),
            "--send",
        ],
    )
    assert report.exit_code == 1
    zpath = Path(report.stdout.strip().splitlines()[0])
    assert zpath.is_file()
    assert project.read_bytes() == before
    assert "not configured" in (report.stderr + report.stdout).lower()


def test_cli_crash_record_has_no_dialog(monkeypatch):
    monkeypatch.setenv("PANELCAD_DIAGNOSTICS", "1")
    from panelcad.diag.runtime import install_process_diagnostics

    install_process_diagnostics(gui=False)
    import click

    @click.command()
    def boom() -> None:
        raise RuntimeError("cli-boom-marker")

    with pytest.raises(RuntimeError, match="cli-boom-marker"):
        boom.main(args=[], standalone_mode=False)
    assert dialogs_shown() == 0
    assert any("cli-boom-marker" in item.read_text(encoding="utf-8") for item in crashes_dir().glob("*.json"))


def test_reporter_plugin(tmp_path):
    del tmp_path
    seen: dict[str, str] = {}

    class Echo:
        name = "echo"

        def send(self, summary: str, zip_path: Path | None = None) -> SendResult:
            seen["summary"] = summary
            seen["zip"] = str(zip_path or "")
            return SendResult(ok=True, reporter=self.name, detail="queued")

    def factory(settings, transport):
        del settings, transport
        return Echo()

    register_reporter(factory)
    session = Session.create("Plugin")
    result = session.report_bug("Plugin title", "Plugin body", send=True)
    assert result["sent"] is True
    assert "Plugin title" in seen["summary"]
    assert Path(seen["zip"]).is_file()


def _menu(window: MainWindow, title: str):
    for action in window.menuBar().actions():
        if action.text().replace("&", "").lower().startswith(title.lower()):
            return action.menu()
    raise AssertionError(title)


def test_windows_profile_redaction_in_log_and_fault(monkeypatch):
    """USERPROFILE / LOCALAPPDATA paths become ~\\... in the log and in a fault dump."""

    profile = r"C:\Users\Ada"
    monkeypatch.setenv("USERPROFILE", profile)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Ada\AppData\Local")
    assert redact_text(r"C:\Users\Ada\drawings\job.pcad.yaml") == r"~\drawings\job.pcad.yaml"
    assert redact_text("C:/Users/Ada/drawings/job.pcad.yaml") == "~/drawings/job.pcad.yaml"
    assert redact_text(r"c:\users\ada\drawings\job.pcad.yaml") == r"~\drawings\job.pcad.yaml"
    assert "Ada Lovelace" in redact_text(r"C:\Users\Ada Lovelace\notes.txt")

    configure_logging()
    local_log = os.environ["LOCALAPPDATA"] + r"\PanelCAD\logs\panelcad.log"
    logging.getLogger("panelcad").info(
        "open %s data %s",
        r"C:\Users\Ada\Documents\panel.pcad.yaml",
        local_log,
    )
    logging.getLogger("panelcad").info("also %s", r"c:\users\ada\drawings\job.pcad.yaml")
    text = log_file_path().read_text(encoding="utf-8")
    assert profile not in text
    assert r"c:\users\ada" not in text.lower()
    assert r"~\Documents\panel.pcad.yaml" in text
    assert r"~\AppData\Local\PanelCAD\logs\panelcad.log" in text
    assert r"~\drawings\job.pcad.yaml" in text

    fault_log_path().write_text(
        "Fatal Python error: abort\n"
        '  File "C:\\Users\\Ada\\AppData\\Local\\PanelCAD\\panelcad.exe", line 1\n'
        '  File "C:/Users/Ada/Documents/panel.pcad.yaml", line 2\n'
        '  File "c:\\users\\ada\\drawings\\job.pcad.yaml", line 3\n',
        encoding="utf-8",
    )
    found = begin_session(gui=True)
    assert found is not None
    assert "faulthandler" in found.reasons
    data = json.loads(Path(found.crash_path).read_text(encoding="utf-8"))
    combined = data["traceback"] + "\n" + str(data.get("recent_log") or "")
    assert r"C:\Users\Ada" not in combined
    assert "C:/Users/Ada" not in combined
    assert r"c:\users\ada" not in combined.lower()
    assert r"~\AppData\Local\PanelCAD\panelcad.exe" in data["traceback"]
    assert "~/Documents/panel.pcad.yaml" in data["traceback"]
    assert r"~\drawings\job.pcad.yaml" in data["traceback"]
    mark_clean_exit(force=True)


def test_slack_message_includes_full_context():
    from panelcad.diag.report import POST_TEXT_LIMIT, ReportRequest, build_report, build_summary

    request = ReportRequest(
        title="Coil tag",
        description="The full story stays in the zip.",
        steps="1. Open the job\n2. Number the wires",
        contact="George",
        traceback='Traceback (most recent call last):\n  File "job.py"\nRuntimeError: boom',
        recent_actions="undo steps: 2; redo steps: 0\nplace M1",
        crash_id="abc123",
        include_system=True,
        include_screenshot=True,
        screenshot_png=b"\x89PNG",
    )
    text = build_summary(request)
    assert text.splitlines()[0] == "*Coil tag*"
    assert "What happened\nThe full story stays in the zip." in text
    assert "Steps to reproduce\n1. Open the job\n2. Number the wires" in text
    assert "contact: George" in text
    assert "version:" in text
    assert "os:" in text
    assert "screenshot: screenshot.png" in text
    assert "Recent actions\nundo steps: 2; redo steps: 0\nplace M1" in text
    if __import__("platform").node().strip():
        assert f"computer: {__import__('platform').node().strip()}" in text
    assert "crash id: abc123" in text
    assert "Traceback (most recent call last)" in text
    assert "exception: RuntimeError: boom" in text
    assert len(text) <= POST_TEXT_LIMIT
    quiet = build_summary(
        ReportRequest(title="Coil tag", description="stay in", steps="do post this", include_system=False)
    )
    assert "computer:" not in quiet
    assert "do post this" in quiet
    assert "stay in" in quiet
    built = build_report(request)
    assert built.payload["description"] == "The full story stays in the zip."
    assert "Number the wires" in built.payload["steps"]
    assert built.payload["recent_actions"].startswith("undo steps: 2")
    assert built.payload["system"]["computer"]
    bare = build_report(ReportRequest(title="", description="First line\nrest", include_system=False))
    assert bare.payload["system"] is None
    assert "rest" in bare.payload["description"]
    fallback = build_summary(ReportRequest(title="", description="First line\nrest", include_system=False))
    assert fallback.splitlines()[0] == "*First line*"
    assert "What happened\nFirst line\nrest" in fallback
    huge = build_summary(
        ReportRequest(
            title="Clip",
            description="KEEP-DESC " + ("x" * 50_000),
            steps="KEEP-STEPS",
            contact="KEEP-CONTACT",
            recent_actions="KEEP-ACTIONS",
            traceback="KEEP-TRACE\n" + ("t" * 20_000),
            include_log=False,
            include_system=False,
        )
    )
    assert len(huge) <= POST_TEXT_LIMIT
    assert huge.splitlines()[0] == "*Clip*"
    assert "KEEP-CONTACT" in huge
    assert "KEEP-DESC" in huge
    assert "KEEP-STEPS" in huge


def test_traceback_summary_uses_innermost_frame():
    from panelcad.diag.report import ReportRequest, build_summary, traceback_summary

    tb = "\n".join(
        [
            "Traceback (most recent call last):",
            '  File "C:\\\\Users\\\\Ada\\\\app.py", line 4, in main',
            "    run()",
            '  File "C:\\\\Users\\\\Ada\\\\wires.py", line 29, in number_wires',
            "    context line 29",
            "RuntimeError: coil tag disappeared",
        ]
    )
    frame, exc = traceback_summary(tb)
    assert frame == "wires.py:29 in number_wires"
    assert exc == "RuntimeError: coil tag disappeared"
    text = build_summary(ReportRequest(title="Coil", description="The long story", traceback=tb, include_system=False))
    assert "Traceback\n" in text
    assert "context line 29" in text
    assert "What happened\nThe long story" in text
    assert "where: wires.py:29 in number_wires" in text
    assert "exception: RuntimeError: coil tag disappeared" in text


def test_local_time_uses_the_pc_zone():
    from datetime import timedelta, tzinfo

    from panelcad.diag.report import ReportRequest, build_report, local_time_text

    class Eastern(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=-4)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return "EDT"

    class OffsetOnly(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=-5)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return ""

    class LongEastern(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=-4)

        def dst(self, dt):
            return timedelta(hours=1)

        def tzname(self, dt):
            return "Eastern Daylight Time"

    class UnknownLong(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=-4)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return "Somewhere Custom Time"

    stamp = "2026-10-08T17:04:00Z"
    assert local_time_text(stamp, tz=Eastern()) == "2026-10-08 13:04 EDT"
    assert plain_when(stamp, tz=Eastern()) == "2026-10-08 13:04 EDT"
    assert local_time_text(stamp, tz=LongEastern()) == "2026-10-08 13:04 EDT"
    assert "Eastern Daylight Time" not in local_time_text(stamp, tz=LongEastern())
    assert local_time_text(stamp, tz=UnknownLong()) == "2026-10-08 13:04 UTC-4"
    assert local_time_text(stamp, tz=OffsetOnly()).endswith("-0500")
    assert "12:04" in local_time_text(stamp, tz=OffsetOnly())
    built = build_report(
        ReportRequest(title="T", description="D", created=stamp, include_log=False, include_system=False)
    )
    assert built.payload["created"] == stamp
    assert built.payload["created_local"] == local_time_text(stamp)
    assert "T17:04:00Z" not in built.payload["created_local"]


def test_unclean_exit_names_the_project():
    from panelcad.diag.crash import note_open_project

    configure_logging()
    begin_session(gui=True)
    note_open_project(r"C:\Users\Ada\drawings\job.pcad.yaml", "Job")
    path = current_lock_path()
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["active"] = "2026-10-08T17:04:00Z"
    path.write_text(json.dumps(data), encoding="utf-8")
    started = str(data["started"])
    _leave_dead_lock()
    again = begin_session(gui=True)
    assert again is not None
    assert again.project_name == "Job"
    assert again.project_path.endswith("job.pcad.yaml")
    assert again.when == "2026-10-08T17:04:00Z"
    assert again.when != started
    acknowledge_unclean(again)
    mark_clean_exit(force=True)


def test_gui_menus_and_dialogs(monkeypatch):
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.screen_limit",
        lambda widget=None: (1366, 768),
    )
    app = QApplication.instance() or QApplication([])
    session = Session.create("Dialogs")
    saved = Path(app_data_dir()) / "job.pcad.yaml"
    # Keep the drawing next to the temp data dir, still under the test tmp root.
    project = saved.parent.parent / "job.pcad.yaml"
    session.save(project)
    window = MainWindow(session)
    window.show()
    app.processEvents()
    help_menu = _menu(window, "Help")
    labels = [action.text().replace("&", "") for action in help_menu.actions() if not action.isSeparator()]
    assert "Report a bug…" in labels
    edit = _menu(window, "Edit")
    edit_labels = [action.text().replace("&", "") for action in edit.actions()]
    assert "Settings…" in edit_labels

    long_trace = "\n".join(f"trace line {index}" for index in range(40))
    bug = BugReportDialog(
        window,
        session,
        what="The coil vanished\nThe rest stays in the zip.",
        steps="1. Place it",
        traceback=long_trace,
    )
    bug.show()
    app.processEvents()
    assert bug.title_edit.text() == ""
    assert not bug.preview.isVisible()
    assert bug.send_btn.isEnabled()
    destination = bug.findChild(QLabel, "bugDestination")
    assert destination is not None
    assert destination.text() == (
        "Sending is not set up on this PC. Click Save and email the file to the office."
    )
    assert bug.height() <= 768 - 48
    preview = bug.preview.toPlainText()
    assert "*The coil vanished*" in preview
    assert "report.json" in preview
    assert "Slack / webhook message" in preview
    assert "The rest stays in the zip." in preview
    bug.show_preview.setChecked(True)
    app.processEvents()
    assert bug.preview.isVisible()
    assert bug.height() <= 768 - 48
    assert bug.preview.verticalScrollBar().maximum() > 0
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.screen_limit",
        lambda widget=None: (1920, 1080),
    )
    bug._fit()
    app.processEvents()
    assert bug.height() <= 1080 - 48
    bug.title_edit.setText("Coil tag")
    assert "*Coil tag*" in bug.preview.toPlainText()
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", "https://hooks.slack.com/services/T/B/x")
    bug._refresh_send()
    assert bug.send_btn.isEnabled()
    assert bug.send_btn.toolTip() == ""
    assert bug.findChild(QLabel, "bugDestination").text() == "Reports go to: your webhook"
    assert "hooks.slack.com" not in bug.findChild(QLabel, "bugDestination").text()
    bug.close()

    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.screen_limit",
        lambda widget=None: (1366, 768),
    )
    try:
        raise RuntimeError("dialog-boom")
    except RuntimeError as exc:
        record = record_exception(exc, source="test", fatal=False)
    crash = CrashDialog(window, record)
    crash.show()
    app.processEvents()
    assert crash.windowTitle() == "PanelCAD hit an error"
    buttons = [button.text() for button in crash.findChildren(QPushButton)]
    assert buttons == ["Report", "Save report", "Close"]
    recovery_text = crash.findChild(QLabel, "crashRecovery").text()
    assert "saved" in recovery_text.lower()
    assert "Dialogs" in recovery_text
    assert not crash.tb_view.isVisible()
    first = crash.findChild(QLabel, "crashTraceFirst").text()
    assert "Traceback (most recent call last)" not in first
    assert "RuntimeError:" in first
    assert ".py:" in first
    crash.tb_toggle.setChecked(True)
    assert crash.tb_view.isVisible()
    assert crash.height() <= 768 - 48
    crash.close()

    info = UncleanExit(
        reasons=["session", "faulthandler"],
        recovery_path=str(project),
        project_path=str(project),
        project_name="Dialogs",
        when="2026-10-08T17:04:00Z",
    )
    recovery = RecoveryDialog(window, session, info)
    recovery.show()
    app.processEvents()
    recovery_buttons = [button.text() for button in recovery.findChildren(QPushButton)]
    assert "Send report" in recovery_buttons
    assert "Reopen recovery copy" in recovery_buttons
    visible = " ".join(label.text() for label in recovery.findChildren(QLabel) if not label.isHidden())
    assert "closed unexpectedly" in visible
    assert "Last active" in visible
    assert plain_when("2026-10-08T17:04:00Z") in visible
    project_heading = recovery.findChild(QLabel, "recoveryProjectHeading")
    copy_heading = recovery.findChild(QLabel, "recoveryCopyHeading")
    assert "font-weight: 600" in project_heading.styleSheet()
    assert "font-weight: 600" in copy_heading.styleSheet()
    assert "Dialogs" in visible
    assert str(project) in visible
    assert "faulthandler" not in visible
    assert "lock" not in visible.lower()
    assert not recovery.details_view.isVisible()
    recovery.details.setChecked(True)
    assert "faulthandler" in recovery.details_view.toPlainText()
    assert recovery.height() <= 768 - 48
    recovery.close()

    settings = SettingsDialog(window, session)
    settings.show()
    app.processEvents()
    test_report = next(button for button in settings.findChildren(QPushButton) if button.text() == "Send test report")
    assert test_report.isEnabled()
    monkeypatch.delenv("PANELCAD_BUG_WEBHOOK", raising=False)
    settings._refresh_test()
    assert test_report.isEnabled() is False
    assert test_report.toolTip() == "Add a webhook, or a bot token and a channel id."
    assert settings.autosave.objectName() == "autosaveSeconds"
    assert settings.autosave.value() == 300
    settings.close()
    window.close()
    assert project.read_bytes()


def test_style_change_after_two_windows_stays_alive():
    """A later Fusion reset must not walk dock tab bars from earlier windows."""

    agent = Agent()
    created = agent.handle(
        {
            "id": 1,
            "op": "create_project",
            "name": "Ladder Job",
            "standard": "ANSI",
            "drawing_style": "ladder",
            "coil_style": "square",
        }
    )
    assert created["ok"] is True
    app = QApplication.instance() or QApplication([])
    first = MainWindow(Session.create("Visible"))
    first.show()
    app.processEvents()
    first.reset_tool_windows()
    app.processEvents()
    first.close()
    second = MainWindow(Session.create("Click"))
    second.show()
    app.processEvents()
    second.close()
    app.setStyle("Fusion")
    app.processEvents()
    third = MainWindow(Session.create("Dialogs"))
    third.show()
    app.processEvents()
    assert third.isVisible()
    third.close()


def test_expanded_report_dialog_stays_inside_the_work_area(monkeypatch):
    """Growing the preview must move the dialog, not leave Save/Send/Close off screen."""

    area = QRect(0, 40, 1920, 1000)
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.screen_work_area",
        lambda widget=None: QRect(area),
    )
    app = QApplication.instance() or QApplication([])
    session = Session.create("Fit")
    bug = BugReportDialog(
        None,
        session,
        what="The coil vanished\n" + ("detail line\n" * 12),
        steps="1. Open the job\n" * 8,
    )
    bug.show()
    app.processEvents()
    low = area.bottom() - 180
    bug.move(80, low)
    app.processEvents()
    bug.show_preview.setChecked(True)
    app.processEvents()
    frame = bug.frameGeometry()
    assert frame.height() > 400
    assert frame.top() >= area.top()
    assert frame.left() >= area.left()
    assert frame.bottom() <= area.bottom()
    assert frame.right() <= area.right()
    assert frame.top() < low
    for label in ("Save", "Send", "Close"):
        button = next(item for item in bug.findChildren(QPushButton) if item.text() == label)
        assert area.contains(button.mapToGlobal(button.rect().center()))
    bug.close()


def test_second_crash_still_recovers_the_open_drawing():
    configure_logging()
    begin_session(gui=True)
    session = Session.create("NEMA seal-in")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(session)
    window.show()
    app.processEvents()
    assert crash_dialog_parent() is window
    assert recovery_session() is session
    try:
        raise RuntimeError("first-crash")
    except RuntimeError as exc:
        record_exception(exc, source="test", fatal=False)
    Session.create("Crash report")
    CrashDialog(None, {"exception": "helper", "traceback": "RuntimeError: helper\n", "path": ""})
    assert recovery_session() is session
    lock = json.loads(current_lock_path().read_text(encoding="utf-8"))
    assert lock["project_name"] == "NEMA seal-in"
    try:
        raise RuntimeError("second-crash")
    except RuntimeError as exc:
        record_exception(exc, source="test", fatal=False)
    assert recovery_session() is session
    lock = json.loads(current_lock_path().read_text(encoding="utf-8"))
    assert lock["project_name"] == "NEMA seal-in"
    body = Path(lock["recovery_path"]).read_text(encoding="utf-8")
    assert "NEMA seal-in" in body
    assert "Crash report" not in body
    copies = sorted(recovery_dir().glob("recovery-*.pcad.yaml"), key=lambda item: item.stat().st_mtime)
    assert len(copies) >= 2
    assert "NEMA seal-in" in copies[-1].read_text(encoding="utf-8")
    assert "Crash report" not in copies[-1].read_text(encoding="utf-8")
    window.close()


def test_recovery_offer_uses_the_lock_not_the_newest_file(tmp_path):
    configure_logging()
    folder = recovery_dir()
    folder.mkdir(parents=True, exist_ok=True)
    nema = folder / "recovery-nema.pcad.yaml"
    dol = folder / "recovery-dol.pcad.yaml"
    nema.write_text("name: NEMA seal-in\n", encoding="utf-8")
    dol.write_text("name: DOL Motor Starter\n", encoding="utf-8")
    now = time.time()
    os.utime(nema, (now - 5000, now - 5000))
    os.utime(dol, (now, now))
    dead = sessions_dir() / "2147483647-stale.json"
    dead.parent.mkdir(parents=True, exist_ok=True)
    dead.write_text(
        json.dumps(
            {
                "pid": 2147483647,
                "started": "dead-start",
                "gui": True,
                "active": "2026-10-08T16:00:00Z",
                "project_name": "NEMA seal-in",
                "project_path": str(tmp_path / "nema.pcad.yaml"),
                "recovery_path": str(nema),
            }
        ),
        encoding="utf-8",
    )
    info = begin_session(gui=True)
    assert info is not None
    assert info.project_name == "NEMA seal-in"
    assert info.recovery_path == str(nema)
    assert "DOL" not in info.recovery_path
    assert info.when == "2026-10-08T16:00:00Z"
    assert dead.is_file()
    acknowledge_unclean(info)
    assert not dead.is_file()
    mark_clean_exit(force=True)


def test_prune_keeps_newest_20_and_drops_older_than_14_days():
    configure_logging()
    begin_session(gui=True)
    folder = recovery_dir()
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for index in range(21):
        path = folder / f"recovery-{index:02d}.pcad.yaml"
        path.write_text(f"n: {index}\n", encoding="utf-8")
        stamp = now - (21 - index) * 10
        os.utime(path, (stamp, stamp))
    old = folder / "recovery-old.pcad.yaml"
    old.write_text("old\n", encoding="utf-8")
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    prune_recovery_files()
    names = {path.name for path in folder.glob("*.pcad.yaml")}
    assert old.name not in names
    assert "recovery-00.pcad.yaml" not in names
    assert "recovery-20.pcad.yaml" in names
    assert len(list(folder.glob("*.pcad.yaml"))) == 20
    kept = folder / "recovery-kept.pcad.yaml"
    kept.write_text("kept\n", encoding="utf-8")
    os.utime(kept, (now - 20 * 86400, now - 20 * 86400))
    lock = current_lock_path()
    assert lock is not None
    data = json.loads(lock.read_text(encoding="utf-8"))
    data["recovery_path"] = str(kept)
    lock.write_text(json.dumps(data), encoding="utf-8")
    prune_recovery_files()
    assert kept.is_file()
    mark_clean_exit(force=True)


def test_autosave_copies_unsaved_work_and_not_the_user_file(tmp_path, monkeypatch):
    from panelcad.diag.settings import BugSettings, autosave_seconds, save_settings

    configure_logging()
    assert autosave_seconds() == 300
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({"autosave_seconds": 120}) + "\n", encoding="utf-8")
    assert autosave_seconds() == 120
    save_settings(BugSettings(webhook_url="https://hooks.slack.com/services/T/B/x"))
    stored = json.loads(settings_path().read_text(encoding="utf-8"))
    assert stored["autosave_seconds"] == 120
    assert stored["bug_reports"]["webhook_url"].startswith("https://hooks.slack.com/")
    begin_session(gui=True)
    session = Session.create("Seal")
    dest = tmp_path / "seal.pcad.yaml"
    session.save(dest)
    set_recovery_session(session)
    before = dest.read_bytes()
    assert autosave_now() is None
    session.project.name = "Seal dirty"
    monkeypatch.setenv("PANELCAD_AUTOSAVE_SECONDS", "0")
    assert autosave_seconds() == 0
    assert autosave_now() is None
    assert dest.read_bytes() == before
    assert list(recovery_dir().glob("*.pcad.yaml")) == []
    monkeypatch.setenv("PANELCAD_AUTOSAVE_SECONDS", "300")
    copied = autosave_now()
    assert copied is not None
    assert copied.parent == recovery_dir()
    assert copied.resolve() != dest.resolve()
    assert dest.read_bytes() == before
    assert "Seal dirty" in copied.read_text(encoding="utf-8")
    assert "Seal dirty" not in dest.read_text(encoding="utf-8")
    lock = json.loads(current_lock_path().read_text(encoding="utf-8"))
    assert lock["recovery_path"] == str(copied)
    assert autosave_now() is None
    data = json.loads(current_lock_path().read_text(encoding="utf-8"))
    data["active"] = "2020-01-01T00:00:00Z"
    current_lock_path().write_text(json.dumps(data), encoding="utf-8")
    heartbeat_now()
    assert json.loads(current_lock_path().read_text(encoding="utf-8"))["active"] != "2020-01-01T00:00:00Z"
    mark_clean_exit(force=True)


def test_log_errors_and_warnings_use_their_levels():
    from panelcad.diag.log import log_event

    configure_logging()
    log_event("error", "level-error-marker")
    log_event("warning", "level-warning-marker")
    log_event("session", "level-info-marker")
    lines = log_file_path().read_text(encoding="utf-8").splitlines()
    assert " ERROR " in next(line for line in lines if "level-error-marker" in line)
    assert " WARNING " in next(line for line in lines if "level-warning-marker" in line)
    assert " INFO " in next(line for line in lines if "level-info-marker" in line)


def test_diagnostics_arm_is_idempotent(monkeypatch):
    from panelcad.diag.runtime import install_process_diagnostics, reset_process_diagnostics

    monkeypatch.setenv("PANELCAD_DIAGNOSTICS", "1")
    reset_process_diagnostics()
    install_process_diagnostics(gui=False)
    install_process_diagnostics(gui=False)
    text = log_file_path().read_text(encoding="utf-8")
    assert text.count("startup version=") == 1
    assert text.count("session started") == 1
    mark_clean_exit(force=True)


def test_clean_exit_disables_faulthandler():
    import faulthandler

    configure_logging()
    begin_session(gui=True)
    assert faulthandler.is_enabled()
    mark_clean_exit(force=True)
    assert faulthandler.is_enabled() is False


def test_recycled_pid_is_not_a_live_session():
    configure_logging()
    token = process_start_token(os.getpid())
    assert token
    assert process_start_token(os.getpid()) == token
    assert process_start_token(2147483647) is None
    begin_session(gui=True)
    mark_clean_exit(force=True)
    stale = sessions_dir() / f"{os.getpid()}-recycled.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "started": "not-" + token,
                "gui": True,
                "active": "2026-10-08T15:00:00Z",
                "project_name": "Stale",
                "project_path": "",
                "recovery_path": "",
            }
        ),
        encoding="utf-8",
    )
    found = begin_session(gui=True)
    assert found is not None
    assert found.project_name == "Stale"
    assert stale.is_file()
    acknowledge_unclean(found)
    assert not stale.is_file()
    assert current_lock_path() is not None and current_lock_path().is_file()
    mark_clean_exit(force=True)


def _start_gui_lock() -> tuple[subprocess.Popen[str], Path]:
    code = """
import os, sys, time
os.environ["PANELCAD_DATA_DIR"] = sys.argv[1]
from panelcad.diag.crash import begin_session, current_lock_path
begin_session(gui=True)
print("LOCK " + str(current_lock_path()), flush=True)
print("PID " + str(os.getpid()), flush=True)
time.sleep(60)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(app_data_dir())],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    box: dict[str, str] = {}

    def _read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("LOCK "):
                box["lock"] = line.split(" ", 1)[1].strip()
            elif line.startswith("PID "):
                box["pid"] = line.split(" ", 1)[1].strip()
            else:
                box["rest"] = box.get("rest", "") + line
            if "lock" in box and "pid" in box:
                return

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(30)
    if "lock" not in box or "pid" not in box:
        _stop_process_tree(proc)
        reader.join(2)
        raise AssertionError(f"gui child produced no lock: {box.get('rest', '')}")
    return proc, Path(box["lock"])


def _lock_pid(lock: Path) -> int:
    """Pid the child interpreter wrote. On Windows this is not Popen.pid."""

    return int(json.loads(lock.read_text(encoding="utf-8"))["pid"])


def _stop_process_tree(proc: subprocess.Popen[str], extra_pid: int | None = None) -> None:
    """Kill the launcher and the real interpreter, including a venv child."""

    pids = [proc.pid]
    if extra_pid and extra_pid not in pids:
        pids.append(extra_pid)
    if sys.platform == "win32":
        for pid in pids:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    elif proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
    if extra_pid and extra_pid != proc.pid and sys.platform != "win32":
        try:
            os.kill(extra_pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _stop_gui_lock(proc: subprocess.Popen[str], lock: Path | None) -> None:
    extra = None
    if lock is not None and lock.is_file():
        try:
            extra = _lock_pid(lock)
        except (OSError, ValueError, json.JSONDecodeError, KeyError):
            extra = None
    _stop_process_tree(proc, extra)


def _wait_until_dead(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process_start_token(pid) is None:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} still looks live")


def test_second_gui_ignores_a_live_lock_and_reports_a_killed_one():
    configure_logging()
    proc, lock = _start_gui_lock()
    try:
        child = _lock_pid(lock)
        info = begin_session(gui=True)
        assert lock.is_file()
        assert process_start_token(child) is not None
        assert str(lock) not in (info.lock_paths if info else [])
        _stop_gui_lock(proc, lock)
        _wait_until_dead(child)
        found = begin_session(gui=True)
        assert found is not None
        assert "session" in found.reasons
        assert str(lock) in found.lock_paths
        assert lock.is_file()
        body = json.loads(lock.read_text(encoding="utf-8"))
        assert body["pid"] == child
        assert found.session is not None
        assert found.session["pid"] == child
        acknowledge_unclean(found)
        assert not lock.is_file()
        assert current_lock_path() is not None and current_lock_path().is_file()
    finally:
        _stop_gui_lock(proc, lock)
        mark_clean_exit(force=True)


def test_cli_does_not_consume_a_live_gui_lock(monkeypatch):
    from typer.testing import CliRunner

    from panelcad.diag.runtime import reset_process_diagnostics

    configure_logging()
    proc, lock = _start_gui_lock()
    try:
        child = _lock_pid(lock)
        monkeypatch.setenv("PANELCAD_DIAGNOSTICS", "1")
        reset_process_diagnostics()
        result = CliRunner().invoke(app, ["logs", "--path"])
        assert result.exit_code == 0, result.output
        assert lock.is_file()
        ours = current_lock_path()
        assert ours is not None and ours.is_file()
        assert ours.resolve() != lock.resolve()
        gui = json.loads(lock.read_text(encoding="utf-8"))
        assert gui["gui"] is True
        assert gui["pid"] == child
        assert process_start_token(gui["pid"]) is not None
        mark_clean_exit(force=True)
        assert not ours.is_file()
        assert lock.is_file()
    finally:
        _stop_gui_lock(proc, lock)


def test_windows_exit_code_required_for_a_live_process():
    """An exited process still answers GetProcessTimes while a handle is open."""

    import ctypes
    from ctypes import wintypes

    from panelcad.diag.crash import STILL_ACTIVE, _windows_process_start

    class Kernel:
        def __init__(
            self,
            *,
            exit_code: int = STILL_ACTIVE,
            opened: int = 1,
            exit_ok: bool = True,
            times_ok: bool = True,
        ) -> None:
            self.exit_code = exit_code
            self.opened = opened
            self.exit_ok = exit_ok
            self.times_ok = times_ok
            self.times_calls = 0
            self.closed = 0

        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            return self.opened

        def GetExitCodeProcess(self, handle: int, code: object) -> int:
            if not self.exit_ok:
                return 0
            ctypes.cast(code, ctypes.POINTER(wintypes.DWORD)).contents.value = self.exit_code
            return 1

        def GetProcessTimes(
            self,
            handle: int,
            created: object,
            exited: object,
            kernel: object,
            user: object,
        ) -> int:
            self.times_calls += 1
            if not self.times_ok:
                return 0
            stamp = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
            stamp.dwLowDateTime = 0x11
            stamp.dwHighDateTime = 0x22
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.closed += 1
            return 1

    dead = Kernel(exit_code=0)
    assert _windows_process_start(4242, dead) is None
    assert dead.times_calls == 0
    assert dead.closed == 1

    live = Kernel()
    assert _windows_process_start(4242, live) == str((0x22 << 32) | 0x11)
    assert live.times_calls == 1
    assert live.closed == 1

    missing = Kernel(opened=0)
    assert _windows_process_start(4242, missing) is None
    assert missing.closed == 0

    failed = Kernel(exit_ok=False)
    assert _windows_process_start(4242, failed) is None
    assert failed.times_calls == 0
    assert failed.closed == 1


def test_reopen_recovery_saves_the_original_project(tmp_path):
    from panelcad.api.activity import journal_path
    from panelcad.io.yaml_format import save_project

    configure_logging()
    begin_session(gui=True)
    session = Session.create("NEMA seal-in")
    original = tmp_path / "NEMA seal-in.pcad.yaml"
    session.save(original)
    before = original.read_bytes()
    session.project.name = "Recovered seal-in"
    folder = recovery_dir()
    folder.mkdir(parents=True, exist_ok=True)
    recovery = folder / "recovery-nema.pcad.yaml"
    save_project(session.project, recovery)
    assert original.read_bytes() == before
    assert journal_path(recovery) is None

    opened = Session.open_recovery(recovery, original)
    assert opened.path == original
    assert opened.recovered is True
    assert opened.project.name == "Recovered seal-in"
    copied = autosave_now()
    assert copied is not None and copied.parent == folder
    assert copied.resolve() != original.resolve()
    assert original.read_bytes() == before
    lock = json.loads(current_lock_path().read_text(encoding="utf-8"))
    assert lock["project_path"] == str(original)

    app = QApplication.instance() or QApplication([])
    window = MainWindow(opened)
    window.show()
    app.processEvents()
    title = window.windowTitle()
    assert "recovered" in title
    assert str(original) in title
    assert str(recovery) not in title
    recovery_bytes = recovery.read_bytes()
    window.save_project()
    app.processEvents()
    assert "recovered" not in window.windowTitle()
    assert "Recovered seal-in" in original.read_text(encoding="utf-8")
    assert recovery.read_bytes() == recovery_bytes
    assert list(folder.glob("*.ai.jsonl")) == []
    assert list(folder.glob("*.ai-in.jsonl")) == []
    assert original.with_name("NEMA seal-in.pcad.ai.jsonl").is_file()
    window.close()

    direct = Session.open(recovery)
    direct.save()
    assert list(folder.glob("*.ai.jsonl")) == []

    bare = Session.open_recovery(recovery, None)
    assert bare.path is None and bare.recovered is True
    assert Session.open_recovery(recovery, recovery).path is None
    assert Session.open_recovery(recovery, "  ").path is None
    window2 = MainWindow(bare)
    window2.show()
    app.processEvents()
    assert "recovered" in window2.windowTitle()
    assert str(recovery) not in window2.windowTitle()
    called: dict[str, bool] = {}
    window2.save_project_as = lambda: called.setdefault("as", True)
    window2.save_project()
    assert called["as"] is True
    window2.close()

    orphan = folder / "recovery-gone.pcad.ai.jsonl"
    orphan.write_text("{}\n", encoding="utf-8")
    cursor = folder / "recovery-nema.pcad.ai-in.cursor"
    cursor.write_text("0", encoding="utf-8")
    prompt = folder / "recovery-nema.pcad.ai-prompt.jsonl"
    prompt.write_text("{}\n", encoding="utf-8")
    removed = {path.name for path in prune_recovery_files()}
    assert not orphan.exists()
    assert not cursor.exists()
    assert not prompt.exists()
    assert recovery.is_file()
    assert orphan.name in removed

    holder = type("Holder", (), {})()

    def _open(path: Path, project_path: str | None = None) -> None:
        holder.args = (Path(path), project_path)

    holder.open_recovery = _open
    info = UncleanExit(
        reasons=["session"],
        recovery_path=str(recovery),
        project_path=str(original),
        project_name="NEMA seal-in",
    )
    dialog = RecoveryDialog(None, Session.create("Prompt"), info)
    dialog._window = holder
    dialog._reopen()
    assert holder.args == (recovery, str(original))
    mark_clean_exit(force=True)


def test_report_dialog_shrinks_when_preview_closes(monkeypatch):
    area = QRect(0, 0, 1920, 1080)
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.screen_work_area",
        lambda widget=None: QRect(area),
    )
    app = QApplication.instance() or QApplication([])
    bug = BugReportDialog(
        None,
        Session.create("Shrink"),
        what="The coil vanished\n" + ("detail line\n" * 12),
        steps="1. Open the job\n" * 6,
    )
    bug.show()
    app.processEvents()
    collapsed = bug.height()
    assert bug.frameGeometry().bottom() <= area.bottom()
    bug.show_preview.setChecked(True)
    app.processEvents()
    expanded = bug.height()
    assert expanded > collapsed + 200
    assert bug.frameGeometry().bottom() <= area.bottom()
    bug.show_preview.setChecked(False)
    for _ in range(8):
        app.processEvents()
    assert bug.preview.isVisible() is False
    assert bug.height() <= collapsed + 24
    assert bug.height() < expanded - 200
    frame = bug.frameGeometry()
    assert frame.top() >= area.top()
    assert frame.bottom() <= area.bottom()
    assert frame.left() >= area.left()
    assert frame.right() <= area.right()
    bug.close()


def test_dialog_height_follows_wrapped_text(monkeypatch):
    from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

    from panelcad.gui.bug_report_dialog import clamp_dialog

    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.screen_work_area",
        lambda widget=None: QRect(0, 0, 1920, 1080),
    )
    app = QApplication.instance() or QApplication([])
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("word " * 200))
    label = layout.itemAt(0).widget()
    assert isinstance(label, QLabel)
    label.setWordWrap(True)
    hint = dialog.sizeHint().height()
    needed = layout.totalHeightForWidth(400)
    assert needed > hint
    clamp_dialog(dialog, width=400)
    dialog.show()
    app.processEvents()
    assert dialog.width() <= 400
    assert dialog.height() >= needed
    assert dialog.height() <= 1080 - 48
    dialog.close()


def test_settings_autosave_and_test_report_button(monkeypatch):
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.QMessageBox.information",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.screen_work_area",
        lambda widget=None: QRect(0, 0, 1920, 1080),
    )
    app = QApplication.instance() or QApplication([])
    settings = SettingsDialog(None, Session.create("Prefs"))
    settings.show()
    app.processEvents()
    assert settings.test_btn.isEnabled() is False
    assert settings.test_btn.toolTip() == "Add a webhook, or a bot token and a channel id."
    settings.webhook.setText("https://hooks.slack.com/services/T/B/x")
    assert settings.test_btn.isEnabled() is True
    settings.webhook.clear()
    settings.token.setText("xoxb-secret")
    assert settings.test_btn.isEnabled() is False
    settings.channel.setText("C0TESTBUGS01")
    assert settings.test_btn.isEnabled() is True
    settings.token.clear()
    settings.channel.clear()
    assert settings.test_btn.isEnabled() is False
    settings.autosave.setValue(15)
    settings._save()
    stored = json.loads(settings_path().read_text(encoding="utf-8"))
    assert stored["autosave_seconds"] == 15
    assert stored["bug_reports"]["webhook_url"] == ""
    settings.close()


def test_autosave_interval_round_trip(monkeypatch):
    from typer.testing import CliRunner

    from panelcad.cli.agent import SETTINGS_SCHEMA
    from panelcad.diag.settings import autosave_seconds, save_settings

    session = Session.create("Interval")
    assert session.app_settings() == {"autosave_seconds": 300}
    assert session.set_autosave_seconds(-4)["autosave_seconds"] == 0
    save_settings(BugSettings(webhook_url="https://hooks.slack.com/services/T/B/kept"))
    assert session.set_autosave_seconds(45)["autosave_seconds"] == 45
    stored = json.loads(settings_path().read_text(encoding="utf-8"))
    assert stored["autosave_seconds"] == 45
    assert stored["bug_reports"]["webhook_url"].endswith("/kept")
    agent = Agent()
    read = agent.handle({"id": "s1", "op": "settings"})
    assert read["ok"] is True
    assert read["result"]["autosave_seconds"] == 45
    wrote = agent.handle({"id": "s2", "op": "settings", "autosave_seconds": 12})
    assert wrote["ok"] is True
    assert wrote["result"]["autosave_seconds"] == 12
    help_row = agent.handle({"id": "h", "op": "help"})
    assert "settings" in help_row["result"]["ops"]
    assert "autosave_seconds" in help_row["result"]["help"]["settings"]
    assert help_row["result"]["schemas"]["settings"] == SETTINGS_SCHEMA
    runner = CliRunner()
    shown = runner.invoke(app, ["settings"])
    assert shown.exit_code == 0, shown.output
    assert "autosave_seconds=12" in shown.stdout
    changed = runner.invoke(app, ["settings", "--autosave-seconds", "0"])
    assert changed.exit_code == 0, changed.output
    assert "autosave_seconds=0" in changed.stdout
    monkeypatch.setenv("PANELCAD_AUTOSAVE_SECONDS", "9")
    assert autosave_seconds() == 9
    overridden = runner.invoke(app, ["settings"])
    assert overridden.exit_code == 0
    assert "autosave_seconds=9" in overridden.stdout


def _use_bot(monkeypatch, tmp_path: Path, token: str, channel: str = "C0TESTBUGS01") -> Path:
    path = tmp_path / "_report_bot.txt"
    body = token + "\n"
    if channel:
        body += channel + "\n"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr("panelcad.diag.endpoint.bot_path", lambda: path)
    return path


def _use_endpoint(monkeypatch, tmp_path: Path, url: str) -> Path:
    path = tmp_path / "_report_endpoint.txt"
    path.write_text(url + "\n", encoding="utf-8")
    monkeypatch.setattr("panelcad.diag.endpoint.endpoint_path", lambda: path)
    return path


def test_embedded_default_sends_and_says_so(tmp_path, monkeypatch, http_server):
    """No env and no settings: the embedded webhook is enough to send."""

    port = http_server.server_address[1]
    url = f"http://127.0.0.1:{port}/services/default"
    _use_endpoint(monkeypatch, tmp_path, url)
    configure_logging()
    logging.getLogger("panelcad").info("leak %s", url)
    session = Session.create("Embedded")
    sent = session.report_bug("Embedded title", f"saw {url}", send=True)
    assert sent["sent"] is True, sent
    assert sent["send"]["message"] == (
        f"Sent to the built-in bug-report channel. Zip saved at {sent['path']}"
    )
    assert url not in sent["send"]["message"]
    hook = next(hit for hit in http_server.hits if hit["path"] == "/services/default")
    posted = json.loads(hook["body"])
    assert url not in posted["text"]
    assert "What happened\nsaw [redacted]" in posted["text"]
    assert posted["text"].splitlines()[0] == "*Embedded title*"
    assert f"version: {__version__}" in posted["text"]
    assert f"zip: {Path(sent['path']).name}" in posted["text"]
    assert posted["zip"] == Path(sent["path"]).name
    assert posted["version"] == __version__
    assert posted["machine"]
    assert "127.0.0.1" not in posted["text"]
    blob = Path(sent["path"]).read_bytes()
    assert url.encode() not in blob
    side = json.loads(Path(sent["status_path"]).read_text(encoding="utf-8"))
    assert side == {
        "status": "sent",
        "reporter": "webhook",
        "time": side["time"],
        "error": "",
    }
    assert side["time"].endswith("Z")
    log_text = log_file_path().read_text(encoding="utf-8")
    assert url not in log_text
    assert "127.0.0.1" not in log_text

    app = QApplication.instance() or QApplication([])
    notices: list[str] = []

    def _notice(*args, **kwargs):
        del kwargs
        notices.append(str(args[2]) if len(args) > 2 else "")

    monkeypatch.setattr("panelcad.gui.bug_report_dialog.QMessageBox.information", _notice)
    monkeypatch.setattr("panelcad.gui.bug_report_dialog.QMessageBox.warning", _notice)
    dialog = BugReportDialog(None, session, what=f"dialog saw {url}")
    dialog.shot.setChecked(False)
    dialog.show()
    app.processEvents()
    assert dialog.send_btn.isEnabled()
    assert dialog.findChild(QLabel, "bugDestination").text() == "Reports go to: PanelCAD team"
    assert url not in dialog.preview.toPlainText()
    dialog.send_btn.click()
    app.processEvents()
    assert notices
    assert notices[-1].startswith("Sent to the built-in bug-report channel.")
    assert url not in notices[-1]
    dialog.close()


def test_unconfigured_send_shows_saved_only(monkeypatch):
    session = Session.create("Nowhere")
    result = session.report_bug("Nowhere", "only local", send=True)
    assert result["sent"] is False
    assert result["send"]["message"] == (
        f"Not sent: Bug reports are not configured. Saved only at {result['path']}"
    )
    assert Path(result["path"]).is_file()
    side = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
    assert side["status"] == "saved_only"
    assert side["reporter"] == ""
    assert side["error"]

    app = QApplication.instance() or QApplication([])
    notices: list[str] = []

    def _notice(*args, **kwargs):
        del kwargs
        notices.append(str(args[2]) if len(args) > 2 else "")

    monkeypatch.setattr("panelcad.gui.bug_report_dialog.QMessageBox.warning", _notice)
    dialog = BugReportDialog(None, session, what="The coil stayed local")
    dialog.shot.setChecked(False)
    dialog.show()
    app.processEvents()
    assert dialog.send_btn.isEnabled()
    assert dialog.findChild(QLabel, "bugDestination").text() == (
        "Sending is not set up on this PC. Click Save and email the file to the office."
    )
    dialog.send_btn.click()
    app.processEvents()
    assert notices[-1].startswith("Not sent:")
    assert "Saved only at" in notices[-1]
    assert "not configured" in notices[-1].lower()
    dialog.close()


def test_endpoint_precedence_and_off_switch(tmp_path, monkeypatch, http_server):
    from panelcad.diag.settings import save_settings

    port = http_server.server_address[1]
    default_url = f"http://127.0.0.1:{port}/services/default"
    settings_url = f"http://127.0.0.1:{port}/services/settings"
    env_url = f"http://127.0.0.1:{port}/services/env"
    _use_endpoint(monkeypatch, tmp_path, default_url)
    save_settings(BugSettings(webhook_url=settings_url))
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", env_url)
    session = Session.create("Order")

    env_sent = session.report_bug("From env", "body", send=True)
    assert env_sent["sent"] is True
    assert [hit["path"] for hit in http_server.hits] == ["/services/env"]
    assert env_sent["send"]["message"].startswith("Sent.")
    assert "PanelCAD team" not in env_sent["send"]["message"]

    http_server.hits.clear()
    monkeypatch.delenv("PANELCAD_BUG_WEBHOOK", raising=False)
    settings_sent = session.report_bug("From settings", "body", send=True)
    assert settings_sent["sent"] is True
    assert [hit["path"] for hit in http_server.hits] == ["/services/settings"]

    http_server.hits.clear()
    save_settings(BugSettings())
    default_sent = session.report_bug("From default", "body", send=True)
    assert default_sent["sent"] is True
    assert [hit["path"] for hit in http_server.hits] == ["/services/default"]
    assert default_sent["send"]["destination"] == "PanelCAD team"
    assert default_sent["send"]["message"].startswith("Sent to the built-in bug-report channel")

    http_server.hits.clear()
    save_settings(BugSettings(webhook_url=settings_url, disabled=True))
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", env_url)
    turned_off = session.report_bug("Off", "body", send=True)
    assert http_server.hits == []
    assert turned_off["sent"] is False
    assert turned_off["send"]["message"] == "Saved only (reporting turned off)"
    off_side = json.loads(Path(turned_off["status_path"]).read_text(encoding="utf-8"))
    assert off_side["status"] == "saved_only"

    http_server.hits.clear()
    monkeypatch.delenv("PANELCAD_BUG_WEBHOOK", raising=False)
    save_settings(BugSettings(webhook_url=settings_url, disabled=False))
    monkeypatch.setenv("PANELCAD_BUG_REPORTS", "off")
    env_off = session.report_bug("Env off", "body", send=True)
    assert http_server.hits == []
    assert env_off["send"]["message"] == "Saved only (reporting turned off)"

    monkeypatch.delenv("PANELCAD_BUG_REPORTS", raising=False)
    monkeypatch.setenv("PANELCAD_BUG_RELAY", f"http://127.0.0.1:{port}/services/relay")
    relayed = session.report_bug("Relay", "body", send=True)
    assert relayed["sent"] is True
    assert [hit["path"] for hit in http_server.hits] == ["/services/relay"]
    assert relayed["send"]["destination"] == "your webhook"
    assert "127.0.0.1" not in relayed["send"]["message"]


def test_webhook_errors_keep_the_zip_and_hide_the_url(monkeypatch):
    secret = "https://hooks.slack.com/services/T00/B00/secretvalue"
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", secret)
    session = Session.create("Http fail")

    class Scripted:
        def __init__(self, status) -> None:
            self.status = status

        def request(self, method, url, *, headers, body, timeout=30.0):
            del method, url, headers, body, timeout
            if self.status == "timeout":
                raise TimeoutError("timed out")
            return self.status, b"nope"

    for status, needle in ((400, "HTTP 400"), (503, "HTTP 503"), ("timeout", "timed out")):
        result = session.report_bug("Nope", "Did not send", send=True, transport=Scripted(status))
        assert result["sent"] is False
        assert result["send"]["message"].startswith("Not sent:")
        assert needle in result["send"]["message"]
        assert result["path"] in result["send"]["message"]
        assert "Saved only at" in result["send"]["message"]
        assert secret not in result["send"]["message"]
        assert "hooks.slack.com" not in result["send"]["message"]
        assert Path(result["path"]).is_file()
        side = json.loads(Path(result["status_path"]).read_text(encoding="utf-8"))
        assert side["status"] == "saved_only"
        assert side["reporter"] == "webhook"
        assert secret not in side["error"]
        assert "hooks.slack.com" not in side["error"]


def test_send_rate_limit(http_server, monkeypatch):
    from panelcad.diag.reporters import SEND_LIMIT, SEND_WINDOW_SECONDS, rate_limit_path

    port = http_server.server_address[1]
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", f"http://127.0.0.1:{port}/services/hook")
    session = Session.create("Rate")
    for index in range(SEND_LIMIT):
        result = session.report_bug(f"Rate {index}", "body", send=True)
        assert result["sent"] is True, result
    blocked = session.report_bug("Blocked", "body", send=True)
    assert blocked["sent"] is False
    assert "rate limit" in blocked["send"]["message"]
    assert blocked["send"]["message"].startswith("Not sent:")
    assert Path(blocked["path"]).is_file()
    assert len(http_server.hits) == SEND_LIMIT
    side = json.loads(Path(blocked["status_path"]).read_text(encoding="utf-8"))
    assert side["status"] == "saved_only"
    assert "rate limit" in side["error"]
    stale = time.time() - SEND_WINDOW_SECONDS - 5
    rate_limit_path().write_text(json.dumps({"times": [stale] * SEND_LIMIT}) + "\n", encoding="utf-8")
    again = session.report_bug("After window", "body", send=True)
    assert again["sent"] is True


def test_slack_summary_is_capped_and_names_the_zip():
    import hashlib
    import platform

    from panelcad.diag.reporters import SLACK_TEXT_LIMIT, slack_document
    from panelcad.diag.report import machine_name_hash

    document = slack_document("m" * (SLACK_TEXT_LIMIT + 80), Path("bug-capped.zip"))
    assert len(document["text"]) <= SLACK_TEXT_LIMIT
    assert document["zip"] == "bug-capped.zip"
    assert document["version"] == __version__
    assert "zip: bug-capped.zip" in document["text"]
    name = platform.node().strip() or "unknown"
    assert document["machine"] == hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    assert document["machine"] == machine_name_hash()
    assert document["machine"] != name


def test_embedded_url_never_appears_in_zip_log_or_dialog(tmp_path, monkeypatch):
    url = "http://127.0.0.1:9/services/embedded-secret"
    _use_endpoint(monkeypatch, tmp_path, url)
    assert url not in redact_text(f"posted {url}")
    assert "embedded-secret" not in redact_text(f"posted {url}")
    configure_logging()
    logging.getLogger("panelcad").info("see %s", url)
    assert url not in log_file_path().read_text(encoding="utf-8")
    session = Session.create("Hide")
    result = session.report_bug("Hide", f"details {url}", send=False)
    assert url.encode() not in Path(result["path"]).read_bytes()
    assert url not in Path(result["status_path"]).read_text(encoding="utf-8")
    app = QApplication.instance() or QApplication([])
    dialog = BugReportDialog(None, session, what=f"details {url}")
    dialog.show()
    app.processEvents()
    assert url not in dialog.preview.toPlainText()
    assert url not in dialog.findChild(QLabel, "bugDestination").text()
    dialog.close()


def test_settings_shows_builtin_channel(tmp_path, monkeypatch):
    url = "http://127.0.0.1:9/services/builtin"
    _use_endpoint(monkeypatch, tmp_path, url)
    from PySide6.QtWidgets import QTabWidget

    app = QApplication.instance() or QApplication([])
    settings = SettingsDialog(None, Session.create("Builtin"))
    tabs = settings.findChild(QTabWidget)
    for index in range(tabs.count()):
        if tabs.tabText(index) == "Bug reports":
            tabs.setCurrentIndex(index)
    settings.show()
    app.processEvents()
    assert settings.builtin.text() == "Using built-in PanelCAD team channel"
    assert settings.builtin.isVisible()
    assert url not in settings.builtin.text()
    assert settings.test_btn.isEnabled()
    assert settings.test_btn.text() == "Send test report"
    settings.webhook.setText("https://hooks.slack.com/services/T/B/mine")
    app.processEvents()
    assert settings.builtin.isVisible() is False
    settings.webhook.clear()
    app.processEvents()
    assert settings.builtin.isVisible()
    settings.reports_off.setChecked(True)
    app.processEvents()
    assert settings.builtin.isVisible() is False
    assert settings.test_btn.isEnabled() is False
    assert settings.test_btn.toolTip() == "Bug reports are turned off."
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.QMessageBox.information",
        lambda *args, **kwargs: None,
    )
    settings._save()
    stored = json.loads(settings_path().read_text(encoding="utf-8"))
    assert stored["bug_reports"]["disabled"] is True
    assert stored["bug_reports"]["webhook_url"] == ""
    assert url not in settings_path().read_text(encoding="utf-8")
    settings.close()


def test_default_webhook_file_ignores_a_token(tmp_path, monkeypatch):
    from panelcad.diag.endpoint import classify_release_webhook, default_webhook_url

    path = _use_endpoint(monkeypatch, tmp_path, "xoxb-FAKE-not-a-webhook")
    assert default_webhook_url() == ""
    path.write_text("https://hooks.slack.com/services/T123/B456/abc\n", encoding="utf-8")
    assert default_webhook_url() == "https://hooks.slack.com/services/T123/B456/abc"
    assert classify_release_webhook("xoxb-FAKE-SECRET") == "bot token"
    assert "SECRET" not in classify_release_webhook("xoxb-FAKE-SECRET")
    assert classify_release_webhook("https://example.com/hook") != ""
    assert classify_release_webhook("https://hooks.slack.com/services/T123/B456/abc") == ""


_SECRET_WEBHOOK = re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]{8}")
_CHANNEL_ID_IN_TREE = re.compile(r"\bC0[A-Z0-9]{8,}\b")
_SKIP_SCAN_DIRS = {
    ".git",
    ".venv",
    ".venvs",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".sharpen",
    ".grok",
    "dist",
    "reference",
}


def _secret_hits(text: str) -> list[str]:
    """Real-looking webhook URLs and bot tokens. Placeholder ellipsis is allowed."""

    hits: list[str] = []
    for line in text.splitlines():
        if _SECRET_WEBHOOK.search(line):
            hits.append(line.strip()[:160])
            continue
        cleaned = line.replace("xoxb-…", "").replace("xoxb-...", "")
        if "xoxb-" in cleaned:
            hits.append(cleaned.strip()[:160])
        if _CHANNEL_ID_IN_TREE.search(line):
            hits.append("slack channel id")
    return hits


def _scan_tree(root: Path) -> list[str]:
    found: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if parts & _SKIP_SCAN_DIRS or "tests" in path.relative_to(root).parts:
            continue
        if path.suffix.lower() in {".png", ".pyc", ".zip", ".so", ".dll", ".exe"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for hit in _secret_hits(text):
            found.append(f"{path.relative_to(root)}: {hit}")
    return found


def _scan_zip(archive: Path) -> list[str]:
    found: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if "/tests/" in name or name.endswith("/"):
                continue
            text = zf.read(name).decode("utf-8", errors="ignore")
            for hit in _secret_hits(text):
                found.append(f"{name}: {hit}")
    return found


def _load_build_release():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "build-release.py"
    spec = importlib.util.spec_from_file_location("panelcad_build_release_bug", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_release_webhook_is_embedded_only_from_the_env(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    build = _load_build_release()
    monkeypatch.delenv("PANELCAD_RELEASE_BUG_WEBHOOK", raising=False)
    tree_hits = _scan_tree(root)
    assert tree_hits == [], tree_hits

    plain, _checksum = build.build_release(root, tmp_path / "plain")
    warning = capsys.readouterr().err
    assert "WARNING" in warning
    assert "PANELCAD_RELEASE_BUG_WEBHOOK" in warning
    assert "PANELCAD_RELEASE_SLACK_BOT_TOKEN" in warning
    with zipfile.ZipFile(plain) as zf:
        assert not any(name.endswith("_report_endpoint.txt") for name in zf.namelist())
        assert not any(name.endswith("_report_bot.txt") for name in zf.namelist())
    assert _scan_zip(plain) == []

    leftover = root / "src" / "panelcad" / "diag" / "_report_endpoint.txt"
    leftover.write_text("https://hooks.slack.com/services/T123/B456/leftover\n", encoding="utf-8")
    try:
        skipped, _ = build.build_release(root, tmp_path / "skip")
        capsys.readouterr()
        with zipfile.ZipFile(skipped) as zf:
            assert not any(name.endswith("_report_endpoint.txt") for name in zf.namelist())
    finally:
        leftover.unlink(missing_ok=True)
    assert not leftover.exists()

    hook = "https://hooks.slack.com/services/T123/B456/placeholder"
    monkeypatch.setenv("PANELCAD_RELEASE_BUG_WEBHOOK", hook)
    embedded, _ = build.build_release(root, tmp_path / "with")
    capsys.readouterr()
    assert not leftover.exists()
    rel = f"PanelCAD-{__version__}/src/panelcad/diag/_report_endpoint.txt"
    with zipfile.ZipFile(embedded) as zf:
        assert zf.read(rel).decode("utf-8").strip() == hook
    assert _scan_zip(embedded) == []
    assert _scan_tree(root) == []

    monkeypatch.setenv("PANELCAD_RELEASE_BUG_WEBHOOK", "xoxb-FAKE-SUPERSECRETTOKEN")
    with pytest.raises(SystemExit) as token_refused:
        build.build_release(root, tmp_path / "token")
    assert "SUPERSECRETTOKEN" not in str(token_refused.value)
    assert "bot token" in str(token_refused.value)

    monkeypatch.setenv("PANELCAD_RELEASE_BUG_WEBHOOK", "https://example.com/services/hook")
    with pytest.raises(SystemExit) as host_refused:
        build.build_release(root, tmp_path / "host")
    assert "example.com" not in str(host_refused.value)
    assert "not a https://hooks.slack.com/services/ URL" in str(host_refused.value)


def test_version_and_path_lines_survive_redaction(tmp_path, monkeypatch):
    """Version lines and path lines stay readable when secrets in the same text are removed."""

    token = "xoxb-fake-redact-token-123456"
    hook = "https://hooks.slack.com/services/T00/B00/secretvalue"
    _use_bot(monkeypatch, tmp_path, token)
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", hook)
    home = Path.home()
    text = "\n".join(
        [
            f"version: {__version__}",
            f"version={__version__}",
            f"saved: {home / 'PanelCAD' / 'reports' / 'bug-1.zip'}",
            f"install={home / 'PanelCAD'}",
            f"note {token} {hook}",
        ]
    )
    out = redact_text(text)
    folded = out.replace("\\", "/")
    assert f"version: {__version__}" in out
    assert f"version={__version__}" in out
    assert "bug-1.zip" in out
    assert "saved: ~/" in folded
    assert "install=~/" in folded
    assert str(home) not in out
    assert token not in out
    assert hook not in out
    assert "hooks.slack.com" not in out


def test_embedded_bot_token_resolution_ignores_channel_override(tmp_path, monkeypatch):
    from panelcad.diag.endpoint import default_bot_token, embedded_channel_id

    token = "xoxb-fake-embedded-resolve-123456"
    path = _use_bot(monkeypatch, tmp_path, "https://hooks.slack.com/services/T/B/not-a-token", channel="")
    assert default_bot_token() == ""
    path.write_text(token + "\n", encoding="utf-8")
    assert default_bot_token() == ""
    path.write_text(token + "\nC0TESTBUGS01\n", encoding="utf-8")
    assert default_bot_token() == token
    assert embedded_channel_id() == "C0TESTBUGS01"

    pinned = BugSettings(slack_channel="C999OVERRIDE").with_env()
    assert pinned.slack_bot_token == token
    assert pinned.slack_channel == "C0TESTBUGS01"
    assert pinned.token_source == "default"
    assert pinned.destination_name() == "PanelCAD team"

    monkeypatch.setenv("PANELCAD_SLACK_CHANNEL", "C888OVERRIDE")
    still_pinned = BugSettings().with_env()
    assert still_pinned.slack_bot_token == token
    assert still_pinned.slack_channel == "C0TESTBUGS01"

    monkeypatch.setenv("PANELCAD_SLACK_BOT_TOKEN", "xoxb-fake-env-token-123456")
    monkeypatch.setenv("PANELCAD_SLACK_CHANNEL", "CTESTENVCH01")
    from_env = BugSettings(
        slack_bot_token="xoxb-fake-settings-token-123456",
        slack_channel="CTESTSETTINGS",
    ).with_env()
    assert from_env.slack_bot_token == "xoxb-fake-env-token-123456"
    assert from_env.slack_channel == "CTESTENVCH01"
    assert from_env.token_source == "env"
    assert from_env.destination_name() == "Slack channel CTESTENVCH01 (bot)"

    monkeypatch.delenv("PANELCAD_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PANELCAD_SLACK_CHANNEL", raising=False)
    from_settings = BugSettings(
        slack_bot_token="xoxb-fake-settings-token-123456",
        slack_channel="CTESTSETTINGS",
    ).with_env()
    assert from_settings.token_source == "settings"
    assert from_settings.slack_bot_token == "xoxb-fake-settings-token-123456"
    assert from_settings.slack_channel == "CTESTSETTINGS"


def test_embedded_bot_uploads_screenshot_and_zip(http_server, monkeypatch, tmp_path):
    """The release bot uploads the image and the zip. A channel override does not move it."""

    token = "xoxb-fake-embedded-upload-123456"
    _use_bot(monkeypatch, tmp_path, token)
    port = http_server.server_address[1]
    _use_endpoint(monkeypatch, tmp_path, f"http://127.0.0.1:{port}/services/default")
    monkeypatch.setenv("PANELCAD_SLACK_CHANNEL", "C888OVERRIDE")
    configure_logging()
    logging.getLogger("panelcad").info("leak-token %s", token)
    set_slack_api_base(f"http://127.0.0.1:{port}/api")
    png = b"\x89PNG\r\n\x1a\n" + b"embedded-screenshot"
    home_file = Path.home() / "drawings" / "job.pcad.yaml"
    session = Session.create("Embedded bot")
    sent = session.report_bug(
        "Embedded bot",
        f"saw the token {token} at {home_file}",
        include_screenshot=True,
        screenshot_png=png,
        send=True,
    )
    assert sent["sent"] is True, sent
    assert sent["send"]["uploaded"] is True
    assert sent["send"]["destination"] == "PanelCAD team"
    assert [row["reporter"] for row in sent["send"]["results"]] == ["slack-bot"]
    assert sent["send"]["message"].startswith("Sent to the built-in bug-report channel")
    paths = [hit["path"] for hit in http_server.hits]
    assert paths == [
        "/api/files.getUploadURLExternal",
        "/upload",
        "/api/files.getUploadURLExternal",
        "/upload",
        "/api/files.completeUploadExternal",
    ]
    assert http_server.hits[0]["headers"]["Authorization"] == f"Bearer {token}"
    assert b"filename=screenshot.png" in http_server.hits[0]["body"]
    assert http_server.hits[1]["body"] == png
    assert Path(sent["path"]).name.encode() in http_server.hits[2]["body"]
    finished = json.loads(http_server.hits[4]["body"])
    assert finished["channel_id"] == "C0TESTBUGS01"
    assert [item["title"] for item in finished["files"]] == ["screenshot.png", Path(sent["path"]).name]
    comment = finished["initial_comment"]
    assert f"version: {__version__}" in comment
    assert "job.pcad.yaml" in comment
    assert "What happened\nsaw the token [redacted]" in comment
    assert str(Path.home()) not in comment
    assert token not in comment
    assert "C999OVERRIDE" not in json.dumps(finished)
    assert "C888OVERRIDE" not in json.dumps(finished)
    blob = Path(sent["path"]).read_bytes()
    assert token.encode() not in blob
    with zipfile.ZipFile(sent["path"]) as archive:
        report_raw = archive.read("report.json").decode("utf-8")
        report = json.loads(report_raw)
        assert token not in report_raw
        assert report["version"] == __version__
        assert "job.pcad.yaml" in report["description"]
        log_tail = archive.read("log.txt").decode("utf-8")
        assert "leak-token" in log_tail
        assert token not in log_tail
        for name in archive.namelist():
            assert token.encode() not in archive.read(name)
    assert token not in log_file_path().read_text(encoding="utf-8")
    assert token not in Path(sent["status_path"]).read_text(encoding="utf-8")


def test_bad_embedded_token_falls_back_to_webhook_text(http_server, monkeypatch, tmp_path):
    """A rejected embedded bot still posts the report text through the webhook."""

    http_server.fail_uploads = True
    token = "xoxb-fake-rejected-token-123456"
    _use_bot(monkeypatch, tmp_path, token)
    port = http_server.server_address[1]
    _use_endpoint(monkeypatch, tmp_path, f"http://127.0.0.1:{port}/services/default")
    configure_logging()
    logging.getLogger("panelcad").info("rejected %s", token)
    set_slack_api_base(f"http://127.0.0.1:{port}/api")
    session = Session.create("Rejected")
    sent = session.report_bug("Rejected token", f"still text {token}", send=True)
    assert sent["sent"] is True, sent
    assert sent["send"]["uploaded"] is False
    reporters = [row["reporter"] for row in sent["send"]["results"]]
    assert reporters == ["slack-bot", "webhook"]
    assert sent["send"]["results"][0]["ok"] is False
    assert sent["send"]["results"][1]["ok"] is True
    posted = json.loads(next(hit["body"] for hit in http_server.hits if hit["path"] == "/services/default"))
    text = posted["text"]
    assert "What happened\nstill text [redacted]" in text
    assert f"version: {__version__}" in text
    assert token not in text
    assert "The report zip was not uploaded." not in text
    assert token.encode() not in Path(sent["path"]).read_bytes()
    with zipfile.ZipFile(sent["path"]) as archive:
        assert token not in archive.read("report.json").decode("utf-8")
        assert token not in archive.read("log.txt").decode("utf-8")


def test_partial_upload_says_the_zip_was_not_uploaded(http_server, monkeypatch):
    """Screenshot shared, zip put failed: the webhook text says the zip was not uploaded."""

    http_server.fail_zip_put = True
    port = http_server.server_address[1]
    monkeypatch.setenv("PANELCAD_BUG_WEBHOOK", f"http://127.0.0.1:{port}/services/hook")
    token = "xoxb-fake-partial-token-123456"
    set_slack_api_base(f"http://127.0.0.1:{port}/api")
    png = b"\x89PNG\r\n\x1a\n" + b"partial-shot"
    session = Session.create("Partial")
    sent = session.report_bug(
        "Partial upload",
        "The sheet stopped.",
        include_screenshot=True,
        screenshot_png=png,
        send=True,
        settings=BugSettings(slack_bot_token=token, slack_channel="C0TESTBUGS01"),
    )
    assert sent["sent"] is True, sent
    reporters = [row["reporter"] for row in sent["send"]["results"]]
    assert reporters == ["slack-bot", "webhook"]
    assert sent["send"]["results"][0]["ok"] is False
    finished = json.loads(
        next(hit["body"] for hit in http_server.hits if hit["path"] == "/api/files.completeUploadExternal")
    )
    assert [item["title"] for item in finished["files"]] == ["screenshot.png"]
    posted = json.loads(next(hit["body"] for hit in http_server.hits if hit["path"] == "/services/hook"))
    assert posted["text"].startswith("The report zip was not uploaded.")
    assert "What happened\nThe sheet stopped." in posted["text"]
    assert f"version: {__version__}" in posted["text"]
    assert token not in posted["text"]


def test_long_report_text_is_a_thread_reply(http_server, monkeypatch):
    """A 40k-sized report is not stuffed into initial_comment. Slack gets it as a reply."""

    from panelcad.diag.reporters import INITIAL_COMMENT_LIMIT, SLACK_TEXT_LIMIT

    port = http_server.server_address[1]
    token = "xoxb-fake-thread-token-123456"
    set_slack_api_base(f"http://127.0.0.1:{port}/api")
    marker = "LONGREPORTMARKER"
    description = marker + (" panel" * 8000)
    assert len(description) > SLACK_TEXT_LIMIT
    session = Session.create("Long")
    sent = session.report_bug(
        "Long title",
        description,
        send=True,
        include_log=False,
        settings=BugSettings(slack_bot_token=token, slack_channel="C0TESTBUGS01"),
    )
    assert sent["sent"] is True, sent
    paths = [hit["path"] for hit in http_server.hits]
    assert paths.index("/api/files.completeUploadExternal") < paths.index("/api/chat.postMessage")
    finished = json.loads(
        next(hit["body"] for hit in http_server.hits if hit["path"] == "/api/files.completeUploadExternal")
    )
    assert len(finished["initial_comment"]) <= INITIAL_COMMENT_LIMIT
    assert marker not in finished["initial_comment"]
    assert "Full report is in the thread." in finished["initial_comment"]
    reply = json.loads(next(hit["body"] for hit in http_server.hits if hit["path"] == "/api/chat.postMessage"))
    assert reply["thread_ts"] == "1700000000.000100"
    assert reply["channel"] == "C0TESTBUGS01"
    assert marker in reply["text"]
    assert len(reply["text"]) > INITIAL_COMMENT_LIMIT
    assert len(reply["text"]) <= SLACK_TEXT_LIMIT
    assert token not in reply["text"]
    assert token not in finished["initial_comment"]


def test_release_bot_token_is_embedded_only_in_the_zip(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    build = _load_build_release()
    token = "xoxb-fake-release-bot-token"
    channel_leak = "xoxb-fake-channel-leak-123456"
    checkout = root / "src" / "panelcad" / "diag" / "_report_bot.txt"
    assert not checkout.exists()
    checkout.write_text("xoxb-fake-leftover-should-not-ship\n", encoding="utf-8")
    monkeypatch.setenv("PANELCAD_RELEASE_SLACK_BOT_TOKEN", token)
    monkeypatch.setenv("PANELCAD_RELEASE_SLACK_CHANNEL", channel_leak)
    try:
        with pytest.raises(SystemExit) as leaked:
            build.build_release(root, tmp_path / "bot-refused")
    finally:
        checkout.unlink(missing_ok=True)
    assert not checkout.exists()
    assert token not in str(leaked.value)
    assert channel_leak not in str(leaked.value)
    assert "bot token" in str(leaked.value)
    monkeypatch.setenv("PANELCAD_RELEASE_SLACK_CHANNEL", "C0TESTBUGS01")
    archive, _checksum = build.build_release(root, tmp_path / "bot")
    err = capsys.readouterr().err
    assert token not in err
    assert channel_leak not in err
    assert "C0TESTBUGS01" not in err
    rel = f"PanelCAD-{__version__}/src/panelcad/diag/_report_bot.txt"
    with zipfile.ZipFile(archive) as zf:
        names = [name for name in zf.namelist() if name.endswith("_report_bot.txt")]
        assert names == [rel]
        lines = zf.read(rel).decode("utf-8").splitlines()
    assert lines == [token, "C0TESTBUGS01"]
    with zipfile.ZipFile(archive) as zf:
        leaked = []
        for name in zf.namelist():
            if "/tests/" in name:
                continue
            data = zf.read(name)
            if channel_leak.encode() in data or b"leftover-should-not-ship" in data:
                leaked.append(name)
            if name != rel and token.encode() in data:
                leaked.append(name)
    assert leaked == []
    assert _scan_tree(root) == []

    monkeypatch.setenv("PANELCAD_RELEASE_BUG_WEBHOOK", "https://hooks.slack.com/services/T123/B456/placeholder")
    monkeypatch.setenv("PANELCAD_RELEASE_UPDATE_FEED", "https://updates.example/feed.json")
    public_zip, _public_sha = build.build_release(root, tmp_path / "public", public=True)
    public_err = capsys.readouterr().err
    assert "Public build" in public_err
    assert token not in public_err
    assert "C0TESTBUGS01" not in public_err
    with zipfile.ZipFile(public_zip) as zf:
        names = zf.namelist()
        assert not any(name.endswith(("_report_bot.txt", "_report_endpoint.txt", "_release_feed.txt")) for name in names)
        markers = [name for name in names if name.endswith("_release_public.txt")]
        assert len(markers) == 1
        assert zf.read(markers[0]) == b"public\n"

    monkeypatch.setenv("PANELCAD_RELEASE_SLACK_BOT_TOKEN", "xoxp-fake-user-token-123456")
    with pytest.raises(SystemExit) as refused:
        build.build_release(root, tmp_path / "refused")
    assert "xoxp-fake-user-token-123456" not in str(refused.value)
    assert "not a Slack bot token" in str(refused.value)
    assert not checkout.exists()
