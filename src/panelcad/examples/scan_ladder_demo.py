"""Demo prints for the scan ladder.

One motor-control set a drafter would issue: an elementary ladder, a
backplate, and the terminal strip for that ladder. Names are fake. Every
panel millimetre comes from a published catalog row.
"""

from __future__ import annotations

from panelcad.api.session import Session
from panelcad.core.layout import (
    footprint_overlaps_tray,
    footprint_size,
    panel_furniture,
    place_on_rail,
    sits_fully_on_a_rail,
)
from panelcad.core.elementary_layout import layout_elementary

# Backplate for a 24 x 20 x 8 in NEMA 12 wall mount (libraries/enclosures.yaml).
_PLATE_W_MM = 17.0 * 25.4
_PLATE_H_MM = 21.0 * 25.4
_BOX_D_MM = 8.0 * 25.4


def build_scan_ladder_demo(name: str = "Demo motor control") -> Session:
    """Elementary, backplate, and terminal strip for one fake starter."""

    session = Session.create(name, sheet_id="elementary", title="Elementary")
    session.set_title_block(
        company="Demo Controls",
        location="Demo MCC",
        drawing_number="E-100",
        drawn_by="Demo",
        standard="NEMA/ANSI",
        drawing_style="ladder",
        coil_style="circle",
        description="Primary fuses and control transformer, then the 120 V seal-in",
    )
    sheet = session.project.sheets[0]
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    _elementary_facts(session)
    # Incoming coordinates are noise. The engine issues the sheet.
    for comp in session.project.sheet("elementary").components:
        comp.x, comp.y = 4.0, 4.0
    layout_elementary(session, session.project.sheet("elementary"))

    _backplate(session)
    session.add_sheet("strip", title="Terminal strip X1", kind="terminal")
    session.project.sheet("strip").strip = "X1"
    session.project.sheet("strip").page.size = "TABLOID"
    session.project.sheet("strip").page.orientation = "landscape"

    _clear_numbers(session)
    session.number_wires(overwrite=True)
    _split_terminal_numbers(session)
    return session


def _elementary_facts(session: Session) -> None:
    """Devices and wires only. Coordinates are not a drawing."""

    def add(library_id: str, ref: str, **props: str) -> None:
        session.place(library_id, ref=ref, x=0, y=0, snap=False, properties=props or None)

    add("nema.protection.fuse_10a", "FU1")
    add("nema.power.control_transformer", "T1")
    add("nema.protection.fuse_10a", "FU2")
    add("nema.operators.estop", "ES1")
    add("nema.operators.pb_nc", "PB2")
    add("nema.operators.pb_no", "PB1")
    add("nema.contactors.coil", "M1")
    add("nema.protection.ol_contact", "OL1")
    add("nema.contactors.aux_no", "M1-13")
    add("nema.contactors.aux_no", "M1-23")
    add("nema.contactors.coil", "CR1")
    add("nema.contactors.aux_no", "CR1-13")
    add("nema.operators.pilot_light", "PL1")
    add("nema.contactors.aux_no", "M1-33")
    for tag in ("X1-1", "X1-2", "X1-3", "X1-4"):
        add("iec.terminals.phoenix_ut_2p5", tag, **({"type": "PE"} if tag == "X1-4" else {}))

    session.wire("L1", "FU1.1")
    session.wire("FU1.2", "T1.H1")
    session.wire("T1.H4", "FU2.1")
    session.wire("FU2.2", "L2")
    session.wire("L1", "ES1.1")
    session.wire("ES1.2", "PB2.1")
    session.wire("PB2.2", "PB1.3")
    session.wire("PB1.4", "M1.A1")
    session.wire("M1.A2", "OL1.95")
    session.wire("OL1.96", "L2")
    # Seal-in parallels the start button.
    session.wire("PB2.2", "M1-13.3")
    session.wire("M1-13.4", "PB1.4")
    session.wire("L1", "M1-23.3")
    session.wire("M1-23.4", "CR1.A1")
    session.wire("CR1.A2", "L2")
    session.wire("L1", "CR1-13.3")
    session.wire("CR1-13.4", "PL1.X1")
    session.wire("PL1.X2", "L2")
    session.wire("L1", "M1-33.3")
    session.wire("M1-33.4", "X1-1.A")
    session.wire("X1-1.B", "L2")
    session.wire("X1-2.A", "X1-3.A")
    session.wire("X1-2.B", "L2")
    session.wire("X1-3.B", "L2")
    session.wire("X1-4.A", "X1-2.A")
    session.wire("X1-4.B", "L2")


def _split_terminal_numbers(session: Session) -> None:
    """Panel-side and field-side numbers stay different on each block.

    A feed-through bonds the two pins into one net. The printed strip still
    shows the panel number above the tag and the field number below it.
    """

    panel = {"X1-1.A": "201", "X1-2.A": "202", "X1-3.A": "202", "X1-4.A": "204"}
    field = {"X1-1.B": "301", "X1-2.B": "302", "X1-3.B": "303", "X1-4.B": "304"}
    wanted = {pin.upper(): number for pin, number in {**panel, **field}.items()}
    for wire in session.project.sheet("elementary").wires:
        ends = {str(wire.frm).upper(), str(wire.to).upper()}
        hits = [wanted[pin] for pin in ends if pin in wanted]
        if not hits or len(set(hits)) != 1:
            continue
        wire.number = hits[0]
        wire.number_locked = True


