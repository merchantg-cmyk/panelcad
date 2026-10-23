from panelcad import Session
from panelcad.core.cleanup import _aabb, _rung_rank
from panelcad.render.io_ladder import IN_CARD_X, LEFT_X, RUNG


def test_rung_rank_fuse_before_stop():
    assert _rung_rank("nema.protection.fuse_10a") < _rung_rank("nema.operators.pb_nc")
    assert _rung_rank("iec.protection.fuse_10a") < _rung_rank("iec.operators.pb_nc")
    assert _rung_rank("nema.terminals.pe") > _rung_rank("nema.protection.overload")
    assert _rung_rank("nema.contactors.aux_no") < _rung_rank("nema.operators.pilot_light")
    assert _rung_rank("nema.contactors.aux_no") < _rung_rank("nema.contactors.coil")


def test_cleanup_scales_piled_io_field_devices():
    s = Session.create("Pile")
    s.place("iec.plc.do16", ref="A4", x=40, y=30)
    s.place("iec.relays.k_4co_24vdc", ref="K1", x=200, y=40)
    s.place("iec.relays.k_4co_24vdc", ref="K2", x=200, y=45)
    s.wire("A4.Q0", "K1.A1")
    s.wire("A4.Q1", "K2.A1")
    s.add_sheet("io", title="I/O", kind="io")
    s.layout_io()
    io = s.sheet("io")
    h1 = s.catalog.get("iec.relays.k_4co_24vdc").schematic_height * io.component("K1").visual_scale()
    h2 = s.catalog.get("iec.relays.k_4co_24vdc").schematic_height * io.component("K2").visual_scale()
    assert h1 <= RUNG
    assert h2 <= RUNG
    result = s.cleanup("io")
    assert result["count"] == 1
    k1 = io.component("K1")
    k2 = io.component("K2")
    assert abs(k1.y - k2.y) > 2.0


def test_layout_io_input_card_right_field_not_on_card():
    s = Session.create("In")
    s.place("iec.plc.di16", ref="A3", x=40, y=30)
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pb_no", ref="S2", x=40, y=50)
    s.wire("S1.14", "A3.I0")
    s.wire("S2.14", "A3.I1")
    s.add_sheet("io", title="I/O", kind="io")
    s.layout_io()
    io = s.sheet("io")
    assert io.component("A3").x == IN_CARD_X
    assert io.component("S1").x == LEFT_X
    assert io.component("S2").x == LEFT_X
    card = _aabb(io, s.catalog, io.component("A3"))
    s1 = _aabb(io, s.catalog, io.component("S1"))
    s2 = _aabb(io, s.catalog, io.component("S2"))
    assert s1[2] <= card[0] + 0.5
    assert s2[2] <= card[0] + 0.5
    assert abs(s1[1] - s2[1]) > 2.0
    s.sheet("io").component("A3").x = 40.0
    s.sheet("io").component("S1").x = 40.0
    s.cleanup("io")
    assert io.component("A3").x == IN_CARD_X
    assert io.component("S1").x == LEFT_X


def test_offpage_wire_places_matching_arrows():
    s = Session.create("X", sheet_id="out", title="Outputs")
    s.place("iec.plc.do16", ref="A4", x=40, y=30, sheet_id="out")
    s.add_sheet("lamps", title="Lights", kind="io")
    s.place("iec.operators.pilot_light", ref="H1", x=200, y=40, sheet_id="lamps")
    result = s.wire("A4.Q0", "H1.X1", net="LT_Red", sheet_id="out", to_sheet="lamps")
    assert isinstance(result, dict)
    assert result["to_sheet"] == "lamps"
    src_op = s.sheet("out").component(result["from_op"])
    dst_op = s.sheet("lamps").component(result["to_op"])
    assert "offpage" in src_op.library_id
    assert "offpage_from" in dst_op.library_id
    assert "TO" in str(src_op.properties.get("callout"))
    assert "FROM" in str(dst_op.properties.get("callout"))
    assert any(w.to.endswith(".A") for w in s.sheet("out").wires)
    assert any(w.frm.endswith(".A") for w in s.sheet("lamps").wires)
    assert s.validate() == []


def test_solenoid_is_generic_field_symbol():
    s = Session.create("Coil")
    y = s.place("iec.loads.solenoid", ref="Y1")
    assert y.library_id == "iec.loads.solenoid"
    assert s.catalog.get(y.library_id).panel.in_panel is False
    assert {p.id for p in s.catalog.get(y.library_id).pins} >= {"A1", "A2"}


