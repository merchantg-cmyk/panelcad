"""The wire-by-wire (From-To) list survives conversion; a lost connection is a hard failure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panelcad.convert import engine
from panelcad.convert.from_to_check import SCORER_VERSION, ConnectionLost, RevertRefused, check_connections
from panelcad.core.models import Project
from tests.test_convert_standard import _convert, _session

KINDS = ["dol", "reversing", "star_delta", "plc_motor", "soft_starter", "vfd"]


def _kwargs(kind: str) -> dict:
    kwargs: dict = {"kind": kind, "control": "local"}
    if kind == "vfd":
        kwargs["drive"] = "generic_1k5"
    if kind == "star_delta":
        kwargs["timer_seconds"] = 8
    if kind == "plc_motor":
        kwargs["hp"] = 5
        kwargs["voltage"] = 480
    if kind == "soft_starter":
        kwargs["bypass"] = True
    return kwargs


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("start", ["NEMA/ANSI", "IEC"])
def test_from_to_nets_survive_conversion_both_ways(kind: str, start: str, tmp_path: Path):
    session = _session(f"ft {start} {kind}", start)
    session.insert_circuit(standard=start, **_kwargs(kind))
    other = "iec" if start == "NEMA/ANSI" else "nema"
    result = _convert(session, other, tmp_path, f"{kind}-out")
    report = result["connection_check"]
    assert report["scorer"] == SCORER_VERSION
    assert report["ok"], (report["missing"], report["extra"])
    assert report["nets_before"] == report["nets_after"] > 0
    saved = json.loads(Path(result["workspace"]).read_text(encoding="utf-8"))
    assert saved["connection_check"]["ok"] is True


def test_lost_connection_is_a_hard_failure_and_nothing_is_saved(tmp_path: Path, monkeypatch):
    session = _session("lost", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    real = engine._apply_symbols

    def drop_one(session_, ws, chunk):
        real(session_, ws, chunk)
        sheet = session_.sheet(chunk["sheet"])
        if sheet.wires:
            sheet.wires.pop()

    monkeypatch.setattr(engine, "_apply_symbols", drop_one)
    out = tmp_path / "lost.pcad.yaml"
    with pytest.raises(ConnectionLost) as err:
        session.convert_standard("iec", out=out, workspace=tmp_path / "lost.convert.json")
    assert err.value.report["missing"]
    assert not out.exists()
    saved = json.loads((tmp_path / "lost.convert.json").read_text(encoding="utf-8"))
    assert saved["connection_check"]["ok"] is False


def test_check_connections_flags_lost_and_merged_nets(tmp_path: Path):
    session = _session("unit", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    before = Project.model_validate(session.project.model_dump(mode="json"))
    same = check_connections(before, session.project, session.catalog, tag_map={}, pin_rewrites=[], rail_map={})
    assert same["ok"] and not same["repaired"]

    after = Project.model_validate(before.model_dump(mode="json"))
    sheet = after.sheet("motor-control")
    a = next(w for w in sheet.wires if str(w.number) == "202")
    b = next(w for w in sheet.wires if str(w.number) == "203")
    b.to = a.frm  # tie 203 onto the 202 net
    merged = check_connections(before, after, session.catalog, tag_map={}, pin_rewrites=[], rail_map={})
    assert not merged["ok"] and merged["extra"] and merged["missing"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("start", ["NEMA/ANSI", "IEC"])
def test_stepped_conversion_is_not_blocked_part_way(kind: str, start: str, tmp_path: Path):
    session = _session(f"step {start} {kind}", start)
    session.insert_circuit(standard=start, **_kwargs(kind))
    other = "iec" if start == "NEMA/ANSI" else "nema"
    plan = engine.conversion_plan(session, other, out=tmp_path / "o.pcad.yaml", workspace=tmp_path / "w.json")
    for _ in range(50):
        result = engine.next_chunk(session, plan["workspace"])
        if result["done"]:
            break
    report = result["connection_check"]
    assert report["ok"] and not report["partial"], (report["missing"], report["extra"])
    assert (tmp_path / "o.pcad.yaml").exists()


def _break_last_chunk(monkeypatch):
    real = engine._execute

    def execute(session_, ws, chunk):
        real(session_, ws, chunk)
        if chunk is ws["chunks"][-1]:
            for sheet in session_.project.sheets:
                if sheet.wires:
                    sheet.wires.pop()
                    break

    monkeypatch.setattr(engine, "_execute", execute)


def test_stepped_block_at_final_step_leaves_no_copy_and_rolls_back(tmp_path: Path, monkeypatch):
    session = _session("step-block", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    before = session.project.model_dump(mode="json")
    out = tmp_path / "o.pcad.yaml"
    plan = engine.conversion_plan(session, "iec", out=out, workspace=tmp_path / "w.json")
    _break_last_chunk(monkeypatch)
    with pytest.raises(ConnectionLost):
        for _ in range(50):
            result = session.convert_standard_next(plan["workspace"])
            assert result["done"] is False
            assert not out.exists(), "half-converted copy saved mid-way"
    assert not out.exists()
    assert session.project.model_dump(mode="json") == before
    # Accepting chunks one at a time does not get past the check.
    ws = json.loads(Path(plan["workspace"]).read_text(encoding="utf-8"))
    for chunk in ws["chunks"]:
        if chunk["status"] == "done":
            session.convert_standard_accept(chunk["id"], plan["workspace"])
    with pytest.raises(ConnectionLost):
        session.convert_standard_next(plan["workspace"])
    assert not out.exists()


def test_rerun_that_breaks_wiring_removes_the_earlier_good_copy(tmp_path: Path, monkeypatch):
    session = _session("rerun-block", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    out = tmp_path / "o.pcad.yaml"
    result = session.convert_standard("iec", out=out, workspace=tmp_path / "w.json")
    assert out.exists() and result["connection_check"]["ok"]
    last = result["chunks"][-1]["id"]
    _break_last_chunk(monkeypatch)
    with pytest.raises(ConnectionLost):
        session.convert_standard_rerun(last, tmp_path / "w.json")
    assert not out.exists()


def test_block_never_deletes_a_file_the_user_changed(tmp_path: Path, monkeypatch):
    session = _session("keep-user", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    out = tmp_path / "o.pcad.yaml"
    result = session.convert_standard("iec", out=out, workspace=tmp_path / "w.json")
    out.write_text(out.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    _break_last_chunk(monkeypatch)
    with pytest.raises(ConnectionLost):
        session.convert_standard_rerun(result["chunks"][-1]["id"], tmp_path / "w.json")
    assert out.exists()


def test_revert_then_finish_saves_again(tmp_path: Path):
    session = _session("revert-finish", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    out = tmp_path / "o.pcad.yaml"
    result = session.convert_standard("iec", out=out, workspace=tmp_path / "w.json")
    session.convert_standard_revert("tags", tmp_path / "w.json")
    assert not out.exists(), "a copy that no longer matches the drawing was left behind"
    for _ in range(50):
        result = session.convert_standard_next(tmp_path / "w.json")
        if result["done"]:
            break
    assert result["connection_check"]["ok"]
    assert out.exists()


def test_revert_that_breaks_wiring_is_refused_and_keeps_the_conversion(tmp_path: Path, monkeypatch):
    session = _session("revert-block", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    original = session.project.model_dump(mode="json")
    out = tmp_path / "o.pcad.yaml"
    ws_path = tmp_path / "w.json"
    result = session.convert_standard("iec", out=out, workspace=ws_path)
    converted = session.project.model_dump(mode="json")
    ws_before = ws_path.read_text(encoding="utf-8")
    out_before = out.read_bytes()
    target = result["chunks"][-1]["id"]
    real = engine._rebuild

    def rebuild(session_, ws):
        real(session_, ws)
        for sheet in session_.project.sheets:
            if sheet.wires:
                sheet.wires.pop()
                break

    monkeypatch.setattr(engine, "_rebuild", rebuild)
    with pytest.raises(RevertRefused) as err:
        session.convert_standard_revert(target, ws_path)
    assert target in str(err.value) and "refused" in str(err.value)
    assert session.project.model_dump(mode="json") == converted != original
    assert ws_path.read_text(encoding="utf-8") == ws_before
    assert out.read_bytes() == out_before


def test_dropped_copy_path_is_not_overwritten_if_recreated(tmp_path: Path):
    session = _session("recreated", "NEMA/ANSI")
    session.insert_circuit(standard="NEMA/ANSI", **_kwargs("dol"))
    out = tmp_path / "o.pcad.yaml"
    session.convert_standard("iec", out=out, workspace=tmp_path / "w.json")
    session.convert_standard_revert("tags", tmp_path / "w.json")
    assert not out.exists()
    out.write_text("user file\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        for _ in range(50):
            if session.convert_standard_next(tmp_path / "w.json")["done"]:
                break
    assert out.read_text(encoding="utf-8") == "user file\n"
