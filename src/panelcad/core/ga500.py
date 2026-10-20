"""GA500 local-control schematic: Figure 2.56 3-wire plus Safe Disable.

Terminals and parameters are taken from the manuals named in the sheet note.
A generic VFD (DI1/COM) is left alone. This runs only after a GA500 catalog
row has been assigned.
"""

from __future__ import annotations

import re
from typing import Any

from panelcad.core.geometry import Point
from panelcad.core.models import PinOverride

# Programming manual TOEPYAIGA5002A: Table 2.45 p.287, Figure 2.56 and the
# 3-wire text on p.291. Installation manual TOEPC7106173TA: note *9 p.44,
# Table 8.4 p.66, Figure 8.1 p.43, notes *16 and *19 pp.44-45, Figure 18.2 p.233.
_PROG = "TOEPYAIGA5002A"
_INST = "TOEPC7106173TA"
_HD_ND = re.compile(r"ND\s+([^,]+),\s*HD\s+([^,]+)", re.IGNORECASE)
# H1 and H2 are 15.79 mm apart on the symbol. At this scale that is 12 mm,
# the same spacing as the two NC contacts on the safety switch.
_COLUMN_SCALE = 0.76
_COLUMN_TOP = 6.0
_COLUMN_GAP = 5.5
_DRIVE_POWER = frozenset({"L1", "L2", "L3", "U", "V", "W"})
# TOEPC7106173TA Figure 7.5 p.36. B is each side. C is above and below.
# Front-view A is 50 mm and is inside C.
_CLEAR_SIDE_MM = 30.0
_CLEAR_ABOVE_MM = 100.0
_CLEAR_BELOW_MM = 100.0
_FUSE_CATALOG = "FWH-70B"
# Eaton Bussmann data sheet 1200 (BH-0 series), Form BH-0, 11-20-02.
# Eaton publishes it at bus-ele-ds-1200-bh-0-series.pdf.
# Electrical characteristics: FWH 70A-100A -> BH-0122, 5/16 stud, 500 V row.
# Plan view: B is the overall length, 164.08 mm. Each base is 53.98 mm across.
# Front view: 68.20 mm from the mounting face to the top of the stud.
# The bases have screw slots. The sheet does not show a DIN clip.
# Note 1: pole-to-pole spacing is not given.
_HOLDER_CATALOG = "BH-0122"
_HOLDER_SOURCE = "Eaton Bussmann data sheet 1200"
_HOLDER_POLE_W = 53.98
_HOLDER_LEN = 164.08
_HOLDER_DEPTH = 68.20
_HOLDER_POLES = 3
# So the holder outline does not lie on the Figure 7.5 line. Not an Eaton clearance.
_DRAW_GAP_MM = 4.0


def apply_ga500_if_assigned(session: Any, result: dict[str, Any]) -> None:
    """Replace a generic VFD run string when the assigned drive is a GA500."""

    if str(result.get("kind") or "") != "vfd":
        return
    sheet = session.sheet(result["sheet"])
    drive = _by_role(sheet, "drive")
    if drive is None:
        return
    description = str(drive.properties.get("description") or "")
    if "GA500" not in description:
        return
    _apply(session, sheet, drive, result)


def sheet_net_crossings(sheet: Any, catalog: Any) -> list[str]:
    """Different nets whose drawn segments cross or overlap."""

    from panelcad.render.wiring import route_sheet_wires

    routes = route_sheet_wires(sheet, catalog)
    segs: list[tuple[str, str, Point, Point]] = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        net = str(wire.net or wire.id)
        for a, b in zip(pts, pts[1:]):
            if abs(a.x - b.x) < 0.15 and abs(a.y - b.y) < 0.15:
                continue
            segs.append((net, wire.id, a, b))
    hits: list[str] = []
    for i, (na, ida, a1, a2) in enumerate(segs):
        for nb, idb, b1, b2 in segs[i + 1 :]:
            if na == nb:
                continue
            if _segs_hit(a1, a2, b1, b2):
                hits.append(f"{na} ({ida}) crosses {nb} ({idb})")
    return hits


def sheet_close_parallels(sheet: Any, catalog: Any, *, gap: float = 6.0, run: float = 18.0) -> list[str]:
    """Different nets that run side by side long enough to read as one wire."""

    from panelcad.render.wiring import route_sheet_wires

    routes = route_sheet_wires(sheet, catalog)
    segs: list[tuple[str, str, Point, Point]] = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        net = str(wire.net or wire.id)
        for a, b in zip(pts, pts[1:]):
            if abs(a.x - b.x) < 0.15 and abs(a.y - b.y) < 0.15:
                continue
            segs.append((net, wire.id, a, b))
    hits: list[str] = []
    for i, (na, ida, a1, a2) in enumerate(segs):
        for nb, idb, b1, b2 in segs[i + 1 :]:
            if na == nb:
                continue
            if _parallel_close(a1, a2, b1, b2, gap, run):
                hits.append(f"{na} ({ida}) stacks on {nb} ({idb})")
    return hits


