"""User price lists. Nothing in this module looks up a price on the network."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from panelcad.quote.money import cents, money_num, parse_number
from panelcad.quote.xlsx_io import read_xlsx

# Slash dates are MM/DD/YYYY unless the first number is greater than 12.
# A whole number in the spreadsheet serial range is an Excel day count
# (epoch 1899-12-30, the spreadsheet leap-year convention).
DATE_NOTE = (
    "Price dates should be YYYY-MM-DD. A slash date is read as MM/DD/YYYY "
    "unless the first number is greater than 12, in which case it is DD/MM/YYYY. "
    "An Excel date cell is read as a day count from 1899-12-30."
)

_CATALOG_ALIASES = {
    "catalog",
    "catalog number",
    "catalog no",
    "catalogue",
    "catalogue number",
    "sku",
    "part",
    "part number",
    "part no",
    "mpn",
    "order number",
    "order no",
    "mfr part number",
    "mfr part no",
    "manufacturer part number",
    "manufacturer part",
}
_VENDOR_ALIASES = {"vendor", "supplier", "distributor"}
_COST_ALIASES = {"cost", "unit cost", "unit price", "price", "price each", "net"}
_DATE_ALIASES = {"date", "price date", "cost date", "as of", "effective", "effective date"}
_LEAD_ALIASES = {"lead", "lead time", "leadtime", "lead days"}
_ROW = "__row__"


def norm_catalog(value: object) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"[\s\-_/\\.]", "", text)


def parse_price_date(value: object) -> date | None:
    """Parse a user-supplied price date. Blank is unknown. Bad text raises."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _excel_serial(float(value))
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        return _excel_serial(float(text))
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if first > 12 and 1 <= second <= 12:
            return date(year, second, first)
        if 1 <= first <= 12 and 1 <= second <= 12:
            return date(year, first, second)
        if second > 12 and 1 <= first <= 12:
            return date(year, first, second)
    raise ValueError(f"unrecognized price date {text!r}. Use YYYY-MM-DD.")


def _excel_serial(serial: float) -> date | None:
    if serial < 20000 or serial > 80000:
        return None
    # Spreadsheet day count, with the 1900 leap-year bug folded into the epoch.
    return date(1899, 12, 30) + timedelta(days=int(serial))


