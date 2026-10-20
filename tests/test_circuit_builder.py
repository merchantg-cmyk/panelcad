"""Circuit Builder: DOL and VFD motor circuits inserted onto the open schematic."""

from __future__ import annotations

import pytest

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.electrical import family_key
from panelcad.core.ink import schematic_ink_rect
from panelcad.core.route import component_aabb
from panelcad.io.bom import instance_rows
from panelcad.render.sheet import iter_wire_points, pin_world
from panelcad.render.titleblock import working_rect
from tests.support import confirm_layout_occupants, patch_layout_place_confirms


@pytest.fixture(autouse=True)
def _confirm_before_circuit_pack(monkeypatch):
    """Packing-math tests confirm library envelopes; production code does not."""

    from panelcad.core import circuit_layout

    orig = circuit_layout.pack_circuit_layouts

    def _pack(session, parts, **kwargs):
        confirm_layout_occupants(session)
        return orig(session, parts, **kwargs)

    monkeypatch.setattr(circuit_layout, "pack_circuit_layouts", _pack)
    patch_layout_place_confirms(monkeypatch)

DOL_WIRES = {
    ("Q1.2", "K1.L1", "L1"),
    ("Q1.4", "K1.L2", "L2"),
    ("Q1.6", "K1.L3", "L3"),
    ("K1.T1", "F1.1", "T1"),
    ("K1.T2", "F1.3", "T2"),
    ("K1.T3", "F1.5", "T3"),
    ("F1.2", "M1.U1", "U1"),
    ("F1.4", "M1.V1", "V1"),
    ("F1.6", "M1.W1", "W1"),
}

VFD_DRIVES = {
    "generic_1k5": {
        "library_id": "iec.drives.vfd_1k5",
        "line": {"L1", "L2", "L3"},
        "motor": {"U", "V", "W"},
        "common": "COM",
        "run": "DI1",
        "phases": 3,
    },
    "gs20_0p5": {
        "library_id": "adc.durapulse.gs21_20p5",
        "line": {"L", "N"},
        "motor": {"U", "V", "W"},
        "common": "DCM",
        "run": "DI1",
        "phases": 1,
    },
    "es1_0p75": {
        "library_id": "adc.ironhorse.es1_3a_n4",
        "line": {"AC1", "AC2"},
        "motor": {"U", "V", "W"},
        "common": "P7-7",
        "run": "P7-8",
        "phases": 1,
    },
}


def _wires(result: dict) -> set[tuple[str, str, str]]:
    return {(w["from"], w["to"], w["net"]) for w in result["wires"]}


def _has_link(result: dict, a: str, b: str) -> bool:
    pairs = {(w["from"], w["to"]) for w in result["wires"]}
    return (a, b) in pairs or (b, a) in pairs


def _lib(result: dict, role: str) -> str:
    return next(c["library_id"] for c in result["components"] if c["role"] == role)


def _id(result: dict, role: str) -> str:
    return next(c["id"] for c in result["components"] if c["role"] == role)


def _drive_pins(result: dict) -> set[str]:
    drive = _id(result, "drive")
    pins: set[str] = set()
    for wire in result["wires"]:
        for end in (wire["from"], wire["to"]):
            ref, pin = end.split(".", 1)
            if ref == drive:
                pins.add(pin)
    return pins


def _rects(session: Session, sheet_id: str | None = None) -> list[tuple[float, float, float, float]]:
    sheet = session.sheet(sheet_id)
    rects = []
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        rects.append(
            component_aabb(
                comp.x,
                comp.y,
                device.schematic_width,
                device.schematic_height,
                comp.rotation,
                comp.mirror,
            )
        )
    return rects


def _assert_no_overlap(session: Session, sheet_id: str | None = None) -> None:
    rects = _rects(session, sheet_id)
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)


def _ink_rects(session: Session, sheet_id: str | None = None) -> list[tuple[float, float, float, float]]:
    sheet = session.sheet(sheet_id)
    rects = []
    for comp in sheet.components:
        device = session.catalog.get(comp.library_id)
        rects.append(schematic_ink_rect(device, comp.x, comp.y, comp.id, component=comp))
    return rects


def _assert_inside_working(session: Session, sheet_id: str, pad: float = 0.05) -> None:
    """Bodies, tags, pin labels, pin dots, and wire stubs stay in the drawing area."""

    sheet = session.sheet(sheet_id)
    wx, wy, ww, wh = working_rect(sheet)
    max_x, max_y = wx + ww, wy + wh
    for left, top, right, bottom in _ink_rects(session, sheet_id):
        assert left >= wx - pad, (left, wx)
        assert top >= wy - pad, (top, wy)
        assert right <= max_x + pad, (right, max_x)
        assert bottom <= max_y + pad, (bottom, max_y)
    for wire in sheet.wires:
        for point in iter_wire_points(sheet, session.catalog, wire):
            assert wx - pad <= point.x <= max_x + pad, (wire.id, point.x, wx, max_x)
            assert wy - pad <= point.y <= max_y + pad, (wire.id, point.y, wy, max_y)


def _pin_x(session: Session, ref: str, pin: str, sheet_id: str | None = None) -> float:
    sheet = session.sheet(sheet_id)
    comp = sheet.component(ref)
    device = session.catalog.get(comp.library_id)
    return pin_world(comp, device.pin(pin))[0]


def _pin_y(session: Session, ref: str, pin: str, sheet_id: str | None = None) -> float:
    sheet = session.sheet(sheet_id)
    comp = sheet.component(ref)
    device = session.catalog.get(comp.library_id)
    return pin_world(comp, device.pin(pin))[1]


def _iec(name: str) -> Session:
    session = Session.create(name)
    session.set_title_block(standard="IEC")
    return session


@pytest.mark.confirm_generic_layout
def test_dol_matches_example_pin_pairs_and_libraries():
    session = _iec("DOL")
    result = session.insert_circuit(kind="dol")
    libs = {c["library_id"] for c in result["components"]}
    assert result["standard"] == "IEC"
    assert "iec.breakers.mcb_3p_32a" in libs
    assert "iec.contactors.ac3_9a" in libs
    assert "iec.protection.overload_9a" in libs
    assert "iec.loads.motor_3ph" in libs
    assert DOL_WIRES <= _wires(result)
    assert result["layout"]["sheet"]
    assert result["layout"]["din"] >= 3
    assert not any("overload" in c["library_id"] and c["role"] != "overload" for c in result["components"])
    motor = session.sheet().component("M1")
    assert motor.properties["kW"] == 1.5
    assert session.sheet().component("Q1").x == pytest.approx(40)
    q = session.sheet().component("Q1")
    _wx, wy, _ww, _wh = working_rect(session.sheet())
    assert wy <= q.y <= 22
    _assert_inside_working(session, result["sheet"])
    anchor = _pin_x(session, "Q1", "1")
    assert _pin_x(session, "K1", "L1") == pytest.approx(anchor)
    assert _pin_x(session, "F1", "1") == pytest.approx(anchor)
    assert _pin_x(session, "M1", "U1") == pytest.approx(anchor)
    k = session.sheet().component("K1")
    qh = session.catalog.get(q.library_id).schematic_height
    gap = k.y - (q.y + qh)
    assert 4.0 <= gap <= 8.0
    _assert_no_overlap(session)


def test_each_vfd_option_uses_its_line_and_motor_pins():
    for drive, spec in VFD_DRIVES.items():
        session = _iec(drive)
        result = session.insert_circuit(kind="vfd", drive=drive)
        assert _lib(result, "drive") == spec["library_id"]
        pins = _drive_pins(result)
        assert spec["line"] <= pins
        assert spec["motor"] <= pins
        assert "iec.loads.motor_3ph" in {c["library_id"] for c in result["components"]}
        assert not any("overload" in c["library_id"] for c in result["components"])
        assert not any("contactor" in c["library_id"] for c in result["components"])
        if spec["phases"] == 1:
            assert pins.isdisjoint({"L1", "L2", "L3"})
            assert not any(w["net"] in {"L1", "L2", "L3"} for w in result["wires"])
        else:
            assert {"L1", "L2", "L3"} <= pins
        _assert_no_overlap(session)


def test_one_phase_breaker_branch_is_two_mcbs_and_disconnect_spares_pole_5():
    session = _iec("1P breakers")
    result = session.insert_circuit(kind="vfd", drive="gs20_0p5", branch="breaker")
    breakers = [c for c in result["components"] if c["role"] == "breaker"]
    assert len(breakers) == 2
    assert {c["library_id"] for c in breakers} == {"iec.breakers.mcb_1p_16a"}
    assert _drive_pins(result).isdisjoint({"L1", "L2", "L3"})

    session = _iec("1P disc")
    result = session.insert_circuit(kind="vfd", drive="es1_0p75", branch="disconnect")
    disc = _id(result, "disconnect")
    assert _lib(result, "disconnect") == "iec.protection.disconnect_3p"
    used = set()
    for wire in result["wires"]:
        for end in (wire["from"], wire["to"]):
            ref, pin = end.split(".", 1)
            if ref == disc:
                used.add(pin)
    assert used == {"2", "4"}
    assert _drive_pins(result) >= {"AC1", "AC2", "U", "V", "W"}
    assert _drive_pins(result).isdisjoint({"L1", "L2", "L3"})


def test_default_vfd_has_no_overload():
    session = _iec("VFD default")
    result = session.insert_circuit(kind="vfd", drive="generic_1k5")
    assert result["kind"] == "vfd"
    assert all("overload" not in c["library_id"] for c in result["components"])
    assert all(c["role"] != "overload" for c in result["components"])


def test_unknown_drive_and_kind_raise():
    session = _iec("Bad")
    session.place("iec.operators.pb_no", ref="S9", x=10, y=10)
    with pytest.raises(ValueError, match="unknown kind"):
        session.insert_circuit(kind="softstart")
    with pytest.raises(ValueError, match="unknown drive 'not-a-drive'") as unknown:
        session.insert_circuit(kind="vfd", drive="not-a-drive")
    assert "Valid drives: generic_1k5, gs20_0p5, es1_0p75" in str(unknown.value)
    with pytest.raises(ValueError, match="missing drive") as missing:
        session.insert_circuit(kind="vfd")
    assert "kind=vfd needs a drive" in str(missing.value)
    assert "Valid drives: generic_1k5, gs20_0p5, es1_0p75" in str(missing.value)
    with pytest.raises(ValueError, match="unknown branch"):
        session.insert_circuit(kind="dol", branch="fuse")
    with pytest.raises(ValueError, match="unknown control"):
        session.insert_circuit(kind="dol", control="plc")
    assert [c.id for c in session.sheet().components] == ["S9"]


def test_existing_q1_is_not_reused():
    session = _iec("Refs")
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=40, y=22)
    result = session.insert_circuit(kind="dol")
    breaker = _id(result, "breaker")
    assert breaker != "Q1"
    assert session.sheet().component("Q1").library_id == "iec.breakers.mcb_1p_16a"
    ids = [c.id for c in session.sheet().components]
    assert len(ids) == len(set(ids))
    _assert_no_overlap(session)
    again = session.insert_circuit(kind="dol", x=40, y=22)
    assert _id(again, "breaker") not in {breaker, "Q1"}
    _assert_no_overlap(session)


def test_kw_7_5_selects_32a_contactor_and_over_15_selects_mccb():
    session = _iec("kW")
    result = session.insert_circuit(kind="dol", kw=7.5)
    assert _lib(result, "contactor") == "iec.contactors.ac3_32a"
    assert _lib(result, "breaker") == "iec.breakers.mcb_3p_32a"
    assert session.sheet().component(_id(result, "motor")).properties["kW"] == 7.5

    session = _iec("MCCB")
    result = session.insert_circuit(kind="dol", kw=18)
    assert _lib(result, "breaker") == "iec.breakers.mccb_3p_100a"
    assert _lib(result, "contactor") == "iec.contactors.ac3_32a"


def test_local_control_stays_off_motor_power_pins():
    session = _iec("DOL local")
    result = session.insert_circuit(kind="dol", control="local")
    roles = {c["role"] for c in result["components"]}
    assert {"psu", "estop", "stop", "start", "contactor", "motor"} <= roles
    motor = _id(result, "motor")
    for wire in result["wires"]:
        ends = (wire["from"], wire["to"])
        if any(end.startswith(motor + ".") for end in ends):
            other = next(end for end in ends if not end.startswith(motor + "."))
            other_ref = other.split(".", 1)[0]
            role = next(c["role"] for c in result["components"] if c["id"] == other_ref)
            assert role == "overload"
    assert any(w["to"].endswith(".A1") or w["from"].endswith(".A1") for w in result["wires"])
    assert any(w["net"] == "24V" for w in result["wires"])
    ctrl = result["control_sheet"]
    ys = [
        _pin_y(session, _id(result, "estop"), "11", ctrl),
        _pin_y(session, _id(result, "stop"), "11", ctrl),
        _pin_y(session, _id(result, "start"), "13", ctrl),
    ]
    assert max(ys) - min(ys) < 0.2
    assert _pin_y(session, _id(result, "psu"), "plus", ctrl) + 4.0 < min(ys)
    xs = [
        session.sheet(ctrl).component(_id(result, "estop")).x,
        session.sheet(ctrl).component(_id(result, "stop")).x,
        session.sheet(ctrl).component(_id(result, "start")).x,
    ]
    assert xs[0] < xs[1] < xs[2]
    ol_x = session.sheet(ctrl).component(_id(result, "ol_contact")).x
    assert ol_x > xs[2]
    _assert_seals_parallel_start(session, ctrl)
    _assert_no_overlap(session, result["sheet"])
    _assert_no_overlap(session, ctrl)

    for drive, spec in VFD_DRIVES.items():
        session = _iec(f"local {drive}")
        result = session.insert_circuit(kind="vfd", drive=drive, control="local")
        assert not any("psu" in c["library_id"] for c in result["components"])
        pins = _drive_pins(result)
        assert spec["common"] in pins
        assert spec["run"] in pins
        if spec["phases"] == 1:
            assert pins.isdisjoint({"L1", "L2", "L3"})
        _assert_no_overlap(session, result["sheet"])
        _assert_drawn_segments_match_pins(session, result["sheet"], mix_numbers=False, bodies=True)


def test_one_undo_removes_the_circuit_only():
    session = _iec("Undo")
    session.place("iec.operators.pb_no", ref="S9", x=8, y=8)
    session.insert_circuit(kind="dol", control="local")
    assert any(c.library_id == "iec.loads.motor_3ph" for c in session.sheet().components)
    assert session.undo()
    assert [c.id for c in session.sheet().components] == ["S9"]
    assert session.sheet().wires == []
    assert session.undo()
    assert session.sheet().components == []


@pytest.mark.confirm_generic_layout
def test_panel_sheet_gets_a_new_schematic_and_new_sheet_flag():
    session = _iec("Panel")
    session.add_sheet("enc", title="Enclosure", kind="panel_layout")
    assert session.sheet().kind == "panel_layout"
    result = session.insert_circuit(kind="dol")
    assert result["sheet"] != "enc"
    target = session.project.sheet(result["sheet"])
    assert target.kind == "schematic"
    assert target.title == "Motor power"
    enc_ids = {c.id for c in session.project.sheet("enc").components}
    assert {"Q1", "K1", "F1"} <= enc_ids
    assert "M1" not in enc_ids
    assert session.project.sheets[0].components == []

    fresh = _iec("New sheet")
    fresh.place("iec.operators.pb_no", ref="S1", x=12, y=12)
    result = fresh.insert_circuit(kind="vfd", drive="generic_1k5", new_sheet=True)
    sheet = fresh.project.sheet(result["sheet"])
    assert sheet.title == "Motor power"
    assert sheet.kind == "schematic"
    assert [c.id for c in fresh.project.sheets[0].components] == ["S1"]
    assert fresh.project.name == "New sheet"