def _apply(session: Any, sheet: Any, drive: Any, result: dict[str, Any]) -> None:
    catalog = str(drive.properties.get("catalog") or "")
    description = str(drive.properties.get("description") or "")
    hd, nd = _ratings(description)
    new_lib = "nema.drives.ga500" if str(drive.library_id).startswith("nema.") else "iec.drives.ga500"
    _drop_control_wires(sheet, drive.id)
    _retarget(session, drive.id, new_lib)
    drive = sheet.component(drive.id)
    rating = _rating_text(catalog, hd, nd)
    drive.properties["rating_lines"] = rating
    drive.properties["ga500_3wire"] = "yes"
    _use_listed_fuse(session, sheet, result)
    _set_motor_nameplate(sheet, catalog)

    estop = _by_role(sheet, "estop")
    stop = _by_role(sheet, "stop")
    start = _by_role(sheet, "start")
    if estop is not None and stop is not None and start is not None:
        estop_lib = _estop_library(estop.library_id)
        _retarget(session, estop.id, estop_lib)
        estop = sheet.component(estop.id)
        estop.properties["tag"] = "SAFE"
        fwd = _place_fwd(session, sheet, start.library_id)
        _align_power_column(session, sheet)
        _straighten_power(session, sheet)
        _place_control(session, sheet, drive, estop, stop, start, fwd)
        _wire_control(session, sheet, drive, estop, stop, start, fwd)
        _place_on_door(session, fwd, result)
        _assign_selector(session, fwd)
        _mark_estop_unselected(session, estop)
        result.setdefault("components", []).append(
            {"id": fwd.id, "library_id": fwd.library_id, "role": "fwd_rev", "sheet": sheet.id}
        )
    else:
        _align_power_column(session, sheet)
        _straighten_power(session, sheet)

    note = _note_text(catalog, hd, nd)
    _place_note(session, sheet, note)
    _refit_panel(session, drive.id)
    for row in result.get("components") or []:
        if row.get("id") == drive.id:
            row["library_id"] = drive.library_id
        if estop is not None and row.get("id") == estop.id:
            row["library_id"] = estop.library_id
    breaker = _by_role(sheet, "breaker")
    if breaker is not None:
        for row in result.get("components") or []:
            if row.get("id") == breaker.id:
                row["library_id"] = breaker.library_id
    blurb = f"GA500 3-wire S1/S2/S5 ({_PROG} p.291). See notes."
    if blurb not in (sheet.description or ""):
        sheet.description = blurb
    from panelcad.render.wiring import clear_route_cache

    clear_route_cache()


def _ratings(description: str) -> tuple[str, str]:
    match = _HD_ND.search(description)
    if not match:
        return ("", "")
    return (match.group(2).strip(), match.group(1).strip())


def _rating_text(catalog: str, hd: str, nd: str) -> str:
    if not hd or not nd:
        return "TODO: HD/ND not in the catalog description"
    return f"HD {hd}\nND {nd}"


def _note_text(catalog: str, hd: str, nd: str) -> str:
    if hd and nd:
        rating = (
            f"{catalog}: HD {hd}. ND {nd}. "
            "Catalog amps are the Heavy Duty value. SL.GA500.01."
        )
    else:
        rating = f"TODO: {catalog} description has no HD/ND pair to draw."
    return (
        f"GA500 3-wire sequence. {_PROG} p.287 Table 2.45 and p.291 Figure 2.56.\n"
        "S1 Run (NO). Turn ON to run. S2 Stop (NC). Turn OFF to stop. "
        "Setting 3-wire on a terminal other than S1 or S2 makes S1 Run and S2 Stop.\n"
        "S5 FWD/REV. H1-05 = 0. OFF = forward, ON = reverse (Figure 2.57).\n"
        "A1-03 = 3330 (3-Wire Initialization). "
        "b1-17 = 0 (disregard an existing RUN command at power-up; default).\n"
        "Switch common is SN (0 V). Sinking, internal supply: jumper SC-SP. "
        "Do not also close SC-SN. Do not close SP-SN.\n"
        f"{_INST} note *9 p.44 and Table 8.4 p.66.\n"
        "ES1 is the safety switch on H1 and H2. "
        "The manual does not call this terminal an E-stop.\n"
        f"{_INST} Figure 8.1 pp.42-43, notes *16 and *19 p.45, Figure 18.2 p.233. "
        "Sourcing only. Remove H1-HC and H2-HC.\n"
        f"{rating}\n"
        f"Branch device is {_FUSE_CATALOG} (Eaton/Bussmann), {_INST} Table 15.4 p.218. "
        f"Three fuses on {_HOLDER_CATALOG} ({_HOLDER_SOURCE}, panel mount, not DIN; outlines touch). "
        "FAZ-C6-3-NA-L is not used.\n"
        "MTR1: 3 hp (2.2 kW), 460/480 V, FLA 4.8 A. "
        "NEC 430.250 is 4.8 A for 3 hp at 460 V. "
        f"{_INST} Table 5.4 p.19: HD 1.5 kW (3 HP) 4.8 A, ND 2.2 kW (3 HP) 5.4 A. "
        "Printed FLA is the NEC current. Printed kW is the ND motor value.\n"
        f"Clearances: {_INST} Figure 7.5 p.36. B = 30 mm each side. "
        "C = 100 mm above/below. Front A = 50 mm top/bottom.\n"
        f"4005A outline, {_INST} Table 7.9 p.31: 108 x 128 x 154 mm, same as 4005ABA.\n"
        "ES1: 2 NC twist-release, 22 mm. Part not selected. "
        "GCX1131 and GCX1136 are 1 NC (cents22mmmush.pdf).\n"
        "SEL1: GCX3300, 22 mm, 2-position, maintained, 1 NO."
    )


def _estop_library(library_id: str) -> str:
    if str(library_id).startswith("nema."):
        return "nema.operators.estop_2nc"
    return library_id


