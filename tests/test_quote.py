"""Quote estimate: labor from the drawing, user prices only, customer file hides cost."""

from __future__ import annotations

import csv
import json
import os
import zipfile
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from panelcad.api.session import Session
from panelcad.cli.agent import Agent, run_agent
from panelcad.examples.dol import build_dol
from panelcad.library.catalog import default_catalog
from panelcad.library.parts import default_parts_catalog
from panelcad.quote.export import (
    CONTENT_W,
    LABOR_CUSTOMER,
    LABOR_INTERNAL,
    PARTS_CUSTOMER,
    PARTS_INTERNAL,
    section_block_mm,
)
from panelcad.quote.labor import (
    EXAMPLE_LABEL,
    count_phrase,
    device_minutes_for,
    drawing_counts,
    example_labor_table,
    set_cutout_counter,
)
from panelcad.quote.money import cents, hours_from_minutes
from panelcad.quote.xlsx_io import read_xlsx, write_xlsx

ROOT = Path(__file__).resolve().parents[1]


def _assert_item_numbers(xml: str) -> None:
    import re

    found = False
    for row, attrs, value in re.findall(r'<c r="A(\d+)"([^/>]*)>(?:<v>([^<]*)</v>)?</c>', xml):
        if int(row) == 1 or not value:
            continue
        found = True
        assert "inlineStr" not in attrs
        assert value.isdigit()
    assert found


@pytest.fixture(autouse=True)
def _quote_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELCAD_QUOTE_DIR", str(tmp_path / "quote-user"))


def _classified(counts: dict) -> set[str]:
    found: set[str] = set()
    groups = [list(refs) for refs in counts["devices"].values()]
    groups.append(counts["door_refs"])
    groups.append(counts["terminal_refs"])
    groups.append(counts["skipped_child"])
    groups.append(counts["skipped_other"])
    total = 0
    for group in groups:
        total += len(group)
        found.update(group)
    assert total == len(found)
    return found


def _length(project, kind: str) -> float:
    panel: list = []
    other: list = []
    for sheet in project.sheets:
        rows = sheet.rails if kind == "rail" else sheet.trays
        if sheet.kind == "panel_layout":
            panel.extend(rows)
        else:
            other.extend(rows)
    use = panel if panel else other
    return sum(float(row.length_mm) for row in use)


def _assert_counts(session: Session) -> dict:
    from panelcad.core.cables import cable_rows

    counts = drawing_counts(session.project, session.catalog)
    ids = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    assert _classified(counts) == ids
    assert counts["cables"] == len(cable_rows(session.project)) + counts["network_links"]
    assert counts["terminations"] == counts["wires"] * 2
    assert counts["din_rail_mm"] == pytest.approx(_length(session.project, "rail"))
    assert counts["duct_mm"] == pytest.approx(_length(session.project, "tray"))
    assert counts["cutouts"] == 0
    points = {(row["module"], row["address"]) for row in session.io_rows()}
    assert counts["plc_io_points"] == len(points)
    table = example_labor_table()
    quote = session.quote_build(today="2026-10-08")
    assert quote["label"] == EXAMPLE_LABEL
    assert quote["sent"] is False
    by_id = {row["id"]: row for row in quote["labor"]}
    for cat, refs in counts["devices"].items():
        mount, wire, _used = device_minutes_for(table, cat)
        line = by_id[f"device:{cat}"]
        assert line["count"] == len(refs)
        assert line["hours"] == hours_from_minutes(len(refs) * (mount + wire))
        assert "min mount" in line["breakdown"]
        assert "1 devices" not in line["breakdown"]
    assert by_id["terminations"]["count"] == counts["wires"] * 2
    assert by_id["terminations"]["breakdown"].startswith(f"{count_phrase(counts['wires'], 'wire')} × 2 ends")
    assert by_id["cables"]["count"] == counts["cables"]
    assert by_id["cables"]["description"] == "Cables"
    assert "network or serial" in by_id["cables"]["breakdown"]
    assert by_id["cutouts"]["count"] == 0
    assert "not counted yet" in by_id["cutouts"]["breakdown"]
    assert "set_cutout_counter" not in by_id["cutouts"]["breakdown"]
    assert by_id["fixed:engineering"]["hours"] == 4
    assert by_id["plc_io"]["count"] == counts["plc_io_points"]
    return counts


def test_column_widths_fit_the_page():
    for columns in (PARTS_INTERNAL, PARTS_CUSTOMER, LABOR_INTERNAL, LABOR_CUSTOMER):
        assert sum(width for _title, width in columns) == pytest.approx(CONTENT_W)


def test_shipped_libraries_have_no_prices():
    for part in default_parts_catalog().parts.values():
        assert part.unit_cost is None
        assert part.price_date == ""
        assert part.vendor == ""
    for device in default_catalog().devices.values():
        assert "unit_cost" not in device.properties
        assert "price_date" not in device.properties


def test_labor_counts_on_example_projects():
    dol_session = build_dol()
    dol = _assert_counts(dol_session)
    # Ethernet and serial links are cables, not wires and not two terminations each.
    assert dol["network_links"] == 3
    assert dol["cables"] == 3
    assert not dol_session.project.cables
    assert dol["wires"] > 0
    assert dol["terminations"] == dol["wires"] * 2
    assert dol["din_rail_mm"] > 0
    assert dol["duct_mm"] > 0
    assert dol["door_refs"]
    assert dol["plc_io_points"] > 0
    starter = Session.open(ROOT / "examples" / "dol_starter.pcad.yaml")
    saved = _assert_counts(starter)
    assert saved["network_links"] == 3
    assert saved["wires"] == 24
    assert saved["cables"] == 3
    assert saved["terminations"] == 48
    assert not starter.project.cables


def test_cable_labor_follows_the_field_schedule():
    from panelcad.core.cables import cable_rows
    from panelcad.core.models import Cable, CableConductor, Wire

    session = Session.create("Schedule")
    sheet = session.sheet()

    def add(wire_id: str, marker: str = "") -> None:
        sheet.wires.append(
            Wire.model_validate(
                {
                    "id": wire_id,
                    "from": f"TB.{wire_id}",
                    "to": f"FLD.{wire_id}",
                    "cable": marker,
                }
            )
        )

    add("w1")
    add("w2")
    add("w3")
    add("plain")
    session.project.cables.append(
        Cable(
            tag="CW1",
            to_device="M1",
            conductors=3,
            assignments=[
                CableConductor(id="1", sheet=sheet.id, wire_id="w1"),
                CableConductor(id="2", sheet=sheet.id, wire_id="w2"),
                CableConductor(id="3", sheet=sheet.id, wire_id="w3"),
                CableConductor(id="SP", spare=True),
            ],
        )
    )
    counts = drawing_counts(session.project, session.catalog)
    assert len(cable_rows(session.project)) == 1
    assert counts["cables"] == 1
    assert counts["wires"] == 1
    assert counts["terminations"] == 2

    # A cable with no drawn conductors is still one labor unit, not one per conductor.
    session.project.cables.append(Cable(tag="CW2", to_device="M2", conductors=4))
    counts = drawing_counts(session.project, session.catalog)
    assert counts["cables"] == 2
    assert counts["wires"] == 1
    assert counts["terminations"] == 2

    # A wire.cable marker that is not already a cable tag is another schedule row.
    add("m1", "C9")
    add("m2", "C9")
    add("tagged", "CW1")
    counts = drawing_counts(session.project, session.catalog)
    assert len(cable_rows(session.project)) == 3
    assert counts["cables"] == 3
    assert counts["wires"] == 1
    assert counts["terminations"] == 2
    quote = session.quote_build(today="2026-10-08")
    cable_line = next(row for row in quote["labor"] if row["id"] == "cables")
    assert cable_line["count"] == 3
    assert cable_line["description"] == "Cables"
    assert "field cable schedule" in cable_line["breakdown"]
    assert "not counted as wires" in cable_line["breakdown"]
    assert any("field cable schedule" in text for text in quote["assumptions"])

    legacy = Session.create("Legacy")
    page = legacy.sheet()
    for wire_id, marker in (("a", "NET1"), ("b", "NET1"), ("c", "")):
        page.wires.append(
            Wire.model_validate(
                {"id": wire_id, "from": f"TB.{wire_id}", "to": f"FLD.{wire_id}", "cable": marker}
            )
        )
    legacy_counts = drawing_counts(legacy.project, legacy.catalog)
    assert len(cable_rows(legacy.project)) == 1
    assert legacy_counts["cables"] == 1
    assert legacy_counts["wires"] == 1
    assert legacy_counts["terminations"] == 2

    # A network-sheet link is one cable. The same link on the schedule is not a second cable.
    net = Session.create("Network")
    power = net.sheet()
    power.wires.append(
        Wire.model_validate({"id": "p1", "from": "TB.1", "to": "M.1", "net": "RUN"})
    )
    power.wires.append(
        Wire.model_validate({"id": "lanwire", "from": "P1.ETH", "to": "A2.ETH", "net": "LAN"})
    )
    lan = net.add_sheet("lan", "LAN", "network")
    lan.wires.append(Wire.model_validate({"id": "e1", "from": "A.ETH", "to": "B.ETH", "net": "LAN"}))
    lan.wires.append(Wire.model_validate({"id": "e2", "from": "A.COM1", "to": "B.COM1", "net": "MB-1"}))
    net_counts = drawing_counts(net.project, net.catalog)
    assert net_counts["network_links"] == 3
    assert net_counts["cables"] == 3
    assert net_counts["wires"] == 1
    assert net_counts["terminations"] == 2
    net.project.cables.append(
        Cable(
            tag="CW1",
            to_device="B",
            conductors=1,
            assignments=[CableConductor(id="1", sheet=lan.id, wire_id="e1")],
        )
    )
    net_counts = drawing_counts(net.project, net.catalog)
    assert len(cable_rows(net.project)) == 1
    assert net_counts["network_links"] == 2
    assert net_counts["cables"] == 3
    assert net_counts["wires"] == 1
    assert net_counts["terminations"] == 2


