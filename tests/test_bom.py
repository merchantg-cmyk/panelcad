from pathlib import Path

from panelcad import Session
from panelcad.core.electrical import is_drawing_only_mark
from panelcad.core.models import Device, PanelFootprint
from panelcad.examples.dol import build_dol
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.quote.build import build_quote
from panelcad.quote.labor import drawing_counts


def test_bom_roundtrip_updates_manufacturer(tmp_path: Path):
    s = build_dol()
    path = tmp_path / "bom.csv"
    s.export_bom(path)
    text = path.read_text(encoding="utf-8-sig")
    assert "ref" in text
    assert "Q1" in text
    assert "manufacturer" in text
    mini = tmp_path / "mini.csv"
    mini.write_text(
        "ref,library_id,manufacturer,part_number,catalog,notes\n"
        "Q1,iec.breakers.mcb_3p_32a,Schneider,iC60N 3P 32A,A9F44332,feed\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(mini)
    assert "Q1" in result["updated"]
    q = s.sheet().component("Q1")
    assert q.properties["manufacturer"] == "Schneider"
    assert q.properties["part_number"] == "iC60N 3P 32A"
    assert q.properties["catalog"] == "A9F44332"


def test_bom_import_places_missing(tmp_path: Path):
    s = Session.create("From BOM")
    csv_path = tmp_path / "in.csv"
    csv_path.write_text(
        "qty,library_id,manufacturer,part_number\n"
        "2,iec.terminals.x_2p5,Wago,2002-1201\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(csv_path)
    assert len(result["created"]) == 2
    assert len(s.sheet().components) == 2
    assert s.sheet().components[0].properties["manufacturer"] == "Wago"


def test_grouped_bom_sums_qty():
    s = build_dol()
    s.place("iec.terminals.x_2p5", ref="X1", x=50, y=40)
    s.place("iec.terminals.x_2p5", ref="X2", x=60, y=40)
    rows = grouped_rows(s.project, s.catalog)
    term = next(r for r in rows if r["library_id"] == "iec.terminals.x_2p5")
    assert term["qty"] == "2"
    assert "X1" in term["ref"] and "X2" in term["ref"]


def test_bom_includes_dimensions_and_din():
    from panelcad.io.bom import instance_rows

    s = build_dol()
    rows = instance_rows(s.project, s.catalog)
    q1 = next(r for r in rows if r["ref"] == "Q1" and "power" in r["sheet"].split(","))
    # DOL Q1 is an assigned catalog breaker (FAZ-C6-3-NA-L), so the BOM shows that size.
    assert q1["width_mm"]
    assert q1["din_rail"] == "yes"
    assert q1["incabinet"] == "yes"
    assert q1["in_panel"] == "yes"
    m1 = next(r for r in rows if r["ref"] == "M1")
    assert m1["incabinet"] == "no"
    assert m1["in_panel"] == "no"
    assert m1["din_rail"] == "no"
    assert m1["mount"] == "none"
    a3 = next(r for r in rows if r["ref"] == "A3" and "io" in r["sheet"].split(","))
    assert a3["width_mm"] == "27"
    assert a3["height_mm"] == "90.3"
    assert a3["dim_source"] == "catalog"
    u1 = next(r for r in rows if r["ref"] == "U1" and "io" in r["sheet"].split(","))
    assert u1["width_mm"] == "68"
    assert u1["din_rail"] == "no"
    assert u1["mount"] == "backplate"


def _ink(
    library_id: str,
    name: str,
    *,
    location: str = "schematic-only",
    category: str = "annotation.mark",
) -> Device:
    """A symbol whose footprint is the generic table fill. The BOM must ignore that size."""

    return Device(
        library_id=library_id,
        id=library_id.rsplit(".", 1)[-1],
        name=name,
        category=category,
        properties={"location": location},
        panel=PanelFootprint(
            width_mm=18.0,
            height_mm=80.0,
            depth_mm=70.0,
            din_rail=True,
            in_panel=True,
            mount="din_rail",
            dim_source="generic",
        ),
    )


def test_drawing_marks_stay_off_the_bom_and_the_quote():
    """Off-page arrows and other non-purchasable ink are not line items.

    The rule is the symbol, not a job name. A field device and a symbol-only
    terminal stay, and neither row is given a guessed millimetre.
    """

    session = Session.create("Drawing marks")
    power = session.sheet().id
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=30, y=30)
    session.place("iec.loads.motor_3ph", ref="M1", x=80, y=30)
    session.place("iec.loads.solenoid", ref="Y1", x=120, y=30)
    session.place("nema.terminals.feed_through", ref="TB1", x=160, y=30)
    heater = _ink("shop.loads.heater", "Heater", location="field", category="load.heater")
    session.catalog.devices[heater.library_id] = heater
    session.place(heater.library_id, ref="HTR1", x=80, y=150)
    q1 = session.sheet().component("Q1")
    q1.properties["location"] = "MCC-1 / Pump 1"
    before = build_quote(session.project, session.catalog, session.parts)
    before_hours = before["totals"]["labor_hours"]

    session.place("iec.connection.offpage", ref="OP1", x=30, y=70, properties={"bom": "yes"})
    session.place("iec.connection.offpage_from", ref="OP2", x=70, y=70)
    session.place("iec.connection.offpage_ref", ref="OP3", x=110, y=70)
    session.place("iec.connection.node", ref="N1", x=30, y=90)
    session.place("iec.connection.junction", ref="J1", x=60, y=90)
    session.place("iec.connection.waypoint", ref="W1", x=90, y=90)
    session.place("iec.notes.block", ref="NTE1", x=20, y=120)
    session.place("iec.notes.spare", ref="SPR1", x=80, y=120)
    session.place("iec.layout.photo", ref="PIC1", x=140, y=120)
    session.place("iec.connection.ground", ref="PE1", x=180, y=90)
    session.place("iec.terminals.bus_v", ref="BUS1", x=200, y=40)
    session.add_sheet("control", kind="schematic")
    session.place("iec.connection.offpage", ref="OP9", x=40, y=40, sheet_id="control")

    ink = _ink("shop.ink.callout", "Sheet callout")
    session.catalog.devices[ink.library_id] = ink
    session.place(ink.library_id, ref="CALL1", x=30, y=150, sheet_id=power)
    page = session.project.sheet(power)
    assert is_drawing_only_mark(ink, page.component("CALL1"))
    assert not is_drawing_only_mark(heater, page.component("HTR1"))

    flat = instance_rows(session.project, session.catalog)
    grouped = grouped_rows(session.project, session.catalog)
    refs = {row["ref"] for row in flat}
    assert refs == {"Q1", "M1", "Y1", "TB1", "HTR1"}
    grouped_refs = {
        part.strip() for row in grouped for part in row["ref"].split(",") if part.strip()
    }
    assert grouped_refs == refs
    drawn = {
        "OP1",
        "OP2",
        "OP3",
        "OP9",
        "N1",
        "J1",
        "W1",
        "NTE1",
        "SPR1",
        "PIC1",
        "PE1",
        "BUS1",
        "CALL1",
    }
    assert not refs & drawn
    assert all("offpage" not in row["library_id"] for row in flat)
    drawing_categories = {
        "connection.offpage",
        "connection.node",
        "connection.notes",
        "connection.ground",
    }
    assert all(row["category"] not in drawing_categories for row in flat)
    heater_row = next(row for row in flat if row["ref"] == "HTR1")
    terminal = next(row for row in flat if row["ref"] == "TB1")
    assert heater_row["width_mm"] == ""
    assert heater_row["height_mm"] == ""
    assert heater_row["depth_mm"] == ""
    assert terminal["width_mm"] == ""
    assert terminal["height_mm"] == ""
    assert terminal["depth_mm"] == ""
    assert "18" not in {heater_row["width_mm"], terminal["width_mm"]}

    quote = build_quote(session.project, session.catalog, session.parts)
    quoted = {
        part.strip()
        for row in quote["parts"]
        for part in str(row["refs"]).split(",")
        if part.strip()
    }
    assert quoted == refs
    assert quote["lines"] == before["lines"]
    assert quote["unpriced"] == before["unpriced"]
    assert quote["totals"]["parts_sell"] == before["totals"]["parts_sell"]
    assert quote["totals"]["total"] == before["totals"]["total"]
    assert quote["totals"]["labor_hours"] == before_hours
    assert any("drawing marks" in text for text in quote["assumptions"])
    counts = drawing_counts(session.project, session.catalog)
    counted = {ref for refs in counts["devices"].values() for ref in refs}
    counted.update(counts["door_refs"])
    counted.update(counts["terminal_refs"])
    assert not counted & drawn
    assert drawn <= set(counts["skipped_other"])


def test_wire_offpage_arrows_are_not_parts():
    session = Session.create("Off page")
    power = session.sheet().id
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=40, y=40)
    session.add_sheet("control", kind="schematic")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="control")
    linked = session.wire_offpage("Q1.2", "K1.A1", from_sheet=power, to_sheet="control", net="101")
    rows = instance_rows(session.project, session.catalog)
    refs = {row["ref"] for row in rows}
    assert refs == {"Q1", "K1"}
    assert linked["from_op"] not in refs
    assert linked["to_op"] not in refs
    quote_refs = {
        part.strip()
        for row in build_quote(session.project, session.catalog, session.parts)["parts"]
        for part in str(row["refs"]).split(",")
        if part.strip()
    }
    assert quote_refs == {"Q1", "K1"}


def test_excel_aliases_and_no_place(tmp_path: Path):
    s = build_dol()
    csv_path = tmp_path / "excel.csv"
    csv_path.write_text(
        "Tag,Manufacturer,Part Number\nK1,ABB,AF09-30-10-13\n",
        encoding="utf-8-sig",
    )
    result = s.import_bom(csv_path, place_missing=False)
    assert "K1" in result["updated"]
    assert s.sheet().component("K1").properties["part_number"] == "AF09-30-10-13"
    assert result["created"] == []