def _drop_control_wires(sheet: Any, drive_id: str) -> None:
    roles = {"estop", "stop", "start", "fwd_rev"}
    ops = {c.id for c in sheet.components if str(c.properties.get("role") or "") in roles}
    kept = []
    for wire in sheet.wires:
        drop = False
        for end in (wire.frm, wire.to):
            ref, _, pin = end.partition(".")
            if ref in ops or (ref == drive_id and pin not in _DRIVE_POWER):
                drop = True
                break
        if not drop:
            kept.append(wire)
    sheet.wires = kept


def _retarget(session: Any, ref: str, library_id: str) -> None:
    for sheet in session.project.sheets:
        try:
            comp = sheet.component(ref)
        except KeyError:
            continue
        comp.library_id = library_id


def _fwd_library(start_library: str) -> str:
    prefix = "nema" if str(start_library).startswith("nema.") else "iec"
    return f"{prefix}.operators.selector_no"


def _place_fwd(session: Any, sheet: Any, start_library: str) -> Any:
    return session.place(
        _fwd_library(start_library),
        x=20.0,
        y=20.0,
        sheet_id=sheet.id,
        properties={
            "role": "fwd_rev",
            "tag": "FWD/REV",
            "notes": f"H1-05=0. OFF = forward, ON = reverse. {_PROG} Figure 2.56.",
        },
        snap=False,
    )


def _align_power_column(session: Any, sheet: Any) -> None:
    from panelcad.render.titleblock import working_rect

    order = ("breaker", "disconnect", "contactor", "drive", "motor")
    column = [comp for role in order if (comp := _by_role(sheet, role)) is not None]
    if len(column) < 2:
        return
    _wx, wy, _ww, wh = working_rect(sheet)
    heights = [_height(session, comp) for comp in column]
    scale = _fit_scale(heights, wh)
    for comp in column:
        comp.scale = scale
    anchor = column[0]
    ref_pins = _phase_ids(session, anchor)
    ref_dev = session.catalog.get(anchor.library_id)
    anchor.x = _wx_pad(sheet) - float(ref_dev.pin(ref_pins[0]).x) * anchor.visual_scale()
    ref_world = [_world_x(anchor, ref_dev.pin(pid).x) for pid in ref_pins]
    for comp in column[1:]:
        _match_phase_x(session, comp, ref_world, anchor.x, scale)
    y = wy + _COLUMN_TOP
    gap = _column_gap(heights, wh, scale)
    for comp in column:
        comp.y = y
        y += _height(session, comp) * comp.visual_scale() + gap


def _column_gap(heights: list[float], working_h: float, scale: float) -> float:
    used = sum(heights) * scale + _COLUMN_TOP
    slack = working_h - used - 1.0
    return max(4.0, slack / max(1, len(heights) - 1))


def _fit_scale(heights: list[float], working_h: float) -> float:
    scale = _COLUMN_SCALE
    gap = _column_gap(heights, working_h, scale)
    used = sum(heights) * scale + gap * (len(heights) - 1) + _COLUMN_TOP
    if used <= working_h:
        return scale
    room = working_h - 1.0 - 4.0 * (len(heights) - 1) - _COLUMN_TOP
    return max(0.62, room / sum(heights))


def _wx_pad(sheet: Any) -> float:
    from panelcad.render.titleblock import working_rect

    wx, _wy, _ww, _wh = working_rect(sheet)
    return wx + 22.0


def _match_phase_x(session: Any, comp: Any, ref_world: list[float], anchor_x: float, scale: float) -> None:
    device = session.catalog.get(comp.library_id)
    pins = _phase_ids(session, comp)
    local = [float(device.pin(pid).x) for pid in pins]
    origin = ref_world[0] - local[0] * scale
    predicted = [origin + local[i] * scale for i in range(3)]
    if all(abs(predicted[i] - ref_world[i]) < 0.25 for i in range(3)):
        comp.x = origin
        return
    comp.x = anchor_x
    comp.scale = scale
    for pid, world_x in zip(pins, ref_world, strict=True):
        pin = device.pin(pid)
        comp.pin_overrides[pid] = PinOverride(x=(world_x - comp.x) / scale, y=float(pin.y), side=pin.side)


def _world_x(comp: Any, local_x: float) -> float:
    return comp.x + float(local_x) * comp.visual_scale()


def _phase_ids(session: Any, comp: Any) -> tuple[str, str, str]:
    device = session.catalog.get(comp.library_id)
    ids = {pin.id for pin in device.pins}
    role = str(comp.properties.get("role") or "")
    if role == "breaker":
        return ("2", "4", "6") if "2" in ids else ("T1", "T2", "T3")
    if role == "disconnect":
        return ("T1", "T2", "T3")
    if role == "contactor":
        return ("L1", "L2", "L3")
    if role == "drive":
        return ("L1", "L2", "L3")
    if "T1" in ids:
        return ("T1", "T2", "T3")
    return ("U1", "V1", "W1")


def _straighten_power(session: Any, sheet: Any) -> None:
    from panelcad.render.wiring import wire_ends

    for wire in sheet.wires:
        ax, ay, _aside, bx, by, _bside = wire_ends(sheet, session.catalog, wire)
        if abs(ax - bx) <= 0.4 and abs(ay - by) > 1.0:
            wire.points = [Point(x=ax, y=(ay + by) / 2.0)]
        elif abs(ay - by) <= 0.4 and abs(ax - bx) > 1.0:
            wire.points = [Point(x=(ax + bx) / 2.0, y=ay)]


