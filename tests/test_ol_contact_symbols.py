"""Overload auxiliary contacts are drawn clean.

The thermal element on a stand-alone 95-96 contact is smooth arcs at the
power-heater size, and the 95-96 / 97-98 contacts built into an overload or a
motor protector are a closed and an open contact, not loose slanted strokes.
"""

from __future__ import annotations

from panelcad.library.catalog import default_catalog

CONTACTS = ("nema.protection.ol_contact", "iec.protection.ol_contact")
BUILT_IN = ("iec.protection.overload_9a", "iec.breakers.mpcb_3p")


def test_ol_contact_thermal_element_is_arcs_at_heater_size():
    cat = default_catalog()
    for lid in CONTACTS:
        device = cat.get(lid)
        arcs = [p for p in device.primitives if p.type == "arc"]
        assert len(arcs) == 3, lid
        assert all(abs(p.r - 1.5) < 1e-9 and abs(abs(p.sweep) - 180) < 1e-9 for p in arcs), lid
        assert not [p for p in device.primitives if p.type == "polyline"], lid
        # Centred over the contact, between its posts at x=8 and x=20.
        xs = [p.x for p in arcs]
        assert abs((min(xs) + max(xs)) / 2 - 14.0) < 1e-9


def _segments(device, lo: float, hi: float):
    return [p for p in device.primitives if p.type == "line" and min(p.x, p.x2) >= 29 and lo <= min(p.y, p.y2) and max(p.y, p.y2) <= hi]


def test_built_in_aux_contacts_join_their_pins():
    cat = default_catalog()
    for lid in BUILT_IN:
        device = cat.get(lid)
        pins = {p.id: p for p in device.pins}
        for a, b in (("95", "96"), ("97", "98")):
            ya, yb = pins[a].y, pins[b].y
            segs = _segments(device, ya, yb)
            ends = {(round(p.x, 2), round(p.y, 2)) for p in segs} | {(round(p.x2, 2), round(p.y2, 2)) for p in segs}
            assert (pins[a].x, ya) in ends and (pins[b].x, yb) in ends, (lid, a, b)
            # Every stroke is vertical, horizontal, or one blade: no stair steps.
            slanted = [p for p in segs if p.x != p.x2 and p.y != p.y2]
            assert len(slanted) == 1, (lid, a, b)