def _header_key(raw: str) -> str:
    text = str(raw or "").strip().lower().replace("_", " ")
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("#", " ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _pick(headers: list[str], aliases: set[str]) -> str | None:
    for header in headers:
        if _header_key(header) in aliases:
            return header
    return None


def _row_date(row: dict[str, Any]) -> date | None:
    raw = row.get("date") or ""
    if not raw:
        return None
    try:
        return parse_price_date(raw)
    except ValueError:
        return None


def _prefer_newer(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """True when candidate should replace current for the same catalog and vendor."""

    new_date = _row_date(candidate)
    old_date = _row_date(current)
    if new_date and old_date and new_date != old_date:
        return new_date > old_date
    if new_date and not old_date:
        return True
    if old_date and not new_date:
        return False
    return True


def _collapse_vendors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per catalog and vendor: the newest price date, else the later row."""

    best: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (norm_catalog(row.get("catalog")), str(row.get("vendor") or "").strip().casefold())
        current = best.get(key)
        if current is None:
            order.append(key)
            best[key] = row
        elif _prefer_newer(row, current):
            best[key] = row
    return [best[key] for key in order]


def _records_from_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
    if not rows:
        raise ValueError("price list is empty")
    headers = [key for key in rows[0].keys() if key != _ROW]
    catalog_col = _pick(headers, _CATALOG_ALIASES)
    cost_col = _pick(headers, _COST_ALIASES)
    if catalog_col is None or cost_col is None:
        found = ", ".join(headers) or "(none)"
        raise ValueError(
            "price list needs a catalog number column and a cost column "
            f"(catalog, vendor, cost, date). Found headers: {found}"
        )
    vendor_col = _pick(headers, _VENDOR_ALIASES)
    date_col = _pick(headers, _DATE_ALIASES)
    lead_col = _pick(headers, _LEAD_ALIASES)
    kept: list[dict[str, Any]] = []
    skipped: list[str] = []
    for row in rows:
        source_row = int(row.get(_ROW) or 0)
        catalog = str(row.get(catalog_col) or "").strip()
        if not catalog:
            continue
        raw_cost = row.get(cost_col)
        if raw_cost is None or str(raw_cost).strip() == "":
            skipped.append(f"row {source_row}: {catalog} has no cost")
            continue
        try:
            cost = parse_number(raw_cost)
        except ValueError as exc:
            skipped.append(f"row {source_row}: {catalog} {exc}")
            continue
        if cost is None:
            skipped.append(f"row {source_row}: {catalog} has no cost")
            continue
        if cost < 0:
            skipped.append(f"row {source_row}: {catalog} cost is negative")
            continue
        price_date = ""
        if date_col:
            raw_date = row.get(date_col)
            if str(raw_date or "").strip():
                try:
                    parsed = parse_price_date(raw_date)
                except ValueError as exc:
                    skipped.append(f"row {source_row}: {exc}")
                    continue
                price_date = parsed.isoformat() if parsed else ""
        kept.append(
            {
                "catalog": catalog,
                "vendor": str(row.get(vendor_col) or "").strip() if vendor_col else "",
                "cost": money_num(cents(cost)),
                "date": price_date,
                "lead_time": str(row.get(lead_col) or "").strip() if lead_col else "",
            }
        )
    collapsed = _collapse_vendors(kept)
    if not collapsed and skipped:
        raise ValueError("price list has no usable rows. " + "; ".join(skipped[:6]))
    return collapsed, skipped


def _decode_csv(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252"), "cp1252"


def _csv_rows(path: Path) -> tuple[list[dict[str, str]], str]:
    text, encoding = _decode_csv(path)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("price list CSV has no header")
    rows: list[dict[str, str]] = []
    for row in reader:
        item = {str(key or ""): str(value or "") for key, value in row.items() if key is not None}
        item[_ROW] = str(reader.line_num)
        rows.append(item)
    return rows, encoding


def _xlsx_rows(path: Path) -> list[dict[str, str]]:
    sheets = read_xlsx(path)
    if not sheets:
        raise ValueError("price list workbook has no sheets")
    grid = next(iter(sheets.values()))
    if not grid:
        raise ValueError("price list sheet is empty")
    headers = [str(cell or "").strip() for cell in grid[0]]
    rows: list[dict[str, str]] = []
    for offset, raw in enumerate(grid[1:], start=2):
        if not any(str(cell or "").strip() for cell in raw):
            continue
        row: dict[str, str] = {_ROW: str(offset)}
        for index, header in enumerate(headers):
            if not header:
                continue
            value = raw[index] if index < len(raw) else ""
            row[header] = "" if value is None else str(value)
        rows.append(row)
    return rows


def read_price_list(path: Path | str) -> tuple[list[dict[str, Any]], list[str], str]:
    """Read a CSV or XLSX price list. Does not fetch anything.

    The third value is the encoding used: ``utf-8``, ``cp1252``, or ``xlsx``.
    """

    src = Path(path)
    if not src.is_file():
        raise ValueError(f"price list not found: {src}")
    suffix = src.suffix.lower()
    if suffix == ".csv":
        rows, encoding = _csv_rows(src)
    elif suffix == ".xlsx":
        rows = _xlsx_rows(src)
        encoding = "xlsx"
    else:
        raise ValueError("price list must be a .csv or .xlsx file")
    kept, skipped = _records_from_rows(rows)
    return kept, skipped, encoding


def choose_offer(
    offers: list[dict[str, Any]],
    *,
    mode: str,
    preferred_vendor: str,
) -> tuple[dict[str, Any] | None, str]:
    """Pick the cheapest offer, or the preferred vendor when that row exists.

    When the chosen row is older than another row, the note says so. The quote
    still flags that line stale once it is past the shop's age limit.
    """

    usable = [row for row in offers if row.get("cost") is not None]
    if not usable:
        return None, ""
    ordered = sorted(usable, key=lambda row: (cents(row["cost"]), str(row.get("vendor") or "").casefold()))
    if mode == "preferred" and preferred_vendor.strip():
        want = preferred_vendor.strip().casefold()
        match = [row for row in ordered if str(row.get("vendor") or "").strip().casefold() == want]
        if match:
            return match[0], _older_note(match[0], usable)
        return ordered[0], "preferred vendor is not on the price list; cheapest used"
    return ordered[0], _older_note(ordered[0], usable)


def _older_note(chosen: dict[str, Any], offers: list[dict[str, Any]]) -> str:
    chosen_date = _row_date(chosen)
    if chosen_date is None:
        return ""
    newer = []
    for row in offers:
        if row is chosen:
            continue
        other = _row_date(row)
        if other is not None and other > chosen_date:
            newer.append(row)
    if not newer:
        return ""
    other = min(newer, key=lambda row: (str(row.get("vendor") or "").casefold(), cents(row["cost"])))
    vendor = str(other.get("vendor") or "another vendor")
    return f"older than {vendor} {other.get('date')}; flagged when it is past the age limit"


def offers_for(prices: list[dict[str, Any]], *numbers: str) -> list[dict[str, Any]]:
    wanted = {norm_catalog(number) for number in numbers if norm_catalog(number)}
    if not wanted:
        return []
    return [row for row in prices if norm_catalog(row.get("catalog")) in wanted]


def age_days(price_date: str, today: date) -> int | None:
    if not price_date:
        return None
    try:
        parsed = parse_price_date(price_date)
    except ValueError:
        return None
    if parsed is None:
        return None
    return (today - parsed).days


def age_label(days: int | None, *, has_price: bool, price_date: str) -> str:
    if not has_price:
        return ""
    if not price_date:
        return "date unknown"
    if days is None:
        return "date unknown"
    if days < 0:
        return "future date"
    if days == 1:
        return "1 day"
    return f"{days} days"