def _place_control(session: Any, sheet: Any, drive: Any, estop: Any, stop: Any, start: Any, fwd: Any) -> None:
    from panelcad.render.titleblock import working_rect

    _wx, wy, _ww, _wh = working_rect(sheet)
    s1 = _pin_xy(session, drive, "S1")
    h1 = _pin_xy(session, drive, "H1")
    base = s1[0] + 36.0
    run_y = wy + 40.0
    fwd_y = run_y + 44.0
    _set_contact_y(session, start, _left_id(session, start), run_y)
    _set_contact_y(session, stop, _left_id(session, stop), run_y)
    _set_contact_y(session, fwd, _left_id(session, fwd), fwd_y)
    start.x = base
    stop.x = base + 38.0
    fwd.x = base + 86.0
    estop.scale = 1.0
    # Channel 1 contact sits on H1. Channel 2 is 12 mm below, matching H2 at the column scale.
    top_pin = _left_id(session, estop, 0)
    estop.y = h1[1] - _local_y(session, estop, top_pin)
    estop.x = base + 150.0
    h2 = _pin_xy(session, drive, "H2")
    _shift_lower_contact(session, estop, h2[1])


def _shift_lower_contact(session: Any, estop: Any, world_y: float) -> None:
    device = session.catalog.get(estop.library_id)
    left = _pins_on(estop, device, "left")
    right = _pins_on(estop, device, "right")
    if len(left) < 2 or len(right) < 2:
        return
    local = (world_y - estop.y) / estop.visual_scale()
    if abs(local - float(left[1].y)) < 0.4:
        return
    estop.pin_overrides[left[1].id] = PinOverride(x=float(left[1].x), y=local, side="left")
    estop.pin_overrides[right[1].id] = PinOverride(x=float(right[1].x), y=local, side="right")


def _wire_control(session: Any, sheet: Any, drive: Any, estop: Any, stop: Any, start: Any, fwd: Any) -> None:
    s1 = _pin_xy(session, drive, "S1")
    s2 = _pin_xy(session, drive, "S2")
    s5 = _pin_xy(session, drive, "S5")
    sn = _pin_xy(session, drive, "SN")
    h1 = _pin_xy(session, drive, "H1")
    h2 = _pin_xy(session, drive, "H2")
    hc = _pin_xy(session, drive, "HC")
    start_l = _pin_xy(session, start, _left_id(session, start))
    start_r = _pin_xy(session, start, _right_id(session, start))
    stop_l = _pin_xy(session, stop, _left_id(session, stop))
    stop_r = _pin_xy(session, stop, _right_id(session, stop))
    fwd_l = _pin_xy(session, fwd, _left_id(session, fwd))
    fwd_r = _pin_xy(session, fwd, _right_id(session, fwd))
    es_l1 = _pin_xy(session, estop, _left_id(session, estop, 0))
    es_r1 = _pin_xy(session, estop, _right_id(session, estop, 0))
    es_l2 = _pin_xy(session, estop, _left_id(session, estop, 1))
    es_r2 = _pin_xy(session, estop, _right_id(session, estop, 1))
    from panelcad.render.titleblock import working_rect

    wx, _wy, ww, _wh = working_rect(sheet)
    node_x = (start_r[0] + stop_l[0]) / 2.0
    s5_lane = fwd_l[0] - 8.0
    # Keep the SN common off the notes block. The notes sit at the right of the working area.
    notes_left = wx + ww - 168.0
    rail_x = min(max(stop_r[0], fwd_r[0]) + 10.0, notes_left - 8.0)
    if rail_x < max(stop_r[0], fwd_r[0]) + 4.0:
        rail_x = max(stop_r[0], fwd_r[0]) + 4.0
    if abs(rail_x - s5_lane) < 10.0:
        rail_x = s5_lane + 18.0

    _link(session, sheet, drive, "S1", start, _left_id(session, start), "RUN", [(s1[0] + 8.0, s1[1]), (s1[0] + 8.0, start_l[1])])
    _link(
        session,
        sheet,
        drive,
        "S2",
        start,
        _right_id(session, start),
        "NODE",
        [(node_x, s2[1]), (node_x, start_r[1])],
    )
    _link(session, sheet, start, _right_id(session, start), stop, _left_id(session, stop), "NODE", [])
    _link(
        session,
        sheet,
        drive,
        "S5",
        fwd,
        _left_id(session, fwd),
        "FWD",
        [(s5_lane, s5[1]), (s5_lane, fwd_l[1])],
    )
    _link(
        session,
        sheet,
        drive,
        "SN",
        stop,
        _right_id(session, stop),
        "SN",
        [(rail_x, sn[1]), (rail_x, stop_r[1])],
    )
    _link(
        session,
        sheet,
        fwd,
        _right_id(session, fwd),
        stop,
        _right_id(session, stop),
        "SN",
        [(rail_x, fwd_r[1]), (rail_x, stop_r[1])],
    )
    _link(session, sheet, drive, "H1", estop, _left_id(session, estop, 0), "SD1", [])
    _link(session, sheet, drive, "H2", estop, _left_id(session, estop, 1), "SD2", [])
    bus_x = es_r1[0] + 12.0
    low_y = max(hc[1] + 6.0, estop.y + _height(session, estop) + 6.0)
    drop_x = estop.x - 6.0
    _link(
        session,
        sheet,
        drive,
        "HC",
        estop,
        _right_id(session, estop, 0),
        "HC",
        [(drop_x, hc[1]), (drop_x, low_y), (bus_x, low_y), (bus_x, es_r1[1])],
    )
    _link(
        session,
        sheet,
        estop,
        _right_id(session, estop, 1),
        estop,
        _right_id(session, estop, 0),
        "HC",
        [(bus_x, es_r2[1]), (bus_x, es_r1[1])],
    )
    sc = _pin_xy(session, drive, "SC")
    sp = _pin_xy(session, drive, "SP")
    stub = sc[0] - 6.0
    _link(session, sheet, drive, "SC", drive, "SP", "SC-SP", [(stub, sc[1]), (stub, sp[1])])


