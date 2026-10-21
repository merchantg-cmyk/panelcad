"""Panel footprint resolution: generic sizes when the catalog leaves W/H/D blank.

Generic table millimetres are for schematic symbols and BOM display only.
A backpanel or door layout may use a part only when it is layout-ready:
a catalog SKU with published W/H, or a person-confirmed envelope.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from panelcad.core.models import Component, Device, DimSource, MountKind, PanelFootprint

# Tag of a contact that belongs to a parent device: F1-95, K1-13, OL1-96, U1-14.
_CHILD_TAG = re.compile(r"^([A-Za-z]+\d+)[-.](\d+)$")

# Typical industrial sizes (mm) used when a library entry has no W/H/D.
# Agents should replace these with OEM catalog numbers via set_panel / set_properties.
GENERIC_BY_CATEGORY: dict[str, tuple[float, float, float, bool, MountKind]] = {
    "protection.breaker": (18.0, 80.0, 70.0, True, "din_rail"),
    "protection.fuse": (18.0, 80.0, 60.0, True, "din_rail"),
    "protection.overload": (45.0, 70.0, 80.0, True, "din_rail"),
    "protection.mpcb": (45.0, 90.0, 80.0, True, "din_rail"),
    "protection.disconnect": (90.0, 140.0, 90.0, False, "none"),
    "switching.contactor": (45.0, 75.0, 85.0, True, "din_rail"),
    "switching.relay": (22.5, 80.0, 70.0, True, "din_rail"),
    "switching.aux": (9.0, 45.0, 70.0, True, "din_rail"),
    "switching.safety": (22.5, 99.0, 114.0, True, "din_rail"),
    "timing.timer": (22.5, 80.0, 70.0, True, "din_rail"),
    "control.plc": (70.0, 90.0, 70.0, True, "din_rail"),
    "control.hmi": (200.0, 150.0, 40.0, False, "door"),
    "power.supply": (40.0, 90.0, 110.0, True, "din_rail"),
    "power.transformer": (80.0, 75.0, 75.0, True, "din_rail"),
    "drive.vfd": (80.0, 150.0, 140.0, False, "backplate"),
    "drive.softstarter": (80.0, 160.0, 140.0, False, "backplate"),
    "switching.softstarter": (80.0, 160.0, 140.0, False, "backplate"),
    "connection.terminal": (5.2, 47.0, 48.0, True, "din_rail"),
    "connection.accessory": (2.5, 47.0, 48.0, True, "din_rail"),
    "connection.jumper": (5.2, 4.0, 3.0, False, "none"),
    "connection.network": (100.0, 28.0, 70.0, True, "din_rail"),
    "connection.node": (0.0, 0.0, 0.0, False, "none"),
    "connection.offpage": (0.0, 0.0, 0.0, False, "none"),
    "connection.notes": (0.0, 0.0, 0.0, False, "none"),
    "load.solenoid": (0.0, 0.0, 0.0, False, "none"),
    "operator.pushbutton": (22.0, 22.0, 50.0, False, "door"),
    "operator.estop": (40.0, 40.0, 60.0, False, "door"),
    "operator.light": (22.0, 22.0, 50.0, False, "door"),
    "operator.selector": (22.0, 22.0, 50.0, False, "door"),
    "operator.limit": (0.0, 0.0, 0.0, False, "none"),
    "load.motor": (200.0, 200.0, 250.0, False, "none"),
}

# Schematic-only / field equipment — never packed onto a panel_layout sheet.
NOT_IN_PANEL_CATEGORIES = {
    "load.motor",
    "load.solenoid",
    "operator.limit",
    "connection.node",
    "connection.offpage",
    "connection.notes",
    "protection.disconnect",
}

_OVERRIDE_KEYS = ("width_mm", "height_mm", "depth_mm", "din_rail", "in_panel", "mount", "dim_source")

# Stored on the component when a person confirms an envelope instead of a SKU.
ENVELOPE_CONFIRMED_KEY = "envelope_confirmed"
ENVELOPE_BY_KEY = "envelope_confirmed_by"
ENVELOPE_AT_KEY = "envelope_confirmed_at"
ENVELOPE_WIDTH_KEY = "envelope_confirmed_width_mm"
ENVELOPE_HEIGHT_KEY = "envelope_confirmed_height_mm"
ENVELOPE_DEPTH_KEY = "envelope_confirmed_depth_mm"
ENVELOPE_MOUNT_KEY = "envelope_confirmed_mount"
ENVELOPE_SNAPSHOT_KEYS = (
    ENVELOPE_WIDTH_KEY,
    ENVELOPE_HEIGHT_KEY,
    ENVELOPE_DEPTH_KEY,
    ENVELOPE_MOUNT_KEY,
)

LAYOUT_READY_FIX = (
    "Pick a catalog part or enter and confirm an envelope "
    "(width, height, depth, mounting)."
)

# Reconstruct (#41) INFO NEEDED placeholders. Do not import reconstruct —
# that module lives on another branch. These names must stay stable so
# reconstruct/placeholder.py can plug into is_layout_ready without a fork.
NEEDS_INFO_LIBRARY_ID = "reconstruct.needs_info"
NEEDS_INFO_KEY = "reconstruct_needs_info"
# Reconstruct (#41) measured-envelope symbol. Name stays here so reconstruct
# can call this module without a second layout-ready gate.
MEASURED_ENVELOPE_LIBRARY_ID = "reconstruct.measured_envelope"
# Legacy key. Findings are derived at check time; do not persist a skip list.
LAYOUT_READY_SKIPPED_KEY = "layout_ready_skipped"

LayoutReadyExtra = Callable[[Device, Component | None], str]
_layout_ready_extras: list[LayoutReadyExtra] = []

_MOUNTS = ("din_rail", "backplate", "door", "field", "none")
_ENVELOPE_REQUIRED = (
    "confirm=true requires explicit width_mm, height_mm, depth_mm, and mount. "
    "Do not confirm a table-fill or guessed size."
)
MIN_ENVELOPE_MM = 1.0
CATALOG_DEPTH_MISSING_KEY = "catalog_depth_missing"
# Caller-supplied place/set_properties keys that must not invent catalog-ready.
_UNTRUSTED_READY_KEYS = (
    "dim_source",
    ENVELOPE_CONFIRMED_KEY,
    ENVELOPE_BY_KEY,
    ENVELOPE_AT_KEY,
    *ENVELOPE_SNAPSHOT_KEYS,
)
_CALLER_CATALOG_KEYS = ("catalog_key", "catalog_verified", "catalog_source")
_CALLER_ENVELOPE_KEYS = (
    ENVELOPE_CONFIRMED_KEY,
    ENVELOPE_BY_KEY,
    ENVELOPE_AT_KEY,
    *ENVELOPE_SNAPSHOT_KEYS,
)
_ASSIGN_SIZE_KEYS = frozenset(
    {"width_mm", "height_mm", "depth_mm", "dim_source", "catalog_depth_missing"}
)
_ASSIGN_MOUNT_KEYS = frozenset({"mount", "din_rail"})


class LayoutNotReadyError(ValueError):
    """Expected refuse: the part is not layout-ready, or the envelope is invalid."""


def register_layout_ready_extra(fn: LayoutReadyExtra) -> None:
    """Plug an extra not-ready reason into ``is_layout_ready`` (e.g. reconstruct)."""

    if fn not in _layout_ready_extras:
        _layout_ready_extras.append(fn)


def needs_info_library_id(library_id: str) -> bool:
    """True for the built-in mark and any other ``*.needs_info`` symbol."""

    lid = str(library_id or "").strip()
    return lid == NEEDS_INFO_LIBRARY_ID or lid.endswith(".needs_info")


def is_info_needed_placeholder(component: Component | None, device: Any = None) -> bool:
    """True for an INFO NEEDED / needs-info mark. Not tied to one project.

    A mark is the built-in symbol, any library id ending in ``.needs_info``,
    a component or device flagged ``reconstruct_needs_info``, a device named
    INFO NEEDED, or a component whose description is exactly that. Callers
    that already loaded the library device should pass it so a flag stored
    on the symbol counts even when the instance left it off.
    """

    lid = ""
    if component is not None:
        lid = str(getattr(component, "library_id", "") or "")
    if not lid and device is not None:
        lid = str(getattr(device, "library_id", "") or "")
    if needs_info_library_id(lid):
        return True
    if component is not None and _truthy((component.properties or {}).get(NEEDS_INFO_KEY)) is True:
        return True
    if component is not None:
        description = " ".join(str((component.properties or {}).get("description") or "").split()).casefold()
        if description == "info needed":
            return True
    if device is None:
        return False
    if needs_info_library_id(str(getattr(device, "library_id", "") or "")):
        return True
    if str(getattr(device, "name", "") or "").strip().casefold() == "info needed":
        return True
    return _truthy((getattr(device, "properties", None) or {}).get(NEEDS_INFO_KEY)) is True


def max_confirmed_envelope_mm() -> float:
    """Largest enclosure catalog dimension. Confirmed envelopes may not exceed it."""

    from panelcad.library.enclosures import largest_box_mm

    return float(largest_box_mm())


def require_explicit_envelope(
    width_mm: Any,
    height_mm: Any,
    depth_mm: Any,
    mount: Any,
) -> tuple[float, float, float, str]:
    """Require person-entered W/H/D/mount. Rejects missing, NaN, < 1 mm, absurd."""

    if width_mm is None or height_mm is None or depth_mm is None or mount in (None, ""):
        raise LayoutNotReadyError(_ENVELOPE_REQUIRED)
    try:
        width = float(width_mm)
        height = float(height_mm)
        depth = float(depth_mm)
    except (TypeError, ValueError) as exc:
        raise LayoutNotReadyError(
            "confirmed envelope needs numeric width_mm, height_mm, and depth_mm"
        ) from exc
    if not (math.isfinite(width) and math.isfinite(height) and math.isfinite(depth)):
        raise LayoutNotReadyError(
            "confirmed envelope needs finite width_mm, height_mm, and depth_mm"
        )
    kind = str(mount).strip()
    if kind not in _MOUNTS:
        raise LayoutNotReadyError(
            "confirmed envelope needs mount (din_rail, backplate, door, field, or none)"
        )
    if width < MIN_ENVELOPE_MM or height < MIN_ENVELOPE_MM or depth < MIN_ENVELOPE_MM:
        raise LayoutNotReadyError(
            "confirmed envelope needs width_mm, height_mm, and depth_mm of at least 1 mm"
        )
    limit = max_confirmed_envelope_mm()
    if width > limit or height > limit or depth > limit:
        raise LayoutNotReadyError(
            f"confirmed envelope {width:g}×{height:g}×{depth:g} mm is larger than "
            f"the largest catalog enclosure ({limit:g} mm). Check the units."
        )
    return width, height, depth, kind


def generic_for(category: str) -> tuple[float, float, float, bool, MountKind]:
    if category in GENERIC_BY_CATEGORY:
        return GENERIC_BY_CATEGORY[category]
    family = category.split(".")[0] if "." in category else category
    for key, val in GENERIC_BY_CATEGORY.items():
        if key.startswith(family + ".") or key == family:
            return val
    return (18.0, 80.0, 70.0, True, "din_rail")


def infer_in_panel(category: str, width: float, height: float, explicit: bool | None) -> bool:
    if explicit is not None:
        return bool(explicit)
    if category in NOT_IN_PANEL_CATEGORIES:
        return False
    if width <= 0 and height <= 0:
        return False
    return True


def infer_mount(category: str, din_rail: bool, in_panel: bool, explicit: str | None) -> MountKind:
    if explicit in ("din_rail", "backplate", "door", "field", "none"):
        return explicit  # type: ignore[return-value]
    if not in_panel:
        return "none"
    if din_rail:
        return "din_rail"
    if category.startswith("operator") or category.startswith("control.hmi"):
        return "door"
    if category.startswith("drive"):
        return "backplate"
    return "backplate"


def _symbol_only(raw: dict[str, Any]) -> bool:
    flag = raw.get("symbol_only")
    if isinstance(flag, str):
        return flag.strip().lower() in {"1", "true", "yes"}
    return flag is True


def resolve_panel(category: str, raw: dict[str, Any]) -> PanelFootprint:
    """Build a footprint, filling blank W/H/D from the generic table.

    ``symbol_only`` keeps the schematic symbol and does not invent millimetres.
    """
    if _symbol_only(raw):
        return PanelFootprint(
            width_mm=0.0,
            height_mm=0.0,
            depth_mm=0.0,
            din_rail=False,
            in_panel=False,
            mount="none",
            dim_source="generic",
            depth_from_generic=False,
        )
    gw, gh, gd, gdin, gmount = generic_for(category)
    w = _num(raw.get("width_mm", raw.get("w")))
    h = _num(raw.get("height_mm", raw.get("h")))
    d = _num(raw.get("depth_mm", raw.get("d")))
    din = raw.get("din_rail", raw.get("din"))
    if isinstance(din, str):
        din = din.lower() in ("1", "true", "yes", "din", "din_rail")
    filled = w is not None and h is not None and w > 0 and h > 0
    source: DimSource = "generic"
    depth_from_generic = False
    if not filled:
        w = gw if w is None or w <= 0 else w
        h = gh if h is None or h <= 0 else h
        if d is None or d <= 0:
            d = gd
            depth_from_generic = True
        if din is None:
            din = gdin
    else:
        if d is None or d <= 0:
            d = gd
            depth_from_generic = True
        if din is None:
            din = gdin
    explicit_source = raw.get("dim_source", raw.get("dims"))
    if explicit_source in ("generic", "catalog", "measured"):
        source = explicit_source
    if not filled and source == "catalog":
        # YAML claimed catalog without published W/H — still a generic fill.
        source = "generic"
    in_panel_raw = raw.get("in_panel")
    if isinstance(in_panel_raw, str):
        in_panel_raw = in_panel_raw.lower() in ("1", "true", "yes")
    in_panel = infer_in_panel(category, float(w or 0), float(h or 0), in_panel_raw if isinstance(in_panel_raw, bool) else None)
    if din is None:
        din = bool(in_panel and gdin)
    mount = infer_mount(category, bool(din), in_panel, raw.get("mount"))
    if not in_panel:
        din = False
        if mount == "din_rail":
            mount = "none"
    return PanelFootprint(
        width_mm=float(w or 0),
        height_mm=float(h or 0),
        depth_mm=float(d or 0),
        din_rail=bool(din),
        in_panel=in_panel,
        mount=mount,
        dim_source=source,
        depth_from_generic=depth_from_generic,
    )


def _truthy(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _contact_symbol(device: Device) -> bool:
    """Schematic contact of a contactor, overload, timer, or soft starter."""

    lid = (device.library_id or "").lower()
    cat = (device.category or "").lower()
    name = (device.name or "").lower()
    if "ol_contact" in lid or lid.endswith("aux_no") or lid.endswith("aux_nc"):
        return True
    if cat.startswith("switching.aux"):
        return True
    if "contact" in name and ("overload" in name or "aux" in name or "relay" in name):
        return True
    return False


def is_child_contact(device: Device, component: Component | None = None) -> bool:
    """True when this symbol is a contact of a parent device, not a catalog part.

    Overload 95-96/97-98, contactor auxiliaries, and soft-starter relay outputs
    roll up to the parent: no layout footprint and no BOM line. An explicit
    add-on block (``properties.addon``) is a real catalog part and stays.
    """

    if component is None or not _contact_symbol(device):
        return False
    if _truthy(component.properties.get("addon")):
        return False
    parent = str(component.properties.get("parent") or "").strip()
    if parent and parent != component.id:
        return True
    match = _CHILD_TAG.match(component.id or "")
    return bool(match and match.group(1) != component.id)


def is_incabinet(device: Device, component: Component | None = None) -> bool:
    """True if this part occupies cabinet volume (not motor, notes, or door hardware)."""
    if is_info_needed_placeholder(component, device):
        return False
    if component is not None:
        for key in ("incabinet", "in_panel"):
            if key in component.properties:
                flag = _truthy(component.properties.get(key))
                if flag is not None:
                    return flag
    fp = effective_footprint(device, component)
    if fp.mount == "door":
        return False
    return bool(fp.in_panel)


def display_footprint_size(device: Device, component: Component | None = None) -> tuple[float, float]:
    """Drawn layout size. `display_width_mm` / `display_height_mm` override catalog millimetres.

    BOM still uses `effective_footprint` (catalog / measured). Door photos use display
    size so a 219 mm stack light is not a tall empty bar on the sheet.
    """
    fp = effective_footprint(device, component)
    w = float(fp.width_mm)
    h = float(fp.height_mm)
    if component is not None:
        dw = _num(component.properties.get("display_width_mm"))
        dh = _num(component.properties.get("display_height_mm"))
        if dw is not None and dw > 0:
            w = dw
        if dh is not None and dh > 0:
            h = dh
        s = component.visual_scale()
        w *= s
        h *= s
    return (max(0.0, w), max(0.0, h))


def _zero_mark_footprint() -> PanelFootprint:
    """INFO NEEDED has no body. The generic 18×80×70 table fill is not a size."""

    return PanelFootprint(
        width_mm=0.0,
        height_mm=0.0,
        depth_mm=0.0,
        din_rail=False,
        in_panel=False,
        mount="none",
        dim_source="generic",
        depth_from_generic=False,
    )


def effective_footprint(device: Device, component: Component | None = None) -> PanelFootprint:
    """Library footprint plus optional instance overrides (OEM pull, BOM import)."""
    if is_info_needed_placeholder(component, device):
        return _zero_mark_footprint()
    base = device.panel
    data = base.model_dump()
    if component is not None:
        for key in _OVERRIDE_KEYS:
            if key not in component.properties:
                continue
            val = component.properties[key]
            if val == "" or val is None:
                continue
            if key in ("width_mm", "height_mm", "depth_mm"):
                try:
                    data[key] = float(val)
                except (TypeError, ValueError):
                    continue
            elif key in ("din_rail", "in_panel"):
                data[key] = str(val).lower() in ("1", "true", "yes")
            elif key == "mount" and str(val) in ("din_rail", "backplate", "door", "field", "none"):
                data[key] = str(val)
            elif key == "dim_source" and str(val) in ("generic", "catalog", "measured"):
                data[key] = str(val)
        if _num(component.properties.get("depth_mm")) is not None and float(
            component.properties.get("depth_mm") or 0
        ) > 0:
            data["depth_from_generic"] = False
        if _truthy(component.properties.get(CATALOG_DEPTH_MISSING_KEY)) is True:
            data["depth_from_generic"] = True
    return PanelFootprint.model_validate(data)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _finite_num(value: Any) -> float | None:
    number = _num(value)
    if number is None or not math.isfinite(number):
        return None
    return number


def refuse_nonfinite_sizes(properties: dict[str, Any]) -> None:
    """Raise when caller W/H/D is present and not a finite number."""

    for key in ("width_mm", "height_mm", "depth_mm"):
        if key not in properties or properties[key] in ("", None):
            continue
        number = _num(properties[key])
        if number is not None and not math.isfinite(number):
            raise LayoutNotReadyError(
                f"{key} must be a finite number. NaN and infinity cannot be a catalog or measured size."
            )


def _envelope_matches_snapshot(props: dict[str, Any]) -> bool:
    """True when instance W/H/D/mount still equal the stored confirmation."""

    for key, current_key in (
        (ENVELOPE_WIDTH_KEY, "width_mm"),
        (ENVELOPE_HEIGHT_KEY, "height_mm"),
        (ENVELOPE_DEPTH_KEY, "depth_mm"),
    ):
        confirmed = _finite_num(props.get(key))
        if confirmed is None:
            continue
        if not _same_mm(_finite_num(props.get(current_key)), confirmed):
            return False
    confirmed_mount = str(props.get(ENVELOPE_MOUNT_KEY) or "").strip()
    if confirmed_mount and str(props.get("mount") or "").strip() != confirmed_mount:
        return False
    return True


def _instance_has_envelope(component: Component | None) -> bool:
    """True when W/H/D/mount are stored on the instance, not filled from the library table."""

    if component is None:
        return False
    props = component.properties or {}
    for key in ("width_mm", "height_mm", "depth_mm"):
        number = _finite_num(props.get(key))
        if number is None or number <= 0:
            return False
    return str(props.get("mount") or "").strip() in _MOUNTS


def envelope_is_confirmed(component: Component | None) -> bool:
    """True when a person confirmed the instance envelope (not a silent default).

    New confirms store W/H/D/mount beside the flag; those values must still
    match. Older YAML that only has the flag stays confirmed (legacy).
    """

    if component is None:
        return False
    props = component.properties or {}
    if _truthy(props.get(ENVELOPE_CONFIRMED_KEY)) is not True:
        return False
    if any(props.get(key) not in ("", None) for key in ENVELOPE_SNAPSHOT_KEYS):
        return _envelope_matches_snapshot(props)
    return True


def occupies_panel_layout(device: Device, component: Component | None = None) -> bool:
    """True when this part would take space on a backpanel or door layout.

    Child contacts, schematic-only / field equipment, and rail furniture do not.
    """

    if is_info_needed_placeholder(component, device):
        return True
    if component is not None and is_child_contact(device, component):
        return False
    if component is not None:
        props = component.properties or {}
        if _truthy(props.get("layout_furniture")):
            return False
        accessory = str(props.get("accessory") or "")
        role = str(props.get("role") or "")
        if accessory == "end_stop" or role == "end_stop":
            return False
    fp = effective_footprint(device, component)
    cat = (device.category or "").lower()
    if fp.mount == "door" or cat.startswith("operator") or cat.startswith("control.hmi"):
        return True
    if cat in NOT_IN_PANEL_CATEGORIES:
        return False
    if component is not None:
        for key in ("incabinet", "in_panel"):
            if key in component.properties:
                flag = _truthy(component.properties.get(key))
                if flag is False:
                    return False
    if fp.mount in {"none", "field"} and not fp.in_panel:
        return False
    return bool(fp.in_panel)


def is_layout_ready(
    device: Device, component: Component | None = None, parts: Any = None
) -> bool:
    """True when layout / enclosure / cutout math may use this part's millimetres.

    Ready means a catalog footprint with published W×H, or a confirmed envelope
    (W, H, D, mounting) stored on the component. Generic table sizes are not ready.
    """

    return not layout_ready_gap(device, component, parts=parts)


def should_skip_layout_occupant(
    project: Any,
    device: Device | None,
    component: Component | None,
    *,
    sheet: str = "",
) -> bool:
    """True when an occupying part must not land on a layout.

    Uses ``is_layout_ready``. Does not mutate the project.
    """

    _ = project, sheet
    if is_info_needed_placeholder(component, device):
        lid = str(getattr(component, "library_id", "") or "")
        return lid != NEEDS_INFO_LIBRARY_ID
    if device is None:
        return component is not None
    if not occupies_panel_layout(device, component):
        return False
    return not is_layout_ready(device, component)


def _parts_for_lookup(parts: Any = None) -> Any:
    """Session catalog when given; otherwise the builtin parts library for this search path."""

    if parts is not None:
        return parts
    from panelcad.library.parts import default_parts_catalog

    return default_parts_catalog()


def resolve_assigned_part(props: dict[str, Any] | None, parts: Any = None) -> Any:
    """Parts-DB row written by ``assign_part`` (``catalog_key`` only)."""

    data = props or {}
    key = str(data.get("catalog_key") or "").strip()
    if not key:
        return None
    catalog = _parts_for_lookup(parts)
    return getattr(catalog, "parts", {}).get(key)


def catalog_row_fits_device(device: Device | None, part: Any) -> bool:
    """True when the SKU's library_id or category matches the symbol."""

    if device is None or part is None:
        return False
    if str(part.library_id or "") == str(device.library_id or ""):
        return True
    return bool(part.category) and str(part.category) == str(device.category or "")


