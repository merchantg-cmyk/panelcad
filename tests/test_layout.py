import pytest
from pathlib import Path

from panelcad import Session
from panelcad.core.layout import (
    RAIL_HEIGHT_MM,
    clip_offset,
    footprint_size,
    pack_din,
    rail_aabb,
    rail_height,
    rects_overlap,
    tray_aabb,
)
from panelcad.core.models import Component
from panelcad.examples.dol import build_dol
from panelcad.io.yaml_format import load_project
from tests.support import confirm_layout_occupants


def _place_on_layout(session: Session, library_id: str, ref: str, x: float, y: float, **kwargs):
    """Place a generic symbol on a layout sheet the way a confirmed envelope would."""

    home = session.active_sheet_id
    schematic = next((sh for sh in session.project.sheets if sh.kind == "schematic"), None)
    if schematic is None:
        schematic = session.add_sheet("power", kind="schematic")
    session.place(library_id, ref=ref, x=x, y=y, sheet_id=schematic.id)
    confirm_layout_occupants(session)
    result = session.place_from_bom(library_id, ref=ref, x=x, y=y, sheet_id=home, **kwargs)
    session.set_active_sheet(home)
    return session.sheet(home).component(ref) if result.get("placed") else session.sheet(home).component(ref)


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_recommend_cabinet_from_incabinet_bom():
    s = build_dol()
    rec = s.recommend_cabinet(apply=True)
    assert rec["incabinet_count"] >= 1
    assert rec["width_mm"] >= 300
    assert rec["height_mm"] >= 300
    assert rec["depth_mm"] >= 150
    assert rec["applied"] is True
    enc = s.project.sheet("enclosure").enclosure
    assert enc is not None
    assert enc.width_mm == rec["width_mm"]
    # main d639fc7 named the box from the cabinet size alone. The name now
    # records that the size came from the in-cabinet BOM.
    assert "BOM items in cabinet" in (enc.name or "")
    from panelcad.io.bom import instance_rows

    rows = instance_rows(s.project, s.catalog)
    m1 = next(r for r in rows if r["ref"] == "M1")
    assert m1["incabinet"] == "no"
    assert "M1" not in rec["incabinet_refs"]


def test_display_size_does_not_change_catalog_bom():
    s = Session.create("Door")
    s.add_sheet("door", kind="door_layout")
    h = s.place("oem.patlite.lr4_302wjnw_ryg", ref="H1", x=40, y=40)
    s.set_panel("H1", width_mm=40, height_mm=219, dim_source="catalog")
    catalog_h = footprint_size(s.catalog.get(h.library_id), h)[1]
    assert catalog_h >= 200
    s.set_display_size("H1", 48, 150)
    from panelcad.library.panel import display_footprint_size, effective_footprint

    drawn_w, drawn_h = display_footprint_size(s.catalog.get(h.library_id), s.sheet().component("H1"))
    assert drawn_h == 150
    assert drawn_w == 48
    pack_h = footprint_size(s.catalog.get(h.library_id), s.sheet().component("H1"))[1]
    assert pack_h >= 200
    fp = effective_footprint(s.catalog.get(h.library_id), s.sheet().component("H1"))
    assert fp.height_mm == 219.0


def test_cleanup_does_not_shove_din_off_rail():
    s = build_dol()
    enc = s.sheet("enclosure")
    before = {c.id: (c.x, c.y) for c in enc.components}
    s.cleanup(sheet_id="enclosure")
    from panelcad.library.panel import effective_footprint

    for c in enc.components:
        device = s.catalog.get(c.library_id)
        if not effective_footprint(device, c).din_rail:
            continue
        assert (c.x, c.y) == before[c.id], f"cleanup moved DIN {c.id}"
        clip_y = c.y + clip_offset(device, c)
        own = min(enc.rails, key=lambda r: abs(r.y - clip_y))
        assert abs(own.y - clip_y) < 1.0, f"{c.id} clip {clip_y:.1f} not on a rail (nearest {own.id} {own.y:.1f})"


