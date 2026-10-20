"""Automatic wire numbers, locked/conventional names, and the from/to list."""

import shutil
from pathlib import Path

import yaml

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.examples.dol import build_dol


def test_new_wire_gets_sheet_line_number():
    s = Session.create("Auto")
    assert s.wire_numbering()["scheme"] == "sheet_line"
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    w = s.wire("K1.A2", "H1.X1")
    assert w.number == "101"
    assert w.caption() == "101"
    w2 = s.wire("K1.A1", "H1.X2")
    assert w2.number == "102"


def test_conventional_net_is_the_number_and_is_not_overwritten():
    s = Session.create("Conv")
    s.place("iec.power.psu_24v_5a", ref="G1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    w = s.wire("G1.plus", "H1.X1", net="24V")
    assert w.number == "24V"
    result = s.number_wires(overwrite=True)
    assert w.number == "24V"
    assert result["assigned"] == 0


def test_connected_segments_share_a_number_through_a_junction():
    s = Session.create("Tee")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.connection.node", ref="N1", x=60, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=100, y=20)
    s.place("iec.operators.pilot_light", ref="H2", x=100, y=60)
    first = s.wire("K1.A2", "N1.A")
    assert first.number == "101"
    second = s.wire("N1.B", "H1.X1")
    assert second.number == "101"
    third = s.wire("N1.A", "H2.X1")
    assert {first.number, second.number, third.number} == {"101"}
    through = s.wire("K1.A1", "H1.X2")
    assert through.number == "102"


def test_ladder_rails_share_a_number_across_sheets():
    s = Session.create("Rails")
    s.set_title_block(drawing_style="ladder")
    s.place("iec.contactors.coil", ref="K1", x=80, y=30)
    s.wire("L1", "K1.A1")
    s.wire("K1.A2", "L2")
    s.add_sheet("two", kind="schematic")
    s.place("iec.contactors.coil", ref="K2", x=80, y=30, sheet_id="two")
    s.wire("L1", "K2.A1", sheet_id="two")
    s.wire("K2.A2", "L2", sheet_id="two")
    s.number_wires(overwrite=True)
    l2 = {w.number for sh in s.project.sheets for w in sh.wires if "L2" in (w.frm, w.to)}
    l1 = {w.number for sh in s.project.sheets for w in sh.wires if "L1" in (w.frm, w.to)}
    assert len(l1) == 1 and next(iter(l1))
    assert len(l2) == 1 and next(iter(l2))
    assert l1 != l2


def test_same_net_on_two_sheets_shares_one_number():
    s = Session.create("Nets")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    s.wire("K1.A2", "H1.X1", net="RUN")
    s.add_sheet("control", kind="schematic")
    s.place("iec.contactors.coil", ref="K2", x=20, y=20, sheet_id="control")
    s.place("iec.operators.pilot_light", ref="H2", x=80, y=20, sheet_id="control")
    s.wire("K2.A2", "H2.X1", net="RUN", sheet_id="control")
    assert s.sheet("main").wires[0].number == "101"
    assert s.sheet("control").wires[0].number == "101"


def test_locked_blank_stays_blank():
    """A cleared, locked number is not filled by the next incremental pass."""

    s = Session.create("Blank lock")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    s.place("iec.operators.pb_no", ref="S1", x=20, y=80)
    kept = s.wire("K1.A2", "H1.X1")
    kept.number = ""
    kept.number_locked = True
    other = s.wire("S1.13", "S1.14")
    assert kept.number == ""
    assert kept.number_locked is True
    assert other.number
    s.number_wires(overwrite=True, scheme="sequential", start=1)
    assert kept.number == ""
    assert other.number == "1"


def test_locked_and_user_set_numbers_survive_renumber():
    s = Session.create("Lock")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    s.place("iec.operators.pb_no", ref="S1", x=20, y=80)
    s.wire("K1.A2", "H1.X1")
    s.wire("S1.13", "S1.14")
    s.set_wire_spec(s.sheet().wires[0].id, number="W99")
    assert s.sheet().wires[0].number_locked is True
    s.number_wires(overwrite=True, scheme="sequential", start=1)
    assert s.sheet().wires[0].number == "W99"
    assert s.sheet().wires[1].number == "1"


def test_l1_power_stays_and_iec_scheme_uses_sheet_dash():
    s = Session.create("IEC")
    s.set_wire_numbering(scheme="iec", start=1)
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=20, y=20)
    s.place("iec.contactors.ac3_9a", ref="K1", x=20, y=80)
    l1 = s.wire("Q1.2", "K1.L1", net="L1")
    t1 = s.wire("K1.T1", "K1.T2")
    assert l1.number == "L1"
    assert t1.number == "1-1"
    s.add_sheet("aux", kind="schematic")
    s.place("iec.contactors.coil", ref="K2", x=20, y=20, sheet_id="aux")
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20, sheet_id="aux")
    other = s.wire("K2.A1", "H1.X1", sheet_id="aux")
    assert other.number == "2-1"