def _link(
    session: Any,
    sheet: Any,
    a: Any,
    a_pin: str,
    b: Any,
    b_pin: str,
    net: str,
    interior: list[tuple[float, float]],
) -> None:
    wire = session.wire(f"{a.id}.{a_pin}", f"{b.id}.{b_pin}", net=net, sheet_id=sheet.id)
    wire.points = [Point(x=round(x, 2), y=round(y, 2)) for x, y in interior]


def _place_note(session: Any, sheet: Any, text: str) -> None:
    from panelcad.render.titleblock import working_rect

    wx, wy, ww, _wh = working_rect(sheet)
    # The control rail stays at wx+ww-168 (see _wire_control). Wrapped text uses
    # the real 0.62 em advance, so a 160 mm box there covers ES1. The open
    # column is to the right of the HC drop (x=298): 8 mm clear, inside the frame.
    width = 96.0
    session.place(
        "iec.notes.block",
        ref="NOTES",
        x=wx + ww - width - 3.0,
        y=wy + 2.0,
        sheet_id=sheet.id,
        properties={"role": "notes", "text": text, "width": width},
        snap=False,
    )


def _place_on_door(session: Any, fwd: Any, result: dict[str, Any] | None = None) -> None:
    """Copy the selector onto the door when it is layout-ready.

    A generic selector is refused by the layout gate. That refusal must not
    undo the rest of the insert: record the skip and leave the door alone,
    the same way circuit layout skips an unready operator.
    """

    from panelcad.core.layout import footprint_size
    from panelcad.library.panel import (
        LayoutNotReadyError,
        is_layout_ready,
        layout_ready_error,
        occupies_panel_layout,
    )

    door = next((sheet for sheet in session.project.sheets if sheet.kind == "door_layout"), None)
    if door is None:
        return
    if any(comp.id == fwd.id for comp in door.components):
        return
    device = session.catalog.get(fwd.library_id)
    parts = getattr(session, "parts", None)
    if occupies_panel_layout(device, fwd) and not is_layout_ready(device, fwd, parts=parts):
        _record_door_layout_skip(
            result,
            fwd,
            door,
            layout_ready_error(fwd.id, device, fwd, parts=parts),
        )
        return
    cursor = 12.0
    for comp in door.components:
        placed = session.catalog.get(comp.library_id)
        width, _height = footprint_size(placed, comp)
        cursor = max(cursor, comp.x + max(width, 22.0) + 12.0)
    try:
        session.place(
            fwd.library_id,
            ref=fwd.id,
            x=cursor,
            y=12.0,
            sheet_id=door.id,
            properties=dict(fwd.properties),
            snap=False,
        )
    except LayoutNotReadyError as exc:
        _record_door_layout_skip(result, fwd, door, str(exc))


def _record_door_layout_skip(
    result: dict[str, Any] | None,
    fwd: Any,
    door: Any,
    message: str,
) -> None:
    """layout_not_ready finding for a door place the gate refused."""

    finding = {
        "check": "layout_not_ready",
        "action": "skipped",
        "severity": "error",
        "ref": str(fwd.id),
        "sheet": str(getattr(door, "id", "") or ""),
        "library_id": str(fwd.library_id),
        "message": message,
    }
    if result is None:
        return
    findings = result.setdefault("findings", [])
    if isinstance(findings, list):
        findings.append(finding)
    if not message:
        return
    warnings = result.setdefault("warnings", [])
    if isinstance(warnings, list) and message not in warnings:
        warnings.append(message)
    layout = result.get("layout")
    if isinstance(layout, dict):
        layout_warnings = layout.setdefault("warnings", [])
        if isinstance(layout_warnings, list) and message not in layout_warnings:
            layout_warnings.append(message)


def _rename_from_category(session: Any, old: str) -> str:
    """Retag every copy of ``old`` from the device category on this drawing standard."""

    from panelcad.core.electrical import rename_component
    from panelcad.core.refs import next_tag, resolve_ref_prefix, sheet_drawing_standard

    home = None
    comp = None
    for page in session.project.sheets:
        try:
            found = page.component(old)
        except KeyError:
            continue
        if home is None or page.kind == "schematic":
            home = page
            comp = found
        if page.kind == "schematic":
            break
    if comp is None or home is None:
        return old
    device = session.catalog.get(comp.library_id)
    prefix = resolve_ref_prefix(
        device,
        standard=sheet_drawing_standard(session.project, home),
        maps=getattr(session.catalog, "ref_prefix_maps", None),
        overrides=getattr(session.project, "ref_prefixes", None),
    )
    tail = old[len(prefix) :] if old.startswith(prefix) else ""
    if tail.isdigit():
        return old
    used = {item.id for page in session.project.sheets for item in page.components}
    used.discard(old)
    new = next_tag(prefix, used)
    for page in list(session.project.sheets):
        if any(item.id == old for item in page.components):
            rename_component(session.project, page, old, new)
    return new