def _same_mm(instance: float | None, published: float) -> bool:
    if instance is None:
        return False
    return math.isclose(instance, published, rel_tol=0.0, abs_tol=1e-6)


def _instance_matches_published(props: dict[str, Any], part: Any) -> bool:
    """True when instance W/H (and published D/mount) equal the parts-DB row."""

    if part is None or not part.has_footprint():
        return False
    width = _finite_num(props.get("width_mm"))
    height = _finite_num(props.get("height_mm"))
    if not _same_mm(width, float(part.width_mm or 0)):
        return False
    if not _same_mm(height, float(part.height_mm or 0)):
        return False
    if part.depth_mm is not None:
        depth = _finite_num(props.get("depth_mm"))
        if not _same_mm(depth, float(part.depth_mm)):
            return False
    from panelcad.library.parts import mounting_to_panel

    published_mount = mounting_to_panel(part.mounting)
    mount = str(props.get("mount") or "").strip()
    if (
        mount
        and published_mount
        and published_mount not in {"", "none"}
        and mount != published_mount
    ):
        return False
    return True


def assigned_catalog_ready(
    component: Component | None, parts: Any = None, device: Device | None = None
) -> bool:
    """True when ``assign_part`` wrote a fitting parts-DB row with a used footprint."""

    if component is None:
        return False
    if not assigned_catalog_ready_props(component.properties or {}, parts=parts):
        return False
    if device is None:
        return True
    part = resolve_assigned_part(component.properties or {}, parts)
    return catalog_row_fits_device(device, part)