def test_child_contact_is_not_a_second_device():
    session = Session.create("Family")
    session.place("iec.contactors.coil", ref="K1", x=40, y=40)
    session.place("iec.contactors.aux_no", ref="K1-13", x=40, y=80)
    session.place("iec.terminals.x_2p5", ref="X1", x=80, y=40)
    counts = drawing_counts(session.project, session.catalog)
    assert counts["skipped_child"] == ["K1-13"]
    assert "K1" in counts["devices"]["switching.contactor"]
    assert counts["terminal_refs"] == ["X1"]
    assert "X1" not in counts["devices"].get("connection.terminal", [])


def test_plc_points_are_not_counted_twice():
    session = Session.create("IO")
    session.place("iec.plc.compact_16dio", ref="A1", x=40, y=40)
    once = drawing_counts(session.project, session.catalog)["plc_io_points"]
    assert once == 8
    session.add_sheet("copy", title="Copy", kind="schematic")
    session.place("iec.plc.compact_16dio", ref="A1", x=40, y=40, sheet_id="copy")
    assert len(session.io_rows()) == once * 2
    assert drawing_counts(session.project, session.catalog)["plc_io_points"] == once


def test_cutout_hook_stays_zero_until_registered():
    session = build_dol()
    assert drawing_counts(session.project, session.catalog)["cutouts"] == 0

    def _four(_project, _catalog):
        return 4

    set_cutout_counter(_four)
    try:
        quote = session.quote_build(today="2026-10-08")
        row = next(item for item in quote["labor"] if item["id"] == "cutouts")
        assert row["count"] == 4
        assert row["hours"] == hours_from_minutes(4 * 8)
    finally:
        set_cutout_counter(None)
    assert drawing_counts(session.project, session.catalog)["cutouts"] == 0


def _price_session() -> tuple[Session, str]:
    session = Session.create("Priced")
    session.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=30)
    session.assign_part("Q1", "1489-M3C160")
    session.place("iec.relays.k_4co_24vdc", ref="K9", x=90, y=30)
    key = next(
        row["key"]
        for row in session.quote_build()["parts"]
        if "1489" in f"{row['catalog']} {row['key']}"
    )
    return session, key


def test_price_list_cheapest_preferred_and_staleness(tmp_path: Path):
    session, key = _price_session()
    csv_path = tmp_path / "prices.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["catalog number", "vendor", "cost", "date", "lead time"],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "catalog number": "1489-M3C160",
                    "vendor": "Alpha",
                    "cost": "40.00",
                    "date": "2020-01-01",
                    "lead time": "2 weeks",
                },
                {
                    "catalog number": "1489-M3C160",
                    "vendor": "Beta",
                    "cost": "55.00",
                    "date": "2026-09-01",
                    "lead time": "5 days",
                },
            ]
        )
    imported = session.quote_import_prices(csv_path)
    assert imported["sent"] is False
    assert imported["rows"] == 2
    assert imported["vendors"] == ["Alpha", "Beta"]
    quote = session.quote_build(today="2026-10-08")
    row = next(item for item in quote["parts"] if item["key"] == key)
    assert row["source"] == "price_list"
    assert row["vendor"] == "Alpha"
    assert row["unit_cost"] == 40.0
    assert row["lead_time"] == "2 weeks"
    assert row["stale"] is True
    assert row["age_days"] == (date(2026, 10, 8) - date(2020, 1, 1)).days
    assert "day" in row["age_label"]
    missing = [item for item in quote["parts"] if item["missing_price"]]
    assert missing
    assert all(item["flag"] == "NO PRICE" for item in missing)
    assert quote["unpriced"] == len(missing)
    assert quote["totals"]["parts_cost"] == pytest.approx(40.0)

    session.quote_set_rates(price_mode="preferred", preferred_vendor="Beta")
    preferred = session.quote_build(today="2026-10-08")
    row = next(item for item in preferred["parts"] if item["key"] == key)
    assert row["vendor"] == "Beta"
    assert row["unit_cost"] == 55.0
    assert row["stale"] is False
    assert row["age_days"] == (date(2026, 10, 8) - date(2026, 9, 1)).days

    session.quote_set_rates(price_mode="preferred", preferred_vendor="Nobody")
    fallback = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert fallback["vendor"] == "Alpha"
    assert "cheapest" in fallback["note"]
    cheapest = next(item for item in quote["parts"] if item["key"] == key)
    assert "older than Beta" in cheapest["note"]
    assert cheapest["stale"] is True


def test_manual_beats_list_and_library_price(tmp_path: Path):
    session, key = _price_session()
    comp = session.project.sheets[0].component("Q1")
    comp.properties["unit_cost"] = 19.5
    comp.properties["price_date"] = "2026-10-01"
    comp.properties["vendor"] = "Shelf"
    library = session.quote_build(today="2026-10-08")
    row = next(item for item in library["parts"] if item["key"] == key)
    assert row["source"] == "library"
    assert row["unit_cost"] == 19.5
    assert row["vendor"] == "Shelf"
    assert row["stale"] is False

    part = session.parts.get("1489-M3C160")
    previous = part.unit_cost
    part.unit_cost = 8.0
    part.price_date = "2026-10-02"
    try:
        comp.properties.pop("unit_cost", None)
        comp.properties.pop("vendor", None)
        comp.properties.pop("price_date", None)
        from_part = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
        assert from_part["source"] == "library"
        assert from_part["unit_cost"] == 8.0
    finally:
        part.unit_cost = previous
        part.price_date = ""

    csv_path = tmp_path / "one.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("catalog,vendor,cost,date\n1489-M3C160,Alpha,40,2026-10-01\n")
    session.quote_import_prices(csv_path)
    listed = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert listed["source"] == "price_list"
    assert listed["unit_cost"] == 40.0

    session.quote_set_rates(
        manual={key: {"unit_cost": 12.25, "vendor": "Typed", "lead_time": "1 day", "price_date": "2026-10-08"}}
    )
    typed = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert typed["source"] == "manual"
    assert typed["unit_cost"] == 12.25
    assert typed["vendor"] == "Typed"
    assert typed["flag"] == ""


def test_xlsx_price_list_imports(tmp_path: Path):
    session, key = _price_session()
    book = tmp_path / "prices.xlsx"
    write_xlsx(
        book,
        {
            "Prices": [
                ["Catalog", "Vendor", "Cost", "Date", "Lead time"],
                ["1489-M3C160", "Gamma", 17.5, "2026-08-01", "3 days"],
            ]
        },
    )
    result = session.quote_import_prices(book)
    assert result["rows"] == 1
    row = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert row["vendor"] == "Gamma"
    assert row["unit_cost"] == 17.5
    assert row["stale"] is False


