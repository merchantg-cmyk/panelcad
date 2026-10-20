"""Write a quote to xlsx, PDF, or CSV. Files only — nothing is uploaded or mailed."""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from panelcad.core.models import Project
from panelcad.library.catalog import Catalog
from panelcad.library.parts import PartsCatalog
from panelcad.quote.build import build_quote
from panelcad.quote.labor import EXAMPLE_LABEL, FILES_ONLY, count_phrase
from panelcad.quote.money import Money, cents, format_amount, format_percent, format_unit_price, to_decimal
from panelcad.quote.xlsx_io import SheetNumber, write_xlsx

PAGE_W = 215.9
PAGE_H = 279.4
MARGIN = 12.0
CONTENT_W = PAGE_W - 2 * MARGIN

# Widths sum to CONTENT_W so a column cannot run off the page.
PARTS_INTERNAL = (
    ("Item", 11.0),
    ("Qty", 12.0),
    ("Catalog", 28.0),
    ("Description", 32.9),
    ("Vendor", 18.0),
    ("Unit cost", 18.0),
    ("Ext. cost", 18.0),
    ("Lead", 22.0),
    ("Age", 32.0),
)
_RIGHT_HEADERS = {"Qty", "Unit cost", "Ext. cost", "Unit price", "Amount", "Hours", "Cost", "Count"}
PARTS_CUSTOMER = (
    ("Item", 14.0),
    ("Qty", 14.0),
    ("Catalog", 36.0),
    ("Description", 62.0),
    ("Unit price", 24.0),
    ("Amount", 24.0),
    ("Lead", 17.9),
)
LABOR_INTERNAL = (
    ("Description", 62.0),
    ("Count", 18.0),
    ("How computed", 70.0),
    ("Hours", 16.0),
    ("Cost", 25.9),
)
LABOR_CUSTOMER = (
    ("Description", 130.0),
    ("Amount", 61.9),
)


def _check_widths() -> None:
    for columns in (PARTS_INTERNAL, PARTS_CUSTOMER, LABOR_INTERNAL, LABOR_CUSTOMER):
        total = sum(width for _title, width in columns)
        if abs(total - CONTENT_W) > 0.05:
            raise RuntimeError(f"quote columns {total} != {CONTENT_W}")


_check_widths()


def resolve_format(path: Path, fmt: str | None) -> tuple[Path, str]:
    mapping = {".xlsx": "xlsx", ".pdf": "pdf", ".csv": "csv", ".json": "json"}
    kind = (fmt or "").lower().lstrip(".")
    if not kind:
        kind = mapping.get(path.suffix.lower(), "xlsx")
    if kind not in mapping.values():
        raise ValueError("format must be xlsx, pdf, csv, or json")
    if path.suffix.lower() != "." + kind:
        path = path.with_suffix("." + kind)
    return path, kind


def export_quote(
    project: Project,
    catalog: Catalog,
    path: Path | str,
    *,
    parts: PartsCatalog | None = None,
    fmt: str | None = None,
    view: str = "internal",
    today: date | None = None,
) -> dict[str, Any]:
    """Write the quote. view=customer drops cost and markup columns. Nothing is sent."""

    audience = str(view or "internal").lower()
    if audience not in {"internal", "customer"}:
        raise ValueError("view must be internal or customer")
    dest, kind = resolve_format(Path(path), fmt)
    quote = build_quote(project, catalog, parts, today=today)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pages = 0
    if kind == "xlsx":
        write_xlsx(dest, workbook_sheets(quote, audience))
    elif kind == "csv":
        _write_csv(dest, quote, audience)
    elif kind == "json":
        payload = customer_document(quote) if audience == "customer" else quote
        dest.write_text(json.dumps(payload, indent=2, cls=_QuoteEncoder), encoding="utf-8")
    else:
        pages = _write_pdf(dest, quote, audience)
    return {
        "path": str(dest),
        "format": kind,
        "view": audience,
        "pages": pages,
        "sent": False,
        "files_only": True,
        "incomplete": bool(quote["incomplete"]),
        "unpriced": int(quote["unpriced"]),
        "lines": int(quote["lines"]),
        "total_label": str(quote["total_label"]),
        "example_defaults": bool(quote["example_defaults"]),
    }