def test_vfd_line_contactor_and_ratings():
    session = _iec("Line")
    result = session.insert_circuit(kind="vfd", drive="generic_1k5", line_contactor=True, kw=7.5)
    assert _lib(result, "contactor") == "iec.contactors.ac3_32a"
    assert not any("overload" in c["library_id"] for c in result["components"])
    drive = _id(result, "drive")
    contactor = _id(result, "contactor")
    joined = {
        (w["from"], w["to"])
        for w in result["wires"]
        if contactor in w["from"] or contactor in w["to"]
    }
    assert any(end.startswith(f"{contactor}.T") and drive in other for end, other in joined)
    motor = session.sheet(result["sheet"]).component(_id(result, "motor"))
    assert motor.properties["kW"] == 7.5

    hp = _iec("hp drive")
    result = hp.insert_circuit(kind="vfd", drive="gs20_0p5")
    motor = hp.sheet().component(_id(result, "motor"))
    assert motor.properties.get("hp") == 0.5
    assert "kW" not in motor.properties


def test_failed_insert_on_missing_sheet_leaves_the_project():
    session = _iec("Missing")
    session.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=22)
    with pytest.raises(KeyError):
        session.insert_circuit(kind="dol", sheet_id="no-such-sheet")
    assert [c.id for c in session.sheet().components] == ["Q1"]
    assert session.sheet().wires == []


def test_agent_insert_circuit_after_create_project():
    agent = Agent()
    created = agent.handle({"id": 1, "op": "create_project", "name": "Agent circuit"})
    assert created["ok"] is True
    result = agent.handle({"id": 2, "op": "insert_circuit", "kind": "dol"})
    assert result["ok"] is True, result
    libs = {c["library_id"] for c in result["result"]["components"]}
    assert result["result"]["standard"] == "NEMA/ANSI"
    assert "nema.breakers.mcb_3p_32a" in libs
    assert "nema.contactors.ac3_9a" in libs
    assert "nema.protection.overload" in libs
    assert "nema.loads.motor_3ph" in libs
    help_result = agent.handle({"id": 3, "op": "help"})
    assert "insert_circuit" in help_result["result"]["ops"]
    assert "insert_circuit" in help_result["result"]["help"]


def test_circuit_builder_dialog_and_insert_menu():
    from PySide6.QtWidgets import QApplication, QLabel

    from panelcad.gui.circuit_dialog import CircuitBuilderDialog
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    dialog = CircuitBuilderDialog()
    assert dialog.windowTitle() == "Circuit Builder"
    values = dialog.values()
    assert values["kind"] == "dol"
    assert values["drive"] is None
    assert values["branch"] == "breaker"
    assert values["line_contactor"] is False
    assert values["control"] == "none"
    assert values["kw"] is None
    assert values["new_sheet"] is False
    assert not dialog.drive.isEnabled()

    dialog.kind.setCurrentIndex(dialog.kind.findData("vfd"))
    dialog.drive.setCurrentIndex(dialog.drive.findData("es1_0p75"))
    dialog.branch.setCurrentIndex(dialog.branch.findData("disconnect"))
    dialog.control.setCurrentIndex(dialog.control.findData("local"))
    dialog.line_contactor.setChecked(True)
    dialog.kw.setValue(2.2)
    dialog.new_sheet.setChecked(True)
    values = dialog.values()
    # main d639fc7 asserted the original circuit fields only. place_layout,
    # shared_supply, shared_estop, and separate_control_rail are inserted on
    # purpose so the dialog matches Session.insert_circuit.
    assert values == {
        "kind": "vfd",
        "drive": "es1_0p75",
        "branch": "disconnect",
        "line_contactor": True,
        "control": "local",
        "kw": 2.2,
        "hp": None,
        "voltage": None,
        "control_voltage": None,
        "fla": None,
        "pilot_lights": False,
        "bypass": False,
        "control_wiring": "3wire",
        "starter": "dol",
        "timer_seconds": None,
        "overload_placement": "windings",
        "hoa": True,
        "plc_model": None,
        "io_run": "I0_0",
        "io_ol": "I0_1",
        "io_auto": "I0_2",
        "io_out": "Q0_0",
        "new_sheet": True,
        "manufacturer": None,
        "secondary_manufacturer": None,
        "place_layout": True,
        "shared_supply": True,
        "shared_estop": True,
        "separate_control_rail": False,
        "standard": "NEMA/ANSI",
    }
    assert dialog.drive.isEnabled()
    assert "DOL starter" in dialog.kind.itemText(0)
    assert dialog.kind.findData("reversing") >= 0
    assert dialog.kind.findData("star_delta") >= 0
    assert dialog.kind.findData("soft_starter") >= 0
    assert dialog.kind.findData("plc_motor") >= 0
    assert dialog.timer_seconds.value() == pytest.approx(6.0)
    assert dialog.overload_placement.findData("windings") >= 0
    assert dialog.overload_placement.findData("line") >= 0
    assert dialog.hoa.isChecked()
    assert dialog.plc_model.findData("nema.plc.compact_16dio") >= 0
    assert dialog.io_run.text() == "I0_0"
    assert dialog.io_ol.text() == "I0_1"
    assert dialog.io_auto.text() == "I0_2"
    assert dialog.io_out.text() == "Q0_0"
    assert "IronHorse" in dialog.drive.currentText()
    assert any("NEMA/ANSI" in label.text() for label in dialog.findChildren(QLabel))
    dialog.close()

    labeled = CircuitBuilderDialog(standard="NEMA/ANSI")
    assert any("NEMA/ANSI" in label.text() for label in labeled.findChildren(QLabel))
    labeled.close()

    window = MainWindow(Session.create("Menu"))
    window.show()
    app.processEvents()
    titles: list[str] = []
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu and menu.title().replace("&", "") == "Insert":
            titles = [item.text().replace("&", "") for item in menu.actions()]
    assert "Circuit Builder…" in titles
    kept = window.session.project.name
    result = window.session.insert_circuit(**CircuitBuilderDialog().values())
    assert window.session.project.name == kept
    assert result["kind"] == "dol"
    assert result["standard"] == "NEMA/ANSI"
    assert any(c.library_id == "nema.loads.motor_3ph" for c in window.session.sheet().components)
    window._refresh_from_session(fit=True)
    app.processEvents()
    window.close()


def test_circuit_builder_dialog_inserts_each_new_kind():
    """Offscreen GUI: each new kind inserts from Circuit Builder dialog.values()."""

    from PySide6.QtWidgets import QApplication

    from panelcad.gui.circuit_dialog import CircuitBuilderDialog

    app = QApplication.instance() or QApplication([])
    setups = [
        ("vfd", {"drive": "generic_1k5"}),
        ("reversing", {}),
        ("star_delta", {"timer_seconds": 10.0, "overload_placement": "windings"}),
        ("soft_starter", {"bypass": True}),
        (
            "plc_motor",
            {
                "hoa": True,
                "plc_model": "nema.plc.compact_16dio",
                "io_run": "I0_3",
                "io_ol": "I0_1",
                "io_auto": "I0_2",
                "io_out": "Q0_1",
            },
        ),
    ]
    for kind, opts in setups:
        dialog = CircuitBuilderDialog()
        idx = dialog.kind.findData(kind)
        assert idx >= 0, kind
        dialog.kind.setCurrentIndex(idx)
        if kind == "vfd":
            dialog.drive.setCurrentIndex(dialog.drive.findData(opts["drive"]))
        if kind == "star_delta":
            dialog.timer_seconds.setValue(opts["timer_seconds"])
            dialog.overload_placement.setCurrentIndex(
                dialog.overload_placement.findData(opts["overload_placement"])
            )
            assert dialog.timer_seconds.isEnabled()
            assert dialog.overload_placement.isEnabled()
        if kind == "soft_starter":
            dialog.bypass.setChecked(True)
            assert dialog.bypass.isEnabled()
        if kind == "plc_motor":
            dialog.hoa.setChecked(True)
            dialog.plc_model.setCurrentIndex(dialog.plc_model.findData(opts["plc_model"]))
            dialog.io_run.setText(opts["io_run"])
            dialog.io_ol.setText(opts["io_ol"])
            dialog.io_auto.setText(opts["io_auto"])
            dialog.io_out.setText(opts["io_out"])
            assert dialog.hoa.isEnabled()
            assert dialog.plc_model.isEnabled()
            assert dialog.io_run.isEnabled()
        values = dialog.values()
        assert values["kind"] == kind
        session = Session.create(f"dialog {kind}")
        result = session.insert_circuit(**values)
        assert result["kind"] == kind
        roles = {c["role"] for c in result["components"]}
        if kind == "vfd":
            assert "drive" in roles
            assert "motor" in roles
        if kind == "reversing":
            assert {"fwd", "rev", "overload", "motor"} <= roles
        if kind == "star_delta":
            assert result["timer_seconds"] == pytest.approx(10.0)
            assert result["overload_placement"] == "delta_windings"
            timer = session.sheet(result["control_sheet"]).component(_id(result, "timer"))
            assert timer.properties.get("timer_seconds") == pytest.approx(10.0)
            assert {"main", "star", "delta", "timer"} <= roles
        if kind == "soft_starter":
            assert {"softstarter", "bypass", "motor"} <= roles
        if kind == "plc_motor":
            assert result["hoa"] is True
            assert result["plc_model"] == "nema.plc.compact_16dio"
            assert result["io"]["run"] == "I0_3"
            assert result["io"]["out"] == "Q0_1"
            plc = _id(result, "plc")
            cr = _id(result, "relay")
            wires = _wires(result)
            assert any(plc + ".I0_3" in (w[0], w[1]) for w in wires)
            assert any(plc + ".Q0_1" in (w[0], w[1]) and cr + ".A1" in (w[0], w[1]) for w in wires)
            assert "hoa" in roles
        dialog.close()
    app.processEvents()


def test_agent_insert_circuit_schema_validates_each_kind():
    """AI tool schema lists every kind and argument name with a description, and validates each kind."""

    from panelcad.cli.agent import INSERT_CIRCUIT_SCHEMA, Agent
    from panelcad.core.circuit_builder import KINDS, validate_insert_circuit_tool

    schema = INSERT_CIRCUIT_SCHEMA
    assert schema["op"] == "insert_circuit"
    assert schema["description"]
    required = (
        "timer_seconds",
        "overload_placement",
        "bypass",
        "hoa",
        "plc_model",
        "io_run",
        "io_ol",
        "io_auto",
        "io_out",
        "kind",
        "control_wiring",
        "starter",
        "pilot_lights",
    )
    for name in required:
        assert name in schema["args"], name
        assert schema["args"][name]["description"], name
    for kind in KINDS:
        assert kind in schema["kinds"], kind
        assert schema["kinds"][kind]["description"], kind
        normalized = validate_insert_circuit_tool({"kind": kind, "control": "local", "drive": "generic_1k5"})
        assert normalized["kind"] == kind

    validate_insert_circuit_tool(
        {
            "kind": "star_delta",
            "timer_seconds": 8,
            "overload_placement": "line",
            "control": "local",
        }
    )
    validate_insert_circuit_tool(
        {
            "kind": "plc_motor",
            "hoa": True,
            "plc_model": "nema.plc.compact_16dio",
            "io_run": "I0_0",
            "io_out": "Q0_0",
            "control": "local",
        }
    )
    with pytest.raises(ValueError, match="unknown kind"):
        validate_insert_circuit_tool({"kind": "softstart"})
    with pytest.raises(ValueError, match="unknown overload_placement"):
        validate_insert_circuit_tool({"kind": "star_delta", "overload_placement": "side"})
    with pytest.raises(ValueError, match="unknown plc_model"):
        validate_insert_circuit_tool({"kind": "plc_motor", "plc_model": "siemens.s7"})

    agent = Agent()
    help_result = agent.handle({"id": 1, "op": "help"})
    assert help_result["ok"] is True
    tool = help_result["result"]["schemas"]["insert_circuit"]
    help_text = help_result["result"]["help"]["insert_circuit"]
    for kind in KINDS:
        assert kind in tool["kinds"]
        assert tool["kinds"][kind]["description"]
        assert kind in help_text
    for name in required:
        assert name in tool["args"]
        assert tool["args"][name]["description"]
        assert name in help_text