def test_customer_view_hides_costs(tmp_path: Path):
    session, key = _price_session()
    session.quote_set_rates(
        number="Q-100",
        parts_markup_percent=20,
        manual={key: {"unit_cost": 123.45, "vendor": "Desk", "lead_time": "4 days", "price_date": "2026-10-01"}},
    )
    sell = cents(Decimal("123.45") * Decimal("1.20"))
    assert sell == Decimal("148.14")
    built = session.quote_build(today="2026-10-08")
    priced = next(item for item in built["parts"] if item["key"] == key)
    assert priced["extended_sell"] == Decimal("148.14")
    assert priced["extended_cost"] == Decimal("123.45")
    internal_pdf = tmp_path / "internal.pdf"
    customer_pdf = tmp_path / "customer.pdf"
    customer_csv = tmp_path / "customer.csv"
    internal_xlsx = tmp_path / "internal.xlsx"
    customer_xlsx = tmp_path / "customer.xlsx"
    assert session.quote_export(internal_pdf, view="internal", today="2026-10-08")["sent"] is False
    assert session.quote_export(customer_pdf, view="customer", today="2026-10-08")["sent"] is False
    session.quote_export(customer_csv, view="customer", today="2026-10-08")
    session.quote_export(internal_xlsx, view="internal", today="2026-10-08")
    session.quote_export(customer_xlsx, view="customer", today="2026-10-08")
    customer_json = tmp_path / "customer.json"
    exported_json = session.quote_export(customer_json, fmt="json", view="customer", today="2026-10-08")
    assert exported_json["incomplete"] is True
    assert exported_json["unpriced"] == built["unpriced"]
    assert exported_json["unpriced"] > 0

    from panelcad.render.print_package import extract_pdf_text

    internal_text = extract_pdf_text(internal_pdf).lower()
    customer_text = extract_pdf_text(customer_pdf).lower()
    assert "unit cost" in internal_text
    assert "markup" in internal_text
    assert "123.45" in internal_text
    assert "148.14" in internal_text
    assert "set_cutout_counter" not in internal_text
    assert "not counted yet" in internal_text
    assert EXAMPLE_LABEL in extract_pdf_text(internal_pdf)
    assert "unit cost" not in customer_text
    assert "markup" not in customer_text
    assert "shop rate" not in customer_text
    assert "123.45" not in customer_text
    assert "148.14" in customer_text
    assert "drawing feature is connected" not in customer_text
    assert "set_cutout_counter" not in customer_text
    assert "pending" in customer_text
    assert "not sent" in customer_text
    assert "incomplete" in customer_text
    assert EXAMPLE_LABEL not in extract_pdf_text(customer_pdf)
    assert "leaves out the shop buy-price columns" not in extract_pdf_text(customer_pdf)
    assert "control.plc" not in customer_text
    assert "load.motor" not in customer_text

    header = customer_csv.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "unit_cost" not in header
    assert "markup" not in header
    assert "shop_rate" not in header
    assert "unit_price" in header
    body = customer_csv.read_text(encoding="utf-8-sig")
    assert "123.45" not in body
    assert "148.14" in body
    assert "section" in header
    assert "\ntotal," in "\n" + body
    assert "INCOMPLETE" in body
    assert EXAMPLE_LABEL not in body

    internal_book = read_xlsx(internal_xlsx)
    customer_book = read_xlsx(customer_xlsx)
    assert list(internal_book) == ["Summary", "Parts", "Labor", "Assumptions"]
    assert list(customer_book) == ["Summary", "Parts", "Labor", "Assumptions"]
    assert "Unit cost" in internal_book["Parts"][0]
    assert "How computed" in " ".join(internal_book["Labor"][0])
    assert EXAMPLE_LABEL in " ".join(cell for row in internal_book["Assumptions"] for cell in row)
    customer_parts_header = " ".join(customer_book["Parts"][0])
    assert "Unit cost" not in customer_parts_header
    assert "Markup" not in customer_parts_header
    assert "Unit price" in customer_parts_header
    customer_blob = " ".join(cell for rows in customer_book.values() for row in rows for cell in row)
    assert "123.45" not in customer_blob
    assert "148.14" in customer_blob
    assert EXAMPLE_LABEL not in customer_blob
    assert "leaves out the shop buy-price columns" not in customer_blob
    assert "INCOMPLETE" in customer_blob
    assert "drawing feature is connected" not in customer_blob
    assert "set_cutout_counter" not in customer_blob
    assert "control.plc" not in customer_blob.casefold()
    with zipfile.ZipFile(internal_xlsx) as book:
        labor_xml = book.read("xl/worksheets/sheet3.xml").decode()
        internal_styles = book.read("xl/styles.xml").decode()
    assert 'numFmtId="164"' in internal_styles
    assert 'formatCode="0.000"' in internal_styles
    assert 's="2"' in labor_xml
    internal_notes = " ".join(cell for row in internal_book["Assumptions"] for cell in row)
    assert "set_cutout_counter" not in internal_notes
    assert "not counted yet" in internal_notes
    with zipfile.ZipFile(internal_xlsx) as book:
        _assert_item_numbers(book.read("xl/worksheets/sheet2.xml").decode())
    with zipfile.ZipFile(customer_xlsx) as book:
        _assert_item_numbers(book.read("xl/worksheets/sheet2.xml").decode())
    with zipfile.ZipFile(customer_xlsx) as book:
        styles = book.read("xl/styles.xml").decode()
        summary = book.read("xl/worksheets/sheet1.xml").decode()
    assert 'numFmtId="4"' in styles
    assert 's="1"' in summary
    document = json.loads(customer_json.read_text(encoding="utf-8"))
    keys: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            keys.update(str(key) for key in node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(document)
    for forbidden in ("unit_cost", "extended_cost", "markup_percent", "rate_per_hour", "vendor"):
        assert forbidden not in keys
    raw_json = customer_json.read_text(encoding="utf-8").casefold()
    assert "unit_cost" not in raw_json
    assert "markup" not in raw_json
    assert "shop rate" not in raw_json
    assert EXAMPLE_LABEL not in customer_json.read_text(encoding="utf-8")
    assert "drawing feature is connected" not in document["notes"]
    assert "set_cutout_counter" not in " ".join(document["notes"])


def test_agent_and_cli_quote(tmp_path: Path):
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "Agent quote"})["ok"]
    assert agent.handle({"id": 2, "op": "place", "library_id": "iec.terminals.x_2p5", "ref": "X1", "x": 20, "y": 20})["ok"]
    built = agent.handle({"id": 3, "op": "quote_build", "today": "2026-10-08"})
    assert built["ok"] is True, built
    assert built["result"]["sent"] is False
    assert built["result"]["label"] == EXAMPLE_LABEL
    rates = agent.handle(
        {"id": 4, "op": "quote_set_rates", "number": "Q-7", "parts_markup_percent": 10, "scope": "project"}
    )
    assert rates["ok"] is True, rates
    assert rates["result"]["number"] == "Q-7"
    csv_path = tmp_path / "list.csv"
    csv_path.write_text("sku,vendor,cost,date\nNO-SUCH,Acme,3.50,2026-10-01\n", encoding="utf-8")
    imported = agent.handle({"id": 5, "op": "quote_import_prices", "path": str(csv_path)})
    assert imported["ok"] is True, imported
    assert imported["result"]["rows"] == 1
    dest = tmp_path / "agent.xlsx"
    exported = agent.handle({"id": 6, "op": "quote_export", "path": str(dest), "view": "internal"})
    assert exported["ok"] is True, exported
    assert exported["result"]["sent"] is False
    assert dest.is_file()

    project = tmp_path / "job.pcad.yaml"
    agent.session.save(project)
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    runner = CliRunner()
    built_cli = runner.invoke(app, ["quote", "build", str(project), "--today", "2026-10-08"])
    assert built_cli.exit_code == 0, built_cli.output
    assert "nothing sent" in built_cli.output
    assert EXAMPLE_LABEL in built_cli.output
    pdf = tmp_path / "cli-customer.pdf"
    exported_cli = runner.invoke(
        app,
        ["quote", "export", str(project), "--out", str(pdf), "--view", "customer", "--today", "2026-10-08"],
    )
    assert exported_cli.exit_code == 0, exported_cli.output
    assert "sent=False" in exported_cli.output
    assert "incomplete=True" in exported_cli.output
    assert "warning: INCOMPLETE" in exported_cli.output
    assert "warning: example defaults, edit these" in exported_cli.output
    assert pdf.is_file()