def _clear_numbers(session: Session) -> None:
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            wire.net = None
            wire.label = ""
            wire.number = ""
            wire.number_locked = False


def _backplate(session: Session) -> None:
    """Power on the top rail, control in the middle, terminals on the bottom rail."""

    session.add_sheet("backplate", title="Backplate", kind="panel_layout")
    sheet = session.project.sheet("backplate")
    sheet.page.size = "ANSI_D"
    sheet.page.orientation = "portrait"
    session.set_enclosure(
        width_mm=_PLATE_W_MM,
        height_mm=_PLATE_H_MM,
        depth_mm=_BOX_D_MM,
        x=28.0,
        y=36.0,
        name="Backplate for NEMA 12, 24 x 20 x 8 in",
        sheet_id="backplate",
        panel_width_mm=_PLATE_W_MM,
        panel_height_mm=_PLATE_H_MM,
    )
    enc = sheet.enclosure
    assert enc is not None
    rails, trays = panel_furniture(enc)
    for item in (*rails, *trays):
        item.x += enc.x
        item.y += enc.y
    power = rails[0]
    gap = 130.0
    control = session.add_rail("r-control", power.x, power.y + gap, power.length_mm, sheet_id="backplate")
    field = session.add_rail("r-field", power.x, power.y + 2 * gap, power.length_mm, sheet_id="backplate")
    power.id = "r-power"
    sheet.rails = [power, control, field]
    sheet.trays = trays

    # Published outlines. The symbol stays NEMA so a JIC tag is still a JIC tag.
    _place_assigned(session, "DISC1", "nema.breakers.mcb_3p_32a", "FAZ-C6-3-NA-L", "backplate")
    _place_assigned(session, "FU1", "nema.protection.fuse_10a", "EHCC3DIU", "backplate")
    _place_assigned(session, "M1", "nema.contactors.ac3_9a", "GH15BN-3-10A", "backplate")
    _place_assigned(session, "PS1", "nema.power.psu_24v_5a", "PSP24-060S", "backplate")
    _place_assigned(session, "CR1", "nema.contactors.ac3_9a", "GH15BN-3-10A", "backplate")
    for tag in ("X1-1", "X1-2", "X1-3", "X1-4"):
        session.place(
            "iec.terminals.phoenix_ut_2p5",
            ref=tag,
            x=0,
            y=0,
            sheet_id="backplate",
            snap=False,
            properties={"type": "PE"} if tag == "X1-4" else None,
        )

    _seat(session, "backplate", power, ("DISC1", "FU1", "M1"))
    _seat(session, "backplate", control, ("PS1", "CR1"))
    _seat(session, "backplate", field, ("X1-1", "X1-2", "X1-3", "X1-4"))
    session.delete_sheet("part-hold")


def _place_assigned(session: Session, ref: str, library_id: str, catalog: str, sheet_id: str) -> None:
    """Assign a published outline, then seat the part on the backplate.

    A layout sheet refuses a generic body. The part is created on a holding
    schematic, given its catalog row, and moved. The elementary keeps its own
    symbol when the tag is already there.
    """

    hold_id = "part-hold"
    if hold_id not in {item.id for item in session.project.sheets}:
        session.add_sheet(hold_id, title="Part hold", kind="schematic")
    session.place(library_id, ref=ref, x=0, y=0, sheet_id=hold_id, snap=False)
    session.assign_part(ref, catalog, manufacturer="AutomationDirect")
    hold = session.project.sheet(hold_id)
    comp = hold.component(ref)
    hold.components = [item for item in hold.components if item.id != ref]
    session.project.sheet(sheet_id).components.append(comp)


def _seat(session: Session, sheet_id: str, rail, refs: tuple[str, ...]) -> None:
    sheet = session.project.sheet(sheet_id)
    x = float(rail.x) + 2.0
    for ref in refs:
        comp = sheet.component(ref)
        device = session.catalog.get(comp.library_id)
        width, _height = footprint_size(device, comp)
        comp.x, comp.y = place_on_rail(device, rail, x, comp)
        x = float(comp.x) + width + 6.0


def layout_problems(session: Session) -> list[str]:
    """Devices sitting on a duct, or a DIN module that missed its rail."""

    problems: list[str] = []
    for sheet in session.project.sheets:
        if sheet.kind != "panel_layout":
            continue
        for comp in sheet.components:
            device = session.catalog.get(comp.library_id)
            width, height = footprint_size(device, comp)
            if width > 0 and height > 0 and footprint_overlaps_tray(comp.x, comp.y, width, height, sheet.trays):
                problems.append(f"{comp.id} on_wire_duct")
            if not sits_fully_on_a_rail(comp, device, sheet.rails):
                problems.append(f"{comp.id} off_din_rail")
    return problems
