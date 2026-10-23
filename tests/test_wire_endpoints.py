"""Wire ends land on symbol ink, and placed labels stay clear of wires."""

from __future__ import annotations

import re

from panelcad.core.checks import CHECK_IDS, run_checks
from panelcad.core.models import Component, Device, PinDef, Wire
from panelcad.core.pin_ink import pin_gap, pin_lead
from panelcad.core.primitives import Primitive
from panelcad.examples.dol import build_dol
from panelcad.render.label_avoid import covered_labels, resolve_labels
from panelcad.render.painter import draw_device, draw_primitive
from panelcad.render.sheet import pin_world
from panelcad.render.svg import SvgPainter

LINE = re.compile(r'<line x1="([-\d.]+)" y1="([-\d.]+)" x2="([-\d.]+)" y2="([-\d.]+)"')


def _inset_device(lib_id: str = "test.inset", *, pin_x: float = 0.0) -> Device:
    # Body inset 2 mm from the frame the pins sit on, as some generated libraries draw it.
    return Device(
        library_id=lib_id,
        id=lib_id,
        name="Inset box",
        category="control.relay",
        ref_prefix="K",
        schematic_width=20.0,
        schematic_height=20.0,
        pins=[
            PinDef(id="1", x=pin_x, y=10.0, side="left"),
            PinDef(id="2", x=20.0, y=10.0, side="right"),
        ],
        primitives=[Primitive(type="rect", x=2.0, y=2.0, w=16.0, h=16.0)],
    )


def _lines(p: SvgPainter) -> list[tuple[float, ...]]:
    return [tuple(float(v) for v in m.groups()) for m in LINE.finditer("".join(p.parts))]


def test_inset_body_gets_lead_to_pin():
    dev = _inset_device()
    comp = Component(id="K1", library_id=dev.library_id, x=50.0, y=50.0)
    assert pin_lead(dev, comp, dev.pins[0]) == (2.0, 10.0)
    assert pin_gap(dev, comp, dev.pins[0]) == 0.0
    p = SvgPainter()
    draw_device(p, dev, comp, captions=False)
    x, y = pin_world(comp, dev.pins[0])
    assert any(
        {(round(a, 2), round(b, 2)), (round(c, 2), round(d, 2))} == {(round(x, 2), round(y, 2)), (52.0, 60.0)}
        for a, b, c, d in _lines(p)
    )


def test_body_on_frame_gets_no_lead():
    dev = _inset_device()
    dev.primitives = [Primitive(type="rect", x=0.0, y=0.0, w=20.0, h=20.0)]
    comp = Component(id="K1", library_id=dev.library_id, x=0.0, y=0.0)
    assert pin_lead(dev, comp, dev.pins[0]) is None


def test_mirror_flips_primitive_like_pin_world():
    p = SvgPainter()
    draw_primitive(p, Primitive(type="line", x=0, y=0, x2=5, y2=0), 100.0, 100.0, mirror=True)
    (x1, _y1, x2, _y2), = _lines(p)
    assert {x1, x2} == {100.0, 95.0}
    dev = _inset_device()
    comp = Component(id="K1", library_id=dev.library_id, x=100.0, y=100.0, mirror=True)
    assert pin_world(comp, dev.pins[1])[0] < 100.0


def test_rotated_symbol_uses_resolved_tag_seat():
    dev = _inset_device()
    comp = Component(id="K7", library_id=dev.library_id, x=50.0, y=50.0, rotation=90)
    p = SvgPainter()
    draw_device(p, dev, comp, tag_at=(123.0, 77.0, "start", 2.5))
    assert re.search(r'x="123\.0+" y="77\.0+"[^>]*>K7<', "".join(p.parts)) or "123.000" in "".join(p.parts)


def _dol_with(dev: Device, *, comp_x: float = 120.0, comp_y: float = 120.0):
    s = build_dol()
    s.catalog.devices[dev.library_id] = dev
    sheet = s.project.sheets[0]
    sheet.components.append(Component(id="KX1", library_id=dev.library_id, x=comp_x, y=comp_y))
    sheet.components.append(Component(id="KX2", library_id=dev.library_id, x=comp_x + 60.0, y=comp_y))
    sheet.wires.append(Wire(id="wx1", **{"from": "KX1.2", "to": "KX2.1"}))
    return s, sheet


def test_off_pin_check_flags_unreachable_pin_only():
    s, _sheet = _dol_with(_inset_device())
    assert "wire_end_off_pin" in CHECK_IDS and "label_covered_by_wire" in CHECK_IDS
    assert not [f for f in run_checks(s.project, s.catalog) if f.check == "wire_end_off_pin"]
    # Pin floats 15 mm left of the body: too far for a lead, so it is flagged.
    s2, _ = _dol_with(_inset_device("test.float", pin_x=-15.0))
    hits = [f for f in run_checks(s2.project, s2.catalog) if f.check == "wire_end_off_pin"]
    assert [f.target for f in hits] == ["KX2.1"]