def _separation(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    dx = max(0.0, max(bx - (ax + aw), ax - (bx + bw)))
    dy = max(0.0, max(by - (ay + ah), ay - (by + bh)))
    if dx == 0.0 and dy == 0.0:
        return 0.0
    if dx == 0.0:
        return dy
    if dy == 0.0:
        return dx
    return (dx * dx + dy * dy) ** 0.5


@pytest.mark.confirm_generic_layout
def test_dol_backplate_devices_clear_wire_duct():
    """Q1, K1, F1, and U1 sit on rails between ducts, with real clearance."""

    from panelcad.core.layout import TRAY_TO_RAIL_MM, TRAY_TO_RAIL_X_MM

    s = build_dol()
    enc = s.sheet("enclosure")
    assert enc.enclosure is not None
    scale = float(enc.enclosure.draw_scale or 1.0)
    ducts = {tray.id: tray_aabb(tray) for tray in enc.trays}
    assert "wt-top" in ducts and "wt-left" in ducts
    want = {"Q1", "K1", "F1", "U1"}
    seen = set()
    for comp in enc.components:
        if comp.id not in want:
            continue
        seen.add(comp.id)
        device = s.catalog.get(comp.library_id)
        box = (comp.x, comp.y, *footprint_size(device, comp))
        for name in ("wt-top", "wt-left"):
            if comp.id == "U1" and name == "wt-top":
                continue
            gap = _separation(box, ducts[name]) / scale
            assert gap + 1e-6 >= min(TRAY_TO_RAIL_MM, TRAY_TO_RAIL_X_MM) - 1.0, (
                f"{comp.id} is {gap:.1f} mm from {name}"
            )
    assert seen == want
    from panelcad.core.layout import nearest_rail
    from panelcad.library.panel import effective_footprint

    for comp in enc.components:
        device = s.catalog.get(comp.library_id)
        fp = effective_footprint(device, comp)
        width, _height = footprint_size(device, comp)
        if comp.id == "U1":
            assert fp.din_rail is False
            assert fp.mount == "backplate"
            assert comp.scale >= scale * 0.95
            continue
        if not fp.din_rail:
            continue
        rail = nearest_rail(enc, comp.y + clip_offset(device, comp))
        assert rail is not None, comp.id
        assert comp.x + 0.6 >= rail.x, comp.id
        assert comp.x + width <= rail.x + rail.length_mm + 0.6, (
            f"{comp.id} ends at {comp.x + width:.1f}, {rail.id} ends at {rail.x + rail.length_mm:.1f}"
        )


def test_pack_clears_trays_and_next_rail():
    s = build_dol()
    enc = s.sheet("enclosure")
    assert enc.enclosure is not None
    assert enc.rails
    sc = enc.enclosure.draw_scale or 1.0
    assert abs(rail_height(enc.rails[0]) - RAIL_HEIGHT_MM * sc) < 0.05
    trays = [tray_aabb(t) for t in enc.trays]
    for c in enc.components:
        device = s.catalog.get(c.library_id)
        w, h = footprint_size(device, c)
        for i, (tx, ty, tw, th) in enumerate(trays):
            assert not rects_overlap(c.x, c.y, w, h, tx, ty, tw, th), (
                f"{c.id} sits on tray {enc.trays[i].id}"
            )
        clip_y = c.y + clip_offset(device, c)
        own = min(enc.rails, key=lambda r: abs(r.y - clip_y))
        for rail in enc.rails:
            if rail.id == own.id:
                continue
            rx, ry, rw, rh = rail_aabb(rail)
            assert not rects_overlap(c.x, c.y, w, h, rx, ry, rw, rh), f"{c.id} hangs onto {rail.id}"
    refs = {c.id for c in enc.components}
    if "U1" in refs:
        u1 = enc.component("U1")
        left = next(t for t in enc.trays if t.id == "wt-left")
        lx, ly, lw, lh = tray_aabb(left)
        uw, uh = footprint_size(s.catalog.get(u1.library_id), u1)
        assert not rects_overlap(u1.x, u1.y, uw, uh, lx, ly, lw, lh)


@pytest.mark.confirm_generic_layout
def test_cut_sheet_pictures_skip_scans(tmp_path: Path):
    s = Session.create("Pics")
    s.add_sheet("panel", kind="panel_layout")
    _place_on_layout(s, "iec.breakers.mcb_3p_32a", "Q1", 40, 40)
    s.add_sheet("door", kind="door_layout")
    s.place("oem.patlite.lr4_302wjnw_ryg", ref="HDoor", x=40, y=40)
    s.save(tmp_path / "job.pcad.yaml")
    (tmp_path / "mcb_3p_32a.png").write_bytes(_PNG)
    (tmp_path / "LR4-302WJNW-RYG.png").write_bytes(_PNG)
    (tmp_path / "PIC1_pic.png").write_bytes(_PNG)
    (tmp_path / "HDoor_pic.png").write_bytes(_PNG)
    result = s.apply_cut_sheet_pictures(tmp_path)
    assert "Q1" in result["refs"]
    assert "HDoor" in result["refs"]
    assert s.sheet("panel").component("Q1").picture.endswith("_pic.png")
    assert s.sheet("door").component("HDoor").picture.endswith("_pic.png")
    assert result["skipped"] >= 2


@pytest.mark.confirm_generic_layout
def test_picture_is_layout_only(tmp_path: Path):
    img = tmp_path / "dev.png"
    img.write_bytes(_PNG)
    s = Session.create("Pic")
    s.add_sheet("panel", kind="panel_layout")
    _place_on_layout(s, "iec.breakers.mcb_3p_32a", "Q1", 40, 40)
    dest = s.save(tmp_path / "job.pcad.yaml")
    s.set_picture("Q1", img)
    assert s.sheet().component("Q1").picture.endswith("_pic.png")
    s.save(dest)
    again = Session.open(dest)
    assert again.sheet("panel").component("Q1").picture
    again.clear_picture("Q1", sheet_id="panel")
    assert again.sheet("panel").component("Q1").picture == ""


def test_panel_sheet_gets_rails_and_enclosure():
    s = Session.create("Box")
    sheet = s.add_sheet("panel", title="Enclosure", kind="panel_layout")
    assert sheet.kind == "panel_layout"
    assert sheet.enclosure is not None
    assert len(sheet.rails) >= 1
    assert sheet.page.orientation == "portrait"


@pytest.mark.confirm_generic_layout
def test_place_snaps_din_device_to_rail():
    s = Session.create("Snap")
    s.add_sheet("panel", kind="panel_layout")
    rail = s.sheet().rails[0]
    q = _place_on_layout(s, "iec.breakers.mcb_3p_32a", "Q1", rail.x + 12.4, rail.y + 40)
    device = s.catalog.get("iec.breakers.mcb_3p_32a")
    assert q.y == rail.y - clip_offset(device)
    assert q.x >= rail.x


def test_layout_from_schematic_uses_a_standard_scale_and_duct_to_duct_rails():
    from panelcad.core.layout import STANDARD_DRAW_DENOMINATORS, _opening_between_side_ducts, format_draw_ratio
    from panelcad.render.titleblock import sheet_scale_label

    session = build_dol()
    sheet = session.sheet("enclosure")
    enc = sheet.enclosure
    assert enc is not None
    assert any(abs(enc.draw_scale * denom - 1.0) <= 1e-3 for denom in STANDARD_DRAW_DENOMINATORS)
    assert "1:2.3" not in format_draw_ratio(enc.draw_scale)
    opening = _opening_between_side_ducts(sheet.trays, enc)
    assert opening is not None
    ox, length = opening
    assert sheet.rails
    for rail in sheet.rails:
        assert abs(rail.x - ox) <= 0.8
        assert abs(rail.length_mm - length) <= 0.8
    assert sheet_scale_label(sheet, session.project.title_block) == format_draw_ratio(enc.draw_scale)


def test_captions_and_legends_stay_clear():
    from panelcad.core.models import Component
    from panelcad.render.layout import (
        _legend_lines,
        caption_baseline,
        caption_width,
        fit_footprint_caption,
        operator_tag_origin,
        should_draw_din_clip,
    )

    lines, size = fit_footprint_caption("CLICK C2-03CPU power supply catalog", 14.0, 10.0)
    assert lines
    for line in lines:
        assert caption_width(line, size) <= 14.05
    device = build_dol().catalog.get("iec.protection.overload_9a")
    plug = Component(
        id="F1",
        library_id=device.library_id,
        x=0.0,
        y=0.0,
        properties={"role": "overload", "mounting": "plug_on"},
    )
    assert should_draw_din_clip(device, plug, 45.0, 70.0) is False
    din = Component(id="K1", library_id="iec.contactors.ac3_9a", x=0.0, y=0.0)
    contactor = build_dol().catalog.get("iec.contactors.ac3_9a")
    assert should_draw_din_clip(contactor, din, 45.0, 80.0) is True
    clip = 20.0
    baseline = caption_baseline(20.0, 2.4, clip_y=clip, body_top=0.0, body_bot=70.0)
    assert baseline is not None
    assert baseline - 2.4 > clip + 0.4 or baseline < clip - 1.0
    tx, _ty = operator_tag_origin(10.0, 10.0, 22.0, 22.0)
    assert tx >= 10.0 + 22.0
    legend, legend_size = _legend_lines("HAND-OFF-AUTO", 40.0, 10.0)
    for line in legend:
        assert caption_width(line, legend_size) <= 40.0 - 2.4


@pytest.mark.confirm_generic_layout
def test_layout_from_schematic_packs_without_overlap():
    s = build_dol()
    enc = s.sheet("enclosure")
    assert enc.kind == "panel_layout"
    assert enc.rails
    refs = {c.id for c in enc.components}
    assert "Q1" in refs
    assert "K1" in refs
    assert "A2" in refs
    assert "P1" not in refs  # HMI is door-mounted, incabinet=no
    assert "M1" not in refs  # motor is field equipment, not in the enclosure
    assert enc.trays
    assert any(t.orientation == "h" for t in enc.trays)
    assert enc.rails

    boxes = []
    for c in enc.components:
        device = s.catalog.get(c.library_id)
        w, h = footprint_size(device, c)
        boxes.append((c.id, c.x, c.y, w, h))
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            ax, ay, aw, ah = a[1], a[2], a[3], a[4]
            bx, by, bw, bh = b[1], b[2], b[3], b[4]
            overlap = ax < bx + bw - 0.5 and ax + aw > bx + 0.5 and ay < by + bh - 0.5 and ay + ah > by + 0.5
            assert not overlap, (
                f"{a[0]}@{a[1]:.1f},{a[2]:.1f} {aw:.1f}x{ah:.1f} overlaps "
                f"{b[0]}@{b[1]:.1f},{b[2]:.1f} {bw:.1f}x{bh:.1f}"
            )
    assert s.validate() == []


@pytest.mark.confirm_generic_layout
def test_layout_yaml_roundtrip(tmp_path: Path):
    s = build_dol()
    path = tmp_path / "job.pcad.yaml"
    s.save(path)
    raw = path.read_text(encoding="utf-8")
    assert "kind: panel_layout" in raw
    assert "rails:" in raw
    again = Session.open(path)
    enc = again.sheet("enclosure")
    assert enc.enclosure is not None
    assert enc.component("Q1").library_id == "iec.breakers.mcb_3p_32a"
    loaded = load_project(path)
    assert loaded.sheet("enclosure").kind == "panel_layout"


@pytest.mark.confirm_generic_layout
def test_layout_svg_draws_rails_and_footprints(tmp_path: Path):
    s = build_dol()
    dest = tmp_path / "enc.svg"
    s.export_svg(dest, sheet_id="enclosure")
    svg = dest.read_text(encoding="utf-8")
    assert "Q1" in svg
    assert "TS35" in svg
    assert "Enclosure" in svg or "400" in svg
    assert "panel mount" in svg


def test_pack_din_overflows_to_next_rail():
    from panelcad.library.catalog import default_catalog

    cat = default_catalog()
    device = cat.get("iec.breakers.mcb_3p_32a")
    from panelcad.core.models import Rail
    from panelcad.library.panel import apply_confirmed_envelope

    rails = [Rail(id="r1", x=0, y=80, length_mm=60)]  # one 54mm breaker + gap fits; second overflows
    ghosts = []
    for ref in ("Q1", "Q2"):
        ghost = Component(id=ref, library_id=device.library_id, x=0, y=0)
        apply_confirmed_envelope(
            ghost,
            width_mm=54.0,
            height_mm=80.0,
            depth_mm=70.0,
            mount="din_rail",
            confirmed_by="test",
        )
        ghosts.append((ref, device, ghost))
    items = ghosts
    placed, out_rails = pack_din(items, rails)
    assert len(placed) == 2
    assert placed[0][1] < placed[1][1] or abs(placed[0][2] - placed[1][2]) > 1
    assert len(out_rails) >= 2


@pytest.mark.confirm_generic_layout
def test_node_and_disconnect_not_packed():
    s = Session.create("Skip")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1")
    s.place("iec.connection.node", ref="N1")
    s.place("iec.protection.disconnect_3p", ref="Q9")
    s.place("iec.loads.motor_3ph", ref="M1")
    confirm_layout_occupants(s)
    s.add_sheet("enclosure", kind="panel_layout")
    s.set_enclosure(width_mm=600, height_mm=800, depth_mm=250, sheet_id="enclosure")
    s.layout_from_schematic()
    refs = {c.id for c in s.sheet("enclosure").components}
    assert "Q1" in refs
    assert "N1" not in refs
    assert "Q9" not in refs
    assert "M1" not in refs


def test_set_panel_oem_override():
    s = Session.create("OEM")
    s.place("iec.breakers.mcb_1p_16a", ref="Q1")
    result = s.set_panel(ref="Q1", width_mm=17.5, height_mm=89, depth_mm=69, din_rail=True, dim_source="catalog")
    assert result["panel"]["width_mm"] == 17.5
    # Instance dim_source=catalog is not a verified SKU.
    assert result["panel"]["dim_source"] == "measured"
    q1 = s.sheet().component("Q1")
    device = s.catalog.get(q1.library_id)
    from panelcad.library.panel import is_layout_ready
    from panelcad.io.bom import instance_rows

    assert not is_layout_ready(device, q1)
    row = next(r for r in instance_rows(s.project, s.catalog) if r["ref"] == "Q1")
    assert row["width_mm"] == ""
    assert row["dim_source"] == "measured"


def test_scale_roundtrip_and_pin_world(tmp_path: Path):
    from panelcad.render.sheet import pin_world

    s = Session.create("Scale")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.set_scale("S1", 2.0)
    device = s.catalog.get("iec.operators.pb_no")
    pin = device.pin("13")
    ax, ay = pin_world(s.sheet().component("S1"), pin)
    s.set_scale("S1", 1.0)
    bx, by = pin_world(s.sheet().component("S1"), pin)
    assert abs((ax - 40) - 2 * (bx - 40)) < 1e-6
    path = tmp_path / "s.pcad.yaml"
    s.set_scale("S1", 1.5)
    s.save(path)
    again = Session.open(path)
    assert again.sheet().component("S1").visual_scale() == 1.5


def test_gui_click_pin_starts_wire():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import PinItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("Wire")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=80, y=40)
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    pin = next(i for i in canvas.scene.items() if isinstance(i, PinItem) and i.component_id == "S1")
    assert pin.acceptedMouseButtons() == Qt.MouseButton.LeftButton
    scene_pt = pin.scenePos()
    view_pt = canvas.mapFromScene(scene_pt)
    ev = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(view_pt),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.mousePressEvent(ev)
    assert canvas.wire_from == pin.ref()
    win.close()