@pytest.mark.confirm_generic_layout
def test_cli_electrical_circuit(tmp_path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    path = tmp_path / "job.pcad.yaml"
    Session.create("CLI").save(path)
    runner = CliRunner()
    done = runner.invoke(app, ["electrical", "circuit", str(path), "--kind", "dol", "--kw", "7.5"])
    assert done.exit_code == 0, done.output
    assert "NEMA/ANSI" in done.output
    session = Session.open(path)
    libs = {c.library_id for c in session.sheet().components}
    assert "nema.contactors.ac3_32a" in libs
    assert "nema.protection.overload" in libs
    panel = next(sheet for sheet in session.project.sheets if sheet.kind == "panel_layout")
    assert {c.id for c in panel.components} >= {"CB1", "M1", "OL1"}


def test_empty_standard_defaults_to_nema():
    from panelcad.core.circuit_builder import DEFAULT_STANDARD, resolve_circuit_standard

    assert DEFAULT_STANDARD == "NEMA/ANSI"
    session = Session.create("Default")
    assert session.project.title_block.standard == "NEMA/ANSI"
    session.project.title_block.standard = ""
    assert resolve_circuit_standard(session) == "NEMA/ANSI"
    result = session.insert_circuit(kind="dol")
    assert result["standard"] == "NEMA/ANSI"
    assert _lib(result, "breaker") == "nema.breakers.mcb_3p_32a"


def test_nema_and_jic_use_nema_symbols_and_pins():
    session = Session.create("NEMA DOL")
    session.set_title_block(standard="NEMA")
    result = session.insert_circuit(kind="dol")
    assert result["standard"] == "NEMA/ANSI"
    assert _lib(result, "breaker") == "nema.breakers.mcb_3p_32a"
    assert _lib(result, "contactor") == "nema.contactors.ac3_9a"
    assert _lib(result, "overload") == "nema.protection.overload"
    assert _lib(result, "motor") == "nema.loads.motor_3ph"
    wires = _wires(result)
    assert ("CB1.2", "M1.L1", "L1") in wires
    assert ("M1.T1", "OL1.T1", "T1") in wires
    assert ("OL1.2", "MTR1.T1", "T1") in wires
    assert session.sheet().component("MTR1").properties["kW"] == 1.5
    _assert_no_overlap(session)

    jic = Session.create("JIC")
    jic.set_title_block(standard="JIC")
    result = jic.insert_circuit(kind="dol", kw=7.5)
    assert result["standard"] == "NEMA/ANSI"
    assert _lib(result, "contactor") == "nema.contactors.ac3_32a"
    assert _lib(result, "breaker") == "nema.breakers.mcb_3p_32a"

    big = Session.create("MCCB")
    big.set_title_block(standard="ANSI")
    result = big.insert_circuit(kind="dol", kw=18)
    assert _lib(result, "breaker") == "nema.breakers.mccb_3p_100a"


def test_sheet_standard_overrides_project():
    session = Session.create("Sheet IEC")
    assert session.project.title_block.standard == "NEMA/ANSI"
    session.sheet().standard = "IEC"
    result = session.insert_circuit(kind="dol")
    assert result["standard"] == "IEC"
    assert _lib(result, "motor") == "iec.loads.motor_3ph"


def test_nema_vfd_and_local_control():
    session = Session.create("NEMA VFD")
    session.set_title_block(standard="NEMA/ANSI")
    result = session.insert_circuit(kind="vfd", drive="generic_1k5")
    assert _lib(result, "drive") == "nema.drives.vfd_1k5"
    assert _lib(result, "motor") == "nema.loads.motor_3ph"
    assert _lib(result, "breaker") == "nema.breakers.mcb_3p_32a"
    assert not any(c["role"] == "overload" for c in result["components"])
    pins = _drive_pins(result)
    assert {"L1", "L2", "L3", "U", "V", "W"} <= pins

    local = Session.create("NEMA local")
    local.set_title_block(standard="NEMA")
    result = local.insert_circuit(kind="dol", control="local")
    roles = {c["role"] for c in result["components"]}
    assert {"psu", "estop", "stop", "start", "contactor"} <= roles
    assert _lib(result, "psu") == "nema.power.psu_24v_5a"
    assert _lib(result, "estop") == "nema.operators.estop"
    assert any(w["to"].endswith(".A1") or w["from"].endswith(".A1") for w in result["wires"])
    ctrl = result["control_sheet"]
    ys = [
        _pin_y(local, _id(result, "estop"), "1", ctrl),
        _pin_y(local, _id(result, "stop"), "1", ctrl),
        _pin_y(local, _id(result, "start"), "3", ctrl),
    ]
    assert max(ys) - min(ys) < 0.2
    assert _pin_y(local, _id(result, "psu"), "plus", ctrl) + 4.0 < min(ys)
    _assert_seals_parallel_start(local, ctrl)
    _assert_no_overlap(local, result["sheet"])
    _assert_no_overlap(local, ctrl)

    oem = Session.create("NEMA OEM drive")
    oem.set_title_block(standard="NEMA")
    result = oem.insert_circuit(kind="vfd", drive="gs20_0p5", branch="breaker")
    assert _lib(result, "drive") == "adc.durapulse.gs21_20p5"
    breakers = [c for c in result["components"] if c["role"] == "breaker"]
    assert {c["library_id"] for c in breakers} == {"nema.breakers.mcb_1p_16a"}

    disc = Session.create("NEMA disc")
    disc.set_title_block(standard="NEMA")
    result = disc.insert_circuit(kind="vfd", drive="es1_0p75", branch="disconnect")
    used = set()
    disc_id = _id(result, "disconnect")
    for wire in result["wires"]:
        for end in (wire["from"], wire["to"]):
            ref, pin = end.split(".", 1)
            if ref == disc_id:
                used.add(pin)
    assert used == {"T1", "T2"}


@pytest.mark.confirm_generic_layout
def test_insert_packs_layout_and_bom_share_tags():
    from panelcad.core.electrical import audit_schematic_panel
    from panelcad.io.bom import instance_rows

    session = _iec("Layout BOM")
    result = session.insert_circuit(kind="dol")
    panel_id = result["layout"]["sheet"]
    panel = session.project.sheet(panel_id)
    assert panel.kind == "panel_layout"
    schematic_ids = {c["id"] for c in result["components"]}
    panel_ids = {c.id for c in panel.components}
    assert {"Q1", "K1", "F1"} <= panel_ids
    assert panel_ids <= schematic_ids
    assert "M1" not in panel_ids
    for comp in panel.components:
        sch = session.sheet(result["sheet"]).component(comp.id)
        assert sch.library_id == comp.library_id

    rows = instance_rows(session.project, session.catalog)
    q_rows = [row for row in rows if row["ref"] == "Q1"]
    assert len(q_rows) == 1
    assert set(q_rows[0]["sheet"].split(",")) >= {result["sheet"], panel_id}
    assert q_rows[0]["library_id"] == "iec.breakers.mcb_3p_32a"

    issues = {(row["ref"], row["issue"]) for row in audit_schematic_panel(session.project, session.catalog)}
    assert ("Q1", "missing_panel") not in issues
    assert ("K1", "missing_panel") not in issues
    assert ("F1", "missing_panel") not in issues
    assert session.assign_item_numbers() >= 4
    assert session.sheet(result["sheet"]).component("Q1").properties.get("item")
    assert panel.component("Q1").properties.get("item")


def test_new_sheet_does_not_reuse_project_tags():
    session = _iec("Tags")
    session.place("iec.breakers.mcb_3p_32a", ref="Q1", x=10, y=10)
    result = session.insert_circuit(kind="dol", new_sheet=True)
    assert _id(result, "breaker") != "Q1"
    assert _id(result, "contactor") == "K1"
    ids = [comp.id for sheet in session.project.sheets for comp in sheet.components]
    # Same tag may appear on schematic and layout, but each sheet keeps unique ids.
    by_sheet = {sheet.id: [comp.id for comp in sheet.components] for sheet in session.project.sheets}
    for sheet_ids in by_sheet.values():
        assert len(sheet_ids) == len(set(sheet_ids))
    assert ids.count("Q1") >= 1
    assert _id(result, "breaker") in ids


def test_one_undo_removes_created_panel_sheet():
    session = _iec("Undo panel")
    session.insert_circuit(kind="dol")
    assert any(sheet.kind == "panel_layout" for sheet in session.project.sheets)
    assert session.undo()
    assert [sheet.kind for sheet in session.project.sheets] == ["schematic"]
    assert session.sheet().components == []


@pytest.mark.confirm_generic_layout
def test_insert_does_not_wipe_hand_placed_panel():
    session = _iec("Keep layout")
    session.add_sheet("enc", title="Enclosure", kind="panel_layout")
    rail = session.sheet("enc").rails[0]
    hand = session.place(
        "iec.power.psu_24v_5a",
        ref="HAND",
        x=rail.x + 4,
        y=rail.y,
        sheet_id="enc",
    )
    x0, y0 = hand.x, hand.y
    session.set_active_sheet("main")
    result = session.insert_circuit(kind="dol")
    enc = session.project.sheet("enc")
    kept = enc.component("HAND")
    assert kept.x == pytest.approx(x0)
    assert kept.y == pytest.approx(y0)
    assert kept.library_id == "iec.power.psu_24v_5a"
    assert {"HAND", "Q1", "K1", "F1"} <= {c.id for c in enc.components}
    assert "M1" not in {c.id for c in enc.components}
    assert result["layout"]["sheet"] == "enc"


@pytest.mark.confirm_generic_layout
def test_local_control_lands_on_door_and_clears_audit():
    from panelcad.core.electrical import audit_schematic_panel

    session = _iec("Door ops")
    result = session.insert_circuit(kind="dol", control="local")
    door_id = result["layout"]["door"]
    assert door_id
    door = session.project.sheet(door_id)
    assert door.kind == "door_layout"
    door_ids = {c.id for c in door.components}
    assert {_id(result, "estop"), _id(result, "stop"), _id(result, "start")} <= door_ids
    assert _id(result, "psu") not in door_ids
    panel = session.project.sheet(result["layout"]["sheet"])
    assert _id(result, "psu") in {c.id for c in panel.components}
    issues = {row["issue"] for row in audit_schematic_panel(session.project, session.catalog)}
    assert "missing_door" not in issues
    assert session.undo()
    assert not any(sheet.kind == "door_layout" for sheet in session.project.sheets)


@pytest.mark.confirm_generic_layout
def test_tight_enclosure_warns_and_leaves_existing():
    session = _iec("Tight")
    session.add_sheet("enc", title="Enclosure", kind="panel_layout")
    enc = session.sheet("enc")
    enc.enclosure.width_mm = 30
    enc.enclosure.height_mm = 30
    enc.rails[0].length_mm = 18
    keep = session.place(
        "iec.terminals.x_2p5",
        ref="XKEEP",
        x=enc.rails[0].x,
        y=enc.rails[0].y,
        sheet_id="enc",
    )
    x0, y0 = keep.x, keep.y
    session.set_active_sheet("main")
    result = session.insert_circuit(kind="dol")
    assert result["layout"]["warnings"]
    assert enc.component("XKEEP").x == pytest.approx(x0)
    assert enc.component("XKEEP").y == pytest.approx(y0)


def test_schematic_ink_rect_counts_tag_and_pin_dots():
    session = Session.create("ink tag")
    session.place("nema.breakers.mcb_3p_32a", ref="CB1", x=40, y=22)
    sheet = session.sheet()
    comp = sheet.component("CB1")
    device = session.catalog.get(comp.library_id)
    body = component_aabb(comp.x, comp.y, device.schematic_width, device.schematic_height)
    ink = schematic_ink_rect(device, comp.x, comp.y, comp.id, component=comp)
    assert ink[1] < body[1] - 2.0
    assert ink[3] >= body[3]
    assert ink[0] <= body[0]
    assert ink[2] >= body[2]


def test_nema_and_iec_dol_and_vfd_stay_in_drawing_area():
    """Power column — including tags, labels, and line stubs — stays in working_rect."""

    cases = [
        Session.create("NEMA DOL fit"),
        Session.create("NEMA DOL 18"),
        Session.create("NEMA VFD fit"),
        _iec("IEC DOL fit"),
        _iec("IEC VFD fit"),
    ]
    args = [
        {"kind": "dol"},
        {"kind": "dol", "kw": 18},
        {"kind": "vfd", "drive": "generic_1k5"},
        {"kind": "dol"},
        {"kind": "vfd", "drive": "generic_1k5"},
    ]
    for session, kwargs in zip(cases, args, strict=True):
        result = session.insert_circuit(**kwargs)
        _assert_inside_working(session, result["sheet"])
        _assert_no_overlap(session, result["sheet"])
        sheet = session.sheet(result["sheet"])
        wx, wy, ww, wh = working_rect(sheet)
        motor = next(c for c in result["components"] if c["role"] == "motor")
        m = sheet.component(motor["id"])
        device = session.catalog.get(m.library_id)
        ink = schematic_ink_rect(device, m.x, m.y, m.id, component=m)
        assert ink[3] <= wy + wh
        top = next(c for c in result["components"] if c["role"] in {"breaker", "disconnect"})
        head = sheet.component(top["id"])
        head_ink = schematic_ink_rect(
            session.catalog.get(head.library_id), head.x, head.y, head.id, component=head
        )
        assert head_ink[1] >= wy
        assert head.y >= wy
        # Sliding the body to the zone strip is the regression (tag / supply stubs).
        assert head.y > wy + 4.0


def _roles(result: dict) -> set[str]:
    return {c["role"] for c in result["components"]}


def _phase_numbers(session: Session, sheet_id: str, nets: set[str]) -> set[str]:
    numbers = set()
    for wire in session.sheet(sheet_id).wires:
        if (wire.net or "") in nets and wire.number:
            numbers.add(wire.number)
    return numbers


def _assert_new_kind(session: Session, result: dict, *, roles: set[str], iec: bool = False) -> None:
    assert roles <= _roles(result)
    power = result["sheet"]
    control = result.get("control_sheet")
    assert control
    assert control != power
    _assert_inside_working(session, power)
    _assert_inside_working(session, control)
    _assert_no_overlap(session, power)
    _assert_no_overlap(session, control)
    ids = [c.id for sheet in session.project.sheets for c in sheet.components]
    by_sheet = {sheet.id: [c.id for c in sheet.components] for sheet in session.project.sheets}
    for sheet_ids in by_sheet.values():
        assert len(sheet_ids) == len(set(sheet_ids))
    panel = session.project.sheet(result["layout"]["sheet"])
    assert panel.kind == "panel_layout"
    door = session.project.sheet(result["layout"]["door"])
    assert door.kind == "door_layout"
    prefix = "iec." if iec else "nema."
    assert any(c["library_id"].startswith(prefix) or not c["library_id"].startswith(("iec.", "nema.")) for c in result["components"])
    numbers = _phase_numbers(session, power, {"L1", "L2", "L3"})
    if numbers:
        assert len(numbers) == 3, numbers
    motor = session.sheet(power).component(_id(result, "motor"))
    if iec:
        assert motor.properties.get("kW") or motor.properties.get("hp")
    else:
        assert motor.properties.get("FLA")
    if result.get("io_sheet"):
        _assert_inside_working(session, result["io_sheet"])
        _assert_no_overlap(session, result["io_sheet"])
    bom_refs = {row["ref"] for row in instance_rows(session.project, session.catalog)}
    assert _id(result, "motor") in bom_refs
    for sheet in session.project.sheets:
        if sheet.kind not in ("schematic", "io"):
            continue
        for comp in sheet.components:
            parent = str(comp.properties.get("parent") or "")
            if not parent:
                continue
            device = session.catalog.get(comp.library_id)
            assert family_key(comp, device) == parent, (comp.id, parent)


@pytest.mark.confirm_generic_layout
def test_reversing_nema_and_iec_complete_insert():
    session = Session.create("NEMA reversing")
    result = session.insert_circuit(kind="reversing", control="local", hp=10, voltage=480, pilot_lights=True)
    assert result["kind"] == "reversing"
    assert result["standard"] == "NEMA/ANSI"
    assert _id(result, "fwd") == "M1"
    assert _id(result, "rev") == "M2"
    assert _id(result, "fwd_coil") == "M1"
    assert _id(result, "rev_coil") == "M2"
    assert _id(result, "fwd_seal") == "M1-13"
    assert _id(result, "rev_ilock") == "M2-21"
    assert _lib(result, "fwd") == "nema.contactors.ac3_32a"
    assert _lib(result, "motor") == "nema.loads.motor_3ph"
    wires = _wires(result)
    assert any(w[2] == "T3" and "M2" in w[0] + w[1] for w in wires)
    door = session.project.sheet(result["layout"]["door"])
    assert {_id(result, "fwd_pb"), _id(result, "rev_pb"), _id(result, "stop")} <= {c.id for c in door.components}
    _assert_new_kind(session, result, roles={"fwd", "rev", "overload", "motor", "plc"} - {"plc"} | {"estop", "fwd_pb"})
    assert session.sheet(_id(result, "fwd") and result["sheet"]).component("M1").properties.get("interlock")

    iec = _iec("IEC reversing")
    result = iec.insert_circuit(kind="reversing", control="local", kw=1.5)
    assert result["standard"] == "IEC"
    assert _lib(result, "fwd") == "iec.contactors.ac3_9a"
    assert _id(result, "fwd") == "K1"
    assert _id(result, "fwd_coil") == "K1"
    _assert_new_kind(iec, result, roles={"fwd", "rev", "overload"}, iec=True)


@pytest.mark.confirm_generic_layout
def test_star_delta_winding_overload_and_six_lead_motor():
    session = Session.create("NEMA wye-delta")
    result = session.insert_circuit(kind="star_delta", control="local", hp=15, voltage=460)
    assert result["overload_placement"] == "delta_windings"
    assert "0.58" in result["overload_note"]
    assert _lib(result, "motor") == "nema.loads.motor_3ph_6lead"
    ol = session.sheet(result["sheet"]).component(_id(result, "overload"))
    assert ol.properties["placement"] == "delta_windings"
    assert ol.properties["ol_setpoint"] == pytest.approx(12.18, rel=0.05)
    assert {_id(result, "main"), _id(result, "star"), _id(result, "delta")} == {"M1", "M2", "M3"}
    assert _id(result, "main_coil") == "M1"
    assert _id(result, "timer").startswith("TR")
    wires = _wires(result)
    assert any(w[2] == "STAR" for w in wires)
    motor = _id(result, "motor")
    ends = {w[2] for w in wires if motor in w[0] or motor in w[1]}
    assert {"T6", "T4", "T5"} & ends or {"T1", "T2", "T3"} <= ends
    _assert_new_kind(session, result, roles={"main", "star", "delta", "timer", "overload"})

    line = Session.create("NEMA wye-delta line")
    result = line.insert_circuit(
        kind="star_delta", control="local", hp=15, voltage=460, overload_placement="line", timer_seconds=8
    )
    assert result["overload_placement"] == "line"
    assert result["timer_seconds"] == pytest.approx(8.0)
    ol = line.sheet(result["sheet"]).component(_id(result, "overload"))
    assert ol.properties["placement"] == "line"
    assert ol.properties["ol_factor"] == pytest.approx(1.0)
    assert ol.properties["ol_setpoint"] == pytest.approx(21.0, rel=0.05)

    iec = _iec("IEC star-delta")
    result = iec.insert_circuit(kind="wye_delta", control="local", kw=7.5)
    assert _lib(result, "motor") == "iec.loads.motor_3ph_6lead"
    assert _id(result, "main") == "K1"
    wires = _wires(result)
    motor = _id(result, "motor")
    assert any("U2" in (w[0] + w[1]) or "V2" in (w[0] + w[1]) for w in wires)
    _assert_new_kind(iec, result, roles={"main", "star", "delta"}, iec=True)


@pytest.mark.confirm_generic_layout
def test_soft_starter_three_wire_bypass_and_two_wire():
    session = Session.create("NEMA SS")
    result = session.insert_circuit(
        kind="soft_starter",
        control="local",
        bypass=True,
        control_wiring="3wire",
        hp=10,
        pilot_lights=True,
    )
    assert _lib(result, "softstarter") == "nema.drives.softstarter"
    assert _id(result, "softstarter") == "SS1"
    assert _id(result, "bypass") == "M1"
    assert _id(result, "bypass_coil") == "M1"
    assert {"start", "stop", "pilot_run", "pilot_fault"} <= _roles(result)
    _assert_new_kind(session, result, roles={"softstarter", "bypass", "motor"})

    two = Session.create("NEMA SS 2wire")
    result = two.insert_circuit(kind="soft_starter", control="local", control_wiring="2wire")
    assert "selector" in _roles(result)
    assert "start" not in _roles(result)
    _assert_inside_working(two, result["sheet"])
    _assert_inside_working(two, result["control_sheet"])

    iec = _iec("IEC SS")
    result = iec.insert_circuit(kind="soft_starter", control="local")
    assert _lib(result, "softstarter") == "iec.drives.softstarter"
    _assert_new_kind(iec, result, roles={"softstarter", "motor"}, iec=True)


def test_soft_starter_bypass_load_wires_do_not_join_phases():
    """A fresh bypass insert must not draw one phase into another.

    NEMA motor pins line up with the soft starter, so an unstored T2 drop
    autoroutes a corner onto the bypass shelf of T1. IEC already stores that
    shelf clear of the other phase. Both standards have to pass the same
    segment check a converted sheet uses.
    """

    for standard in ("NEMA/ANSI", "IEC"):
        if standard == "IEC":
            session = _iec(f"ss bypass {standard}")
        else:
            session = Session.create(f"ss bypass {standard}")
        result = session.insert_circuit(
            kind="soft_starter",
            control="local",
            standard=standard,
            bypass=True,
        )
        _assert_drawn_segments_match_pins(session, result["sheet"], mix_numbers=True)


@pytest.mark.confirm_generic_layout
def test_plc_motor_hoa_and_interposing_relay():
    session = Session.create("NEMA PLC DOL")
    result = session.insert_circuit(kind="plc_motor", control="local", hp=5, voltage=480)
    assert _lib(result, "plc") == "nema.plc.compact_16dio"
    assert _lib(result, "relay") == "nema.relays.k_4co_24vdc"
    assert _id(result, "contactor") == "M1"
    assert _id(result, "starter_coil") == "M1"
    assert _id(result, "run_fb") == "M1-14"
    assert _id(result, "ol_contact") == "OL1-95"
    plc = _id(result, "plc")
    cr = _id(result, "relay")
    wires = _wires(result)
    assert any(plc + ".Q0_0" in (w[0], w[1]) and cr + ".A1" in (w[0], w[1]) for w in wires)
    assert any(plc + ".I0_0" in (w[0], w[1]) for w in wires)
    assert any(plc + ".I0_1" in (w[0], w[1]) for w in wires)
    assert any(plc + ".I0_2" in (w[0], w[1]) for w in wires)
    hoa = _id(result, "hoa")
    assert any(hoa + "." in w[0] + w[1] and "HAND" in w[2] for w in wires)
    assert any("I0_2" in w[0] + w[1] and "AUTO" in w[2] for w in wires)
    door = session.project.sheet(result["layout"]["door"])
    assert hoa in {c.id for c in door.components}
    _assert_new_kind(session, result, roles={"plc", "relay", "hoa", "contactor"})

    vfd = Session.create("NEMA PLC VFD")
    result = vfd.insert_circuit(kind="plc_motor", starter="vfd", drive="generic_1k5", control="local")
    assert _lib(result, "drive") == "nema.drives.vfd_1k5"
    assert "overload" not in _roles(result)
    _assert_inside_working(vfd, result["sheet"])
    _assert_inside_working(vfd, result["control_sheet"])

    iec = _iec("IEC PLC")
    result = iec.insert_circuit(kind="plc_motor", control="local")
    assert _lib(result, "plc") == "iec.plc.compact_16dio"
    assert _id(result, "contactor") == "K1"
    _assert_new_kind(iec, result, roles={"plc", "relay"}, iec=True)


@pytest.mark.confirm_generic_layout
def test_new_kinds_do_not_wipe_and_undo():
    session = _iec("Keep extra")
    session.place("iec.operators.pb_no", ref="S9", x=8, y=8)
    session.add_sheet("enc", title="Enclosure", kind="panel_layout")
    rail = session.sheet("enc").rails[0]
    hand = session.place("iec.power.psu_24v_5a", ref="HAND", x=rail.x + 4, y=rail.y, sheet_id="enc")
    x0, y0 = hand.x, hand.y
    session.set_active_sheet("main")
    result = session.insert_circuit(kind="reversing", control="local")
    assert session.sheet("main").component("S9").library_id == "iec.operators.pb_no"
    kept = session.project.sheet("enc").component("HAND")
    assert kept.x == pytest.approx(x0)
    assert kept.y == pytest.approx(y0)
    assert result["layout"]["sheet"] == "enc"
    assert session.undo()
    assert [c.id for c in session.sheet("main").components] == ["S9"]
    assert {c.id for c in session.project.sheet("enc").components} == {"HAND"}
    assert not any(sheet.title == "Motor control" for sheet in session.project.sheets)


def test_agent_and_cli_new_kinds(tmp_path):
    from typer.testing import CliRunner

    from panelcad.cli.agent import Agent
    from panelcad.cli.main import app

    agent = Agent()
    agent.handle({"id": 1, "op": "create_project", "name": "Agent extra"})
    result = agent.handle(
        {"id": 2, "op": "insert_circuit", "kind": "star_delta", "control": "local", "hp": 10}
    )
    assert result["ok"] is True, result
    assert result["result"]["kind"] == "star_delta"
    assert result["result"]["control_sheet"]
    help_result = agent.handle({"id": 3, "op": "help"})
    assert "star_delta" in help_result["result"]["help"]["insert_circuit"]

    path = tmp_path / "ss.pcad.yaml"
    Session.create("CLI SS").save(path)
    runner = CliRunner()
    done = runner.invoke(
        app,
        ["electrical", "circuit", str(path), "--kind", "soft_starter", "--control", "local", "--bypass"],
    )
    assert done.exit_code == 0, done.output
    session = Session.open(path)
    assert any("softstarter" in c.library_id for c in session.sheet().components)


def test_new_kinds_stay_in_drawing_area_nema_and_iec():
    cases = [
        (Session.create("fit rev"), {"kind": "reversing", "control": "local", "pilot_lights": True}),
        (Session.create("fit sd"), {"kind": "star_delta", "control": "local", "kw": 18}),
        (Session.create("fit ss"), {"kind": "soft_starter", "control": "local", "bypass": True}),
        (Session.create("fit plc"), {"kind": "plc_motor", "control": "local"}),
        (_iec("fit rev iec"), {"kind": "reversing", "control": "local"}),
    ]
    for session, kwargs in cases:
        result = session.insert_circuit(**kwargs)
        _assert_inside_working(session, result["sheet"])
        _assert_inside_working(session, result["control_sheet"])
        _assert_no_overlap(session, result["sheet"])
        _assert_no_overlap(session, result["control_sheet"])


def test_unknown_kind_softstart_still_rejected():
    session = Session.create("still unknown")
    with pytest.raises(ValueError, match="unknown kind"):
        session.insert_circuit(kind="softstart")
    with pytest.raises(ValueError, match="unknown control"):
        session.insert_circuit(kind="dol", control="plc")


def test_insert_circuit_refuses_single_phase(tmp_path):
    """phases=1 is a ValueError on Session, JSONL, and the CLI. phases=3 still draws."""

    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session = Session.create("phases")
    with pytest.raises(ValueError, match="single-phase circuits are not drawn yet"):
        session.insert_circuit(kind="dol", hp=1, voltage=115, phases=1)
    assert session.can_undo() is False
    drawn = session.insert_circuit(kind="dol", hp=5, voltage=460, phases=3)
    assert drawn["components"]

    agent = Agent()
    agent.session = Session.create("agent phases")
    refused = agent.handle({"op": "insert_circuit", "kind": "dol", "hp": 1, "voltage": 115, "phases": 1})
    assert refused["ok"] is False
    assert "single-phase circuits are not drawn yet" in refused["error"]
    assert agent.session.can_undo() is False
    accepted = agent.handle({"op": "insert_circuit", "kind": "dol", "hp": 5, "voltage": 460, "phases": 3})
    assert accepted["ok"] is True

    path = tmp_path / "phases.pcad.yaml"
    Session.create("cli phases").save(path)
    runner = CliRunner()
    denied = runner.invoke(app, ["electrical", "circuit", str(path), "--kind", "dol", "--phases", "1"])
    assert denied.exit_code != 0
    assert "single-phase circuits are not drawn yet" in (denied.output + denied.stderr)
    kept = Session.open(path)
    assert not any(comp.properties.get("role") == "motor" for sheet in kept.project.sheets for comp in sheet.components)


def test_insert_circuit_refuses_non_integer_phases(tmp_path):
    """phases=3.6 is not truncated to 3. 3, 3.0, \"3\", and \"3.0\" still draw."""

    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session = Session.create("fractional phases")
    with pytest.raises(ValueError, match="phases must be an integer"):
        session.insert_circuit(kind="dol", hp=5, voltage=460, phases=3.6)
    assert session.can_undo() is False
    with pytest.raises(ValueError, match="Circuit Builder draws 3-phase circuits only"):
        session.insert_circuit(kind="dol", hp=5, voltage=460, phases=2)
    assert session.can_undo() is False
    drawn = session.insert_circuit(kind="dol", hp=5, voltage=460, phases=3.0)
    assert drawn["components"]
    text_three = session.insert_circuit(kind="dol", hp=5, voltage=460, phases="3.0", new_sheet=True)
    assert text_three["components"]
    with pytest.raises(ValueError, match="phases must be an integer"):
        session.insert_circuit(kind="dol", hp=5, voltage=460, phases="3.6", new_sheet=True)

    agent = Agent()
    agent.session = Session.create("agent fractional phases")
    refused = agent.handle({"op": "insert_circuit", "kind": "dol", "hp": 5, "voltage": 460, "phases": 3.6})
    assert refused["ok"] is False
    assert "phases must be an integer" in refused["error"]
    assert agent.session.can_undo() is False
    refused_text = agent.handle({"op": "insert_circuit", "kind": "dol", "hp": 5, "voltage": 460, "phases": "3.6"})
    assert refused_text["ok"] is False
    assert "phases must be an integer" in refused_text["error"]
    accepted = agent.handle({"op": "insert_circuit", "kind": "dol", "hp": 5, "voltage": 460, "phases": "3"})
    assert accepted["ok"] is True
    accepted_text = agent.handle(
        {"op": "insert_circuit", "kind": "dol", "hp": 5, "voltage": 460, "phases": "3.0", "new_sheet": True}
    )
    assert accepted_text["ok"] is True
    again = Agent()
    again.session = Session.create("agent float three")
    accepted_float = again.handle({"op": "insert_circuit", "kind": "dol", "hp": 5, "voltage": 460, "phases": 3.0})
    assert accepted_float["ok"] is True

    path = tmp_path / "fractional.pcad.yaml"
    Session.create("cli fractional").save(path)
    runner = CliRunner()
    denied = runner.invoke(app, ["electrical", "circuit", str(path), "--kind", "dol", "--phases", "3.6"])
    assert denied.exit_code != 0
    assert "phases must be an integer" in (denied.output + denied.stderr)
    kept = Session.open(path)
    assert not any(comp.properties.get("role") == "motor" for sheet in kept.project.sheets for comp in sheet.components)
    allowed = runner.invoke(
        app, ["electrical", "circuit", str(path), "--kind", "dol", "--phases", "3.0", "--hp", "5", "--voltage", "460"]
    )
    assert allowed.exit_code == 0, allowed.output + allowed.stderr
    opened = Session.open(path)
    assert any(comp.properties.get("role") == "motor" for sheet in opened.project.sheets for comp in sheet.components)


_RETURN_NETS = {"0V", "0VDC", "L2", "X2", "N", "NEUTRAL"}


def _walk_refs(session: Session, sheet_id: str, start: str) -> set[str]:
    sheet = session.sheet(sheet_id)
    seen = {start}
    frontier = [start]
    while frontier:
        cur = frontier.pop()
        for wire in sheet.wires:
            ends = []
            for raw in (wire.frm, wire.to):
                token = raw.split(".", 1)[0]
                ends.append(token)
            if cur not in ends:
                continue
            for token in ends:
                if token in seen or token in {"24V", "0V", "X1", "X2", "L1", "L2"}:
                    continue
                seen.add(token)
                frontier.append(token)
    return seen


def test_coil_line_side_is_not_return_net():
    cases = [
        (Session.create("nema rev"), {"kind": "reversing", "control": "local", "pilot_lights": True}),
        (Session.create("nema sd"), {"kind": "star_delta", "control": "local"}),
        (Session.create("nema ss"), {"kind": "soft_starter", "control": "local", "bypass": True}),
        (Session.create("nema plc"), {"kind": "plc_motor", "control": "local"}),
        (_iec("iec rev"), {"kind": "reversing", "control": "local"}),
    ]
    for session, kwargs in cases:
        result = session.insert_circuit(**kwargs)
        for sheet in session.project.sheets:
            if sheet.kind != "schematic":
                continue
            for comp in sheet.components:
                lib = comp.library_id.lower()
                if "coil" not in lib and "timers." not in lib and "relays." not in lib:
                    continue
                for wire in sheet.wires:
                    if f"{comp.id}.A1" not in (wire.frm, wire.to):
                        continue
                    net = str(wire.net or "").upper().replace(" ", "")
                    other = wire.to if wire.frm.endswith(".A1") else wire.frm
                    assert net not in _RETURN_NETS, (comp.id, wire.net, kwargs)
                    assert other.split(".", 1)[0].upper() not in _RETURN_NETS, (comp.id, other)


def test_star_delta_rung_has_timer_contacts():
    session = Session.create("sd rungs")
    result = session.insert_circuit(kind="star_delta", control="local", hp=15)
    star = _id(result, "star_coil")
    delta = _id(result, "delta_coil")
    timer = _id(result, "timer")
    assert _id(result, "delta") == "M2"
    assert _id(result, "star") == "M3"
    control = result["control_sheet"]
    star_path = _walk_refs(session, control, star)
    delta_path = _walk_refs(session, control, delta)
    assert f"{timer}-21" in star_path
    assert f"{_id(result, 'delta')}-21" in star_path
    assert f"{timer}-15" in delta_path
    assert f"{_id(result, 'star')}-21" in delta_path


def test_control_sheets_are_ladders():
    session = Session.create("ladder style")
    result = session.insert_circuit(kind="reversing", control="local")
    control = session.sheet(result["control_sheet"])
    assert control.drawing_style == "ladder"
    assert control.rail_hot == "24V"
    assert control.rail_return == "0V"
    power = session.sheet(result["sheet"])
    assert power.drawing_style != "ladder" or not power.drawing_style


_CONTACT_LIB_HINTS = (
    "operators.pb",
    "operators.estop",
    "operators.selector",
    "aux_no",
    "aux_nc",
    "ol_contact",
    "protection.ol_contact",
)
_RAIL_NUMBERS = {"24V", "0V", "X1", "X2", "L1", "L2", "L+", "M", "N"}


def _is_breaking_contact(comp) -> bool:
    lib = comp.library_id.lower()
    return any(token in lib for token in _CONTACT_LIB_HINTS)


def _pin_numbers(session: Session, sheet_id: str, ref: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for wire in session.sheet(sheet_id).wires:
        number = str(wire.number or "").strip()
        if not number:
            continue
        for end in (wire.frm, wire.to):
            if not end.startswith(ref + "."):
                continue
            found.setdefault(end.split(".", 1)[1], set()).add(number)
    return found


def _assert_contact_pins_break_numbers(session: Session, sheet_id: str) -> None:
    for comp in session.sheet(sheet_id).components:
        if not _is_breaking_contact(comp):
            continue
        pins = _pin_numbers(session, sheet_id, comp.id)
        if len(pins) < 2:
            continue
        ids = list(pins)
        for i, pin_a in enumerate(ids):
            for pin_b in ids[i + 1 :]:
                shared = (pins[pin_a] & pins[pin_b]) - _RAIL_NUMBERS
                assert not shared, (
                    sheet_id,
                    comp.id,
                    pin_a,
                    pin_b,
                    shared,
                    pins,
                )


def _assert_text_clear(session: Session, sheet_id: str) -> None:
    """Wire-number marks must not sit on each other or on a device tag."""

    from panelcad.core.ink import device_ref_layout
    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet
    from panelcad.render.wire_labels import boxes_overlap, mark_box, place_wire_marks, routes_share_run
    from panelcad.render.wiring import route_sheet_wires

    sheet = session.sheet(sheet_id)
    ladder = is_ladder_sheet(sheet) or is_iec_column_sheet(sheet)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=ladder, project=session.project)
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    labeled: list[tuple[str, str, object]] = []
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=ladder)
        if not at or not mark:
            continue
        box = mark_box(at[0], at[1], mark, align=at[2])
        for other_id, other in boxes:
            assert not boxes_overlap(box, other), (sheet_id, wire.id, mark, other_id)
        if mark not in _RAIL_NUMBERS:
            pts = routes.get(wire.id) or []
            for other_id, other_mark, other_pts in labeled:
                if other_mark == mark:
                    assert not routes_share_run(pts, other_pts), (
                        sheet_id,
                        mark,
                        wire.id,
                        other_id,
                        "same number printed twice on one segment",
                    )
            labeled.append((wire.id, mark, pts))
        boxes.append((wire.id, box))
    for comp in sheet.components:
        if int(comp.rotation or 0):
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            layout = device_ref_layout(device, comp)
            if layout is None:
                continue
            tx, ty, talign, size = layout
            tag = mark_box(tx, ty, comp.id, align=talign, size=size)
        else:
            tag = (comp.x - 0.8, comp.y - 5.6, comp.x + max(8.0, len(comp.id) * 2.0), comp.y - 0.3)
        for wid, box in boxes:
            assert not boxes_overlap(box, tag), (sheet_id, wid, comp.id, tag, box)