def test_tag_and_mark_keep_off_foreign_wire():
    s, sheet = _dol_with(_inset_device())
    # A conductor straight down through where KX1's tag would sit by default.
    sheet.components.append(Component(id="KX3", library_id="test.inset", x=100.0, y=60.0))
    sheet.components.append(Component(id="KX4", library_id="test.inset", x=100.0, y=200.0))
    sheet.wires.append(Wire(id="wx2", **{"from": "KX3.2", "to": "KX4.2"}, points=[{"x": 121.0, "y": 70.0}, {"x": 121.0, "y": 210.0}]))
    s.catalog.devices["test.inset"] = _inset_device()
    resolve_labels(s.project, s.catalog, sheet)
    assert not [c for c in covered_labels(s.project, s.catalog, sheet) if c.owner in ("KX1", "wx1")]


def test_label_check_reports_covered_labels(monkeypatch):
    from panelcad.render import label_avoid
    from panelcad.render.label_avoid import CoveredLabel

    s = build_dol()
    sheet_id = s.project.sheets[0].id

    def fake(project, catalog, sheet, **_kw):
        return [CoveredLabel("mark", "w9", "W9", 1.0, 2.0, ("wire w3",))] if sheet.id == sheet_id else []

    monkeypatch.setattr(label_avoid, "covered_labels", fake)
    hits = [f for f in run_checks(s.project, s.catalog, label_cover=True) if f.check == "label_covered_by_wire"]
    assert [(f.sheet, f.ref, f.target) for f in hits] == [(sheet_id, "w9", "mark")]
    assert "wire w3" in hits[0].message


def test_quick_check_only_reads_placed_labels(monkeypatch):
    from panelcad.render import label_avoid

    s = build_dol()
    label_avoid.clear_label_cache() if hasattr(label_avoid, "clear_label_cache") else label_avoid._STORED.clear()
    calls = []
    real = label_avoid.covered_labels

    def spy(*a, **kw):
        calls.append(kw.get("peek"))
        return real(*a, **kw)

    monkeypatch.setattr(label_avoid, "covered_labels", spy)
    run_checks(s.project, s.catalog)
    assert calls and all(calls)
    assert label_avoid.resolve_labels(s.project, s.catalog, s.project.sheets[0], peek=True) is None


def test_io_sheet_seats_clear_of_card_and_stacked_neighbour():
    # Packed I/O page: the module is drawn as a card wider than its symbol, and
    # two field devices stack one rung apart. Wire numbers must not sit on the
    # card border, and the upper device's tag must not sit on the lower one.
    from panelcad import Session
    from panelcad.render.io_ladder import io_card_size
    from panelcad.render.label_avoid import _hand_io_cards
    from panelcad.render.sheet_labels import sheet_symbol_bodies
    from panelcad.render.wire_labels import boxes_overlap, mark_box

    s = Session.create("IO seat")
    s.set_title_block(rail_hot="+24V", rail_return="0V")
    s.add_sheet("out", "Outputs", kind="io")
    s.place("oem.keyence.kv_b16tc", ref="A1", x=88, y=32, sheet_id="out", snap=False)
    s.place("nema.operators.pilot_light", ref="LT1", x=172, y=40, sheet_id="out", snap=False)
    s.place("nema.operators.pilot_light", ref="LT2", x=172, y=54, sheet_id="out", snap=False)
    w1 = s.wire("A1.Q0", "LT1.X1", sheet_id="out", number="Q40.0R")
    w2 = s.wire("A1.Q1", "LT2.X1", sheet_id="out", number="Q40.1R")
    sheet = s.project.sheet("out")
    cards = _hand_io_cards(sheet, s.catalog)
    a1 = next(c for c in sheet.components if c.id == "A1")
    w, h = io_card_size(s.catalog.get(a1.library_id), a1)
    assert cards["A1"] == (88.0, 32.0, 88.0 + w, 32.0 + h)
    res = resolve_labels(s.project, s.catalog, sheet, ladder=False, captions=False)
    by_id = {x.id: x for x in sheet.wires}
    for wire in (w1, w2):
        x, y, align = res.marks[wire.id]
        box = mark_box(x, y, by_id[wire.id].drawn_mark(ladder=False), align=align)
        assert not boxes_overlap(box, cards["A1"]), (wire.id, box)
    bodies = dict(sheet_symbol_bodies(sheet, s.catalog))
    from panelcad.core.ink import text_box

    tx, ty, talign, tsize = res.tags["LT1"]
    tag = text_box(tx, ty, "LT1", tsize, talign)
    assert not boxes_overlap(tag, bodies["body:LT2"]), tag