def test_gui_middle_click_deletes_device():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("MidDel")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "S1")
    center = item.sceneBoundingRect().center()
    vp = QPointF(canvas.mapFromScene(center))

    def ev(typ, button, buttons):
        return QMouseEvent(typ, vp, button, buttons, Qt.KeyboardModifier.NoModifier)

    canvas.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton))
    canvas.mouseReleaseEvent(
        ev(QMouseEvent.Type.MouseButtonRelease, Qt.MouseButton.MiddleButton, Qt.MouseButton.NoButton)
    )
    assert [c.id for c in s.sheet().components] == []
    assert s.undo()
    assert s.sheet().component("S1").id == "S1"
    win.close()


def test_gui_drag_wire_segment():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import WireItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("PullGui")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=180, y=40)
    s.wire("S1.14", "H1.X1")
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    item = next(i for i in canvas.scene.items() if isinstance(i, WireItem))
    mid = item.path().pointAtPercent(0.5)

    def ev(typ, pos, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton):
        vp = canvas.mapFromScene(pos)
        return QMouseEvent(typ, QPointF(vp), button, buttons, Qt.KeyboardModifier.NoModifier)

    canvas.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, mid))
    dragged = QPointF(mid.x(), mid.y() + 18)
    canvas.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, dragged))
    canvas.mouseReleaseEvent(
        ev(QMouseEvent.Type.MouseButtonRelease, dragged, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    )
    assert s.sheet().wires[0].points
    win.close()


def test_gui_place_click_on_empty_still_places():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("PlaceClick")
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    canvas.set_place_mode("iec.operators.pb_no")
    empty = QPointF(200, 120)

    def ev(typ, pos, buttons):
        vp = QPointF(canvas.mapFromScene(pos))
        return QMouseEvent(typ, vp, Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)

    canvas.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, empty, Qt.MouseButton.LeftButton))
    canvas.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, empty, Qt.MouseButton.NoButton))
    assert len(s.sheet().components) == 1
    assert canvas.mode == "place"
    win.close()