def _gap(a, b) -> float:
    """Clearance between two AABBs. Negative means overlap."""
    dx = max(a[0], b[0]) - min(a[2], b[2])
    dy = max(a[1], b[1]) - min(a[3], b[3])
    if dx > 0 and dy > 0:
        return (dx**2 + dy**2) ** 0.5
    if dx > 0:
        return dx
    if dy > 0:
        return dy
    return min(dx, dy)


def test_cleanup_demotes_coil_only_contactor():
    s = Session.create("Ctrl")
    s.place("iec.contactors.ac3_9a", ref="K1", x=80, y=80)
    s.place("iec.operators.pb_no", ref="START", x=40, y=80)
    s.wire("START.14", "K1.A1")
    s.cleanup()
    assert s.sheet().component("K1").library_id == "iec.contactors.coil"


def test_cleanup_keeps_power_contactor_with_t_leads():
    s = Session.create("Pwr")
    s.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    s.place("iec.loads.motor_3ph", ref="M1", x=120, y=40)
    s.wire("K1.T1", "M1.U1")
    s.cleanup()
    assert "ac3" in s.sheet().component("K1").library_id


def test_cleanup_lays_control_rung_left_to_right():
    s = Session.create("Rung")
    s.place("iec.operators.pb_nc", ref="STOP", x=220, y=40)
    s.place("iec.operators.pb_no", ref="START", x=40, y=200)
    s.place("iec.contactors.coil", ref="K1", x=300, y=160)
    s.wire("STOP.12", "START.13")
    s.wire("START.14", "K1.A1")
    s.cleanup()
    xs = {c.id: c.x for c in s.sheet().components}
    assert xs["STOP"] < xs["START"] < xs["K1"]
    ys = [c.y for c in s.sheet().components]
    assert max(ys) - min(ys) < 50


def test_freeform_cleanup_does_not_force_rung():
    s = Session.create("Free")
    s.set_title_block(drawing_style="freeform")
    s.place("iec.operators.pb_no", ref="START", x=40, y=40)
    s.place("iec.contactors.coil", ref="K1", x=220, y=180)
    s.wire("START.14", "K1.A1")
    s.cleanup()
    start = s.sheet().component("START")
    coil = s.sheet().component("K1")
    assert coil.y > start.y + 80
    assert abs(coil.x - 220) < 20


def test_ladder_cleanup_keeps_motor_off_control_rung():
    s = Session.create("Ladder")
    s.set_title_block(drawing_style="ladder")
    s.place("nema.operators.pb_nc", ref="STOP", x=200, y=200)
    s.place("nema.operators.pb_no", ref="START", x=40, y=40)
    s.place("nema.contactors.coil", ref="M", x=300, y=90)
    s.place("nema.loads.motor_3ph", ref="M1", x=80, y=80)
    s.wire("STOP.2", "START.3")
    s.wire("START.4", "M.A1")
    s.cleanup()

    def pin_world_y(ref: str, pid: str) -> float:
        comp = s.sheet().component(ref)
        pin = next(p for p in s.catalog.get(comp.library_id).pins if p.id == pid)
        return comp.y + pin.y * comp.visual_scale()

    y_ctrl = {pin_world_y("STOP", "2"), pin_world_y("START", "3"), pin_world_y("M", "A1")}
    assert max(y_ctrl) - min(y_ctrl) < 8
    assert s.sheet().component("M1").y > s.sheet().component("M").y + 20
    xs = [s.sheet().component(r).x for r in ("STOP", "START", "M")]
    assert xs == sorted(xs) or True
    assert s.sheet().component("STOP").x < s.sheet().component("START").x < s.sheet().component("M").x
    ends = {w.frm.split(".")[0] for w in s.sheet().wires} | {w.to.split(".")[0] for w in s.sheet().wires}
    assert "L1" in ends
    assert "L2" in ends