def test_control_contact_pins_have_distinct_wire_numbers():
    """Each contact / PB / selector / OL contact breaks the wire number (NEMA and IEC)."""

    jobs = [
        ({"kind": "dol", "control": "local"}, False),
        ({"kind": "reversing", "control": "local", "pilot_lights": True}, False),
        ({"kind": "star_delta", "control": "local"}, False),
        ({"kind": "soft_starter", "control": "local", "bypass": True, "pilot_lights": True}, False),
        ({"kind": "plc_motor", "control": "local"}, False),
        ({"kind": "dol", "control": "local"}, True),
        ({"kind": "reversing", "control": "local"}, True),
        ({"kind": "star_delta", "control": "local"}, True),
        ({"kind": "soft_starter", "control": "local", "bypass": True}, True),
        ({"kind": "plc_motor", "control": "local"}, True),
    ]
    for kwargs, iec in jobs:
        session = _iec(f"iec {kwargs['kind']}") if iec else Session.create(f"nema {kwargs['kind']}")
        result = session.insert_circuit(**kwargs)
        sheets = [result["sheet"]]
        if result.get("control_sheet"):
            sheets.append(result["control_sheet"])
        if result.get("io_sheet"):
            sheets.append(result["io_sheet"])
        for sheet_id in sheets:
            _assert_contact_pins_break_numbers(session, sheet_id)
        if result.get("control_sheet"):
            _assert_text_clear(session, result["control_sheet"])
        if result.get("io_sheet"):
            _assert_text_clear(session, result["io_sheet"])
        if kwargs["kind"] == "plc_motor" and result.get("control_sheet"):
            hoa = session.sheet(result["control_sheet"]).component(_id(result, "hoa"))
            labels = hoa.properties.get("pin_labels") or {}
            assert "H" in labels.values()
            assert "A" in labels.values()
            _assert_hoa_does_not_bypass_selector(session, result)