def test_gui_empty_drag_switches_to_select_without_placing():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("PlaceDrag")
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    canvas.set_place_mode("iec.operators.pb_no")
    start = QPointF(200, 120)
    dragged = QPointF(230, 150)

    def ev(typ, pos, buttons):
        vp = QPointF(canvas.mapFromScene(pos))
        return QMouseEvent(typ, vp, Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)

    canvas.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton))
    canvas.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, dragged, Qt.MouseButton.LeftButton))
    canvas.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, dragged, Qt.MouseButton.NoButton))
    assert s.sheet().components == []
    assert canvas.mode == "select"
    assert canvas.place_library_id is None
    win.close()


def test_gui_empty_drag_in_wire_mode_switches_to_select():
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("WireDrag")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    canvas.set_wire_mode()
    start = QPointF(200, 120)
    dragged = QPointF(230, 150)

    def ev(typ, pos, buttons):
        vp = QPointF(canvas.mapFromScene(pos))
        return QMouseEvent(typ, vp, Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)

    canvas.mousePressEvent(ev(QMouseEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton))
    canvas.mouseMoveEvent(ev(QMouseEvent.Type.MouseMove, dragged, Qt.MouseButton.LeftButton))
    canvas.mouseReleaseEvent(ev(QMouseEvent.Type.MouseButtonRelease, dragged, Qt.MouseButton.NoButton))
    assert canvas.mode == "select"
    assert canvas.wire_from is None
    assert [c.id for c in s.sheet().components] == ["S1"]
    win.close()