def assigned_catalog_ready_props(props: dict[str, Any] | None, parts: Any = None) -> bool:
    """Same as ``assigned_catalog_ready`` for a raw property dict.

    Labels are not required here. GA500 FU1 keeps catalog/part_number FWH-70B
    (the fuse) and ``catalog_key`` Eaton|bh-0122 (the holder that has the
    published footprint). Identity edits still drop a stale key via
    ``assignment_labels_match`` / ``drop_stale_assignment``.
    """

    data = props or {}
    part = resolve_assigned_part(data, parts)
    return _instance_matches_published(data, part)


def _label_matches_assigned_row(value: str, part: Any) -> bool:
    token = str(value or "").strip()
    if not token:
        return True
    catalog = str(getattr(part, "catalog", "") or "")
    number = str(getattr(part, "part_number", None) or catalog)
    folded = token.casefold()
    return folded == catalog.casefold() or folded == number.casefold()


def assignment_labels_match(props: dict[str, Any] | None, parts: Any = None) -> bool:
    """True when catalog / part_number / manufacturer still name the assigned row.

    Catalog and part_number are compared separately. Each non-empty field must
    equal the row's catalog or part number (case-insensitive).
    """

    data = props or {}
    part = resolve_assigned_part(data, parts)
    if part is None:
        return not str(data.get("catalog_key") or "").strip()
    if not _label_matches_assigned_row(str(data.get("catalog") or ""), part):
        return False
    if not _label_matches_assigned_row(str(data.get("part_number") or ""), part):
        return False
    maker = str(data.get("manufacturer") or "").strip()
    if maker:
        from panelcad.library.parts import normalize_manufacturer

        if (
            normalize_manufacturer(maker).casefold()
            != normalize_manufacturer(str(part.manufacturer or "")).casefold()
        ):
            return False
    return True