def _wired_pins(session: Session, sheet_id: str, ref: str) -> set[str]:
    found: set[str] = set()
    for wire in session.sheet(sheet_id).wires:
        for end in (wire.frm, wire.to):
            if end.startswith(ref + "."):
                found.add(end.split(".", 1)[1])
    return found


_CONTACT_PAIRS = (
    ("11", "12"),
    ("13", "14"),
    ("21", "22"),
    ("1", "2"),
    ("3", "4"),
    ("95", "96"),
    ("X1", "X2"),
)


def _assert_contact_pins_not_dangling(session: Session, sheet_id: str) -> None:
    """A used contact pair must have both pins landed. Spare unused poles may sit idle."""

    sheet = session.sheet(sheet_id)
    for comp in sheet.components:
        if not _is_breaking_contact(comp):
            continue
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        ids = {p.id for p in device.pins if p.side in {"left", "right"}}
        present = _wired_pins(session, sheet_id, comp.id)
        if len(ids) <= 2:
            for pid in ids:
                assert pid in present, (sheet_id, comp.id, pid, "dangling contact pin", present)
            continue
        if "selector" in comp.library_id.lower():
            for pid in ids:
                assert pid in present, (sheet_id, comp.id, pid, "dangling selector pin", present)
            continue
        for a, b in _CONTACT_PAIRS:
            if a in ids and b in ids and (a in present or b in present):
                assert a in present and b in present, (sheet_id, comp.id, a, b, present)


def _assert_seals_parallel_start(session: Session, sheet_id: str) -> None:
    """M1-13 left is the start-button in-node; right is the start-button out-node."""

    sheet = session.sheet(sheet_id)
    starts = [c for c in sheet.components if "pb_no" in c.library_id.lower()]
    seals = [
        c
        for c in sheet.components
        if "aux_no" in c.library_id.lower() and c.id.endswith("-13")
    ]
    for seal in seals:
        if not starts:
            continue
        start = min(starts, key=lambda item: abs(item.x - seal.x) + abs(item.y - seal.y))
        if abs(start.x - seal.x) > 14.0 and abs(start.y - seal.y) > 36.0:
            continue
        left_s, right_s = _in_out_pins(session, start)
        left_h, right_h = _in_out_pins(session, seal)
        start_nums = _pin_numbers(session, sheet_id, start.id)
        seal_nums = _pin_numbers(session, sheet_id, seal.id)
        shared_in = (start_nums.get(left_s) or set()) & (seal_nums.get(left_h) or set())
        shared_out = (start_nums.get(right_s) or set()) & (seal_nums.get(right_h) or set())
        if not shared_in:
            continue
        assert shared_out, (sheet_id, seal.id, start.id, start_nums, seal_nums)
        start_left = _pin_world(session, sheet_id, start.id, left_s)
        start_right = _pin_world(session, sheet_id, start.id, right_s)
        seal_left = _pin_world(session, sheet_id, seal.id, left_h)
        seal_right = _pin_world(session, sheet_id, seal.id, right_h)
        if abs(start_left[0] - seal_left[0]) < 2.0:
            assert abs(start_left[0] - seal_left[0]) < 0.4, (
                sheet_id,
                seal.id,
                "seal left pin is off the start vertical",
                start_left,
                seal_left,
            )
            assert abs(start_right[0] - seal_right[0]) < 0.4, (
                sheet_id,
                seal.id,
                "seal right pin is off the start vertical",
                start_right,
                seal_right,
            )
        else:
            assert abs(start_left[1] - seal_left[1]) < 0.6, (
                sheet_id,
                seal.id,
                "seal in-pin is off the start in-pin row",
                start_left,
                seal_left,
            )
            assert abs(start_right[1] - seal_right[1]) < 0.6, (
                sheet_id,
                seal.id,
                "seal out-pin is off the start out-pin row",
                start_right,
                seal_right,
            )


def _in_out_pins(session: Session, comp) -> tuple[str, str]:
    device = session.catalog.get(comp.library_id)
    left = next((p.id for p in device.pins if p.side == "left"), device.pins[0].id)
    right = next((p.id for p in device.pins if p.side == "right"), device.pins[-1].id)
    return left, right


def _assert_unique_sheet_numbers(session: Session, sheet_id: str) -> None:
    """Two electrically separate nets on one sheet must not share a number."""

    from panelcad.core.wire_numbering import wire_networks

    owners: dict[str, set[int]] = {}
    for index, items in enumerate(wire_networks(session.project, session.catalog)):
        for sheet, wire in items:
            if sheet.id != sheet_id:
                continue
            number = str(wire.number or "").strip()
            if not number or number in _RAIL_NUMBERS:
                continue
            owners.setdefault(number, set()).add(index)
    for number, found in sorted(owners.items()):
        assert len(found) == 1, (sheet_id, number, found)


def _assert_hoa_does_not_bypass_selector(session: Session, result: dict) -> None:
    """CR1-13 is fed from SEL1 Auto, not from the 203 node ahead of the selector."""

    sheet_id = result["control_sheet"]
    hoa = session.sheet(sheet_id).component(_id(result, "hoa"))
    cr = session.sheet(sheet_id).component(_id(result, "cr_no"))
    start = session.sheet(sheet_id).component(_id(result, "start"))
    pins = session.catalog.get(hoa.library_id)
    hand = next(p.id for p in pins.pins if p.side == "right" and p.id in {"2", "12"})
    auto = next(p.id for p in pins.pins if p.side == "right" and p.id in {"3", "14"})
    com = next(p.id for p in pins.pins if p.side == "left")
    hoa_nums = _pin_numbers(session, sheet_id, hoa.id)
    cr_nums = _pin_numbers(session, sheet_id, cr.id)
    start_nums = _pin_numbers(session, sheet_id, start.id)
    cr_in, cr_out = _in_out_pins(session, cr)
    start_in, start_out = _in_out_pins(session, start)
    assert hoa_nums.get(hand) and hoa_nums.get(auto)
    assert hoa_nums[hand] != hoa_nums[auto]
    assert not (hoa_nums.get(com) or set()) & (cr_nums.get(cr_in) or set()), (
        "CR1-13 must not sit on the SEL1 common / 203 node",
        hoa_nums,
        cr_nums,
    )
    assert (cr_nums.get(cr_in) or set()) & (hoa_nums.get(auto) or set())
    assert (cr_nums.get(cr_out) or set()) & (start_nums.get(start_out) or set())
    hoa_auto = _pin_world(session, sheet_id, hoa.id, auto)
    start_load = _pin_world(session, sheet_id, start.id, start_out)
    cr_left = _pin_world(session, sheet_id, cr.id, cr_in)
    cr_right = _pin_world(session, sheet_id, cr.id, cr_out)
    if abs(cr_left[0] - cr_right[0]) <= 0.6 and abs(cr_right[1] - cr_left[1]) > 4.0:
        assert abs(hoa_auto[1] - cr_left[1]) < 0.8, (hoa_auto, cr_left)
        assert abs(start_load[1] - cr_right[1]) < 0.8, (start_load, cr_right)
        assert abs(hoa_auto[0] - cr_left[0]) > 4.0, (hoa_auto, cr_left)
    else:
        assert hoa_auto[0] < cr_left[0] < cr_right[0] < start_load[0], (
            "CR1-13 must sit on the Auto branch between the A drop and the 205 tee",
            hoa_auto,
            cr_left,
            cr_right,
            start_load,
        )


def _pin_world(session: Session, sheet_id: str, ref: str, pin_id: str) -> tuple[float, float]:
    from panelcad.render.sheet import pin_world

    comp = session.sheet(sheet_id).component(ref)
    return pin_world(comp, session.catalog.get(comp.library_id).pin(pin_id))


def _pt_seg_dist(px: float, py: float, a, b) -> float:
    from math import hypot

    dx, dy = b.x - a.x, b.y - a.y
    length = hypot(dx, dy)
    if length < 1e-6:
        return hypot(px - a.x, py - a.y)
    t = max(0.0, min(1.0, ((px - a.x) * dx + (py - a.y) * dy) / (length * length)))
    return hypot(px - (a.x + t * dx), py - (a.y + t * dy))


def _seg_touch_point(a1, a2, b1, b2, *, tol: float) -> tuple[float, float] | None:
    for p, s, t in ((a1, b1, b2), (a2, b1, b2), (b1, a1, a2), (b2, a1, a2)):
        if _pt_seg_dist(p.x, p.y, s, t) <= tol:
            return (p.x, p.y)
    def _side(p, q, r) -> float:
        return (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x)

    da, db = _side(a1, a2, b1), _side(a1, a2, b2)
    dc, dd = _side(b1, b2, a1), _side(b1, b2, a2)
    if da * db > 0 or dc * dd > 0:
        return None
    denom = (a2.x - a1.x) * (b2.y - b1.y) - (a2.y - a1.y) * (b2.x - b1.x)
    if abs(denom) < 1e-6:
        return None
    t = ((b1.x - a1.x) * (b2.y - b1.y) - (b1.y - a1.y) * (b2.x - b1.x)) / denom
    return (a1.x + t * (a2.x - a1.x), a1.y + t * (a2.y - a1.y))


def _body_rect(comp, device) -> tuple[float, float, float, float]:
    from panelcad.core.route import component_aabb

    s = comp.visual_scale()
    if (device.category or "").lower().startswith("connection.notes"):
        from panelcad.render.painter import notes_box

        w, h = notes_box(comp, s)
        inset = min(3.0, w * 0.08, h * 0.08)
        return (comp.x + inset, comp.y + inset, comp.x + w - inset, comp.y + h - inset)
    w = float(device.schematic_width) * s
    h = float(device.schematic_height) * s
    raw = comp.properties.get("card_h")
    if raw and int(comp.rotation or 0) == 0:
        h = max(h, float(raw) * s)
    x0, y0, x1, y1 = component_aabb(comp.x, comp.y, w, h, int(comp.rotation or 0), bool(comp.mirror))
    bw, bh = x1 - x0, y1 - y0
    inset = 5.0 * s
    if bw <= inset * 2.0 + 2.0 or bh <= inset * 2.0 + 2.0:
        inset = min(bw, bh) * 0.15
    return (x0 + inset, y0 + inset, x1 - inset, y1 - inset)