def test_gui_arrow_key_nudges_selected_once():
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import SNAP, ComponentItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("Nudge")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    item = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "S1")
    item.setSelected(True)

    def key(k):
        return QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier)

    canvas.keyPressEvent(key(Qt.Key.Key_Right))
    assert s.sheet().component("S1").x == 40 + SNAP
    assert s.sheet().component("S1").y == 40
    canvas.keyPressEvent(key(Qt.Key.Key_Down))
    assert s.sheet().component("S1").x == 40 + SNAP
    assert s.sheet().component("S1").y == 40 + SNAP
    canvas.keyPressEvent(key(Qt.Key.Key_Left))
    canvas.keyPressEvent(key(Qt.Key.Key_Up))
    assert s.sheet().component("S1").x == 40
    assert s.sheet().component("S1").y == 40
    win.close()


def test_gui_arrow_nudge_multi_select_is_one_undo():
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import SNAP, ComponentItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("NudgeMany")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pb_nc", ref="S2", x=80, y=40)
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    for item in canvas.scene.items():
        if isinstance(item, ComponentItem):
            item.setSelected(True)
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right, Qt.KeyboardModifier.NoModifier)
    canvas.keyPressEvent(ev)
    assert s.sheet().component("S1").x == 40 + SNAP
    assert s.sheet().component("S2").x == 80 + SNAP
    assert s.undo()
    assert s.sheet().component("S1").x == 40
    assert s.sheet().component("S2").x == 80
    assert s.redo()
    assert s.sheet().component("S1").x == 40 + SNAP
    assert s.sheet().component("S2").x == 80 + SNAP
    win.close()


def _library_child(dock, library_id: str):
    from PySide6.QtCore import Qt

    for i in range(dock.tree.topLevelItemCount()):
        cat = dock.tree.topLevelItem(i)
        for j in range(cat.childCount()):
            child = cat.child(j)
            if child.data(0, Qt.ItemDataRole.UserRole) == library_id:
                return child
    raise AssertionError(f"no library row {library_id}")


def test_gui_library_press_arms_place_mode():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("LibPress")
    win = MainWindow(s)
    win.show()
    lib_id = "iec.operators.pb_no"
    child = _library_child(win.library_dock, lib_id)
    win.library_dock.tree.itemPressed.emit(child, 0)
    assert win.canvas.mode == "place"
    assert win.canvas.place_library_id == lib_id
    win.close()


