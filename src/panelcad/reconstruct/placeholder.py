"""Reconstruct-only 'needs info' placeholder. Not a generic part."""

from __future__ import annotations

from typing import Any

from panelcad.library.panel import (
    MEASURED_ENVELOPE_LIBRARY_ID,
    LayoutNotReadyError,
    envelope_is_confirmed,
    register_layout_ready_extra,
    require_explicit_envelope,
)
from panelcad.reconstruct.geometry import mm_per_px, photo_to_mm
from panelcad.reconstruct.models import ObservedItem, SCALE_ERROR_TOLERANCE_MM

NEEDS_INFO_LIBRARY_ID = "reconstruct.needs_info"
# Drawing-only mark. Never stored as a tech-entered millimetre.
PLACEHOLDER_MARK_W = 18.0
PLACEHOLDER_MARK_H = 12.0
MISSING_FIELDS = (
    "part_number",
    "manufacturer",
    "width_mm",
    "height_mm",
    "depth_mm",
    "mount",
    "terminals",
    "wires",
)


def reconstruct_mode(session: Any) -> bool:
    store = getattr(getattr(session, "project", None), "reconstruct", None)
    if store is None:
        return False
    return bool(getattr(store, "photos", None) or getattr(store, "items", None))


def is_needs_info_component(comp: Any) -> bool:
    """True only for the reconstruct.needs_info library symbol."""

    return str(getattr(comp, "library_id", "") or "") == NEEDS_INFO_LIBRARY_ID


def project_has_needs_info(project: Any) -> bool:
    for sheet in getattr(project, "sheets", []) or []:
        for comp in getattr(sheet, "components", []) or []:
            if is_needs_info_component(comp):
                return True
    store = getattr(project, "reconstruct", None)
    if store is None:
        return False
    return any(getattr(item, "status", "") == "needs_info" for item in getattr(store, "items", []) or [])


def missing_fields(item: ObservedItem) -> list[str]:
    missing: list[str] = []
    if not str(item.part_number or "").strip():
        missing.append("part_number")
    if not str(item.brand or "").strip():
        missing.append("manufacturer")
    if not (item.envelope_w_mm and float(item.envelope_w_mm) > 0):
        missing.append("width_mm")
    if not (item.envelope_h_mm and float(item.envelope_h_mm) > 0):
        missing.append("height_mm")
    if not (item.envelope_d_mm and float(item.envelope_d_mm) > 0):
        missing.append("depth_mm")
    if str(item.mount or "") not in {"din_rail", "backplate", "door", "field"}:
        missing.append("mount")
    if item.role == "part" and not item.terminal:
        missing.append("terminals")
    if item.role == "part" and not item.wire_number:
        missing.append("wires")
    return missing


def approx_size(photo: Any, region: Any) -> dict[str, float | None]:
    """Photo-derived size estimate. Never a real dimension."""

    if mm_per_px(photo) is None:
        return {"approx_width_mm": None, "approx_height_mm": None, "approx_error_mm": None}
    x0, y0 = float(getattr(region, "x", 0) or 0), float(getattr(region, "y", 0) or 0)
    x1 = x0 + float(getattr(region, "w", 0) or 0)
    y1 = y0 + float(getattr(region, "h", 0) or 0)
    a = photo_to_mm(photo, x0, y0)
    b = photo_to_mm(photo, x1, y1)
    if a is None or b is None:
        return {"approx_width_mm": None, "approx_height_mm": None, "approx_error_mm": None}
    return {
        "approx_width_mm": abs(b[0] - a[0]),
        "approx_height_mm": abs(b[1] - a[1]),
        "approx_error_mm": float(getattr(photo, "scale_error_mm", None) or SCALE_ERROR_TOLERANCE_MM),
    }


def placeholder_properties(item: ObservedItem, photo: Any) -> dict[str, Any]:
    missing = missing_fields(item)
    # No photo-derived millimetres. The renderer uses a fixed mark, not these fields.
    return {
        "reconstruct_needs_info": "yes",
        "reconstruct_item": item.id,
        "reconstruct_photo": item.photo_id,
        "photo_region_x": item.region.x,
        "photo_region_y": item.region.y,
        "photo_region_w": item.region.w,
        "photo_region_h": item.region.h,
        "in_panel": "no",
        "incabinet": "no",
        "bom": "no",
        "label_text": item.label_text,
        "part_number": item.part_number,
        "marker_label": item.part_number or item.label_text or item.tag,
        "needs_missing": ",".join(missing),
        "description": "INFO NEEDED",
        "notes": "reconstruct placeholder — not a catalog part",
    }


def resolved_properties(item: ObservedItem) -> dict[str, Any]:
    from panelcad.reconstruct.layout import placement_properties

    props = placement_properties(item)
    props["reconstruct_needs_info"] = ""
    props.pop("needs_missing", None)
    props["in_panel"] = "yes"
    props["incabinet"] = "yes"
    props["bom"] = "yes"
    return {key: value for key, value in props.items() if value not in ("", None)}


def _reconstruct_layout_ready_extra(device: Any, component: Any) -> str:
    """PR #40 extras hook: reconstruct.measured_envelope is confirm-path only."""

    _ = device
    if component is None:
        return ""
    lid = str(getattr(component, "library_id", "") or "")
    if lid != MEASURED_ENVELOPE_LIBRARY_ID:
        return ""
    props = getattr(component, "properties", None) or {}
    try:
        require_explicit_envelope(
            props.get("width_mm"),
            props.get("height_mm"),
            props.get("depth_mm"),
            props.get("mount"),
        )
    except LayoutNotReadyError:
        return "reconstruct.measured_envelope needs a confirmed envelope from reconstruct_confirm_item"
    if not envelope_is_confirmed(component):
        return "reconstruct.measured_envelope needs a confirmed envelope from reconstruct_confirm_item"
    return ""


register_layout_ready_extra(_reconstruct_layout_ready_extra)