class _QuoteEncoder(json.JSONEncoder):
    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            return format(o, "f")
        if isinstance(o, Money):
            return o.text()
        return super().default(o)


def _item_cell(value: object) -> int | str:
    """BOM item numbers are spreadsheet numbers. Anything else stays text."""

    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    return text


def _money_cell(value: object) -> Money | str:
    if value is None or value == "":
        return ""
    return Money(cents(value))  # type: ignore[arg-type]


def _hours_cell(value: object) -> SheetNumber:
    return SheetNumber(to_decimal(value), 2)


def _percent_cell(value: object) -> SheetNumber:
    return SheetNumber(to_decimal(value))


def _needs_info_part(row: dict[str, Any]) -> bool:
    return bool(row.get("needs_info"))


def _unit_price_amount(row: dict[str, Any]) -> Decimal | None:
    if _needs_info_part(row) or row.get("missing_price"):
        return None
    qty = int(row.get("qty") or 0)
    if qty <= 0 or row.get("extended_sell") in (None, ""):
        return None
    return cents(row["extended_sell"]) / Decimal(qty)


def _unit_price_cell(row: dict[str, Any]) -> Money | str:
    if _needs_info_part(row):
        return "needs-info"
    if row.get("missing_price"):
        return "Pending"
    amount = _unit_price_amount(row)
    if amount is None:
        return ""
    return Money(amount)


def _csv_money(value: object) -> str:
    if value is None or value == "":
        return ""
    return format(cents(value), ".2f")  # type: ignore[arg-type]


def _visible_labor(quote: dict[str, Any], customer: bool) -> list[dict[str, Any]]:
    if not customer:
        return list(quote["labor"])
    return [row for row in quote["labor"] if to_decimal(row.get("hours") or 0) != 0]


def customer_document(quote: dict[str, Any]) -> dict[str, Any]:
    """Customer JSON. Sell amounts only. No cost, markup, vendor, or shop rate."""

    totals = quote["totals"]
    return {
        "number": quote["number"],
        "customer": quote["customer"],
        "project": quote["project"],
        "prepared": quote["prepared"],
        "currency": quote["currency"],
        "incomplete": quote["incomplete"],
        "unpriced": quote["unpriced"],
        "needs_info": quote.get("needs_info") or 0,
        "needs_info_label": quote.get("needs_info_label") or "",
        "lines": quote["lines"],
        "total_label": quote["total_label"],
        "parts": [
            {
                "item": row["item"],
                "qty": row["qty"],
                "catalog": row["catalog"] or row["part_number"],
                "description": row["description"],
                "refs": row["refs"],
                "unit_price": (
                    "needs-info"
                    if _needs_info_part(row)
                    else "Pending"
                    if row["missing_price"]
                    else format_unit_price(row["extended_sell"], row["qty"])
                ),
                "amount": ""
                if _needs_info_part(row) or row["missing_price"]
                else format(cents(row["extended_sell"]), ".2f"),
                "lead_time": row["lead_time"],
            }
            for row in quote["parts"]
        ],
        "labor": [
            {"description": row["description"], "amount": format(cents(row["sell"]), ".2f")}
            for row in _visible_labor(quote, True)
        ],
        "totals": {
            "parts": format(cents(totals["parts_sell"]), ".2f"),
            "labor": format(cents(totals["labor_sell"]), ".2f"),
            "contingency": format(cents(totals["contingency"]), ".2f"),
            "freight": format(cents(totals["freight"]), ".2f"),
            "tax": format(cents(totals["tax"]), ".2f"),
            "total": format(cents(totals["total"]), ".2f"),
        },
        "notes": _pdf_notes(quote, True),
        "sent": False,
        "files_only": True,
    }


