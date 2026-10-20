"""Sample jobs with field cables leaving the panel.

A reversing motor lands on TB1. The PLC sample lands the VFD motor on the
drive output terminals and an analog transmitter on TB2. Used by tests and
by the cable-schedule artifacts. Cable part numbers and gland sizes are left
blank.
"""

from __future__ import annotations

from panelcad.api.session import Session
from panelcad.core.models import WireTray
from panelcad.library.catalog import _device_from_raw


def build_reversing_motor_sample() -> Session:
    """NEMA reversing starter. Motor leads land on TB1. Cable is 3C+G #14 TC-ER."""

    session = Session.create("Reversing motor")
    session.project.title_block.drawing_number = "E-301"
    session.project.title_block.company = "Panel shop"
    session.insert_circuit(kind="reversing", hp=7.5, voltage=480)
    motor = _motor_ref(session)
    sheet = _sheet_with_wires(session, motor)
    _land_on_strip(session, motor, "TB1", sheet_id=sheet, gauge="#14")
    _add_tray(session, "TRAY-A")
    session.propose_cables()
    for cable in session.project.cables:
        if cable.to_device == motor:
            session.update_cable(cable.tag, length=120, slack_pct=10)
    return session


def build_plc_hoa_analog_sample() -> Session:
    """PLC motor with HOA. VFD output stays on the drive. Analog transmitter on TB2."""

    session = Session.create("PLC HOA and analog")
    session.project.title_block.drawing_number = "E-214"
    session.project.title_block.company = "Panel shop"
    session.insert_circuit(
        kind="plc_motor",
        starter="vfd",
        drive="generic_1k5",
        control="local",
        hoa=True,
        hp=15,
        voltage=480,
    )
    motor = _motor_ref(session)
    sheet = _sheet_with_wires(session, motor)
    _set_gauge_on_device(session, motor, "#12")
    _add_transmitter(session, sheet)
    _add_tray(session, "TRAY-A")
    session.propose_cables()
    for cable in session.project.cables:
        if cable.to_device == motor:
            session.update_cable(cable.tag, length=75, slack_pct=10)
        elif cable.to_kind == "instrument":
            session.update_cable(cable.tag, length=40, slack_pct=10)
    return session


def _motor_ref(session: Session) -> str:
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.properties.get("role") == "motor":
                return comp.id
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                continue
            if device.category == "load.motor":
                return comp.id
    raise RuntimeError("sample has no motor")


def _sheet_with_wires(session: Session, ref: str) -> str:
    prefix = ref + "."
    for sheet in session.project.sheets:
        if sheet.kind in {"panel_layout", "door_layout", "terminal"}:
            continue
        if any(str(wire.frm).startswith(prefix) or str(wire.to).startswith(prefix) for wire in sheet.wires):
            return sheet.id
    raise RuntimeError(f"no wires land on {ref}")


def _set_gauge_on_device(session: Session, device_ref: str, gauge: str) -> None:
    prefix = device_ref + "."
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            if str(wire.frm).startswith(prefix) or str(wire.to).startswith(prefix):
                wire.gauge = gauge


def _land_on_strip(session: Session, device_ref: str, strip: str, *, sheet_id: str, gauge: str) -> None:
    """Put a terminal between the panel device and the field device. Pin B is the field side."""

    sheet = session.sheet(sheet_id)
    prefix = device_ref + "."
    wires = [
        wire
        for wire in sheet.wires
        if str(wire.frm).startswith(prefix) or str(wire.to).startswith(prefix)
    ]
    wires.sort(key=lambda wire: str(wire.frm) + str(wire.to))
    y = 36.0
    for index, wire in enumerate(wires, start=1):
        if str(wire.frm).startswith(prefix):
            field_end, panel_end = str(wire.frm), str(wire.to)
        else:
            field_end, panel_end = str(wire.to), str(wire.frm)
        ref = f"{strip}-{index}"
        session.place(
            "iec.terminals.x_2p5",
            ref=ref,
            x=230,
            y=y,
            sheet_id=sheet.id,
            properties={"strip": strip, "terminal": str(index)},
            snap=False,
        )
        y += 16
        wire.frm = panel_end
        wire.to = f"{ref}.A"
        session.wire(
            f"{ref}.B",
            field_end,
            net=wire.net or None,
            gauge=gauge or wire.gauge or None,
            color=wire.color or None,
            sheet_id=sheet.id,
        )
    session.place(
        "iec.terminals.pe_4",
        ref=f"{strip}-G",
        x=230,
        y=y,
        sheet_id=sheet.id,
        properties={"strip": strip, "terminal": "G"},
        snap=False,
    )


def _add_transmitter(session: Session, sheet_id: str) -> None:
    raw = {
        "id": "instrument.transmitter_420",
        "name": "4-20 mA transmitter",
        "category": "instrument.transmitter",
        "ref_prefix": "TT",
        "tags": ["transmitter", "analog", "field"],
        "schematic": {
            "width": 28,
            "height": 22,
            "pins": [
                {"id": "1", "x": 0, "y": 6, "side": "left"},
                {"id": "2", "x": 0, "y": 16, "side": "left"},
            ],
            "draw": [{"circle": [16, 11, 8]}, {"text": [16, 12, "TT", 3]}],
        },
        "panel": {"w": 0, "h": 0, "d": 0, "din": False, "in_panel": False, "mount": "field"},
        "props": {"location": "field", "signal": "analog"},
    }
    device = _device_from_raw("local", raw)
    session.catalog.devices[device.library_id] = device
    session.place(device.library_id, ref="TT1", x=280, y=120, sheet_id=sheet_id, snap=False)
    y = 120.0
    for index, pin in enumerate(("1", "2"), start=1):
        ref = f"TB2-{index}"
        session.place(
            "iec.terminals.x_2p5",
            ref=ref,
            x=230,
            y=y,
            sheet_id=sheet_id,
            properties={"strip": "TB2", "terminal": str(index)},
            snap=False,
        )
        session.wire(f"{ref}.B", f"TT1.{pin}", gauge="#18", sheet_id=sheet_id)
        y += 16
    session.place(
        "iec.terminals.pe_4",
        ref="TB2-G",
        x=230,
        y=y,
        sheet_id=sheet_id,
        properties={"strip": "TB2", "terminal": "G"},
        snap=False,
    )


def _add_tray(session: Session, tray_id: str) -> None:
    panel = next((sheet for sheet in session.project.sheets if sheet.kind == "panel_layout"), None)
    if panel is None:
        panel = session.add_sheet("panel", title="Enclosure", kind="panel_layout")
    if any(tray.id == tray_id for tray in panel.trays):
        return
    panel.trays.append(WireTray(id=tray_id, x=30, y=40, length_mm=500, width_mm=60))
