"""Assemble a quote from the drawing and user prices. Never sends it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from panelcad.core.models import Project
from panelcad.io.bom import grouped_rows
from panelcad.library.catalog import Catalog
from panelcad.library.panel import needs_info_library_id
from panelcad.library.parts import PartsCatalog
from panelcad.quote.labor import (
    CUTOUT_NOTE,
    DISCLAIMER,
    EXAMPLE_LABEL,
    FILES_ONLY,
    PRICE_RULE,
    example_defaults_remaining,
    labor_lines,
    load_user_labor,
    merge_labor,
    resolved_labor,
    write_user_labor,
)
from panelcad.quote.money import cents, format_percent, hours_from_minutes, money_num, parse_number, to_decimal
from panelcad.quote.prices import (
    DATE_NOTE,
    age_days,
    age_label,
    choose_offer,
    norm_catalog,
    offers_for,
    parse_price_date,
)

_COST_KEYS = ("unit_cost", "cost")
_DATE_KEYS = ("price_date", "cost_date")
_VENDOR_KEYS = ("vendor", "supplier")
_LEAD_KEYS = ("lead_time", "lead")
_HEADER_FIELDS = (
    "number",
    "customer",
    "project",
    "currency",
    "price_mode",
    "preferred_vendor",
    "parts_markup_percent",
    "labor_markup_percent",
    "category_markups",
    "tax_percent",
    "freight",
    "contingency_percent",
    "stale_after_days",
)


def read_quote(project: Project) -> dict[str, Any]:
    raw = project.properties.get("quote")
    return raw if isinstance(raw, dict) else {}


def ensure_quote(project: Project) -> dict[str, Any]:
    raw = project.properties.get("quote")
    if not isinstance(raw, dict):
        raw = {}
        project.properties["quote"] = raw
    return raw


def line_key(catalog: str, part_number: str, library_id: str, manufacturer: str) -> str:
    token = norm_catalog(catalog) or norm_catalog(part_number)
    if token:
        return "cat:" + token
    return "lib:" + str(library_id or "").casefold() + "|" + norm_catalog(manufacturer)


def _first_text(props: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key not in props:
            continue
        text = str(props.get(key) or "").strip()
        if text:
            return text
    return ""


def _stored_price(props: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(props, dict):
        return None
    raw = None
    for key in _COST_KEYS:
        if key in props and str(props.get(key) or "").strip() != "":
            raw = props.get(key)
            break
    if raw is None:
        return None
    try:
        cost = parse_number(raw)
    except ValueError:
        return None
    if cost is None or cost < 0:
        return None
    price_date = ""
    raw_date = _first_text(props, _DATE_KEYS)
    if raw_date:
        try:
            parsed = parse_price_date(raw_date)
        except ValueError:
            parsed = None
        price_date = parsed.isoformat() if parsed else ""
    return {
        "unit_cost": money_num(cost),
        "vendor": _first_text(props, _VENDOR_KEYS),
        "lead_time": _first_text(props, _LEAD_KEYS),
        "price_date": price_date,
        "source": "library",
        "note": "",
    }


def _component_price(project: Project, refs: list[str]) -> dict[str, Any] | None:
    wanted = set(refs)
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id not in wanted:
                continue
            found = _stored_price(comp.properties)
            if found is not None:
                return found
    return None


def _library_symbol_price(catalog: Catalog, library_id: str) -> dict[str, Any] | None:
    try:
        device = catalog.get(library_id)
    except KeyError:
        return None
    return _stored_price(device.properties)


def _catalog_part_price(
    parts: PartsCatalog | None,
    number: str,
    manufacturer: str,
) -> dict[str, Any] | None:
    if parts is None or not str(number or "").strip():
        return None
    try:
        part = parts.get(number, manufacturer or None)
    except KeyError:
        try:
            part = parts.get(number)
        except KeyError:
            return None
    if part.unit_cost is None:
        return None
    return {
        "unit_cost": money_num(part.unit_cost),
        "vendor": str(part.vendor or "").strip(),
        "lead_time": str(part.lead_time or "").strip(),
        "price_date": str(part.price_date or "").strip(),
        "source": "library",
        "note": "",
    }


def _manual_price(manual: dict[str, Any], key: str) -> dict[str, Any] | None:
    row = manual.get(key)
    if not isinstance(row, dict) or "unit_cost" not in row:
        return None
    raw = row.get("unit_cost")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        cost = parse_number(raw)
    except ValueError:
        return None
    if cost is None or cost < 0:
        return None
    price_date = str(row.get("price_date") or "").strip()
    if price_date:
        try:
            parsed = parse_price_date(price_date)
        except ValueError:
            parsed = None
        price_date = parsed.isoformat() if parsed else ""
    return {
        "unit_cost": money_num(cost),
        "vendor": str(row.get("vendor") or "").strip(),
        "lead_time": str(row.get("lead_time") or "").strip(),
        "price_date": price_date,
        "source": "manual",
        "note": "",
    }


def _percent(value: object, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    number = to_decimal(value)
    if not number.is_finite():
        raise ValueError("not a finite number")
    return number


def needs_info_label(count: int) -> str:
    """How many needs-info parts were left out. Empty when there are none."""

    if count <= 0:
        return ""
    noun = "part" if count == 1 else "parts"
    return f"{count} {noun} need info (not counted)"


def total_label(unpriced: int, lines: int, needs_info: int = 0) -> str:
    """Priced total, or why it is incomplete.

    Needs-info stays out of the unpriced count. It still makes the quote
    incomplete, with its own sentence.
    """

    if unpriced and needs_info:
        noun = "part" if needs_info == 1 else "parts"
        return (
            f"INCOMPLETE – {unpriced} of {lines} lines unpriced, not included; "
            f"{needs_info} {noun} need info, not included"
        )
    if unpriced:
        return f"INCOMPLETE – {unpriced} of {lines} lines unpriced, not included"
    if needs_info:
        noun = "part" if needs_info == 1 else "parts"
        return f"INCOMPLETE – {needs_info} {noun} need info, not included"
    return "Total"


def part_markup(blob: dict[str, Any], category: str) -> Decimal:
    overrides = blob.get("category_markups")
    if isinstance(overrides, dict) and category in overrides and overrides[category] not in (None, ""):
        return _percent(overrides[category])
    return _percent(blob.get("parts_markup_percent"), "0")


def parts_markup_label(blob: dict[str, Any], percent: Decimal) -> str:
    overrides = blob.get("category_markups")
    if isinstance(overrides, dict) and any(value not in (None, "") for value in overrides.values()):
        return "Parts markup (by category)"
    return f"Parts markup ({format_percent(percent)}%)"


def _header(project: Project, blob: dict[str, Any]) -> dict[str, str]:
    customer = str(blob.get("customer") or "").strip() or str(project.title_block.customer or "").strip()
    name = str(blob.get("project") or "").strip() or str(project.name or "").strip()
    return {
        "number": str(blob.get("number") or "").strip(),
        "customer": customer,
        "project": name,
        "currency": str(blob.get("currency") or "").strip(),
    }


def _price_parts(
    project: Project,
    catalog: Catalog,
    parts: PartsCatalog | None,
    *,
    today: date,
) -> list[dict[str, Any]]:
    blob = read_quote(project)
    manual = blob.get("manual") if isinstance(blob.get("manual"), dict) else {}
    book = blob.get("prices") if isinstance(blob.get("prices"), list) else []
    mode = str(blob.get("price_mode") or "cheapest")
    preferred = str(blob.get("preferred_vendor") or "")
    labor = resolved_labor(project)
    stale_after = blob.get("stale_after_days")
    if stale_after is None or stale_after == "":
        stale_after = labor.get("stale_after_days", 365)
    stale_days = int(stale_after)
    rows: list[dict[str, Any]] = []
    for bom in grouped_rows(project, catalog):
        library_id = str(bom.get("library_id") or "")
        description = " ".join(str(bom.get("description") or "").split()).casefold()
        if needs_info_library_id(library_id) or description == "info needed":
            continue
        refs = [part.strip() for part in str(bom.get("ref") or "").split(",") if part.strip()]
        key = line_key(
            str(bom.get("catalog") or ""),
            str(bom.get("part_number") or ""),
            str(bom.get("library_id") or ""),
            str(bom.get("manufacturer") or ""),
        )
        manual_row = manual.get(key) if isinstance(manual.get(key), dict) else {}
        chosen = _manual_price(manual, key) if _has_manual_cost(manual_row) else None
        note = ""
        if chosen is None:
            offers = offers_for(book, str(bom.get("catalog") or ""), str(bom.get("part_number") or ""))
            offer, note = choose_offer(offers, mode=mode, preferred_vendor=preferred)
            if offer is not None:
                chosen = {
                    "unit_cost": cents(offer["cost"]),
                    "vendor": str(offer.get("vendor") or ""),
                    "lead_time": str(offer.get("lead_time") or ""),
                    "price_date": str(offer.get("date") or ""),
                    "source": "price_list",
                    "note": note,
                }
        if chosen is None:
            chosen = _component_price(project, refs)
        if chosen is None:
            chosen = _library_symbol_price(catalog, str(bom.get("library_id") or ""))
        if chosen is None:
            for number in (str(bom.get("catalog") or ""), str(bom.get("part_number") or "")):
                chosen = _catalog_part_price(parts, number, str(bom.get("manufacturer") or ""))
                if chosen is not None:
                    break
        if chosen is not None and chosen.get("source") != "manual":
            chosen = _overlay_manual(chosen, manual_row)
        qty = int(float(bom.get("qty") or 1))
        category = str(bom.get("category") or "")
        markup = part_markup(blob, category)
        unit = None if chosen is None else cents(chosen["unit_cost"])
        missing = unit is None
        # Extended sell is extended cost × (1 + markup). Unit sell is display-only.
        extended = None if unit is None else cents(Decimal(qty) * unit)
        extended_sell = None if extended is None else cents(extended * (Decimal(1) + markup / Decimal(100)))
        unit_sell = None if extended_sell is None or qty <= 0 else extended_sell / Decimal(qty)
        price_date = "" if chosen is None else str(chosen.get("price_date") or "")
        days = age_days(price_date, today) if not missing else None
        stale = bool(days is not None and days > stale_days)
        if missing:
            flag = "NO PRICE"
        elif stale:
            flag = "STALE"
        elif not price_date:
            flag = "DATE UNKNOWN"
        else:
            flag = ""
        rows.append(
            {
                "item": str(bom.get("item") or ""),
                "key": key,
                "qty": qty,
                "refs": ", ".join(refs),
                "description": str(bom.get("description") or ""),
                "manufacturer": str(bom.get("manufacturer") or ""),
                "catalog": str(bom.get("catalog") or ""),
                "part_number": str(bom.get("part_number") or ""),
                "library_id": str(bom.get("library_id") or ""),
                "category": category,
                "vendor": "" if chosen is None else str(chosen.get("vendor") or ""),
                "lead_time": "" if chosen is None else str(chosen.get("lead_time") or ""),
                "unit_cost": unit,
                "extended_cost": extended,
                "price_date": price_date,
                "age_days": days,
                "age_label": age_label(days, has_price=not missing, price_date=price_date),
                "source": "missing" if chosen is None else str(chosen.get("source") or ""),
                "missing_price": missing,
                "stale": stale,
                "flag": flag,
                "note": "" if chosen is None else str(chosen.get("note") or note),
                "markup_percent": markup,
                "unit_sell": unit_sell,
                "extended_sell": extended_sell,
            }
        )
    return rows


def _needs_info_part_count(project: Project, catalog: Catalog | None = None) -> int:
    """How many needs-info parts stay off the quote. Zero when there are none."""

    from panelcad.needs_info import grouped_needs_info_rows

    total = 0
    for raw in grouped_needs_info_rows(project, catalog=catalog):
        try:
            total += int(float(raw.get("qty") or 0))
        except (TypeError, ValueError):
            continue
    return total


def _has_manual_cost(row: dict[str, Any]) -> bool:
    return "unit_cost" in row and row.get("unit_cost") not in (None, "")


def _overlay_manual(chosen: dict[str, Any], manual_row: dict[str, Any]) -> dict[str, Any]:
    """Apply vendor, lead, or date typed on the grid without freezing the list cost."""

    if not manual_row:
        return chosen
    out = dict(chosen)
    for name in ("vendor", "lead_time", "price_date"):
        if name not in manual_row:
            continue
        text = str(manual_row.get(name) or "").strip()
        if name == "price_date" and text:
            try:
                parsed = parse_price_date(text)
            except ValueError:
                parsed = None
            text = parsed.isoformat() if parsed else ""
        if text:
            out[name] = text
    return out


def _cost_labor(lines: list[dict[str, Any]], labor_markup: Decimal) -> list[dict[str, Any]]:
    priced: list[dict[str, Any]] = []
    for line in lines:
        row = dict(line)
        rate = row.get("rate_per_hour")
        # Price the exact minutes. The 3-decimal hour figure is display only.
        if row.get("minutes") is None:
            minutes = to_decimal(row.get("hours") or 0) * Decimal(60)
        else:
            minutes = to_decimal(row.get("minutes") or 0)
        row["hours"] = hours_from_minutes(minutes)
        row["markup_percent"] = labor_markup
        if rate is None:
            row["cost"] = None
            row["sell"] = None
        else:
            cost = cents(minutes * to_decimal(rate) / Decimal(60))
            row["cost"] = cost
            row["sell"] = cents(cost * (Decimal(1) + labor_markup / Decimal(100)))
        priced.append(row)
    return priced


def _sum(values: list[Any]) -> Decimal:
    total = Decimal(0)
    for value in values:
        if value is not None:
            total += cents(value)
    return cents(total)


def build_quote(
    project: Project,
    catalog: Catalog,
    parts: PartsCatalog | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Price the grouped BOM and estimate labor. Does not write a file or open a socket."""

    day = today or date.today()
    blob = read_quote(project)
    labor_table = resolved_labor(project)
    header = _header(project, blob)
    part_rows = _price_parts(project, catalog, parts, today=day)
    labor_markup = _percent(blob.get("labor_markup_percent"), "0")
    labor = _cost_labor(labor_lines(project, catalog, labor_table), labor_markup)
    parts_cost = _sum([row["extended_cost"] for row in part_rows])
    parts_sell = _sum([row["extended_sell"] for row in part_rows])
    labor_cost = _sum([row["cost"] for row in labor])
    labor_sell = _sum([row["sell"] for row in labor])
    labor_hours = _sum_hours(labor)
    net = cents(parts_sell + labor_sell)
    contingency_pct = _percent(blob.get("contingency_percent"), "0")
    contingency = cents(net * contingency_pct / Decimal(100))
    freight = cents(_percent(blob.get("freight"), "0"))
    tax_pct = _percent(blob.get("tax_percent"), "0")
    pre_tax = cents(net + contingency + freight)
    tax = cents(pre_tax * tax_pct / Decimal(100))
    total = cents(pre_tax + tax)
    unpriced = sum(1 for row in part_rows if row["missing_price"])
    line_count = len(part_rows)
    needs_parts = _needs_info_part_count(project, catalog)
    stale = sum(1 for row in part_rows if row["stale"])
    stale_after = blob.get("stale_after_days")
    if stale_after is None or stale_after == "":
        stale_after = labor_table.get("stale_after_days", 365)
    defaults_left = example_defaults_remaining(labor_table)
    parts_percent = _percent(blob.get("parts_markup_percent"), "0")
    assumptions = [
        PRICE_RULE,
        FILES_ONLY,
        CUTOUT_NOTE,
        DATE_NOTE,
        "Parts follow the grouped bill of materials: one line per catalog part, quantity is the tag count.",
        "A typed quote-grid cost wins. Vendor, lead time, and price date typed on a row overlay the list "
        "without freezing its cost. Then the imported price list (cheapest, or the preferred vendor; "
        "the newest row per vendor), then a price stored on the placed part, the library symbol, or the manufacturer part.",
        "Lines with no price are flagged and left out of the parts total.",
        "Needs-info parts stay off the quote until a library part is chosen. "
        "The total note says how many were left out. They are not unpriced, "
        "and no size is guessed.",
        "Off-page arrows and other drawing marks are not purchasable parts. "
        "They stay off the bill of materials and this quote. No size is guessed for them.",
        f"A price older than {int(stale_after)} days is flagged stale. That limit is an example until you edit it.",
        "Each labor line is priced from its minutes times the shop rate divided by 60, "
        "rounded half up to the cent. Hours shown are minutes divided by 60, rounded half up to "
        "3 decimal places, and are not used to price the line.",
        "Device lines are count times (mount minutes + wire minutes).",
        "Terminations are two per counted wire. Each row of the field cable schedule is its own cable, "
        "and each network or serial link is its own cable. Those conductors are not also a wire or two "
        "terminations. Panel and door sheets are not counted again.",
        "Door devices (operator symbols, door-layout tags, or mount door) are their own line.",
        "Terminal blocks are their own line. Child contacts that share a parent tag are not a second device.",
        "DIN rail and duct lengths are the panel-layout lengths. If the panel has none, other sheets are used.",
        "PLC I/O points are unique module and address pairs from the I/O list.",
        "Parts sell is the extended cost times (1 + markup / 100), rounded half up to the cent. "
        "The unit price is that extended sell divided by the quantity and is not used to add up the quote. "
        "A category markup overrides the parts markup.",
        "Labor sell is each line's cost times (1 + labor markup / 100), rounded half up to the cent, then added.",
        "Contingency is a percent of parts sell plus labor sell. Tax applies after contingency and freight.",
        "The customer file shows sell amounts only.",
    ]
    if defaults_left:
        assumptions.insert(0, f"{EXAMPLE_LABEL}. {DISCLAIMER}")
    return {
        "label": EXAMPLE_LABEL if defaults_left else "",
        "disclaimer": DISCLAIMER,
        "files_only": True,
        "sent": False,
        "prepared": day.isoformat(),
        "number": header["number"],
        "customer": header["customer"],
        "project": header["project"],
        "currency": header["currency"],
        "price_mode": str(blob.get("price_mode") or "cheapest"),
        "preferred_vendor": str(blob.get("preferred_vendor") or ""),
        "stale_after_days": int(stale_after),
        "parts_markup_percent": parts_percent,
        "parts_markup_label": parts_markup_label(blob, parts_percent),
        "labor_markup_percent": labor_markup,
        "tax_percent": tax_pct,
        "freight": freight,
        "contingency_percent": contingency_pct,
        "parts": part_rows,
        "labor": labor,
        "lines": line_count,
        "unpriced": unpriced,
        "needs_info": needs_parts,
        "needs_info_label": needs_info_label(needs_parts),
        "incomplete": unpriced > 0 or needs_parts > 0,
        "total_label": total_label(unpriced, line_count, needs_parts),
        "example_defaults": bool(defaults_left),
        "stale": stale,
        "assumptions": assumptions,
        "totals": {
            "parts_cost": parts_cost,
            "parts_sell": parts_sell,
            "labor_hours": labor_hours,
            "labor_cost": labor_cost,
            "labor_sell": labor_sell,
            "contingency": contingency,
            "freight": freight,
            "tax": tax,
            "total": total,
            "unpriced": unpriced,
            "needs_info": needs_parts,
            "stale": stale,
        },
    }