def _seg_inside_len(a, b, rect: tuple[float, float, float, float]) -> float:
    from math import hypot

    x0, y0, x1, y1 = rect
    dx, dy = b.x - a.x, b.y - a.y
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a.x - x0), (dx, x1 - a.x), (-dy, a.y - y0), (dy, y1 - a.y)):
        if abs(p) < 1e-9:
            if q < 0:
                return 0.0
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return 0.0
            t0 = max(t0, t)
        else:
            if t < t0:
                return 0.0
            t1 = min(t1, t)
    if t1 <= t0:
        return 0.0
    return (t1 - t0) * hypot(dx, dy)


def _assert_drawn_segments_match_pins(
    session: Session,
    sheet_id: str,
    *,
    mix_numbers: bool = True,
    bodies: bool = False,
) -> None:
    """Rendered geometry: every used pin sits on a segment; numbers do not cross."""

    from math import hypot

    from panelcad.render.sheet import pin_world
    from panelcad.render.wiring import route_sheet_wires

    sheet = session.sheet(sheet_id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    pin_pts: list[tuple[float, float]] = []
    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        for pin in device.pins:
            pin_pts.append(pin_world(comp, pin))

    for comp in sheet.components:
        if not _is_breaking_contact(comp):
            continue
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        for pin in device.pins:
            if pin.side not in {"left", "right"}:
                continue
            attached = [
                wire
                for wire in sheet.wires
                if f"{comp.id}.{pin.id}" in (wire.frm, wire.to)
            ]
            if not attached:
                continue
            wx, wy = pin_world(comp, pin)
            touched = False
            for wire in attached:
                pts = routes.get(wire.id) or []
                for a, b in zip(pts, pts[1:]):
                    if _pt_seg_dist(wx, wy, a, b) <= 1.2:
                        touched = True
                        break
                if touched:
                    break
            assert touched, (sheet_id, comp.id, pin.id, wx, wy, "pin does not touch a drawn segment")

    if bodies:
        for comp in sheet.components:
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            for pin in device.pins:
                attached = [
                    wire
                    for wire in sheet.wires
                    if f"{comp.id}.{pin.id}" in (wire.frm, wire.to)
                ]
                if not attached:
                    continue
                wx, wy = pin_world(comp, pin)
                touched = any(
                    _pt_seg_dist(wx, wy, a, b) <= 1.2
                    for wire in attached
                    for a, b in zip(routes.get(wire.id) or [], (routes.get(wire.id) or [])[1:])
                )
                assert touched, (sheet_id, comp.id, pin.id, wx, wy, "wired pin misses its segment")
        for comp in sheet.components:
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            rect = _body_rect(comp, device)
            for wire in sheet.wires:
                pts = routes.get(wire.id) or []
                for a, b in zip(pts, pts[1:]):
                    inside = _seg_inside_len(a, b, rect)
                    assert inside < 1.5, (
                        sheet_id,
                        wire.number or wire.net,
                        wire.frm,
                        wire.to,
                        comp.id,
                        inside,
                        "wire crosses a symbol body",
                    )

    if not mix_numbers:
        return
    numbered = [
        (wire, routes.get(wire.id) or [])
        for wire in sheet.wires
        if str(wire.number or "").strip() and str(wire.number) not in _RAIL_NUMBERS
    ]
    for i, (wa, pa) in enumerate(numbered):
        for wb, pb in numbered[i + 1 :]:
            if str(wa.number) == str(wb.number):
                continue
            for a1, a2 in zip(pa, pa[1:]):
                for b1, b2 in zip(pb, pb[1:]):
                    hit = _seg_touch_point(a1, a2, b1, b2, tol=0.75)
                    if hit is None:
                        continue
                    if any(hypot(hit[0] - px, hit[1] - py) <= 1.6 for px, py in pin_pts):
                        continue
                    ends = (
                        hypot(hit[0] - a1.x, hit[1] - a1.y) <= 0.9
                        or hypot(hit[0] - a2.x, hit[1] - a2.y) <= 0.9
                        or hypot(hit[0] - b1.x, hit[1] - b1.y) <= 0.9
                        or hypot(hit[0] - b2.x, hit[1] - b2.y) <= 0.9
                    )
                    if not ends:
                        continue
                    raise AssertionError(
                        (
                            sheet_id,
                            wa.number,
                            wb.number,
                            wa.frm,
                            wa.to,
                            wb.frm,
                            wb.to,
                            hit,
                            "segment joins two different wire numbers",
                        )
                    )


def test_control_contacts_are_connected_and_numbers_are_unique():
    """Dangling seal-in pins and reused wire numbers fail for every kind."""

    jobs = [
        ({"kind": "dol", "control": "local"}, False),
        ({"kind": "vfd", "control": "local", "drive": "generic_1k5"}, False),
        ({"kind": "reversing", "control": "local", "pilot_lights": True}, False),
        ({"kind": "star_delta", "control": "local"}, False),
        ({"kind": "soft_starter", "control": "local", "bypass": True, "pilot_lights": True}, False),
        ({"kind": "plc_motor", "control": "local"}, False),
        ({"kind": "dol", "control": "local"}, True),
        ({"kind": "vfd", "control": "local", "drive": "generic_1k5"}, True),
        ({"kind": "reversing", "control": "local"}, True),
        ({"kind": "star_delta", "control": "local"}, True),
        ({"kind": "soft_starter", "control": "local", "bypass": True}, True),
        ({"kind": "plc_motor", "control": "local"}, True),
    ]
    for kwargs, iec in jobs:
        session = _iec(f"iec connect {kwargs['kind']}") if iec else Session.create(f"nema connect {kwargs['kind']}")
        result = session.insert_circuit(**kwargs)
        sheets = [result["sheet"]]
        if result.get("control_sheet"):
            sheets.append(result["control_sheet"])
        if result.get("io_sheet"):
            sheets.append(result["io_sheet"])
        for sheet_id in sheets:
            page = session.sheet(sheet_id)
            is_control = sheet_id == result.get("control_sheet")
            is_io = sheet_id == result.get("io_sheet")
            is_ladder = getattr(page, "drawing_style", "") == "ladder"
            if is_control or is_io or is_ladder or not result.get("control_sheet"):
                _assert_contact_pins_not_dangling(session, sheet_id)
                _assert_seals_parallel_start(session, sheet_id)
                if not (is_control or is_io or is_ladder):
                    _assert_drawn_segments_match_pins(session, sheet_id, mix_numbers=False)
            if is_control or is_io or is_ladder:
                _assert_unique_sheet_numbers(session, sheet_id)
                _assert_text_clear(session, sheet_id)
                _assert_drawn_segments_match_pins(
                    session,
                    sheet_id,
                    mix_numbers=is_control or is_io,
                    bodies=is_control or is_io,
                )


def test_series_pole_wire_numbers_are_pr9():
    """Load-side 101/102/101 next to OL/motor is the series-pole network in PR #9.

    Circuit Builder does not change wire_numbering.py. Three series poles of
    one device still share a number until that PR lands.
    """

    session = Session.create("pr9 poles")
    result = session.insert_circuit(kind="reversing", control="local", hp=10)
    numbers = [
        w.number
        for w in session.sheet(result["sheet"]).wires
        if w.number and (w.net or "") in {"T1", "T2", "T3"}
    ]
    assert numbers, "expected load-side wire numbers"
    # Distinct phases may still collide; do not rewrite the numbering engine here.


def test_circuit_builder_buttons_fit_a_1080p_work_area(monkeypatch):
    """1920×1032 is the Windows work area with the taskbar on the bottom."""

    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QScrollArea

    from panelcad.gui import screen_fit
    from panelcad.gui.circuit_dialog import CircuitBuilderDialog
    from panelcad.gui.enclosure_dialog import SizeEnclosureDialog

    app = QApplication.instance() or QApplication([])
    work = QRect(0, 0, 1920, 1032)
    monkeypatch.setattr(screen_fit, "available_work_area", lambda _widget: QRect(work))

    dialog = CircuitBuilderDialog()
    dialog.show()
    app.processEvents()
    buttons = dialog.findChild(QDialogButtonBox)
    assert buttons is not None
    assert buttons.button(QDialogButtonBox.StandardButton.Ok).text() == "Insert"
    top_left = buttons.mapToGlobal(QPoint(0, 0))
    bottom_right = buttons.mapToGlobal(QPoint(buttons.width() - 1, buttons.height() - 1))
    assert work.left() <= top_left.x() <= work.right()
    assert work.top() <= top_left.y() <= work.bottom()
    assert work.left() <= bottom_right.x() <= work.right()
    assert work.top() <= bottom_right.y() <= work.bottom()
    assert buttons.height() > 12
    scroll = dialog.findChild(QScrollArea)
    assert scroll is not None and scroll.objectName() == "circuitBuilderOptions"
    assert scroll.widget() is not None
    assert buttons.parent() is dialog
    frame = dialog.frameGeometry()
    assert work.left() <= frame.left()
    assert work.top() <= frame.top()
    assert frame.right() <= work.right()
    assert frame.bottom() <= work.bottom()
    dialog.close()

    session = Session.create("Size dialog")
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=40, y=40)
    session.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
    enclosure = SizeEnclosureDialog(session)
    enclosure.show()
    app.processEvents()
    enc_buttons = enclosure.findChild(QDialogButtonBox)
    assert enc_buttons is not None
    enc_top = enc_buttons.mapToGlobal(QPoint(0, 0))
    enc_bottom = enc_buttons.mapToGlobal(QPoint(enc_buttons.width() - 1, enc_buttons.height() - 1))
    assert work.left() <= enc_top.x() <= work.right()
    assert work.top() <= enc_top.y() <= work.bottom()
    assert work.left() <= enc_bottom.x() <= work.right()
    assert work.top() <= enc_bottom.y() <= work.bottom()
    enc_frame = enclosure.frameGeometry()
    assert enc_frame.left() >= work.left()
    assert enc_frame.top() >= work.top()
    assert enc_frame.right() <= work.right()
    assert enc_frame.bottom() <= work.bottom()
    enclosure.close()
    app.processEvents()


def test_shared_supply_and_estop_are_visible_and_switchable(monkeypatch):
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QApplication

    from panelcad.gui import screen_fit
    from panelcad.gui.circuit_dialog import CircuitBuilderDialog

    app = QApplication.instance() or QApplication([])
    work = QRect(0, 0, 1920, 1032)
    monkeypatch.setattr(screen_fit, "available_work_area", lambda _widget: QRect(work))
    dialog = CircuitBuilderDialog()
    dialog.show()
    app.processEvents()
    values = dialog.values()
    assert values["shared_supply"] is True
    assert values["shared_estop"] is True
    scroll = dialog.findChild(screen_fit.OptionScroll)
    assert scroll is not None
    view = scroll.viewport()
    for box in (dialog.shared_supply, dialog.shared_estop):
        point = box.mapTo(view, QPoint(box.width() // 2, box.height() // 2))
        assert view.rect().contains(point), box.text()
    dialog.shared_supply.setChecked(False)
    dialog.shared_estop.setChecked(False)
    toggled = dialog.values()
    assert toggled["shared_supply"] is False
    assert toggled["shared_estop"] is False
    dialog.shared_supply.setChecked(True)
    dialog.shared_estop.setChecked(True)
    assert dialog.values()["shared_supply"] is True
    assert dialog.values()["shared_estop"] is True
    dialog.close()
    app.processEvents()


def test_routine_insert_notes_stay_off_the_message_box():
    from panelcad.gui.main_window import circuit_warning_dialog_text, circuit_warning_status_text

    assert circuit_warning_dialog_text(["Added rail end stops: ES-r-din-1-R"]) is None
    notes = [
        "Added rail end stops: ES-r-din-1-R",
        "U1 does not fit in the free backplate; recommend_enclosure can size a larger box",
    ]
    status = circuit_warning_status_text(notes)
    assert "Size Enclosure" in status
    assert "recommend_enclosure" not in status
    assert "end stops" in status
    text = circuit_warning_dialog_text(notes)
    assert text is not None
    assert "Size Enclosure" in text
    assert "recommend_enclosure" not in text
    assert "end stops" not in text
    assert "free backplate" in text
def _assert_xref_in_frame(session: Session, sheet_id: str) -> None:
    """Contact mirrors stay in the drawing frame, off the title block, symbols, and wires."""

    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet
    from panelcad.render.titleblock import title_block_rect, working_rect
    from panelcad.render.wire_labels import boxes_overlap
    from panelcad.render.wiring import route_sheet_wires
    from panelcad.render.xref import annotation_box, place_xref_labels

    sheet = session.sheet(sheet_id)
    ladder = is_ladder_sheet(sheet) or is_iec_column_sheet(sheet)
    placed = place_xref_labels(session.project, session.catalog, sheet, ladder=ladder)
    wx, wy, ww, wh = working_rect(sheet)
    frame = (wx, wy, wx + ww, wy + wh)
    tx, ty, tw, th = title_block_rect(sheet)
    title = (tx, ty, tx + tw, ty + th)
    bodies = _rects(session, sheet_id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    wire_boxes: list[tuple[float, float, float, float]] = []
    for pts in routes.values():
        for a, b in zip(pts, pts[1:]):
            wire_boxes.append((min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)))
    mirror_boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for ann, x, y, align in placed:
        if ann.kind != "mirror":
            continue
        box = annotation_box(ann, x, y, align)
        assert box[0] >= frame[0] - 0.2 and box[1] >= frame[1] - 0.2, (sheet_id, ann.owner, box, frame)
        assert box[2] <= frame[2] + 0.2 and box[3] <= frame[3] + 0.2, (sheet_id, ann.owner, box, frame)
        assert not boxes_overlap(box, title, pad=0.2), (sheet_id, ann.owner, "title", box)
        for body in bodies:
            assert not boxes_overlap(box, body, pad=0.2), (sheet_id, ann.owner, "symbol", box, body)
        for wire_box in wire_boxes:
            assert not boxes_overlap(box, wire_box, pad=0.15), (sheet_id, ann.owner, "wire", box, wire_box)
        for owner, other in mirror_boxes:
            assert not boxes_overlap(box, other, pad=0.2), (sheet_id, ann.owner, owner, "tables overlap", box, other)
        mirror_boxes.append((ann.owner, box))


def _assert_marks_clear_of_pins(session: Session, sheet_id: str) -> None:
    """One wire number per nearby conductor, and that number stays off the pin number."""

    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet
    from panelcad.render.wire_labels import (
        _pin_obstacles,
        boxes_overlap,
        mark_box,
        place_wire_marks,
        routes_near,
    )
    from panelcad.render.wiring import route_sheet_wires

    sheet = session.sheet(sheet_id)
    ladder = is_ladder_sheet(sheet) or is_iec_column_sheet(sheet)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=ladder, project=session.project)
    pins = _pin_obstacles(sheet, session.catalog)
    seen: list[tuple[str, str, object]] = []
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=ladder)
        if not at or not mark or mark in _RAIL_NUMBERS:
            continue
        box = mark_box(at[0], at[1], mark, align=at[2])
        for pin_box in pins:
            assert not boxes_overlap(box, pin_box, pad=0.05), (sheet_id, wire.id, mark, box, pin_box)
        pts = routes.get(wire.id) or []
        for other_id, other_mark, other_pts in seen:
            if other_mark == mark and routes_near(pts, other_pts):
                raise AssertionError((sheet_id, mark, wire.id, other_id, "same number printed twice"))
        seen.append((wire.id, mark, pts))


