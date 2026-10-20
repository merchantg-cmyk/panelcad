"""NEMA start/stop seal-in and an IEC contact-mirror sample.

Circuits are real ladders: coils return to L2, off-page arrows carry a control
signal from the L1 side or a contact output. Wire numbers come from the
existing auto-numberer — this file does not invent them.
"""

from __future__ import annotations

from panelcad.api.session import Session
from panelcad.core.zones import zone_grid


def _pin_y(session: Session, library_id: str, pin_id: str) -> float:
    return float(session.catalog.get(library_id).pin(pin_id).y)


def _on_rung(
    session: Session,
    library_id: str,
    ref: str,
    x: float,
    rung_y: float,
    pin: str,
    *,
    sheet_id: str | None = None,
    properties: dict | None = None,
):
    return session.place(
        library_id,
        ref=ref,
        x=x,
        y=rung_y - _pin_y(session, library_id, pin),
        sheet_id=sheet_id,
        properties=properties,
        snap=False,
    )


def _rung_y(session: Session, index: int, sheet_id: str | None = None) -> float:
    from panelcad.core.zones import rung_y

    return rung_y(session.sheet(sheet_id), index, project=session.project)


def _clear_then_number(session: Session) -> None:
    """Drop builder nets/labels and run the project auto-numberer."""

    for sheet in session.project.sheets:
        for wire in sheet.wires:
            ids = f"{wire.frm} {wire.to}"
            # Same conductor through the off-page pair — one net, one number.
            wire.net = "OP" if "OP1" in ids or "OP2" in ids else None
            wire.label = ""
            wire.number = ""
            wire.number_locked = False
    session.number_wires(overwrite=True)


def build_nema_seal_in(name: str = "NEMA seal-in") -> Session:
    """Sheet 1: stop / start / M1 + seal-in, CR coil to L2, CR NO off-page.

    Sheet 2: FROM that contact output through CR1 NC and PL1 to L2.
    """

    s = Session.create(name, sheet_id="control", title="Control")
    s.set_title_block(
        company="ECS Shop",
        customer="Ada",
        location="MCC demo",
        drawing_number="E-XREF",
        drawn_by="PanelCAD",
        standard="NEMA/ANSI",
        drawing_style="ladder",
        description="Start/stop seal-in with CR cross-reference",
    )

    # Line 102 — L1 STOP START M1 OL L2
    y102 = _rung_y(s, 2)
    _on_rung(s, "nema.operators.pb_nc", "PB1", 46, y102, "1")
    _on_rung(s, "nema.operators.pb_no", "PB2", 88, y102, "3")
    _on_rung(s, "nema.contactors.coil", "M1", 210, y102, "A1")
    _on_rung(s, "nema.protection.ol_contact", "OL1", 255, y102, "95")
    s.wire("L1", "PB1.1")
    s.wire("PB1.2", "PB2.3")
    s.wire("PB2.4", "M1.A1")
    s.wire("M1.A2", "OL1.95")
    s.wire("OL1.96", "L2")

    # Line 104 — M1 seal-in around START (own rung)
    y104 = _rung_y(s, 4)
    _on_rung(s, "nema.contactors.aux_no", "M1-13", 88, y104, "3")
    s.wire("PB2.3", "M1-13.3")
    s.wire("M1-13.4", "M1.A1")

    # Line 106 — CR1 coil straight L1 to L2
    y106 = _rung_y(s, 6)
    _on_rung(s, "nema.contactors.coil", "CR1", 210, y106, "A1")
    s.wire("L1", "CR1.A1")
    s.wire("CR1.A2", "L2")

    # Line 108 — L1 CR1-13 (NO) TO sheet 2
    y108 = _rung_y(s, 8)
    _on_rung(s, "nema.contactors.aux_no", "CR1-13", 88, y108, "3")
    _on_rung(
        s,
        "iec.connection.offpage",
        "OP1",
        155,
        y108,
        "A",
        properties={
            "dest_sheet": "aux",
            "dest_ref": "CR1-21",
            "dest_pin": "1",
            "role": "to",
            "link": "control:OP1.A|aux:CR1-21.1",
        },
    )
    s.wire("L1", "CR1-13.3")
    s.wire("CR1-13.4", "OP1.A")

    s.add_sheet("aux", kind="schematic", title="Auxiliary")
    s.set_title_block(description="CR1 NC continued from sheet 1")

    # Line 202 — FROM sheet 1 through CR1-21 and PL1 to L2 (no second L1 feed)
    y202 = _rung_y(s, 2, "aux")
    _on_rung(
        s,
        "iec.connection.offpage_from",
        "OP2",
        40,
        y202,
        "A",
        sheet_id="aux",
        properties={
            "dest_sheet": "control",
            "dest_ref": "CR1-13",
            "dest_pin": "4",
            "role": "from",
            "link": "control:CR1-13.4|aux:OP2.A",
        },
    )
    _on_rung(s, "nema.contactors.aux_nc", "CR1-21", 110, y202, "1", sheet_id="aux")
    _on_rung(s, "nema.operators.pilot_light", "PL1", 200, y202, "X1", sheet_id="aux")
    s.wire("OP2.A", "CR1-21.1", sheet_id="aux")
    s.wire("CR1-21.2", "PL1.X1", sheet_id="aux")
    s.wire("PL1.X2", "L2", sheet_id="aux")

    _clear_then_number(s)
    return s


