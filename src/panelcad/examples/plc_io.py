"""NEMA PLC-motor sample used for the I/O list and tag-export artifacts.

Circuit Builder inserts the PLC/HOA motor circuit from PR #14. An analog
module on the PLC I/O sheet adds one AI and one AO so the tag files include
Local:slot:I.Chn.Data and WX/WY rows. The level and speed terminals sit on
their own rungs, below the discrete contacts.
"""

from __future__ import annotations

from panelcad import Session


def build_plc_motor_io() -> Session:
    session = Session.create("NEMA PLC motor")
    session.set_title_block(
        company="PanelCAD",
        customer="Plant",
        drawing_number="E-PLC-IO",
        rev="A",
        title="PLC I/O",
    )
    result = session.insert_circuit(
        kind="plc_motor",
        control="local",
        hp=5,
        voltage=480,
        hoa=True,
        plc_model="nema.plc.compact_16dio",
    )
    sheet = result.get("io_sheet") or result["control_sheet"]
    session.place("nema.plc.analog_1ai_1ao", ref="AIO1", x=220, y=36, sheet_id=sheet)
    # Below the discrete ladder (OL1-96 sits near y=121). Pin A is 12 mm down,
    # so these origins land on later rungs than the overload contact.
    session.place("iec.terminals.x_2p5", ref="LT1", x=40, y=188, sheet_id=sheet)
    session.place("iec.terminals.x_2p5", ref="CV1", x=100, y=214, sheet_id=sheet)
    session.set_component_properties("LT1", {"function": "Level"}, sheet_id=sheet)
    session.set_component_properties("CV1", {"function": "Speed"}, sheet_id=sheet)
    session.wire("LT1.A", "AIO1.AI0", sheet_id=sheet, number="201")
    session.wire("AIO1.AO0", "CV1.A", sheet_id=sheet, number="202")
    return session
