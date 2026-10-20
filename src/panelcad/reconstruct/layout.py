"""Place only layout-ready parts from panel photos. No generic placeholders."""

from __future__ import annotations

import re
from typing import Any

from panelcad.library.panel import (
    ENVELOPE_AT_KEY,
    ENVELOPE_BY_KEY,
    ENVELOPE_CONFIRMED_KEY,
    MEASURED_ENVELOPE_LIBRARY_ID,
    _finite_num,
    is_layout_ready,
)
from panelcad.reconstruct.geometry import metric_scale, mm_per_px, photo_to_mm, rectified_size, region_top_left_mm
from panelcad.reconstruct.models import ObservedItem, Reconstruction, item_is_layout_ready

_MEASURED_DESC = re.compile(
    r"Measured envelope(?: \(unconfirmed, was (?P<was>[^)]+)\)| (?P<size>[\d.]+×[\d.]+×[\d.]+) mm)"
)

LAYOUT_PROP_KEYS = (
    "reconstruct_item",
    "reconstruct_photo",
    "photo_region_x",
    "photo_region_y",
    "photo_region_w",
    "photo_region_h",
    "envelope_confirmed",
    "dim_source",
)

# Person-confirmed envelope fields. ``assign_part`` writes the same names for a
# SKU, so an envelope switch must keep these and drop every other assignment key.
ENVELOPE_SIZE_KEYS = frozenset(
    {
        "width_mm",
        "height_mm",
        "depth_mm",
        "dim_source",
        "mount",
        "din_rail",
        "description",
    }
)
ENVELOPE_PERSON_KEYS = (
    ENVELOPE_CONFIRMED_KEY,
    ENVELOPE_BY_KEY,
    ENVELOPE_AT_KEY,
    "mount",
)


def assignment_keys_to_strip() -> frozenset[str]:
    """Every key ``assign_part`` writes, except the person's envelope fields.

    ``ASSIGNED_KEYS`` already includes the rating extras (``coil_voltage``,
    ``contact_config``, ``mounting``, ``range``, ``cutout``, and the rest).
    """

    from panelcad.library.parts import ASSIGNED_KEYS

    return frozenset(ASSIGNED_KEYS) - ENVELOPE_SIZE_KEYS


def ready_items(store: Reconstruction) -> list[ObservedItem]:
    return [item for item in store.items if item_is_layout_ready(item) and item.role == "part"]


def measured_envelope_description(width: float, height: float, depth: float) -> str:
    return f"Measured envelope {width:g}×{height:g}×{depth:g} mm"


def _is_measured_envelope_component(component: Any) -> bool:
    if component is None:
        return False
    lid = str(getattr(component, "library_id", "") or "")
    desc = str((component.properties or {}).get("description") or "")
    return lid == MEASURED_ENVELOPE_LIBRARY_ID or desc.startswith("Measured envelope")


def mark_confirmed_measured_description(
    component: Any, width: float, height: float, depth: float
) -> None:
    """Restore the confirmed W×H×D line after a person re-confirms the envelope."""

    if not _is_measured_envelope_component(component):
        return
    component.properties["description"] = measured_envelope_description(width, height, depth)


def mark_unconfirmed_measured_description(component: Any, *, was: tuple[Any, Any, Any] | None = None) -> None:
    """Keep a measured-envelope BOM/quote line from claiming a cleared confirmation."""

    if not _is_measured_envelope_component(component):
        return
    props = component.properties or {}
    desc = str(props.get("description") or "")
    if "unconfirmed, was" in desc:
        return
    label = ""
    match = _MEASURED_DESC.match(desc)
    if match and match.group("was"):
        return
    if match and match.group("size"):
        label = f"{match.group('size')} mm"
    if not label and was is not None:
        width, height, depth = was
        if width is not None and height is not None and depth is not None:
            label = f"{width:g}×{height:g}×{depth:g} mm"
    if not label:
        width = _finite_num(props.get("width_mm"))
        height = _finite_num(props.get("height_mm"))
        depth = _finite_num(props.get("depth_mm"))
        if width is not None and height is not None and depth is not None:
            label = f"{width:g}×{height:g}×{depth:g} mm"
    if not label:
        props["description"] = "Measured envelope (unconfirmed)"
        return
    props["description"] = f"Measured envelope (unconfirmed, was {label})"


def drop_envelope_confirmation(component: Any, *, was: tuple[Any, Any, Any] | None = None) -> None:
    """Clear confirmation markers and rewrite a measured-envelope description.

    Every size/mount path that drops confirmation must go through this helper
    (set_panel, set_properties, BOM import) so the BOM/quote line cannot stay
    on the old confirmed W×H×D text.
    """

    from panelcad.library.panel import clear_envelope_confirmation

    if component is None:
        return
    mark_unconfirmed_measured_description(component, was=was)
    clear_envelope_confirmation(component)