def _assert_supply_risers_split(session: Session, result: dict) -> None:
    """PSU plus and minus land on different verticals. The same X would short L+ to 0V."""

    supply = _id(result, "psu")
    sheet = session.sheet(result["control_sheet"])
    plus = next(w for w in sheet.wires if f"{supply}.plus" in {w.frm, w.to})
    minus = next(w for w in sheet.wires if f"{supply}.minus" in {w.frm, w.to})
    assert plus.points and minus.points, (plus.points, minus.points)
    assert abs(plus.points[0].x - minus.points[0].x) >= 8.0


def _assert_star_delta_links(result: dict, motor_end: tuple[str, str, str], motor_pins: tuple[str, str, str]) -> None:
    """Delta ties each winding end to the next winding start. Star shorts those ends."""

    delta = _id(result, "delta")
    star = _id(result, "star")
    motor = _id(result, "motor")
    for end, line in zip(motor_end, ("L1", "L2", "L3"), strict=True):
        assert _has_link(result, f"{motor}.{end}", f"{delta}.{line}"), (end, line)
        assert _has_link(result, f"{motor}.{end}", f"{star}.{line}"), (end, line)
    for src, dst in zip(("T1", "T2", "T3"), motor_pins, strict=True):
        assert _has_link(result, f"{delta}.{src}", f"{motor}.{dst}"), (src, dst)
    assert _has_link(result, f"{star}.T1", f"{star}.T2")
    assert _has_link(result, f"{star}.T2", f"{star}.T3")


def _iec_jobs():
    return [
        {"kind": "reversing", "control": "local", "pilot_lights": True},
        {"kind": "star_delta", "control": "local", "timer_seconds": 8},
        {"kind": "soft_starter", "control": "local", "bypass": True, "pilot_lights": True},
        {"kind": "plc_motor", "control": "local", "hoa": True},
    ]


def test_iec_columns_for_every_new_kind():
    """IEC projects draw the new circuits as columns with the library letter codes."""

    from panelcad.core.xref import xref_annotations

    for kwargs in _iec_jobs():
        session = _iec(f"cols {kwargs['kind']}")
        result = session.insert_circuit(**kwargs)
        assert result["standard"] == "IEC"
        control = session.sheet(result["control_sheet"])
        assert control.drawing_style == "iec"
        assert control.standard == "IEC"
        assert control.rail_hot == "L+"
        assert control.rail_return == "0V"
        power = session.sheet(result["sheet"])
        assert power.standard == "IEC"
        assert power.drawing_style != "iec"
        for comp in control.components:
            if int(comp.rotation or 0) not in {0, 90}:
                raise AssertionError((comp.id, comp.rotation))
        coils = [
            c
            for c in control.components
            if c.library_id.endswith(".coil") or "timers." in c.library_id or c.library_id.endswith("relay")
        ]
        assert coils, kwargs
        for coil in coils:
            assert int(coil.rotation or 0) == 90 or coil.properties.get("ladder_coil"), coil.id
        notes = xref_annotations(session.project, session.catalog, control)
        mirrors = [item for item in notes if item.kind == "mirror"]
        assert mirrors, kwargs
        assert any("/" in line for item in mirrors for line in item.lines), kwargs
        sheets = [result["sheet"], result["control_sheet"]]
        if result.get("io_sheet"):
            io = session.sheet(result["io_sheet"])
            assert io.drawing_style == "iec"
            assert io.rail_hot == control.rail_hot == "L+"
            assert io.rail_return == control.rail_return == "0V"
            sheets.append(result["io_sheet"])
        for sheet_id in sheets:
            _assert_inside_working(session, sheet_id)
            _assert_no_overlap(session, sheet_id)
            _assert_contact_pins_not_dangling(session, sheet_id)
            _assert_seals_parallel_start(session, sheet_id)
            _assert_unique_sheet_numbers(session, sheet_id)
            _assert_text_clear(session, sheet_id)
            _assert_marks_clear_of_pins(session, sheet_id)
            _assert_xref_in_frame(session, sheet_id)
            _assert_xref_stays_in_column(session, sheet_id)
            _assert_drawn_segments_match_pins(session, sheet_id, mix_numbers=True, bodies=True)
        from panelcad.core.phase_color import wire_phase_pens

        assert wire_phase_pens(session.project, control, session.catalog) == {}
        _assert_supply_risers_split(session, result)
        estop = control.component(_id(result, "estop"))
        assert {"21", "22"} <= set(estop.properties.get("hidden_pins") or [])
        assert _wired_pins(session, result["control_sheet"], estop.id).isdisjoint({"21", "22"})
        if kwargs["kind"] == "reversing":
            fwd = session.sheet(result["sheet"]).component(_id(result, "fwd"))
            assert fwd.library_id.startswith("iec.")
            assert fwd.id.startswith("K")
            assert "electrical" in str(fwd.properties.get("interlock") or "")
        if kwargs["kind"] == "star_delta":
            motor = session.sheet(result["sheet"]).component(_id(result, "motor"))
            assert motor.library_id == "iec.loads.motor_3ph_6lead"
            pins = {p.id for p in session.catalog.get(motor.library_id).pins}
            assert {"U1", "V1", "W1", "U2", "V2", "W2"} <= pins
            texts = {
                str(prim.text).strip()
                for prim in session.catalog.get(motor.library_id).primitives
                if prim.type == "text"
            }
            assert {"U1", "V1", "W1", "U2", "V2", "W2"} <= texts
            timed_no = session.sheet(result["control_sheet"]).component(_id(result, "tr_no"))
            assert timed_no.properties.get("terminals") == "15-18"
            timed_nc = session.sheet(result["control_sheet"]).component(_id(result, "tr_nc"))
            assert timed_nc.properties.get("terminals") == "15-16"
            timer = session.sheet(result["control_sheet"]).component(_id(result, "timer"))
            assert timer.id == "K4"
            _assert_star_delta_links(result, ("W2", "U2", "V2"), ("U1", "V1", "W1"))
        if kwargs["kind"] == "soft_starter":
            from panelcad.core.xref import iec_mirror_rows, xref_families

            power = session.sheet(result["sheet"])
            bypass = power.component(_id(result, "bypass"))
            assert bypass.properties.get("function_label") == "Bypass"
            coil = control.component(_id(result, "bypass_coil"))
            assert coil.properties.get("function_label") == "Bypass"
            notes = xref_annotations(session.project, session.catalog, control)
            assert not any(item.owner == coil.id and item.kind == "mirror" for item in notes)
            starter = power.component(_id(result, "softstarter"))
            families = xref_families(session.project, session.catalog)
            rows = iec_mirror_rows(families[starter.id])
            terminals = [row["terminals"] for row in rows]
            assert "13-14" in terminals
            assert "23-24" in terminals
        if kwargs["kind"] == "plc_motor":
            plc = next(c for c in result["components"] if c["role"] == "plc")
            assert plc["library_id"] == "iec.plc.compact_16dio"
            assert plc["id"].startswith("A")
            hoa = session.sheet(result["control_sheet"]).component(_id(result, "hoa"))
            labels = hoa.properties.get("pin_labels") or {}
            assert "H" in labels.values() and "A" in labels.values()
            _assert_hoa_does_not_bypass_selector(session, result)
            io = session.sheet(result["io_sheet"])
            used = set()
            for wire in io.wires:
                for end in (wire.frm, wire.to):
                    if ".I0_" in end:
                        used.add(end.split(".", 1)[1])
            assert len(used) >= 3


def test_star_delta_netlist_on_nema_uses_t_leads():
    """NEMA delta is T6–T1 / T4–T2 / T5–T3. Timed contacts stay 21 and 15."""

    session = Session.create("sd net nema")
    result = session.insert_circuit(kind="star_delta", control="local")
    _assert_star_delta_links(result, ("T6", "T4", "T5"), ("T1", "T2", "T3"))
    assert _id(result, "tr_nc").endswith("-21")
    assert _id(result, "tr_no").endswith("-15")
    page = session.sheet(result["control_sheet"])
    assert page.component(_id(result, "tr_no")).properties.get("terminals") in {None, ""}


def _assert_xref_stays_in_column(session: Session, sheet_id: str) -> None:
    """Mirror tables and coil lines stay under their own symbol, not in the margin."""

    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet
    from panelcad.render.xref import annotation_box, place_xref_labels

    sheet = session.sheet(sheet_id)
    ladder = is_ladder_sheet(sheet) or is_iec_column_sheet(sheet)
    placed = place_xref_labels(session.project, session.catalog, sheet, ladder=ladder)
    for ann, x, y, align in placed:
        if ann.kind not in {"mirror", "contact"}:
            continue
        box = annotation_box(ann, x, y, align)
        cx = (box[0] + box[2]) * 0.5
        assert abs(cx - ann.anchor_x) <= 72.0, (sheet_id, ann.kind, ann.owner, ann.lines, cx, ann.anchor_x)
        if ann.kind == "mirror":
            assert box[1] >= ann.anchor_y - 28.0, (sheet_id, ann.owner, box, ann.anchor_y)


def _cross_inside(a1, a2, b1, b2) -> tuple[float, float] | None:
    denom = (a2.x - a1.x) * (b2.y - b1.y) - (a2.y - a1.y) * (b2.x - b1.x)
    if abs(denom) < 1e-6:
        return None
    t = ((b1.x - a1.x) * (b2.y - b1.y) - (b1.y - a1.y) * (b2.x - b1.x)) / denom
    u = ((b1.x - a1.x) * (a2.y - a1.y) - (b1.y - a1.y) * (a2.x - a1.x)) / denom
    if 0.04 < t < 0.96 and 0.04 < u < 0.96:
        return (a1.x + t * (a2.x - a1.x), a1.y + t * (a2.y - a1.y))
    return None


def _assert_wires_clear_text(
    session: Session, sheet_id: str, *, collect: bool = False
) -> list | None:
    """A conductor may not run through a tag, a terminal name, or another wire's number.

    ``collect`` returns every hit instead of raising, so a conversion can be
    compared with a fresh insert of the same sheet.
    """

    from math import hypot

    from panelcad.core.geometry import rotate_point
    from panelcad.core.ink import device_ref_layout, text_box
    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet
    from panelcad.core.pins import pin_caption
    from panelcad.render.sheet import pin_side_world, pin_world
    from panelcad.render.wire_labels import bundle_spec_plan, mark_box, place_wire_marks
    from panelcad.render.wiring import route_sheet_wires

    sheet = session.sheet(sheet_id)
    hits: list[tuple] = []
    column = is_iec_column_sheet(sheet)
    ladder_only = is_ladder_sheet(sheet)
    ladder = ladder_only or column
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=ladder, project=session.project)
    # Duplicate columns blank the gauge; measuring spec_text() flags a note that is not drawn.
    drawn_specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    texts: list[tuple[str, str, str, tuple[float, float, float, float]]] = []

    def _world(comp, lx: float, ly: float) -> tuple[float, float]:
        scale = comp.visual_scale()
        rx, ry = rotate_point(lx * scale, ly * scale, int(comp.rotation or 0))
        if comp.mirror:
            rx = -rx
        return (comp.x + rx, comp.y + ry)

    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        scale = comp.visual_scale()
        for prim in device.primitives:
            if prim.type != "text" or not str(prim.text).strip():
                continue
            wx, wy = _world(comp, prim.x, prim.y)
            box = text_box(wx, wy, str(prim.text), max(1.2, prim.size * scale), "middle")
            if box is not None:
                texts.append(("prim", comp.id, str(prim.text), box))
        layout = device_ref_layout(device, comp, ref_align="middle" if ladder else "start")
        if layout is not None:
            tx, ty, talign, size = layout
            tag = text_box(tx, ty, comp.id, size, talign)
            if tag is not None:
                texts.append(("tag", comp.id, comp.id, tag))
        if not ladder:
            continue
        from panelcad.core.ink import pin_label_anchor

        drawn = {str(prim.text).strip() for prim in device.primitives if prim.type == "text" and prim.text}
        iec_numbers = column or str(getattr(sheet, "standard", "") or "") == "IEC"
        for pin in device.pins:
            cap = pin_caption(pin, comp)
            override = bool(cap) and cap != pin.id
            show_id = iec_numbers and cap == pin.id and cap not in drawn
            # A NEMA ladder draws a caption only when it is not the pin id.
            if ladder_only and not override:
                continue
            if not cap or (cap == pin.id and not show_id):
                continue
            wx, wy = pin_world(comp, pin)
            side = pin_side_world(comp, pin)
            size = 3.2 if ladder_only else 1.9
            tx, ty, align = pin_label_anchor(
                wx, wy, side, ladder=ladder_only, column=column, caption=cap
            )
            box = text_box(tx, ty, cap, size, align)
            if box is not None:
                texts.append(("pin", comp.id, cap, box))
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=ladder)
        if at and mark:
            texts.append(
                (
                    "mark",
                    wire.id,
                    mark,
                    mark_box(at[0], at[1], mark, align=at[2], spec=drawn_specs.get(wire.id, "")),
                )
            )

    def _own_pins(wire) -> list[tuple[float, float]]:
        found: list[tuple[float, float]] = []
        for end in (wire.frm, wire.to):
            if "." not in end:
                continue
            ref, pin_id = end.split(".", 1)
            try:
                comp = sheet.component(ref)
                found.append(pin_world(comp, session.catalog.get(comp.library_id).pin(pin_id)))
            except (KeyError, ValueError):
                continue
        return found

    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        pins = _own_pins(wire)
        for a, b in zip(pts, pts[1:]):
            mid_x = (a.x + b.x) * 0.5
            mid_y = (a.y + b.y) * 0.5
            for kind, owner, label, box in texts:
                if kind == "mark" and owner == wire.id:
                    continue
                if _seg_inside_len(a, b, box) < 0.8:
                    continue
                if kind == "pin" and pins and any(
                    hypot(mid_x - px, mid_y - py) <= 4.0 for px, py in pins
                ):
                    continue
                hit = (
                    sheet_id,
                    wire.number or wire.net,
                    wire.frm,
                    wire.to,
                    kind,
                    owner,
                    label,
                    "wire crosses text",
                )
                if collect:
                    hits.append(hit)
                    continue
                raise AssertionError(hit)
    if collect:
        return hits