def test_quote_panel_edits_and_renders(tmp_path: Path):
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication, QHeaderView

    from panelcad.gui.main_window import MainWindow
    from panelcad.render.print_package import render_package_page_png

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("DejaVu Sans", 10))
    session = build_dol()
    window = MainWindow(session)
    window.resize(1440, 960)
    window.show()
    app.processEvents()
    window.show_quote()
    app.processEvents()
    assert EXAMPLE_LABEL in window.quote.banner.text()
    assert "does not send" in window.quote.banner.text()
    assert window.quote.parts.rowCount() > 0
    assert "INCOMPLETE" in window.quote.totals.text()
    assert "unpriced, not included" in window.quote.totals.text()
    reports = []
    for action in window.menuBar().actions():
        menu = action.menu()
        if menu and menu.title().replace("&", "") == "Reports":
            reports = [item.text().replace("&", "") for item in menu.actions()]
    assert "Quote" in reports
    assert not window.quote.parts.verticalHeader().isVisible()
    counts = [window.quote.breakdown.item(r, 1).text() for r in range(window.quote.breakdown.rowCount())]
    assert any(text.endswith("devices") for text in counts)
    assert any(text.endswith("wires") for text in counts)
    assert any(text.endswith("cables") for text in counts)
    assert any(text.endswith("ends") for text in counts)
    assert not any(text.endswith(" device") and not text.startswith("1 ") for text in counts)
    labels = [window.quote.labor.item(r, 0).text() for r in range(window.quote.labor.rowCount())]
    assert any(text.startswith("Minutes per cutout") for text in labels)
    assert not any("hooked" in text for text in labels)

    cost_item = window.quote.parts.item(0, 5)
    assert cost_item is not None
    cost_item.setText("15.00")
    app.processEvents()
    extended = window.quote.parts.item(0, 6)
    assert extended is not None
    assert "15.00" in extended.text()
    assert "15.00" in window.quote.totals.text()

    window.quote_wrap.setFloating(True)
    window.quote_wrap.resize(1360, 880)
    window.quote_wrap.show()
    app.processEvents()
    shot_env = os.environ.get("PANELCAD_QUOTE_SCREENSHOTS", "").strip()
    dest = Path(shot_env) if shot_env else tmp_path / "quote-screens"
    dest.mkdir(parents=True, exist_ok=True)
    window.quote.parts.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    window.quote.parts.setRowHeight(0, 400)
    window.quote.resize(1100, 700)
    app.processEvents()
    assert window.quote.parts.rowHeight(0) < 120
    panel = dest / "quote-panel.png"
    assert window.quote_wrap.grab().save(str(panel), "PNG")
    assert panel.stat().st_size > 1000

    internal = tmp_path / "panel-internal.pdf"
    customer = tmp_path / "panel-customer.pdf"
    session.quote_set_rates(parts_markup_percent=20, number="Q-22", customer="Riverside Pump Station")
    key = window.quote._part_keys[0]
    session.quote_set_rates(manual={key: {"unit_cost": 15, "price_date": "2026-10-01", "vendor": "Bench"}})
    session.quote_export(internal, view="internal", today="2026-10-08")
    session.quote_export(customer, view="customer", today="2026-10-08")
    window.quote.refresh()
    app.processEvents()
    assert window.quote_wrap.grab().save(str(panel), "PNG")
    from PySide6.QtPdf import QPdfDocument

    def _pages(path: Path) -> int:
        doc = QPdfDocument()
        err = doc.load(str(path))
        none = getattr(getattr(QPdfDocument, "Error", None), "None_", 0)
        if err != none:
            raise RuntimeError(f"could not load {path}")
        return doc.pageCount()

    for pdf, stem in ((internal, "quote-internal"), (customer, "quote-customer")):
        for index in range(_pages(pdf)):
            name = f"{stem}.png" if index == 0 else f"{stem}-{index + 1}.png"
            render_package_page_png(pdf, index, dest / name, width_px=1700)
    window.close()


def test_extended_sell_uses_extended_cost_and_half_up():
    session = Session.create("Hundred")
    for index in range(100):
        ref = f"Q{index + 1}"
        session.place("iec.breakers.mcb_3p_32a", ref=ref, x=20, y=20)
        session.assign_part(ref, "1489-M3C160")
    key = next(row["key"] for row in session.quote_build()["parts"] if row["qty"] == 100)
    session.quote_set_rates(
        parts_markup_percent=15,
        manual={key: {"unit_cost": "0.10", "price_date": "2026-10-01"}},
    )
    row = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert row["extended_cost"] == Decimal("10.00")
    assert row["extended_sell"] == Decimal("11.50")

    single, one = _price_session()
    single.quote_set_rates(
        parts_markup_percent=15,
        manual={one: {"unit_cost": "626.50", "price_date": "2026-10-01"}},
    )
    priced = next(item for item in single.quote_build(today="2026-10-08")["parts"] if item["key"] == one)
    assert priced["extended_sell"] == Decimal("720.48")
    assert cents(Decimal("626.50") * Decimal("1.15")) == Decimal("720.48")


def test_labor_cost_uses_exact_minutes():
    session, _key = _price_session()
    session.quote_set_rates(labor={"device_minutes": {"protection.breaker": {"mount": 1, "wire": 0}}})
    quote = session.quote_build(today="2026-10-08")
    line = next(row for row in quote["labor"] if row["id"] == "device:protection.breaker")
    assert line["count"] == 1
    assert "1 device" in line["breakdown"]
    assert "1 devices" not in line["breakdown"]
    assert line["hours"] == Decimal("0.017")
    assert line["cost"] == Decimal("1.25")
    assert "0.017 h" in line["breakdown"]
    assert any("3 decimal" in text for text in quote["assumptions"])
    assert any("not used to price" in text for text in quote["assumptions"])
    assert not any("then multiplied by the shop rate" in text for text in quote["assumptions"])

    two_hundred = Session.create("Minutes")
    two_hundred.place("iec.breakers.mcb_3p_32a", ref="Q1", x=20, y=20)
    two_hundred.quote_set_rates(labor={"device_minutes": {"protection.breaker": {"mount": 200, "wire": 0}}})
    priced = two_hundred.quote_build(today="2026-10-08")
    row = next(item for item in priced["labor"] if item["id"] == "device:protection.breaker")
    assert row["minutes"] == 200
    assert row["hours"] == Decimal("3.333")
    assert row["cost"] == Decimal("250.00")
    two_hundred.quote_set_rates(labor={"device_minutes": {"protection.breaker": {"mount": 50, "wire": 0}}})
    row = next(item for item in two_hundred.quote_build()["labor"] if item["id"] == "device:protection.breaker")
    assert row["cost"] == Decimal("62.50")
    two_hundred.quote_set_rates(labor={"device_minutes": {"protection.breaker": {"mount": 128, "wire": 0}}})
    row = next(item for item in two_hundred.quote_build()["labor"] if item["id"] == "device:protection.breaker")
    assert row["cost"] == Decimal("160.00")