def _use_listed_fuse(session: Any, sheet: Any, result: dict[str, Any]) -> None:
    """Table 15.4 names FWH-70B. The tag follows the fuse category, not the old breaker prefix."""

    breaker = _by_role(sheet, "breaker")
    if breaker is None:
        return
    old = breaker.id
    prefix = "nema" if str(breaker.library_id).startswith("nema.") else "iec"
    library_id = f"{prefix}.protection.fuse_3p"
    _retarget(session, old, library_id)
    new = _rename_from_category(session, old)
    holder = None
    parts = getattr(session, "parts", None)
    if parts is not None:
        try:
            holder = parts.get(_HOLDER_CATALOG, "Eaton")
        except KeyError:
            holder = None
    assigned = holder.assignment_properties() if holder is not None else {}
    _write_props(
        session,
        new,
        {
            **assigned,
            "manufacturer": "Eaton/Bussmann",
            "part_number": _FUSE_CATALOG,
            "catalog": _FUSE_CATALOG,
            "description": (
                f"Semiconductor fuse {_FUSE_CATALOG}. {_INST} Table 15.4 p.218 for model 4005. "
                f"Three fuses, each on an Eaton {_HOLDER_CATALOG} block ({_HOLDER_SOURCE}: "
                "FWH 70A-100A, 5/16-18 stud, 500 V, panel mount). "
                "No AutomationDirect or LS part in this library was verified for this 480 V row."
            ),
            "notes": (
                f"{_HOLDER_SOURCE}. FWH 70A-100A -> {_HOLDER_CATALOG}. "
                f"Overall length {_HOLDER_LEN:g} mm, base width {_HOLDER_POLE_W:g} mm, "
                f"stud height {_HOLDER_DEPTH:g} mm. Not DIN. "
                "Pole spacing is not in the data sheet, so the three outlines touch."
            ),
            "catalog_verified": assigned.get("catalog_verified") or "partial",
            "catalog_source": assigned.get("catalog_source") or f"{_INST} Table 15.4 p.218",
            "catalog_key": assigned.get("catalog_key") or "",
            "dim_source": "catalog",
            "width_mm": _HOLDER_POLE_W * _HOLDER_POLES,
            "height_mm": _HOLDER_LEN,
            "depth_mm": _HOLDER_DEPTH,
            "din_rail": False,
            "in_panel": True,
            "mount": "backplate",
            "pole_count": _HOLDER_POLES,
            "pole_width_mm": _HOLDER_POLE_W,
            "pole_gap_mm": 0,
            "pole_label": _HOLDER_CATALOG,
        },
        clear=True,
    )
    _rewrite_preferred(
        result,
        old,
        id=new,
        manufacturer="Eaton/Bussmann",
        catalog=_FUSE_CATALOG,
        verified="partial",
        source=f"{_INST} Table 15.4 p.218",
        library_id=library_id,
        reason="Yaskawa lists this fuse. No library breaker was verified at 480 V for 4005.",
    )
    for row in result.get("components") or []:
        if str(row.get("id") or "") == old:
            row["id"] = new
            row["library_id"] = library_id


def _set_motor_nameplate(sheet: Any, catalog: str) -> None:
    """The 1.5 kW / 4.8 A sample does not match a 3 hp motor. Leave other nameplates alone."""

    if "4005" not in catalog:
        return
    motor = _by_role(sheet, "motor")
    if motor is None:
        return
    kw = _as_float(motor.properties.get("kW"))
    fla = _as_float(motor.properties.get("FLA", motor.properties.get("fla")))
    hp = _as_float(motor.properties.get("hp"))
    if hp is not None and abs(hp - 3.0) < 0.05 and kw is not None and abs(kw - 2.2) < 0.05:
        motor.properties["voltage"] = 480
        motor.properties["voltage_label"] = "460/480"
        motor.properties["FLA"] = 4.8
        return
    kw_is_sample = kw is not None and abs(kw - 1.5) < 0.05
    fla_is_sample = fla is not None and abs(fla - 4.8) < 0.05
    if not kw_is_sample and not fla_is_sample:
        return
    motor.properties["hp"] = 3
    motor.properties["kW"] = 2.2
    motor.properties["FLA"] = 4.8
    motor.properties["voltage"] = 480
    motor.properties["voltage_label"] = "460/480"


def _assign_selector(session: Any, fwd: Any) -> None:
    session.assign_part(fwd.id, "GCX3300", manufacturer="AutomationDirect")
    _write_props(
        session,
        fwd.id,
        {"catalog_fallback": "yes", "catalog_primary": "Yaskawa"},
        clear=False,
    )


def _mark_estop_unselected(session: Any, estop: Any) -> None:
    _write_props(
        session,
        estop.id,
        {
            "manufacturer": "",
            "part_number": "part not selected",
            "catalog": "",
            "description": "Emergency stop, 2 NC, twist-release, 22 mm. Part not selected.",
            "catalog_verified": "",
            "catalog_source": "",
            "notes": (
                "GCX1131 and GCX1136 are 1 NC twist-release (cents22mmmush.pdf). "
                "Safe Disable uses two channels, so those parts are not used."
            ),
        },
        clear=True,
    )