def assignment_preserve_instance_keys(part: Any) -> frozenset[str]:
    """Instance keys ``assign_part`` must not pop when the SKU has no published value."""

    keep: set[str] = set()
    if part is None or not part.has_footprint():
        keep.update(_ASSIGN_SIZE_KEYS)
    from panelcad.library.parts import mounting_to_panel

    if part is None or not mounting_to_panel(getattr(part, "mounting", "")):
        keep.update(_ASSIGN_MOUNT_KEYS)
    return frozenset(keep)


def assignment_clears_confirmation(part: Any, props: dict[str, Any] | None) -> bool:
    """True when assign_part writes a published footprint or a different mount."""

    if part is None:
        return False
    if part.has_footprint():
        return True
    from panelcad.library.parts import mounting_to_panel

    published = mounting_to_panel(getattr(part, "mounting", ""))
    if not published:
        return False
    return str((props or {}).get("mount") or "").strip() != published


def library_catalog_ready(device: Device) -> bool:
    """True when the library device itself published catalog W×H."""

    lib = device.panel
    width = float(lib.width_mm or 0)
    height = float(lib.height_mm or 0)
    return str(lib.dim_source or "") == "catalog" and width > 0 and height > 0


def instance_overrides_catalog_size(device: Device, component: Component | None) -> bool:
    """True when instance W/H (or a published D) is not the library catalog size.

    Mount is not compared: a catalog body keeps its size on the door or the rail.
    """

    if component is None:
        return False
    props = component.properties or {}
    lib = device.panel
    checks: list[tuple[str, Any]] = [("width_mm", lib.width_mm), ("height_mm", lib.height_mm)]
    if not lib.depth_from_generic and float(lib.depth_mm or 0) > 0:
        checks.append(("depth_mm", lib.depth_mm))
    for key, published in checks:
        if key not in props or props[key] in ("", None):
            continue
        try:
            inst = float(props[key])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(inst) or abs(inst - float(published or 0)) > 1e-6:
            return True
    return False


