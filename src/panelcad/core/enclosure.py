"""Shop-style enclosure auto-size: DIN rows, duct, spare space, heat, standard box."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from panelcad.core.layout import (
    DIN_CLIP_FROM_TOP,
    DOOR_HEADING_MM,
    clip_from_body_top,
    DOOR_MARGIN_MM,
    DOOR_PITCH_Y_MM,
    MODULE_GAP_MM,
    RAIL_HEIGHT_MM,
    backplate_sheet_bounds,
    door_row_pitch,
    footprint_size,
    legend_plate_rect,
    rail_aabb,
    tray_aabb,
)
from panelcad.core.models import Component, Device, Enclosure, Project, Rail, Sheet, WireTray
from panelcad.core.refs import sheet_drawing_standard
from panelcad.library.catalog import Catalog, normalize_standard
from panelcad.library.enclosures import (
    INCH_MM,
    EnclosureLibrary,
    StandardEnclosure,
    load_enclosures,
)
from panelcad.library.panel import (
    assigned_catalog_ready,
    effective_footprint,
    instance_overrides_catalog_size,
    is_child_contact,
    is_incabinet,
    is_layout_ready,
    library_catalog_ready,
    occupies_panel_layout,
)

PartRole = Literal["din", "terminal", "block", "door", "skip"]

WATT_KEYS = ("watts", "Pd", "pd", "heat_w", "heat", "dissipation", "power_loss", "loss_w")
HP_KEYS = ("hp", "HP", "horsepower")
KW_KEYS = ("kw", "kW", "Pn", "pn", "power_kw", "rating_kw")
VA_KEYS = ("VA", "va", "rating_va")
VFD_EFFICIENCY_DEFAULT = 0.97
PSU_EFFICIENCY_DEFAULT = 0.88
TRANSFORMER_EFFICIENCY_DEFAULT = 0.95

_NUM_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)")
_KW_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*kW\b", re.I)
_W_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*W\b", re.I)
_HP_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*h(?:p|orse)", re.I)
_VA_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*VA\b", re.I)
_VOLT_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*V", re.I)
_AMP_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)\s*A\b", re.I)

# NEMA 4 / 4X satisfy a Type 12 indoor spec; Type 1 does not.
_NEMA_COVERS: dict[str, frozenset[str]] = {
    "1": frozenset({"1", "12", "4", "4X"}),
    "12": frozenset({"12", "4", "4X"}),
    "4": frozenset({"4", "4X"}),
    "4X": frozenset({"4X"}),
}


@dataclass
class SizingOptions:
    spare_pct: float = 0.25
    duct_face_in: float = 2.0
    duct_depth_in: float = 3.0
    rail_to_duct_in: float = 1.0
    wall_gutter_in: float = 0.5
    airflow_gap_in: float = 1.0
    side_ducts: bool = True
    ambient_c: float = 40.0
    max_internal_c: float = 50.0
    vfd_efficiency: float = VFD_EFFICIENCY_DEFAULT
    psu_efficiency: float = PSU_EFFICIENCY_DEFAULT
    transformer_efficiency: float = TRANSFORMER_EFFICIENCY_DEFAULT
    heat_k_steel: float = 5.5
    heat_k_stainless: float = 4.5
    heat_k_nonmetallic: float = 3.5
    nema: str = "12"
    material: str = "steel"
    mount: str = "any"
    # Power stays left and control (timer, relay, PSU, PLC) stays right on the
    # same rail. A separate control rail is only for when this is set, or when
    # the power rail has no room left.
    separate_control_rail: bool = False

    @property
    def duct_face_mm(self) -> float:
        return self.duct_face_in * INCH_MM

    @property
    def duct_depth_mm(self) -> float:
        return self.duct_depth_in * INCH_MM

    @property
    def rail_to_duct_mm(self) -> float:
        return self.rail_to_duct_in * INCH_MM

    @property
    def wall_gutter_mm(self) -> float:
        return self.wall_gutter_in * INCH_MM

    @property
    def airflow_gap_mm(self) -> float:
        return self.airflow_gap_in * INCH_MM

    def to_dict(self) -> dict[str, Any]:
        return {
            "spare_pct": self.spare_pct,
            "duct_face_in": self.duct_face_in,
            "duct_depth_in": self.duct_depth_in,
            "rail_to_duct_in": self.rail_to_duct_in,
            "wall_gutter_in": self.wall_gutter_in,
            "airflow_gap_in": self.airflow_gap_in,
            "side_ducts": self.side_ducts,
            "ambient_c": self.ambient_c,
            "max_internal_c": self.max_internal_c,
            "vfd_efficiency": self.vfd_efficiency,
            "psu_efficiency": self.psu_efficiency,
            "transformer_efficiency": self.transformer_efficiency,
            "nema": self.nema,
            "material": self.material,
            "mount": self.mount,
            "separate_control_rail": self.separate_control_rail,
        }


@dataclass
class SizedPart:
    ref: str
    library_id: str
    device: Device
    component: Component
    role: PartRole
    width_mm: float
    height_mm: float
    depth_mm: float
    dim_source: str
    missing_size: bool
    category: str
    catalog_dims_missing: bool = False
    layout_band: str = ""
    layout_group: str = ""
    layout_order: float = 0.0


@dataclass
class LayoutItem:
    kind: str
    x: float
    y: float
    w: float
    h: float
    ref: str = ""
    label: str = ""
    role: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "w": round(self.w, 2),
            "h": round(self.h, 2),
            "ref": self.ref,
            "label": self.label,
            "role": self.role,
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def options_from_defaults(raw: dict[str, Any] | None = None, **overrides: Any) -> SizingOptions:
    data = dict(raw or {})
    data.update({k: v for k, v in overrides.items() if v is not None})
    known = {f.name for f in SizingOptions.__dataclass_fields__.values()}
    picked = {k: v for k, v in data.items() if k in known}
    for key in ("side_ducts", "separate_control_rail"):
        if key in picked:
            picked[key] = _as_bool(picked[key])
    return SizingOptions(**picked)


def display_units(standard: str) -> str:
    return "in" if normalize_standard(standard, default="NEMA/ANSI") == "NEMA/ANSI" else "mm"


def format_length(mm: float, units: str) -> str:
    if units == "in":
        value = mm / INCH_MM
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return f"{text} in"
    return f"{mm:.0f} mm"


def format_box(h_mm: float, w_mm: float, d_mm: float, units: str) -> str:
    return (
        f"{format_length(h_mm, units)} × {format_length(w_mm, units)} × {format_length(d_mm, units)}"
    )


def _merged_props(device: Device, component: Component | None) -> dict[str, Any]:
    props: dict[str, Any] = dict(device.properties or {})
    if component is not None:
        props.update(component.properties or {})
    return props


def _first_number(value: Any, pattern: re.Pattern[str] | None = None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if pattern is not None:
        match = pattern.search(text)
        if match:
            return float(match.group(1))
    if _NUM_RE.fullmatch(text):
        return float(text)
    return None


def parse_watts_value(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if _KW_RE.search(text):
        return float(_KW_RE.search(text).group(1)) * 1000.0
    if _W_RE.search(text):
        return float(_W_RE.search(text).group(1))
    if _NUM_RE.fullmatch(text):
        return float(text)
    return None


def parse_kw(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _KW_RE.search(text)
    if match:
        return float(match.group(1))
    if _NUM_RE.fullmatch(text):
        return float(text)
    return None


def parse_hp(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _HP_RE.search(text)
    if match:
        return float(match.group(1))
    if _NUM_RE.fullmatch(text):
        return float(text)
    return None


def parse_va(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _VA_RE.search(text)
    if match:
        return float(match.group(1))
    if _NUM_RE.fullmatch(text):
        return float(text)
    return None


def hp_to_kw(hp: float) -> float:
    return float(hp) * 0.746


def loss_from_output(output_w: float, efficiency: float) -> float:
    eta = min(0.99, max(0.5, float(efficiency)))
    if output_w <= 0:
        return 0.0
    return output_w * (1.0 / eta - 1.0)


def _explicit_out_of_cabinet(component: Component | None) -> bool:
    """True when this instance says it does not occupy the cabinet.

    Catalog assign copies ``mount`` and ``din_rail`` onto every sheet that shares
    the tag, including an elementary coil. ``in_panel`` / ``incabinet`` are left
    alone, so an explicit false still means the symbol is not a second device.
    """

    if component is None:
        return False
    for key in ("incabinet", "in_panel"):
        if key not in component.properties:
            continue
        raw = component.properties.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, bool):
            return not raw
        return str(raw).strip().lower() not in ("1", "true", "yes", "y")
    return False


def is_generated_rail_stop(component: Component | None) -> bool:
    """True for a finish_rails stop (ES-<rail>-L/R), not a terminal-strip stop.

    Strip stops keep their own tags (XS1, and the strip's end covers). Those
    still pack onto the terminal rail. A generated rail stop is furniture:
    applying the enclosure again must reuse it, not treat it as another terminal.
    """

    if component is None:
        return False
    ident = str(component.id or "")
    if not ident.startswith("ES-"):
        return False
    props = component.properties or {}
    accessory = str(props.get("accessory") or "")
    role = str(props.get("role") or "")
    return accessory == "end_stop" or role == "end_stop" or bool(props.get("layout_furniture"))


def classify_role(device: Device, component: Component | None) -> PartRole:
    if component is not None and is_child_contact(device, component):
        return "skip"
    fp = effective_footprint(device, component)
    cat = (device.category or "").lower()
    if fp.mount == "door" or cat.startswith("operator") or cat.startswith("control.hmi"):
        return "door"
    if _explicit_out_of_cabinet(component):
        return "skip"
    if not is_incabinet(device, component) and fp.mount in {"none", "field"}:
        return "skip"
    lid = (device.library_id or "").lower()
    if cat.startswith("connection.accessory"):
        # DIN end stops and end plates ride the terminal rail. Jumpers are not this category.
        if (fp.din_rail or fp.mount == "din_rail") and is_incabinet(device, component):
            return "terminal"
        return "skip"
    if cat.startswith("connection.terminal") or ".terminals." in lid:
        return "terminal"
    if cat.startswith("drive") or cat.startswith("power.transformer"):
        return "block"
    if cat.startswith("power.supply") and (fp.width_mm >= 80.0 or fp.depth_mm >= 120.0 or not fp.din_rail):
        return "block"
    if fp.mount == "backplate" or (fp.in_panel and not fp.din_rail):
        return "block"
    if fp.din_rail or fp.mount == "din_rail":
        return "din"
    if fp.in_panel:
        return "block"
    return "skip"


def device_heat(
    device: Device,
    component: Component | None,
    options: SizingOptions,
) -> tuple[float | None, str]:
    """Return (watts, source). source is library, vfd_estimate, psu_estimate, transformer_estimate, or unknown."""

    props = _merged_props(device, component)
    for key in WATT_KEYS:
        watts = parse_watts_value(props.get(key))
        if watts is not None:
            return (watts, "library")
    cat = (device.category or "").lower()
    kw = None
    for key in KW_KEYS:
        kw = parse_kw(props.get(key))
        if kw is not None:
            break
    hp = None
    for key in HP_KEYS:
        hp = parse_hp(props.get(key))
        if hp is not None:
            break
    if kw is None and hp is not None:
        kw = hp_to_kw(hp)
    if cat.startswith("drive") or "vfd" in device.library_id.lower():
        if kw is not None:
            return (loss_from_output(kw * 1000.0, options.vfd_efficiency), "vfd_estimate")
        return (None, "unknown")
    if cat.startswith("power.supply"):
        vout = _first_number(props.get("Vout") or props.get("V") or props.get("voltage"), _VOLT_RE)
        iout = _first_number(props.get("Iout") or props.get("current"), _AMP_RE)
        if vout is not None and iout is not None:
            return (loss_from_output(vout * iout, options.psu_efficiency), "psu_estimate")
        if kw is not None:
            return (loss_from_output(kw * 1000.0, options.psu_efficiency), "psu_estimate")
        return (None, "unknown")
    if cat.startswith("power.transformer"):
        va = None
        for key in VA_KEYS:
            va = parse_va(props.get(key))
            if va is not None:
                break
        if va is not None:
            return (loss_from_output(va, options.transformer_efficiency), "transformer_estimate")
        return (None, "unknown")
    return (None, "unknown")


def enclosure_surface_m2(box: StandardEnclosure) -> float:
    """Effective radiating area. Wall-mount drops the back; floor-mount drops the bottom."""

    h = box.height_mm / 1000.0
    w = box.width_mm / 1000.0
    d = box.depth_mm / 1000.0
    if box.mount == "floor":
        return 2.0 * h * w + 2.0 * h * d + w * d
    return w * h + 2.0 * h * d + 2.0 * w * d


def heat_transfer_k(material: str, options: SizingOptions) -> float:
    token = (material or "steel").lower()
    if "stainless" in token or token in {"ss", "ss304", "ss316"}:
        return options.heat_k_stainless
    if token in {"polyester", "plastic", "nonmetallic", "frp", "fiberglass"}:
        return options.heat_k_nonmetallic
    return options.heat_k_steel


def temperature_rise_k(watts: float, surface_m2: float, k_w_m2k: float) -> float:
    if surface_m2 <= 0 or k_w_m2k <= 0:
        return 0.0 if watts <= 0 else float("inf")
    return watts / (k_w_m2k * surface_m2)


def cooling_for(nema: str, rise_k: float, allowed_k: float) -> str | None:
    if rise_k <= allowed_k + 1e-6:
        return None
    rating = str(nema or "12").upper()
    if rating in {"4", "4X"}:
        return "air_conditioner"
    return "fan_filter"


def _source_sheets(project: Project) -> list[Sheet]:
    return [sheet for sheet in project.sheets if sheet.kind in ("schematic", "io", "panel_layout", "door_layout")]


def _catalog_dims_missing(component: Component, dim_source: str) -> bool:
    """True when a catalog SKU is assigned and the footprint is not the maker drawing."""

    sku = str(component.properties.get("catalog") or component.properties.get("part_number") or "").strip()
    if not sku:
        return False
    return str(dim_source or "") != "catalog"


def _part_from_device(
    ref: str,
    device: Device,
    component: Component | None,
    role: PartRole,
) -> SizedPart:
    ghost = component or Component(id=ref, library_id=device.library_id, x=0.0, y=0.0, scale=1.0)
    w, h = footprint_size(device, ghost)
    fp = effective_footprint(device, ghost)
    ready = is_layout_ready(device, ghost) or not occupies_panel_layout(device, ghost)
    if not ready:
        w, h = 0.0, 0.0
    depth = float(fp.depth_mm or 0.0) if ready and not fp.depth_from_generic else 0.0
    props = ghost.properties or {}
    try:
        layout_order = float(props.get("layout_order") or 0.0)
    except (TypeError, ValueError):
        layout_order = 0.0
    return SizedPart(
        ref=ref,
        library_id=device.library_id,
        device=device,
        component=ghost,
        role=role,
        width_mm=w,
        height_mm=h,
        depth_mm=depth,
        dim_source=str(fp.dim_source or "generic"),
        missing_size=w <= 0 or h <= 0,
        category=device.category or "",
        catalog_dims_missing=_catalog_dims_missing(ghost, str(fp.dim_source or "")),
        layout_band=str(props.get("layout_band") or ""),
        layout_group=str(props.get("layout_group") or ""),
        layout_order=layout_order,
    )


def collect_strip_hardware(project: Project, catalog: Catalog) -> list[SizedPart]:
    """Terminals plus end covers, end stops, partitions, and markers from strip editor data.

    Widths match the strip drawing / vendor export (catalog footprint, else WIDTHS).
    A strip that has terminals but no end hardware gets the same default ends
    ``open_terminal_strip`` would add (end plate + end stop on each side).
    Marker text rides on each terminal the way the export ``marker`` column does;
    it does not add a second body on the rail.
    """

    from panelcad.core.terminal_strip import (
        ACCESSORIES,
        BLOCK_H,
        WIDTHS,
        layout_terminal_strip,
        list_terminal_strips,
    )

    parts: list[SizedPart] = []
    seen: set[str] = set()

    def _real_component(ref: str) -> Component | None:
        for sheet in project.sheets:
            try:
                return sheet.component(ref)
            except KeyError:
                continue
        return None

    def _add(kind: str, ref: str, library_id: str, width: float, height: float) -> None:
        if ref in seen:
            return
        seen.add(ref)
        device = catalog.devices.get(library_id)
        if device is None:
            device = Device(
                library_id=library_id,
                id=library_id,
                name=kind,
                category="connection.terminal" if kind == "terminal" else "connection.accessory",
            )
        real = _real_component(ref)
        props: dict[str, Any] = dict(real.properties) if real is not None else {}
        if kind != "terminal":
            props.setdefault("accessory", kind)
        ghost = Component(
            id=ref,
            library_id=device.library_id,
            x=0.0,
            y=0.0,
            scale=1.0,
            properties=props,
        )
        fp = effective_footprint(device, ghost)
        if occupies_panel_layout(device, ghost):
            ready = is_layout_ready(device, ghost)
        else:
            ready = assigned_catalog_ready(ghost, device=device) or (
                library_catalog_ready(device)
                and not instance_overrides_catalog_size(device, ghost)
            )
        w = float(width) if ready and width > 0 else (float(fp.width_mm) if ready and fp.width_mm > 0 else 0.0)
        h = float(height) if ready and height > 0 else (float(fp.height_mm) if ready and fp.height_mm > 0 else 0.0)
        depth = float(fp.depth_mm) if ready and fp.depth_mm > 0 and not fp.depth_from_generic else 0.0
        parts.append(
            SizedPart(
                ref=ref,
                library_id=device.library_id,
                device=device,
                component=ghost,
                role="terminal",
                width_mm=w,
                height_mm=h,
                depth_mm=depth,
                dim_source=str(fp.dim_source or "generic"),
                missing_size=w <= 0 or h <= 0 or not ready,
                category=device.category or ("connection.terminal" if kind == "terminal" else "connection.accessory"),
                catalog_dims_missing=_catalog_dims_missing(ghost, str(fp.dim_source or "")),
            )
        )

    for row in list_terminal_strips(project, catalog):
        name = str(row.get("strip") or "")
        if not name:
            continue
        layout = layout_terminal_strip(project, catalog, name)
        items = list(layout.get("items") or [])
        if not items and not int(row.get("terminals") or 0):
            continue
        kinds = {str(item.get("kind") or "") for item in items}
        if any(str(item.get("kind") or "") == "terminal" for item in items):
            prefix: list[dict[str, Any]] = []
            suffix: list[dict[str, Any]] = []
            if "end_stop" not in kinds:
                lid, _pfx = ACCESSORIES["end_stop"]
                end_dev = catalog.devices.get(lid)
                end_ready = bool(end_dev is not None and is_layout_ready(end_dev, None))
                prefix.append(
                    {
                        "kind": "end_stop",
                        "ref": f"{name}-XS-L",
                        "library_id": lid,
                        "w": WIDTHS["end_stop"] if end_ready else 0.0,
                        "h": BLOCK_H if end_ready else 0.0,
                    }
                )
                suffix.append(
                    {
                        "kind": "end_stop",
                        "ref": f"{name}-XS-R",
                        "library_id": lid,
                        "w": WIDTHS["end_stop"] if end_ready else 0.0,
                        "h": BLOCK_H if end_ready else 0.0,
                    }
                )
            if "end_plate" not in kinds:
                lid, _pfx = ACCESSORIES["end_plate"]
                end_dev = catalog.devices.get(lid)
                end_ready = bool(end_dev is not None and is_layout_ready(end_dev, None))
                prefix.append(
                    {
                        "kind": "end_plate",
                        "ref": f"{name}-XP-L",
                        "library_id": lid,
                        "w": WIDTHS["end_plate"] if end_ready else 0.0,
                        "h": BLOCK_H if end_ready else 0.0,
                    }
                )
                suffix.insert(
                    0,
                    {
                        "kind": "end_plate",
                        "ref": f"{name}-XP-R",
                        "library_id": lid,
                        "w": WIDTHS["end_plate"] if end_ready else 0.0,
                        "h": BLOCK_H if end_ready else 0.0,
                    },
                )
            items = prefix + items + suffix
        for item in items:
            kind = str(item.get("kind") or "terminal")
            ref = str(item.get("ref") or "")
            if not ref:
                continue
            lid = str(item.get("library_id") or ACCESSORIES.get(kind, ("", ""))[0])
            _add(kind, ref, lid, float(item.get("w") or 0.0), float(item.get("h") or 0.0))
    return parts


def collect_parts(project: Project, catalog: Catalog) -> tuple[list[SizedPart], list[SizedPart]]:
    """Unique in-cabinet / door parts. Later sheets (layout) win on instance props."""

    by_ref: dict[str, SizedPart] = {}
    for sheet in _source_sheets(project):
        for comp in sheet.components:
            device = catalog.devices.get(comp.library_id)
            if device is None:
                continue
            # Generated rail stops are rebuilt by finish_rails. Packing them
            # again puts another stop on the terminal rail and mints ES-…-R2.
            if is_generated_rail_stop(comp):
                continue
            from panelcad.library.panel import is_info_needed_placeholder

            if is_info_needed_placeholder(comp, device):
                continue
            role = classify_role(device, comp)
            if role == "skip":
                continue
            ghost = Component(
                id=comp.id,
                library_id=comp.library_id,
                x=0.0,
                y=0.0,
                scale=1.0,
                properties=dict(comp.properties),
            )
            by_ref[comp.id] = _part_from_device(comp.id, device, ghost, role)
    strip_parts = collect_strip_hardware(project, catalog)
    strip_refs = {part.ref for part in strip_parts}
    for part in strip_parts:
        by_ref[part.ref] = part
    # Strip editor data is the width source of truth; drop leftover schematic
    # terminals that the strip collector already replaced under another ref.
    cabinet = [p for p in by_ref.values() if p.role != "door"]
    if strip_refs:
        cabinet = [p for p in cabinet if p.role != "terminal" or p.ref in strip_refs]
        have = {p.ref for p in cabinet}
        cabinet.extend(part for part in strip_parts if part.ref not in have)
    door = [p for p in by_ref.values() if p.role == "door"]
    others = [p for p in cabinet if p.role != "terminal"]
    terminals = [p for p in cabinet if p.role == "terminal"]
    others.sort(key=lambda p: (p.role, p.ref))
    order = {part.ref: index for index, part in enumerate(strip_parts)}
    terminals.sort(key=lambda p: (order.get(p.ref, 10_000), p.ref))
    cabinet = others + terminals
    door.sort(key=lambda p: p.ref)
    return cabinet, door


_CONTACTOR_ROLES = frozenset({"fwd", "rev", "main", "star", "delta", "contactor", "bypass"})
_PLUG_ON_RE = re.compile(
    r"plug[-\s]?on|mounts?\s+(?:directly\s+)?(?:on|under|to)\b|"
    r"mounted\s+(?:directly\s+)?(?:on|under|to)\b|direct(?:ly)?[\s-]+mount",
    re.I,
)


def _part_role(part: SizedPart) -> str:
    props = part.component.properties if part.component is not None else {}
    return str(props.get("role") or "")


def _is_contactor(part: SizedPart) -> bool:
    if _part_role(part) in _CONTACTOR_ROLES:
        return True
    return (part.category or "").lower().startswith("switching.contactor")


def _is_overload(part: SizedPart) -> bool:
    if _part_role(part) == "overload":
        return True
    return (part.category or "").lower().startswith("protection.overload")


def _is_motor_cluster(part: SizedPart) -> bool:
    """Contactor, DIN overload, or interlock. These wrap as one piece."""

    role = _part_role(part)
    cat = (part.category or "").lower()
    if role in _CONTACTOR_ROLES or role in {"overload", "interlock"}:
        return True
    if cat.startswith("switching.contactor") or cat.startswith("protection.overload"):
        return True
    return "interlock" in cat


def is_plug_on_overload(device: Device, component: Component | None) -> bool:
    """Overload that clips under its contactor instead of taking rail width.

    Library ``mounting: plug_on`` is the explicit fact. Catalog text such as
    "mounts on" / "mounts directly under" counts the same way, so a part that
    is still tagged DIN but says it mounts on the contactor is stacked too.
    A generic stand-alone overload has neither, and stays on the rail.
    """

    if device is None:
        return False
    props = component.properties if component is not None else {}
    role = str(props.get("role") or "")
    cat = (device.category or "").lower()
    if role != "overload" and not cat.startswith("protection.overload"):
        return False
    mounting = str(props.get("mounting") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if mounting in {"plug_on", "plugon"}:
        return True
    text = " ".join(
        str(props.get(key) or "")
        for key in ("description", "notes", "function", "catalog", "part_number")
    )
    return _PLUG_ON_RE.search(text) is not None


def _plug_on_mount(part: SizedPart) -> bool:
    return is_plug_on_overload(part.device, part.component)


def _host_contactor(overload: SizedPart, contactors: list[SizedPart]) -> SizedPart | None:
    """The contactor this overload sits under: same motor, last one at or before it."""

    if not contactors:
        return None
    props = overload.component.properties if overload.component is not None else {}
    parent = str(props.get("parent") or "").strip()
    group = overload.layout_group
    pool = [part for part in contactors if group and part.layout_group == group] or list(contactors)
    if parent:
        named = next((part for part in pool if part.ref == parent), None)
        if named is not None:
            return named
    before = [part for part in pool if part.layout_order <= overload.layout_order + 1e-9]
    if before:
        return max(before, key=lambda part: (part.layout_order, part.ref))
    return min(pool, key=lambda part: (part.layout_order, part.ref))


def _plug_on_stacks(
    parts: list[SizedPart],
    contactors: list[SizedPart],
) -> dict[str, list[SizedPart]]:
    """Host ref → plug-on overloads. Overloads with no contactor are left out."""

    stacks: dict[str, list[SizedPart]] = {}
    for part in parts:
        if not _plug_on_mount(part):
            continue
        host = _host_contactor(part, contactors)
        if host is None or host.ref == part.ref:
            continue
        stacks.setdefault(host.ref, []).append(part)
    for group in stacks.values():
        group.sort(key=lambda part: (part.layout_order, part.ref))
    return stacks


def _module_width(part: SizedPart, stacks: dict[str, list[SizedPart]] | None = None) -> float:
    """Column width. A plug-on overload wider than its contactor widens the column."""

    width = max(part.width_mm, 1.0)
    for extra in (stacks or {}).get(part.ref, ()):
        width = max(width, max(extra.width_mm, 1.0))
    return width


def _module_body(part: SizedPart, stacks: dict[str, list[SizedPart]] | None = None) -> float:
    """Contactor height plus every overload stacked directly under it."""

    height = part.height_mm
    for extra in (stacks or {}).get(part.ref, ()):
        height += max(extra.height_mm, 0.0)
    return height


def _run_width(parts: list[SizedPart], gap: float) -> float:
    if not parts:
        return 0.0
    return sum(max(part.width_mm, 1.0) for part in parts) + gap * max(0, len(parts) - 1)


def _atomic_runs(parts: list[SizedPart]) -> list[list[SizedPart]]:
    """Split a motor group so the contactor, overload, and interlock stay one run."""

    runs: list[list[SizedPart]] = []
    current: list[SizedPart] = []
    for part in parts:
        if _is_motor_cluster(part):
            current.append(part)
            continue
        if current:
            runs.append(current)
            current = []
        runs.append([part])
    if current:
        runs.append(current)
    return runs


def _pack_rows(parts: list[SizedPart], inner_w: float, gap: float = MODULE_GAP_MM) -> list[list[SizedPart]]:
    """Wrap a run of DIN / terminal parts onto as many rows as ``inner_w`` needs.

    Parts with no layout band or group keep the original left-to-right wrap.
    A layout group stays on one row, and a layout band change starts a new row.
    When the group is wider than the rail, the breaker can wrap on its own.
    The motor's contactor, overload, and interlock stay together on one row.
    """

    if not any(part.layout_band or part.layout_group for part in parts):
        rows: list[list[SizedPart]] = []
        current: list[SizedPart] = []
        cursor = 0.0
        for part in parts:
            w = max(part.width_mm, 1.0)
            if current and cursor + w > inner_w + 1e-6:
                rows.append(current)
                current = []
                cursor = 0.0
            current.append(part)
            cursor += w + gap
        if current:
            rows.append(current)
        return rows or [[]]

    segments: list[list[SizedPart]] = []
    segment: list[SizedPart] = []
    for part in parts:
        if segment and (
            (part.layout_band and segment[0].layout_band and part.layout_band != segment[0].layout_band)
            or not (part.layout_group and part.layout_group == segment[0].layout_group)
        ):
            segments.append(segment)
            segment = []
        segment.append(part)
    if segment:
        segments.append(segment)

    rows = []
    current = []
    cursor = 0.0
    for segment in segments:
        width = _run_width(segment, gap)
        if width > inner_w + 1e-6:
            # The group does not fit this rail. Wrap at breaker / control
            # boundaries. Do not peel the overload or the interlock off the
            # contactors they belong to; a cluster wider than the rail stays
            # intact so the box fails the width check instead of splitting it.
            if current:
                rows.append(current)
                current = []
                cursor = 0.0
            for run in _atomic_runs(segment):
                run_w = _run_width(run, gap)
                if current and cursor + run_w > inner_w + 1e-6:
                    rows.append(current)
                    current = []
                    cursor = 0.0
                current.extend(run)
                cursor += run_w + gap
            continue
        if current and cursor + width > inner_w + 1e-6:
            rows.append(current)
            current = []
            cursor = 0.0
        current.extend(segment)
        cursor += width + gap
    if current:
        rows.append(current)
    return rows or [[]]


# Open gap between the power devices and the control devices on one rail.
CONTROL_RAIL_GAP_MM = 20.0


def _din_span(
    parts: list[SizedPart],
    stacks: dict[str, list[SizedPart]] | None = None,
) -> float:
    """Width of a DIN run. The first control device after power gets a 20 mm gap.

    A plug-on overload is not in ``parts``. It adds rail width only when it is
    wider than the contactor it hangs under.
    """

    span = 0.0
    seen_control = False
    seen_power = False
    for part in parts:
        if span > 0.0:
            if part.layout_band == "control" and seen_power and not seen_control:
                span += CONTROL_RAIL_GAP_MM
            else:
                span += MODULE_GAP_MM
        span += _module_width(part, stacks)
        if part.layout_band == "control":
            seen_control = True
        else:
            seen_power = True
    return span


def _merge_control_onto_power(
    power_rows: list[list[SizedPart]],
    control: list[SizedPart],
    usable: float,
    *,
    separate: bool,
    stacks: dict[str, list[SizedPart]] | None = None,
) -> tuple[list[list[SizedPart]], list[list[SizedPart]]]:
    """Put control on the right of the last power rail when the 20 mm gap fits.

    Earlier rows stay power-only. A short breaker row above a wrapped motor
    does not pick up the timer and the supply; those follow the contactors.
    """

    if separate:
        extra = _pack_rows(control, usable) if control else []
        return [list(row) for row in power_rows], extra
    rows = [list(row) for row in power_rows]
    pending = list(control)
    if not rows:
        return (_pack_rows(pending, usable) if pending else []), []
    last = rows[-1]
    while pending:
        if _din_span(last + [pending[0]], stacks) <= usable + 1e-6:
            last.append(pending.pop(0))
        else:
            break
    extra = _pack_rows(pending, usable) if pending else []
    return rows + extra, []


def _row_pitch(
    row: list[SizedPart],
    options: SizingOptions,
    stacks: dict[str, list[SizedPart]] | None = None,
) -> float:
    """Vertical module of a DIN row: device, both clearances, and the duct below."""

    body = max((_module_body(part, stacks) for part in row), default=RAIL_HEIGHT_MM)
    return body + 2.0 * options.rail_to_duct_mm + options.duct_face_mm


def _seat_blocks(
    rows: list[list[SizedPart]],
    blocks: list[SizedPart],
    usable: float,
    options: SizingOptions,
    stacks: dict[str, list[SizedPart]] | None = None,
) -> tuple[list[list[SizedPart]], list[SizedPart]]:
    """Panel-mount devices sit beside whichever row has the height and the width."""

    beside: list[list[SizedPart]] = [[] for _ in rows]
    used = [_din_span(row, stacks) for row in rows]
    leftover: list[SizedPart] = []
    for block in blocks:
        placed = False
        for index, row in enumerate(rows):
            if not row:
                continue
            if block.height_mm > _row_pitch(row, options, stacks) + 1e-6:
                continue
            gap = options.airflow_gap_mm if used[index] > 0.0 else 0.0
            need = gap + block.width_mm
            if beside[index]:
                need = options.airflow_gap_mm + block.width_mm
            if used[index] + need <= usable + 1e-6:
                beside[index].append(block)
                used[index] += need
                placed = True
                break
        if not placed:
            leftover.append(block)
    return beside, leftover


def _row_content_width(
    din: list[SizedPart],
    blocks: list[SizedPart],
    options: SizingOptions,
    stacks: dict[str, list[SizedPart]] | None = None,
) -> float:
    span = _din_span(din, stacks)
    if not blocks:
        return span
    extra = sum(block.width_mm for block in blocks) + options.airflow_gap_mm * max(0, len(blocks) - 1)
    if span > 0.0:
        extra += options.airflow_gap_mm
    return span + extra


def expand_frame_to_panel(
    items: list[LayoutItem],
    panel_w: float,
    panel_h: float,
    options: SizingOptions,
) -> list[LayoutItem]:
    """Stretch the perimeter ducts out to the subpanel. Devices stay at the top.

    The pack is only as tall as the parts. The subpanel is taller, and the
    bottom duct moves down to the subpanel edge so the frame is not floating
    in the upper half of the enclosure.
    """

    if panel_w <= 1.0 or panel_h <= 1.0:
        return items
    wall = options.wall_gutter_mm
    duct = options.duct_face_mm
    side = duct if options.side_ducts else 0.0
    inner_x = wall + side
    inner_w = max(40.0, panel_w - 2.0 * wall - 2.0 * side)
    top_y = wall
    bot_y = max(top_y + duct, panel_h - wall - duct)
    for item in items:
        if item.kind != "duct":
            continue
        if item.ref == "wt-top":
            item.x, item.y, item.w, item.h = wall, top_y, max(1.0, panel_w - 2.0 * wall), duct
        elif item.ref == "wt-bot":
            item.x, item.y, item.w, item.h = wall, bot_y, max(1.0, panel_w - 2.0 * wall), duct
        elif item.ref == "wt-left":
            item.x = wall
            item.y = top_y + duct + 0.2
            item.w = side
            item.h = max(0.0, bot_y - item.y - 0.2)
        elif item.ref == "wt-right":
            item.x = wall + side + inner_w
            item.y = top_y + duct + 0.2
            item.w = side
            item.h = max(0.0, bot_y - item.y - 0.2)
        elif item.h + 1e-6 < item.w:
            item.x = inner_x
            item.w = inner_w
    return items


def arrange_backpanel(
    cabinet: list[SizedPart],
    options: SizingOptions,
    inner_w: float | None = None,
) -> tuple[list[LayoutItem], float, float, float]:
    """Shop stack: DIN rows (duct between), blocks, then terminals above the bottom duct.

    ``inner_w`` is the candidate box's usable rail length. DIN and terminals wrap onto
    extra rows when they no longer fit that width — they are not stretched onto one
    long rail just because a wider catalog box exists.
    """

    din = [p for p in cabinet if p.role == "din" and not p.missing_size]
    terminals = [p for p in cabinet if p.role == "terminal" and not p.missing_size]
    blocks = [p for p in cabinet if p.role == "block" and not p.missing_size]
    banded = any(p.layout_band for p in cabinet)
    if banded:
        power = [p for p in din if p.layout_band != "control"]
        control = [p for p in din if p.layout_band == "control"]
        power.sort(key=lambda p: (p.layout_group, p.layout_order, p.ref))
        control.sort(key=lambda p: (p.layout_group, p.layout_order, p.ref))
        if any(p.layout_band == "terminal" for p in terminals):
            terminals.sort(key=lambda p: (p.layout_group, p.layout_order, p.ref))
    else:
        din.sort(key=lambda p: (p.height_mm, p.ref))
        power = din
        control = []
    blocks.sort(key=lambda p: (-p.width_mm * p.height_mm, p.ref))
    # Plug-on overloads are not rail modules and not panel-mount blocks.
    # They hang in the contactor's column. A generic DIN overload stays in `power`.
    contactors = [part for part in power if _is_contactor(part)]
    stacks = _plug_on_stacks(power + control + blocks, contactors)
    claimed = {part.ref for group in stacks.values() for part in group}
    if claimed:
        power = [part for part in power if part.ref not in claimed]
        control = [part for part in control if part.ref not in claimed]
        blocks = [part for part in blocks if part.ref not in claimed]

    wall = options.wall_gutter_mm
    duct = options.duct_face_mm
    clear = options.rail_to_duct_mm
    side = duct if options.side_ducts else 0.0
    gap = options.airflow_gap_mm

    min_rail = 80.0
    if power:
        min_rail = max(min_rail, max(p.width_mm for p in power))
    if control:
        min_rail = max(min_rail, max(p.width_mm for p in control))
    if terminals:
        min_rail = max(min_rail, max(p.width_mm for p in terminals))
    block_w = max((p.width_mm + 2.0 * gap for p in blocks), default=0.0)
    guess = inner_w if inner_w is not None else max(min_rail, block_w, 80.0)
    usable = max(60.0, guess)

    power_rows = _pack_rows(power, usable) if power else []
    din_rows, control_rows = _merge_control_onto_power(
        power_rows,
        control,
        usable,
        separate=bool(options.separate_control_rail),
        stacks=stacks,
    )
    beside, loose_blocks = _seat_blocks(din_rows, blocks, usable, options, stacks)
    term_rows = _pack_rows(terminals, usable) if terminals else []

    def _mixed_h(row: list[SizedPart], extra: list[SizedPart]) -> float:
        heights = [_module_body(part, stacks) for part in row] + [part.height_mm for part in extra]
        return max(heights, default=RAIL_HEIGHT_MM)

    content_w = 0.0
    for index, row in enumerate(din_rows):
        content_w = max(
            content_w,
            _row_content_width(row, beside[index] if index < len(beside) else [], options, stacks),
        )
    for row in control_rows:
        content_w = max(content_w, _din_span(row, stacks))
    for row in term_rows:
        content_w = max(content_w, _din_span(row))
    # Leftover panel-mount devices wrap on their own band, still inside `usable`.
    loose_widths: list[float] = []
    cursor = 0.0
    for part in loose_blocks:
        need = part.width_mm if cursor <= 0.0 else part.width_mm + gap
        if cursor > 0.0 and cursor + need > usable + 1e-6:
            loose_widths.append(cursor)
            cursor = part.width_mm
        else:
            cursor = cursor + need if cursor > 0.0 else part.width_mm
    if cursor > 0.0:
        loose_widths.append(cursor)
    if loose_widths:
        content_w = max(content_w, max(loose_widths))
    # Do not clip a motor cluster that is wider than this rail. The candidate
    # box then fails the panel-width check instead of hiding the overflow.
    content_w = max(content_w, 40.0)

    items: list[LayoutItem] = []
    y = wall
    field_x = wall + side
    field_w = content_w
    frame_w = side + content_w + side

    def _h_duct(y0: float, ident: str, *, inner: bool = False) -> float:
        items.append(
            LayoutItem(
                kind="duct",
                x=field_x if inner else wall,
                y=y0,
                w=field_w if inner else frame_w,
                h=duct,
                ref=ident,
                label="wire duct",
            )
        )
        return y0 + duct

    def _place_din(row: list[SizedPart], y_rail_top: float, role: str, extras: list[SizedPart]) -> float:
        row_h = _mixed_h(row, extras)
        span = _din_span(row, stacks if role == "din" else None)
        if span > 0.0:
            # The rail covers the modules that clip to it. A plug-on overload
            # hangs under its contactor and does not add a module or a stub.
            # A terminal strip is 47 mm tall, so its clip is 11.75 mm from the
            # top, not the 15 mm line used for a full-height breaker.
            clip = DIN_CLIP_FROM_TOP
            if role == "terminal":
                clips = sorted(
                    clip_from_body_top(part.height_mm) for part in row if part.height_mm > 0
                )
                if clips:
                    clip = clips[len(clips) // 2]
            items.append(
                LayoutItem(
                    kind="rail",
                    x=field_x,
                    y=y_rail_top + clip - RAIL_HEIGHT_MM / 2.0,
                    w=span,
                    h=RAIL_HEIGHT_MM,
                    ref=f"r-{role}-{len(items)}",
                    label="TS35",
                    role=role,
                )
            )
        x = field_x
        seen_control = False
        seen_power = False
        for part in row:
            if x > field_x:
                if part.layout_band == "control" and seen_power and not seen_control:
                    x += CONTROL_RAIL_GAP_MM
                else:
                    x += MODULE_GAP_MM
            column = _module_width(part, stacks)
            items.append(
                LayoutItem(
                    kind="device",
                    x=x + max(0.0, (column - part.width_mm) / 2.0),
                    y=y_rail_top,
                    w=part.width_mm,
                    h=part.height_mm,
                    ref=part.ref,
                    label=part.ref,
                    role=role,
                )
            )
            stacked_y = y_rail_top + part.height_mm
            for extra in stacks.get(part.ref, ()):
                items.append(
                    LayoutItem(
                        kind="device",
                        x=x + max(0.0, (column - extra.width_mm) / 2.0),
                        y=stacked_y,
                        w=extra.width_mm,
                        h=extra.height_mm,
                        ref=extra.ref,
                        label=extra.ref,
                        role=role,
                    )
                )
                stacked_y += extra.height_mm
            x += column
            if part.layout_band == "control":
                seen_control = True
            else:
                seen_power = True
        bx = field_x + span
        if extras and span > 0.0:
            bx += gap
        for part in extras:
            items.append(
                LayoutItem(
                    kind="block",
                    x=bx,
                    y=y_rail_top,
                    w=part.width_mm,
                    h=part.height_mm,
                    ref=part.ref,
                    label=part.ref,
                    role="block",
                )
            )
            bx += part.width_mm + gap
        return y_rail_top + row_h

    y = _h_duct(y, "wt-top")
    field_top = y
    for index, row in enumerate(din_rows):
        extras = beside[index] if index < len(beside) else []
        y += clear
        y = _place_din(row, y, "din", extras)
        y += clear
        if index + 1 < len(din_rows) or loose_blocks or control_rows or term_rows:
            y = _h_duct(y, f"wt-din-{len(items)}", inner=True)
    if loose_blocks:
        y += gap
        x = field_x
        row_h = 0.0
        for part in loose_blocks:
            if x > field_x and x + part.width_mm > field_x + field_w + 1e-6:
                x = field_x
                y += row_h + gap
                row_h = 0.0
            items.append(
                LayoutItem(
                    kind="block",
                    x=x,
                    y=y,
                    w=part.width_mm,
                    h=part.height_mm,
                    ref=part.ref,
                    label=part.ref,
                    role="block",
                )
            )
            x += part.width_mm + gap
            row_h = max(row_h, part.height_mm)
        y += row_h + gap
        if term_rows or control_rows:
            y = _h_duct(y, "wt-block", inner=True)
    elif not din_rows and (control_rows or term_rows):
        y += clear
        y = _h_duct(y, "wt-mid", inner=True)
    if control_rows:
        for index, row in enumerate(control_rows):
            y += clear
            y = _place_din(row, y, "din", [])
            y += clear
            if index + 1 < len(control_rows) or term_rows:
                y = _h_duct(y, f"wt-ctl-{len(items)}", inner=True)
    for index, row in enumerate(term_rows):
        y += clear
        y = _place_din(row, y, "terminal", [])
        y += clear
        if index + 1 < len(term_rows):
            y = _h_duct(y, f"wt-term-{len(items)}", inner=True)
    field_bot = y
    y = _h_duct(y, "wt-bot")

    if options.side_ducts:
        # Stop a hair short of the top and bottom ducts so the corner joint
        # does not occupy the same area after the sheet scale.
        side_h = max(0.0, field_bot - field_top - 0.4)
        items.append(
            LayoutItem(kind="duct", x=wall, y=field_top + 0.2, w=side, h=side_h, ref="wt-left", label="wire duct")
        )
        items.append(
            LayoutItem(
                kind="duct",
                x=wall + side + content_w,
                y=field_top + 0.2,
                w=side,
                h=side_h,
                ref="wt-right",
                label="wire duct",
            )
        )

    packed_w = wall + frame_w + wall
    packed_h = y + wall
    max_depth = max((p.depth_mm for p in cabinet if not p.missing_size), default=60.0)
    return items, packed_w, packed_h, max_depth


def _door_layout_order(part: SizedPart) -> float | None:
    props = part.component.properties if part.component is not None else {}
    if "layout_order" not in props:
        return None
    try:
        return float(props.get("layout_order"))
    except (TypeError, ValueError):
        return None


def _door_group_key(part: SizedPart) -> str:
    props = part.component.properties if part.component is not None else {}
    role = str(props.get("role") or "")
    if role == "estop":
        return "estop"
    group = part.layout_group or str(props.get("layout_group") or "")
    return group or part.ref


def _arrange_door_grid(present: list[SizedPart]) -> tuple[list[LayoutItem], float, float, float]:
    """One row per motor, 22 mm operators on 55 mm centres, legend under each."""

    def _insert_index(part: SizedPart) -> int:
        props = part.component.properties if part.component is not None else {}
        try:
            return int(props.get("layout_insert") or 0)
        except (TypeError, ValueError):
            return 0

    ordered = sorted(
        present,
        key=lambda part: (
            0 if _door_group_key(part) == "estop" else 1,
            _insert_index(part),
            _door_layout_order(part) if _door_layout_order(part) is not None else 0.0,
            part.ref,
        ),
    )
    groups: list[str] = []
    members: dict[str, list[SizedPart]] = {}
    for part in ordered:
        key = _door_group_key(part)
        if key not in members:
            groups.append(key)
            members[key] = []
        members[key].append(part)
    groups = [key for key in groups if key == "estop"] + [key for key in groups if key != "estop"]
    y = DOOR_MARGIN_MM
    items: list[LayoutItem] = []
    max_right = DOOR_MARGIN_MM
    max_depth = 0.0
    for key in groups:
        row_parts = members[key]
        pitch_x = door_row_pitch(
            [
                (
                    part.width_mm,
                    part.height_mm,
                    str((part.component.properties if part.component is not None else {}).get("legend") or ""),
                )
                for part in row_parts
            ]
        )
        if key != "estop":
            y += DOOR_HEADING_MM
        cols = 4
        for index, part in enumerate(row_parts):
            col = index % cols
            row = index // cols
            x = DOOR_MARGIN_MM + col * pitch_x
            yy = y + row * DOOR_PITCH_Y_MM
            legend = str((part.component.properties if part.component is not None else {}).get("legend") or "")
            items.append(
                LayoutItem(
                    kind="door",
                    x=x,
                    y=yy,
                    w=part.width_mm,
                    h=part.height_mm,
                    ref=part.ref,
                    label=legend or part.ref,
                    role="door",
                )
            )
            plate = legend_plate_rect(x, yy, part.width_mm, part.height_mm, legend)
            max_right = max(max_right, x + part.width_mm, plate[0] + plate[2])
            max_depth = max(max_depth, part.depth_mm)
        rows = (len(row_parts) + cols - 1) // cols
        y += max(rows, 1) * DOOR_PITCH_Y_MM
    width = max_right + DOOR_MARGIN_MM
    height = y + DOOR_MARGIN_MM
    return items, width, height, max_depth


def center_door_items(
    items: list[LayoutItem],
    width: float,
    height: float,
    margin: float = DOOR_MARGIN_MM,
) -> list[LayoutItem]:
    """Shift a door grid so operators, legends, and headings sit inside the outline."""

    if not items or width <= 0 or height <= 0:
        return items
    min_x = min_y = 1e9
    max_x = max_y = -1e9
    for item in items:
        min_x = min(min_x, item.x)
        min_y = min(min_y, item.y - DOOR_HEADING_MM)
        max_x = max(max_x, item.x + item.w)
        max_y = max(max_y, item.y + item.h)
        px, py, pw, ph = legend_plate_rect(
            item.x, item.y, item.w, item.h, item.label if item.kind == "door" else ""
        )
        min_x = min(min_x, px)
        min_y = min(min_y, py)
        max_x = max(max_x, px + pw)
        max_y = max(max_y, py + ph)
    span_w = max_x - min_x
    span_h = max_y - min_y
    ox = (width - span_w) / 2.0 - min_x
    oy = (height - span_h) / 2.0 - min_y
    if min_x + ox < margin:
        ox = margin - min_x
    if min_y + oy < margin:
        oy = margin - min_y
    if max_x + ox > width - margin and span_w + 2 * margin <= width:
        ox = width - margin - max_x
    if max_y + oy > height - margin and span_h + 2 * margin <= height:
        oy = height - margin - max_y
    for item in items:
        item.x += ox
        item.y += oy
    return items


def arrange_door(door: list[SizedPart], options: SizingOptions) -> tuple[list[LayoutItem], float, float, float]:
    if not door:
        return [], 0.0, 0.0, 0.0
    gap = options.airflow_gap_mm
    present = [part for part in door if not part.missing_size]
    if any(_door_layout_order(part) is not None for part in present):
        return _arrange_door_grid(present)
    x, y = gap, gap
    row_h = 0.0
    width = gap
    items: list[LayoutItem] = []
    ranked = sorted(door, key=lambda p: (-(p.width_mm * p.height_mm), p.ref))
    max_depth = 0.0
    for part in ranked:
        if part.missing_size:
            continue
        if x > gap and x + part.width_mm > 400.0:
            x = gap
            y += row_h + gap
            row_h = 0.0
        items.append(
            LayoutItem(
                kind="door",
                x=x,
                y=y,
                w=part.width_mm,
                h=part.height_mm,
                ref=part.ref,
                label=part.ref,
                role="door",
            )
        )
        x += part.width_mm + gap
        row_h = max(row_h, part.height_mm)
        width = max(width, x)
        max_depth = max(max_depth, part.depth_mm)
    height = y + row_h + gap
    return items, width, height, max_depth


def _nema_ok(wanted: str, have: str) -> bool:
    cover = _NEMA_COVERS.get(str(wanted), frozenset({str(wanted)}))
    return str(have) in cover


def _candidate_boxes(library: EnclosureLibrary, options: SizingOptions) -> list[StandardEnclosure]:
    wanted_nema = str(options.nema or "12")
    wanted_mat = (options.material or "").strip().lower()
    wanted_mount = (options.mount or "any").strip().lower()
    pool = [
        box
        for box in library.boxes
        if _nema_ok(wanted_nema, box.nema)
        and (not wanted_mat or wanted_mat == "any" or box.material.lower() == wanted_mat)
        and (wanted_mount in {"", "any"} or box.mount == wanted_mount)
    ]
    if not pool:
        pool = list(library.boxes)

    def _key(box: StandardEnclosure) -> tuple:
        exact = 0 if box.nema == wanted_nema else 1
        panel_area = box.panel_width_mm * box.panel_height_mm
        return (exact, panel_area, box.height_mm, box.width_mm, box.depth_mm)

    return sorted(pool, key=_key)


def frame_on_box(
    items: list[LayoutItem],
    box: StandardEnclosure,
) -> tuple[list[LayoutItem], float, float]:
    """Shift a panel-origin pack onto the back panel of the chosen box (enclosure coords)."""

    ox = max(0.0, (box.width_mm - box.panel_width_mm) / 2.0)
    oy = max(0.0, (box.height_mm - box.panel_height_mm) / 2.0)
    framed = [
        LayoutItem(
            kind=item.kind,
            x=item.x + ox,
            y=item.y + oy,
            w=item.w,
            h=item.h,
            ref=item.ref,
            label=item.label,
            role=item.role,
        )
        for item in items
    ]
    return framed, box.width_mm, box.height_mm


def usable_inner_width(box: StandardEnclosure, options: SizingOptions) -> float:
    side = options.duct_face_mm if options.side_ducts else 0.0
    return max(80.0, box.panel_width_mm - 2.0 * options.wall_gutter_mm - 2.0 * side)


def pick_enclosure(
    library: EnclosureLibrary,
    need_w: float,
    need_h: float,
    need_d: float,
    options: SizingOptions,
) -> tuple[StandardEnclosure | None, list[StandardEnclosure]]:
    """Smallest catalog box whose usable back panel and depth cover the need."""

    pool = _candidate_boxes(library, options)
    fitting = [
        box
        for box in pool
        if box.panel_width_mm + 1e-6 >= need_w
        and box.panel_height_mm + 1e-6 >= need_h
        and box.depth_mm + 1e-6 >= need_d
    ]
    if fitting:
        return fitting[0], fitting
    if pool:
        fallback = max(pool, key=lambda b: (b.panel_width_mm * b.panel_height_mm, b.depth_mm))
        return fallback, []
    return None, []


def heat_report(
    cabinet: list[SizedPart],
    box: StandardEnclosure | None,
    options: SizingOptions,
    *,
    enclosure: Enclosure | None = None,
) -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    known = 0.0
    unknown: list[str] = []
    for part in cabinet:
        if part.category.startswith("connection.accessory"):
            continue
        watts, source = device_heat(part.device, part.component, options)
        devices.append(
            {
                "ref": part.ref,
                "watts": None if watts is None else round(watts, 2),
                "source": source,
                "library_id": part.library_id,
            }
        )
        if watts is None:
            unknown.append(part.ref)
        else:
            known += watts
    allowed = max(0.1, options.max_internal_c - options.ambient_c)
    if box is not None:
        area = enclosure_surface_m2(box)
        k = heat_transfer_k(box.material, options)
        nema = box.nema
        mount = box.mount
        h_mm, w_mm, d_mm = box.height_mm, box.width_mm, box.depth_mm
    elif enclosure is not None:
        proxy = StandardEnclosure(
            id="current",
            mount="wall",
            nema=options.nema,
            material=options.material,
            height_mm=enclosure.height_mm,
            width_mm=enclosure.width_mm,
            depth_mm=enclosure.depth_mm,
            panel_height_mm=enclosure.height_mm,
            panel_width_mm=enclosure.width_mm,
        )
        area = enclosure_surface_m2(proxy)
        k = heat_transfer_k(options.material, options)
        nema = options.nema
        mount = "wall"
        h_mm, w_mm, d_mm = enclosure.height_mm, enclosure.width_mm, enclosure.depth_mm
    else:
        area = 0.0
        k = options.heat_k_steel
        nema = options.nema
        mount = "wall"
        h_mm = w_mm = d_mm = 0.0
    rise = temperature_rise_k(known, area, k) if known > 0 and area > 0 else 0.0
    cooling = cooling_for(nema, rise, allowed) if known > 0 and area > 0 else None
    ok = known <= 0 or area <= 0 or rise <= allowed + 1e-6
    return {
        "watts_known": round(known, 2),
        "unknown_refs": unknown,
        "unknown_count": len(unknown),
        "devices": devices,
        "ambient_c": options.ambient_c,
        "max_internal_c": options.max_internal_c,
        "allowed_rise_k": round(allowed, 2),
        "surface_m2": round(area, 4),
        "k_w_m2k": k,
        "rise_k": None if (known <= 0 or area <= 0) else round(rise, 2),
        "internal_c": None if (known <= 0 or area <= 0) else round(options.ambient_c + rise, 2),
        "ok": ok,
        "cooling": cooling,
        "mount": mount,
        "box_h_mm": h_mm,
        "box_w_mm": w_mm,
        "box_d_mm": d_mm,
        "formula": "ΔT = P / (k · A); wall-mount A excludes the back, floor-mount A excludes the bottom",
    }


def _library_box_for_enclosure(library: EnclosureLibrary, enc: Enclosure) -> StandardEnclosure | None:
    catalog_no = str(enc.catalog or "").strip()
    if catalog_no:
        for box in library.boxes:
            if str(box.catalog or "").strip() == catalog_no:
                return box
    for box in library.boxes:
        if (
            abs(box.width_mm - float(enc.width_mm)) < 1.5
            and abs(box.height_mm - float(enc.height_mm)) < 1.5
            and abs(box.depth_mm - float(enc.depth_mm)) < 1.5
        ):
            return box
    return None


def _placed_insert_frame(
    project: Project,
    catalog: Catalog,
    library: EnclosureLibrary,
) -> tuple[StandardEnclosure, list[LayoutItem], float, float] | None:
    """Enclosure-relative items when a circuit insert already sits in a catalog box.

    Returns the library box, items in enclosure coordinates (true mm), and the
    content's far edges on the subpanel. None when the sheet is still the
    default outline, a part is shrunk, or something leaves the subpanel.
    """

    sheet, enc = current_panel_enclosure(project)
    if sheet is None or enc is None:
        return None
    box = _library_box_for_enclosure(library, enc)
    if box is None or box.panel_width_mm < 40.0 or box.panel_height_mm < 40.0:
        return None
    scale = float(enc.draw_scale or 0.0)
    if scale <= 1e-6:
        return None
    plate = backplate_sheet_bounds(enc)
    items: list[LayoutItem] = []
    far_x = 0.0
    far_y = 0.0

    def take(kind: str, sx: float, sy: float, sw: float, sh: float, ref: str, label: str, role: str) -> bool:
        nonlocal far_x, far_y
        if sw <= 0.0 or sh <= 0.0:
            return True
        if sx < plate[0] - 1.5 or sy < plate[1] - 1.5 or sx + sw > plate[2] + 1.5 or sy + sh > plate[3] + 1.5:
            return False
        items.append(
            LayoutItem(
                kind=kind,
                x=(sx - float(enc.x)) / scale,
                y=(sy - float(enc.y)) / scale,
                w=sw / scale,
                h=sh / scale,
                ref=ref,
                label=label or ref,
                role=role,
            )
        )
        far_x = max(far_x, (sx + sw - plate[0]) / scale)
        far_y = max(far_y, (sy + sh - plate[1]) / scale)
        return True

    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        width, height = footprint_size(device, comp)
        if width <= 0.0 or height <= 0.0:
            continue
        ratio = float(comp.scale or 1.0) / scale
        if ratio < 0.98 or ratio > 1.05:
            return None
        if not take("device", comp.x, comp.y, width, height, comp.id, comp.id, str(comp.properties.get("layout_band") or "")):
            return None
    for rail in sheet.rails:
        rx, ry, rw, rh = rail_aabb(rail)
        rail_role = "terminal" if "terminal" in rail.id else "din"
        if not take("rail", rx, ry, rw, rh, rail.id, "TS35", rail_role):
            return None
    for tray in sheet.trays:
        tx, ty, tw, th = tray_aabb(tray)
        if not take("duct", tx, ty, tw, th, tray.id, "wire duct", ""):
            return None
    if not items:
        return None
    return box, items, far_x, far_y


def recommend_enclosure(
    project: Project,
    catalog: Catalog,
    *,
    library: EnclosureLibrary | None = None,
    options: SizingOptions | None = None,
    project_dir: Any = None,
) -> dict[str, Any]:
    """Recommend a standard box. Does not mutate the project."""

    lib = library or load_enclosures(project_dir)
    opts = options or options_from_defaults(lib.defaults)
    standard = sheet_drawing_standard(project, None)
    units = display_units(standard)
    cabinet, door = collect_parts(project, catalog)
    door_items, door_w, door_h, door_depth = arrange_door(door, opts)
    spare = max(0.0, float(opts.spare_pct))
    max_part_w = max((p.width_mm for p in cabinet if not p.missing_size), default=80.0)
    max_depth = max((p.depth_mm for p in cabinet if not p.missing_size), default=60.0)
    need_d = max(max_depth, opts.duct_depth_mm) + door_depth + opts.rail_to_duct_mm

    chosen: StandardEnclosure | None = None
    items: list[LayoutItem] = []
    packed_w = packed_h = 0.0
    heat: dict[str, Any] = {}
    # Geometric fits only. Candidates are smallest panel first. Each one is
    # re-packed to that subpanel's usable width: devices wrap onto another
    # row, and a panel-mount part sits on whichever row has room. The first
    # box whose panel covers that pack (width, height, and depth) wins.
    # Heat is reported and does not jump the box. `separate_control_rail`
    # still forces control onto its own rail.
    for box in _candidate_boxes(lib, opts):
        inner = usable_inner_width(box, opts)
        if max_part_w > inner + 1e-6:
            continue
        trial_items, trial_w, trial_h, trial_d = arrange_backpanel(cabinet, opts, inner_w=inner)
        need_h = max(trial_h, door_h)
        need_w = max(trial_w, door_w)
        trial_depth = max(trial_d, opts.duct_depth_mm) + door_depth + opts.rail_to_duct_mm
        if (
            box.panel_width_mm + 1e-6 < need_w
            or box.panel_height_mm + 1e-6 < need_h
            or box.depth_mm + 1e-6 < trial_depth
        ):
            continue
        chosen = box
        items = trial_items
        packed_w, packed_h, max_depth = trial_w, trial_h, trial_d
        heat = heat_report(cabinet, box, opts)
        break
    if chosen is None:
        pool = _candidate_boxes(lib, opts)
        chosen = max(pool, key=lambda b: (b.panel_width_mm * b.panel_height_mm, b.depth_mm)) if pool else None
        if chosen is not None:
            items, packed_w, packed_h, max_depth = arrange_backpanel(
                cabinet, opts, inner_w=usable_inner_width(chosen, opts)
            )
            heat = heat_report(cabinet, chosen, opts)
    if not heat:
        heat = heat_report(cabinet, chosen, opts)
    if chosen is not None and items:
        packed_w = max(packed_w, max((it.x + it.w for it in items), default=packed_w))
        packed_h = max(packed_h, max((it.y + it.h for it in items), default=packed_h) + opts.wall_gutter_mm)

    # The insert's ducts and row pitch are already on the sheet. A fresh pack
    # uses a 2 in duct and 1 in rail clearance and can be taller than the
    # panel that drawing fits. Keep that box, and keep its arrangement, when
    # the placed parts are inside it and the theoretical pack is not.
    keep_placed = False
    placed = _placed_insert_frame(project, catalog, lib)
    if placed is not None and chosen is not None:
        held, held_items, content_w, content_h = placed
        depth_need = max(max_depth, opts.duct_depth_mm) + door_depth + opts.rail_to_duct_mm
        theoretical_misses_current = (
            held.panel_width_mm + 1.0 < packed_w
            or held.panel_height_mm + 1.0 < packed_h
            or held.depth_mm + 1.0 < depth_need
        )
        content_fits = (
            content_w <= held.panel_width_mm + 1.5
            and content_h <= held.panel_height_mm + 1.5
            and door_w <= held.panel_width_mm + 1.5
            and door_h <= held.panel_height_mm + 1.5
            and held.depth_mm + 1e-6 >= depth_need
        )
        stored_split = bool((project.properties.get("enclosure_sizing") or {}).get("separate_control_rail"))
        split_changed = bool(opts.separate_control_rail) != stored_split
        if theoretical_misses_current and content_fits and not split_changed:
            chosen = held
            items = held_items
            packed_w, packed_h = content_w, content_h
            max_depth = min(max_depth, held.depth_mm)
            heat = heat_report(cabinet, chosen, opts)
            keep_placed = True

    # Spare is leftover back-panel height (future rows) and unused rail, not a wider box.
    need_w = max(packed_w, door_w)
    need_h = max(packed_h * (1.0 + spare), door_h)
    need_d = max(max_depth, opts.duct_depth_mm) + door_depth + opts.rail_to_duct_mm

    # fit is geometric: the catalog panel covers the wrapped back-panel.
    # Spare is reported. It does not pick a larger box.
    # Heat does not change this.
    fit = bool(
        chosen is not None
        and chosen.panel_width_mm + 1e-6 >= packed_w
        and chosen.panel_height_mm + 1e-6 >= packed_h
        and chosen.depth_mm + 1e-6 >= need_d
    )
    door_clash = bool(chosen is not None and door_depth > 0 and max_depth + door_depth + opts.rail_to_duct_mm > chosen.depth_mm + 1e-6)
    missing = [p.ref for p in cabinet + door if p.missing_size]
    if keep_placed and chosen is not None:
        frame_w, frame_h = chosen.width_mm, chosen.height_mm
    elif chosen is not None and items:
        items = expand_frame_to_panel(items, chosen.panel_width_mm, chosen.panel_height_mm, opts)
        items, frame_w, frame_h = frame_on_box(items, chosen)
    else:
        frame_w, frame_h = packed_w, packed_h
    if chosen is not None and door_items:
        center_door_items(door_items, chosen.width_mm, chosen.height_mm)
    reasons = _reasons(cabinet, door, opts, packed_w, packed_h, chosen, fit, heat, door_clash, missing, units)
    if keep_placed and chosen is not None:
        name = chosen.catalog or chosen.id
        reasons.insert(
            0,
            f"Circuit insert already fits {name}; Size Enclosure keeps that box instead of a taller duct pack",
        )

    enclosure_row = chosen.to_row() if chosen is not None else None
    if enclosure_row is not None:
        enclosure_row["name"] = chosen.display_name(units=units)

    return {
        "units": units,
        "standard": standard,
        "options": opts.to_dict(),
        "enclosure": enclosure_row,
        "fit": fit,
        "needed": {
            "width_mm": round(need_w, 2),
            "height_mm": round(need_h, 2),
            "depth_mm": round(need_d, 2),
            "width_in": round(need_w / INCH_MM, 3),
            "height_in": round(need_h / INCH_MM, 3),
            "depth_in": round(need_d / INCH_MM, 3),
            "packed_width_mm": round(packed_w, 2),
            "packed_height_mm": round(packed_h, 2),
            "spare_pct": spare,
        },
        "reasons": reasons,
        "parts": [_part_row(p) for p in cabinet],
        "door_parts": [_part_row(p) for p in door],
        "missing_size": missing,
        "catalog_dims_missing": [p.ref for p in cabinet + door if p.catalog_dims_missing],
        "door": {
            "count": len(door),
            "needed_width_mm": round(door_w, 2),
            "needed_height_mm": round(door_h, 2),
            "max_depth_mm": round(door_depth, 2),
            "clash": door_clash,
            "items": [it.to_row() for it in door_items],
        },
        "heat": heat,
        "arrangement": {
            "width_mm": round(frame_w, 2),
            "height_mm": round(frame_h, 2),
            "items": [it.to_row() for it in items],
        },
        "assumptions": _assumptions(opts, units),
        "applied": False,
    }


def _part_row(part: SizedPart) -> dict[str, Any]:
    return {
        "ref": part.ref,
        "library_id": part.library_id,
        "role": part.role,
        "width_mm": round(part.width_mm, 2),
        "height_mm": round(part.height_mm, 2),
        "depth_mm": round(part.depth_mm, 2),
        "dim_source": part.dim_source,
        "missing_size": part.missing_size,
        "catalog_dims_missing": part.catalog_dims_missing,
        "category": part.category,
    }


def _reasons(
    cabinet: list[SizedPart],
    door: list[SizedPart],
    options: SizingOptions,
    packed_w: float,
    packed_h: float,
    box: StandardEnclosure | None,
    fit: bool,
    heat: dict[str, Any],
    door_clash: bool,
    missing: list[str],
    units: str,
) -> list[str]:
    plug_on = [p for p in cabinet if _plug_on_mount(p)]
    plug_refs = {p.ref for p in plug_on}
    din = sum(1 for p in cabinet if p.role == "din" and p.ref not in plug_refs)
    terminals = sum(
        1
        for p in cabinet
        if p.role == "terminal" and (p.category.startswith("connection.terminal") or ".terminals." in p.library_id.lower())
    )
    hardware = sum(
        1
        for p in cabinet
        if p.role == "terminal" and p.category.startswith("connection.accessory")
    )
    blocks = sum(1 for p in cabinet if p.role == "block" and p.ref not in plug_refs)
    lines = [
        f"{len(cabinet)} in-cabinet part(s): {din} DIN-rail, {terminals} terminal"
        + (f" + {hardware} strip end/marker" if hardware else "")
        + (f", {len(plug_on)} plug-on overload(s)" if plug_on else "")
        + f", {blocks} back-panel block(s)",
        f"Door devices: {len(door)} (operators, HMI, disconnect handle)",
        f"Duct {options.duct_face_in:g} in × {options.duct_depth_in:g} in with {options.rail_to_duct_in:g} in rail clearance"
        + (" and side ducts" if options.side_ducts else ""),
        f"Packed back-panel {format_length(packed_w, units)} × {format_length(packed_h, units)} before {options.spare_pct:.0%} spare",
    ]
    if box is not None and packed_h > 1.0:
        height_spare = (box.panel_height_mm - packed_h) / packed_h
        area = max(packed_w * packed_h, 1.0)
        area_spare = (box.panel_width_mm * box.panel_height_mm - area) / area
        lines.append(
            f"Target spare {options.spare_pct:.0%} of packed height "
            f"({format_length(packed_h * options.spare_pct, units)}). "
            f"Catalog panel {format_length(box.panel_width_mm, units)} × "
            f"{format_length(box.panel_height_mm, units)} is {height_spare:.0%} taller "
            f"than the packed back-panel ({area_spare:.0%} by area)."
        )
        lines.append("Heat is reported and does not change the catalog box.")
        if height_spare > 0.32:
            lines.append(
                "Height spare is above 30% because the next smaller catalog box "
                "does not fit this wrapped back-panel."
            )
        elif height_spare < 0.18:
            lines.append(
                "Height spare is under 20%. This is the smallest catalog box "
                "whose panel covers the wrapped back-panel."
            )
    if box is None:
        lines.append("No standard enclosure in the library")
    elif fit:
        lines.append(f"Catalog box: {box.display_name(units=units)}")
    else:
        lines.append(f"Largest catalog box still too small: {box.display_name(units=units)}")
    if missing:
        lines.append("No library size for: " + ", ".join(missing))
    if door_clash:
        lines.append("Door-device depth plus back-panel devices need a deeper box")
    watts = heat.get("watts_known") or 0
    rise = heat.get("rise_k")
    if watts and rise is not None:
        lines.append(
            f"Heat {watts:g} W → ΔT {rise:g} K "
            f"(ambient {options.ambient_c:g} °C, limit {options.max_internal_c:g} °C, "
            f"A={heat.get('surface_m2')} m², k={heat.get('k_w_m2k')} W/m²·K)"
        )
        if not heat.get("ok"):
            cool = heat.get("cooling") or "cooling"
            lines.append(f"Temperature rise over the limit — recommend {cool.replace('_', ' ')}")
    unknown_refs = list(heat.get("unknown_refs") or [])
    if unknown_refs:
        shown = unknown_refs[:12]
        extra = len(unknown_refs) - len(shown)
        tail = f", … and {extra} more" if extra else ""
        lines.append(f"Heat unknown ({len(unknown_refs)}): " + ", ".join(shown) + tail)
    return lines


def _assumptions(options: SizingOptions, units: str) -> dict[str, Any]:
    return {
        "units": units,
        "spare_pct": options.spare_pct,
        "duct": f"{options.duct_face_in:g} × {options.duct_depth_in:g} in",
        "rail_to_duct_in": options.rail_to_duct_in,
        "wall_gutter_in": options.wall_gutter_in,
        "ambient_c": options.ambient_c,
        "max_internal_c": options.max_internal_c,
        "vfd_efficiency": options.vfd_efficiency,
        "psu_efficiency": options.psu_efficiency,
        "transformer_efficiency": options.transformer_efficiency,
        "heat_k_steel": options.heat_k_steel,
        "nema": options.nema,
        "material": options.material,
        "notes": [
            "DIN-rail rows with wire duct above and below; terminals along the bottom; VFDs / transformers / large PSUs as blocks",
            "Does not optimize placement; produces a believable shop arrangement",
            "Catalog numbers are blank unless confirmed on the maker's official page",
        ],
    }


def furniture_from_arrangement(
    items: list[dict[str, Any]] | list[LayoutItem],
    ox: float,
    oy: float,
    scale: float,
) -> tuple[list[Rail], list[WireTray]]:
    rails: list[Rail] = []
    trays: list[WireTray] = []
    for raw in items:
        row = raw.to_row() if isinstance(raw, LayoutItem) else raw
        kind = row.get("kind")
        x = ox + float(row["x"]) * scale
        y = oy + float(row["y"]) * scale
        w = float(row["w"]) * scale
        h = float(row["h"]) * scale
        ident = str(row.get("ref") or kind)
        if kind == "rail":
            rails.append(Rail(id=ident, x=x, y=y + h / 2.0, length_mm=w, height_mm=h))
        elif kind == "duct":
            if h >= w:
                trays.append(WireTray(id=ident, x=x, y=y, length_mm=h, width_mm=w, orientation="v"))
            else:
                trays.append(WireTray(id=ident, x=x, y=y, length_mm=w, width_mm=h, orientation="h"))
    return rails, trays


def current_panel_enclosure(project: Project) -> tuple[Sheet | None, Enclosure | None]:
    for sheet in project.sheets:
        if sheet.kind == "panel_layout" and sheet.enclosure is not None:
            return sheet, sheet.enclosure
    for sheet in project.sheets:
        if sheet.kind == "panel_layout":
            return sheet, sheet.enclosure
    return None, None