def _refit_panel(session: Any, drive_id: str) -> None:
    """Put the fuse holders on the backplate, clear of the drive keep-out.

    BH-0122 is panel-mounted. A DIN rail is drawn only for parts that are
    actually DIN, and that rail is only as long as those parts.
    """

    from panelcad.core.layout import MODULE_GAP_MM, NAME_STRIP_MM, RAIL_HEIGHT_MM, fit_enclosure_scale
    from panelcad.core.models import Enclosure, Rail
    from panelcad.library.panel import effective_footprint
    from panelcad.render.titleblock import working_rect

    panel = next((item for item in session.project.sheets if item.kind == "panel_layout"), None)
    if panel is None or panel.enclosure is None:
        return
    din: list[tuple[Any, float, float]] = []
    field: list[tuple[Any, float, float]] = []
    for comp in panel.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        fp = effective_footprint(device, comp)
        saved = comp.scale
        comp.scale = 1.0
        width = float(fp.width_mm)
        height = float(fp.height_mm)
        comp.scale = saved
        if width <= 0 or height <= 0:
            continue
        role = str(comp.properties.get("role") or "")
        if role == "drive" or fp.mount == "backplate":
            field.append((comp, width, height))
        elif fp.din_rail:
            din.append((comp, width, height))
    if not din and not field:
        return
    din.sort(key=lambda row: row[0].id)
    holders: list[tuple[Any, float, float]] = []
    drives: list[tuple[Any, float, float]] = []
    other_field: list[tuple[Any, float, float]] = []
    for comp, width, height in field:
        role = str(comp.properties.get("role") or "")
        if comp.id == drive_id or role == "drive":
            drives.append((comp, width, height))
        elif role == "breaker" or comp.properties.get("pole_count"):
            holders.append((comp, width, height))
        else:
            other_field.append((comp, width, height))
    gap = MODULE_GAP_MM
    end = 4.0
    span = sum(width for _comp, width, _height in din) + gap * max(0, len(din) - 1)
    rail_len = (end + span + end) if din else 0.0
    max_h = max((height for _comp, _width, height in din), default=0.0)
    wall = 8.0
    holder_w = max((width for _comp, width, _height in holders), default=0.0)
    drive_w = max((width for _comp, width, _height in drives), default=0.0)
    inner_w = max(rail_len, holder_w, drive_w + 2 * _CLEAR_SIDE_MM, 80.0)
    enc_w = inner_w + 2 * wall
    top = NAME_STRIP_MM + 6.0
    cursor_y = top
    rail_y = top
    if din:
        rail_y = top + max_h / 2.0
        cursor_y = top + max_h + 8.0
    places: list[tuple[Any, float, float, str]] = []
    for comp, width, height in holders:
        places.append((comp, wall + (inner_w - width) / 2.0, cursor_y, "holder"))
        cursor_y += height + _DRAW_GAP_MM
    for comp, width, height in drives:
        drive_y = cursor_y + _CLEAR_ABOVE_MM
        places.append((comp, wall + (inner_w - width) / 2.0, drive_y, "drive"))
        cursor_y = drive_y + height + _CLEAR_BELOW_MM
    for comp, width, height in other_field:
        places.append((comp, wall + (inner_w - width) / 2.0, cursor_y, "field"))
        cursor_y += height + 8.0
    enc_h = cursor_y + wall
    wx, wy, ww, wh = working_rect(panel)
    scale = fit_enclosure_scale(enc_w, enc_h, ww - 8.0, wh - 12.0)
    ox, oy = wx + 4.0, wy + 4.0
    panel.enclosure = Enclosure(
        x=ox,
        y=oy,
        width_mm=enc_w,
        height_mm=enc_h,
        depth_mm=0.0,
        name=(
            f"Backplate {enc_w:.0f}×{enc_h:.0f} mm. "
            "Figure 7.5 clearances. Cabinet not selected."
        ),
        draw_scale=scale,
    )
    panel.trays = []
    if din:
        rail_x = wall + max(0.0, (inner_w - rail_len) / 2.0)
        panel.rails = [
            Rail(
                id="r1",
                x=ox + rail_x * scale,
                y=oy + rail_y * scale,
                length_mm=rail_len * scale,
                height_mm=RAIL_HEIGHT_MM * scale,
            )
        ]
        group = sum(width for _c, width, _h in din) + gap * max(0, len(din) - 1)
        cursor = rail_x + (rail_len - group) / 2.0
        for comp, width, height in din:
            comp.x = ox + cursor * scale
            comp.y = oy + (rail_y - height / 2.0) * scale
            comp.scale = scale
            comp.properties["din_clip"] = "center"
            cursor += width + gap
    else:
        panel.rails = []
    for comp, x, y, kind in places:
        comp.x = ox + x * scale
        comp.y = oy + y * scale
        comp.scale = scale
        comp.properties["mount"] = "backplate"
        comp.properties["din_rail"] = False
        if kind == "drive":
            comp.properties["keepout_left_mm"] = _CLEAR_SIDE_MM
            comp.properties["keepout_right_mm"] = _CLEAR_SIDE_MM
            comp.properties["keepout_top_mm"] = _CLEAR_ABOVE_MM
            comp.properties["keepout_bottom_mm"] = _CLEAR_BELOW_MM
            comp.properties["keepout_note"] = (
                f"Fig 7.5 p.36: B 30 mm sides, C 100 mm above/below (front A is 50 mm)"
            )
    schematic = session.project.sheets
    for sheet in schematic:
        if sheet.kind == "panel_layout":
            continue
        try:
            drive = sheet.component(drive_id)
        except KeyError:
            continue
        drive.properties["keepout_left_mm"] = _CLEAR_SIDE_MM
        drive.properties["keepout_right_mm"] = _CLEAR_SIDE_MM
        drive.properties["keepout_top_mm"] = _CLEAR_ABOVE_MM
        drive.properties["keepout_bottom_mm"] = _CLEAR_BELOW_MM
        drive.properties["keepout_note"] = f"{_INST} Figure 7.5 p.36. B 30 mm sides. C 100 mm above and below."


