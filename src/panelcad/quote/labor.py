"""Labor hours from the drawing, using an editable shop table.

The numbers shipped in ``example_labor_table`` are placeholders so a quote can
be added up before the shop types its own minutes. They are not taken from
NECA, RSMeans, or any other labor standard. A quote labels a value
"example defaults, edit these" only while that value is still the placeholder.
"""

from __future__ import annotations

import os
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import yaml

from panelcad.core.cables import cable_rows
from panelcad.core.electrical import family_key, io_rows, is_drawing_only_mark, symbol_role
from panelcad.core.jumper import is_jumper_device
from panelcad.core.models import Component, Device, Project, Sheet
from panelcad.core.wire_numbering import is_comms_wire
from panelcad.library.catalog import Catalog
from panelcad.library.panel import effective_footprint, is_info_needed_placeholder
from panelcad.quote.money import hours_from_minutes, to_decimal

EXAMPLE_LABEL = "example defaults, edit these"
DISCLAIMER = (
    "These minutes, hours, and shop rates are example placeholders so a quote "
    "can be calculated. They are not industry standards, union agreements, or "
    "published labor units. Edit them for your shop."
)
FILES_ONLY = "A quote is written to a file on this computer. PanelCAD does not send it."
PRICE_RULE = (
    "Part prices come only from a price list you import, a price stored on a "
    "library part, or a value you type in the quote grid. PanelCAD does not "
    "invent or look up prices."
)
CUTOUT_NOTE = "Cutouts are not counted yet; enter minutes per cutout."

# Example minutes only. See EXAMPLE_LABEL. Not a published labor table.
_EXAMPLE_DEVICE_MINUTES: dict[str, dict[str, float]] = {
    "protection.breaker": {"mount": 15, "wire": 10},
    "protection.mpcb": {"mount": 15, "wire": 10},
    "protection.fuse": {"mount": 8, "wire": 6},
    "protection.overload": {"mount": 12, "wire": 8},
    "protection.disconnect": {"mount": 20, "wire": 12},
    "switching.contactor": {"mount": 12, "wire": 15},
    "switching.relay": {"mount": 8, "wire": 10},
    "switching.safety": {"mount": 20, "wire": 15},
    "switching.aux": {"mount": 5, "wire": 5},
    "timing.timer": {"mount": 8, "wire": 8},
    "power.supply": {"mount": 15, "wire": 10},
    "power.transformer": {"mount": 25, "wire": 15},
    "power.receptacle": {"mount": 15, "wire": 8},
    "power.load": {"mount": 10, "wire": 8},
    "power.device": {"mount": 10, "wire": 8},
    "control.plc": {"mount": 30, "wire": 20},
    "control.hmi": {"mount": 25, "wire": 10},
    "drive.vfd": {"mount": 30, "wire": 20},
    "drive.softstarter": {"mount": 30, "wire": 20},
    "load.motor": {"mount": 0, "wire": 20},
    "load.solenoid": {"mount": 0, "wire": 10},
    "connection.network": {"mount": 10, "wire": 8},
    "connection.accessory": {"mount": 3, "wire": 0},
    "connection.ground": {"mount": 8, "wire": 5},
    "connection.bus": {"mount": 10, "wire": 6},
    "default": {"mount": 10, "wire": 8},
}

_LAYOUT_KINDS = {"panel_layout", "door_layout"}

CutoutCounter = Callable[[Project, Catalog], int]
_cutout_counter: CutoutCounter | None = None


def set_cutout_counter(fn: CutoutCounter | None) -> None:
    """Register a cutout count when that feature lands. Until then the count is 0."""

    global _cutout_counter
    _cutout_counter = fn