def _money(quote: dict[str, Any], value: float | None) -> str:
    return format_amount(value, str(quote.get("currency") or ""))


def _describe(row: dict[str, Any]) -> str:
    text = str(row.get("description") or "")
    refs = str(row.get("refs") or "")
    if refs:
        return f"{text} [{refs}]"
    return text


def workbook_sheets(quote: dict[str, Any], view: str) -> dict[str, list[list[object]]]:
    customer = view == "customer"
    totals = quote["totals"]
    summary: list[list[object]] = [
        ["Quote", quote["number"]],
        ["Customer", quote["customer"]],
        ["Project", quote["project"]],
        ["Prepared", quote["prepared"]],
    ]
    if not customer and quote.get("label"):
        summary.append(["Label", quote["label"]])
    summary.append(["Files only", FILES_ONLY])
    summary.append([quote["total_label"], _money_cell(totals["total"])])
    if customer:
        summary.extend(
            [
                ["Parts", _money_cell(totals["parts_sell"])],
                ["Labor", _money_cell(totals["labor_sell"])],
                ["Contingency", _money_cell(totals["contingency"])],
                ["Freight", _money_cell(totals["freight"])],
                ["Tax", _money_cell(totals["tax"])],
                ["Pending lines", quote["unpriced"]],
                ["Lines", quote["lines"]],
            ]
        )
    else:
        summary.extend(
            [
                ["Price mode", quote["price_mode"]],
                ["Preferred vendor", quote["preferred_vendor"]],
                ["Parts cost", _money_cell(totals["parts_cost"])],
                [quote["parts_markup_label"], _money_cell(totals["parts_sell"] - totals["parts_cost"])],
                ["Parts sell", _money_cell(totals["parts_sell"])],
                ["Labor hours", _hours_cell(totals["labor_hours"])],
                ["Labor cost", _money_cell(totals["labor_cost"])],
                ["Labor markup percent", _percent_cell(quote["labor_markup_percent"])],
                ["Labor sell", _money_cell(totals["labor_sell"])],
                ["Contingency percent", _percent_cell(quote["contingency_percent"])],
                ["Contingency", _money_cell(totals["contingency"])],
                ["Freight", _money_cell(totals["freight"])],
                ["Tax percent", _percent_cell(quote["tax_percent"])],
                ["Tax", _money_cell(totals["tax"])],
                ["Unpriced lines", quote["unpriced"]],
                ["Needs info (not counted)", quote.get("needs_info") or 0],
                ["Lines", quote["lines"]],
                ["Stale prices", quote["stale"]],
                ["Stale after days", quote["stale_after_days"]],
            ]
        )
    if customer:
        part_header = ["Item", "Qty", "Catalog", "Description", "Refs", "Unit price", "Amount", "Lead time"]
    else:
        part_header = [
            "Item",
            "Qty",
            "Catalog",
            "Description",
            "Refs",
            "Vendor",
            "Unit cost",
            "Extended cost",
            "Markup percent",
            "Unit price",
            "Amount",
            "Lead time",
            "Price date",
            "Age",
            "Flag",
            "Source",
            "Category",
            "Library id",
        ]
    parts_rows: list[list[object]] = [part_header]
    for row in quote["parts"]:
        if customer:
            parts_rows.append(
                [
                    _item_cell(row["item"]),
                    row["qty"],
                    row["catalog"] or row["part_number"],
                    row["description"],
                    row["refs"],
                    _unit_price_cell(row),
                    "" if _needs_info_part(row) or row["missing_price"] else _money_cell(row["extended_sell"]),
                    row["lead_time"],
                ]
            )
        else:
            parts_rows.append(
                [
                    _item_cell(row["item"]),
                    row["qty"],
                    row["catalog"] or row["part_number"],
                    row["description"],
                    row["refs"],
                    row["vendor"],
                    "" if row["unit_cost"] is None else _money_cell(row["unit_cost"]),
                    "" if row["extended_cost"] is None else _money_cell(row["extended_cost"]),
                    _percent_cell(row["markup_percent"]),
                    _unit_price_cell(row),
                    "" if _needs_info_part(row) or row["missing_price"] else _money_cell(row["extended_sell"]),
                    row["lead_time"],
                    row["price_date"],
                    row["age_label"],
                    row["flag"],
                    row["source"],
                    row["category"],
                    row["library_id"],
                ]
            )
    if customer:
        labor_header = ["Description", "Amount"]
    else:
        labor_header = [
            "Description",
            "Count",
            "Unit",
            "Minutes",
            "Hours",
            "Rate id",
            "Shop rate",
            "Cost",
            "Markup percent",
            "Sell",
            "How computed",
        ]
    labor_rows: list[list[object]] = [labor_header]
    for row in _visible_labor(quote, customer):
        if customer:
            labor_rows.append([row["description"], _money_cell(row["sell"])])
        else:
            labor_rows.append(
                [
                    row["description"],
                    row["count"],
                    row["unit"],
                    row["minutes"],
                    _hours_cell(row["hours"]),
                    row["rate_id"],
                    row["rate_per_hour"],
                    "" if row["cost"] is None else _money_cell(row["cost"]),
                    _percent_cell(row["markup_percent"]),
                    "" if row["sell"] is None else _money_cell(row["sell"]),
                    row["breakdown"],
                ]
            )
    assumptions = [[FILES_ONLY]]
    if customer:
        assumptions.append(["Lines marked Pending have no price and are not in the total."])
        assumptions.append([quote["total_label"]])
    else:
        if quote.get("label"):
            assumptions.insert(0, [quote["label"]])
        assumptions.extend([[line] for line in quote["assumptions"]])
    return {
        "Summary": summary,
        "Parts": parts_rows,
        "Labor": labor_rows,
        "Assumptions": assumptions,
    }