def test_opening_an_existing_file_does_not_renumber(tmp_path: Path):
    src = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"
    before = yaml.safe_load(src.read_text(encoding="utf-8"))
    copy = tmp_path / src.name
    shutil.copy2(src, copy)
    s = Session.open(copy)
    after = [w.number for sh in s.project.sheets for w in sh.wires]
    assert after == [""] * len(after)
    dest = tmp_path / "opened.pcad.yaml"
    s.save(dest)
    dumped = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert "wire_numbering" not in dumped
    assert [w.get("number") for sh in dumped["sheets"] for w in sh.get("wires") or []] == [
        w.get("number") for sh in before["sheets"] for w in sh.get("wires") or []
    ]


def test_delete_then_new_wire_keeps_auto_on():
    s = Session.create("Del")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    w = s.wire("K1.A2", "H1.X1")
    s.delete_wire(w.id)
    again = s.wire("K1.A2", "H1.X1")
    assert again.number == "101"


def test_insert_circuit_numbers_power_leads():
    s = Session.create("CB")
    result = s.insert_circuit(kind="dol", branch="breaker")
    assert result["wires"]
    power = s.sheet(result["sheet"])
    l1 = next(w for w in power.wires if w.net == "L1")
    t1 = next(w for w in power.wires if w.net == "T1")
    assert l1.number == "L1"
    assert t1.number
    assert t1.number != "L1"


def test_number_wires_is_one_undo_step():
    s = Session.create("UndoN")
    s.set_wire_numbering(enabled=False)
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    s.wire("K1.A2", "H1.X1")
    assert s.sheet().wires[0].number == ""
    s.number_wires()
    assert s.sheet().wires[0].number == "101"
    assert s.undo()
    assert s.sheet().wires[0].number == ""


def test_wire_list_has_device_terminal_color_gauge(tmp_path: Path):
    s = Session.create("List")
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    s.wire("K1.A2", "H1.X1", net="RUN", color="BU", gauge="18")
    rows = s.wire_list_rows()
    assert rows[0]["from_device"] == "K1"
    assert rows[0]["from_terminal"] == "A2"
    assert rows[0]["to_device"] == "H1"
    assert rows[0]["to_terminal"] == "X1"
    assert rows[0]["number"] == "101"
    assert rows[0]["color"] == "BU"
    assert rows[0]["gauge"] == "18"
    path = tmp_path / "wires.csv"
    s.export_wire_list(path)
    text = path.read_text(encoding="utf-8")
    assert "from_device" in text
    assert "K1" in text
    assert "A2" in text
    assert "101" in text
    assert "BU" in text


def test_disabled_auto_leaves_new_wires_blank():
    s = Session.create("Off")
    s.set_wire_numbering(enabled=False)
    s.place("iec.contactors.coil", ref="K1", x=20, y=20)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=20)
    w = s.wire("K1.A2", "H1.X1")
    assert w.number == ""


def test_agent_auto_number_settings_and_wire_list(tmp_path: Path):
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "AgentN"})
    assert created["ok"]
    assert created["result"]["wire_numbering"]["scheme"] == "sheet_line"
    assert agent.handle({"id": 2, "op": "place", "library_id": "iec.contactors.coil", "ref": "K1", "x": 20, "y": 20})["ok"]
    assert agent.handle({"id": 3, "op": "place", "library_id": "iec.operators.pilot_light", "ref": "H1", "x": 80, "y": 20})["ok"]
    wired = agent.handle({"id": 4, "op": "wire", "from": "K1.A2", "to": "H1.X1", "net": "RUN"})
    assert wired["ok"], wired
    assert wired["result"]["number"] == "101"
    listed = agent.handle({"id": 5, "op": "wire_list"})
    assert listed["ok"]
    assert listed["result"]["rows"][0]["from_device"] == "K1"
    settings = agent.handle({"id": 6, "op": "set_wire_numbering", "scheme": "sequential", "start": 20})
    assert settings["result"]["scheme"] == "sequential"
    dest = tmp_path / "wl.csv"
    exported = agent.handle({"id": 7, "op": "export_wire_list", "path": str(dest)})
    assert exported["ok"]
    assert dest.is_file()


def test_builtin_dol_auto_numbers_and_export():
    s = build_dol()
    power = {w.net: w.number for w in s.sheet("power").wires}
    assert power["L1"] == "L1"
    assert power["L2"] == "L2"
    assert power["L3"] == "L3"
    assert power["T1"]
    assert power["T1"] != "L1"
    rows = s.wire_list_rows()
    assert any(row["from_device"] == "Q1" and row["number"] == "L1" for row in rows)
    snap = s.describe()
    assert snap["wire_numbering"]["scheme"] == "sheet_line"
    eth = [w.number for w in s.sheet("ethernet").wires]
    assert len(eth) == 2
    assert eth[0] and eth[1]
    assert eth[0] != eth[1], eth
    serial = [w.number for w in s.sheet("serial").wires]
    assert serial and serial[0] != eth[0]