def count_cutouts(project: Project, catalog: Catalog) -> int:
    if _cutout_counter is None:
        return 0
    try:
        value = int(_cutout_counter(project, catalog))
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def example_labor_table() -> dict[str, Any]:
    """A fresh copy of the shipped placeholders. Not a shop's real rates."""

    return {
        "label": EXAMPLE_LABEL,
        "disclaimer": DISCLAIMER,
        "currency": "",
        "stale_after_days": 365,
        "wire_minutes": 3,
        "termination_minutes": 2,
        "cable_minutes": 10,
        "terminal_block_minutes": 4,
        "duct_minutes_per_m": 12,
        "din_minutes_per_m": 8,
        "door_mount_minutes": 10,
        "door_wire_minutes": 6,
        "cutout_minutes": 8,
        "plc_io_minutes": 3,
        "device_minutes": deepcopy(_EXAMPLE_DEVICE_MINUTES),
        "fixed": {
            "engineering": {
                "description": "Engineering",
                "hours": 4,
                "rate": "engineering",
            },
            "programming": {
                "description": "Programming",
                "hours": 6,
                "rate": "engineering",
            },
            "testing_fat": {
                "description": "Testing / FAT",
                "hours": 4,
                "rate": "shop",
            },
            "documentation": {
                "description": "Documentation",
                "hours": 2,
                "rate": "engineering",
            },
        },
        "shop_rates": [
            {"id": "shop", "name": "Shop labor", "per_hour": 75},
            {"id": "engineering", "name": "Engineering", "per_hour": 110},
        ],
    }


