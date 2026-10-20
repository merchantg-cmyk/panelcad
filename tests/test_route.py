from panelcad import Session
from panelcad.core.route import coincident_run_length, path_hits_obstacles, set_run_channel
from panelcad.core.geometry import Point
from panelcad.render.sheet import iter_wire_points
from panelcad.render.wiring import component_obstacle, sheet_obstacles


def test_rails_alone_do_not_cut_through_a_symbol():
    """L1/L2 on a power sheet is not a ladder. The wire goes around the part."""

    s = Session.create("Power rails")
    s.set_title_block(rails="L1/L2")
    s.place("iec.operators.pb_no", ref="S1", x=30, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=200, y=40)
    blocker = s.place("iec.relays.k_4co_24vdc", ref="K1", x=100, y=28)
    s.wire("S1.14", "H1.X1", net="RUN")
    pts = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[0])
    for a, b in zip(pts, pts[1:]):
        assert abs(a.x - b.x) < 0.8 or abs(a.y - b.y) < 0.8
    box = component_obstacle(s.sheet(), s.catalog, blocker)
    assert not path_hits_obstacles(pts, [box])


def test_stored_diagonal_midpoint_is_replaced():
    s = Session.create("Diagonal")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=180, y=110)
    s.wire("S1.14", "H1.X1")
    wire = s.sheet().wires[0]
    ends = iter_wire_points(s.sheet(), s.catalog, wire)
    ax, ay, bx, by = ends[0].x, ends[0].y, ends[-1].x, ends[-1].y
    s.set_wire_route(wire.id, [{"x": (ax + bx) / 2.0, "y": (ay + by) / 2.0}])
    assert wire.points == []
    pts = iter_wire_points(s.sheet(), s.catalog, wire)
    for a, b in zip(pts, pts[1:]):
        assert abs(a.x - b.x) < 0.8 or abs(a.y - b.y) < 0.8, (a, b)


def test_wire_goes_around_middle_device():
    s = Session.create("Route")
    s.place("iec.operators.pb_no", ref="S1", x=30, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=200, y=40)
    blocker = s.place("iec.relays.k_4co_24vdc", ref="K1", x=100, y=28)
    s.wire("S1.14", "H1.X1", net="RUN")
    wire = s.sheet().wires[0]
    pts = iter_wire_points(s.sheet(), s.catalog, wire)
    assert len(pts) >= 2
    assert len(pts) <= 8
    box = component_obstacle(s.sheet(), s.catalog, blocker)
    assert not path_hits_obstacles(pts, [box])


def test_title_block_is_an_obstacle():
    s = Session.create("Title")
    pw, ph = s.sheet().page.dimensions()
    s.place("iec.operators.pb_no", ref="S1", x=40, y=ph - 80)
    s.place("iec.operators.pilot_light", ref="H1", x=220, y=ph - 80)
    s.wire("S1.14", "H1.X1")
    pts = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[0])
    obstacles = sheet_obstacles(s.sheet(), s.catalog, skip={"S1", "H1"})
    title = obstacles[-1]
    assert title[3] > title[1]
    assert not path_hits_obstacles(pts, [title])


def test_wire_does_not_loop_around_its_own_symbol():
    s = Session.create("Self")
    s.place("iec.relays.k_4co_24vdc", ref="K1", x=40, y=40)
    s.place("iec.drives.vfd_1k5", ref="U1", x=140, y=36)
    s.wire("K1.14", "U1.DI1", net="EN")
    pts = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[0])
    assert len(pts) <= 8
    length = sum(abs(a.x - b.x) + abs(a.y - b.y) for a, b in zip(pts, pts[1:]))
    assert length < 250
    box = component_obstacle(s.sheet(), s.catalog, s.sheet().component("K1"))
    assert not path_hits_obstacles(pts, [box])
    u1 = component_obstacle(s.sheet(), s.catalog, s.sheet().component("U1"))
    assert not path_hits_obstacles(pts, [u1])