def refuse_generic(item: ObservedItem) -> None:
    if not item_is_layout_ready(item):
        raise ValueError(
            f"{item.id} is not layout-ready: confirm a catalog match or a measured envelope"
        )


def placement_properties(item: ObservedItem) -> dict[str, Any]:
    props: dict[str, Any] = {
        "reconstruct_item": item.id,
        "reconstruct_photo": item.photo_id,
        "photo_region_x": item.region.x,
        "photo_region_y": item.region.y,
        "photo_region_w": item.region.w,
        "photo_region_h": item.region.h,
    }
    if item.tag:
        props["tag"] = item.tag
    if item.status == "catalog" and item.matches:
        hit = item.matches[0]
        props["catalog"] = hit.catalog
        props["part_number"] = hit.catalog
        props["manufacturer"] = hit.manufacturer
        props["catalog_verified"] = hit.verified
        props["catalog_source"] = hit.source
        from panelcad.library.parts import part_key

        if hit.manufacturer and hit.catalog:
            props["catalog_key"] = part_key(hit.manufacturer, hit.catalog)
        if hit.width_mm:
            props["width_mm"] = hit.width_mm
        if hit.height_mm:
            props["height_mm"] = hit.height_mm
        if hit.depth_mm:
            props["depth_mm"] = hit.depth_mm
        props["dim_source"] = "catalog"
        props["description"] = hit.catalog
        props["notes"] = ""
    elif item.status == "envelope" and item.envelope_confirmed:
        props["width_mm"] = item.envelope_w_mm
        props["height_mm"] = item.envelope_h_mm
        props["depth_mm"] = item.envelope_d_mm
        props["dim_source"] = "measured"
        props["envelope_confirmed"] = "yes"
        props["mount"] = item.mount
        props["description"] = measured_envelope_description(
            float(item.envelope_w_mm), float(item.envelope_h_mm), float(item.envelope_d_mm)
        )
        props["notes"] = "tech-measured envelope — not a catalog part"
    props = strip_other_status_keys(props, item.status)
    if item.status == "envelope" and item.envelope_confirmed:
        props.update(label_identity(item))
    return props


def label_identity(item: ObservedItem) -> dict[str, str]:
    """Manufacturer and part number read off the label. Empty when the label had none."""

    props: dict[str, str] = {}
    brand = str(item.label_brand or item.brand or "").strip()
    number = str(item.label_part_number or "").strip()
    if item.status == "envelope":
        brand = str(item.label_brand or "").strip()
        number = str(item.label_part_number or "").strip()
    if brand:
        props["manufacturer"] = brand
    if number:
        props["part_number"] = number
        props["catalog"] = number
    return props


def strip_other_status_keys(properties: dict[str, Any], status: str) -> dict[str, Any]:
    """Drop the other confirm status's keys. Catalog ↔ envelope switches share this."""

    props = dict(properties)
    if status == "catalog":
        for key in ENVELOPE_PERSON_KEYS:
            props.pop(key, None)
        notes = str(props.get("notes") or "")
        if notes.startswith("tech-measured") or "placeholder" in notes.casefold():
            props["notes"] = ""
    elif status == "envelope":
        for key in assignment_keys_to_strip():
            props.pop(key, None)
    return props


def apply_item_status_to_component(component: Any, item: ObservedItem) -> None:
    """Write this item's confirm status onto a placed component and drop the other."""

    from panelcad.reconstruct.placeholder import resolved_properties

    incoming = strip_other_status_keys(resolved_properties(item), item.status)
    merged = strip_other_status_keys({**(component.properties or {}), **incoming}, item.status)
    if item.status == "catalog":
        merged["notes"] = str(incoming.get("notes") or "")
        if item.matches:
            merged["description"] = item.matches[0].catalog
    elif item.status == "envelope":
        for key in ("manufacturer", "part_number", "catalog", "catalog_key"):
            merged.pop(key, None)
        merged.update(label_identity(item))
    for key in (
        "reconstruct_needs_info",
        "needs_missing",
        "approx_width_mm",
        "approx_height_mm",
        "approx_error_mm",
        "display_width_mm",
        "display_height_mm",
        "poles",
        "Ie",
        "AC3",
        "coil",
        "standard",
    ):
        merged.pop(key, None)
    merged["in_panel"] = "yes"
    merged["bom"] = "yes"
    component.properties = {k: v for k, v in merged.items() if v not in ("", None)}