def test_price_list_encoding_dates_newest_and_numbers(tmp_path: Path):
    from panelcad.quote.money import parse_number
    from panelcad.quote.prices import parse_price_date, read_price_list

    assert parse_price_date(datetime(2026, 9, 30)) == date(2026, 9, 30)
    assert parse_price_date(46295) == date(2026, 9, 30)
    assert parse_price_date("46295") == date(2026, 9, 30)
    assert parse_number("1.234,50") == Decimal("1234.50")
    assert parse_number("12,50") == Decimal("12.50")
    assert parse_number("12.50 USD") == Decimal("12.50")
    assert parse_number("USD 12.50") == Decimal("12.50")
    assert parse_number("usd 12,50") == Decimal("12.50")
    assert parse_number("1,234.50") == Decimal("1234.50")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_number("1,234")
    with pytest.raises(ValueError, match="ambiguous"):
        parse_number("1.234")
    with pytest.raises(ValueError, match="finite"):
        parse_number("nan")
    with pytest.raises(ValueError, match="finite"):
        parse_number(float("inf"))

    session, key = _price_session()
    encoded = tmp_path / "cp1252.csv"
    encoded.write_bytes(b"catalog,vendor,cost,date\n1489-M3C160,Acme\x96North,10.00,2026-10-01\n")
    imported = session.quote_import_prices(encoded)
    assert imported["rows"] == 1
    assert imported["encoding"] == "cp1252"
    row = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert "\u2013" in row["vendor"]
    assert row["unit_cost"] == 10

    book = tmp_path / "serial.xlsx"
    write_xlsx(
        book,
        {"Prices": [["Catalog", "Vendor", "Cost", "Date"], ["1489-M3C160", "Gamma", 17.5, 46295]]},
    )
    serial = session.quote_import_prices(book)
    assert serial["encoding"] == "xlsx"
    row = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert row["price_date"] == "2026-09-30"
    assert row["vendor"] == "Gamma"

    newest = tmp_path / "newest.csv"
    newest.write_text(
        "catalog,vendor,cost,date\n"
        "1489-M3C160,Alpha,40.00,2020-01-01\n"
        "1489-M3C160,Alpha,55.00,2026-09-01\n",
        encoding="utf-8",
    )
    result = session.quote_import_prices(newest)
    assert result["rows"] == 1
    assert result["encoding"] == "utf-8"
    row = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert row["unit_cost"] == 55
    assert row["stale"] is False

    mixed = tmp_path / "mixed.csv"
    mixed.write_text(
        "Catalog #,Vendor,Unit Price ($),Date\n"
        '1489-M3C160,Alpha,"1.234,50",2026-10-01\n'
        'WIDGET,Beta,"12,50",2026-10-01\n'
        "BAD,Beta,\"1,234\",2026-10-01\n"
        "NAN,Beta,nan,2026-10-01\n"
        "OK,Beta,12.50 USD,2026-10-01\n",
        encoding="utf-8",
    )
    mixed_result = session.quote_import_prices(mixed)
    costs = {item["catalog"]: item["cost"] for item in session.project.properties["quote"]["prices"]}
    assert costs["1489-M3C160"] == 1234.5
    assert costs["WIDGET"] == 12.5
    assert costs["OK"] == 12.5
    assert "BAD" not in costs
    assert "NAN" not in costs
    assert any("row 4" in line and "ambiguous" in line for line in mixed_result["skipped"])
    assert any("row 5" in line and "finite" in line for line in mixed_result["skipped"])

    blanks = tmp_path / "blanks.csv"
    blanks.write_text("catalog,cost\nABC,1.00\n\nDEF,bad\n", encoding="utf-8")
    _rows, skipped, encoding = read_price_list(blanks)
    assert encoding == "utf-8"
    assert any(line.startswith("row 4:") for line in skipped)

    sheet = tmp_path / "blanks.xlsx"
    write_xlsx(sheet, {"Prices": [["Catalog", "Cost"], ["ABC", 1], ["", ""], ["DEF", "nope"]]})
    _rows, skipped, encoding = read_price_list(sheet)
    assert encoding == "xlsx"
    assert any(line.startswith("row 4:") for line in skipped)

    gap = tmp_path / "gap.xlsx"
    write_xlsx(gap, {"Prices": [["Catalog", "Cost"], ["ABC", 1], ["DEF", "nope"]]})
    with zipfile.ZipFile(gap, "r") as book:
        xml = book.read("xl/worksheets/sheet1.xml").decode()
        others = {name: book.read(name) for name in book.namelist() if name != "xl/worksheets/sheet1.xml"}
    xml = xml.replace('<row r="3">', '<row r="6">', 1)
    with zipfile.ZipFile(gap, "w") as book:
        for name, blob in others.items():
            book.writestr(name, blob)
        book.writestr("xl/worksheets/sheet1.xml", xml)
    _rows, skipped, _encoding = read_price_list(gap)
    assert any(line.startswith("row 6:") for line in skipped)
    assert not any(line.startswith("row 4:") for line in skipped)

    missing = tmp_path / "headers.csv"
    missing.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Found headers: Foo, Bar"):
        read_price_list(missing)

    part_header = tmp_path / "part.csv"
    part_header.write_text("Part #,Unit Price ($)\n1489-M3C160,8.00\n", encoding="utf-8")
    rows, skipped, _encoding = read_price_list(part_header)
    assert rows[0]["catalog"] == "1489-M3C160"
    assert rows[0]["cost"] == 8
    assert skipped == []

    aliases = tmp_path / "aliases.csv"
    aliases.write_text("Mfr Part Number,Price Each\n1489-M3C160,9.50\n", encoding="utf-8")
    rows, skipped, encoding = read_price_list(aliases)
    assert encoding == "utf-8"
    assert skipped == []
    assert rows[0]["catalog"] == "1489-M3C160"
    assert rows[0]["cost"] == 9.5


def test_grid_overlay_does_not_freeze_the_list_price(tmp_path: Path):
    session, key = _price_session()
    first = tmp_path / "one.csv"
    first.write_text(
        "catalog,vendor,cost,date,lead time\n1489-M3C160,Alpha,18.00,2026-10-01,6 weeks\n",
        encoding="utf-8",
    )
    session.quote_import_prices(first)
    session.quote_set_rates(manual={key: {"lead_time": "9 weeks"}})
    stored = session.project.properties["quote"]["manual"][key]
    assert "unit_cost" not in stored
    row = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert row["unit_cost"] == 18
    assert row["lead_time"] == "9 weeks"
    assert row["source"] == "price_list"

    second = tmp_path / "two.csv"
    second.write_text(
        "catalog,vendor,cost,date,lead time\n1489-M3C160,Alpha,21.00,2026-10-02,6 weeks\n",
        encoding="utf-8",
    )
    session.quote_import_prices(second)
    row = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert row["unit_cost"] == 21
    assert row["lead_time"] == "9 weeks"

    session.quote_set_rates(manual={key: {"unit_cost": "22"}})
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["unit_cost"] == 22
    assert stored["price_date"] == date.today().isoformat()
    typed = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert typed["source"] == "manual"
    assert typed["unit_cost"] == 22

    session.quote_set_rates(manual={key: {"unit_cost": ""}})
    stored = session.project.properties["quote"]["manual"][key]
    assert "unit_cost" not in stored
    assert "price_date" not in stored
    cleared = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert cleared["source"] == "price_list"
    assert cleared["unit_cost"] == 21
    assert cleared["price_date"] == "2026-10-02"
    assert cleared["lead_time"] == "9 weeks"


