"""A hand-made off-page wire must not move devices that are already drawn."""

from __future__ import annotations

from panelcad import Session
from panelcad.core.ladder import rail_xs


def _page() -> Session:
    s = Session.create("Offpage keep")
    s.set_title_block(drawing_style="ladder")
    s.add_sheet("ctl", "Control")
    s.add_sheet("pwr", "Power")
    _left, right = rail_xs(s.project.sheet("ctl"))
    # A pilot light parked against the right rail, on the rung the arrow wants.
    s.place("nema.operators.pb_no", ref="PB1", x=60, y=60, sheet_id="ctl", snap=False)
    s.place("nema.operators.pilot_light", ref="LT1", x=right - 24, y=60, sheet_id="ctl", snap=False)
    s.place("nema.operators.pb_no", ref="PB2", x=right - 60, y=60, sheet_id="ctl", snap=False)
    s.place("nema.relays.cr", ref="CR1", x=120, y=80, sheet_id="pwr", snap=False)
    return s


def _positions(s: Session) -> dict[tuple[str, str], tuple[float, float]]:
    return {(sh.id, c.id): (c.x, c.y) for sh in s.project.sheets for c in sh.components}


def test_wire_offpage_does_not_shuffle_drawn_devices():
    s = _page()
    before = _positions(s)
    pb = s.project.sheet("ctl").component("PB1")
    pin = s.catalog.get(pb.library_id).pins[-1].id
    out = s.wire_offpage(f"PB1.{pin}", "CR1.A1", from_sheet="ctl", to_sheet="pwr", net="7")
    after = _positions(s)
    assert {k: v for k, v in after.items() if k in before} == before
    arrow = s.project.sheet("ctl").component(out["from_op"])
    assert arrow.properties["role"] == "to"
    assert s.project.sheet("pwr").component(out["to_op"]).properties["role"] == "from"


def test_wire_offpage_arrow_takes_a_clear_slot():
    from panelcad.core.circuit_ladder import _arrow_hits_device
    from panelcad.core.spacing import _body_box, _boxes_hit

    s = _page()
    pb = s.project.sheet("ctl").component("PB1")
    pin = s.catalog.get(pb.library_id).pins[-1].id
    out = s.wire_offpage(f"PB1.{pin}", "CR1.A1", from_sheet="ctl", to_sheet="pwr", net="7")
    sheet = s.project.sheet("ctl")
    arrow = sheet.component(out["from_op"])
    box = _body_box(arrow, s.catalog.get(arrow.library_id))
    assert not _arrow_hits_device(sheet, s.catalog, box, _body_box, _boxes_hit)