def _write_csv(path: Path, quote: dict[str, Any], view: str) -> None:
    customer = view == "customer"
    if customer:
        fields = [
            "section",
            "item",
            "description",
            "qty",
            "catalog",
            "lead_time",
            "unit_price",
            "extended_price",
        ]
    else:
        fields = [
            "section",
            "item",
            "description",
            "qty",
            "catalog",
            "vendor",
            "unit_cost",
            "extended_cost",
            "markup_percent",
            "unit_price",
            "extended_price",
            "lead_time",
            "price_date",
            "age",
            "flag",
            "hours",
            "shop_rate",
            "breakdown",
        ]
    rows: list[dict[str, object]] = []
    for part in quote["parts"]:
        base = {
            "section": "part",
            "item": part["item"],
            "description": part["description"],
            "qty": part["qty"],
            "catalog": part["catalog"] or part["part_number"],
            "lead_time": part["lead_time"],
            "unit_price": (
                "needs-info"
                if _needs_info_part(part)
                else "Pending"
                if part["missing_price"]
                else format_unit_price(part["extended_sell"], part["qty"])
            ),
            "extended_price": ""
            if _needs_info_part(part) or part["missing_price"]
            else _csv_money(part["extended_sell"]),
        }
        if not customer:
            base.update(
                {
                    "vendor": part["vendor"],
                    "unit_cost": _csv_money(part["unit_cost"]),
                    "extended_cost": _csv_money(part["extended_cost"]),
                    "markup_percent": part["markup_percent"],
                    "price_date": part["price_date"],
                    "age": part["age_label"],
                    "flag": part["flag"],
                    "hours": "",
                    "shop_rate": "",
                    "breakdown": part["note"],
                }
            )
        rows.append(base)
    for labor in _visible_labor(quote, customer):
        base = {
            "section": "labor",
            "item": "" if customer else labor["id"],
            "description": labor["description"],
            "qty": labor["count"],
            "catalog": "",
            "lead_time": "",
            "unit_price": "",
            "extended_price": _csv_money(labor["sell"]),
        }
        if not customer:
            base.update(
                {
                    "vendor": "",
                    "unit_cost": _csv_money(labor["cost"]),
                    "extended_cost": _csv_money(labor["cost"]),
                    "markup_percent": labor["markup_percent"],
                    "price_date": "",
                    "age": "",
                    "flag": "",
                    "hours": labor["hours"],
                    "shop_rate": labor["rate_per_hour"],
                    "breakdown": labor["breakdown"],
                }
            )
        rows.append(base)
    totals = quote["totals"]
    total_rows = [
        ("Parts", totals["parts_sell"], totals["parts_cost"]),
        ("Labor", totals["labor_sell"], totals["labor_cost"]),
        ("Contingency", totals["contingency"], None),
        ("Freight", totals["freight"], None),
        ("Tax", totals["tax"], None),
        (quote["total_label"], totals["total"], None),
    ]
    for label, sell, cost in total_rows:
        base = {
            "section": "total",
            "item": "",
            "description": label,
            "qty": "",
            "catalog": "",
            "lead_time": "",
            "unit_price": "",
            "extended_price": _csv_money(sell),
        }
        if not customer:
            base.update(
                {
                    "vendor": "",
                    "unit_cost": "" if cost is None else _csv_money(cost),
                    "extended_cost": "" if cost is None else _csv_money(cost),
                    "markup_percent": "",
                    "price_date": "",
                    "age": "",
                    "flag": "",
                    "hours": "",
                    "shop_rate": "",
                    "breakdown": "",
                }
            )
        rows.append(base)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_pdf(path: Path, quote: dict[str, Any], view: str) -> int:
    from PySide6.QtGui import QColor, QPainter, QPdfWriter

    from panelcad.api.session import _ensure_qt_app
    from panelcad.render.page import configure_full_page
    from panelcad.render.qt_painter import QtPainter

    # QFontDatabase needs an application. Offscreen when nothing is on screen.
    _ensure_qt_app()
    writer = QPdfWriter(str(path))
    writer.setResolution(300)
    title = quote["project"] or "PanelCAD quote"
    writer.setTitle(title)
    if hasattr(writer, "setCreator"):
        writer.setCreator("PanelCAD")
    configure_full_page(writer, PAGE_W, PAGE_H)
    painter = QPainter(writer)
    try:
        dpi_x = float(writer.logicalDpiX() or 72)
        dpi_y = float(writer.logicalDpiY() or 72)
        painter.scale(dpi_x / 25.4, dpi_y / 25.4)
        pages = _paint_quote(painter, writer, QtPainter(painter, print_paper=True), quote, view)
    finally:
        if painter.isActive():
            painter.end()
    return pages


