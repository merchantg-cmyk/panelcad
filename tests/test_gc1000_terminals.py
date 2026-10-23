"""The GC-1000 symbol carries the catalog terminals, including test outputs."""

from __future__ import annotations

from panelcad import Session


def test_gc1000_test_outputs_are_wireable():
    s = Session.create("GC")
    s.place("oem.keyence.gc1000", ref="GC1", x=150, y=40, snap=False)
    s.place("nema.operators.estop_2nc", ref="ES1", x=40, y=110, snap=False)
    first = s.catalog.get("nema.operators.estop_2nc").pins[0].id
    for pin in ("To0", "To1", "To2", "To3"):
        w = s.wire(f"GC1.{pin}", f"ES1.{first}")
        assert w.frm == f"GC1.{pin}"
        s.undo()


def test_gc1000_catalog_terminals_and_size():
    gc = Session.create("GC").catalog.get("oem.keyence.gc1000")
    ids = {p.id for p in gc.pins}
    assert {"To0", "To1", "To2", "To3"} <= ids
    assert {"So0", "So1", "So2", "So3", "So4", "So5", "P24", "M"} <= ids
    assert gc.pin("Si4").label == "Si4-Si5"
    ys = {}
    for p in gc.pins:
        assert (p.side, p.y) not in ys, f"{p.id} overlaps {ys.get((p.side, p.y))}"
        ys[(p.side, p.y)] = p.id
    # Keyence GC-1000 spec sheet: 60 x 95 x 90 mm (W x D x H).
    assert (gc.panel.width_mm, gc.panel.height_mm, gc.panel.depth_mm) == (60.0, 90.0, 95.0)
    assert gc.panel.dim_source == "catalog"