def envelope_edit_changes(props: dict[str, Any] | None, incoming: dict[str, Any]) -> bool:
    """True when ``incoming`` W/H/D/mount differ from the stored values."""

    current = props or {}
    for key in ("width_mm", "height_mm", "depth_mm"):
        if key not in incoming:
            continue
        new = _finite_num(incoming.get(key))
        old = _finite_num(current.get(key))
        if new is None or old is None:
            if new is not None or old is not None or incoming.get(key) not in ("", None):
                return True
            continue
        if not _same_mm(old, new):
            return True
    if "mount" in incoming:
        if str(incoming.get("mount") or "").strip() != str(current.get("mount") or "").strip():
            return True
    return False


def clear_envelope_confirmation(component: Component) -> None:
    """Drop person-confirmation markers. Size edits must call this."""

    for key in _CALLER_ENVELOPE_KEYS:
        component.properties.pop(key, None)


def strip_placeholder_guesses(
    library_id: str, properties: dict[str, Any] | None
) -> dict[str, Any]:
    """INFO NEEDED marks carry no guessed or photo-derived millimetres."""

    props = dict(properties or {})
    if not needs_info_library_id(str(library_id or "")):
        return props
    for key in (
        "width_mm",
        "height_mm",
        "depth_mm",
        "mount",
        "dim_source",
        *_CALLER_CATALOG_KEYS,
        *_CALLER_ENVELOPE_KEYS,
    ):
        props.pop(key, None)
    return props