def _paint_quote(qpainter, writer, qt, quote: dict[str, Any], view: str) -> int:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor

    customer = view == "customer"
    state = {"page": 1, "y": MARGIN}

    def new_page() -> None:
        writer.newPage()
        state["page"] += 1
        state["y"] = MARGIN
        qpainter.fillRect(QRectF(0, 0, PAGE_W, PAGE_H), QColor("#ffffff"))
        _footer(qt, quote, state["page"], customer)

    qpainter.fillRect(QRectF(0, 0, PAGE_W, PAGE_H), QColor("#ffffff"))
    _footer(qt, quote, 1, customer)
    state["y"] = _title_block(qt, quote, customer)

    def begin_section(title: str, columns, first_cells: list[str] | None = None) -> None:
        if state["y"] + section_block_mm(columns, first_cells) > PAGE_H - 18:
            new_page()
        state["y"] = _section_title(qt, state["y"], title)
        state["y"] = _header_row(qpainter, qt, state["y"], columns)

    columns = PARTS_CUSTOMER if customer else PARTS_INTERNAL
    part_rows = list(quote["parts"])
    first_part = _pdf_part_cells(quote, part_rows[0], customer) if part_rows else None
    begin_section("Parts", columns, first_part)
    for row in part_rows:
        cells = _pdf_part_cells(quote, row, customer)
        height = _row_height(cells, columns)
        if state["y"] + height > PAGE_H - 18:
            new_page()
            state["y"] = _header_row(qpainter, qt, state["y"], columns)
        _data_row(qpainter, qt, state["y"], columns, cells, height, _row_fill(row, customer))
        state["y"] += height
    state["y"] += 4
    labor_cols = LABOR_CUSTOMER if customer else LABOR_INTERNAL
    labor_rows = _visible_labor(quote, customer)
    first_labor = _pdf_labor_cells(quote, labor_rows[0], customer) if labor_rows else None
    begin_section("Labor", labor_cols, first_labor)
    for row in labor_rows:
        cells = _pdf_labor_cells(quote, row, customer)
        height = _row_height(cells, labor_cols)
        if state["y"] + height > PAGE_H - 18:
            new_page()
            state["y"] = _header_row(qpainter, qt, state["y"], labor_cols)
        _data_row(qpainter, qt, state["y"], labor_cols, cells, height, "#ffffff")
        state["y"] += height
    state["y"] += 5
    totals_h = _totals_height(quote, customer)
    if state["y"] + totals_h > PAGE_H - 16:
        new_page()
    _totals(qt, state["y"], quote, customer)
    state["y"] += totals_h + 3
    notes = _pdf_notes(quote, customer)
    for note in notes:
        wrapped = _wrap(note, CONTENT_W - 2, 2.3, 4)
        need = 3.2 * len(wrapped) + 1
        if state["y"] + need > PAGE_H - 16:
            new_page()
        for line in wrapped:
            qt.text(MARGIN, state["y"] + 2.6, line, 2.3, align="start")
            state["y"] += 3.2
        state["y"] += 1.2
    # Stamp the real page count into the footers by redrawing is not available
    # after the page is closed, so the footer shows the page index only.
    return state["page"]