_ASSIGNED_CLEAR = (
    "manufacturer",
    "part_number",
    "catalog",
    "description",
    "catalog_verified",
    "catalog_source",
    "catalog_key",
    "catalog_fallback",
    "catalog_primary",
    "amps",
    "amps_min",
    "amps_max",
    "In",
    "Ie",
    "range",
    "poles",
    "coil_voltage",
    "coil",
    "contact_config",
    "ka",
    "Icu",
    "sccr",
    "heat_w",
    "width_mm",
    "height_mm",
    "depth_mm",
    "dim_source",
    "mount",
    "din_rail",
)


def _write_props(session: Any, ref: str, props: dict[str, Any], *, clear: bool) -> None:
    for sheet in session.project.sheets:
        try:
            comp = sheet.component(ref)
        except KeyError:
            continue
        if clear:
            for key in _ASSIGNED_CLEAR:
                comp.properties.pop(key, None)
        comp.properties.update(props)


def _rewrite_preferred(result: dict[str, Any], ref: str, **fields: Any) -> None:
    rows = result.get("preferred")
    if not isinstance(rows, list):
        return
    for row in rows:
        if str(row.get("id") or "") == ref:
            row.pop("catalog_fallback", None)
            row.pop("catalog_primary", None)
            row.update(fields)
            return


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _by_role(sheet: Any, role: str) -> Any:
    return next((comp for comp in sheet.components if str(comp.properties.get("role") or "") == role), None)


def _height(session: Any, comp: Any) -> float:
    return float(session.catalog.get(comp.library_id).schematic_height)


def _pin_xy(session: Any, comp: Any, pin_id: str) -> tuple[float, float]:
    from panelcad.render.sheet import pin_world

    device = session.catalog.get(comp.library_id)
    return pin_world(comp, device.pin(pin_id))


def _local_y(session: Any, comp: Any, pin_id: str) -> float:
    device = session.catalog.get(comp.library_id)
    return float(comp.pin_xy(device.pin(pin_id))[1]) * comp.visual_scale()


def _pins_on(comp: Any, device: Any, side: str) -> list[Any]:
    pins = [pin for pin in device.pins if comp.pin_side(pin) == side]
    pins.sort(key=lambda pin: comp.pin_xy(pin)[1])
    return pins


def _left_id(session: Any, comp: Any, index: int = 0) -> str:
    device = session.catalog.get(comp.library_id)
    return _pins_on(comp, device, "left")[index].id


def _right_id(session: Any, comp: Any, index: int = 0) -> str:
    device = session.catalog.get(comp.library_id)
    return _pins_on(comp, device, "right")[index].id


def _set_contact_y(session: Any, comp: Any, pin_id: str, world_y: float) -> None:
    comp.y = world_y - _local_y(session, comp, pin_id)


def _segs_hit(a: Point, b: Point, c: Point, d: Point, eps: float = 0.4) -> bool:
    ax, ay, bx, by = a.x, a.y, b.x, b.y
    cx, cy, dx, dy = c.x, c.y, d.x, d.y
    if max(ax, bx) + eps < min(cx, dx) or max(cx, dx) + eps < min(ax, bx):
        return False
    if max(ay, by) + eps < min(cy, dy) or max(cy, dy) + eps < min(ay, by):
        return False
    if abs(ay - by) <= eps and abs(cy - dy) <= eps and abs(ay - cy) <= eps:
        return _overlap(ax, bx, cx, dx) > 0.6
    if abs(ax - bx) <= eps and abs(cx - dx) <= eps and abs(ax - cx) <= eps:
        return _overlap(ay, by, cy, dy) > 0.6
    d1 = _cross(cx, cy, dx, dy, ax, ay)
    d2 = _cross(cx, cy, dx, dy, bx, by)
    d3 = _cross(ax, ay, bx, by, cx, cy)
    d4 = _cross(ax, ay, bx, by, dx, dy)
    if d1 * d2 < -1e-4 and d3 * d4 < -1e-4:
        return True
    return _endpoint_on(a, b, c, d, eps) or _endpoint_on(c, d, a, b, eps)


def _parallel_close(a: Point, b: Point, c: Point, d: Point, gap: float, run: float) -> bool:
    if abs(a.y - b.y) <= 0.4 and abs(c.y - d.y) <= 0.4 and abs(a.y - c.y) < gap:
        return _overlap(a.x, b.x, c.x, d.x) > run
    if abs(a.x - b.x) <= 0.4 and abs(c.x - d.x) <= 0.4 and abs(a.x - c.x) < gap:
        return _overlap(a.y, b.y, c.y, d.y) > run
    return False


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    left = max(min(a0, a1), min(b0, b1))
    right = min(max(a0, a1), max(b0, b1))
    return right - left


def _cross(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _endpoint_on(a: Point, b: Point, c: Point, d: Point, eps: float) -> bool:
    return _point_on_seg(a, c, d, eps) or _point_on_seg(b, c, d, eps)


def _point_on_seg(p: Point, a: Point, b: Point, eps: float) -> bool:
    if abs(a.x - b.x) <= eps:
        if abs(p.x - a.x) > eps:
            return False
        lo, hi = sorted((a.y, b.y))
        return lo - eps <= p.y <= hi + eps and min(abs(p.y - a.y), abs(p.y - b.y)) > 0.6
    if abs(a.y - b.y) <= eps:
        if abs(p.y - a.y) > eps:
            return False
        lo, hi = sorted((a.x, b.x))
        return lo - eps <= p.x <= hi + eps and min(abs(p.x - a.x), abs(p.x - b.x)) > 0.6
    return False