def sanitize_caller_properties(
    properties: dict[str, Any] | None, parts: Any = None
) -> dict[str, Any]:
    """Drop caller trust markers. ``assign_part`` is the only catalog-ready write."""

    _ = parts
    props = dict(properties or {})
    for key in _CALLER_ENVELOPE_KEYS:
        props.pop(key, None)
    for key in _CALLER_CATALOG_KEYS:
        props.pop(key, None)
    if str(props.get("dim_source") or "") == "catalog":
        props.pop("dim_source", None)
    return props


def trusted_place_properties(
    properties: dict[str, Any] | None, parts: Any = None
) -> dict[str, Any]:
    """Keep a real assigned SKU; drop caller confirmation and catalog laundering."""

    props = sanitize_caller_properties(properties, parts=parts)
    if assigned_catalog_ready_props(props, parts=parts):
        return props
    for key in _UNTRUSTED_READY_KEYS:
        props.pop(key, None)
    for key in ("width_mm", "height_mm", "depth_mm"):
        props.pop(key, None)
    return props


def _catalog_not_ready_message(component: Component, parts: Any = None) -> str:
    props = component.properties or {}
    sku = str(props.get("catalog") or props.get("part_number") or "").strip()
    key = str(props.get("catalog_key") or "").strip()
    label = sku or key
    part = resolve_assigned_part(props, parts)
    if part is not None:
        if not part.has_footprint():
            return f"catalog {part.catalog} has no published width/height. {LAYOUT_READY_FIX}"
        published_w = float(part.width_mm or 0)
        published_h = float(part.height_mm or 0)
        width = _finite_num(props.get("width_mm"))
        height = _finite_num(props.get("height_mm"))
        if _same_mm(width, published_w) and _same_mm(height, published_h):
            if part.depth_mm is not None:
                depth = _finite_num(props.get("depth_mm"))
                published_d = float(part.depth_mm)
                if not _same_mm(depth, published_d):
                    shown = f"{depth:g}" if depth is not None else "missing"
                    return (
                        f"catalog {part.catalog} depth {shown} does not match published "
                        f"{published_d:g}. {LAYOUT_READY_FIX}"
                    )
            from panelcad.library.parts import mounting_to_panel

            published_mount = mounting_to_panel(part.mounting)
            mount = str(props.get("mount") or "").strip()
            if (
                mount
                and published_mount
                and published_mount not in {"", "none"}
                and mount != published_mount
            ):
                return (
                    f"catalog {part.catalog} mount {mount} does not match published "
                    f"{published_mount}. {LAYOUT_READY_FIX}"
                )
        return (
            f"catalog {part.catalog} instance size does not match published "
            f"{published_w:g}×{published_h:g}. {LAYOUT_READY_FIX}"
        )
    width = _finite_num(props.get("width_mm"))
    height = _finite_num(props.get("height_mm"))
    depth = _finite_num(props.get("depth_mm"))
    if width is not None and height is not None and depth is not None:
        return (
            f"measured {width:g}×{height:g}×{depth:g} mm is not confirmed. "
            "Confirm the envelope (confirm_envelope) or assign_part a verified SKU."
        )
    if width is not None and height is not None:
        return (
            f"measured {width:g}×{height:g} mm is incomplete and not confirmed. "
            "Confirm the envelope (confirm_envelope) or assign_part a verified SKU."
        )
    if label:
        if key:
            return f"catalog {label} is not a parts-library row. {LAYOUT_READY_FIX}"
        return (
            f"catalog {label} is not assigned. Use assign_part. {LAYOUT_READY_FIX}"
        )
    return f"generic/placeholder size, not a catalog part. {LAYOUT_READY_FIX}"


