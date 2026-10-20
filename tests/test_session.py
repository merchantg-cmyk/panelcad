from pathlib import Path

from panelcad import Session
from panelcad.io.yaml_format import load_project


def test_drawing_prefs_on_title_block(tmp_path: Path):
    s = Session.create("Prefs")
    s.set_title_block(standard="ANSI", drawing_style="ladder", coil_style="square", rails="24VDC/0VDC")
    tb = s.get_title_block()
    assert tb["standard"] == "NEMA/ANSI"
    assert tb["drawing_style"] == "ladder"
    assert tb["rail_hot"] == "24VDC"
    assert tb["rail_return"] == "0VDC"
    assert tb["coil_style"] == "square"
    path = tmp_path / "prefs.pcad.yaml"
    s.save(path)
    again = Session.open(path)
    tb2 = again.get_title_block()
    assert tb2["standard"] == "NEMA/ANSI"
    assert tb2["drawing_style"] == "ladder"
    assert tb2["coil_style"] == "square"
    assert tb2["rail_hot"] == "24VDC"
    assert tb2["rail_return"] == "0VDC"
    snap = again.describe()
    assert snap["title_block"]["drawing_style"] == "ladder"


def test_ask_print_style_pending_until_chosen():
    s = Session.create("New Print")
    pending = s.ask_print_style()
    assert pending["pending"] is True
    assert {o["id"] for o in pending["options"]} == {"schematic", "ladder", "freeform"}
    s.set_title_block(drawing_style="freeform")
    done = s.ask_print_style()
    assert done["pending"] is False
    assert done["drawing_style"] == "freeform"


def test_ladder_rails_are_valid_wire_ends():
    s = Session.create("Elem")
    s.set_title_block(drawing_style="ladder", rails="120/N")
    assert s.project.title_block.rail_hot == "120"
    assert s.project.title_block.rail_return == "N"
    s.place("nema.operators.pb_nc", ref="STOP", x=40, y=40)
    s.place("nema.operators.pb_no", ref="START", x=80, y=40)
    w = s.wire("120", "STOP.1")
    assert w.frm == "120"
    s.wire("STOP.2", "START.3")
    assert s.validate() == []


def test_wire_rejects_coil_onto_motor_power_lead():
    s = Session.create("Mix")
    s.place("nema.contactors.coil", ref="M", x=40, y=40)
    s.place("nema.loads.motor_3ph", ref="M1", x=120, y=40)
    try:
        s.wire("M.A2", "M1.T1")
        raise AssertionError("coil A2 to motor T1 must fail")
    except ValueError as exc:
        assert "motor power" in str(exc).lower() or "control" in str(exc).lower()
    s.place("iec.contactors.ac3_9a", ref="K1", x=80, y=80)
    power = s.wire("K1.T1", "M1.T1")
    assert power.frm == "K1.T1"


def test_place_wire_save_roundtrip(tmp_path: Path):
    s = Session.create("Test Panel")
    q1 = s.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    k1 = s.place("iec.relays.k_4co_24vdc", ref="K1", x=60, y=20)
    assert q1.id == "Q1"
    s.wire("Q1.2", "K1.A1", net="L")
    path = tmp_path / "t.pcad.yaml"
    s.save(path)
    again = Session.open(path)
    assert again.project.name == "Test Panel"
    assert again.sheet().component("Q1").library_id == "iec.breakers.mcb_1p_16a"
    assert again.sheet().wires[0].frm == "Q1.2"
    raw = load_project(path)
    dumped = path.read_text(encoding="utf-8")
    assert "from: Q1.2" in dumped
    assert raw.sheets[0].wires[0].to == "K1.A1"
    assert again.validate() == []


def test_drop_wire_routes_when_device_moves():
    from panelcad.core.geometry import Point

    s = Session.create("Move")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=180, y=40)
    w = s.wire("S1.14", "H1.X1")
    s.set_wire_route(w.id, [Point(x=60, y=80), Point(x=160, y=80)])
    assert s.sheet().wires[0].points
    n = s.drop_wire_routes(["S1"])
    assert n == 1
    assert s.sheet().wires[0].points == []
    s.set_wire_route(w.id, [Point(x=60, y=80), Point(x=160, y=80)])
    assert s.drop_wire_routes(["K9"]) == 0
    assert s.sheet().wires[0].points
    assert s.drop_wire_routes() == 1
    assert s.sheet().wires[0].points == []