def test_ladder_fuse_sits_inline_on_the_rung():
    s = Session.create("Fuse flow")
    s.set_title_block(drawing_style="ladder", coil_style="square")
    s.place("nema.protection.fuse_10a", ref="FU1", x=200, y=200)
    s.place("nema.operators.pb_nc", ref="STOP", x=40, y=40)
    s.place("nema.contactors.coil", ref="M", x=300, y=90)
    s.wire("FU1.2", "STOP.1")
    s.wire("STOP.2", "M.A1")
    s.cleanup()
    from panelcad.render.sheet import pin_world

    fu = s.sheet().component("FU1")
    stop = s.sheet().component("STOP")
    assert fu.rotation == 0
    fu_y = pin_world(fu, s.catalog.get(fu.library_id).pin("1"))[1]
    stop_y = pin_world(stop, s.catalog.get(stop.library_id).pin("1"))[1]
    assert abs(fu_y - stop_y) < 2
    assert fu.x < stop.x


def test_ladder_hold_in_aux_sits_under_start():
    s = Session.create("Hold")
    s.set_title_block(drawing_style="ladder", rails="L1/L2")
    s.place("nema.operators.pb_nc", ref="STOP", x=200, y=200)
    s.place("nema.operators.pb_no", ref="START", x=40, y=40)
    s.place("nema.contactors.coil", ref="M", x=300, y=90)
    s.place("nema.protection.ol_contact", ref="OL", x=360, y=80)
    s.place("nema.contactors.aux_no", ref="M3", x=10, y=10)
    s.wire("STOP.2", "START.3")
    s.wire("START.4", "M.A1")
    s.wire("M.A2", "OL.95")
    s.wire("START.3", "M3.3")
    s.wire("M3.4", "START.4")
    s.cleanup()
    sheet = s.sheet()
    stop, start, coil, ol, hold = (sheet.component(r) for r in ("STOP", "START", "M", "OL", "M3"))
    assert stop.x < start.x < coil.x < ol.x
    from panelcad.core.ladder import rail_xs

    left, right = rail_xs(sheet)
    assert coil.x > (left + right) / 2
    assert right - ol.x < 80
    assert abs(hold.x - start.x) < 8
    assert hold.y > start.y + 10
    series_y = [stop.y, start.y, coil.y, ol.y]
    assert max(series_y) - min(series_y) < 12
    ends = {w.frm.split(".")[0] for w in sheet.wires} | {w.to.split(".")[0] for w in sheet.wires}
    assert "L1" in ends
    assert "L2" in ends
    assert not any("motor" in c.library_id for c in sheet.components)


def test_ladder_completes_aux_lamp_rung_to_rails():
    s = Session.create("Relay")
    s.set_title_block(drawing_style="ladder", rails="L1/L2")
    s.place("nema.operators.pb_no", ref="PB1", x=40, y=40)
    s.place("nema.contactors.coil", ref="CR1", x=200, y=40)
    s.place("nema.contactors.aux_no", ref="CR14", x=40, y=80)
    s.place("nema.operators.pilot_light", ref="PL1", x=120, y=80)
    s.wire("PB1.3", "CR1.A1")
    s.wire("CR14.4", "PL1.X1")
    s.cleanup()
    ends = {(w.frm, w.to) for w in s.sheet().wires} | {(w.to, w.frm) for w in s.sheet().wires}
    assert any(a == "L1" or b == "L1" for a, b in ends)
    lamp_on_net = any("PL1" in a or "PL1" in b for a, b in ends)
    aux_on_net = any("CR14" in a or "CR14" in b for a, b in ends)
    assert lamp_on_net and aux_on_net
    rails = [e for pair in ends for e in pair if e in {"L1", "L2"}]
    assert "L1" in rails and "L2" in rails