def measured_face_mm(photo: Any) -> tuple[float, float] | None:
    """Face width and height from the scale the tech marked. None if unscaled."""

    metric = metric_scale(photo)
    if metric is None or not metric.get("ready"):
        return None
    face_w = float(getattr(photo, "face_width_mm", 0) or 0)
    face_h = float(getattr(photo, "face_height_mm", 0) or 0)
    if face_w > 0 and face_h > 0:
        return (face_w, face_h)
    dest_w, dest_h = rectified_size(photo)
    width = dest_w * float(metric["sx"])
    height = dest_h * float(metric["sy"])
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def din_rail_face_y(photo: Any) -> float | None:
    """Face Y of a marked 35 mm DIN rail, before the enclosure origin is added."""

    for ref in (getattr(photo, "scale", None), getattr(photo, "scale_b", None)):
        if ref is None or str(getattr(ref, "kind", "") or "") != "din_rail":
            continue
        mid_x = (float(ref.p1_x) + float(ref.p2_x)) / 2.0
        mid_y = (float(ref.p1_y) + float(ref.p2_y)) / 2.0
        point = photo_to_mm(photo, mid_x, mid_y)
        if point is None:
            continue
        return point[1] - float(getattr(photo, "origin_y_mm", 0) or 0)
    return None


def fit_sheet_to_photo(sheet: Any, photo: Any, items: list[Any] | None = None) -> None:
    """Seat photo millimetres on the enclosure.

    The enclosure and rail change size only when the tech marked the corners
    as the backpanel. A part's face, or two lengths with no backpanel corners,
    leave the enclosure and rail as they are and shift the parts so they sit
    inside it, keeping their spacing.
    """

    metric = metric_scale(photo)
    if metric is None or not metric.get("ready"):
        return
    enc = getattr(sheet, "enclosure", None)
    if enc is None:
        return
    if getattr(photo, "face_is_backpanel", False):
        face_w = float(getattr(photo, "face_width_mm", 0) or 0)
        face_h = float(getattr(photo, "face_height_mm", 0) or 0)
        if face_w <= 0 or face_h <= 0:
            return
        enc.width_mm = face_w
        enc.height_mm = face_h
        if float(getattr(photo, "origin_x_mm", 0) or 0) == 0 and float(getattr(photo, "origin_y_mm", 0) or 0) == 0:
            photo.origin_x_mm = float(enc.x)
            photo.origin_y_mm = float(enc.y)
        if getattr(sheet, "kind", "") == "panel_layout":
            rails = getattr(sheet, "rails", None) or []
            if rails:
                rail = rails[0]
                rail.x = float(enc.x)
                rail.length_mm = face_w
                rail_y = din_rail_face_y(photo)
                if rail_y is not None:
                    rail.y = float(enc.y) + rail_y
        return
    _seat_parts_in_enclosure(photo, enc, items or [])


def _seat_parts_in_enclosure(photo: Any, enclosure: Any, items: list[Any]) -> None:
    """Shift the photo origin so every part's top-left is inside the enclosure.

    Relative spacing stays the spacing on the photo. The enclosure size is untouched.
    """

    photo.origin_x_mm = 0.0
    photo.origin_y_mm = 0.0
    xs: list[float] = []
    ys: list[float] = []
    for item in items:
        if str(getattr(item, "photo_id", "") or "") != str(getattr(photo, "id", "") or ""):
            continue
        if str(getattr(item, "role", "") or "") != "part":
            continue
        xy = region_top_left_mm(photo, item.region)
        if xy is None:
            continue
        xs.append(xy[0])
        ys.append(xy[1])
    if not xs:
        photo.origin_x_mm = float(enclosure.x)
        photo.origin_y_mm = float(enclosure.y)
        return
    photo.origin_x_mm = float(enclosure.x) - min(xs)
    photo.origin_y_mm = float(enclosure.y) - min(ys)


def measured_xy(store: Reconstruction, item: ObservedItem) -> tuple[float, float] | None:
    photo = store.photo(item.photo_id)
    if mm_per_px(photo) is None:
        return None
    return region_top_left_mm(photo, item.region)


def target_sheet_kind(role: str) -> str:
    if role in {"door_inside", "door_outside"}:
        return "door_layout"
    return "panel_layout"


def component_is_reconstructed(comp: Any) -> bool:
    props = getattr(comp, "properties", None) or {}
    return bool(props.get("reconstruct_item") or props.get("reconstruct_photo"))


def reconstructed_layout_ready(comp: Any, device: Any = None) -> bool:
    """Same layout-ready rule as ``is_layout_ready(device, component)``."""

    if device is None:
        from panelcad.library.catalog import default_catalog

        device = default_catalog().devices.get(str(getattr(comp, "library_id", "") or ""))
    if device is None:
        return False
    return is_layout_ready(device, comp)