def test_auto_ref():
    s = Session.create("Refs")
    a = s.place("iec.operators.pb_no")
    b = s.place("iec.operators.pb_no")
    assert a.id == "PB1"
    assert b.id == "PB2"
    iec = Session.create("IEC Refs")
    iec.set_title_block(standard="IEC")
    c = iec.place("iec.operators.pb_no")
    d = iec.place("iec.operators.pb_no")
    assert c.id == "S1"
    assert d.id == "S2"


def test_close_sheet_keeps_page_and_reopens():
    s = Session.create("Tabs")
    s.add_sheet("two", title="Two")
    s.add_sheet("three", title="Three")
    assert [sh.id for sh in s.open_sheets()] == ["main", "two", "three"]
    s.close_sheet("two")
    assert s.project.sheet("two").closed is True
    assert [sh.id for sh in s.open_sheets()] == ["main", "three"]
    assert s.project.sheet("two") is not None
    dumped = s.project.model_dump()
    assert any(sh["id"] == "two" for sh in dumped["sheets"])
    s.open_sheet("two")
    assert s.project.sheet("two").closed is False
    assert s.active_sheet_id == "two"
    try:
        s.close_sheet("two")
        s.close_sheet("three")
        s.close_sheet("main")
        raise AssertionError("should keep one page open")
    except ValueError:
        pass
    assert len(s.open_sheets()) == 1


def test_duplicate_rotate_delete_wire():
    s = Session.create("Dup")
    s.set_title_block(standard="IEC")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.set_address("S1", ip="10.0.0.9", node="3")
    copy = s.duplicate_component("S1")
    assert copy.id == "S2"
    assert copy.x == 50.0
    assert copy.y == 50.0
    assert copy.properties.get("ip") == "10.0.0.9"
    rotated = s.rotate_component("S1", 90)
    assert rotated.rotation == 90
    s.rotate_component("S1", 90)
    assert s.sheet().component("S1").rotation == 180
    w = s.wire("S1.13", "S2.13", net="X")
    assert any(x.id == w.id for x in s.sheet().wires)
    s.set_wire_label(w.id, label="START")
    assert s.sheet().wires[0].caption() == "101"
    assert s.sheet().wires[0].label == "START"
    s.delete_wire(w.id)
    assert s.sheet().wires == []


def test_slide_and_lock_connection_points(tmp_path: Path):
    from panelcad.render.sheet import pin_world

    s = Session.create("Pins")
    k = s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    assert k.pins_locked is True
    device = s.catalog.get("iec.contactors.ac3_9a")
    a1 = device.pin("A1")
    before = pin_world(k, a1)
    s.set_pins_locked("K1", False)
    assert s.sheet().component("K1").pins_locked is False
    ov = s.set_pin("K1", "A1", x=0, y=22, side="left")
    assert ov.x == 0
    assert ov.y == 22
    after = pin_world(s.sheet().component("K1"), a1)
    assert after[1] != before[1]
    path = tmp_path / "pins.pcad.yaml"
    s.save(path)
    dumped = path.read_text(encoding="utf-8")
    assert "pins_locked: false" in dumped
    assert "A1:" in dumped
    again = Session.open(path)
    assert again.sheet().component("K1").pins_locked is False
    assert again.sheet().component("K1").pin_overrides["A1"].y == 22
    copy = again.duplicate_component("K1")
    assert copy.pin_overrides["A1"].y == 22
    again.reset_pins("K1")
    assert again.sheet().component("K1").pin_overrides == {}
    again.set_pins_locked("K1", True)
    assert again.sheet().component("K1").pins_locked is True


def test_oem_update_request_and_apply(tmp_path: Path):
    s = Session.create("OEM")
    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=40)
    s.save(tmp_path / "oem.pcad.yaml")
    queued = s.request_oem_update(["Q1"])
    assert queued["count"] == 1
    assert s.sheet().component("Q1").properties.get("oem_status") == "requested"
    s.apply_oem("Q1", width_mm=54, height_mm=80, depth_mm=70, manufacturer="Schneider", part_number="iC60N")
    assert s.sheet().component("Q1").properties.get("oem_status") == "catalog"
    assert s.sheet().component("Q1").properties.get("width_mm") == 54
    all_q = s.request_oem_update()
    assert all_q["count"] >= 1


def test_unknown_pin_rejected():
    s = Session.create("Bad")
    s.place("iec.operators.pilot_light", ref="H1")
    try:
        s.wire("H1.NOPE", "H1.X2")
        raise AssertionError("should have failed")
    except KeyError:
        pass
