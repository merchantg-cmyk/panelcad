"""Problems-dock findings for as-built photo reconstruction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.core.checks import Finding
from panelcad.reconstruct.models import Reconstruction, item_is_layout_ready

RECONSTRUCT_CHECKS = (
    "reconstruct_unconfirmed",
    "reconstruct_unscaled",
    "reconstruct_unreadable",
    "reconstruct_needs_info",
    "reconstruct_inferred_wire",
    "reconstruct_generic_layout",
    "reconstruct_print_mismatch",
    "reconstruct_print_unextracted",
    "reconstruct_photo_missing",
    "reconstruct_scale_perspective",
    "layout_not_ready",
)


def reconstruct_findings(project: Any, project_path: Path | str | None = None) -> list[Finding]:
    store: Reconstruction | None = getattr(project, "reconstruct", None)
    if store is None or store.is_empty():
        return _orphaned_placeholders(project)
    findings: list[Finding] = []
    from panelcad.reconstruct.geometry import metric_scale, mm_per_px
    from panelcad.reconstruct.store import resolve_path

    root = project_path or getattr(project, "path", None)
    scaled_photos = {photo.id for photo in store.photos if mm_per_px(photo) is not None}
    for photo in store.photos:
        if photo.resolved_path and Path(photo.resolved_path).is_file():
            continue
        found = resolve_path(photo.path, Path(root) if root else None)
        if found.is_file():
            continue
        if photo.source_path and Path(photo.source_path).is_file():
            continue
        findings.append(
            Finding(
                "reconstruct_photo_missing",
                "error",
                f"photo {photo.id} file is missing ({photo.path}) — re-attach it",
                extra={
                    "photo_id": photo.id,
                    "action": "reattach",
                    "fix": "re-attach this photo",
                },
            )
        )
    if store.photos and not scaled_photos:
        findings.append(
            Finding(
                "reconstruct_unscaled",
                "warning",
                "panel photos have no scale yet — mark two known lengths or the enclosure width and height",
                extra={"action": "confirm_scale"},
            )
        )
    for photo in store.photos:
        if photo.id not in scaled_photos:
            continue
        metric = metric_scale(photo)
        if metric and metric.get("perspective_warning") and metric.get("message"):
            findings.append(
                Finding(
                    "reconstruct_scale_perspective",
                    "info",
                    str(metric["message"]),
                    ref=str(photo.original_name or photo.id),
                    extra={"photo_id": photo.id, "action": "confirm_scale"},
                )
            )
    for photo in store.photos:
        if photo.unreadable:
            findings.append(
                Finding(
                    "reconstruct_unreadable",
                    "warning",
                    photo.vision_note
                    or f"photo {photo.id} could not be read — reshoot the nameplate square-on, out of glare",
                    extra={"photo_id": photo.id, "action": "reshoot", "fix": "reshoot or confirm envelope"},
                )
            )
    for item in store.items:
        if item.role != "part":
            continue
        if item.status == "needs_info":
            findings.append(
                Finding(
                    "reconstruct_needs_info",
                    "warning",
                    _needs_info_message(item, photo=_photo_for(store, item.photo_id)),
                    sheet=item.sheet,
                    ref=item.placed_ref or item.tag or item.id,
                    target=item.id,
                    extra={
                        "photo_id": item.photo_id,
                        "item_id": item.id,
                        "action": "pick_catalog",
                        "fix": "Pick a catalog part or measure W × H × D.",
                    },
                )
            )
            continue
        if (item.unreadable or item.status == "unconfirmed") and not item_is_layout_ready(item):
            findings.append(
                Finding(
                    "reconstruct_unconfirmed",
                    "warning",
                    _unconfirmed_message(item),
                    sheet=item.sheet,
                    ref=item.placed_ref or item.tag or item.id,
                    target=item.id,
                    extra={
                        "photo_id": item.photo_id,
                        "item_id": item.id,
                        "action": "pick_catalog",
                        "fix": "pick catalog part or confirm envelope",
                    },
                )
            )
            continue
        if item.photo_id not in scaled_photos and item_is_layout_ready(item) and not item.placed_ref:
            findings.append(
                Finding(
                    "reconstruct_unscaled",
                    "warning",
                    f"{item.id} is confirmed but photo {item.photo_id} has no scale, so it cannot be placed",
                    ref=item.tag or item.id,
                    target=item.id,
                    extra={"photo_id": item.photo_id, "item_id": item.id, "action": "confirm_scale"},
                )
            )
    findings.extend(_generic_on_layout(project))
    findings.extend(_inferred_wires(project))
    for mismatch in store.mismatches:
        findings.append(
            Finding(
                mismatch.check if mismatch.check.startswith("reconstruct_") else "reconstruct_print_mismatch",
                "info",
                mismatch.message,
                ref=mismatch.photo_tag or mismatch.print_tag,
                target=mismatch.item_id,
                extra={
                    "photo_id": mismatch.photo_id,
                    "item_id": mismatch.item_id,
                    "action": "compare",
                },
            )
        )
    return findings


def _photo_for(store: Any, photo_id: str) -> Any:
    try:
        return store.photo(photo_id)
    except KeyError:
        return None


def _photo_number(photo_id: str) -> str:
    text = str(photo_id or "")
    if len(text) > 1 and text[0] == "p" and text[1:].isdigit():
        return text[1:]
    return text or "?"


def _needs_info_message(item, photo: Any = None) -> str:
    tag = str(item.placed_ref or item.tag or "").strip()
    name = str(item.part_number or item.label_text or "").strip()
    photo_n = _photo_number(item.photo_id)
    original = str(getattr(photo, "original_name", "") or "").strip()
    photo = f"photo {photo_n} ({original})" if original else f"photo {photo_n}"
    if tag and name:
        who = f"{tag} ({name}, {photo})"
    elif name:
        who = f"{name} ({photo})"
    elif tag:
        who = f"{tag} ({photo})"
    else:
        who = f"This part ({photo})"
    return f"{who}: size unknown. Pick a catalog part or measure W × H × D."


def _unconfirmed_message(item) -> str:
    label = item.part_number or item.label_text or item.tag or item.id
    if item.unreadable:
        return f"{label} on photo {item.photo_id} is unreadable — pick a catalog part or confirm an envelope"
    return f"{label} on photo {item.photo_id} is unconfirmed — pick a catalog part or confirm an envelope"


def _orphaned_placeholders(project: Any) -> list[Finding]:
    from panelcad.reconstruct.placeholder import is_needs_info_component

    findings: list[Finding] = []
    for sheet in getattr(project, "sheets", []) or []:
        for comp in getattr(sheet, "components", []) or []:
            if not is_needs_info_component(comp):
                continue
            if str((comp.properties or {}).get("print_xref") or "").lower() in {"1", "true", "yes"}:
                continue
            findings.append(
                Finding(
                    "reconstruct_needs_info",
                    "error",
                    f"{comp.id} is INFO NEEDED — not layout-ready and not for build",
                    sheet=sheet.id,
                    ref=comp.id,
                    target=comp.id,
                    extra={
                        "action": "needs_info",
                        "fix": "confirm a catalog SKU or a measured envelope",
                        "library_id": str(comp.library_id or ""),
                    },
                )
            )
    return findings


def _generic_on_layout(project: Any) -> list[Finding]:
    from panelcad.library.panel import LAYOUT_READY_FIX, is_layout_ready

    findings: list[Finding] = []
    for sheet in project.sheets:
        if sheet.kind not in {"panel_layout", "door_layout"}:
            continue
        for comp in sheet.components:
            props = comp.properties or {}
            if not props.get("reconstruct_item"):
                continue
            if str(props.get("reconstruct_needs_info") or "").lower() in {"1", "true", "yes"}:
                continue
            from panelcad.library.catalog import default_catalog

            device = default_catalog().devices.get(str(comp.library_id or ""))
            if device is not None and is_layout_ready(device, comp):
                continue
            findings.append(
                Finding(
                    "reconstruct_generic_layout",
                    "error",
                    f"{comp.id} on {sheet.id} is not layout-ready — {LAYOUT_READY_FIX}",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                    extra={
                        "photo_id": str(props.get("reconstruct_photo") or ""),
                        "item_id": str(props.get("reconstruct_item") or ""),
                        "action": "pick_catalog",
                        "fix": LAYOUT_READY_FIX,
                    },
                )
            )
    return findings


def _inferred_wires(project: Any) -> list[Finding]:
    findings: list[Finding] = []
    for sheet in project.sheets:
        for wire in sheet.wires:
            if not getattr(wire, "inferred", False):
                continue
            findings.append(
                Finding(
                    "reconstruct_inferred_wire",
                    "warning",
                    f"inferred connection {wire.frm} → {wire.to} is not confirmed",
                    sheet=sheet.id,
                    ref=wire.id,
                    target=str(wire.number or ""),
                    extra={
                        "action": "confirm_connection",
                        "item_id": wire.id,
                        "photo_id": _wire_photo_id(project, wire),
                    },
                )
            )
    return findings


def _wire_photo_id(project: Any, wire: Any) -> str:
    store = getattr(project, "reconstruct", None)
    if store is None:
        return ""
    for item in store.items:
        if item.wire_number and str(item.wire_number) == str(wire.number or ""):
            return item.photo_id
        if item.placed_ref and item.placed_ref in {
            str(wire.frm).split(".")[0],
            str(wire.to).split(".")[0],
        }:
            return item.photo_id
    return ""