def _unit_price_text(quote: dict[str, Any], row: dict[str, Any]) -> str:
    if _needs_info_part(row):
        return "needs-info"
    if row["missing_price"]:
        return "Pending"
    amount = _unit_price_amount(row)
    if amount is None:
        return ""
    return format_amount(amount, str(quote.get("currency") or ""))


def _pdf_part_cells(quote: dict[str, Any], row: dict[str, Any], customer: bool) -> list[str]:
    catalog = row["catalog"] or row["part_number"]
    if customer:
        amount = "" if _needs_info_part(row) or row["missing_price"] else _money(quote, row["extended_sell"])
        return [
            str(row["item"]),
            str(row["qty"]),
            catalog,
            _describe(row),
            _unit_price_text(quote, row),
            amount,
            row["lead_time"],
        ]
    if _needs_info_part(row):
        age = "needs-info"
    elif row["missing_price"]:
        age = "NO PRICE"
    elif row["stale"]:
        age = f"{row['age_label']} STALE".strip()
    elif row["flag"]:
        age = row["flag"]
    else:
        age = row["age_label"]
    return [
        str(row["item"]),
        str(row["qty"]),
        catalog,
        _describe(row),
        row["vendor"],
        "" if row["unit_cost"] is None else _money(quote, row["unit_cost"]),
        "" if row["extended_cost"] is None else _money(quote, row["extended_cost"]),
        row["lead_time"],
        age,
    ]


def _pdf_labor_cells(quote: dict[str, Any], row: dict[str, Any], customer: bool) -> list[str]:
    if customer:
        amount = "" if row["sell"] is None else _money(quote, row["sell"])
        return [row["description"], amount]
    count = row["count"]
    unit = str(row.get("unit") or "")
    if unit == "m":
        count_text = f"{float(count):.3f} m"
    else:
        count_text = count_phrase(float(count), unit)
    return [
        row["description"],
        count_text,
        row["breakdown"],
        f"{to_decimal(row['hours']):.3f}",
        "" if row["cost"] is None else _money(quote, row["cost"]),
    ]


def _pdf_notes(quote: dict[str, Any], customer: bool) -> list[str]:
    if customer:
        return [
            FILES_ONLY,
            "Lines marked Pending have no price and are not included in the total.",
            quote["total_label"],
        ]
    return list(quote["assumptions"])