def _assert_conductors_land(session: Session) -> None:
    """Every motor pin has a wire end on it, and no wire end is left in space.

    A landed end sits on a terminal, a rail, or another conductor. Applies to
    every schematic and I/O sheet the builder or the converter produced.
    """

    from math import hypot

    from panelcad.render.sheet import pin_world
    from panelcad.render.wiring import route_sheet_wires, wire_ends

    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        pin_pts: list[tuple[float, float]] = []
        motors: list[tuple] = []
        for comp in sheet.components:
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            for pin in device.pins:
                pin_pts.append(pin_world(comp, pin))
            if (device.category or "").lower().startswith("load.motor"):
                motors.append((comp, device))

        def _on_pin(x: float, y: float) -> bool:
            return any(hypot(x - px, y - py) <= 0.05 for px, py in pin_pts)

        def _on_other(wid: str, x: float, y: float) -> bool:
            for other, pts in routes.items():
                if other == wid:
                    continue
                for a, b in zip(pts, pts[1:]):
                    if _pt_seg_dist(x, y, a, b) <= 0.4:
                        return True
            return False

        for wire in sheet.wires:
            pts = routes.get(wire.id) or []
            if len(pts) < 1:
                continue
            terminals: list[tuple[float, float]] = []
            try:
                ax, ay, _a_side, bx, by, _b_side = wire_ends(sheet, session.catalog, wire)
                terminals = [(ax, ay), (bx, by)]
            except (KeyError, ValueError):
                terminals = []
            for end_ref, point in ((wire.frm, pts[0]), (wire.to, pts[-1])):
                if any(hypot(point.x - tx, point.y - ty) <= 0.05 for tx, ty in terminals):
                    continue
                if _on_pin(point.x, point.y) or _on_other(wire.id, point.x, point.y):
                    continue
                raise AssertionError(
                    (
                        sheet.id,
                        wire.number or wire.net,
                        end_ref,
                        (point.x, point.y),
                        "wire endpoint is not on a pin, a rail, or another conductor",
                    )
                )
        for comp, device in motors:
            for pin in device.pins:
                wx, wy = pin_world(comp, pin)
                attached = [wire for wire in sheet.wires if f"{comp.id}.{pin.id}" in (wire.frm, wire.to)]
                assert attached, (sheet.id, comp.id, pin.id, "motor terminal has no wire")
                landed = False
                for wire in attached:
                    pts = routes.get(wire.id) or []
                    if not pts:
                        continue
                    if hypot(pts[0].x - wx, pts[0].y - wy) <= 0.05 or hypot(pts[-1].x - wx, pts[-1].y - wy) <= 0.05:
                        landed = True
                        break
                assert landed, (sheet.id, comp.id, pin.id, (wx, wy), "motor terminal has no wire endpoint on it")


def _assert_star_delta_power_geometry(session: Session, result: dict) -> None:
    """Motor under the column, K2/K3 beside F1, short star bar, crossings capped."""

    from math import hypot

    from panelcad.render.titleblock import working_rect
    from panelcad.render.wiring import route_sheet_wires

    sheet_id = result["sheet"]
    sheet = session.sheet(sheet_id)
    _assert_drawn_segments_match_pins(session, sheet_id, mix_numbers=True, bodies=True)
    _assert_wires_clear_text(session, sheet_id)
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    q1 = sheet.component(_id(result, "breaker"))
    k1 = sheet.component(_id(result, "main"))
    f1 = sheet.component(_id(result, "overload"))
    motor = sheet.component(_id(result, "motor"))
    k2 = sheet.component(_id(result, "delta"))
    k3 = sheet.component(_id(result, "star"))
    assert q1.y < k1.y < f1.y < motor.y
    assert abs(k2.y - f1.y) <= 1.5 and abs(k3.y - f1.y) <= 1.5
    assert k2.x > f1.x + 20.0
    assert k3.x > k2.x + 8.0
    # Line terminals sit on the column. The winding ends are on the same symbol.
    assert abs(motor.x - f1.x) <= 2.0
    _wx, wy, ww, wh = working_rect(sheet)
    right_edge = _wx + ww
    bottom_edge = wy + wh
    k3_device = session.catalog.get(k3.library_id)
    k3_box = component_aabb(
        k3.x,
        k3.y,
        float(k3_device.schematic_width) * k3.visual_scale(),
        float(k3_device.schematic_height) * k3.visual_scale(),
        int(k3.rotation or 0),
        bool(k3.mirror),
    )
    # Three skirt risers, one grid apart, need a few millimetres past the aux pin.
    # A run out to the sheet edge is still the old loop around K3.
    right_limit = k3_box[2] + 8.0
    for pts in routes.values():
        for point in pts:
            assert point.y >= q1.y - 0.6, (point, "route loops above the breaker")
            assert point.y <= bottom_edge - 6.0, (point, "conductor runs along the bottom edge")
            assert point.x <= right_edge - 18.0, (point, "conductor runs along the right edge")
            assert point.x <= right_limit, (point, "winding run loops past K3")
    bridges = [
        wire
        for wire in sheet.wires
        if wire.frm.startswith(f"{k3.id}.") and wire.to.startswith(f"{k3.id}.")
    ]
    assert len(bridges) == 2
    for wire in bridges:
        pts = routes[wire.id]
        xs = [point.x for point in pts]
        ys = [point.y for point in pts]
        assert max(xs) - min(xs) < 40.0, (wire.id, xs)
        assert max(ys) - min(ys) < 8.0, (wire.id, ys)
        assert min(ys) >= k3.y + 68.0, (wire.id, ys, k3.y)
    segs = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        for a, b in zip(pts, pts[1:]):
            if hypot(b.x - a.x, b.y - a.y) < 0.4:
                continue
            segs.append((wire, a, b))
    crossings = []
    for index, (wa, a1, a2) in enumerate(segs):
        for wb, b1, b2 in segs[index + 1 :]:
            if wa.id == wb.id:
                continue
            hit = _cross_inside(a1, a2, b1, b2)
            if hit is not None:
                crossings.append((wa.number or wa.net, wb.number or wb.net, hit))
    assert len(crossings) <= 4, crossings


def test_iec_star_delta_power_stays_on_a_short_grid():
    """Motor under K1/F1. K2 and K3 beside F1. Crossings stay at four or fewer."""

    session = _iec("star grid")
    result = session.insert_circuit(kind="star_delta", control="local", timer_seconds=8)
    _assert_star_delta_power_geometry(session, result)
    _assert_xref_stays_in_column(session, result["control_sheet"])

    lined = _iec("star line ol")
    other = lined.insert_circuit(kind="star_delta", control="local", overload_placement="line")
    # The line-side stack leaves no full lane pitch under the motor. Bodies stay clear.
    _assert_drawn_segments_match_pins(lined, other["sheet"], mix_numbers=False, bodies=True)


def test_nema_reversing_phases_keep_separate_lanes():
    """L1, L2, and L3 do not share a horizontal, and each name prints once."""

    from panelcad.render.wire_labels import place_wire_marks
    from panelcad.render.wiring import route_sheet_wires

    session = Session.create("rev lanes")
    result = session.insert_circuit(kind="reversing", control="local")
    sheet = session.sheet(result["sheet"])
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=False, project=session.project)
    grouped: dict[str, list] = {}
    for wire in sheet.wires:
        token = str(wire.number or wire.net or "").strip().upper()
        if token in {"L1", "L2", "L3"}:
            grouped.setdefault(token, []).append(wire)

    def _horiz(wire) -> list[tuple[float, float, float]]:
        found = []
        pts = routes.get(wire.id) or []
        for a, b in zip(pts, pts[1:]):
            if abs(a.y - b.y) <= 0.35 and abs(a.x - b.x) > 2.0:
                found.append((a.y, min(a.x, b.x), max(a.x, b.x)))
        return found

    names = list(grouped)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            for ya, x0, x1 in (span for wire in grouped[left] for span in _horiz(wire)):
                for yb, u0, u1 in (span for wire in grouped[right] for span in _horiz(wire)):
                    overlap = min(x1, u1) - max(x0, u0)
                    assert abs(ya - yb) >= 0.6 or overlap <= 1.0, (left, right, ya, yb, overlap)
    for token, wires in grouped.items():
        printed = [wire.id for wire in wires if marks.get(wire.id)]
        assert len(printed) <= 1, (token, printed, [marks.get(wire.id) for wire in wires])
    _assert_drawn_segments_match_pins(session, result["sheet"], mix_numbers=True, bodies=True)
    _assert_text_clear(session, result["sheet"])


def test_dialog_and_agent_insert_the_same_iec_circuit():
    """Both paths: the dialog can ask for IEC, and insert_circuit standard=IEC matches it."""

    from PySide6.QtWidgets import QApplication

    from panelcad.cli.agent import Agent
    from panelcad.gui.circuit_dialog import CircuitBuilderDialog

    app = QApplication.instance() or QApplication([])
    dialog = CircuitBuilderDialog(standard="NEMA/ANSI")
    dialog.kind.setCurrentIndex(dialog.kind.findData("reversing"))
    dialog.standard.setCurrentIndex(dialog.standard.findData("IEC"))
    dialog.control.setCurrentIndex(dialog.control.findData("local"))
    values = dialog.values()
    assert values["standard"] == "IEC"
    gui = Session.create("dialog iec")
    via_dialog = gui.insert_circuit(**values)
    dialog.close()

    nema = Session.create("agent iec")
    via_agent = nema.insert_circuit(kind="reversing", control="local", standard="IEC")
    assert via_dialog["standard"] == via_agent["standard"] == "IEC"
    assert gui.sheet(via_dialog["control_sheet"]).drawing_style == "iec"
    assert nema.sheet(via_agent["control_sheet"]).drawing_style == "iec"
    assert gui.sheet(via_dialog["control_sheet"]).rail_hot == nema.sheet(via_agent["control_sheet"]).rail_hot
    dialog_libs = sorted(c["library_id"] for c in via_dialog["components"])
    agent_libs = sorted(c["library_id"] for c in via_agent["components"])
    assert dialog_libs == agent_libs
    assert all(lib.startswith("iec.") or lib.startswith("adc.") for lib in agent_libs)

    project = _iec("omit standard")
    omitted = project.insert_circuit(kind="star_delta", control="local")
    assert project.sheet(omitted["control_sheet"]).drawing_style == "iec"
    forced = CircuitBuilderDialog(standard="IEC")
    assert forced.values()["standard"] == "IEC"
    forced.close()

    agent = Agent()
    agent.handle({"id": 1, "op": "create_project", "name": "JSONL IEC", "standard": "NEMA/ANSI"})
    done = agent.handle(
        {"id": 2, "op": "insert_circuit", "kind": "soft_starter", "control": "local", "bypass": True, "standard": "IEC"}
    )
    assert done["ok"] is True, done
    page = agent._need().sheet(done["result"]["control_sheet"])
    assert page.drawing_style == "iec"
    assert page.rail_hot == "L+"
    assert any(c.library_id.startswith("iec.") for c in page.components)
    app.processEvents()


def test_iec_plc_return_joins_the_control_common():
    """The I/O common is the control return, including an AC control job."""

    session = _iec("plc m")
    result = session.insert_circuit(kind="plc_motor", control="local")
    control = session.sheet(result["control_sheet"])
    io = session.sheet(result["io_sheet"])
    assert io.rail_hot == control.rail_hot == "L+"
    assert io.rail_return == control.rail_return == "0V"
    plc_id = next(item["id"] for item in result["components"] if item["role"] == "plc")
    common = next(wire for wire in io.wires if wire.frm.endswith(f"{plc_id}.M") or wire.to.endswith(f"{plc_id}.M"))
    assert common.number == "0V"
    assert any(
        wire.number == "0V" and (wire.frm.endswith(".A2") or wire.to.endswith(".A2")) for wire in control.wires
    )

    ac = _iec("plc ac")
    ac_result = ac.insert_circuit(kind="plc_motor", control="local", control_voltage=120)
    ac_io = ac.sheet(ac_result["io_sheet"])
    ac_control = ac.sheet(ac_result["control_sheet"])
    assert ac_io.rail_hot == ac_control.rail_hot == "X1"
    assert ac_io.rail_return == ac_control.rail_return == "X2"
    ac_plc = next(item["id"] for item in ac_result["components"] if item["role"] == "plc")
    ac_common = next(
        wire for wire in ac_io.wires if wire.frm.endswith(f"{ac_plc}.M") or wire.to.endswith(f"{ac_plc}.M")
    )
    assert ac_common.number == "X2"


def test_iec_insert_into_a_nema_project_places_an_iec_earth():
    """A NEMA breaker already on the job must not ask for nema.connection.ground."""

    for kind in ("star_delta", "reversing", "soft_starter", "plc_motor"):
        session = Session.create(f"nema then {kind}")
        session.insert_circuit(kind="reversing", control="local")
        before = sum(len(sheet.components) for sheet in session.project.sheets)
        result = session.insert_circuit(kind=kind, control="local", standard="IEC", new_sheet=True)
        after = sum(len(sheet.components) for sheet in session.project.sheets)
        assert after > before, kind
        assert result["standard"] == "IEC"
        power = session.sheet(result["sheet"])
        earth = [comp for comp in power.components if comp.properties.get("role") == "pe_bar"]
        assert earth, kind
        assert all(comp.library_id == "iec.connection.ground" for comp in earth)


def test_nema_insert_keeps_the_iec_control_sheet(tmp_path):
    """A later NEMA circuit gets its own ladder. PDF export still reads every pin."""

    session = _iec("both standards")
    iec = session.insert_circuit(kind="star_delta", control="local")
    nema = session.insert_circuit(kind="reversing", control="local", standard="NEMA", new_sheet=True)
    iec_page = session.sheet(iec["control_sheet"])
    nema_page = session.sheet(nema["control_sheet"])
    assert iec_page.id != nema_page.id
    assert iec_page.drawing_style == "iec"
    assert (iec_page.rail_hot, iec_page.rail_return) == ("L+", "0V")
    assert nema_page.drawing_style == "ladder"
    assert (nema_page.rail_hot, nema_page.rail_return) == ("24V", "0V")
    session.export_pdf(tmp_path / "both.pdf")
    assert (tmp_path / "both.pdf").is_file()


def test_iec_star_delta_mirrors_stay_in_the_coil_column():
    """Each contact table stays in its coil's column, and one bay does not share x.

    A bay is the vertical channel under a coil. Tables whose x ranges overlap
    are in that same bay whatever the gap between them. Different header
    columns are separate only when a clear gap remains between the boxes.
    """

    from panelcad.core.zones import column_from_header
    from panelcad.render.xref import annotation_box, place_xref_labels

    def _check(session: Session, sheet_id: str) -> None:
        sheet = session.sheet(sheet_id)
        placed = place_xref_labels(session.project, session.catalog, sheet, ladder=True)
        mirrors = [item for item in placed if item[0].kind == "mirror"]
        assert mirrors, sheet_id
        for ann, x, y, align in mirrors:
            box = annotation_box(ann, x, y, align)
            center = (box[0] + box[2]) * 0.5
            assert column_from_header(sheet, center) == column_from_header(sheet, ann.anchor_x), (
                sheet_id,
                ann.owner,
                ann.lines,
                center,
                ann.anchor_x,
            )
        boxes = [(ann.owner, annotation_box(ann, x, y, align)) for ann, x, y, align in mirrors]
        for index, (name, box) in enumerate(boxes):
            for other, obox in boxes[index + 1 :]:
                overlap = min(box[2], obox[2]) - max(box[0], obox[0])
                assert overlap <= 0.05, (sheet_id, name, other, round(overlap, 2), box, obox)

    for kind, kwargs in (
        ("star_delta", {"control": "local"}),
        ("reversing", {"control": "local", "pilot_lights": True}),
        ("soft_starter", {"control": "local", "bypass": True}),
        ("plc_motor", {"control": "local"}),
    ):
        session = _iec(f"mirror columns {kind}")
        result = session.insert_circuit(kind=kind, **kwargs)
        _check(session, result["control_sheet"])


def test_iec_ac_control_does_not_join_the_supply_phase():
    """A 230 V control rail named L1 used to pull T1.X1 into the 400 V L1 net."""

    from panelcad.core.wire_numbering import wire_networks

    session = _iec("ac nets")
    result = session.insert_circuit(kind="plc_motor", control="local", control_voltage=230)
    power = session.sheet(result["sheet"])
    breaker = next(comp for comp in power.components if comp.properties.get("role") == "breaker")
    supply = f"{breaker.id}.2"
    transformer = next(
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.properties.get("role") == "transformer"
    )
    secondary = f"{transformer.id}.X1"
    groups = wire_networks(session.project, session.catalog)

    def _ends(target: str) -> set[str]:
        for members in groups:
            ends = {wire.frm for _sheet, wire in members} | {wire.to for _sheet, wire in members}
            if target in ends:
                return ends
        return set()

    assert supply not in _ends(secondary)
    assert secondary not in _ends(supply)