def quote_dir() -> Path:
    override = os.environ.get("PANELCAD_QUOTE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".panelcad" / "quote"


def user_labor_path() -> Path:
    return quote_dir() / "labor.yaml"


def merge_labor(base: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, Any]:
    """Deep-merge a labor overlay onto a base table. The example label stays."""

    out = deepcopy(base)
    if not overlay:
        out["label"] = EXAMPLE_LABEL
        out["disclaimer"] = DISCLAIMER
        return out
    for key, value in overlay.items():
        if key in {"label", "disclaimer"}:
            continue
        if key == "device_minutes" and isinstance(value, dict):
            bucket = out.setdefault("device_minutes", {})
            for cat, mins in value.items():
                if isinstance(mins, dict) and isinstance(bucket.get(cat), dict):
                    bucket[cat] = {**bucket[cat], **mins}
                else:
                    bucket[cat] = deepcopy(mins)
        elif key == "fixed" and isinstance(value, dict):
            bucket = out.setdefault("fixed", {})
            for name, row in value.items():
                if isinstance(row, dict) and isinstance(bucket.get(name), dict):
                    bucket[name] = {**bucket[name], **row}
                else:
                    bucket[name] = deepcopy(row)
        elif key == "shop_rates" and isinstance(value, list):
            out["shop_rates"] = deepcopy(value)
        else:
            out[key] = deepcopy(value)
    out["label"] = EXAMPLE_LABEL
    out["disclaimer"] = DISCLAIMER
    return out


def load_user_labor() -> dict[str, Any]:
    path = user_labor_path()
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a labor table")
    return raw


def write_user_labor(table: dict[str, Any]) -> Path:
    """Save a shop labor table. The example label is written back onto the file."""

    path = user_labor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = deepcopy(table)
    data["label"] = EXAMPLE_LABEL
    data["disclaimer"] = DISCLAIMER
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def project_labor_overlay(project: Project) -> dict[str, Any]:
    raw = project.properties.get("quote")
    if not isinstance(raw, dict):
        return {}
    labor = raw.get("labor")
    return labor if isinstance(labor, dict) else {}


def resolved_labor(project: Project) -> dict[str, Any]:
    """Example defaults, then the user file, then this project's overrides."""

    table = merge_labor(example_labor_table(), load_user_labor())
    return merge_labor(table, project_labor_overlay(project))


def _device(catalog: Catalog, comp: Component) -> Device | None:
    try:
        return catalog.get(comp.library_id)
    except KeyError:
        return None


def _is_terminal(device: Device) -> bool:
    return symbol_role(device) == "terminal" or (device.category or "").startswith("connection.terminal")


def _is_door(items: list[tuple[Sheet, Component, Device]], device: Device, comp: Component) -> bool:
    if (device.category or "").startswith("operator."):
        return True
    if any(sheet.kind == "door_layout" for sheet, _comp, _device in items):
        return True
    return effective_footprint(device, comp).mount == "door"


def _prefer_body(items: list[tuple[Sheet, Component, Device]]) -> tuple[Sheet, Component, Device]:
    for item in items:
        if item[0].kind not in _LAYOUT_KINDS:
            return item
    return items[0]


def _sum_length(project: Project, kind: str) -> float:
    """Panel-layout length when the panel has any. Otherwise every sheet."""

    panel = 0.0
    panel_n = 0
    other = 0.0
    for sheet in project.sheets:
        rows = sheet.rails if kind == "rail" else sheet.trays
        total = sum(float(row.length_mm) for row in rows)
        if sheet.kind == "panel_layout":
            panel += total
            panel_n += len(rows)
        else:
            other += total
    if panel_n:
        return panel
    return panel + other


def drawing_counts(project: Project, catalog: Catalog) -> dict[str, Any]:
    """Count labor drivers off the drawing. Each tag is classified once."""

    by_ref: dict[str, list[tuple[Sheet, Component, Device]]] = {}
    skipped_other: list[str] = []
    for sheet in project.sheets:
        for comp in sheet.components:
            device = _device(catalog, comp)
            if is_info_needed_placeholder(comp, device):
                if comp.id not in skipped_other and comp.id not in by_ref:
                    skipped_other.append(comp.id)
                continue
            if device is None or is_jumper_device(device) or is_drawing_only_mark(device, comp):
                if comp.id not in skipped_other and comp.id not in by_ref:
                    skipped_other.append(comp.id)
                continue
            if comp.id in skipped_other:
                skipped_other.remove(comp.id)
            by_ref.setdefault(comp.id, []).append((sheet, comp, device))

    devices: dict[str, list[str]] = {}
    door_refs: list[str] = []
    terminal_refs: list[str] = []
    skipped_child: list[str] = []
    for ref, items in by_ref.items():
        _sheet, comp, device = _prefer_body(items)
        role = symbol_role(device)
        if role == "contact":
            parent = family_key(comp, device)
            if parent and parent != ref and parent in by_ref:
                skipped_child.append(ref)
                continue
        if _is_terminal(device):
            terminal_refs.append(ref)
            continue
        if _is_door(items, device, comp):
            door_refs.append(ref)
            continue
        cat = device.category or "other"
        devices.setdefault(cat, []).append(ref)

    # One field-cable schedule row is one cable. A network or serial link that
    # is not already a conductor on that schedule is also one cable. Neither is
    # a wire, and neither adds two terminations.
    schedule = cable_rows(project)
    covered = _schedule_conductors(project)
    wires = 0
    network_links = 0
    for sheet in project.sheets:
        if sheet.kind in _LAYOUT_KINDS:
            continue
        for wire in sheet.wires:
            if (sheet.id, wire.id) in covered:
                continue
            if is_comms_wire(sheet, wire, catalog):
                network_links += 1
                continue
            wires += 1
    points = {(row["module"], row["address"]) for row in io_rows(project, catalog)}
    return {
        "devices": {cat: list(refs) for cat, refs in sorted(devices.items())},
        "door_refs": door_refs,
        "terminal_refs": terminal_refs,
        "skipped_child": skipped_child,
        "skipped_other": skipped_other,
        "wires": wires,
        "cables": len(schedule) + network_links,
        "network_links": network_links,
        "terminations": wires * 2,
        "duct_mm": _sum_length(project, "tray"),
        "din_rail_mm": _sum_length(project, "rail"),
        "cutouts": count_cutouts(project, catalog),
        "plc_io_points": len(points),
    }


def _schedule_conductors(project: Project) -> set[tuple[str, str]]:
    """(sheet id, wire id) for conductors that already belong to the cable schedule.

    An assignment with a sheet matches that sheet. An assignment with a wire id
    and no sheet matches that wire id wherever it is drawn. A ``wire.cable``
    marker is the older schedule grouping and is the same exclusion.
    """

    covered: set[tuple[str, str]] = set()
    anywhere: set[str] = set()
    for cable in project.cables:
        for land in cable.assignments:
            wire_id = str(land.wire_id or "").strip()
            if not wire_id:
                continue
            sheet_id = str(land.sheet or "").strip()
            if sheet_id:
                covered.add((sheet_id, wire_id))
            else:
                anywhere.add(wire_id)
    for sheet in project.sheets:
        for wire in sheet.wires:
            if str(wire.cable or "").strip() or wire.id in anywhere:
                covered.add((sheet.id, wire.id))
    return covered


def _fmt_min(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}"


def _hours(minutes: float) -> Decimal:
    return hours_from_minutes(minutes)


def count_phrase(count: float, singular: str, plural: str | None = None) -> str:
    """'1 device', '2 devices'. The number is not a rate."""

    word = singular if Decimal(str(count)) == 1 else (plural or singular + "s")
    if abs(float(count) - round(float(count))) < 1e-6:
        shown = str(int(round(float(count))))
    else:
        shown = f"{float(count):g}"
    return f"{shown} {word}"


# Readable names for category ids already in the library. Not a trade-name list.
_CATEGORY_NAMES = {
    "connection.accessory": "accessory",
    "connection.bus": "bus bar",
    "connection.ground": "ground",
    "connection.network": "network device",
    "control.hmi": "HMI",
    "control.plc": "PLC",
    "drive.softstarter": "soft starter",
    "drive.vfd": "VFD",
    "load.motor": "motor",
    "load.solenoid": "solenoid",
    "operator.estop": "emergency stop",
    "operator.light": "pilot light",
    "operator.limit": "limit switch",
    "operator.pushbutton": "pushbutton",
    "operator.selector": "selector switch",
    "power.device": "power device",
    "power.load": "power load",
    "power.receptacle": "receptacle",
    "power.supply": "power supply",
    "power.transformer": "transformer",
    "protection.breaker": "breaker",
    "protection.disconnect": "disconnect",
    "protection.fuse": "fuse",
    "protection.mpcb": "motor protector",
    "protection.overload": "overload",
    "switching.aux": "auxiliary contact",
    "switching.contactor": "contactor",
    "switching.relay": "relay",
    "switching.safety": "safety relay",
    "timing.timer": "timer",
}


def category_name(category: str) -> str:
    if category in _CATEGORY_NAMES:
        return _CATEGORY_NAMES[category]
    return str(category or "device").split(".")[-1].replace("_", " ")


def _same(left: object, right: object) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not isinstance(left, bool) and not isinstance(right, bool):
        return abs(float(left) - float(right)) < 1e-9
    return left == right


def uses_example_minutes(table: dict[str, Any], line_id: str) -> bool:
    """True when this line's minutes or fixed hours are still the shipped placeholders."""

    example = example_labor_table()
    if line_id.startswith("device:"):
        cat = line_id.split(":", 1)[1]
        current = (table.get("device_minutes") or {}).get(cat)
        shipped = (example.get("device_minutes") or {}).get(cat)
        if shipped is None:
            current = current or (table.get("device_minutes") or {}).get("default")
            shipped = (example.get("device_minutes") or {}).get("default")
        return _same(current, shipped)
    direct = {
        "wires": "wire_minutes",
        "terminations": "termination_minutes",
        "cables": "cable_minutes",
        "terminals": "terminal_block_minutes",
        "duct": "duct_minutes_per_m",
        "din": "din_minutes_per_m",
        "cutouts": "cutout_minutes",
        "plc_io": "plc_io_minutes",
    }
    if line_id in direct:
        key = direct[line_id]
        return _same(table.get(key), example.get(key))
    if line_id == "door":
        return _same(table.get("door_mount_minutes"), example.get("door_mount_minutes")) and _same(
            table.get("door_wire_minutes"), example.get("door_wire_minutes")
        )
    if line_id.startswith("fixed:"):
        key = line_id.split(":", 1)[1]
        current = ((table.get("fixed") or {}).get(key) or {}).get("hours")
        shipped = ((example.get("fixed") or {}).get(key) or {}).get("hours")
        return _same(current, shipped)
    if line_id.startswith("rate:"):
        rate_id = line_id.split(":", 1)[1]
        shipped_rates = {str(row.get("id")): row.get("per_hour") for row in example.get("shop_rates") or []}
        if rate_id not in shipped_rates:
            return False
        for row in table.get("shop_rates") or []:
            if str(row.get("id")) == rate_id:
                return _same(row.get("per_hour"), shipped_rates[rate_id])
        return False
    return False


def example_defaults_remaining(table: dict[str, Any]) -> list[str]:
    """Ids of labor inputs that still equal the shipped placeholders."""

    example = example_labor_table()
    found: list[str] = []
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
        "stale_after_days",
    ):
        if _same(table.get(key), example.get(key)):
            found.append(key)
    for cat, shipped in (example.get("device_minutes") or {}).items():
        if _same((table.get("device_minutes") or {}).get(cat), shipped):
            found.append(f"device:{cat}")
    for key, shipped in (example.get("fixed") or {}).items():
        current = (table.get("fixed") or {}).get(key) or {}
        if _same(current.get("hours"), shipped.get("hours")):
            found.append(f"fixed:{key}")
    shipped_rates = {str(row.get("id")): row.get("per_hour") for row in example.get("shop_rates") or []}
    for row in table.get("shop_rates") or []:
        rate_id = str(row.get("id"))
        if rate_id in shipped_rates and _same(row.get("per_hour"), shipped_rates[rate_id]):
            found.append(f"rate:{rate_id}")
    return found