def _row_fill(row: dict[str, Any], customer: bool) -> str:
    if row["missing_price"]:
        return "#f8d7d7"
    if not customer and row["stale"]:
        return "#f8e8c0"
    return "#ffffff"


def _title_block(qt, quote: dict[str, Any], customer: bool) -> float:
    qt.set_stroke("#222222", 0.4)
    qt.set_fill("#f4f1e8")
    qt.rect(MARGIN, MARGIN, CONTENT_W, 28)
    heading = "QUOTATION" if customer else "QUOTE — INTERNAL"
    qt.text(MARGIN + 2, MARGIN + 6, heading, 5.0, align="start")
    if not customer:
        qt.text(MARGIN + 2, MARGIN + 11, "Internal cost view", 2.4, align="start")
    meta_y = MARGIN + 16
    bits = [
        f"Quote {quote['number'] or '—'}",
        f"Customer {quote['customer'] or '—'}",
        f"Project {quote['project'] or '—'}",
        f"Prepared {quote['prepared']}",
    ]
    qt.text(MARGIN + 2, meta_y, "   ".join(bits), 2.5, align="start")
    if not customer and quote.get("label"):
        qt.text(MARGIN + 2, meta_y + 5, str(quote["label"]), 2.4, align="start")
    return MARGIN + 32


def _section_title(qt, y: float, title: str) -> float:
    qt.text(MARGIN, y + 4, title, 3.6, align="start")
    return y + 6


def _header_row(qpainter, qt, y: float, columns: tuple[tuple[str, float], ...]) -> float:
    from PySide6.QtCore import QRectF

    qt.set_stroke("#222222", 0.25)
    qt.set_fill("#e4e4e4")
    qt.rect(MARGIN, y, CONTENT_W, 6.2)
    x = MARGIN
    for title, width in columns:
        qpainter.save()
        qpainter.setClipRect(QRectF(x, y, width, 6.2))
        if title in _RIGHT_HEADERS:
            qt.text(x + width - 1.0, y + 4.2, title, 2.3, align="end")
        else:
            qt.text(x + 1, y + 4.2, title, 2.3, align="start")
        qpainter.restore()
        x += width
    return y + 6.2


def _data_row(qpainter, qt, y: float, columns, cells: list[str], height: float, fill: str) -> None:
    from PySide6.QtCore import QRectF

    qt.set_stroke("#bbbbbb", 0.15)
    qt.set_fill(fill)
    qt.rect(MARGIN, y, CONTENT_W, height)
    x = MARGIN
    for (title, width), text in zip(columns, cells):
        qpainter.save()
        qpainter.setClipRect(QRectF(x, y, width, height))
        lines = _wrap(text, width - 1.6, 2.2, 4)
        baseline = y + 3.0
        for line in lines:
            align = "end" if title in {"Qty", "Unit cost", "Ext. cost", "Unit price", "Amount", "Hours", "Cost", "Count"} else "start"
            if align == "end":
                qt.text(x + width - 0.8, baseline, line, 2.2, align="end")
            else:
                qt.text(x + 0.8, baseline, line, 2.2, align="start")
            baseline += 2.8
        qpainter.restore()
        x += width


def section_block_mm(columns, first_cells: list[str] | None) -> float:
    """Title, column header, and the real height of the first row."""

    first_h = _row_height(first_cells, columns) if first_cells else 6.4
    return 6.0 + 6.2 + first_h


def _row_height(cells: list[str], columns) -> float:
    lines = 1
    for (_title, width), text in zip(columns, cells):
        lines = max(lines, len(_wrap(text, width - 1.6, 2.2, 4)))
    return max(6.4, lines * 2.8 + 1.2)


def _totals_height(quote: dict[str, Any], customer: bool) -> float:
    return 6 + len(_total_rows(quote, customer)) * 4.4