def test_stacked_power_pins_stay_local():
    """Fuse under a disconnect must not route over the top of the page."""
    s = Session.create("Power")
    s.place("iec.protection.disconnect_3p", ref="Q1", x=30, y=22)
    s.place("iec.protection.fuse_10a", ref="F1A", x=22, y=80)
    s.place("iec.protection.fuse_10a", ref="F1B", x=40, y=80)
    s.place("iec.protection.fuse_10a", ref="F1C", x=58, y=80)
    s.wire("Q1.2", "F1A.1", net="L1")
    s.wire("Q1.4", "F1B.1", net="L2")
    s.wire("Q1.6", "F1C.1", net="L3")
    for w in s.sheet().wires:
        pts = iter_wire_points(s.sheet(), s.catalog, w)
        ys = [p.y for p in pts]
        assert max(ys) < 90
        assert min(ys) > 50


def test_naive_clear_path_stays_simple():
    s = Session.create("Clear")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=120, y=40)
    s.wire("S1.14", "H1.X1")
    pts = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[0])
    assert 2 <= len(pts) <= 8


def _interior_segs(pts):
    segs = list(zip(pts, pts[1:]))
    if len(segs) >= 3:
        segs = segs[1:-1]
    return {
        (round(a.x, 1), round(a.y, 1), round(b.x, 1), round(b.y, 1))
        for a, b in segs
    }


def test_set_run_channel_pulls_horizontal_keep_pins():
    pts = [
        Point(x=0, y=10),
        Point(x=8, y=10),
        Point(x=80, y=10),
        Point(x=90, y=10),
    ]
    out = set_run_channel(pts, 1, "h", 22)
    assert abs(out[0].y - 10) < 0.2
    assert abs(out[-1].y - 10) < 0.2
    ys = [p.y for p in out[1:-1]]
    assert any(abs(y - 22) < 0.8 for y in ys)


def test_parallel_runs_pulled_apart_even_without_shared_pin():
    """Two side-by-side nets on the same row must not share one horizontal."""

    s = Session.create("Pull")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=200, y=40)
    s.place("iec.operators.pb_no", ref="S2", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H2", x=200, y=40)
    s.wire("S1.14", "H1.X1", net="A")
    s.wire("S2.14", "H2.X1", net="B")
    p1 = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[0])
    p2 = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[1])
    assert coincident_run_length(p1, p2) < 8.0, (p1, p2, coincident_run_length(p1, p2))


def test_wires_may_cross():
    s = Session.create("Cross")
    s.place("iec.operators.pb_no", ref="S1", x=30, y=80)
    s.place("iec.operators.pilot_light", ref="H1", x=200, y=80)
    s.place("iec.operators.pb_no", ref="S2", x=110, y=20)
    s.place("iec.operators.pilot_light", ref="H2", x=110, y=150)
    s.wire("S1.14", "H1.X1", net="H")
    s.wire("S2.14", "H2.X1", net="V")
    p1 = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[0])
    p2 = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[1])
    assert coincident_run_length(p1, p2) < 8.0
    assert len(p1) <= 10
    assert len(p2) <= 10


def _retraces(pts) -> list[tuple[str, float]]:
    hits = []
    for index, (start, end) in enumerate(zip(pts, pts[1:])):
        for left, right in list(zip(pts, pts[1:]))[index + 1 :]:
            if abs(start.y - end.y) <= 0.4 and abs(left.y - right.y) <= 0.4 and abs(start.y - left.y) <= 0.4:
                a0, a1 = sorted((start.x, end.x))
                b0, b1 = sorted((left.x, right.x))
                share = min(a1, b1) - max(a0, b0)
                if share > 0.8:
                    hits.append(("H", share))
            if abs(start.x - end.x) <= 0.4 and abs(left.x - right.x) <= 0.4 and abs(start.x - left.x) <= 0.4:
                a0, a1 = sorted((start.y, end.y))
                b0, b1 = sorted((left.y, right.y))
                share = min(a1, b1) - max(a0, b0)
                if share > 0.8:
                    hits.append(("V", share))
    return hits