def test_gui_library_drop_arms_place_mode():
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import LIBRARY_MIME
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("LibDrop")
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    mime = QMimeData()
    mime.setData(LIBRARY_MIME, b"iec.operators.pb_no")
    pos = QPointF(canvas.mapFromScene(QPointF(120, 80)))
    ev = QDropEvent(
        pos,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    canvas.dropEvent(ev)
    assert any(c.library_id == "iec.operators.pb_no" for c in s.sheet().components)
    assert canvas.mode == "place"
    assert canvas.place_library_id == "iec.operators.pb_no"
    win.close()


@pytest.mark.confirm_generic_layout
def test_agent_layout_ops(tmp_path: Path):
    from panelcad.cli.agent import Agent

    src = tmp_path / "job.pcad.yaml"
    build_dol().save(src)
    agent = Agent()
    r = agent.handle({"id": 1, "op": "open", "path": str(src)})
    assert r["ok"]
    r = agent.handle({"id": 2, "op": "set_active_sheet", "sheet_id": "enclosure"})
    assert r["ok"]
    r = agent.handle({"id": 3, "op": "list_rails"})
    assert r["ok"]
    assert len(r["result"]["rails"]) >= 1
    r = agent.handle({"id": 4, "op": "layout_from_schematic", "sheet": "enclosure"})
    assert r["ok"]
    assert r["result"]["din"] >= 5


def test_gui_right_click_menus():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMenu

    from panelcad.gui.canvas import ComponentItem, WireItem
    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    s = Session.create("Menus")
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    s.place("iec.operators.pilot_light", ref="H1", x=90, y=40)
    s.wire("S1.14", "H1.X1", net="L")
    win = MainWindow(s)
    win.show()
    canvas = win.canvas
    app.processEvents()

    device = next(i for i in canvas.scene.items() if isinstance(i, ComponentItem) and i.component_id == "S1")
    view_pt = canvas.mapFromScene(device.sceneBoundingRect().center())
    menu = QMenu()
    canvas.fill_context_menu(menu, view_pt)
    titles = [a.text() for a in menu.actions()]
    assert "Object editor…" in titles
    assert "Set address…" in titles
    assert "Duplicate" in titles
    assert "Rotate 90°" in titles
    assert "Unlock connection points" in titles
    assert "Delete" in titles

    empty = QMenu()
    canvas.fill_context_menu(empty, canvas.mapFromScene(canvas.scene.sceneRect().center()))
    empty_titles = [a.text() for a in empty.actions()]
    # Center of a small project may still hit a device; fall back to a far corner.
    if "Object editor…" in empty_titles:
        empty = QMenu()
        canvas.fill_context_menu(empty, canvas.mapFromScene(canvas.scene.sceneRect().bottomRight()))
        empty_titles = [a.text() for a in empty.actions()]
    assert "New page…" in empty_titles
    assert "Close this page" in empty_titles
    assert "Delete this page…" in empty_titles
    assert "Communications page…" not in empty_titles

    wire = next(i for i in canvas.scene.items() if isinstance(i, WireItem))
    wire_titles: list[str] = []
    for t in (0.25, 0.5, 0.75):
        pt = canvas.mapFromScene(wire.path().pointAtPercent(t))
        if canvas._wire_at(pt) is None:
            continue
        wire_menu = QMenu()
        canvas.fill_context_menu(wire_menu, pt)
        wire_titles = [a.text() for a in wire_menu.actions()]
        break
    assert any(title.startswith("Delete wire") for title in wire_titles)
    assert "Set label…" in wire_titles

    assert win.library_dock.tree.viewport().contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    assert win.props.list.viewport().contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    assert win.bom.table.viewport().contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    assert win.sheet_bar.tabs.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    win.close()


def test_gui_drawing_prefs_inform_title_block():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Session.create("Prefs"))
    win.show()
    app.processEvents()
    win.set_drawing_standard("ANSI")
    win.set_drawing_style("ladder")
    win.set_coil_style("square")
    tb = win.session.project.title_block
    assert tb.standard == "NEMA/ANSI"
    assert tb.drawing_style == "ladder"
    assert tb.coil_style == "square"
    win.set_drawing_style("freeform")
    assert win.session.project.title_block.drawing_style == "freeform"
    labels = [a.text() for a in win.menuBar().actions()]
    assert any("Edit" in t for t in labels)
    win.close()


def test_ladder_symbols_paint_without_error():
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.canvas import ComponentItem

    app = QApplication.instance() or QApplication([])
    s = Session.create("Vis")
    s.set_title_block(drawing_style="ladder", coil_style="square")
    s.place("nema.operators.pb_nc", ref="STOP", x=40, y=40)
    s.place("nema.operators.pb_no", ref="START", x=80, y=40)
    s.place("nema.contactors.coil", ref="M", x=120, y=40)
    s.place("nema.protection.ol_contact", ref="OL", x=160, y=40)
    s.place("nema.contactors.aux_no", ref="M3", x=80, y=80)
    img = QImage(80, 80, QImage.Format.Format_ARGB32)
    for ref in ("STOP", "START", "M", "OL", "M3"):
        comp = s.sheet().component(ref)
        item = ComponentItem(comp, s.catalog.get(comp.library_id))
        img.fill(QColor("#f7f4ea"))
        p = QPainter(img)
        item.paint(p, None, None)
        p.end()
        bottom = [c for c in item.childItems() if getattr(c, "pin_id", "") in ("3", "4") and ref == "M"]
        if ref == "M":
            assert bottom == []