def test_ladder_wires_meet_pins_not_a_bus_through_symbols(tmp_path):
    """Rungs are pin-to-pin wires. A full L1–L2 stroke through the lamp is overlay, not a connection."""

    import re

    from panelcad.core.ladder import rail_xs
    from panelcad.render.sheet import pin_world

    s = Session.create("Relay")
    s.set_title_block(drawing_style="ladder", rails="L1/L2")
    s.place("nema.operators.pb_no", ref="PB1", x=40, y=40)
    s.place("nema.contactors.coil", ref="CR1", x=200, y=40)
    s.place("nema.contactors.aux_no", ref="CR14", x=40, y=80)
    s.place("nema.operators.pilot_light", ref="PL1", x=120, y=80)
    s.wire("PB1.4", "CR1.A1")
    s.wire("CR1.A2", "L2")
    s.wire("CR14.4", "PL1.X1")
    s.cleanup()
    svg = s.export_svg(tmp_path / "ladder.svg").read_text(encoding="utf-8")
    segs: list[tuple[float, float, float, float]] = []
    for m in re.finditer(
        r'<line x1="([^"]+)" y1="([^"]+)" x2="([^"]+)" y2="([^"]+)"', svg
    ):
        segs.append(tuple(float(g) for g in m.groups()))
    for m in re.finditer(r'<polyline points="([^"]+)"', svg):
        pts = [tuple(map(float, p.split(","))) for p in m.group(1).split()]
        segs.extend((a[0], a[1], b[0], b[1]) for a, b in zip(pts, pts[1:]))
    left, right = rail_xs(s.sheet())
    span = right - left
    through = [
        seg
        for seg in segs
        if abs(seg[1] - seg[3]) < 1.0 and abs(seg[2] - seg[0]) > span - 12
    ]
    assert not through, through
    lamp = s.sheet().component("PL1")
    lx, ly = pin_world(lamp, s.catalog.get(lamp.library_id).pins[0])
    hits_lamp = [
        seg
        for seg in segs
        if min(abs(seg[0] - lx) + abs(seg[1] - ly), abs(seg[2] - lx) + abs(seg[3] - ly)) < 4.0
    ]
    assert hits_lamp, (lx, ly, segs[:8])


def test_ladder_outputs_hug_return_rail():
    """PLC look: contacts at L1, coil and lamp at L2."""

    from panelcad.core.ladder import rail_xs

    s = Session.create("Relay")
    s.set_title_block(drawing_style="ladder", rails="L1/L2")
    s.place("nema.operators.pb_no", ref="PB1", x=40, y=40)
    s.place("nema.contactors.coil", ref="CR1", x=80, y=40)
    s.place("nema.contactors.aux_no", ref="CR14", x=40, y=80)
    s.place("nema.operators.pilot_light", ref="PL1", x=80, y=80)
    s.wire("PB1.4", "CR1.A1")
    s.wire("CR1.A2", "L2")
    s.wire("CR14.4", "PL1.X1")
    s.cleanup()
    left, right = rail_xs(s.sheet())
    mid = (left + right) / 2
    pb = s.sheet().component("PB1")
    coil = s.sheet().component("CR1")
    aux = s.sheet().component("CR14")
    lamp = s.sheet().component("PL1")
    assert pb.x < mid < coil.x
    assert aux.x < mid < lamp.x
    assert abs(coil.x - lamp.x) < 8
    from panelcad.core.sheet_quality import sheet_quality

    q = sheet_quality(s.sheet(), s.catalog, s.project)
    assert q["overlaps"] == 0
    assert q["through"] == 0


def test_ladder_lamp_is_not_in_series_with_the_coil():
    s = Session.create("Relay")
    s.set_title_block(drawing_style="ladder", rails="L1/L2")
    s.place("nema.operators.pb_no", ref="PB1", x=40, y=40)
    s.place("nema.contactors.coil", ref="CR1", x=200, y=40)
    s.place("nema.contactors.aux_no", ref="CR14", x=10, y=10)
    s.place("nema.operators.pilot_light", ref="PL1", x=300, y=40)
    s.wire("PB1.3", "CR1.A1")
    s.wire("CR1.A2", "L2")
    s.cleanup()
    pb = s.sheet().component("PB1")
    coil = s.sheet().component("CR1")
    aux = s.sheet().component("CR14")
    lamp = s.sheet().component("PL1")
    assert abs(pb.y - coil.y) < 16
    assert lamp.y > coil.y + 10
    assert abs(aux.y - lamp.y) < 16
    assert aux.x < lamp.x
    from panelcad.core.ladder import rail_xs

    left, right = rail_xs(s.sheet())
    mid = (left + right) / 2
    assert pb.x < mid
    assert aux.x < mid
    assert coil.x > mid
    assert lamp.x > mid
    assert right - lamp.x < 80
    assert right - coil.x < 80


def test_cleanup_spreads_cramped_schematic_symbols():
    s = Session.create("Crowd")
    s.place("iec.operators.pb_no", ref="S1", x=50, y=200)
    s.place("iec.operators.pb_nc", ref="STOP", x=80, y=200)
    s.place("iec.contactors.coil", ref="K1", x=80, y=80)
    s.cleanup()
    sheet = s.sheet()
    boxes = [_aabb(sheet, s.catalog, c) for c in sheet.components]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert _gap(a, b) >= 16.0, (a, b, _gap(a, b))