def test_short_drop_does_not_stroke_the_lead_twice():
    """A bottom pin a few millimetres from the next top pin leaves once.

    A 6 mm stub used to run past the other pin and come back, so the lead was drawn twice.
    """

    s = Session.create("Short drop")
    s.place("iec.protection.disconnect_3p", ref="Q1", x=40, y=20, snap=False)
    s.place("iec.protection.fuse_10a", ref="F1A", x=29, y=76, rotation=90, snap=False)
    s.place("iec.protection.fuse_10a", ref="F1B", x=66, y=76, rotation=90, snap=False)
    s.place("iec.protection.fuse_10a", ref="F1C", x=110, y=76, rotation=90, snap=False)
    s.wire("Q1.2", "F1A.1", number="L1")
    s.wire("Q1.4", "F1B.1", number="L2")
    s.wire("Q1.6", "F1C.1", number="L3")
    for wire in s.sheet().wires:
        pts = iter_wire_points(s.sheet(), s.catalog, wire)
        assert _retraces(pts) == [], (wire.frm, wire.to, pts)
        assert abs(pts[0].y - pts[1].y) > 0.5 or abs(pts[0].x - pts[1].x) > 0.5


def test_return_keeps_its_own_lane_beside_phase_leads():
    """Motor leads stay 4 mm apart. A return and an enable do not sit in that gap."""

    s = Session.create("Phase lanes")
    s.place("iec.drives.vfd_1k5", ref="U1", x=52, y=156, snap=False)
    s.place("iec.loads.motor_3ph", ref="M1", x=150, y=178, snap=False)
    s.place("iec.relays.k_4co_24vdc", ref="K1", x=150, y=108, snap=False)
    s.place("iec.power.psu_24v_5a", ref="G1", x=248, y=138, snap=False)
    s.wire("U1.U", "M1.U1", number="201")
    s.wire("U1.V", "M1.V1", number="202")
    s.wire("U1.W", "M1.W1", number="203")
    s.wire("K1.14", "U1.DI1", number="204")
    s.wire("U1.COM", "G1.minus", number="0V")
    sheet = s.sheet()
    routes = {
        wire.number: iter_wire_points(sheet, s.catalog, wire) for wire in sheet.wires
    }
    for number, pts in routes.items():
        assert _retraces(pts) == [], (number, pts)
    crowded = _parallel_closer_than(routes, gap=2.0, share=8.0)
    assert crowded == [], crowded


def _parallel_closer_than(routes: dict, *, gap: float, share: float) -> list[str]:
    segs = []
    for number, pts in routes.items():
        for start, end in zip(pts, pts[1:]):
            segs.append((str(number), start.x, start.y, end.x, end.y))
    hits = []
    for index, (num_a, ax, ay, bx, by) in enumerate(segs):
        for num_b, cx, cy, dx, dy in segs[index + 1 :]:
            if num_a == num_b:
                continue
            if abs(ay - by) <= 0.4 and abs(cy - dy) <= 0.4 and abs(ay - cy) < gap:
                a0, a1 = sorted((ax, bx))
                b0, b1 = sorted((cx, dx))
                span = min(a1, b1) - max(a0, b0)
                if span > share:
                    hits.append(f"H {num_a}/{num_b} dy={abs(ay - cy):.2f} share={span:.1f}")
            if abs(ax - bx) <= 0.4 and abs(cx - dx) <= 0.4 and abs(ax - cx) < gap:
                a0, a1 = sorted((ay, by))
                b0, b1 = sorted((cy, dy))
                span = min(a1, b1) - max(a0, b0)
                if span > share:
                    hits.append(f"V {num_a}/{num_b} dx={abs(ax - cx):.2f} share={span:.1f}")
    return hits