def test_example_defaults_are_labelled_only_while_they_remain(tmp_path: Path):
    from panelcad.quote.labor import labor_overrides, resolved_labor, user_labor_path
    from panelcad.render.print_package import extract_pdf_text

    session = Session.create("Defaults")
    session.place("iec.plc.compact_16dio", ref="A1", x=40, y=40)
    quote = session.quote_build(today="2026-10-08")
    wires = next(row for row in quote["labor"] if row["id"] == "wires")
    assert wires["example_default"] is True
    assert "example default" in wires["breakdown"]
    assert quote["label"] == EXAMPLE_LABEL
    session.quote_set_rates(labor={"wire_minutes": 9})
    quote = session.quote_build(today="2026-10-08")
    wires = next(row for row in quote["labor"] if row["id"] == "wires")
    assert wires["example_default"] is False
    assert "example default" not in wires["breakdown"]
    assert quote["example_defaults"] is True
    plc = next(row for row in quote["labor"] if row["id"] == "device:control.plc")
    assert plc["description"] == "Mount and wire PLC"
    assert "control.plc" not in plc["description"]

    from panelcad.quote.export import customer_document

    document = customer_document(quote)
    descriptions = [row["description"] for row in document["labor"]]
    assert "Mount and wire PLC" in descriptions
    assert all("control.plc" not in name and "load.motor" not in name for name in descriptions)
    assert "Cutouts" not in descriptions
    assert "Door devices" not in descriptions
    assert all(row["amount"] != "0.00" for row in document["labor"])

    overrides = labor_overrides(resolved_labor(session.project))
    assert "wire_minutes" in overrides
    assert "device_minutes" not in overrides
    session.quote_set_rates(labor=overrides, scope="user", clear_project_labor=True)
    saved = user_labor_path().read_text(encoding="utf-8")
    assert "wire_minutes: 9" in saved
    assert "device_minutes" not in saved

    customer = tmp_path / "customer.pdf"
    result = session.quote_export(customer, view="customer", today="2026-10-08")
    assert result["example_defaults"] is True
    text = extract_pdf_text(customer)
    assert EXAMPLE_LABEL not in text
    assert "leaves out the shop buy-price columns" not in text

    table = example_labor_table()
    for key in (
        "wire_minutes",
        "termination_minutes",
        "cable_minutes",
        "terminal_block_minutes",
        "duct_minutes_per_m",
        "din_minutes_per_m",
        "door_mount_minutes",
        "door_wire_minutes",
        "cutout_minutes",
        "plc_io_minutes",
    ):
        table[key] = float(table[key]) + 1
    table["stale_after_days"] = 400
    for row in table["device_minutes"].values():
        row["mount"] = float(row["mount"]) + 1
        row["wire"] = float(row["wire"]) + 1
    for row in table["fixed"].values():
        row["hours"] = float(row["hours"]) + 1
    for row in table["shop_rates"]:
        row["per_hour"] = float(row["per_hour"]) + 1
    session.quote_set_rates(labor=table, scope="project")
    tuned = session.quote_build(today="2026-10-08")
    assert tuned["example_defaults"] is False
    assert tuned["label"] == ""
    assert all("example default" not in row["breakdown"] for row in tuned["labor"])


def test_header_undo_category_markup_and_nonfinite_numbers():
    session = Session.create("Header")
    session.quote_set_rates(number="Q-1", project="West pump")
    assert session.project.properties["quote"]["project"] == "West pump"
    depth = len(session._undo)
    session.quote_set_rates(number="Q-1", project="West pump")
    assert len(session._undo) == depth
    session.quote_set_rates(parts_markup_percent=20, category_markups={"protection.breaker": 40})
    quote = session.quote_build()
    assert quote["parts_markup_label"] == "Parts markup (by category)"
    with pytest.raises(ValueError, match="finite"):
        session.quote_set_rates(parts_markup_percent=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        session.quote_set_rates(freight=float("inf"))
    with pytest.raises(ValueError, match="finite"):
        session.quote_set_rates(tax_percent="NaN")
    assert session.project.properties["quote"]["parts_markup_percent"] == 20

    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "JSONL"})["ok"]
    rated = agent.handle({"id": 2, "op": "quote_set_rates", "project": "West pump", "number": "Q-3"})
    assert rated["ok"] is True, rated
    assert rated["result"]["project"] == "West pump"
    assert rated["result"]["number"] == "Q-3"

    fresh = Session.create("Pump job")
    fresh.project.title_block.customer = "Acme"
    depth = len(fresh._undo)
    fresh.quote_set_rates(
        number="",
        customer="Acme",
        project="Pump job",
        currency="",
        price_mode="cheapest",
        parts_markup_percent=0,
        labor_markup_percent=0,
        tax_percent=0,
        freight=0,
        contingency_percent=0,
    )
    assert not fresh.project.properties.get("quote")
    assert len(fresh._undo) == depth
    fresh.project.title_block.customer = "Later"
    assert fresh.quote_build()["customer"] == "Later"


