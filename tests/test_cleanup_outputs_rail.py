"""Cleanup seats rung outputs (coils, relays, pilot lights, OL) at the return rail."""

from __future__ import annotations

import pytest

from panelcad.api.session import Session
from panelcad.core.cleanup import OUTPUT_RAIL_GAP, OUTPUT_SERIES_GAP, seat_outputs_at_rail
from panelcad.core.ladder import rail_xs
from panelcad.core.power_layout import _body
from panelcad.core.sheet_quality import sheet_quality

RUNGS = [
    ("nema.operators.pb_no", "nema.contactors.coil", "CR1", None),
    ("nema.contactors.aux_no", "nema.operators.pilot_light", "PL1", None),
    ("nema.operators.pb_no", "nema.relays.cr", "CR2", None),
    ("nema.contactors.aux_no", "nema.contactors.coil", "CR4", "nema.protection.ol_contact"),
]


def _pin(s, comp, side):
    pins = [p.id for p in s.catalog.get(comp.library_id).pins if p.side == side]
    pins = pins or [p.id for p in s.catalog.get(comp.library_id).pins]
    return pins[0] if side == "left" else pins[-1]


def _hand_ladder(style):
    """A quick hand placement: each output dropped right after its contact."""

    s = Session.create("outputs at rail")
    s.project.title_block.drawing_style = style
    sheet = s.project.sheets[0]
    sheet.drawing_style = style
    y = 40.0
    for contact, output, ref, tail in RUNGS:
        a = s.place(contact, None, x=50, y=y, sheet_id=sheet.id)
        b = s.place(output, ref, x=110, y=y, sheet_id=sheet.id)
        s.wire("L1", f"{a.id}.{_pin(s, a, 'left')}", sheet_id=sheet.id)
        s.wire(f"{a.id}.{_pin(s, a, 'right')}", f"{b.id}.{_pin(s, b, 'left')}", sheet_id=sheet.id)
        last = b
        if tail:
            t = s.place(tail, None, x=160, y=y, sheet_id=sheet.id)
            s.wire(f"{b.id}.{_pin(s, b, 'right')}", f"{t.id}.{_pin(s, t, 'left')}", sheet_id=sheet.id)
            last = t
        s.wire(f"{last.id}.{_pin(s, last, 'right')}", "L2", sheet_id=sheet.id)
        y += 30.0
    return s, sheet.id


def _right_gap(s, sheet, ref):
    comp = next(c for c in sheet.components if c.id == ref)
    return rail_xs(sheet)[1] - _body(comp, s.catalog.get(comp.library_id))[2]


@pytest.mark.parametrize("style", ["ladder", ""])
def test_cleanup_puts_last_output_at_return_rail(style):
    s, sid = _hand_ladder(style)
    s.cleanup()
    sheet = s.sheet(sid)
    for ref in ("CR1", "PL1", "CR2", "OL1"):
        assert _right_gap(s, sheet, ref) == pytest.approx(OUTPUT_RAIL_GAP, abs=0.6), ref
    # The coil feeding the overload sits right beside it, not mid-page.
    coil = _body(next(c for c in sheet.components if c.id == "CR4"), s.catalog.get("nema.contactors.coil"))
    ol = _body(next(c for c in sheet.components if c.id == "OL1"), s.catalog.get("nema.protection.ol_contact"))
    assert 0.0 <= ol[0] - coil[2] <= OUTPUT_SERIES_GAP + 0.6
    # Contacts stay at the hot rail.
    assert next(c for c in sheet.components if c.id == "PB1").x < 80


@pytest.mark.parametrize("style", ["ladder", ""])
def test_seating_never_adds_overlaps_or_crossings(style):
    s, sid = _hand_ladder(style)
    s.cleanup()
    sheet = s.sheet(sid)
    before = sheet_quality(sheet, s.catalog, s.project)
    for comp in sheet.components:
        if comp.id in {"CR1", "PL1"}:
            comp.x -= 150.0
    seat_outputs_at_rail(s, sheet)
    after = sheet_quality(sheet, s.catalog, s.project)
    assert after["overlaps"] <= before["overlaps"]
    assert after["through"] <= before["through"]
    assert after["crossings"] <= before["crossings"]


def test_second_cleanup_is_stable():
    s, sid = _hand_ladder("ladder")
    s.cleanup()
    first = [(c.id, round(c.x, 2), round(c.y, 2)) for c in s.sheet(sid).components]
    s.cleanup()
    assert [(c.id, round(c.x, 2), round(c.y, 2)) for c in s.sheet(sid).components] == first


def test_rung_not_ending_at_return_rail_is_left_alone():
    s, sid = _hand_ladder("ladder")
    sheet = s.sheet(sid)
    # Re-land CR1 on a terminal instead of L2: no longer a rail output.
    for wire in sheet.wires:
        if wire.to == "L2" and wire.frm.startswith("CR1."):
            wire.to = "PL1.X1"
    x0 = next(c for c in sheet.components if c.id == "CR1").x
    seat_outputs_at_rail(s, sheet)
    assert next(c for c in sheet.components if c.id == "CR1").x == x0


def test_built_circuit_outputs_unchanged():
    s = Session.create("built")
    s.insert_circuit(kind="dol", hp=5, voltage=480, control="local", pilot_lights=True)
    sheet = s.sheet("motor-control")
    before = {c.id: (c.x, c.y) for c in sheet.components}
    assert seat_outputs_at_rail(s, sheet) == 0
    assert {c.id: (c.x, c.y) for c in sheet.components} == before


@pytest.mark.parametrize("where", ["title_block", "sheet"])
def test_freeform_page_is_left_alone(where):
    from panelcad.core.cleanup import _seats_outputs

    s, sid = _hand_ladder("ladder")
    if where == "title_block":
        s.project.title_block.drawing_style = "freeform"
    s.sheet(sid).drawing_style = "freeform"
    assert not _seats_outputs(s, s.sheet(sid))


def test_iec_page_is_left_alone():
    from panelcad.core.cleanup import _seats_outputs

    s, sid = _hand_ladder("ladder")
    s.sheet(sid).drawing_style = "iec"
    assert not _seats_outputs(s, s.sheet(sid))
