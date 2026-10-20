import json
from io import StringIO

from panelcad.cli.agent import Agent, run_agent


def test_agent_create_project_returns_drawing_prefs():
    agent = Agent()
    r = agent.handle(
        {
            "id": 1,
            "op": "create_project",
            "name": "Ladder Job",
            "standard": "ANSI",
            "drawing_style": "ladder",
            "coil_style": "square",
        }
    )
    assert r["ok"] is True, r
    result = r["result"]
    assert result["standard"] == "NEMA/ANSI"
    assert result["drawing_style"] == "ladder"
    assert result["coil_style"] == "square"
    assert result["style_needed"] is False


def test_agent_ask_style_before_print():
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "Blank"})
    assert created["ok"]
    assert created["result"]["style_needed"] is True
    assert created["result"]["next"] == "ask_style"
    asked = agent.handle({"id": 2, "op": "ask_style"})
    assert asked["ok"] is True, asked
    assert asked["result"]["pending"] is True
    assert {o["id"] for o in asked["result"]["options"]} == {"schematic", "ladder", "freeform"}
    agent.handle({"id": 3, "op": "set_title_block", "drawing_style": "freeform"})
    again = agent.handle({"id": 4, "op": "ask_style"})
    assert again["result"]["pending"] is False
    assert again["result"]["drawing_style"] == "freeform"


def test_agent_title_block_drawn_alias():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "TB"})["ok"]
    r = agent.handle({"id": 2, "op": "set_title_block", "drawn": "Gemma", "dwg": "E-1"})
    assert r["ok"] is True, r
    assert r["result"]["drawn_by"] == "Gemma"
    assert r["result"]["drawing_number"] == "E-1"


def test_agent_rejects_wire_to_same_pin():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Loop"})["ok"]
    assert agent.handle(
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "M",
            "x": 40,
            "y": 40,
        }
    )["ok"]
    r = agent.handle({"id": 3, "op": "wire", "from": "M.A2", "to": "M.A2", "net": "L3"})
    assert r["ok"] is False
    assert "same pin" in str(r.get("error") or "").lower()


def test_agent_create_place_validate():
    agent = Agent()
    r = agent.handle({"id": 1, "op": "create_project", "name": "Agent Panel"})
    assert r["ok"]
    r = agent.handle(
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.terminals.x_2p5",
            "ref": "X1",
            "x": 10,
            "y": 10,
        }
    )
    assert r["ok"]
    r = agent.handle({"id": 3, "op": "library_search", "query": "vfd"})
    assert r["ok"]
    assert any("vfd" in d["library_id"] for d in r["result"]["devices"])
    r = agent.handle({"id": 4, "op": "validate"})
    assert r["ok"]
    assert r["result"]["ok"] is True


def test_agent_jsonl_stream(tmp_path):
    out_file = tmp_path / "p.pcad.yaml"
    cmds = [
        {"id": 1, "op": "create_project", "name": "Stream"},
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.protection.fuse_10a",
            "ref": "F1",
            "x": 15,
            "y": 15,
        },
        {"id": 3, "op": "save", "path": str(out_file)},
    ]
    stdin = StringIO("\n".join(json.dumps(c) for c in cmds) + "\n")
    stdout = StringIO()
    run_agent(stdin, stdout)
    lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert all(line["ok"] for line in lines)
    assert out_file.is_file()


def test_agent_close_and_open_sheet():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Tabs"})["ok"]
    assert agent.handle({"id": 2, "op": "add_sheet", "sheet_id": "two", "title": "Two"})["ok"]
    r = agent.handle({"id": 3, "op": "close_sheet", "sheet_id": "two"})
    assert r["ok"]
    r = agent.handle({"id": 4, "op": "list_sheets"})
    two = next(s for s in r["result"]["sheets"] if s["id"] == "two")
    assert two["closed"] is True
    r = agent.handle({"id": 5, "op": "open_sheet", "sheet_id": "two"})
    assert r["ok"]
    assert r["result"]["closed"] is False


def test_agent_duplicate_rotate_delete_wire():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Ops"})["ok"]
    assert agent.handle({"id": 2, "op": "set_title_block", "standard": "IEC"})["ok"]
    assert agent.handle(
        {"id": 3, "op": "place", "library_id": "iec.operators.pb_no", "ref": "S1", "x": 40, "y": 40}
    )["ok"]
    r = agent.handle({"id": 3, "op": "duplicate_component", "ref": "S1"})
    assert r["ok"]
    assert r["result"]["id"] == "S2"
    r = agent.handle({"id": 4, "op": "rotate_component", "ref": "S1", "degrees": 90})
    assert r["ok"]
    assert r["result"]["rotation"] == 90
    r = agent.handle({"id": 5, "op": "list_components"})
    pins = next(c for c in r["result"]["components"] if c["id"] == "S1")["pins"]
    pin = pins[0]
    w = agent.handle({"id": 6, "op": "wire", "from": f"S1.{pin}", "to": f"S2.{pin}"})
    assert w["ok"]
    r = agent.handle({"id": 7, "op": "delete_wire", "wire_id": w["result"]["id"]})
    assert r["ok"]
    r = agent.handle({"id": 8, "op": "list_wires"})
    assert r["ok"]
    assert r["result"]["wires"] == []
    r = agent.handle({"id": 9, "op": "set_pins_locked", "ref": "S1", "locked": False})
    assert r["ok"]
    assert r["result"]["pins_locked"] is False
    r = agent.handle({"id": 10, "op": "set_pin", "ref": "S1", "pin_id": pin, "x": 0, "y": 8})
    assert r["ok"]
    assert r["result"]["y"] == 8
    r = agent.handle({"id": 11, "op": "reset_pins", "ref": "S1"})
    assert r["ok"]
    assert r["result"]["pin_overrides"] == {}


def test_place_and_search_return_pins_and_standard():
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "Pins"})
    assert created["ok"]
    assert created["result"]["sheet"] == "main"
    placed = agent.handle(
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.operators.pb_no",
            "ref": "START",
            "x": 40,
            "y": 40,
        }
    )
    assert placed["ok"], placed
    result = placed["result"]
    assert result["id"] == "START"
    assert result["pins"] == ["13", "14"]
    assert result["standard"] == "IEC"
    search = agent.handle({"id": 3, "op": "library_search", "query": "contactor"})
    assert search["ok"], search
    devices = search["result"]["devices"]
    assert devices
    hit = next(d for d in devices if d["library_id"] == "iec.contactors.ac3_9a")
    assert "A1" in hit["pins"]
    assert "A2" in hit["pins"]
    assert hit["standard"] == "IEC"
    ranks = {"IEC": 0, "NEMA/ANSI": 1, "OEM": 2}
    values = [ranks.get(d["standard"], 9) for d in devices]
    assert values == sorted(values)


def test_wire_unknown_pin_lists_real_pins():
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Pins"})["ok"]
    assert agent.handle(
        {
            "id": 2,
            "op": "place",
            "library_id": "iec.protection.fuse_10a",
            "ref": "FU1",
            "x": 40,
            "y": 40,
        }
    )["ok"]
    r = agent.handle({"id": 3, "op": "wire", "from": "FU1.13", "to": "FU1.2"})
    assert r["ok"] is False
    err = str(r.get("error") or "")
    assert "13" in err
    assert "pins=" in err
    assert "1" in err
    assert "2" in err