def _sum_hours(lines: list[dict[str, Any]]) -> Decimal:
    total = Decimal(0)
    for row in lines:
        total += to_decimal(row.get("hours") or 0)
    return total.quantize(Decimal("0.001"))


def _check_percent(name: str, value: object) -> Decimal:
    try:
        number = to_decimal(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if number < -100 or number > 1000:
        raise ValueError(f"{name} must be between -100 and 1000")
    return number


def _stored_number(number: Decimal) -> float:
    return float(format(number, "f"))


def _matches_computed(value: object, computed: object) -> bool:
    left = "" if value is None else str(value).strip()
    right = "" if computed is None else str(computed).strip()
    return left == right


def _put(blob: dict[str, Any], key: str, value: object, *, default: object = None) -> None:
    """Write a header field. Blank or the default clears it. An equal value is left untouched."""

    if isinstance(value, Decimal):
        value = _stored_number(value)
    blank = value is None or (isinstance(value, str) and value.strip() == "")
    if blank or (default is not None and value == default):
        blob.pop(key, None)
        return
    if blob.get(key) == value:
        return
    blob[key] = value


def apply_rates(project: Project, /, **fields: Any) -> dict[str, Any]:
    """Store quote header, markups, and manual prices. Does not send anything."""

    existed = isinstance(project.properties.get("quote"), dict)
    blob = ensure_quote(project)
    unknown = set(fields) - set(_HEADER_FIELDS) - {
        "labor",
        "shop_rates",
        "manual",
        "scope",
        "clear_project_labor",
    }
    if unknown:
        raise ValueError("unknown quote field: " + ", ".join(sorted(unknown)))
    if "price_mode" in fields and fields["price_mode"] not in (None, "", "cheapest", "preferred"):
        raise ValueError("price_mode must be cheapest or preferred")
    for key in ("parts_markup_percent", "labor_markup_percent", "tax_percent", "contingency_percent"):
        if key in fields and fields[key] not in (None, ""):
            fields[key] = _check_percent(key, fields[key])
    if "freight" in fields and fields["freight"] not in (None, ""):
        try:
            freight = to_decimal(fields["freight"])
        except ValueError as exc:
            raise ValueError("freight must be a finite number") from exc
        if freight < 0:
            raise ValueError("freight cannot be negative")
        fields["freight"] = _stored_number(freight)
    if "stale_after_days" in fields and fields["stale_after_days"] not in (None, ""):
        try:
            days = int(to_decimal(fields["stale_after_days"]))
        except ValueError as exc:
            raise ValueError("stale_after_days must be a finite number") from exc
        if days < 0:
            raise ValueError("stale_after_days cannot be negative")
        fields["stale_after_days"] = days
    if "category_markups" in fields and fields["category_markups"] is not None:
        raw = fields["category_markups"]
        if not isinstance(raw, dict):
            raise ValueError("category_markups must be a mapping of category to percent")
        current = blob.get("category_markups") if isinstance(blob.get("category_markups"), dict) else {}
        merged = dict(current)
        for cat, percent in raw.items():
            if percent is None or percent == "":
                merged.pop(str(cat), None)
            else:
                merged[str(cat)] = _stored_number(_check_percent(str(cat), percent))
        if merged == current:
            pass
        elif merged:
            blob["category_markups"] = merged
        else:
            blob.pop("category_markups", None)
    defaults = {
        "price_mode": "cheapest",
        "parts_markup_percent": 0,
        "labor_markup_percent": 0,
        "tax_percent": 0,
        "freight": 0,
        "contingency_percent": 0,
    }
    # A customer or project name that still matches the title block is not stored,
    # so a later title-block edit keeps flowing into the quote.
    if "customer" in fields and _matches_computed(fields.get("customer"), project.title_block.customer):
        fields["customer"] = ""
    if "project" in fields and _matches_computed(fields.get("project"), project.name):
        fields["project"] = ""
    for key in _HEADER_FIELDS:
        if key == "category_markups" or key not in fields:
            continue
        _put(blob, key, fields[key], default=defaults.get(key))
    if "manual" in fields and fields["manual"] is not None:
        _store_manual(blob, fields["manual"])
    labor_patch: dict[str, Any] = {}
    if isinstance(fields.get("labor"), dict):
        labor_patch.update(fields["labor"])
    if fields.get("shop_rates") is not None:
        _validate_shop_rates(fields["shop_rates"])
        labor_patch["shop_rates"] = fields["shop_rates"]
    scope = str(fields.get("scope") or "project")
    if scope not in {"project", "user", "both"}:
        raise ValueError("scope must be project, user, or both")
    if labor_patch and scope in {"user", "both"}:
        # Store the overlay only. Merging onto the example table would copy every
        # placeholder into the shop file and freeze them as if they had been typed.
        current_user = load_user_labor()
        stored = merge_labor(current_user, labor_patch) if current_user else dict(labor_patch)
        write_user_labor(stored)
    if fields.get("clear_project_labor"):
        blob.pop("labor", None)
    elif labor_patch and scope in {"project", "both"}:
        current = blob.get("labor") if isinstance(blob.get("labor"), dict) else {}
        merged = merge_labor(current, labor_patch)
        merged.pop("label", None)
        merged.pop("disclaimer", None)
        blob["labor"] = merged
    if not blob and not existed:
        project.properties.pop("quote", None)
        return {}
    return blob


def _validate_shop_rates(rates: object) -> None:
    if not isinstance(rates, list) or not rates:
        raise ValueError("shop_rates must be a non-empty list")
    seen: set[str] = set()
    for row in rates:
        if not isinstance(row, dict):
            raise ValueError("shop rate must be a mapping")
        rate_id = str(row.get("id") or "").strip()
        if not rate_id:
            raise ValueError("shop rate needs an id")
        if rate_id in seen:
            raise ValueError("shop rate ids must be unique")
        seen.add(rate_id)
        if row.get("per_hour") is None or row.get("per_hour") == "":
            raise ValueError(f"shop rate {rate_id} needs per_hour")
        try:
            per_hour = to_decimal(row["per_hour"])
        except ValueError as exc:
            raise ValueError(f"shop rate {rate_id} must be a finite number") from exc
        if per_hour < 0:
            raise ValueError("shop rate cannot be negative")


def _store_manual(blob: dict[str, Any], incoming: object) -> None:
    if not isinstance(incoming, dict):
        raise ValueError("manual must be a mapping of line key to price fields")
    current = blob.get("manual") if isinstance(blob.get("manual"), dict) else {}
    for key, row in incoming.items():
        if row is None:
            current.pop(str(key), None)
            continue
        if not isinstance(row, dict):
            raise ValueError("manual price must be a mapping")
        stored = dict(current.get(str(key)) or {})
        if "unit_cost" in row:
            if row["unit_cost"] is None or str(row["unit_cost"]).strip() == "":
                stored.pop("unit_cost", None)
                # A date that belongs to the typed cost leaves with it, including
                # one typed later in its own patch. A date entered before any
                # cost still overlays the list price.
                if stored.pop("price_date_defaulted", None) or stored.pop("price_date_with_cost", None):
                    stored.pop("price_date", None)
            else:
                try:
                    cost = parse_number(row["unit_cost"])
                except ValueError as exc:
                    raise ValueError(f"unit_cost {exc}") from exc
                if cost is None or cost < 0:
                    raise ValueError("unit_cost cannot be negative")
                stored["unit_cost"] = money_num(cost)
                if "price_date" not in row and not str(stored.get("price_date") or "").strip():
                    stored["price_date"] = date.today().isoformat()
                    stored["price_date_defaulted"] = True
                    stored.pop("price_date_with_cost", None)
        for name in ("vendor", "lead_time", "price_date"):
            if name not in row:
                continue
            text = str(row.get(name) or "").strip()
            if name == "price_date" and text:
                parsed = parse_price_date(text)
                text = parsed.isoformat() if parsed else ""
            if text:
                stored[name] = text
                if name == "price_date":
                    stored.pop("price_date_defaulted", None)
                    # The grid sends each cell as its own patch. A date typed
                    # while a manual cost is already stored still belongs to
                    # that cost, the same as a date sent in the same patch.
                    cost_text = row.get("unit_cost")
                    incoming_cost = (
                        "unit_cost" in row
                        and cost_text is not None
                        and str(cost_text).strip() != ""
                    )
                    stored_cost = stored.get("unit_cost") not in (None, "")
                    if incoming_cost or stored_cost:
                        stored["price_date_with_cost"] = True
                    else:
                        stored.pop("price_date_with_cost", None)
            else:
                stored.pop(name, None)
                if name == "price_date":
                    stored.pop("price_date_defaulted", None)
                    stored.pop("price_date_with_cost", None)
        if stored:
            current[str(key)] = stored
        else:
            current.pop(str(key), None)
    blob["manual"] = current