def layout_ready_gap(
    device: Device, component: Component | None = None, parts: Any = None
) -> str:
    """Empty when the part may be laid out; otherwise the shop fix.

    This is the single gate. Reconstruct (#41) plugs in via
    ``register_layout_ready_extra`` or ``reconstruct_needs_info`` /
    ``reconstruct.needs_info``. The extra callback signature is unchanged.

    Catalog-ready means a library published W×H, or ``assign_part``
    ``catalog_key`` that resolves to a fitting parts-DB row whose published
    W/H (and D/mount when the row has them) are present and equal on the
    instance. A typed ``catalog`` / ``part_number`` is not enough.
    Caller-written ``catalog_key`` / ``envelope_confirmed`` do not count.
    Confirmed envelopes use ``require_explicit_envelope``.
    """

    for extra in _layout_ready_extras:
        gap = extra(device, component)
        if gap:
            return gap
    if is_info_needed_placeholder(component, device):
        return f"INFO NEEDED placeholder. {LAYOUT_READY_FIX}"
    if not occupies_panel_layout(device, component):
        return ""
    if assigned_catalog_ready(component, parts=parts, device=device):
        return ""
    if library_catalog_ready(device) and not instance_overrides_catalog_size(device, component):
        return ""
    if envelope_is_confirmed(component):
        fp = effective_footprint(device, component)
        try:
            if not _instance_has_envelope(component):
                raise LayoutNotReadyError(_ENVELOPE_REQUIRED)
            require_explicit_envelope(fp.width_mm, fp.height_mm, fp.depth_mm, fp.mount)
        except LayoutNotReadyError:
            return (
                f"{LAYOUT_READY_FIX} Confirmed envelope is missing width, height, "
                "depth, or mounting."
            )
        return ""
    if component is not None:
        return _catalog_not_ready_message(component, parts=parts)
    return f"generic/placeholder size, not a catalog part. {LAYOUT_READY_FIX}"


