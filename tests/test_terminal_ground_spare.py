"""One-pin ground/PE terminals: wired pins are not spare, and PE is never spare.

Synthetic library parts only. A terminal with a single pin G (no A or B) must
show its wires and must not be reported as spare in terminal_strip_rows, the
strip layout, the print package terminal table and the vendor strip export.
"""

from __future__ import annotations

from panelcad import Session
from panelcad.core.electrical import is_pe_terminal, terminal_strip_rows
from panelcad.core.models import Device, PinDef
from panelcad.core.terminal_strip import layout_terminal_strip
from panelcad.io.vendor_strip.export import export_rows
from panelcad.render.print_package import _collect_report_rows_and_note


def _one_pin(library_id: str, **props) -> Device:
    return Device(
        library_id=library_id,
        id=library_id.split(".")[-1],
        name="One-pin terminal",
        category="connection.terminal",
        ref_prefix="X",
        schematic_width=16,
        schematic_height=26,
        pins=[PinDef(id="G", x=8, y=0, side="top")],
        properties=dict(props),
    )


def _session() -> Session:
    session = Session.create("Ground terminals")
    for dev in (
        _one_pin("shop.terminals.gx1", type="PE"),  # PE by type, no 'pe' in the id
        _one_pin("shop.terminals.onepin"),  # plain one-pin, not PE
    ):
        session.catalog.devices[dev.library_id] = dev
    session.place("iec.terminals.x_2p5", ref="X9-1", x=40, y=20)
    session.place("shop.terminals.gx1", ref="X9-2", x=40, y=50)  # wired PE
    session.place("shop.terminals.gx1", ref="X9-3", x=40, y=80)  # unwired PE
    session.place("shop.terminals.onepin", ref="X9-4", x=40, y=110)  # wired one-pin
    session.place("shop.terminals.onepin", ref="X9-5", x=40, y=140)  # unwired one-pin
    session.place("iec.terminals.x_2p5", ref="X9-6", x=40, y=170)  # unwired, forced not spare
    session.set_wire_numbering(enabled=False)
    session.wire("X9-1.A", "X9-2.G", net="GND")
    session.wire("X9-4.G", "X9-1.B", net="GND")
    sheet = session.sheet()
    for comp in sheet.components:
        if comp.id == "X9-6":
            comp.properties["spare"] = "no"
    return session


def _by_ref(rows, key="ref"):
    return {row[key]: row for row in rows}


def test_strip_rows_wired_one_pin_and_pe_are_not_spare():
    session = _session()
    rows = _by_ref(terminal_strip_rows(session.project, session.catalog))
    assert rows["X9-2"]["spare"] == ""
    assert "GND" in rows["X9-2"]["internal"] + rows["X9-2"]["external"]
    assert rows["X9-4"]["spare"] == ""
    assert "GND" in rows["X9-4"]["internal"] + rows["X9-4"]["external"]
    assert rows["X9-3"]["spare"] == ""  # PE terminal is never spare by default
    assert rows["X9-5"]["spare"] == "yes"  # unwired ordinary terminal is still spare
    assert rows["X9-6"]["spare"] == ""  # spare: no forces it off


def test_spare_yes_still_forces_spare_on():
    session = _session()
    for comp in session.sheet().components:
        if comp.id == "X9-2":
            comp.properties["spare"] = "yes"
    rows = _by_ref(terminal_strip_rows(session.project, session.catalog))
    assert rows["X9-2"]["spare"] == "yes"


def test_pe_detection_uses_type_not_only_library_id():
    session = _session()
    comps = {c.id: c for c in session.sheet().components}
    cat = session.catalog
    assert is_pe_terminal(comps["X9-2"], cat.devices["shop.terminals.gx1"])
    assert not is_pe_terminal(comps["X9-4"], cat.devices["shop.terminals.onepin"])
    # Substring 'pe' inside a word (e.g. "type") is not PE.
    assert not is_pe_terminal(comps["X9-1"], cat.devices.get(comps["X9-1"].library_id))


def test_layout_vendor_export_and_print_table_agree():
    session = _session()
    layout = layout_terminal_strip(session.project, session.catalog, "X9")
    items = {item["ref"]: item for item in layout["items"] if item["kind"] == "terminal"}
    assert items["X9-2"]["pe"] is True and items["X9-2"]["spare"] is False
    assert "GND" in items["X9-2"]["internal"] + items["X9-2"]["external"]
    assert items["X9-4"]["spare"] is False and items["X9-4"]["pe"] is False
    assert items["X9-6"]["spare"] is False

    vendor = _by_ref(export_rows(session.project, session.catalog, "X9"), key="tag")
    assert vendor["X9-2"]["spare"] == "false"
    assert "GND" in vendor["X9-2"]["internal"] + vendor["X9-2"]["external"]
    assert vendor["X9-4"]["spare"] == "false"
    assert vendor["X9-5"]["spare"] == "true"

    table, _note = _collect_report_rows_and_note(session, "terminals")
    table = _by_ref(table)
    assert table["X9-2"]["spare"] == "" and "GND" in table["X9-2"]["internal"] + table["X9-2"]["external"]