def test_print_style_dialog_has_three_examples():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.style_dialog import PrintStyleDialog, style_pixmap

    app = QApplication.instance() or QApplication([])
    dlg = PrintStyleDialog()
    ids = [c.style_id for c in dlg._cards]
    assert ids == ["schematic", "ladder", "freeform"]
    for sid in ids:
        pix = style_pixmap(sid)
        assert pix.width() >= 200
        assert not pix.isNull()
    dlg._pick(dlg._cards[2])
    assert dlg.choice == "freeform"
    dlg.close()


def test_file_new_prompts_for_print_style(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "panelcad.gui.style_dialog.choose_print_style",
        lambda *a, **k: "ladder",
    )
    win = MainWindow(Session.create("Old"))
    win.new_project()
    assert win.session.project.name == "Untitled Panel"
    assert win.session.project.title_block.drawing_style == "ladder"
    assert win.session.project.title_block.coil_style == "square"
    monkeypatch.setattr(
        "panelcad.gui.style_dialog.choose_print_style",
        lambda *a, **k: "",
    )
    win.new_project()
    assert win.session.project.title_block.drawing_style == "ladder"
    win.close()


def _visible_dock_tab_titles(win) -> list[tuple[str, ...]]:
    from PySide6.QtWidgets import QTabBar

    sheet_tabs = win.sheet_bar.tabs
    found: list[tuple[str, ...]] = []
    for bar in win.findChildren(QTabBar):
        if not bar.isVisible() or bar is sheet_tabs:
            continue
        titles = tuple(bar.tabText(index).replace("&", "") for index in range(bar.count()))
        if len(titles) >= 2:
            found.append(titles)
    return found


def _tab_bar_covers_dock(win, titles: tuple[str, ...]) -> bool:
    from PySide6.QtWidgets import QTabBar

    wanted = set(titles)
    for bar in win.findChildren(QTabBar):
        if not bar.isVisible():
            continue
        got = {bar.tabText(index).replace("&", "") for index in range(bar.count())}
        if got != wanted:
            continue
        origin = bar.mapTo(win, bar.rect().topLeft())
        rect = bar.rect().translated(origin)
        if not win.rect().intersects(rect):
            return True
        for dock in win.findChildren(type(win.lib_wrap)):
            if not dock.isVisible() or dock.isFloating():
                continue
            body_origin = dock.mapTo(win, dock.rect().topLeft())
            body = dock.rect().translated(body_origin)
            hit = rect.intersected(body)
            if not hit.isEmpty() and hit.width() * hit.height() > 40:
                return True
    return False


def test_restore_state_keeps_one_bottom_tab_bar():
    """saveState/restoreState and Reset tool windows leave one bottom bar and one right column."""

    from PySide6.QtWidgets import QApplication, QTabBar

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    first = MainWindow(Session.create("Layout source"))
    first.resize(1495, 860)
    first.show()
    app.processEvents()
    state = first.saveState()
    first.close()
    app.processEvents()

    win = MainWindow(Session.create("Layout restore"))
    assert win.restoreState(state)
    win.resize(1495, 860)
    win.show()
    app.processEvents()
    app.processEvents()

    def _one_of_each(window) -> None:
        titles = _visible_dock_tab_titles(window)
        bottom = [item for item in titles if {"BOM", "Motor sizing"} <= set(item)]
        right = [item for item in titles if {"Inspector", "AI"} <= set(item)]
        assert len(bottom) == 1, titles
        assert len(right) == 1, titles
        assert {_dock_title_key(title) for title in bottom[0]} == {
            "BOM",
            "Problems",
            "Photo",
            "Power",
            "Quote",
            "Motor sizing",
            "Next steps",
            "Troubleshoot",
        }, bottom
        assert set(right[0]) == {"Inspector", "AI"}, right
        assert not _tab_bar_covers_dock(window, bottom[0]), bottom
        assert not _tab_bar_covers_dock(window, right[0]), right
        assert window.sheet_bar.tabs.isVisible()
        quote_tabs = [
            bar
            for bar in window.quote_wrap.findChildren(QTabBar)
            if {bar.tabText(i).replace("&", "") for i in range(bar.count())} >= {"Parts", "Labor rates"}
        ]
        assert quote_tabs
        assert all(not bar.isHidden() for bar in quote_tabs)

    _one_of_each(win)
    win.reset_tool_windows()
    app.processEvents()
    app.processEvents()
    _one_of_each(win)
    win.close()
    app.processEvents()


def _pump_event_loop(milliseconds: int) -> None:
    """Run the Qt event loop for a real interval, then drain what that posted."""

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def _dock_title_key(title: str) -> str:
    if title.startswith("Problems"):
        return "Problems"
    return title