def export_dimension_mm(
    device: Device, component: Component | None = None
) -> tuple[str, str, str]:
    """BOM / report millimetres. Generic table fills stay blank."""

    fp = effective_footprint(device, component)
    if str(fp.dim_source or "generic") == "generic":
        return "", "", ""
    if occupies_panel_layout(device, component) and not is_layout_ready(device, component):
        return "", "", ""
    depth = ""
    if not fp.depth_from_generic and float(fp.depth_mm or 0) > 0:
        if component is None or _truthy((component.properties or {}).get(CATALOG_DEPTH_MISSING_KEY)) is not True:
            depth = f"{fp.depth_mm:g}"
    return f"{fp.width_mm:g}", f"{fp.height_mm:g}", depth


def layout_ready_error(
    ref: str, device: Device, component: Component | None = None, parts: Any = None
) -> str:
    """Session / CLI / agent error when a layout op is refused."""

    gap = layout_ready_gap(device, component, parts=parts)
    tag = str(ref or getattr(component, "id", "") or device.library_id)
    if not gap:
        return ""
    return f"{tag} is not layout-ready: {gap}"


def raise_layout_not_ready(
    ref: str, device: Device, component: Component | None = None, parts: Any = None
) -> None:
    """Raise ``LayoutNotReadyError`` when the part may not go on a layout."""

    message = layout_ready_error(ref, device, component, parts=parts)
    if message:
        raise LayoutNotReadyError(message)


def apply_confirmed_envelope(
    component: Component,
    *,
    width_mm: float,
    height_mm: float,
    depth_mm: float,
    mount: str,
    din_rail: bool | None = None,
    in_panel: bool | None = None,
    confirmed_by: str = "",
    confirmed_at: str = "",
) -> dict[str, Any]:
    """Store a person-confirmed envelope on the component. Does not invent sizes."""

    width, height, depth, kind = require_explicit_envelope(
        width_mm, height_mm, depth_mm, mount
    )
    when = str(confirmed_at or "").strip() or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    who = str(confirmed_by or "").strip()
    component.properties["width_mm"] = width
    component.properties["height_mm"] = height
    component.properties["depth_mm"] = depth
    component.properties["mount"] = kind
    component.properties["dim_source"] = "measured"
    if din_rail is None:
        din_rail = kind == "din_rail"
    component.properties["din_rail"] = bool(din_rail)
    if in_panel is None:
        in_panel = kind in {"din_rail", "backplate", "door"}
    component.properties["in_panel"] = bool(in_panel)
    component.properties["incabinet"] = bool(in_panel) and kind != "door"
    component.properties[ENVELOPE_CONFIRMED_KEY] = True
    component.properties[ENVELOPE_BY_KEY] = who
    component.properties[ENVELOPE_AT_KEY] = when
    component.properties[ENVELOPE_WIDTH_KEY] = width
    component.properties[ENVELOPE_HEIGHT_KEY] = height
    component.properties[ENVELOPE_DEPTH_KEY] = depth
    component.properties[ENVELOPE_MOUNT_KEY] = kind
    return {
        "width_mm": width,
        "height_mm": height,
        "depth_mm": depth,
        "mount": kind,
        "dim_source": "measured",
        ENVELOPE_CONFIRMED_KEY: True,
        ENVELOPE_BY_KEY: who,
        ENVELOPE_AT_KEY: when,
        ENVELOPE_WIDTH_KEY: width,
        ENVELOPE_HEIGHT_KEY: height,
        ENVELOPE_DEPTH_KEY: depth,
        ENVELOPE_MOUNT_KEY: kind,
    }