def labor_overrides(table: dict[str, Any]) -> dict[str, Any]:
    """Values that differ from the shipped placeholders. Unchanged example numbers are left out."""

    example = example_labor_table()
    out: dict[str, Any] = {}
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
        "stale_after_days",
        "currency",
    ):
        if key in table and not _same(table.get(key), example.get(key)):
            out[key] = deepcopy(table[key])
    minutes = {}
    for cat, row in (table.get("device_minutes") or {}).items():
        if not _same(row, (example.get("device_minutes") or {}).get(cat)):
            minutes[cat] = deepcopy(row)
    if minutes:
        out["device_minutes"] = minutes
    fixed = {}
    for key, row in (table.get("fixed") or {}).items():
        shipped = (example.get("fixed") or {}).get(key) or {}
        if not isinstance(row, dict):
            continue
        changed = {
            name: value
            for name, value in row.items()
            if name not in {"description"} and not _same(value, shipped.get(name))
        }
        if changed:
            fixed[key] = changed
    if fixed:
        out["fixed"] = fixed
    if not _same(table.get("shop_rates"), example.get("shop_rates")):
        out["shop_rates"] = deepcopy(table.get("shop_rates") or [])
    return out


def device_minutes_for(table: dict[str, Any], category: str) -> tuple[float, float, str]:
    bucket = table.get("device_minutes") or {}
    row = bucket.get(category)
    if isinstance(row, dict):
        return float(row.get("mount") or 0), float(row.get("wire") or 0), category
    default = bucket.get("default") if isinstance(bucket.get("default"), dict) else {}
    return float(default.get("mount") or 0), float(default.get("wire") or 0), "default"


