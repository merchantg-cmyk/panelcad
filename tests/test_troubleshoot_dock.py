"""Troubleshoot dock: guided checks, sheet highlight, log export, and the user's AI."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from typer.testing import CliRunner

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.ai.troubleshoot_explain import explain_guide, explain_reason
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.core.troubleshoot import citation_errors, prose_violations
from panelcad.gui.canvas import TraceHalo, TraceWireMark
from panelcad.gui.main_window import MainWindow
from panelcad.settings import invalidate

ROOT = Path(__file__).resolve().parents[1]
SEAL = ROOT / "examples" / "xref_nema_seal_in.pcad.yaml"
BANNED = ("jumper", "bypass", "force", "defeat", "override")


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    for name in (
        "PANELCAD_AI_LOOKUP",
        "PANELCAD_AI_PROVIDER",
        "PANELCAD_AI_ENDPOINT",
        "PANELCAD_AI_BASE_URL",
        "PANELCAD_AI_API_KEY",
        "PANELCAD_AI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    invalidate()
    yield
    invalidate()


def _app() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def _clean(payload: dict) -> None:
    blob = json.dumps(payload).casefold()
    assert prose_violations(payload) == []
    for word in BANNED:
        assert word not in blob


def test_guide_starts_on_a_lockout_check_and_uses_the_drawing():
    session = Session.open(SEAL)
    payload = session.troubleshoot_guide("M1", "motor_wont_start")
    _clean(payload)
    assert payload["op"] == "troubleshoot_guide"
    assert payload["read_only"] is True
    assert payload["ref"] == session.circuit_for("M1")["ref"]
    assert "lockout" in payload["safety"]["loto"].casefold()
    assert payload["cross_refs"]
    assert payload["path"]
    current = payload["next_check"]
    assert current["energized"] == "DE-ENERGIZED"
    assert current["device"]
    assert payload["highlight"]["focus"] == current["device"]
    assert payload["highlight"]["sheet"] == current["sheet"]
    assert "Lock out" in payload["instruction"]
    wires = {wire.number for sheet in session.project.sheets for wire in sheet.wires}
    assert set(payload["highlight"]["wires"]) <= wires
    assert citation_errors(session.project, payload, session.catalog) == []


def test_wrong_reading_names_the_fault_between_the_last_ok_and_that_point():
    session = Session.open(SEAL)
    checks = session.checkpoints("M1", "motor_wont_start")["checks"]
    live = [row for row in checks if row["energized"] == "LIVE"]
    readings = [
        {"id": live[0]["id"], "result": "ok"},
        {"device": "PB1", "terminal": "2", "result": "ok"},
        {"wire": "103", "result": "wrong"},
    ]
    payload = session.troubleshoot_guide("M1", "motor won't start", readings)
    _clean(payload)
    assert "PB1" in payload["fault_text"]
    assert "PB2" in payload["fault_text"]
    assert "103" in payload["fault_text"]
    assert payload["next_role"] in {"fault", "gap"}
    if payload["next_role"] == "fault":
        assert payload["next_check"]["device"] == "PB2"
    else:
        wrong = next(row for row in payload["checks"] if row["device"] == "PB2" and row.get("wire") == "103")
        assert payload["next_check"]["order"] <= wrong["order"]
    assert {row["ref"] for row in payload["causes"]} == {"PB2"}
    assert payload["results"][live[0]["id"]] == "ok"


def test_explain_stays_off_without_the_users_account():
    reason = explain_reason()
    assert "Settings > AI" in reason
    assert "key" in reason.casefold()
    session = Session.open(SEAL)
    payload = session.troubleshoot_guide("M1", "motor_wont_start")
    result = explain_guide(session.project, session.catalog, payload)
    assert result["ok"] is False
    assert result["text"] == ""
    assert "Settings > AI" in result["reason"]


def test_explain_calls_only_the_configured_account_and_drops_a_bad_reply(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    monkeypatch.setenv("PANELCAD_AI_PROVIDER", "openai")
    monkeypatch.setenv("PANELCAD_AI_API_KEY", "sk-user-test")
    monkeypatch.setenv("PANELCAD_AI_MODEL", "gpt-test")
    session = Session.open(SEAL)
    payload = session.troubleshoot_guide("M1", "motor_wont_start")
    seen: dict[str, str] = {}

    def transport(req):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        body = json.loads(req.data.decode("utf-8"))
        assert "web_search" not in json.dumps(body)
        assert body["model"] == "gpt-test"
        assert "sk-user-test" not in json.dumps(body)
        seen["system"] = body["input"][0]["content"]

        class Response:
            def read(self) -> bytes:
                text = "Follow the checklist on the drawing. The next check is de-energized."
                return json.dumps({"output": [{"content": [{"text": text}]}]}).encode()

        return Response()

    result = explain_guide(session.project, session.catalog, payload, transport=transport)
    assert result["ok"] is True
    assert "checklist" in result["text"].casefold()
    assert seen["auth"] == "Bearer sk-user-test"
    assert "api.openai.com" in seen["url"]
    assert "jumper" in seen["system"].casefold()

    def bypass(req):
        del req

        class Response:
            def read(self) -> bytes:
                return json.dumps(
                    {"output": [{"content": [{"text": "Jumper the stop and force the output."}]}]}
                ).encode()

        return Response()

    dropped = explain_guide(session.project, session.catalog, payload, transport=bypass)
    assert dropped["ok"] is False
    assert dropped.get("discarded") is True
    assert dropped["text"] == ""
    assert "jumper" not in dropped["reason"].casefold()

    def invented(req):
        del req

        class Response:
            def read(self) -> bytes:
                return json.dumps(
                    {"output": [{"content": [{"text": "Measure wire 999 at M1 terminal Z9."}]}]}
                ).encode()

        return Response()

    cited = explain_guide(session.project, session.catalog, payload, transport=invented)
    assert cited["ok"] is False
    assert cited["text"] == ""


def test_explain_source_has_no_built_in_key():
    text = (ROOT / "src" / "panelcad" / "ai" / "troubleshoot_explain.py").read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "xai-" not in text
    assert "Bearer " in text  # the header is built from the saved key, not a literal token


def test_session_csv_and_pdf_keep_the_project_file(tmp_path: Path):
    _app()
    session = Session.open(SEAL)
    project = tmp_path / "seal.pcad.yaml"
    session.save(project)
    before = project.read_bytes()
    opened = Session.open(project)
    csv_path = opened.export_troubleshoot_csv(
        tmp_path / "log.csv",
        ref="M1",
        symptom="motor_wont_start",
        who="George",
        note="first pass",
    )
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert rows[0]["who"] == "George"
    assert rows[0]["ref"] == "M1"
    assert "lockout" in rows[0]["safety"].casefold()
    assert any(row["row_kind"] == "check" for row in rows)
    assert "\r\n" in csv_path.read_bytes().decode("utf-8-sig")
    pdf_path = opened.export_troubleshoot_pdf(
        tmp_path / "log.pdf",
        ref="M1",
        symptom="motor_wont_start",
        who="George",
    )
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    folded = " ".join(text.split()).casefold()
    assert "lockout" in folded or "qualified persons" in folded
    assert "nfpa 70e" in folded
    assert "m1" in folded
    for word in BANNED:
        assert word not in folded
    assert project.read_bytes() == before


def test_jsonl_and_cli_guide_match_session(tmp_path: Path):
    project = tmp_path / "seal.pcad.yaml"
    Session.open(SEAL).save(project)
    session = Session.open(project)
    expected = session.troubleshoot_guide("M1", "motor_wont_start")
    agent = Agent()
    assert agent.handle({"op": "open", "path": str(project)})["ok"]
    reply = agent.handle(
        {"id": "g1", "op": "troubleshoot_guide", "ref": "M1", "symptom": "motor_wont_start"}
    )
    assert reply["ok"] is True
    assert reply["id"] == "g1"
    assert reply["result"]["instruction"] == expected["instruction"]
    assert reply["result"]["highlight"] == expected["highlight"]
    help_reply = agent.handle({"op": "help"})
    assert "troubleshoot_guide" in help_reply["result"]["ops"]
    assert "troubleshoot_guide" in help_reply["result"]["schemas"]

    runner = CliRunner()
    shown = runner.invoke(
        app,
        ["electrical", "troubleshoot", "guide", str(project), "--ref", "M1", "--symptom", "motor_wont_start"],
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["next_check"]["device"] == expected["next_check"]["device"]
    csv_out = tmp_path / "out.csv"
    exported = runner.invoke(
        app,
        [
            "electrical",
            "troubleshoot",
            "session-csv",
            str(project),
            "--ref",
            "M1",
            "--symptom",
            "motor_wont_start",
            "--out",
            str(csv_out),
            "--who",
            "Sam",
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert "Sam" in csv_out.read_text(encoding="utf-8-sig")

    drawing = open_drawing(project)
    steps = [
        _response("troubleshoot_guide", {"ref": "M1", "symptom": "motor_wont_start"}, "walk"),
        ModelResponse(parts=[TextPart(content="Next check is on the drawing.")]),
    ]

    def caller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return steps.pop(0)

    with drawing_agent.override(model=FunctionModel(caller)):
        result = drawing_agent.run_sync("Why is M1 not running?", deps=drawing)
    assert "Next check" in result.output
    blob = str(result.all_messages())
    assert "troubleshoot_guide" in blob


def test_dock_walks_checks_highlights_the_sheet_and_exports(tmp_path: Path):
    _app()
    session = Session.open(SEAL)
    project = tmp_path / "seal.pcad.yaml"
    session.save(project)
    window = MainWindow(Session.open(project))
    window.show_troubleshoot()
    dock = window.troubleshoot
    assert "lockout" in dock.banner.text().casefold()
    assert "nfpa 70e" in dock.banner.text().casefold()
    assert dock.device.findData("M1") >= 0
    dock.symptom.setCurrentIndex(dock.symptom.findData("motor_wont_start"))
    dock.device.setCurrentIndex(dock.device.findData("M1"))
    dock.start()
    from PySide6.QtWidgets import QFrame, QLabel, QPushButton

    guide = dock.current_guide()
    assert guide is not None
    assert guide["next_check"]["energized"] == "DE-ENERGIZED"
    current = window.findChild(QFrame, "troubleshootCurrent")
    assert current is not None
    assert "Do this one" in current.findChild(QLabel).text()
    focus = guide["highlight"]["focus"]
    assert window.canvas._trace_highlight["focus"] == focus
    assert window.session.active_sheet_id == guide["highlight"]["sheet"]
    assert any(isinstance(item, TraceHalo) for item in window.canvas._items)
    if guide["highlight"]["wires"]:
        assert any(isinstance(item, TraceWireMark) for item in window.canvas._items)
    check_id = guide["next_check"]["id"]
    order = guide["next_check"]["order"]
    wrong = window.findChild(QPushButton, f"troubleshootWrong_{order}")
    assert wrong is not None
    wrong.click()
    _app().processEvents()
    updated = dock.current_guide()
    assert updated is not None
    assert updated["fault_text"]
    assert updated["results"][check_id] == "wrong"
    assert "fault" in dock.fault.text().casefold()
    narrowed = set(updated["highlight"]["devices"])
    assert narrowed
    assert narrowed < set(guide["highlight"]["devices"])
    assert set(window.canvas._trace_highlight["devices"]) == narrowed
    dock.who.setText("Alex")
    dock.note.setText("open at the first point")
    saved = dock.save_log()
    assert saved is not None
    log = json.loads((tmp_path / "seal.troubleshoot-log.json").read_text(encoding="utf-8"))
    assert log["entries"][0]["who"] == "Alex"
    assert "troubleshoot" not in project.read_text(encoding="utf-8")
    csv_path = dock.write_csv(tmp_path / "dock.csv")
    text = csv_path.read_text(encoding="utf-8-sig")
    assert "Alex" in text
    assert "wrong" in text
    pdf_path = dock.write_pdf(tmp_path / "dock.pdf")
    assert pdf_path.is_file() and pdf_path.stat().st_size > 0
    assert dock.explain_button.isEnabled() is False
    assert "Settings > AI" in dock.explain_button.toolTip()
    window.close()


def test_explain_runs_off_the_ui_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import threading
    import time

    from PySide6.QtCore import QTimer

    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    monkeypatch.setenv("PANELCAD_AI_PROVIDER", "openai")
    monkeypatch.setenv("PANELCAD_AI_API_KEY", "sk-user-test")
    monkeypatch.setenv("PANELCAD_AI_MODEL", "gpt-test")
    started = threading.Event()
    release = threading.Event()

    def slow(*_args, **_kwargs):
        started.set()
        release.wait(5)
        return {"ok": True, "text": "Follow the checklist on the drawing.", "reason": ""}

    monkeypatch.setattr("panelcad.ai.troubleshoot_explain.explain_guide", slow)
    _app()
    session = Session.open(SEAL)
    project = tmp_path / "seal.pcad.yaml"
    session.save(project)
    window = MainWindow(Session.open(project))
    try:
        window.show_troubleshoot()
        dock = window.troubleshoot
        dock.symptom.setCurrentIndex(dock.symptom.findData("motor_wont_start"))
        dock.device.setCurrentIndex(dock.device.findData("M1"))
        dock.start()
        assert dock.explain_button.isEnabled()
        began = time.monotonic()
        dock.explain_now()
        assert time.monotonic() - began < 1
        assert started.wait(2)
        ran = {"ok": False}
        QTimer.singleShot(0, lambda: ran.__setitem__("ok", True))
        _app().processEvents()
        assert ran["ok"] is True
        assert dock.explain_busy.isHidden() is False
        assert dock.explain_cancel.isHidden() is False
        assert dock.explain_button.isEnabled() is False
        assert dock.explain_text.text() == "Explaining this step…"
        release.set()
        deadline = time.monotonic() + 2
        while "checklist" not in dock.explain_text.text().casefold() and time.monotonic() < deadline:
            _app().processEvents()
            time.sleep(0.01)
        assert "checklist" in dock.explain_text.text().casefold()
        assert dock.explain_busy.isHidden()
        assert dock.explain_cancel.isHidden()
        assert dock.explain_button.isEnabled()
    finally:
        release.set()
        window.close()


def test_explain_cancel_discards_a_late_reply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import threading
    import time

    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    monkeypatch.setenv("PANELCAD_AI_PROVIDER", "openai")
    monkeypatch.setenv("PANELCAD_AI_API_KEY", "sk-user-test")
    monkeypatch.setenv("PANELCAD_AI_MODEL", "gpt-test")
    started = threading.Event()
    release = threading.Event()

    def slow(*_args, **_kwargs):
        started.set()
        release.wait(5)
        return {"ok": True, "text": "late answer that must not show", "reason": ""}

    monkeypatch.setattr("panelcad.ai.troubleshoot_explain.explain_guide", slow)
    _app()
    session = Session.open(SEAL)
    project = tmp_path / "seal.pcad.yaml"
    session.save(project)
    window = MainWindow(Session.open(project))
    try:
        window.show_troubleshoot()
        dock = window.troubleshoot
        dock.symptom.setCurrentIndex(dock.symptom.findData("motor_wont_start"))
        dock.device.setCurrentIndex(dock.device.findData("M1"))
        dock.start()
        dock.explain_now()
        assert started.wait(2)
        dock.explain_cancel.click()
        _app().processEvents()
        assert dock.explain_text.text() == "Explain cancelled."
        assert dock.explain_busy.isHidden()
        assert dock.explain_cancel.isHidden()
        release.set()
        deadline = time.monotonic() + 2
        while dock._explain_running and time.monotonic() < deadline:
            _app().processEvents()
            time.sleep(0.01)
        _app().processEvents()
        assert "late answer" not in dock.explain_text.text().casefold()
        assert dock.explain_text.text() == "Explain cancelled."
        assert dock.explain_button.isEnabled()
    finally:
        release.set()
        window.close()


def test_cancel_stops_explain_before_the_provider_timeout(monkeypatch: pytest.MonkeyPatch):
    import socket
    import threading
    import time

    from panelcad.ai.troubleshoot_explain import ExplainCall, explain_guide

    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    monkeypatch.setenv("PANELCAD_AI_PROVIDER", "compatible")
    monkeypatch.setenv("PANELCAD_AI_API_KEY", "sk-user-test")
    monkeypatch.setenv("PANELCAD_AI_MODEL", "gpt-test")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(10)
    port = sock.getsockname()[1]
    held = threading.Event()

    def _accept() -> None:
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        held.wait(30)
        try:
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_accept, daemon=True).start()
    monkeypatch.setenv("PANELCAD_AI_ENDPOINT", f"http://127.0.0.1:{port}/v1")
    session = Session.open(SEAL)
    payload = session.troubleshoot_guide("M1", "motor_wont_start")
    call = ExplainCall()

    def _cancel() -> None:
        time.sleep(0.15)
        call.cancel()

    threading.Thread(target=_cancel, daemon=True).start()
    started = time.monotonic()
    try:
        result = explain_guide(session.project, session.catalog, payload, cancel=call, timeout=45)
    finally:
        held.set()
        sock.close()
    assert time.monotonic() - started < 5
    assert result["cancelled"] is True
    assert result["text"] == ""
    assert result["ok"] is False


def test_explain_already_cancelled_does_not_call_the_account(monkeypatch: pytest.MonkeyPatch):
    from panelcad.ai.troubleshoot_explain import ExplainCall, explain_guide

    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    monkeypatch.setenv("PANELCAD_AI_PROVIDER", "openai")
    monkeypatch.setenv("PANELCAD_AI_API_KEY", "sk-user-test")
    monkeypatch.setenv("PANELCAD_AI_MODEL", "gpt-test")
    session = Session.open(SEAL)
    payload = session.troubleshoot_guide("M1", "motor_wont_start")
    call = ExplainCall()
    call.cancel()

    def transport(_req):
        raise AssertionError("cancelled explain must not call the account")

    result = explain_guide(session.project, session.catalog, payload, transport=transport, cancel=call)
    assert result["cancelled"] is True
    assert result["text"] == ""


def test_empty_drawing_explains_what_to_do_next():
    _app()
    window = MainWindow(Session.create("Empty panel"))
    window.show_troubleshoot()
    dock = window.troubleshoot
    assert "No device tags" in dock.empty.message.text()
    assert dock.start_button.isEnabled() is False
    assert "wires" in dock.start_button.toolTip()
    window.close()


def _response(tool_name: str, args: dict, call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=call_id)])