def test_reset_tool_windows_keeps_one_inspector_ai_bar():
    """Reset on a 1680x1050 DOL editor leaves one Inspector|AI bar after the resize settles.

    resizeDocks shows a second right-hand bar on a later event-loop turn. Two
    processEvents() calls return before that show. A 200 ms timer reaches it.
    """

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(build_dol())
    win.resize(1680, 1050)
    win.show()
    _pump_event_loop(200)
    assert win.width() >= 1680
    assert win.height() >= 1050

    def _right_and_bottom():
        titles = _visible_dock_tab_titles(win)
        right = [item for item in titles if {"Inspector", "AI"} <= set(item)]
        bottom = [item for item in titles if {"BOM", "Motor sizing"} <= set(item)]
        return titles, right, bottom

    win.reset_tool_windows()
    _pump_event_loop(200)
    titles, right, bottom = _right_and_bottom()
    assert len(right) == 1, titles
    assert set(right[0]) == {"Inspector", "AI"}, right
    assert len(bottom) == 1, titles
    _pump_event_loop(800)
    titles, right, bottom = _right_and_bottom()
    assert len(right) == 1, titles
    assert set(right[0]) == {"Inspector", "AI"}, right
    assert len(bottom) == 1, titles
    assert {_dock_title_key(title) for title in bottom[0]} == {
        "BOM",
        "Problems",
        "Photo",
        "Power",
        "Quote",
        "Motor sizing",
        "Next steps",
        "Troubleshoot",
    }, bottom
    assert not _tab_bar_covers_dock(win, right[0]), right
    assert win.sheet_bar.tabs.isVisible()

    win.removeDockWidget(win.quote_wrap)
    win.removeDockWidget(win.sizing_wrap)
    win.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, win.quote_wrap)
    win.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, win.sizing_wrap)
    win.tabifyDockWidget(win.quote_wrap, win.sizing_wrap)
    win.quote_wrap.show()
    win.sizing_wrap.show()
    win.quote_wrap.raise_()
    _pump_event_loop(200)
    titles = _visible_dock_tab_titles(win)
    quote = [item for item in titles if set(item) == {"Quote", "Motor sizing"}]
    power = [
        item
        for item in titles
        if {"BOM", "Power"} <= {_dock_title_key(title) for title in item}
        and "Quote" not in item
        and "Motor sizing" not in item
    ]
    assert len(quote) == 1, titles
    assert len(power) == 1, titles
    assert "Problems" in {_dock_title_key(title) for title in power[0]}, power

    before = win.session.active_sheet_id
    current = win.sheet_bar.tabs.currentIndex()
    other = next(index for index in range(win.sheet_bar.tabs.count()) if index != current)
    win.sheet_bar.tabs.setCurrentIndex(other)
    app.processEvents()
    assert win.session.active_sheet_id != before
    win.quote.tabs.setCurrentIndex(1)
    app.processEvents()
    assert win.quote.tabs.tabText(win.quote.tabs.currentIndex()).replace("&", "") == "Labor rates"
    win.close()


def test_gui_view_menu_restores_docks():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Session.create("Docks"))
    win.show()
    app.processEvents()
    win.lib_wrap.hide()
    win.prop_wrap.hide()
    assert not win.lib_wrap.isVisible()
    assert not win.prop_wrap.isVisible()
    win.lib_wrap.toggleViewAction().trigger()
    win.prop_wrap.toggleViewAction().trigger()
    app.processEvents()
    assert win.lib_wrap.isVisible()
    assert win.prop_wrap.isVisible()
    win.lib_wrap.hide()
    win.reset_tool_windows()
    app.processEvents()
    assert win.lib_wrap.isVisible()
    assert win.prop_wrap.isVisible()
    assert win.bom_wrap.isVisible()
    win.close()


def test_gui_close_tab_does_not_delete():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(build_dol())
    win.show()
    app.processEvents()
    before = [s.id for s in win.session.project.sheets]
    assert win.sheet_bar.tabs.count() == len(before)
    win.close_sheet_id("ethernet")
    assert [s.id for s in win.session.project.sheets] == before
    assert win.session.project.sheet("ethernet").closed is True
    assert win.sheet_bar.tabs.count() == len(before) - 1
    win.reopen_sheet_id("ethernet")
    assert win.session.project.sheet("ethernet").closed is False
    assert win.sheet_bar.tabs.count() == len(before)
    win.close()


def test_fit_footprint_caption_wraps_narrow_module():
    from panelcad.render.layout import caption_width, fit_footprint_caption

    lines, size = fit_footprint_caption("CLICK 16-pt 24VDC", 24.6, 12.0)
    assert lines
    assert all(caption_width(line, size) <= 24.6 + 0.05 for line in lines)
    assert size >= 1.2
    narrow, narrow_size = fit_footprint_caption("Thermal overload", 5.0, 8.0)
    # main d639fc7 dropped a caption narrower than 6 mm, so this 5 mm module
    # produced []. The module now keeps an ellipsized line that fits the body.
    assert narrow
    assert all(caption_width(line, narrow_size) <= 5.05 for line in narrow)