def rate_per_hour(table: dict[str, Any], rate_id: str) -> float | None:
    for row in table.get("shop_rates") or []:
        if str(row.get("id") or "") == rate_id:
            try:
                return float(row.get("per_hour"))
            except (TypeError, ValueError):
                return None
    rates = list(table.get("shop_rates") or [])
    if rates:
        try:
            return float(rates[0].get("per_hour"))
        except (TypeError, ValueError):
            return None
    return None


def _line(
    *,
    line_id: str,
    section: str,
    description: str,
    count: float,
    unit: str,
    minutes: float,
    hours: float,
    rate_id: str,
    rate: float | None,
    breakdown: str,
) -> dict[str, Any]:
    return {
        "id": line_id,
        "section": section,
        "description": description,
        "count": count,
        "unit": unit,
        "minutes": minutes,
        "hours": hours,
        "rate_id": rate_id,
        "rate_per_hour": rate,
        "breakdown": breakdown,
    }


def labor_lines(project: Project, catalog: Catalog, table: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One row per labor driver, with the arithmetic in ``breakdown``."""

    table = table or resolved_labor(project)
    counts = drawing_counts(project, catalog)
    shop = rate_per_hour(table, "shop")
    lines: list[dict[str, Any]] = []
    for cat, refs in counts["devices"].items():
        mount, wire, used = device_minutes_for(table, cat)
        each = mount + wire
        total_min = len(refs) * each
        name = category_name(cat)
        lines.append(
            _line(
                line_id=f"device:{cat}",
                section="device",
                description=f"Mount and wire {name}",
                count=len(refs),
                unit="device",
                minutes=total_min,
                hours=_hours(total_min),
                rate_id="shop",
                rate=shop,
                breakdown=(
                    f"{count_phrase(len(refs), 'device')} × ({_fmt_min(mount)} min mount + {_fmt_min(wire)} min wire)"
                    f" = {_fmt_min(total_min)} min = {_hours(total_min):.3f} h"
                    f" ({cat})"
                ),
            )
        )

    door_n = len(counts["door_refs"])
    door_each = float(table.get("door_mount_minutes") or 0) + float(table.get("door_wire_minutes") or 0)
    door_min = door_n * door_each
    lines.append(
        _line(
            line_id="door",
            section="door",
            description="Door devices",
            count=door_n,
            unit="device",
            minutes=door_min,
            hours=_hours(door_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{count_phrase(door_n, 'device')} × ({_fmt_min(float(table.get('door_mount_minutes') or 0))} min mount"
                f" + {_fmt_min(float(table.get('door_wire_minutes') or 0))} min wire)"
                f" = {_fmt_min(door_min)} min = {_hours(door_min):.3f} h"
            ),
        )
    )

    wire_n = int(counts["wires"])
    wire_each = float(table.get("wire_minutes") or 0)
    wire_min = wire_n * wire_each
    lines.append(
        _line(
            line_id="wires",
            section="wire",
            description="Wires",
            count=wire_n,
            unit="wire",
            minutes=wire_min,
            hours=_hours(wire_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{count_phrase(wire_n, 'wire')} × {_fmt_min(wire_each)} min"
                f" = {_fmt_min(wire_min)} min = {_hours(wire_min):.3f} h"
                " (schematic and I/O sheets; field-cable conductors and network or serial"
                " links are a separate line; panel and door sheets are not counted again)"
            ),
        )
    )

    cable_n = int(counts.get("cables") or 0)
    cable_each = float(table.get("cable_minutes") or 0)
    cable_min = cable_n * cable_each
    lines.append(
        _line(
            line_id="cables",
            section="cable",
            description="Cables",
            count=cable_n,
            unit="cable",
            minutes=cable_min,
            hours=_hours(cable_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{count_phrase(cable_n, 'cable')} × {_fmt_min(cable_each)} min"
                f" = {_fmt_min(cable_min)} min = {_hours(cable_min):.3f} h"
                " (field cable schedule, one cable per row and not per conductor,"
                " plus each network or serial link;"
                " not counted as wires or as two terminations)"
            ),
        )
    )

    term_each = float(table.get("termination_minutes") or 0)
    ends = int(counts["terminations"])
    term_min = ends * term_each
    lines.append(
        _line(
            line_id="terminations",
            section="wire",
            description="Terminations (both ends)",
            count=ends,
            unit="end",
            minutes=term_min,
            hours=_hours(term_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{count_phrase(wire_n, 'wire')} × 2 ends × {_fmt_min(term_each)} min"
                f" = {_fmt_min(term_min)} min = {_hours(term_min):.3f} h"
            ),
        )
    )

    blocks = len(counts["terminal_refs"])
    block_each = float(table.get("terminal_block_minutes") or 0)
    block_min = blocks * block_each
    lines.append(
        _line(
            line_id="terminals",
            section="terminal",
            description="Terminal blocks",
            count=blocks,
            unit="block",
            minutes=block_min,
            hours=_hours(block_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{count_phrase(blocks, 'block')} × {_fmt_min(block_each)} min"
                f" = {_fmt_min(block_min)} min = {_hours(block_min):.3f} h"
            ),
        )
    )

    duct_mm = float(counts["duct_mm"])
    duct_rate = float(table.get("duct_minutes_per_m") or 0)
    duct_min = (duct_mm / 1000.0) * duct_rate
    lines.append(
        _line(
            line_id="duct",
            section="duct",
            description="Wire duct",
            count=duct_mm / 1000.0,
            unit="m",
            minutes=duct_min,
            hours=_hours(duct_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{duct_mm:.0f} mm = {duct_mm / 1000.0:.3f} m × {_fmt_min(duct_rate)} min/m"
                f" = {_fmt_min(duct_min)} min = {_hours(duct_min):.3f} h"
            ),
        )
    )

    rail_mm = float(counts["din_rail_mm"])
    rail_rate = float(table.get("din_minutes_per_m") or 0)
    rail_min = (rail_mm / 1000.0) * rail_rate
    lines.append(
        _line(
            line_id="din",
            section="din",
            description="DIN rail",
            count=rail_mm / 1000.0,
            unit="m",
            minutes=rail_min,
            hours=_hours(rail_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{rail_mm:.0f} mm = {rail_mm / 1000.0:.3f} m × {_fmt_min(rail_rate)} min/m"
                f" = {_fmt_min(rail_min)} min = {_hours(rail_min):.3f} h"
            ),
        )
    )

    cut_n = int(counts["cutouts"])
    cut_each = float(table.get("cutout_minutes") or 0)
    cut_min = cut_n * cut_each
    lines.append(
        _line(
            line_id="cutouts",
            section="cutout",
            description="Cutouts",
            count=cut_n,
            unit="cutout",
            minutes=cut_min,
            hours=_hours(cut_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{count_phrase(cut_n, 'cutout')} × {_fmt_min(cut_each)} min"
                f" = {_fmt_min(cut_min)} min = {_hours(cut_min):.3f} h. {CUTOUT_NOTE}"
            ),
        )
    )

    io_n = int(counts["plc_io_points"])
    io_each = float(table.get("plc_io_minutes") or 0)
    io_min = io_n * io_each
    lines.append(
        _line(
            line_id="plc_io",
            section="io",
            description="PLC I/O points",
            count=io_n,
            unit="point",
            minutes=io_min,
            hours=_hours(io_min),
            rate_id="shop",
            rate=shop,
            breakdown=(
                f"{count_phrase(io_n, 'point')} × {_fmt_min(io_each)} min"
                f" = {_fmt_min(io_min)} min = {_hours(io_min):.3f} h"
                " (unique module and address on the I/O list)"
            ),
        )
    )

    fixed = table.get("fixed") if isinstance(table.get("fixed"), dict) else {}
    for key in ("engineering", "programming", "testing_fat", "documentation"):
        row = fixed.get(key) if isinstance(fixed.get(key), dict) else {}
        hours = float(row.get("hours") or 0)
        rate_id = str(row.get("rate") or "shop")
        name = str(row.get("description") or key)
        lines.append(
            _line(
                line_id=f"fixed:{key}",
                section="fixed",
                description=name,
                count=1,
                unit="lot",
                minutes=hours * 60.0,
                hours=hours,
                rate_id=rate_id,
                rate=rate_per_hour(table, rate_id),
                breakdown=f"fixed line {hours:.2f} h",
            )
        )
    for line in lines:
        line["example_default"] = uses_example_minutes(table, str(line["id"]))
        if line["example_default"]:
            line["breakdown"] = f"{line['breakdown']} — example default"
    return lines


def labor_form_rows(table: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat editor rows: value is the first number, value2 is wire minutes when used."""

    rows: list[dict[str, Any]] = []
    for rate in table.get("shop_rates") or []:
        rows.append(
            {
                "id": f"rate:{rate.get('id')}",
                "label": f"Shop rate — {rate.get('name') or rate.get('id')} ({rate.get('id')})",
                "kind": "rate",
                "value": rate.get("per_hour"),
                "value2": None,
                "unit": "per hour",
            }
        )
    singles = (
        ("wire_minutes", "Minutes per wire", "minutes"),
        ("termination_minutes", "Minutes per termination", "minutes"),
        ("cable_minutes", "Minutes per field cable", "minutes"),
        ("terminal_block_minutes", "Minutes per terminal block", "minutes"),
        ("duct_minutes_per_m", "Duct minutes per metre", "min/m"),
        ("din_minutes_per_m", "DIN rail minutes per metre", "min/m"),
        ("door_mount_minutes", "Door device mount minutes", "minutes"),
        ("door_wire_minutes", "Door device wire minutes", "minutes"),
        ("cutout_minutes", "Minutes per cutout", "minutes"),
        ("plc_io_minutes", "Minutes per PLC I/O point", "minutes"),
        ("stale_after_days", "Flag a price older than", "days"),
    )
    for key, label, unit in singles:
        rows.append(
            {
                "id": key,
                "label": label,
                "kind": "number",
                "value": table.get(key),
                "value2": None,
                "unit": unit,
            }
        )
    fixed = table.get("fixed") if isinstance(table.get("fixed"), dict) else {}
    for key in ("engineering", "programming", "testing_fat", "documentation"):
        row = fixed.get(key) if isinstance(fixed.get(key), dict) else {}
        rows.append(
            {
                "id": f"fixed:{key}",
                "label": f"{row.get('description') or key} hours",
                "kind": "fixed",
                "value": row.get("hours"),
                "value2": row.get("rate") or "shop",
                "unit": "hours",
            }
        )
    minutes = table.get("device_minutes") if isinstance(table.get("device_minutes"), dict) else {}
    names = sorted(key for key in minutes if key != "default")
    if "default" in minutes:
        names.append("default")
    for cat in names:
        row = minutes.get(cat) if isinstance(minutes.get(cat), dict) else {}
        label = "Other categories (default)" if cat == "default" else cat
        rows.append(
            {
                "id": f"device:{cat}",
                "label": label,
                "kind": "device",
                "value": row.get("mount"),
                "value2": row.get("wire"),
                "unit": "mount / wire min",
            }
        )
    return rows


def _finite(value: Any, name: str) -> float:
    try:
        number = to_decimal(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if number < 0:
        raise ValueError(f"{name} cannot be negative")
    return float(number)


def labor_patch(row_id: str, value: Any, value2: Any = None, table: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn one editor edit into a labor overlay. Shop-rate edits replace the rate list."""

    if row_id.startswith("rate:"):
        rate_id = row_id.split(":", 1)[1]
        rates = deepcopy((table or example_labor_table()).get("shop_rates") or [])
        found = False
        for row in rates:
            if str(row.get("id")) == rate_id:
                row["per_hour"] = _finite(value, rate_id)
                found = True
        if not found:
            raise ValueError(f"unknown shop rate '{rate_id}'")
        return {"shop_rates": rates}
    if row_id.startswith("fixed:"):
        key = row_id.split(":", 1)[1]
        return {"fixed": {key: {"hours": _finite(value, key)}}}
    if row_id.startswith("device:"):
        cat = row_id.split(":", 1)[1]
        return {"device_minutes": {cat: {"mount": _finite(value, cat), "wire": _finite(value2 or 0, cat)}}}
    if row_id == "stale_after_days":
        return {"stale_after_days": int(_finite(value, row_id))}
    return {row_id: _finite(value, row_id)}
