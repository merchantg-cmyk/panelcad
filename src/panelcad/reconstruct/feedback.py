"""Cabinet photo feedback overlay, missed-device mechanism & Capture-Phase Site-Exit Check.

Highlights every detected device directly on the real cabinet photo:
- Missing-info devices get a high-visibility amber/red bounding box plus an
  explicit callout badge listing what PanelCAD is missing (part #, verified
  W×H×D envelope, mounting, unreadable label / close-up needed, wire landing,
  print mismatch).
- Confirmed layout-ready devices get a clean green bounding box + checkmark
  badge so a human can immediately see every device the bot/software detected,
  and spot any physical device on the cabinet photo that has NO box around it
  (completely missed by the bot/software).
- Capture-Phase Site-Exit Check (`safe_to_leave_site`, `site_exit_banner`,
  `on_site_shot_list`, `link_closeup_to_device`): runs right in Step 1 (Add
  Photos / Capture Phase) on real cabinet photos so a technician standing in
  front of the open cabinet knows immediately whether they can close the door
  and leave the site or which close-up photos / device details they must grab
  first.
- Supports adding a missed device directly from a bounding box drawn on the
  photo (`add_missed_device_on_photo`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from panelcad.reconstruct.checklist import rebuild_checklist
from panelcad.reconstruct.geometry import mm_per_px
from panelcad.reconstruct.match import match_catalog
from panelcad.reconstruct.models import (
    ObservedItem,
    Photo,
    PhotoRegion,
    Reconstruction,
    catalog_match_has_footprint,
    ensure_reconstruction,
    item_is_layout_ready,
    public_item,
    public_photo,
)
from panelcad.reconstruct.placeholder import missing_fields
from panelcad.reconstruct.store import load_upright_image, next_id, resolve_path

FeedbackFilterMode = Literal["all", "missing_only", "confirmed_only"]
FeedbackStatusCategory = Literal["missing_info", "confirmed"]
SiteExitStatus = Literal["ready", "do_not_leave", "needs_analysis"]

MISSED_DEVICE_PROMPT = (
    "Any physical cabinet component without a box was missed by detection — "
    "drag a box on the photo to add a missed device, or click a red/amber box to supply missing info."
)


class DeviceFeedbackBox(BaseModel):
    item_id: str
    photo_id: str
    tag: str = ""
    role: str = "part"
    status: str = "unconfirmed"
    status_category: FeedbackStatusCategory = "missing_info"
    layout_ready: bool = False
    part_number: str = ""
    brand: str = ""
    label_text: str = ""
    region: dict[str, float] = Field(default_factory=lambda: {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0})
    missing_fields: list[str] = Field(default_factory=list)
    missing_reasons: list[str] = Field(default_factory=list)
    header_label: str = ""
    badge_lines: list[str] = Field(default_factory=list)
    needs_closeup: bool = False
    closeup_photo_id: str = ""
    placed_ref: str = ""
    confidence: float = 0.0
    dimensions_label: str = ""
    envelope_w_mm: float | None = None
    envelope_h_mm: float | None = None
    envelope_d_mm: float | None = None
    mount: str = ""
    confirmed_summary: list[str] = Field(default_factory=list)
    checklist_prompts: list[str] = Field(default_factory=list)


class PhotoFeedbackReport(BaseModel):
    photo_id: str
    photo_name: str = ""
    photo_role: str = "backpanel"
    photo_path: str = ""
    width_px: int = 0
    height_px: int = 0
    scaled: bool = False
    mm_per_px: float | None = None
    filter_mode: FeedbackFilterMode = "all"
    total_detected: int = 0
    confirmed_count: int = 0
    missing_info_count: int = 0
    closeup_needed_count: int = 0
    unboxed_count: int = 0
    safe_to_leave_site: bool = False
    site_exit_status: SiteExitStatus = "needs_analysis"
    site_exit_banner: str = ""
    on_site_shot_list: list[str] = Field(default_factory=list)
    boxes: list[DeviceFeedbackBox] = Field(default_factory=list)
    summary_text: str = ""
    missed_device_prompt: str = MISSED_DEVICE_PROMPT


def _extract_project_and_session(target: Any) -> tuple[Any, Any, Path | None]:
    if hasattr(target, "project"):
        session = target
        project = target.project
        base_path = getattr(target, "path", None)
        return session, project, base_path
    if isinstance(target, Reconstruction):
        return None, None, None
    return None, target, None


def _resolve_store(target: Any) -> tuple[Reconstruction, Any, Path | None]:
    if isinstance(target, Reconstruction):
        return target, None, None
    session, project, base_path = _extract_project_and_session(target)
    store = ensure_reconstruction(project)
    return store, session, base_path


def _pick_photo(store: Reconstruction, photo_id: str | None = None) -> Photo:
    if not store.photos:
        raise ValueError("no panel photos in reconstruction")
    if photo_id:
        return store.photo(photo_id)
    for preferred in ("backpanel", "door_inside", "door_outside"):
        for candidate in store.photos:
            if candidate.role == preferred:
                return candidate
    return store.photos[0]


def _item_dimensions_label(item: ObservedItem) -> str:
    if item.status == "catalog" and item.matches and catalog_match_has_footprint(item.matches[0]):
        m = item.matches[0]
        return f"{float(m.width_mm):g}×{float(m.height_mm):g}×{float(m.depth_mm):g} mm"
    if item.envelope_confirmed and item.envelope_w_mm and item.envelope_h_mm and item.envelope_d_mm:
        return f"{float(item.envelope_w_mm):g}×{float(item.envelope_h_mm):g}×{float(item.envelope_d_mm):g} mm"
    return ""


def _device_missing_analysis(
    store: Reconstruction,
    photo: Photo,
    item: ObservedItem,
) -> tuple[list[str], list[str], bool]:
    """Return (missing_field_tokens, human_readable_missing_reasons, needs_closeup)."""
    raw_fields = list(missing_fields(item))
    reasons: list[str] = []
    tokens: list[str] = []
    closeup_id = str(getattr(item, "closeup_photo_id", "") or "").strip()
    has_part = bool(str(item.part_number or "").strip())
    needs_closeup = bool((item.unreadable or (not has_part and not item.envelope_confirmed)) and not closeup_id)

    ready = item_is_layout_ready(item)

    if item.unreadable and not closeup_id:
        tokens.append("unreadable")
        reasons.append("MISSING: Unreadable Nameplate — Needs Close-Up Photo")

    if not str(item.tag or "").strip():
        tokens.append("tag")
        reasons.append("MISSING: Device Tag (e.g. K1, CB1, PSU1)")

    if not ready:
        has_brand = bool(str(item.brand or "").strip())
        if item.status == "catalog" and item.matches:
            match = item.matches[0]
            if not catalog_match_has_footprint(match):
                missing_dims: list[str] = []
                if not (match.width_mm and float(match.width_mm) > 0):
                    missing_dims.append("W")
                    tokens.append("width_mm")
                if not (match.height_mm and float(match.height_mm) > 0):
                    missing_dims.append("H")
                    tokens.append("height_mm")
                if not (match.depth_mm and float(match.depth_mm) > 0):
                    missing_dims.append("D")
                    tokens.append("depth_mm")
                dim_str = "×".join(missing_dims) if missing_dims else "W×H×D"
                reasons.append(f"MISSING: Verified Footprint ({dim_str} mm) for {match.catalog}")
        else:
            if not has_part and not item.envelope_confirmed:
                tokens.append("part_number")
                if not has_brand:
                    tokens.append("manufacturer")
                reasons.append("MISSING: Part # / Catalog SKU (or Confirmed W×H×D)")
            elif has_part and not item.matches and not item.envelope_confirmed:
                tokens.append("catalog_match")
                reasons.append(f"MISSING: Catalog Match for '{item.part_number}' (or W×H×D)")
            elif item.matches and item.status != "catalog" and not item.envelope_confirmed:
                cand = item.matches[0]
                tokens.append("confirm_catalog")
                reasons.append(f"MISSING: Confirm Catalog SKU ({cand.manufacturer} {cand.catalog})")

            if not item.envelope_confirmed and item.status != "catalog":
                missing_env: list[str] = []
                if not (item.envelope_w_mm and float(item.envelope_w_mm) > 0):
                    missing_env.append("W")
                    tokens.append("width_mm")
                if not (item.envelope_h_mm and float(item.envelope_h_mm) > 0):
                    missing_env.append("H")
                    tokens.append("height_mm")
                if not (item.envelope_d_mm and float(item.envelope_d_mm) > 0):
                    missing_env.append("D")
                    tokens.append("depth_mm")
                if missing_env:
                    reasons.append(f"MISSING: Verified Dimensions ({'×'.join(missing_env)} mm)")
                if str(item.mount or "") not in {"din_rail", "backplate", "door", "field"}:
                    tokens.append("mount")
                    reasons.append("MISSING: Mounting Type (DIN Rail / Backplate / Door)")

    # Check open checklist items tied to this item or its tag/wires
    for step in store.checklist:
        if step.status != "open":
            continue
        if step.item_id == item.id:
            if step.kind == "trace_wire" and step.wire_number:
                tokens.append("wire_landing")
                msg = f"MISSING: Wire #{step.wire_number} Terminal Landing"
                if msg not in reasons:
                    reasons.append(msg)
            elif step.kind in {"identify_part", "needs_info"} and not reasons:
                tokens.append(step.kind)
                reasons.append("MISSING: Part # or Confirmed W×H×D Envelope")
        elif item.tag and step.from_ref and step.from_ref.split(":", 1)[0] == item.tag:
            if step.kind == "trace_wire":
                tokens.append("wire_landing")
                wn = f" #{step.wire_number}" if step.wire_number else ""
                msg = f"MISSING: Wire{wn} Landing from {step.from_ref}"
                if msg not in reasons:
                    reasons.append(msg)

    # Check print mismatches referencing this device's tag
    if item.tag:
        for mismatch in store.mismatches:
            ref = getattr(mismatch, "ref", "") or getattr(mismatch, "photo_tag", "") or ""
            detail = getattr(mismatch, "detail", "") or getattr(mismatch, "message", "") or getattr(mismatch, "check", "")
            if ref == item.tag or ref.startswith(f"{item.tag}:"):
                tokens.append("print_mismatch")
                msg = f"MISMATCH: {detail}"
                if msg not in reasons:
                    reasons.append(msg)

    # Deduplicate tokens while keeping order
    seen: set[str] = set()
    dedup_tokens: list[str] = []
    for tok in tokens + [f for f in raw_fields if f not in tokens and not ready]:
        if tok not in seen:
            seen.add(tok)
            dedup_tokens.append(tok)

    return dedup_tokens, reasons, needs_closeup


def build_device_feedback_box(
    store: Reconstruction,
    photo: Photo,
    item: ObservedItem,
) -> DeviceFeedbackBox:
    ready = item_is_layout_ready(item)
    tokens, reasons, needs_closeup = _device_missing_analysis(store, photo, item)
    closeup_id = str(getattr(item, "closeup_photo_id", "") or "").strip()
    # A device is "confirmed" only if it is layout_ready AND has no open missing reasons
    is_confirmed = bool(ready and not reasons and (not item.unreadable or closeup_id))
    category: FeedbackStatusCategory = "confirmed" if is_confirmed else "missing_info"
    dims = _item_dimensions_label(item)

    ident_tag = item.tag or item.placed_ref or item.id
    part_desc = " ".join(x for x in (item.brand, item.part_number) if x).strip()
    if not item.part_number and item.label_text:
        lbl = item.label_text.strip()
        if lbl and lbl.lower() not in part_desc.lower():
            part_desc = f"{part_desc} · {lbl}" if part_desc else lbl

    if is_confirmed:
        header = f"✓ {ident_tag}"
        if part_desc:
            header += f" · {part_desc}"
        badge_lines = []
        if part_desc:
            badge_lines.append(f"Part: {part_desc}")
        if dims:
            badge_lines.append(f"Size: {dims}")
        if closeup_id:
            badge_lines.append(f"Close-Up Linked: {closeup_id}")
    else:
        header = f"⚠ {ident_tag}"
        if part_desc:
            header += f" · {part_desc}"
        else:
            header += " · INFO NEEDED"
        badge_lines = list(reasons) if reasons else ["MISSING: Part # or Verified W×H×D"]

    return DeviceFeedbackBox(
        item_id=item.id,
        photo_id=photo.id,
        tag=item.tag,
        role=item.role,
        status=item.status,
        status_category=category,
        layout_ready=ready,
        part_number=item.part_number,
        brand=item.brand,
        label_text=item.label_text,
        region={
            "x": float(item.region.x or 0.0),
            "y": float(item.region.y or 0.0),
            "w": float(item.region.w or 0.0),
            "h": float(item.region.h or 0.0),
        },
        missing_fields=tokens if not is_confirmed else [],
        missing_reasons=reasons if not is_confirmed else [],
        header_label=header,
        badge_lines=badge_lines,
        needs_closeup=needs_closeup if not is_confirmed else False,
        closeup_photo_id=closeup_id,
        placed_ref=item.placed_ref,
        confidence=float(item.confidence or 0.0),
        dimensions_label=dims,
        envelope_w_mm=item.envelope_w_mm,
        envelope_h_mm=item.envelope_h_mm,
        envelope_d_mm=item.envelope_d_mm,
        mount=item.mount,
        confirmed_summary=list(badge_lines) if is_confirmed else [],
        checklist_prompts=[
            s.prompt for s in store.checklist if s.status == "open" and s.item_id == item.id and s.prompt
        ],
    )


def _build_on_site_shot_list(
    photo: Photo,
    all_boxes: list[DeviceFeedbackBox],
    unboxed_callouts: list[dict[str, Any]] | None = None,
) -> tuple[bool, SiteExitStatus, str, list[str]]:
    """Compute (safe_to_leave_site, site_exit_status, site_exit_banner, on_site_shot_list)."""
    shot_list: list[str] = []
    unboxed = list(unboxed_callouts or [])

    if not all_boxes and not unboxed:
        if photo.unreadable:
            reason = photo.vision_note or "glare / blur / perspective skew"
            shot_list.append(
                f"Reshoot {photo.original_name or photo.id} square-on out of glare ({reason})"
            )
            shot_list.append(
                "Or drag boxes around cabinet devices and attach close-up nameplate photos before leaving site."
            )
            return (
                False,
                "do_not_leave",
                f"⚠ DO NOT LEAVE SITE YET — Photo {photo.id} Unreadable / 0 Devices Captured",
                shot_list,
            )
        shot_list.append(
            "Run 'Check Site-Exit Readiness (Analyze Capture)' or drag boxes around cabinet devices before leaving site."
        )
        return (
            False,
            "needs_analysis",
            "⚠ CAPTURE NOT ANALYZED YET — Check Site-Exit Readiness Before Leaving Cabinet",
            shot_list,
        )

    for b in all_boxes:
        if b.status_category != "missing_info":
            continue
        ident = b.tag or b.placed_ref or b.item_id
        desc = " ".join(x for x in (b.brand, b.part_number) if x).strip() or b.label_text or "device"
        actions: list[str] = []
        if b.needs_closeup:
            actions.append("take close-up photo of nameplate/side label")
        if "tag" in b.missing_fields:
            actions.append("confirm device tag (e.g. K1, PSU1, F1)")
        if "part_number" in b.missing_fields or "catalog_match" in b.missing_fields:
            actions.append("record Part # / Catalog SKU")
        if any(k in b.missing_fields for k in ("width_mm", "height_mm", "depth_mm", "mount")):
            actions.append("verify W×H×D mm & mounting")
        for r in b.missing_reasons:
            if "Wire" in r and "Landing" in r:
                actions.append(r.replace("MISSING: ", "").lower())
        action_str = ", ".join(actions) if actions else "; ".join(b.missing_reasons)
        shot_list.append(f"[{ident} · {desc}] Before leaving site: {action_str}")

    for callout in unboxed:
        lbl = str(callout.get("label") or "Suspected unboxed device on rail")
        dtl = str(callout.get("detail") or "Drag a box on photo & capture close-up")
        shot_list.append(f"[UNBOXED GAP · {lbl}] {dtl}")

    issue_count = sum(1 for b in all_boxes if b.status_category == "missing_info") + len(unboxed)
    if issue_count > 0:
        banner = (
            f"⚠ DO NOT LEAVE SITE YET — {issue_count} Device Close-Up(s) / Context Item(s) Needed "
            f"({len(all_boxes) - sum(1 for b in all_boxes if b.status_category == 'missing_info')}/{len(all_boxes)} Ready)"
        )
        return False, "do_not_leave", banner, shot_list

    banner = (
        f"✓ SITE CAPTURE READY — All {len(all_boxes)} Detected Devices Have Legible Context "
        f"(Safe to Leave Site)"
    )
    return True, "ready", banner, []


def build_photo_feedback_model(
    target: Any,
    photo_id: str | None = None,
    *,
    filter_mode: FeedbackFilterMode = "all",
    include_terminals: bool = False,
    unboxed_callouts: list[dict[str, Any]] | None = None,
) -> PhotoFeedbackReport:
    if filter_mode not in {"all", "missing_only", "confirmed_only"}:
        raise ValueError("filter_mode must be 'all', 'missing_only', or 'confirmed_only'")
    store, session, base_path = _resolve_store(target)
    rebuild_checklist(store)
    photo = _pick_photo(store, photo_id)
    resolved = resolve_path(photo.path, base_path)
    if not resolved.is_file() and photo.source_path and Path(photo.source_path).is_file():
        resolved = Path(photo.source_path)

    allowed_roles = {"part", "terminal"} if include_terminals else {"part"}
    all_boxes: list[DeviceFeedbackBox] = []
    for item in store.items:
        if item.photo_id != photo.id or item.role not in allowed_roles:
            continue
        if float(item.region.w or 0) <= 0 or float(item.region.h or 0) <= 0:
            continue
        all_boxes.append(build_device_feedback_box(store, photo, item))

    confirmed_count = sum(1 for b in all_boxes if b.status_category == "confirmed")
    missing_count = sum(1 for b in all_boxes if b.status_category == "missing_info")
    closeup_needed_count = sum(1 for b in all_boxes if b.needs_closeup)
    total_detected = len(all_boxes)
    unboxed_list = list(unboxed_callouts or [])

    safe_to_leave, exit_status, exit_banner, shot_list = _build_on_site_shot_list(
        photo, all_boxes, unboxed_list
    )

    if filter_mode == "missing_only":
        filtered = [b for b in all_boxes if b.status_category == "missing_info"]
    elif filter_mode == "confirmed_only":
        filtered = [b for b in all_boxes if b.status_category == "confirmed"]
    else:
        filtered = all_boxes

    ratio = mm_per_px(photo)
    scaled = ratio is not None
    photo_name = str(photo.original_name or "").strip() or Path(str(photo.path or photo.id)).name
    summary = (
        f"{exit_banner}  |  {photo_name}: {total_detected} detected "
        f"({confirmed_count} confirmed ✓, {missing_count} missing info ⚠). "
        f"{MISSED_DEVICE_PROMPT}"
    )

    return PhotoFeedbackReport(
        photo_id=photo.id,
        photo_name=photo_name,
        photo_role=photo.role,
        photo_path=str(resolved),
        width_px=int(photo.width_px or 0),
        height_px=int(photo.height_px or 0),
        scaled=scaled,
        mm_per_px=ratio,
        filter_mode=filter_mode,
        total_detected=total_detected,
        confirmed_count=confirmed_count,
        missing_info_count=missing_count,
        closeup_needed_count=closeup_needed_count,
        unboxed_count=len(unboxed_list),
        safe_to_leave_site=safe_to_leave,
        site_exit_status=exit_status,
        site_exit_banner=exit_banner,
        on_site_shot_list=shot_list,
        boxes=filtered,
        summary_text=summary,
        missed_device_prompt=MISSED_DEVICE_PROMPT,
    )


def build_photo_feedback_report(
    target: Any,
    photo_id: str | None = None,
    *,
    filter_mode: FeedbackFilterMode = "all",
    include_terminals: bool = False,
    unboxed_callouts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report = build_photo_feedback_model(
        target,
        photo_id=photo_id,
        filter_mode=filter_mode,
        include_terminals=include_terminals,
        unboxed_callouts=unboxed_callouts,
    )
    return report.model_dump(mode="json")


def _parse_region(
    region: dict[str, Any] | tuple[float, float, float, float] | list[float],
    width_px: int,
    height_px: int,
) -> PhotoRegion:
    w_img = float(max(width_px, 1))
    h_img = float(max(height_px, 1))
    if isinstance(region, (tuple, list)):
        if len(region) != 4:
            raise ValueError("region tuple/list must have 4 numbers: (x, y, w, h)")
        x, y, w, h = (float(v) for v in region)
    elif isinstance(region, dict):
        if all(k in region for k in ("nx", "ny", "nw", "nh")):
            x = float(region["nx"]) * w_img
            y = float(region["ny"]) * h_img
            w = float(region["nw"]) * w_img
            h = float(region["nh"]) * h_img
        else:
            x = float(region.get("x", 0.0))
            y = float(region.get("y", 0.0))
            w = float(region.get("w", 0.0))
            h = float(region.get("h", 0.0))
    else:
        raise ValueError("region must be a dict {'x','y','w','h'} or a 4-tuple (x, y, w, h)")

    if w < 0:
        x = x + w
        w = abs(w)
    if h < 0:
        y = y + h
        h = abs(h)
    if w < 4.0 or h < 4.0:
        raise ValueError("bounding box is too small — drag a box around the missed device")
    if width_px > 0:
        x = max(0.0, min(x, w_img - 2.0))
        w = min(w, w_img - x)
    if height_px > 0:
        y = max(0.0, min(y, h_img - 2.0))
        h = min(h, h_img - y)
    return PhotoRegion(x=round(x, 2), y=round(y, 2), w=round(w, 2), h=round(h, 2))


def add_missed_device_on_photo(
    session: Any,
    photo_id: str,
    region: dict[str, Any] | tuple[float, float, float, float] | list[float],
    *,
    tag: str = "",
    part_number: str = "",
    brand: str = "",
    label_text: str = "",
    envelope_w_mm: float | None = None,
    envelope_h_mm: float | None = None,
    envelope_d_mm: float | None = None,
    mount: str = "",
    library_id: str = "",
    note: str = "",
    closeup_photo_id: str = "",
    snap_to_edges: bool = False,
) -> dict[str, Any]:
    """Register a physical device on a photo that automated detection missed."""
    from panelcad.reconstruct.service import confirm_item, place_needs_info

    store = ensure_reconstruction(session.project)
    photo = store.photo(photo_id)
    box = _parse_region(region, photo.width_px, photo.height_px)
    if snap_to_edges:
        base_path = getattr(session, "path", None)
        resolved = resolve_path(photo.path, base_path)
        if not resolved.is_file() and photo.source_path and Path(photo.source_path).is_file():
            resolved = Path(photo.source_path)
        if resolved.is_file():
            rx, ry, rw, rh = refine_device_box_on_photo(resolved, (box.x, box.y, box.w, box.h))
            box = _parse_region((rx, ry, rw, rh), photo.width_px, photo.height_px)
    item_id = next_id("i", [existing.id for existing in store.items])
    clean_tag = str(tag or "").strip()
    clean_part = str(part_number or "").strip()
    clean_brand = str(brand or "").strip()
    clean_label = str(label_text or note or clean_part or clean_tag or "Missed device (human marked)").strip()

    item = ObservedItem(
        id=item_id,
        photo_id=photo.id,
        role="part",
        tag=clean_tag,
        brand=clean_brand,
        part_number=clean_part,
        label_text=clean_label,
        label_part_number=clean_part,
        label_brand=clean_brand,
        region=box,
        confidence=1.0,
        unreadable=False,
        closeup_photo_id=str(closeup_photo_id or "").strip(),
        status="needs_info",
    )
    if clean_part or clean_brand:
        item.matches = match_catalog(
            session.parts,
            part_number=clean_part,
            brand=clean_brand,
            devices=getattr(session, "catalog", None),
        )
    store.items.append(item)

    have_envelope = (
        envelope_w_mm is not None
        and envelope_h_mm is not None
        and envelope_d_mm is not None
        and float(envelope_w_mm) > 0
        and float(envelope_h_mm) > 0
        and float(envelope_d_mm) > 0
        and str(mount or "").strip() != ""
    )
    confirmed_ok = False
    if clean_part and item.matches:
        try:
            confirm_item(
                session,
                item.id,
                catalog=clean_part,
                manufacturer=clean_brand,
                tag=clean_tag,
                library_id=library_id,
            )
            confirmed_ok = item_is_layout_ready(item)
        except ValueError:
            confirmed_ok = False
    if not confirmed_ok and have_envelope:
        confirm_item(
            session,
            item.id,
            envelope_w_mm=envelope_w_mm,
            envelope_h_mm=envelope_h_mm,
            envelope_d_mm=envelope_d_mm,
            mount=mount,
            tag=clean_tag,
            library_id=library_id,
        )
        if clean_part:
            item.part_number = clean_part
            item.label_part_number = clean_part
        if clean_brand:
            item.brand = clean_brand
            item.label_brand = clean_brand

    if not item_is_layout_ready(item):
        try:
            place_needs_info(session, photo.id)
        except Exception:
            pass

    rebuild_checklist(store)
    feedback_box = build_device_feedback_box(store, photo, item)
    return {
        "item": public_item(item),
        "feedback_box": feedback_box.model_dump(mode="json"),
        "photo": public_photo(photo),
        "feedback": build_photo_feedback_report(session, photo_id=photo.id),
    }


def link_closeup_to_device(
    session: Any,
    item_id: str,
    *,
    closeup_photo_path: Path | str | None = None,
    closeup_photo_id: str = "",
    closeup_role: str = "nameplate",
    tag: str = "",
    part_number: str = "",
    brand: str = "",
    envelope_w_mm: float | None = None,
    envelope_h_mm: float | None = None,
    envelope_d_mm: float | None = None,
    mount: str = "",
    library_id: str = "",
) -> dict[str, Any]:
    """Attach an on-site close-up/nameplate photo and/or read label details to a flagged device during Capture."""
    from panelcad.reconstruct.service import add_photos, confirm_item

    store = ensure_reconstruction(session.project)
    item = store.item(item_id)
    linked_pid = str(closeup_photo_id or "").strip()

    if closeup_photo_path is not None:
        role_val = closeup_role if closeup_role in {"nameplate", "part_label", "terminal_strip", "wire_numbers", "other"} else "nameplate"
        added = add_photos(
            session,
            [closeup_photo_path],
            role=role_val,  # type: ignore[arg-type]
            note=f"Close-up for {item.tag or item.id}",
        )
        added_list = added.get("added") or []
        if added_list:
            linked_pid = str(added_list[0].get("id") or "")

    if linked_pid:
        item.closeup_photo_id = linked_pid
    item.unreadable = False

    clean_tag = str(tag or item.tag or "").strip()
    clean_part = str(part_number or item.part_number or "").strip()
    clean_brand = str(brand or item.brand or "").strip()
    clean_mount = str(mount or item.mount or "din_rail").strip()
    if clean_tag:
        item.tag = clean_tag
    if clean_part:
        item.part_number = clean_part
        item.label_part_number = clean_part
    if clean_brand:
        item.brand = clean_brand
        item.label_brand = clean_brand

    if clean_part or clean_brand:
        item.matches = match_catalog(
            session.parts,
            part_number=clean_part,
            brand=clean_brand,
            devices=getattr(session, "catalog", None),
        )

    confirmed_ok = False
    if clean_part and item.matches:
        try:
            confirm_item(
                session,
                item.id,
                catalog=clean_part,
                manufacturer=clean_brand,
                tag=clean_tag,
                library_id=library_id,
            )
            confirmed_ok = item_is_layout_ready(item)
        except ValueError:
            confirmed_ok = False

    w_val = envelope_w_mm if envelope_w_mm is not None else item.envelope_w_mm
    h_val = envelope_h_mm if envelope_h_mm is not None else item.envelope_h_mm
    d_val = envelope_d_mm if envelope_d_mm is not None else item.envelope_d_mm
    if not confirmed_ok and w_val and h_val and d_val and float(w_val) > 0 and float(h_val) > 0 and float(d_val) > 0:
        confirm_item(
            session,
            item.id,
            envelope_w_mm=float(w_val),
            envelope_h_mm=float(h_val),
            envelope_d_mm=float(d_val),
            mount=clean_mount,
            tag=clean_tag,
            library_id=library_id,
        )
        if clean_part:
            item.part_number = clean_part
            item.label_part_number = clean_part
        if clean_brand:
            item.brand = clean_brand
            item.label_brand = clean_brand

    rebuild_checklist(store)
    parent_photo = store.photo(item.photo_id)
    feedback_box = build_device_feedback_box(store, parent_photo, item)
    return {
        "item": public_item(item),
        "closeup_photo_id": item.closeup_photo_id,
        "feedback_box": feedback_box.model_dump(mode="json"),
        "feedback": build_photo_feedback_report(session, photo_id=parent_photo.id),
    }


def _ensure_qt_gui_app() -> None:
    import os
    import sys
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is not None:
        return
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QGuiApplication([])


def render_annotated_cabinet_photo(
    target: Any,
    photo_id: str | None = None,
    *,
    filter_mode: FeedbackFilterMode = "all",
    selected_item_id: str | None = None,
    unboxed_callouts: list[dict[str, Any]] | None = None,
    include_banner: bool = True,
    output_path: Path | str | None = None,
) -> Any:
    """Render the cabinet photo with color-coded device boxes, missing-info badges, and Site-Exit Readiness status."""
    _ensure_qt_gui_app()
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen

    report = build_photo_feedback_model(
        target,
        photo_id=photo_id,
        filter_mode=filter_mode,
        unboxed_callouts=unboxed_callouts,
    )
    img_path = Path(report.photo_path) if report.photo_path else None
    base_img: QImage | None = None
    if img_path and img_path.is_file():
        base_img = load_upright_image(img_path)
    if base_img is None or base_img.isNull():
        w = max(report.width_px or 800, 320)
        h = max(report.height_px or 600, 240)
        base_img = QImage(w, h, QImage.Format.Format_RGB32)
        base_img.fill(QColor("#2c2f33"))

    photo_w = base_img.width()
    photo_h = base_img.height()

    # Scale typography and stroke thicknesses proportionally for high-res real camera photos (e.g. 4080x3072)
    ui_scale = max(1.0, min(3.2, photo_w / 1280.0))
    top_banner_h = int(round(58 * ui_scale)) if include_banner else 0
    shot_lines = report.on_site_shot_list[:3]
    bottom_extra_lines = len(shot_lines) if (include_banner and not report.safe_to_leave_site) else 0
    bottom_banner_h = int(round((42 + bottom_extra_lines * 18) * ui_scale)) if include_banner else 0

    canvas = QImage(photo_w, photo_h + top_banner_h + bottom_banner_h, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(QColor("#14171a"))

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.drawImage(0, top_banner_h, base_img)

    # Top summary & Site-Exit Readiness banner
    if include_banner:
        if report.safe_to_leave_site:
            banner_bg = QColor(6, 78, 59, 248)
            banner_border = QColor("#10b981")
        else:
            banner_bg = QColor(127, 29, 29, 248)
            banner_border = QColor("#f59e0b")
        painter.fillRect(0, 0, photo_w, top_banner_h, banner_bg)
        painter.setPen(QPen(banner_border, max(2, int(round(2 * ui_scale)))))
        painter.drawLine(0, top_banner_h - 1, photo_w, top_banner_h - 1)

        pad_x = 14.0 * ui_scale
        title_font = QFont("DejaVu Sans", max(9, int(round(10 * ui_scale))), QFont.Weight.Bold)
        painter.setFont(title_font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(
            QRectF(pad_x, 5 * ui_scale, photo_w * 0.64, 24 * ui_scale),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            report.site_exit_banner,
        )

        sub_font = QFont("DejaVu Sans", max(8, int(round(8.5 * ui_scale))))
        painter.setFont(sub_font)
        painter.setPen(QColor("#e2e8f0"))
        role_label = report.photo_role.replace("_", " ").upper()
        scale_str = f"Scale: {report.mm_per_px:.3f} mm/px" if report.scaled and report.mm_per_px else "Scale: NOT SET"
        sub_str = (
            f"Photo: {report.photo_name} ({role_label})   |   "
            f"Detected: {report.total_detected}   |   "
            f"✓ Ready: {report.confirmed_count}   |   "
            f"⚠ Needs Context/Close-Up: {report.missing_info_count}   |   "
            f"{scale_str}"
        )
        painter.drawText(
            QRectF(pad_x, 30 * ui_scale, photo_w - pad_x * 2, 22 * ui_scale),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            sub_str,
        )

        # Legend pills on top-right
        _draw_pill(
            painter,
            x=max(photo_w - 320 * ui_scale, photo_w * 0.65),
            y=8 * ui_scale,
            text=f"✓ {report.confirmed_count} CONFIRMED",
            bg=QColor(16, 185, 129, 235),
            fg=QColor("#052e16"),
            ui_scale=ui_scale,
        )
        _draw_pill(
            painter,
            x=max(photo_w - 170 * ui_scale, photo_w * 0.81),
            y=8 * ui_scale,
            text=f"⚠ {report.missing_info_count} MISSING INFO",
            bg=QColor(245, 158, 11, 242),
            fg=QColor("#1e1b18"),
            ui_scale=ui_scale,
        )

    # Track occupied callout rectangles so callouts don't collide
    occupied: list[QRectF] = []
    for box_entry in report.boxes:
        r = box_entry.region
        occupied.append(
            QRectF(
                float(r["x"]),
                float(r["y"]) + top_banner_h,
                float(r["w"]),
                float(r["h"]),
            )
        )

    ordered_boxes = sorted(
        report.boxes,
        key=lambda b: (0 if b.status_category == "confirmed" else 1, b.item_id == selected_item_id),
    )

    for box_entry in ordered_boxes:
        r = box_entry.region
        bx = float(r["x"])
        by = float(r["y"]) + top_banner_h
        bw = max(float(r["w"]), 8.0)
        bh = max(float(r["h"]), 8.0)
        rect = QRectF(bx, by, bw, bh)
        is_selected = bool(selected_item_id and box_entry.item_id == selected_item_id)

        if box_entry.status_category == "confirmed":
            stroke_color = QColor("#10b981")
            accent_color = QColor("#34d399")
            fill_color = QColor(16, 185, 129, 40 if not is_selected else 72)
            header_bg = QColor(6, 95, 70, 238)
            header_fg = QColor("#ecfdf5")
        else:
            stroke_color = QColor("#ff4d4f")
            accent_color = QColor("#fbbf24")
            fill_color = QColor(255, 77, 79, 52 if not is_selected else 88)
            header_bg = QColor(185, 28, 28, 244)
            header_fg = QColor("#fffbeb")

        painter.setPen(QPen(QColor(0, 0, 0, 195), (5.5 if is_selected else 4.2) * ui_scale))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

        painter.fillRect(rect, fill_color)
        pen_width = (3.8 if is_selected else (3.2 if box_entry.status_category == "missing_info" else 2.5)) * ui_scale
        painter.setPen(QPen(stroke_color, pen_width))
        painter.drawRect(rect)

        bracket = min(16.0 * ui_scale, bw * 0.28, bh * 0.28)
        painter.setPen(QPen(accent_color, 3.2 * ui_scale))
        painter.drawLine(QPointF(bx, by), QPointF(bx + bracket, by))
        painter.drawLine(QPointF(bx, by), QPointF(bx, by + bracket))
        painter.drawLine(QPointF(bx + bw, by), QPointF(bx + bw - bracket, by))
        painter.drawLine(QPointF(bx + bw, by), QPointF(bx + bw, by + bracket))
        painter.drawLine(QPointF(bx, by + bh), QPointF(bx + bracket, by + bh))
        painter.drawLine(QPointF(bx, by + bh), QPointF(bx, by + bh - bracket))
        painter.drawLine(QPointF(bx + bw, by + bh), QPointF(bx + bw - bracket, by + bh))
        painter.drawLine(QPointF(bx + bw, by + bh), QPointF(bx + bw, by + bh - bracket))

        _draw_device_callout_card(
            painter,
            box_rect=rect,
            header_text=box_entry.header_label,
            lines=box_entry.badge_lines,
            header_bg=header_bg,
            header_fg=header_fg,
            border_color=accent_color if box_entry.status_category == "missing_info" else stroke_color,
            is_missing=(box_entry.status_category == "missing_info"),
            min_y=float(top_banner_h + 4 * ui_scale),
            max_y=float(top_banner_h + photo_h - 4 * ui_scale),
            max_x=float(photo_w - 4 * ui_scale),
            occupied=occupied,
            ui_scale=ui_scale,
        )

    for raw_callout in unboxed_callouts or []:
        cx = float(raw_callout.get("x", 0.0))
        cy = float(raw_callout.get("y", 0.0)) + top_banner_h
        cw = max(float(raw_callout.get("w", 40.0)), 12.0)
        ch = max(float(raw_callout.get("h", 40.0)), 12.0)
        crect = QRectF(cx, cy, cw, ch)
        label = str(raw_callout.get("label") or "? UNBOXED DEVICE — MISSED BY BOT")
        detail = str(raw_callout.get("detail") or "Click/drag box here to add missed device")
        dash_pen = QPen(QColor("#38bdf8"), 3.0 * ui_scale, Qt.PenStyle.DashLine)
        painter.setPen(QPen(QColor(0, 0, 0, 185), 4.8 * ui_scale))
        painter.drawRect(crect)
        painter.fillRect(crect, QColor(56, 189, 248, 42))
        painter.setPen(dash_pen)
        painter.drawRect(crect)
        _draw_device_callout_card(
            painter,
            box_rect=crect,
            header_text=label,
            lines=[detail],
            header_bg=QColor(3, 105, 161, 242),
            header_fg=QColor("#f0f9ff"),
            border_color=QColor("#38bdf8"),
            is_missing=True,
            min_y=float(top_banner_h + 4 * ui_scale),
            max_y=float(top_banner_h + photo_h - 4 * ui_scale),
            max_x=float(photo_w - 4 * ui_scale),
            occupied=occupied,
            ui_scale=ui_scale,
        )

    # Bottom guidance + Before-You-Leave-Site shot list banner
    if include_banner:
        by0 = top_banner_h + photo_h
        painter.fillRect(0, by0, photo_w, bottom_banner_h, QColor(18, 22, 28, 248))
        painter.setPen(QPen(QColor("#334155"), max(1, int(round(ui_scale)))))
        painter.drawLine(0, by0, photo_w, by0)
        b_font = QFont("DejaVu Sans", max(8, int(round(8.2 * ui_scale))), QFont.Weight.Bold)
        painter.setFont(b_font)
        painter.setPen(QColor("#fde68a") if not report.safe_to_leave_site else QColor("#86efac"))
        pad_x = 14.0 * ui_scale
        painter.drawText(
            QRectF(pad_x, by0 + 4 * ui_scale, photo_w - pad_x * 2, 22 * ui_scale),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "BEFORE YOU LEAVE SITE: Green = Confirmed (✓)  |  Red/Amber = Needs Close-Up / Info (⚠)  |  "
            "NO BOX = Missed by bot/software (drag a box on the photo to add it).",
        )
        if bottom_extra_lines > 0:
            s_font = QFont("DejaVu Sans", max(7, int(round(7.8 * ui_scale))))
            painter.setFont(s_font)
            painter.setPen(QColor("#fecaca"))
            for idx, shot in enumerate(shot_lines):
                y_line = by0 + (24 + idx * 17) * ui_scale
                painter.drawText(
                    QRectF(pad_x, y_line, photo_w - pad_x * 2, 17 * ui_scale),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    f"• {shot}",
                )

    painter.end()

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(str(out), "PNG")

    return canvas


def _draw_pill(
    painter: Any,
    *,
    x: float,
    y: float,
    text: str,
    bg: Any,
    fg: Any,
    ui_scale: float = 1.0,
) -> None:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QFont, QFontMetrics, QPainterPath

    font = QFont("DejaVu Sans", max(8, int(round(8 * ui_scale))), QFont.Weight.Bold)
    painter.setFont(font)
    fm = QFontMetrics(font)
    w = fm.horizontalAdvance(text) + 18 * ui_scale
    h = 22.0 * ui_scale
    rect = QRectF(x, y, w, h)
    path = QPainterPath()
    path.addRoundedRect(rect, 5.0 * ui_scale, 5.0 * ui_scale)
    painter.fillPath(path, bg)
    painter.setPen(fg)
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def _draw_device_callout_card(
    painter: Any,
    *,
    box_rect: Any,
    header_text: str,
    lines: list[str],
    header_bg: Any,
    header_fg: Any,
    border_color: Any,
    is_missing: bool,
    min_y: float,
    max_y: float,
    max_x: float,
    occupied: list[Any],
    ui_scale: float = 1.0,
) -> None:
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainterPath, QPen

    head_font = QFont("DejaVu Sans", max(8, int(round(8 * ui_scale))), QFont.Weight.Bold)
    body_font = QFont(
        "DejaVu Sans",
        max(7, int(round(7 * ui_scale))),
        QFont.Weight.Bold if is_missing else QFont.Weight.Normal,
    )
    fm_head = QFontMetrics(head_font)
    fm_body = QFontMetrics(body_font)

    shown_lines = lines[:4]
    pad_x = 7.0 * ui_scale
    head_h = 19.0 * ui_scale
    line_h = 15.0 * ui_scale
    body_h = (len(shown_lines) * line_h + 6.0 * ui_scale) if shown_lines else 0.0
    card_h = head_h + body_h

    max_text_w = fm_head.horizontalAdvance(header_text)
    for ln in shown_lines:
        max_text_w = max(max_text_w, fm_body.horizontalAdvance(ln))
    card_w = min(
        max(float(max_text_w) + pad_x * 2 + 6.0 * ui_scale, 128.0 * ui_scale),
        max(220.0 * ui_scale, max_x * 0.44),
    )

    candidates = [
        QRectF(box_rect.left(), box_rect.top() - card_h - 6.0 * ui_scale, card_w, card_h),
        QRectF(box_rect.left(), box_rect.bottom() + 6.0 * ui_scale, card_w, card_h),
        QRectF(box_rect.right() + 8.0 * ui_scale, box_rect.top(), card_w, card_h),
        QRectF(box_rect.left() - card_w - 8.0 * ui_scale, box_rect.top(), card_w, card_h),
    ]

    chosen: QRectF | None = None
    for cand in candidates:
        cx = max(6.0 * ui_scale, min(cand.left(), max_x - card_w))
        cy = max(min_y, min(cand.top(), max_y - card_h))
        clamped = QRectF(cx, cy, card_w, card_h)
        if not any(clamped.intersects(other) for other in occupied):
            chosen = clamped
            break

    if chosen is None:
        best_score = float("inf")
        for cand in candidates:
            cx = max(6.0 * ui_scale, min(cand.left(), max_x - card_w))
            cy = max(min_y, min(cand.top(), max_y - card_h))
            clamped = QRectF(cx, cy, card_w, card_h)
            overlap = 0.0
            for other in occupied:
                inter = clamped.intersected(other)
                if not inter.isEmpty():
                    overlap += inter.width() * inter.height()
            if overlap < best_score:
                best_score = overlap
                chosen = clamped

    assert chosen is not None
    occupied.append(chosen)

    card_center = chosen.center()
    if not chosen.intersects(box_rect.adjusted(-4 * ui_scale, -4 * ui_scale, 4 * ui_scale, 4 * ui_scale)):
        anchor_x = max(box_rect.left(), min(card_center.x(), box_rect.right()))
        anchor_y = max(box_rect.top(), min(card_center.y(), box_rect.bottom()))
        target_x = max(chosen.left(), min(anchor_x, chosen.right()))
        target_y = max(chosen.top(), min(anchor_y, chosen.bottom()))
        painter.setPen(QPen(QColor(0, 0, 0, 180), 3.0 * ui_scale))
        painter.drawLine(QPointF(anchor_x, anchor_y), QPointF(target_x, target_y))
        painter.setPen(QPen(border_color, 1.8 * ui_scale))
        painter.drawLine(QPointF(anchor_x, anchor_y), QPointF(target_x, target_y))

    path = QPainterPath()
    path.addRoundedRect(chosen, 5.0 * ui_scale, 5.0 * ui_scale)
    painter.fillPath(path, QColor(15, 23, 42, 238))

    head_rect = QRectF(chosen.left(), chosen.top(), chosen.width(), head_h)
    head_path = QPainterPath()
    head_path.addRoundedRect(head_rect, 5.0 * ui_scale, 5.0 * ui_scale)
    painter.fillPath(head_path, header_bg)

    painter.setPen(QPen(border_color, (1.8 if is_missing else 1.4) * ui_scale))
    painter.drawRoundedRect(chosen, 5.0 * ui_scale, 5.0 * ui_scale)

    painter.setFont(head_font)
    painter.setPen(header_fg)
    elided_head = fm_head.elidedText(header_text, Qt.TextElideMode.ElideRight, int(chosen.width() - pad_x * 2))
    painter.drawText(
        QRectF(chosen.left() + pad_x, chosen.top(), chosen.width() - pad_x * 2, head_h),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        elided_head,
    )

    if shown_lines:
        painter.setFont(body_font)
        for idx, line in enumerate(shown_lines):
            ly = chosen.top() + head_h + 3.0 * ui_scale + idx * line_h
            if line.startswith("MISSING:") or line.startswith("MISMATCH:"):
                painter.setPen(QColor("#fde047"))
            else:
                painter.setPen(QColor("#a7f3d0"))
            elided = fm_body.elidedText(line, Qt.TextElideMode.ElideRight, int(chosen.width() - pad_x * 2))
            painter.drawText(
                QRectF(chosen.left() + pad_x, ly, chosen.width() - pad_x * 2, line_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided,
            )


def export_photo_feedback(
    target: Any,
    output_path: Path | str,
    photo_id: str | None = None,
    *,
    filter_mode: FeedbackFilterMode = "all",
    selected_item_id: str | None = None,
    unboxed_callouts: list[dict[str, Any]] | None = None,
    include_banner: bool = True,
) -> dict[str, Any]:
    """Render and save the annotated cabinet photo feedback PNG and return its report."""
    out = Path(output_path)
    report = build_photo_feedback_report(
        target,
        photo_id=photo_id,
        filter_mode=filter_mode,
        unboxed_callouts=unboxed_callouts,
    )
    image = render_annotated_cabinet_photo(
        target,
        photo_id=photo_id,
        filter_mode=filter_mode,
        selected_item_id=selected_item_id,
        unboxed_callouts=unboxed_callouts,
        include_banner=include_banner,
        output_path=out,
    )
    return {
        **report,
        "output_path": str(out),
        "exported_width_px": int(image.width()),
        "exported_height_px": int(image.height()),
    }


def refine_device_box_on_photo(
    image_path: Path | str,
    box: tuple[float, float, float, float] | list[float] | dict[str, float],
    *,
    search_margin_ratio: float = 0.12,
) -> tuple[int, int, int, int]:
    """Tighten a coarse AI or user-drawn bounding box to physical component housing edges/seams.

    Analyzes local multi-scale color/Sobel gradients and dark mechanical module seam grooves
    near the left, right, top, and bottom borders of ``box`` while guarding against collapsing
    onto internal text labels.
    """
    _ensure_qt_gui_app()
    from PySide6.QtGui import QImage

    if isinstance(box, dict):
        bx = float(box.get("x", 0.0))
        by = float(box.get("y", 0.0))
        bw = float(box.get("w", 0.0))
        bh = float(box.get("h", 0.0))
    else:
        if len(box) != 4:
            raise ValueError("box must have 4 elements: (x, y, w, h)")
        bx, by, bw, bh = (float(v) for v in box)

    if bw < 0:
        bx += bw
        bw = abs(bw)
    if bh < 0:
        by += bh
        bh = abs(bh)

    img_file = Path(image_path) if image_path else None
    if img_file is None or not img_file.is_file() or bw < 8.0 or bh < 8.0:
        return (int(round(bx)), int(round(by)), max(4, int(round(bw))), max(4, int(round(bh))))

    img = load_upright_image(img_file)
    if img.isNull():
        return (int(round(bx)), int(round(by)), max(4, int(round(bw))), max(4, int(round(bh))))

    img = img.convertToFormat(QImage.Format.Format_RGB32)
    W, H = img.width(), img.height()
    x0 = max(0, min(int(round(bx)), W - 4))
    y0 = max(0, min(int(round(by)), H - 4))
    x1 = max(x0 + 4, min(int(round(bx + bw)), W - 1))
    y1 = max(y0 + 4, min(int(round(by + bh)), H - 1))
    cw = x1 - x0
    ch = y1 - y0

    ratio = max(0.02, min(0.35, float(search_margin_ratio)))
    mx = max(4, min(85, int(round(cw * ratio))))
    my = max(4, min(85, int(round(ch * ratio))))

    cy0 = max(4, int(round(y0 + ch * 0.14)))
    cy1 = min(H - 5, int(round(y1 - ch * 0.14)))
    cx0 = max(4, int(round(x0 + cw * 0.14)))
    cx1 = min(W - 5, int(round(x1 - cw * 0.14)))

    def _lum(px: int, py: int) -> float:
        c = img.pixel(px, py)
        return 0.299 * ((c >> 16) & 255) + 0.587 * ((c >> 8) & 255) + 0.114 * (c & 255)

    def _col_diff(px1: int, py1: int, px2: int, py2: int) -> float:
        c1 = img.pixel(px1, py1)
        c2 = img.pixel(px2, py2)
        dr = abs(((c1 >> 16) & 255) - ((c2 >> 16) & 255))
        dg = abs(((c1 >> 8) & 255) - ((c2 >> 8) & 255))
        db = abs((c1 & 255) - (c2 & 255))
        return 0.35 * dr + 0.45 * dg + 0.20 * db

    def _v_score(x: int) -> float:
        if x < 5 or x >= W - 5 or cy1 <= cy0:
            return 0.0
        ys = range(cy0, cy1 + 1, max(1, (cy1 - cy0) // 42))
        s = 0.0
        count = 0
        for y in ys:
            g_local = 0.55 * _col_diff(x + 1, y, x - 1, y) + 0.45 * _col_diff(x + 3, y, x - 3, y)
            l0 = _lum(x, y)
            l_left = 0.5 * (_lum(x - 2, y) + _lum(x - 4, y))
            l_right = 0.5 * (_lum(x + 2, y) + _lum(x + 4, y))
            step_edge = abs(l_left - l_right)
            valley = max(0.0, min(l_left, l_right) - l0) * 2.4
            s += g_local + 0.65 * step_edge + valley
            count += 1
        return s / max(1, count)

    def _h_score(y: int) -> float:
        if y < 5 or y >= H - 5 or cx1 <= cx0:
            return 0.0
        xs = range(cx0, cx1 + 1, max(1, (cx1 - cx0) // 42))
        s = 0.0
        count = 0
        for x in xs:
            g_local = 0.55 * _col_diff(x, y + 1, x, y - 1) + 0.45 * _col_diff(x, y + 3, x, y - 3)
            l0 = _lum(x, y)
            l_top = 0.5 * (_lum(x, y - 2) + _lum(x, y - 4))
            l_bot = 0.5 * (_lum(x, y + 2) + _lum(x, y + 4))
            step_edge = abs(l_top - l_bot)
            valley = max(0.0, min(l_top, l_bot) - l0) * 2.0
            s += g_local + 0.65 * step_edge + valley
            count += 1
        return s / max(1, count)

    def _pick_edge(center: int, radius: int, limit: int, scorer: Any) -> int:
        lo = max(5, center - radius)
        hi = min(limit - 6, center + radius)
        if hi <= lo:
            return center
        raw = {pos: scorer(pos) for pos in range(lo, hi + 1)}
        smoothed: dict[int, float] = {}
        for pos in range(lo, hi + 1):
            vals = [raw[p] for p in (pos - 1, pos, pos + 1) if p in raw]
            mean_val = sum(vals) / len(vals)
            norm_d = abs(pos - center) / max(1.0, float(radius))
            prior = 1.0 - 0.34 * (norm_d ** 1.35)
            smoothed[pos] = mean_val * prior
        best = max(smoothed, key=smoothed.get)  # type: ignore[arg-type]
        base = smoothed.get(center, 0.0)
        if smoothed[best] < 6.0 or smoothed[best] < base * 1.04:
            return center
        return int(best)

    nx0 = _pick_edge(x0, mx, W, _v_score)
    nx1 = _pick_edge(x1, mx, W, _v_score)
    ny0 = _pick_edge(y0, my, H, _h_score)
    ny1 = _pick_edge(y1, my, H, _h_score)

    if nx1 - nx0 < max(10, int(cw * 0.58)):
        nx0, nx1 = x0, x1
    if ny1 - ny0 < max(10, int(ch * 0.58)):
        ny0, ny1 = y0, y1
    return (int(nx0), int(ny0), int(nx1 - nx0), int(ny1 - ny0))


snap_drawn_box_to_device_edges = refine_device_box_on_photo


def update_device_box_on_photo(
    session: Any,
    item_id: str,
    *,
    box: dict[str, Any] | tuple[float, float, float, float] | list[float] | None = None,
    snap_to_edges: bool = False,
    search_margin_ratio: float = 0.12,
) -> dict[str, Any]:
    """Update or edge-snap an existing device bounding box on a cabinet photo."""
    store = ensure_reconstruction(session.project)
    item = store.item(item_id)
    photo = store.photo(item.photo_id)
    base_path = getattr(session, "path", None)
    resolved = resolve_path(photo.path, base_path)
    if not resolved.is_file() and photo.source_path and Path(photo.source_path).is_file():
        resolved = Path(photo.source_path)

    if box is not None:
        parsed = _parse_region(box, photo.width_px, photo.height_px)
    else:
        parsed = PhotoRegion(
            x=float(item.region.x or 0.0),
            y=float(item.region.y or 0.0),
            w=float(item.region.w or 0.0),
            h=float(item.region.h or 0.0),
        )

    if snap_to_edges and resolved.is_file():
        rx, ry, rw, rh = refine_device_box_on_photo(
            resolved,
            (parsed.x, parsed.y, parsed.w, parsed.h),
            search_margin_ratio=search_margin_ratio,
        )
        parsed = _parse_region((rx, ry, rw, rh), photo.width_px, photo.height_px)

    item.region = parsed
    rebuild_checklist(store)
    feedback_box = build_device_feedback_box(store, photo, item)
    return {
        "item": public_item(item),
        "region": {"x": parsed.x, "y": parsed.y, "w": parsed.w, "h": parsed.h},
        "snapped": bool(snap_to_edges),
        "feedback_box": feedback_box.model_dump(mode="json"),
        "feedback": build_photo_feedback_report(session, photo_id=photo.id),
    }