def test_quote_cli_import_and_set(tmp_path: Path):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session, key = _price_session()
    project = tmp_path / "job.pcad.yaml"
    session.save(project)
    prices = tmp_path / "prices.csv"
    prices.write_text(
        "catalog,vendor,cost,date\n1489-M3C160,Alpha,660,2026-10-01\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    imported = runner.invoke(app, ["quote", "import-prices", str(project), str(prices)])
    assert imported.exit_code == 0, imported.output
    assert "imported 1" in imported.output
    assert "encoding utf-8" in imported.output
    assert "sent=False" in imported.output
    set_cmd = runner.invoke(
        app,
        [
            "quote",
            "set",
            str(project),
            "--number",
            "Q-9",
            "--project-name",
            "West pump",
            "--parts-markup",
            "0",
            "--category",
            "protection.breaker=10",
        ],
    )
    assert set_cmd.exit_code == 0, set_cmd.output
    assert "sent=False" in set_cmd.output
    opened = Session.open(project)
    blob = opened.project.properties["quote"]
    assert blob["number"] == "Q-9"
    assert blob["project"] == "West pump"
    assert blob["category_markups"]["protection.breaker"] == 10
    csv_path = tmp_path / "internal.csv"
    exported = runner.invoke(
        app,
        ["quote", "export", str(project), "--out", str(csv_path), "--view", "internal", "--today", "2026-10-08"],
    )
    assert exported.exit_code == 0, exported.output
    body = csv_path.read_text(encoding="utf-8-sig")
    assert "660.00" in body
    assert "660.0\n" not in body and ",660.0," not in body
    rejected = runner.invoke(app, ["quote", "set", str(project), "--freight", "nan"])
    assert rejected.exit_code != 0
    _ = key


def test_pdf_age_column_and_labor_heading_stays_with_its_rows(tmp_path: Path):
    from panelcad.quote.export import _pdf_part_cells
    from panelcad.render.print_package import extract_pdf_text

    cells = _pdf_part_cells(
        {"currency": ""},
        {
            "item": "1",
            "qty": 1,
            "catalog": "1489",
            "part_number": "",
            "description": "Breaker",
            "refs": "Q1",
            "vendor": "Alpha",
            "unit_cost": Decimal("40"),
            "extended_cost": Decimal("40"),
            "lead_time": "6 weeks",
            "missing_price": False,
            "stale": True,
            "flag": "STALE",
            "age_label": "2458 days",
        },
        False,
    )
    assert cells[-2] == "6 weeks"
    assert cells[-1] == "2458 days STALE"
    assert "weeks" not in cells[-1]

    session = Session.create("Pages")
    for index in range(40):
        ref = f"Q{index + 1}"
        session.place("iec.breakers.mcb_3p_32a", ref=ref, x=20, y=20)
        session.project.sheets[0].component(ref).properties["catalog"] = f"CAT-{index}"
    pdf = tmp_path / "long.pdf"
    session.quote_export(pdf, view="internal", today="2026-10-08")
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    doc.load(str(pdf))
    pages = [extract_pdf_text(pdf, index) for index in range(doc.pageCount())]
    labor_pages = [text for text in pages if "How computed" in text]
    assert labor_pages
    assert "Labor" in labor_pages[0]
    assert "Engineering" in labor_pages[0] or "Wires" in labor_pages[0]
    assert any("Age" in text for text in pages)


def test_quote_panel_header_edits_and_customer_prompt(tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

    from panelcad.gui.main_window import MainWindow
    from panelcad.quote.labor import user_labor_path

    notes: dict = {"warning": [], "question": [], "save": 0, "answer": QMessageBox.StandardButton.No}

    def _warning(*args, **kwargs):
        notes["warning"].append(args[2] if len(args) > 2 else "")
        return QMessageBox.StandardButton.Ok

    def _question(*args, **kwargs):
        notes["question"].append(args[2] if len(args) > 2 else "")
        return notes["answer"]

    def _info(*args, **kwargs):
        return QMessageBox.StandardButton.Ok

    def _save(*args, **kwargs):
        notes["save"] += 1
        return str(tmp_path / "customer.pdf"), "pdf"

    monkeypatch.setattr(QMessageBox, "warning", _warning)
    monkeypatch.setattr(QMessageBox, "question", _question)
    monkeypatch.setattr(QMessageBox, "information", _info)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", _save)

    app = QApplication.instance() or QApplication([])
    session, key = _price_session()
    prices = tmp_path / "list.csv"
    prices.write_text(
        "catalog,vendor,cost,date,lead time\n1489-M3C160,Alpha,18.00,2026-10-01,6 weeks\n",
        encoding="utf-8",
    )
    session.quote_import_prices(prices)
    window = MainWindow(session)
    window.resize(1280, 860)
    window.show()
    app.processEvents()
    window.show_quote()
    app.processEvents()
    dock = window.quote

    def row_of(line_key: str) -> int:
        return dock._part_keys.index(line_key)

    row = row_of(key)
    dock.parts.item(row, 8).setText("9 weeks")
    app.processEvents()
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["lead_time"] == "9 weeks"
    assert "unit_cost" not in stored
    row = row_of(key)
    assert "18.00" in dock.parts.item(row, 5).text()
    assert dock.parts.item(row, 5).textAlignment() & Qt.AlignmentFlag.AlignRight
    assert not (dock.parts.item(row, 7).flags() & Qt.ItemFlag.ItemIsEditable)

    dock.parts.item(row, 5).setText("22.00")
    app.processEvents()
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["unit_cost"] == 22
    assert stored["price_date"] == date.today().isoformat()
    dock.parts.item(row_of(key), 5).setText("")
    app.processEvents()
    stored = session.project.properties["quote"]["manual"][key]
    assert "unit_cost" not in stored
    assert "price_date" not in stored
    assert "18.00" in dock.parts.item(row_of(key), 5).text()
    assert dock.parts.item(row_of(key), 8).text() == "9 weeks"
    assert dock.parts.item(row_of(key), 9).text() == "2026-10-01"

    def edit(widget, text: str) -> None:
        widget.setText(text)
        widget.editingFinished.emit()
        app.processEvents()

    edit(dock.number, "Q-44")
    edit(dock.customer, "Riverside")
    edit(dock.project_name, "West pump")
    edit(dock.currency, "USD")
    edit(dock.preferred, "Beta")
    edit(dock.parts_markup, "15")
    edit(dock.labor_markup, "10")
    edit(dock.tax, "5")
    edit(dock.freight, "12.50")
    edit(dock.contingency, "2")
    dock.mode.setCurrentIndex(dock.mode.findData("preferred"))
    app.processEvents()
    blob = session.project.properties["quote"]
    assert blob["number"] == "Q-44"
    assert blob["customer"] == "Riverside"
    assert blob["project"] == "West pump"
    assert blob["currency"] == "USD"
    assert blob["preferred_vendor"] == "Beta"
    assert blob["parts_markup_percent"] == 15
    assert blob["labor_markup_percent"] == 10
    assert blob["tax_percent"] == 5
    assert blob["freight"] == 12.5
    assert blob["contingency_percent"] == 2
    assert blob["price_mode"] == "preferred"
    quote = session.quote_build(today="2026-10-08")
    priced = next(item for item in quote["parts"] if item["key"] == key)
    assert priced["extended_sell"] == Decimal("20.70")
    assert quote["totals"]["freight"] == Decimal("12.50")
    assert "20.70" in dock.totals.text()
    assert "INCOMPLETE" in dock.totals.text()
    from panelcad.theme import ui_hex

    assert ui_hex("banner_error_bg").lower() in dock.totals.styleSheet().lower()

    depth = len(session._undo)
    dock.number.editingFinished.emit()
    app.processEvents()
    assert len(session._undo) == depth

    first = quote["parts"][0]["category"]
    other = next(item["category"] for item in quote["parts"] if item["category"] != first)
    for index in range(dock.markups.rowCount()):
        if dock.markups.item(index, 0).text() == first:
            dock.markups.item(index, 1).setText("50")
            break
    app.processEvents()
    quote = session.quote_build(today="2026-10-08")
    by_cat = {item["category"]: item for item in quote["parts"]}
    assert by_cat[first]["markup_percent"] == 50
    assert by_cat[other]["markup_percent"] == 15
    assert quote["parts_markup_label"] == "Parts markup (by category)"
    assert "Parts markup (by category)" in dock.totals.text()
    assert "Parts markup (15%)" not in dock.totals.text()

    for index in range(dock.labor.rowCount()):
        if dock.labor.item(index, 0).text().startswith("Minutes per wire"):
            dock.labor.item(index, 1).setText("9")
            break
    app.processEvents()
    dock._save_defaults()
    saved = user_labor_path().read_text(encoding="utf-8")
    assert "wire_minutes: 9" in saved
    assert "device_minutes" not in saved

    dock._export("pdf", "customer")
    assert notes["save"] == 0
    assert any("INCOMPLETE" in text for text in notes["question"])
    assert any(EXAMPLE_LABEL in text for text in notes["warning"])
    notes["answer"] = QMessageBox.StandardButton.Yes
    dock._export("xlsx", "customer")
    dock._export("csv", "customer")
    assert notes["save"] == 2
    assert (tmp_path / "customer.pdf").is_file() or (tmp_path / "customer.xlsx").is_file()

    edit(dock.parts_markup, "nan")
    assert any("finite" in str(text).lower() for text in notes["warning"])
    assert session.project.properties["quote"]["parts_markup_percent"] == 15
    window.close()


def test_clearing_a_typed_cost_restores_a_stale_list_date(tmp_path: Path):
    session = Session.create("Stale drive")
    session.place("adc.durapulse.gs21_20p5", ref="U1", x=40, y=30)
    key = next(row["key"] for row in session.quote_build()["parts"] if "GS21" in row["catalog"])
    prices = tmp_path / "gs21.csv"
    prices.write_text(
        "catalog,vendor,cost,date\nGS21-20P5,AD,450.00,2020-01-15\n",
        encoding="utf-8",
    )
    session.quote_import_prices(prices)
    listed = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert listed["unit_cost"] == 450
    assert listed["price_date"] == "2020-01-15"
    assert listed["stale"] is True
    assert listed["flag"] == "STALE"

    session.quote_set_rates(manual={key: {"unit_cost": "500"}})
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["price_date_defaulted"] is True
    assert stored["price_date"] == date.today().isoformat()
    typed = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert typed["source"] == "manual"
    assert typed["stale"] is False

    session.quote_set_rates(manual={key: {"unit_cost": ""}})
    manual = session.project.properties["quote"].get("manual") or {}
    assert key not in manual
    cleared = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert cleared["source"] == "price_list"
    assert cleared["unit_cost"] == 450
    assert cleared["price_date"] == "2020-01-15"
    assert cleared["flag"] == "STALE"
    assert "NO PRICE" not in cleared["flag"]

    session.quote_set_rates(manual={key: {"unit_cost": "455", "price_date": "2026-09-30"}})
    session.quote_set_rates(manual={key: {"unit_cost": ""}})
    manual = session.project.properties["quote"].get("manual") or {}
    assert key not in manual
    cleared = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert cleared["unit_cost"] == 450
    assert cleared["price_date"] == "2020-01-15"
    assert cleared["flag"] == "STALE"
    assert cleared["age_label"] != "8 days"

    session.quote_set_rates(manual={key: {"price_date": "2026-09-30"}})
    session.quote_set_rates(manual={key: {"unit_cost": "455"}})
    session.quote_set_rates(manual={key: {"unit_cost": ""}})
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["price_date"] == "2026-09-30"
    assert "price_date_with_cost" not in stored
    kept = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert kept["unit_cost"] == 450
    assert kept["price_date"] == "2026-09-30"
    assert kept["source"] == "price_list"

    # Cost first, then a date in its own patch, then clear the cost.
    session.quote_set_rates(manual={key: {"unit_cost": "455"}})
    session.quote_set_rates(manual={key: {"price_date": "2026-09-30"}})
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["unit_cost"] == 455
    assert stored["price_date"] == "2026-09-30"
    assert stored["price_date_with_cost"] is True
    session.quote_set_rates(manual={key: {"unit_cost": ""}})
    manual = session.project.properties["quote"].get("manual") or {}
    assert key not in manual
    separate = next(item for item in session.quote_build(today="2026-10-08")["parts"] if item["key"] == key)
    assert separate["unit_cost"] == 450
    assert separate["price_date"] == "2020-01-15"
    assert separate["stale"] is True
    assert separate["age_days"] == 2458
    assert separate["flag"] == "STALE"


def test_parts_grid_clears_a_date_typed_in_its_own_cell_with_the_cost(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    session = Session.create("Grid date")
    session.place("adc.durapulse.gs21_20p5", ref="U1", x=40, y=30)
    key = next(row["key"] for row in session.quote_build()["parts"] if "GS21" in row["catalog"])
    prices = tmp_path / "gs21.csv"
    prices.write_text(
        "catalog,vendor,cost,date\nGS21-20P5,AD,450.00,2020-01-15\n",
        encoding="utf-8",
    )
    session.quote_import_prices(prices)
    window = MainWindow(session)
    window.resize(1280, 860)
    window.show()
    app.processEvents()
    window.show_quote()
    app.processEvents()
    dock = window.quote
    row = dock._part_keys.index(key)

    dock.parts.item(row, 5).setText("455.00")
    app.processEvents()
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["unit_cost"] == 455
    assert stored["price_date_defaulted"] is True

    row = dock._part_keys.index(key)
    dock.parts.item(row, 9).setText("2026-09-30")
    app.processEvents()
    stored = session.project.properties["quote"]["manual"][key]
    assert stored["unit_cost"] == 455
    assert stored["price_date"] == "2026-09-30"
    assert stored["price_date_with_cost"] is True
    row = dock._part_keys.index(key)
    assert dock.parts.item(row, 11).text() == ""

    row = dock._part_keys.index(key)
    dock.parts.item(row, 5).setText("")
    app.processEvents()
    manual = session.project.properties["quote"].get("manual") or {}
    assert key not in manual
    row = dock._part_keys.index(key)
    assert "450.00" in dock.parts.item(row, 5).text()
    assert dock.parts.item(row, 9).text() == "2020-01-15"
    expected_age = f"{(date.today() - date(2020, 1, 15)).days} days"
    assert dock.parts.item(row, 10).text() == expected_age
    assert dock.parts.item(row, 11).text() == "STALE"
    assert "stale 1" in dock.totals.text()
    window.close()


def test_quote_build_jsonl_then_another_op(tmp_path: Path):
    project = tmp_path / "job.pcad.yaml"
    session = Session.create("JSONL quote")
    session.place("iec.terminals.x_2p5", ref="X1", x=20, y=20)
    session.save(project)
    cmds = [
        {"id": 1, "op": "open", "path": str(project)},
        {"id": 2, "op": "quote_build", "today": "2026-10-08"},
        {"id": 3, "op": "quote_set_rates", "number": "Q-1"},
    ]
    stdout = StringIO()
    run_agent(StringIO("\n".join(json.dumps(cmd) for cmd in cmds) + "\n"), stdout)
    lines = [json.loads(row) for row in stdout.getvalue().splitlines()]
    assert [row["id"] for row in lines] == [1, 2, 3]
    assert all(row["ok"] for row in lines)
    total = lines[1]["result"]["totals"]["labor_cost"]
    assert isinstance(total, str)
    assert Decimal(total) > 0
    hours = lines[1]["result"]["labor"][0]["hours"]
    assert isinstance(hours, str)
    assert lines[2]["result"]["number"] == "Q-1"


def test_run_agent_survives_a_reply_that_is_not_json(monkeypatch):
    def handle(self, msg):
        if msg.get("op") == "bad":
            return {"id": msg.get("id"), "ok": True, "result": object()}
        return {"id": msg.get("id"), "ok": True, "result": {"name": "ok"}}

    monkeypatch.setattr(Agent, "handle", handle)
    stdin = StringIO(
        json.dumps({"id": 1, "op": "bad"}) + "\n" + json.dumps({"id": 2, "op": "next"}) + "\n"
    )
    stdout = StringIO()
    run_agent(stdin, stdout)
    lines = [json.loads(row) for row in stdout.getvalue().splitlines()]
    assert lines[0]["ok"] is False
    assert lines[0]["id"] == 1
    assert "reply was not JSON" in lines[0]["error"]
    assert lines[1]["ok"] is True
    assert lines[1]["result"]["name"] == "ok"


def test_customer_unit_price_matches_the_amount_format(tmp_path: Path):
    from panelcad.render.print_package import extract_pdf_text

    session, key = _price_session()
    session.quote_set_rates(
        currency="USD",
        manual={key: {"unit_cost": "1080", "price_date": "2026-10-01"}},
    )
    pdf = tmp_path / "customer.pdf"
    book = tmp_path / "customer.xlsx"
    session.quote_export(pdf, view="customer", today="2026-10-08")
    session.quote_export(book, view="customer", today="2026-10-08")
    text = extract_pdf_text(pdf)
    assert "1,080.00 USD" in text
    assert "1080.00" not in text.replace("1,080.00", "")
    with zipfile.ZipFile(book) as zipped:
        parts = zipped.read("xl/worksheets/sheet2.xml").decode()
    assert 's="1"><v>1080.00</v>' in parts
    assert "1080.00 USD" not in parts


def test_section_reserves_the_first_row_height():
    from panelcad.quote.export import _row_height

    cells = ["Wires", "21 wires", "x" * 500, "1.050", "26.25 USD"]
    height = _row_height(cells, LABOR_INTERNAL)
    assert height > 6.4
    assert section_block_mm(LABOR_INTERNAL, cells) == pytest.approx(6.0 + 6.2 + height)
    assert section_block_mm(LABOR_INTERNAL, None) == pytest.approx(6.0 + 6.2 + 6.4)


def test_quote_import_and_export_log_their_paths(tmp_path, monkeypatch):
    logged: list[tuple[str, str]] = []

    def capture(action: str, path: object = None) -> None:
        logged.append((action, "" if path is None else str(path)))

    monkeypatch.setattr("panelcad.diag.log.log_io", capture)
    session, key = _price_session()
    prices = tmp_path / "prices.csv"
    prices.write_text("catalog,cost\n1489-M3C160,1.00\n", encoding="utf-8")
    session.quote_import_prices(prices)
    dest = tmp_path / "quote.json"
    session.quote_export(dest, fmt="json", today="2026-10-08")
    assert ("quote_import_prices", str(prices)) in logged
    assert ("quote_export", str(dest)) in logged
    missing = tmp_path / "missing.csv"
    with pytest.raises(ValueError):
        session.quote_import_prices(missing)
    assert any(action == "error" and "quote_import_prices" in path for action, path in logged)
    _ = key


def test_markup_labels_keep_whole_numbers():
    from panelcad.quote.export import _total_rows
    from panelcad.quote.money import format_percent

    assert format_percent(0) == "0"
    assert format_percent(10) == "10"
    assert format_percent(100) == "100"
    assert format_percent(12.5) == "12.5"
    assert format_percent("0") == "0"
    assert format_percent("10") == "10"
    assert format_percent("100") == "100"
    assert format_percent("12.50") == "12.5"
    assert format_percent(0.0) == "0"
    assert format_percent(10.0) == "10"
    assert format_percent(Decimal("10")) == "10"
    assert format_percent(Decimal("100")) == "100"
    assert format_percent(Decimal("12.50")) == "12.5"
    assert format_percent(Decimal("0")) == "0"
    assert count_phrase(8, "device") == "8 devices"
    assert count_phrase(0, "block") == "0 blocks"
    assert count_phrase(1, "device") == "1 device"
    assert count_phrase(24, "wire") == "24 wires"
    assert count_phrase(3, "cable") == "3 cables"
    assert count_phrase(48, "end") == "48 ends"

    session = Session.create("Markup")
    session.project.properties["quote"] = {"labor_markup_percent": 10, "parts_markup_percent": 10}
    quote = session.quote_build()
    assert quote["parts_markup_label"] == "Parts markup (10%)"
    labels = [label for label, _value in _total_rows(quote, False)]
    assert "Parts markup (10%)" in labels
    assert "Labor markup (10%)" in labels
    assert "Parts markup (1%)" not in labels
    assert "Labor markup (1%)" not in labels

    session.project.properties["quote"] = {"labor_markup_percent": 0, "parts_markup_percent": 0}
    quote = session.quote_build()
    assert quote["parts_markup_label"] == "Parts markup (0%)"
    labels = [label for label, _value in _total_rows(quote, False)]
    assert "Labor markup (0%)" in labels
    assert "Labor markup (%)" not in labels

    session.project.properties["quote"] = {"labor_markup_percent": 100, "parts_markup_percent": "12.50"}
    quote = session.quote_build()
    assert quote["parts_markup_label"] == "Parts markup (12.5%)"
    labels = [label for label, _value in _total_rows(quote, False)]
    assert "Labor markup (100%)" in labels