def build_iec_mirror(name: str = "IEC contact mirror") -> Session:
    """K1 on L+/0V: S1, 13-14 seal-in, 21-22 to H1. Unique aux terminals."""

    s = Session.create(name, sheet_id="main", title="Control")
    s.set_title_block(
        company="Acme",
        drawing_number="E-IEC",
        standard="IEC",
        drawing_style="ladder",
        rail_hot="L+",
        rail_return="0V",
        description="Coil with IEC contact mirror",
    )

    def _column_x(sheet_id: str, col: int) -> float:
        grid = zone_grid(s.sheet(sheet_id))
        # Left edge of the column plus a small inset so location.x stays in-col.
        return grid.wx + (col - 1) * grid.cw + 2.0

    x_col3 = _column_x("main", 3)
    y2 = _rung_y(s, 2)
    _on_rung(s, "iec.operators.pb_no", "S1", x_col3, y2, "13")
    _on_rung(s, "iec.contactors.coil", "K1", 210, y2, "A1")
    s.wire("L+", "S1.13")
    s.wire("S1.14", "K1.A1")
    s.wire("K1.A2", "0V")

    y4 = _rung_y(s, 4)
    _on_rung(s, "iec.contactors.aux_no", "K1-14", x_col3, y4, "13")
    s.wire("S1.13", "K1-14.13")
    s.wire("K1-14.14", "K1.A1")

    y6 = _rung_y(s, 6)
    _on_rung(s, "iec.contactors.aux_nc", "K1-21", x_col3, y6, "21")
    _on_rung(s, "iec.operators.pilot_light", "H1", 210, y6, "X1")
    s.wire("L+", "K1-21.21")
    s.wire("K1-21.22", "H1.X1")
    s.wire("H1.X2", "0V")

    s.add_sheet("page2", kind="schematic", title="Contacts")
    s.set_title_block(description="K1 43-44 feeding H2")
    y2p = _rung_y(s, 2, "page2")
    _on_rung(s, "iec.contactors.aux_no", "K1-43", _column_x("page2", 3), y2p, "13", sheet_id="page2")
    _on_rung(s, "iec.operators.pilot_light", "H2", 200, y2p, "X1", sheet_id="page2")
    s.wire("L+", "K1-43.13", sheet_id="page2")
    s.wire("K1-43.14", "H2.X1", sheet_id="page2")
    s.wire("H2.X2", "0V", sheet_id="page2")
    _clear_then_number(s)
    return s