def test_near_parallel_wires_do_not_stack():
    s = Session.create("Chan")
    s.place("iec.operators.pb_no", ref="START", x=40, y=80)
    s.place("iec.protection.fuse_10a", ref="FU1", x=40, y=20)
    s.place("iec.contactors.coil", ref="K1", x=180, y=70)
    s.wire("FU1.2", "K1.A1", net="L1")
    s.wire("START.14", "K1.A1", net="L1")
    p1 = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[0])
    p2 = iter_wire_points(s.sheet(), s.catalog, s.sheet().wires[1])
    shared = _interior_segs(p1) & _interior_segs(p2)
    assert not shared, shared


def test_two_wires_to_same_pin_use_different_channels():
    s = Session.create("Overlap")
    s.place("iec.operators.pb_nc", ref="STOP", x=40, y=80)
    s.place("iec.operators.pb_no", ref="START", x=110, y=80)
    s.place("iec.contactors.coil", ref="K1", x=180, y=70)
    s.place("iec.contactors.aux_no", ref="K13", x=110, y=110)
    s.place("iec.protection.fuse_10a", ref="FU1", x=40, y=20)
    s.wire("FU1.2", "STOP.11", net="L1")
    s.wire("STOP.12", "START.13", net="L1")
    s.wire("START.14", "K1.A1", net="L1")
    s.wire("K13.13", "START.13", net="L1")
    s.cleanup()
    sheet = s.sheet()
    hold = next(w for w in sheet.wires if w.frm.startswith("K13.") or w.to.startswith("K13."))
    series = next(w for w in sheet.wires if "START.14" in (w.frm, w.to) and "K1.A1" in (w.frm, w.to))
    hold_pts = iter_wire_points(sheet, s.catalog, hold)
    series_pts = iter_wire_points(sheet, s.catalog, series)
    shared = _interior_segs(hold_pts) & _interior_segs(series_pts)
    assert not shared, shared


def test_a_nudge_does_not_land_on_another_wire_number():
    """Pulling 105 off its neighbour must not drop it onto 104.

    Half a millimetre of shared channel is enough to count. The old check
    ignored anything shorter than 8 mm, so 7.3 mm on x=47 was accepted.
    """

    from panelcad.core.route import shared_lane_length
    from panelcad.examples.field_cables import build_plc_hoa_analog_sample
    from panelcad.render.wiring import route_sheet_wires

    session = build_plc_hoa_analog_sample()
    session.cleanup("main")
    sheet = session.project.sheet("main")
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    wire_104 = next(wire for wire in sheet.wires if wire.number == "104")
    wire_105 = next(wire for wire in sheet.wires if wire.number == "105")
    shared = shared_lane_length(routes[wire_105.id], [routes[wire_104.id]], min_len=0.5)
    assert shared <= 0.5, (shared, routes[wire_105.id])


def test_moving_off_a_lane_does_not_cut_the_start_contact():
    """A detour that would bridge S1's NO contact is refused. The wire goes around."""

    from pathlib import Path

    from panelcad.render.wiring import route_sheet_wires

    session = Session.open(Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml")
    session.cleanup("control")
    sheet = session.project.sheet("control")
    wire = next(item for item in sheet.wires if item.net == "START")
    pts = route_sheet_wires(sheet, session.catalog, project=session.project)[wire.id]
    box = component_obstacle(sheet, session.catalog, sheet.component("S1"))
    assert not path_hits_obstacles(pts, [box], skip_ends=True), pts


def test_routing_time_stays_near_the_plain_router():
    """Lane repair is a detour for the wires that overlap, not a second search of the sheet."""

    import statistics
    import time

    from panelcad.examples.field_cables import build_reversing_motor_sample
    from panelcad.render.wiring import clear_route_cache, route_sheet_wires

    reversing = build_reversing_motor_sample()

    def median(route) -> float:
        samples = []
        for _ in range(5):
            clear_route_cache()
            started = time.perf_counter()
            route()
            samples.append(time.perf_counter() - started)
        return statistics.median(samples)

    reversing_s = median(
        lambda: [
            route_sheet_wires(sheet, reversing.catalog, project=reversing.project)
            for sheet in reversing.project.sheets
            if sheet.wires
        ]
    )
    # A full extra grid search was about 0.69 s on this sheet.
    assert reversing_s < 0.45, reversing_s
