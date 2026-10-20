"""Read-only troubleshooting engine, open export, and the query API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from typer.testing import CliRunner

from panelcad import Session
from panelcad.ai.drawing import drawing_agent, open_drawing
from panelcad.cli.agent import Agent
from panelcad.cli.main import app
from panelcad.core.troubleshoot import citation_errors, prose_violations
from panelcad.core.xref_index import QueryError
from panelcad.version import __version__

ROOT = Path(__file__).resolve().parents[1]
SEAL = ROOT / "examples" / "xref_nema_seal_in.pcad.yaml"
DOL = ROOT / "examples" / "dol_starter.pcad.yaml"
SYMPTOMS = (
    "motor_wont_start",
    "motor_trips",
    "wont_stop",
    "no_control_power",
    "plc_input_not_seen",
    "plc_output_on_load_off",
    "vfd_faulted",
)
BANNED = ("jumper", "bypass", "force", "defeat", "override")


def _response(tool_name: str, args: dict, call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=call_id)])


def _assert_clean(session: Session, payload: dict) -> None:
    blob = json.dumps(payload).casefold()
    assert prose_violations(payload) == []
    assert citation_errors(session.project, payload, session.catalog) == []
    for word in BANNED:
        assert word not in blob


def test_version_is_0_5_5():
    assert __version__ == "0.5.10"


def test_seal_in_path_cites_only_drawing_wires():
    session = Session.open(SEAL)
    circuit = session.circuit_for("M1")
    _assert_clean(session, circuit)
    kinds = [(row["kind"], row["ref"], tuple(row["terminals"]), row["wire"]) for row in circuit["control"]["segments"]]
    assert ("stop", "PB1", ("1", "2"), "102") in kinds
    assert ("start", "PB2", ("3", "4"), "103") in kinds
    assert ("seal_in", "M1-13", ("3", "4"), "103") in kinds
    assert ("coil", "M1", ("A1", "A2"), "103") in kinds
    assert ("ol_nc", "OL1", ("95", "96"), "104") in kinds
    assert circuit["control"]["hot"]["wire"] == "101"
    assert circuit["safety"]["loto"]
    assert "NFPA 70E" in circuit["safety"]["arc_flash"]
    assert circuit["read_only"] is True


def test_missing_voltage_is_needs_info_and_supply_output_is_kept():
    seal = Session.open(SEAL)
    checks = seal.checkpoints("M1", "motor_wont_start")
    _assert_clean(seal, checks)
    live = [row for row in checks["checks"] if row["energized"] == "LIVE"]
    rail = next(row for row in live if row["device"] == "L1")
    assert rail["expected"]["needs_info"] is True
    assert rail["expected"]["volts"] is None
    assert rail["wire"] == "101"
    assert {row["energized"] for row in checks["checks"]} <= {"LIVE", "DE-ENERGIZED"}
    assert checks["lockout"]
    assert "Lock out" in checks["phases"][0]["when"]

    dol = Session.open(DOL)
    power = dol.checkpoints("G1", "no_control_power")
    _assert_clean(dol, power)
    plus = next(row for row in power["checks"] if row["device"] == "G1" and row["terminal"] == "plus")
    assert plus["energized"] == "LIVE"
    assert plus["expected"]["needs_info"] is False
    assert plus["expected"]["text"] == "24 VDC"
    assert plus["expected"]["source"] == "device:G1.Vout"
    unnamed = next(row for row in live if row["device"] == "PB2")
    assert unnamed["expected"]["needs_info"] is True


def test_wont_stop_state_comes_from_the_open_stop_not_a_guessed_rail_voltage():
    session = Session.open(SEAL)
    checks = session.checkpoints("M1", "won't stop")
    coil = next(row for row in checks["checks"] if row["kind"] == "coil" and row["energized"] == "LIVE")
    assert coil["expected"]["needs_info"] is False
    assert coil["expected"]["text"] == "0 V to L2 when stop is pressed"
    assert coil["expected"]["source"] == "stop open state"
    _assert_clean(session, checks)


def test_power_path_and_safety_device_wording():
    dol = Session.open(DOL)
    circuit = dol.circuit_for("M1")
    _assert_clean(dol, circuit)
    phases = circuit["power"]["phases"]
    assert len(phases) == 3
    refs = [row["ref"] for row in phases[0]["segments"]]
    assert refs[:3] == ["Q1", "K1", "F1"]
    assert "M1" in refs
    assert any("no wire" in note for note in circuit["needs_info"])

    estop = dol.checkpoints("S0", "motor_wont_start")
    _assert_clean(dol, estop)
    row = next(item for item in estop["checks"] if item["device"] == "S0")
    assert row["safety_device"] is True
    assert "Find why the safety circuit is open" in row["instruction"]
    causes = dol.likely_causes("S0", "motor_wont_start")
    _assert_clean(dol, causes)
    safety = next(item for item in causes["causes"] if item["ref"] == "S0")
    assert "reset per the machine's procedure" in safety["text"]


def test_every_symptom_stays_inside_the_drawing():
    session = Session.open(SEAL)
    for symptom in SYMPTOMS:
        checks = session.checkpoints("M1", symptom)
        causes = session.likely_causes("M1", symptom)
        _assert_clean(session, checks)
        _assert_clean(session, causes)
        assert checks["symptom"] == symptom


def test_bisection_keeps_the_segment_between_last_ok_and_first_wrong():
    session = Session.open(SEAL)
    checks = session.checkpoints("M1", "motor_wont_start")["checks"]
    live = [row for row in checks if row["energized"] == "LIVE"]
    readings = [
        {"id": live[0]["id"], "result": "ok"},
        {"device": "PB1", "terminal": "2", "result": "pass"},
        {"wire": "103", "result": "wrong", "measured": "0"},
    ]
    causes = session.likely_causes("M1", "Motor won't start", readings)
    _assert_clean(session, causes)
    assert causes["fault_between"]["last_ok"]["device"] == "PB1"
    assert causes["fault_between"]["first_wrong"]["device"] == "PB2"
    assert causes["fault_between"]["first_wrong"]["wire"] == "103"
    assert {row["ref"] for row in causes["causes"]} == {"PB2"}
    assert "seg-3-PB2-start" in causes["window"]


def test_lockout_wording_names_tagout():
    session = Session.open(SEAL)
    payload = session.troubleshoot_guide("M1", "motor_wont_start")
    _assert_clean(session, payload)
    how = payload["how"].casefold()
    instruction = payload["instruction"].casefold()
    assert "tag out" in how
    assert "lock out first" not in how
    assert "tag out" in instruction
    assert "lock out first" not in instruction
    checks = session.checkpoints("M1", "motor_wont_start")
    continuity = next(row for row in checks["checks"] if row["energized"] == "DE-ENERGIZED")
    text = continuity["instruction"].casefold()
    assert "tag out" in text
    assert "lock out first" not in text


def test_highlight_is_the_fault_window_not_the_whole_circuit():
    session = Session.open(SEAL)
    wide = session.troubleshoot_guide("M1", "motor_wont_start")
    checks = wide["checks"]
    live = [row for row in checks if row["energized"] == "LIVE"]
    readings = [
        {"id": live[0]["id"], "result": "ok"},
        {"device": "PB1", "terminal": "2", "result": "ok"},
        {"wire": "103", "result": "wrong"},
    ]
    payload = session.troubleshoot_guide("M1", "motor_wont_start", readings)
    _assert_clean(session, payload)
    window = set(session.likely_causes("M1", "motor_wont_start", readings)["window"])
    rows = [row for row in payload["checks"] if str(row.get("segment_id") or "") in window]
    assert rows
    devices = {str(row["device"]) for row in rows if row.get("device")}
    wires = {str(row["wire"]) for row in rows if row.get("wire")}
    assert set(payload["highlight"]["devices"]) == devices
    assert set(payload["highlight"]["wires"]) == wires
    assert devices < set(wide["highlight"]["devices"])
    assert wires < set(wide["highlight"]["wires"])
    assert payload["highlight"]["focus"] in devices
    assert payload["highlight"]["focus_wire"] in wires or payload["highlight"]["focus_wire"] == ""


def test_csv_timestamp_is_local_time_with_offset(monkeypatch: pytest.MonkeyPatch):
    from datetime import datetime, timedelta, timezone

    from panelcad.core.troubleshoot_guide import session_rows

    zone = timezone(timedelta(hours=-4))
    fixed = datetime(2026, 10, 16, 15, 4, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "panelcad.core.troubleshoot_guide._local_now",
        lambda: fixed.astimezone(zone),
    )
    session = Session.open(SEAL)
    payload = session.troubleshoot_guide("M1", "motor_wont_start")
    rows = session_rows(payload, who="George")
    assert rows
    assert rows[0]["when"] == "2026-10-16T11:04:05-04:00"
    assert rows[0]["when"].endswith("-04:00")
    assert "+00:00" not in rows[0]["when"]
    supplied = session_rows(payload, when="2026-10-14T00:00:00+00:00")
    assert supplied[0]["when"] == "2026-10-14T00:00:00+00:00"


def test_upstream_downstream_and_plc_point():
    seal = Session.open(SEAL)
    up = seal.upstream("M1.A1")
    down = seal.downstream("m1.a2")
    _assert_clean(seal, up)
    _assert_clean(seal, down)
    assert any(row["ref"] == "PB1" and row["wire"] == "102" for row in up["hops"])
    assert any(row["ref"] == "OL1" and "105" in (row["wire"],) for row in down["hops"])

    dol = Session.open(DOL)
    by_address = dol.plc_point("I0_0")
    by_point = dol.plc_point("I0.0")
    by_tag = dol.plc_point("S2")
    assert by_address["point"]["tag"] == "S2"
    assert by_point["point"]["address"] == "I0_0"
    assert by_tag["point"]["module"] == "A1"
    for payload in (by_address, by_point, by_tag):
        _assert_clean(dol, payload)
        assert "Leave PLC points" in payload["safety"]["prohibited"][1]


def test_queries_reject_missing_drawing_facts():
    session = Session.open(SEAL)
    with pytest.raises(QueryError, match="not on the drawing"):
        session.circuit_for("NOPE")
    with pytest.raises(QueryError, match="symptom"):
        session.checkpoints("M1", "it hums")
    with pytest.raises(QueryError, match="terminal"):
        session.upstream("M1.Z9")
    dol = Session.open(DOL)
    with pytest.raises(QueryError, match="not on the drawing"):
        dol.plc_point("O:9/9")
    with pytest.raises(QueryError, match="does not match"):
        session.likely_causes("M1", "motor_wont_start", [{"wire": "999", "result": "wrong"}])


def test_citation_and_prose_scanners_catch_planted_text():
    session = Session.open(SEAL)
    planted = {"instruction": "Jumper wire 999 at M1 terminal Z9", "device": "M1", "terminal": "Z9", "wire": "999"}
    assert prose_violations(planted)
    assert "wire 999" in citation_errors(session.project, planted, session.catalog)
    assert any("Z9" in item for item in citation_errors(session.project, planted, session.catalog))


def test_export_and_sidecar_log_leave_the_project_file_alone(tmp_path: Path):
    session = Session.open(SEAL)
    project = tmp_path / "seal.pcad.yaml"
    session.save(project)
    before = project.read_bytes()
    dest = tmp_path / "troubleshoot.json"
    written = Session.open(project).export_troubleshoot(dest)
    document = json.loads(written.read_text(encoding="utf-8"))
    assert document["format"] == "panelcad-troubleshoot"
    assert document["schema"] == "1.0"
    assert document["read_only"] is True
    assert document["plc_code_refs"] == []
    assert {row["op"] for row in document["ops"]} == {
        "circuit_for",
        "checkpoints",
        "upstream",
        "downstream",
        "plc_point",
        "likely_causes",
        "troubleshoot_guide",
    }
    assert any(row["device"] == "M1" and "M1-13" in row["contacts"] for row in document["cross_refs"])
    assert any(row["wire_no"] == "103" for row in document["nets"])
    assert any(item["topic"] == "voltage" for item in document["needs_info"])
    assert any(item["topic"] == "plc_program" for item in document["needs_info"])
    opened = Session.open(project)
    _assert_clean(opened, document)
    assert "troubleshoot" not in project.read_text(encoding="utf-8")

    readings = [{"device": "PB2", "terminal": "4", "result": "wrong"}]
    first = opened.append_troubleshoot_log(
        ref="M1",
        symptom="motor_wont_start",
        readings=readings,
        who="Alex",
        note="voltage low",
        project_path=project,
    )
    second = opened.append_troubleshoot_log(
        ref="M1",
        symptom="motor_wont_start",
        readings=[{"id": first["entry"]["readings"][0]["id"], "result": "ok"}],
        project_path=project,
    )
    log_path = tmp_path / "seal.troubleshoot-log.json"
    assert Path(first["path"]) == log_path
    assert Path(second["path"]) == log_path
    log = json.loads(log_path.read_text(encoding="utf-8"))
    assert log["format"] == "panelcad-troubleshoot-log"
    assert log["schema"] == "1.0"
    assert [row["who"] for row in log["entries"]] == ["Alex", ""]
    assert project.read_bytes() == before


def test_package_includes_troubleshoot_json(tmp_path: Path):
    session = Session.open(SEAL)
    root = session.export_package(tmp_path / "pack", pdf=False)
    document = json.loads((root / "troubleshoot.json").read_text(encoding="utf-8"))
    assert document["format"] == "panelcad-troubleshoot"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert "troubleshoot.json" in manifest["files"]


def test_jsonl_cli_and_drawing_agent_match_session(tmp_path: Path):
    project = tmp_path / "seal.pcad.yaml"
    Session.open(SEAL).save(project)
    session = Session.open(project)
    expected = session.circuit_for("M1")

    agent = Agent()
    assert agent.handle({"op": "open", "path": str(project)})["ok"]
    reply = agent.handle({"id": "c1", "op": "circuit_for", "ref": "M1"})
    assert reply["ok"] is True
    assert reply["id"] == "c1"
    assert reply["result"]["control"]["segments"] == expected["control"]["segments"]
    missing = agent.handle({"op": "plc_point", "address": "NOPE"})
    assert missing["ok"] is False
    assert "not on the drawing" in missing["error"]
    assert "crash" not in missing
    help_reply = agent.handle({"op": "help"})
    for op in ("circuit_for", "checkpoints", "upstream", "downstream", "plc_point", "likely_causes"):
        assert op in help_reply["result"]["ops"]
        assert op in help_reply["result"]["schemas"]

    runner = CliRunner()
    shown = runner.invoke(app, ["electrical", "troubleshoot", "circuit", str(project), "--ref", "M1"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["ref"] == "M1"
    rejected = runner.invoke(app, ["electrical", "troubleshoot", "upstream", str(project), "--endpoint", "M1.Z9"])
    assert rejected.exit_code == 1
    assert "terminal" in rejected.stderr

    out = tmp_path / "out.json"
    exported = runner.invoke(
        app, ["electrical", "troubleshoot", "export", str(project), "--out", str(out)]
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == "1.0"
    logged = runner.invoke(
        app,
        [
            "electrical",
            "troubleshoot",
            "log",
            str(project),
            "--ref",
            "M1",
            "--symptom",
            "motor_wont_start",
            "--readings",
            '[{"device": "PB1", "terminal": "2", "result": "ok"}]',
            "--who",
            "Sam",
        ],
    )
    assert logged.exit_code == 0, logged.output
    log = json.loads((tmp_path / "seal.troubleshoot-log.json").read_text(encoding="utf-8"))
    assert log["entries"][0]["who"] == "Sam"
    assert "troubleshoot" not in project.read_text(encoding="utf-8")

    drawing = open_drawing(project)
    steps = [
        _response("circuit_for", {"ref": "M1"}, "path"),
        ModelResponse(parts=[TextPart(content="M1 path is on the drawing.")]),
    ]

    def caller(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return steps.pop(0)

    with drawing_agent.override(model=FunctionModel(caller)):
        result = drawing_agent.run_sync("Why is M1 not running?", deps=drawing)
    assert "M1 path" in result.output
    blob = str(result.all_messages())
    assert "PB1" in blob
    assert "102" in blob