def _total_rows(quote: dict[str, Any], customer: bool) -> list[tuple[str, str]]:
    totals = quote["totals"]
    if customer:
        rows = [
            ("Parts", _money(quote, totals["parts_sell"])),
            ("Labor", _money(quote, totals["labor_sell"])),
            ("Contingency", _money(quote, totals["contingency"])),
            ("Freight", _money(quote, totals["freight"])),
            ("Tax", _money(quote, totals["tax"])),
            (quote["total_label"], _money(quote, totals["total"])),
        ]
        if quote.get("needs_info_label"):
            rows.append((str(quote["needs_info_label"]), ""))
        return rows
    rows = [
        ("Parts cost", _money(quote, totals["parts_cost"])),
        (quote["parts_markup_label"], _money(quote, totals["parts_sell"] - totals["parts_cost"])),
        ("Parts sell", _money(quote, totals["parts_sell"])),
        ("Labor hours", f"{to_decimal(totals['labor_hours']):.3f}"),
        ("Labor cost", _money(quote, totals["labor_cost"])),
        (
            f"Labor markup ({format_percent(quote['labor_markup_percent'])}%)",
            _money(quote, totals["labor_sell"] - totals["labor_cost"]),
        ),
        ("Shop rate is on each labor line", ""),
        ("Labor sell", _money(quote, totals["labor_sell"])),
        (f"Contingency ({quote['contingency_percent']:g}%)", _money(quote, totals["contingency"])),
        ("Freight", _money(quote, totals["freight"])),
        (f"Tax ({quote['tax_percent']:g}%)", _money(quote, totals["tax"])),
        (quote["total_label"], _money(quote, totals["total"])),
        (f"Unpriced lines: {quote['unpriced']}", ""),
    ]
    if quote.get("needs_info_label"):
        rows.append((str(quote["needs_info_label"]), ""))
    rows.append((f"Stale prices: {quote['stale']}", ""))
    return rows


def _totals(qt, y: float, quote: dict[str, Any], customer: bool) -> None:
    rows = _total_rows(quote, customer)
    height = 4 + len(rows) * 4.4
    qt.set_stroke("#222222", 0.3)
    qt.set_fill("#f7f7f7")
    qt.rect(MARGIN, y, CONTENT_W, height)
    cursor = y + 4
    for label, amount in rows:
        qt.text(MARGIN + 2, cursor, label, 2.5, align="start")
        if amount:
            qt.text(MARGIN + CONTENT_W - 2, cursor, amount, 2.5, align="end")
        cursor += 4.4


def _footer(qt, quote: dict[str, Any], page: int, customer: bool) -> None:
    y = PAGE_H - 8
    qt.set_stroke("#222222", 0.2)
    qt.line(MARGIN, y - 2, PAGE_W - MARGIN, y - 2)
    left = "Customer copy. Not sent." if customer else "Internal cost view. Not sent."
    qt.text(MARGIN, y + 1.5, left, 2.2, align="start")
    qt.text(PAGE_W - MARGIN, y + 1.5, f"Page {page}", 2.2, align="end")
    _ = quote


def _wrap(text: str, width_mm: float, size_mm: float, max_lines: int) -> list[str]:
    from PySide6.QtGui import QFont, QFontMetricsF

    from panelcad.render.qt_painter import _font_family

    raw = " ".join(str(text or "").split())
    if not raw:
        return [""]
    font = QFont(_font_family())
    font.setPixelSize(72)
    metrics = QFontMetricsF(font)
    scale = size_mm / 72.0

    def width(sample: str) -> float:
        return metrics.horizontalAdvance(sample) * scale

    if width(raw) <= width_mm:
        return [raw]
    words = raw.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if width(trial) <= width_mm:
            current = trial
            continue
        if current:
            lines.append(current)
            current = ""
        if len(lines) >= max_lines:
            break
        if width(word) <= width_mm:
            current = word
            continue
        chunk = ""
        for ch in word:
            if width(chunk + ch) <= width_mm:
                chunk += ch
            else:
                if chunk:
                    lines.append(chunk)
                chunk = ch
                if len(lines) >= max_lines:
                    break
        current = "" if len(lines) >= max_lines else chunk
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines] or [raw]
