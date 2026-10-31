"""Find DIN-rail terminal strips on a cabinet photo and fill the strip model.

Detection is geometric: a row of tall, narrow housings, split where a gap or an
end stop breaks the row. Green/yellow housings are PE. A saturated bar across
the tops of neighbouring blocks is a jumper comb. Thin pieces at the ends are
end plates. Marker tags and wire numbers are read from the photo when the
glyphs are clear. Anything that cannot be read stays ``?``. No wire number,
tag, block, or millimetre is invented, and no generic placeholder size is
written onto the part.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from panelcad.core.models import Wire
from panelcad.reconstruct.models import ObservedItem, Photo, PhotoRegion, Reconstruction
from panelcad.reconstruct.store import next_id, resolve_path

UNKNOWN = "?"
STRIP_LABEL = "terminal strip"
BLOCK_LABEL = "terminal block"
PANEL_SIDE = "panel side"
FIELD_SIDE = "field side"
_PHOTO_FLAG = "photo_terminal"
_USER_FLAG = "photo_user"
_GROUP_FLAG = "photo_group"

# Feed-through symbol with no panel millimetres. A catalog SKU is the only
# source of a real block size; this intake does not supply one.
_TERMINAL_LIBRARY = "iec.terminals.feed_through"
_END_PLATE_LIBRARY = "iec.accessories.end_plate"
_END_STOP_LIBRARY = "iec.accessories.end_stop"
_OFFPAGE_LIBRARY = "iec.connection.offpage"

_BLANK_TEXT = {"", "?", "??", "unknown", "n/a", "na", "none", "unreadable", "-", "null"}

# Auto-detect stays quiet unless a row looks like terminal blocks: several narrow
# housings of one pitch, aligned, with a screw or spring point in each. A light,
# green, or yellow patch is not enough. Missing a strip is acceptable — the
# tech can draw the box.
_MIN_BLOCKS = 4
_MIN_ASPECT = 3.0
_MAX_ASPECT = 16.0
_MAX_PITCH_CV = 0.2
_MAX_WIDTH_CV = 0.18
_FASTENER_SHARE = 0.75
_HIGH_CONFIDENCE = 0.85
_SUGGESTION_CONFIDENCE = 0.62
# A strip tag or a wire number, not a word or a relay part number.
# Letter tags are X / XT / TB and the same family. RM85 is a part number, not a strip.
_TAG_RE = re.compile(
    r"^(?:PE|GND|(?:XT|TB|TS|TD|XK|XL|XM|XN|XU|XP|X)[0-9]{1,4}(?:-[0-9]{1,3})?|[0-9]{1,5})$"
)
_COUNT_PROMPT = "how many blocks?"


@dataclass
class BlockHit:
    x: int
    y: int
    w: int
    h: int
    pe: bool = False
    end_plate: bool = False
    marker: str = UNKNOWN
    panel_wire: str = UNKNOWN
    field_wire: str = UNKNOWN


@dataclass
class StripHit:
    x: int
    y: int
    w: int
    h: int
    name: str = UNKNOWN
    blocks: list[BlockHit] = field(default_factory=list)
    end_plate_left: bool = False
    end_plate_right: bool = False
    end_stop_left: bool = False
    end_stop_right: bool = False
    jumpers: list[tuple[int, int]] = field(default_factory=list)
    count_uncertain: bool = False
    confidence: float = 0.0


def is_strip_box(item: Any) -> bool:
    return str(getattr(item, "label_text", "") or "").startswith(STRIP_LABEL)


def is_block_box(item: Any) -> bool:
    return str(getattr(item, "label_text", "") or "") == BLOCK_LABEL


def is_strip_feedback_item(item: Any) -> bool:
    return is_strip_box(item) or is_block_box(item)


def load_bgr(path: Path | str) -> np.ndarray | None:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        return None
    return image


def detect_terminal_strips(image: np.ndarray) -> list[StripHit]:
    """Strips in mounting order: top rail first, then left to right."""

    if image is None or image.size == 0:
        return []
    height, width = image.shape[:2]
    if height < 20 or width < 20:
        return []
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    housings = _housing_mask(hsv)
    rects = _housing_rects(housings, width, height)
    strips: list[StripHit] = []
    if rects:
        rows = _cluster_rows(rects)
        for row in rows:
            for group in _split_on_gaps(row):
                hit = _classify_group(image, hsv, group, row_gap_stops=_stops_between(hsv, row))
                if hit is not None and hit.blocks:
                    strips.append(hit)
                elif hit is not None and hit.count_uncertain:
                    strips.append(hit)
    # Colour misses gray and salmon housings. A second pass counts pitched rows
    # from the seams, and still requires a screw or spring in each block.
    strips.extend(_auto_seam_strips(image, existing=strips))
    strips.sort(key=lambda item: (item.y, item.x))
    return strips


def detect_terminal_strips_at(path: Path | str) -> list[StripHit]:
    image = load_bgr(path)
    if image is None:
        return []
    return detect_terminal_strips(image)


def strips_in_box(path: Path | str, region: PhotoRegion) -> list[StripHit]:
    """Run detection inside one box. Coordinates come back in full-photo pixels."""

    image = load_bgr(path)
    if image is None:
        return []
    height, width = image.shape[:2]
    x0 = max(0, int(round(region.x)))
    y0 = max(0, int(round(region.y)))
    x1 = min(width, int(round(region.x + region.w)))
    y1 = min(height, int(round(region.y + region.h)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return []
    found = detect_terminal_strips(image[y0:y1, x0:x1])
    for hit in found:
        _shift(hit, x0, y0)
    return found


def uncertain_strip(region: PhotoRegion, *, name: str = UNKNOWN) -> StripHit:
    """A box the scan could not break into blocks. The count stays unknown."""

    return StripHit(
        x=int(round(region.x)),
        y=int(round(region.y)),
        w=max(1, int(round(region.w))),
        h=max(1, int(round(region.h))),
        name=name or UNKNOWN,
        blocks=[],
        count_uncertain=True,
    )


def merge_extract_text(hits: list[StripHit], extract: dict[str, Any] | None, photo: Photo) -> None:
    """Fill '?' text from a vision extract when a region actually overlaps.

    Geometry keeps the block count, order, PE colour, jumpers, and plates.
    A count that disagrees with the extract is marked uncertain. No extra
    blocks are added to make the numbers match.
    """

    if not hits or not extract:
        return
    width = float(photo.width_px or 1)
    height = float(photo.height_px or 1)
    from panelcad.reconstruct.service import _region

    reported: list[dict[str, Any]] = []
    for raw in extract.get("strips") or []:
        if not isinstance(raw, dict):
            continue
        region = _region(raw, width, height)
        terminals = [term for term in (raw.get("terminals") or []) if isinstance(term, dict)]
        wires = [wire for wire in (raw.get("wire_numbers") or []) if isinstance(wire, dict)]
        reported.append({"raw": raw, "region": region, "terminals": terminals, "wires": wires})
    for hit in hits:
        match = _matching_extract(hit, reported, width, height)
        if match is None:
            continue
        raw = match["raw"]
        tag = _plausible_tag(raw.get("tag") or raw.get("name"))
        if hit.name == UNKNOWN and tag:
            hit.name = tag
        terminals = match["terminals"]
        if terminals and len(terminals) != len(hit.blocks):
            hit.count_uncertain = True
        for block in hit.blocks:
            term = _matching_terminal(block, terminals, width, height)
            if term is None:
                continue
            if bool(term.get("unreadable")):
                continue
            marker = _plausible_tag(term.get("marker") or term.get("id") or term.get("label"))
            panel = _plausible_tag(term.get("panel_wire") or term.get("internal") or term.get("top"))
            field = _plausible_tag(term.get("field_wire") or term.get("external") or term.get("bottom"))
            if block.marker == UNKNOWN and marker:
                block.marker = marker
            if block.panel_wire == UNKNOWN and panel:
                block.panel_wire = panel
            if block.field_wire == UNKNOWN and field:
                block.field_wire = field
        for wire in match["wires"]:
            if bool(wire.get("unreadable")):
                continue
            number = _plausible_tag(wire.get("number") or wire.get("text"))
            if not number:
                continue
            side = str(wire.get("side") or wire.get("landing") or "").strip().lower()
            target = _block_at_wire(hit, wire, width, height)
            if target is None:
                continue
            if side in {"field", "external", "bottom", "b"}:
                if target.field_wire == UNKNOWN:
                    target.field_wire = number
            elif target.panel_wire == UNKNOWN:
                target.panel_wire = number


def absorb_terminal_strips(
    session: Any,
    store: Reconstruction,
    photo: Photo,
    extract: dict[str, Any] | None = None,
) -> list[ObservedItem]:
    """Detect strips on one photo and replace this photo's automatic results."""

    path = _photo_path(session, photo)
    if path is None:
        return []
    image = load_bgr(path)
    if image is None:
        return []
    if int(photo.width_px or 0) <= 0 or int(photo.height_px or 0) <= 0:
        photo.height_px, photo.width_px = int(image.shape[0]), int(image.shape[1])
    hits = detect_terminal_strips(image)
    merge_extract_text(hits, extract, photo)
    return install_strips(session, store, photo, hits, user=False, replace_auto=True)


def install_user_box(
    session: Any,
    store: Reconstruction,
    photo: Photo,
    region: PhotoRegion,
    *,
    tag: str = "",
    group: str | None = None,
    block_count: int | None = None,
) -> list[ObservedItem]:
    """A box the tech drew. Count the blocks, or use the count they typed.

    An empty strip is not created. When the pitch cannot be read and no count
    was given, this raises ``how many blocks?`` and leaves the model alone.
    """

    path = _photo_path(session, photo)
    hits = strips_in_box(path, region) if path is not None else []
    if not hits and path is not None:
        hits = _seam_hits_in_region(path, region)
    wanted = _plausible_tag(tag)
    if hits:
        if wanted:
            for hit in hits:
                hit.name = wanted
    else:
        count = _parse_block_count(block_count)
        if count is None:
            raise ValueError(_COUNT_PROMPT)
        hits = [_even_blocks(region, count, name=wanted or UNKNOWN)]
    _drop_overlapping_suggestions(session, store, photo.id, region, keep=group)
    return install_strips(
        session,
        store,
        photo,
        hits,
        user=True,
        replace_auto=False,
        replace_group=group,
    )


def accept_terminal_strip(
    session: Any,
    item_id: str,
    *,
    tag: str = "",
    block_count: int | None = None,
) -> list[ObservedItem]:
    """Accept one suggested strip and write its terminal-strip sheet.

    A block box accepts its parent strip. Nothing here confirms a panel
    envelope: the photo does not know the block's millimetres, and Accept
    does not ask for width, height, or depth.
    """

    from panelcad.reconstruct.models import ensure_reconstruction

    store = ensure_reconstruction(session.project)
    item = store.item(item_id)
    if is_block_box(item):
        parent = str(item.rail_id or "").strip()
        if not parent:
            raise ValueError(f"{item_id} is not on a terminal strip")
        item = store.item(parent)
    if not is_strip_box(item):
        raise ValueError(f"{item_id} is not a terminal strip")
    photo = store.photo(item.photo_id)
    hint = _plausible_tag(tag)
    if not hint:
        current = str(item.terminal or "").strip()
        hint = "" if current in {"", UNKNOWN} else current
    # The stored box is the housings. Marker and wire glyphs sit just outside it.
    return install_user_box(
        session,
        store,
        photo,
        _pad_region(item.region, photo),
        tag=hint,
        group=item.id,
        block_count=block_count,
    )


def revise_terminal_strip(session: Any, item_id: str, region: PhotoRegion) -> list[ObservedItem]:
    """Move one strip box and detect again inside the new rectangle."""

    from panelcad.reconstruct.models import ensure_reconstruction

    store = ensure_reconstruction(session.project)
    item = store.item(item_id)
    if not is_strip_box(item):
        raise ValueError(f"{item_id} is not a terminal strip box")
    photo = store.photo(item.photo_id)
    hint = str(item.terminal or "").strip()
    if hint == UNKNOWN:
        hint = ""
    return install_user_box(session, store, photo, region, tag=hint, group=item.id)


def install_strips(
    session: Any,
    store: Reconstruction,
    photo: Photo,
    hits: list[StripHit],
    *,
    user: bool,
    replace_auto: bool,
    replace_group: str | None = None,
) -> list[ObservedItem]:
    _purge_items(store, photo.id, group=replace_group, auto_only=replace_auto and not replace_group)
    _purge_model(session, photo.id, group=replace_group, auto_only=replace_auto and not replace_group)
    kept_user = [
        item
        for item in store.items
        if item.photo_id == photo.id and is_strip_box(item) and float(item.confidence or 0) >= 0.99
    ]
    created: list[ObservedItem] = []
    for hit in hits:
        if not user and _covered_by_user(hit, kept_user):
            continue
        created.extend(_install_one(session, store, photo, hit, user=user))
    return created


def _install_one(
    session: Any,
    store: Reconstruction,
    photo: Photo,
    hit: StripHit,
    *,
    user: bool,
) -> list[ObservedItem]:
    read_name = hit.name if hit.name else UNKNOWN
    key = _unique_strip_name(session, store, read_name)
    strip_id = next_id("i", [item.id for item in store.items])
    missing_text = _strip_has_unknown(hit, read_name)
    label = STRIP_LABEL if not hit.count_uncertain else f"{STRIP_LABEL} uncertain"
    strip_item = ObservedItem(
        id=strip_id,
        photo_id=photo.id,
        role="terminal",
        tag=key,
        terminal=read_name,
        label_text=label,
        region=_region(hit.x, hit.y, hit.w, hit.h),
        confidence=1.0 if user else float(hit.confidence or _SUGGESTION_CONFIDENCE),
        unreadable=missing_text or hit.count_uncertain,
        status="unconfirmed",
    )
    store.items.append(strip_item)
    created = [strip_item]
    for index, block in enumerate(hit.blocks, start=1):
        block_id = next_id("i", [item.id for item in store.items])
        block_item = ObservedItem(
            id=block_id,
            photo_id=photo.id,
            role="terminal",
            tag=key,
            terminal=block.marker or UNKNOWN,
            label_text=BLOCK_LABEL,
            region=_region(block.x, block.y, block.w, block.h),
            confidence=1.0 if user else float(hit.confidence or _SUGGESTION_CONFIDENCE),
            unreadable=block.marker == UNKNOWN or block.panel_wire == UNKNOWN or block.field_wire == UNKNOWN,
            status="unconfirmed",
            rail_id=strip_id,
        )
        store.items.append(block_item)
        created.append(block_item)
        for side, value in ((PANEL_SIDE, block.panel_wire), (FIELD_SIDE, block.field_wire)):
            band = _wire_band(block, side, photo)
            wire_item = ObservedItem(
                id=next_id("i", [item.id for item in store.items]),
                photo_id=photo.id,
                role="wire_number",
                tag=key,
                terminal=str(index),
                label_text=side,
                wire_number="" if value == UNKNOWN else value,
                region=band,
                confidence=1.0 if user else float(hit.confidence or _SUGGESTION_CONFIDENCE),
                unreadable=value == UNKNOWN,
                status="unconfirmed",
                rail_id=strip_id,
            )
            store.items.append(wire_item)
            created.append(wire_item)
    # Only a person accepting the row creates terminal-strip sheets. A high
    # automatic score stays a suggestion on the photo.
    if hit.blocks and user:
        _populate_model(session, photo, hit, key=key, read_name=read_name, group=strip_id, user=user)
    return created


def _populate_model(
    session: Any,
    photo: Photo,
    hit: StripHit,
    *,
    key: str,
    read_name: str,
    group: str,
    user: bool,
) -> None:
    sheet = _ensure_strip_sheet(session, key, photo_id=photo.id, group=group, user=user)
    refs: list[str] = []
    for index, block in enumerate(hit.blocks, start=1):
        ref = _unique_component_id(session, f"{_ref_stem(key, index)}-{index}")
        props = {
            "strip": key,
            "terminal": str(index),
            "marker": block.marker or UNKNOWN,
            "panel_wire": block.panel_wire or UNKNOWN,
            "field_wire": block.field_wire or UNKNOWN,
            "split": "yes",
            "location": "schematic-only",
            _PHOTO_FLAG: "yes",
            "photo_id": photo.id,
            _USER_FLAG: "yes" if user else "no",
            _GROUP_FLAG: group,
            "photo_read_name": read_name,
        }
        if block.pe:
            props["type"] = "PE"
        if hit.count_uncertain:
            props["photo_count_uncertain"] = "yes"
        session.place(
            _TERMINAL_LIBRARY,
            ref=ref,
            x=40.0 + index,
            y=30.0 + index * 22.0,
            sheet_id=sheet.id,
            properties=props,
            snap=False,
        )
        refs.append(ref)
        comp = sheet.component(ref)
        _land_wire(session, sheet, comp, "A", block.panel_wire, photo_id=photo.id, group=group, user=user)
        _land_wire(session, sheet, comp, "B", block.field_wire, photo_id=photo.id, group=group, user=user)
    for start, end in hit.jumpers:
        if end <= start:
            continue
        span = refs[start : end + 1]
        if len(span) < 2:
            continue
        label = _next_jumper_id(session)
        for ref in span:
            sheet.component(ref).properties["jumper"] = label
    if hit.end_plate_left:
        _place_end(session, sheet, key, "end_plate", "left", photo, group, user)
    if hit.end_plate_right:
        _place_end(session, sheet, key, "end_plate", "right", photo, group, user)
    if hit.end_stop_left:
        _place_end(session, sheet, key, "end_stop", "left", photo, group, user)
    if hit.end_stop_right:
        _place_end(session, sheet, key, "end_stop", "right", photo, group, user)


def _place_end(
    session: Any,
    sheet: Any,
    key: str,
    kind: str,
    end: str,
    photo: Photo,
    group: str,
    user: bool,
) -> None:
    library = _END_PLATE_LIBRARY if kind == "end_plate" else _END_STOP_LIBRARY
    prefix = "XP" if kind == "end_plate" else "XS"
    ref = _unique_component_id(session, prefix)
    session.place(
        library,
        ref=ref,
        x=10.0 if end == "left" else 200.0,
        y=20.0,
        sheet_id=sheet.id,
        properties={
            "strip": key,
            "accessory": kind,
            "end": end,
            "location": "schematic-only",
            _PHOTO_FLAG: "yes",
            "photo_id": photo.id,
            _USER_FLAG: "yes" if user else "no",
            _GROUP_FLAG: group,
        },
        snap=False,
    )


def _land_wire(
    session: Any,
    sheet: Any,
    comp: Any,
    pin: str,
    value: str,
    *,
    photo_id: str,
    group: str,
    user: bool,
) -> None:
    """One landing on this side. A locked blank number stays '?', not an auto-number."""

    anchor = _unique_component_id(session, "MK")
    session.place(
        _OFFPAGE_LIBRARY,
        ref=anchor,
        x=240.0,
        y=float(comp.y),
        sheet_id=sheet.id,
        properties={
            _PHOTO_FLAG: "yes",
            "photo_id": photo_id,
            _USER_FLAG: "yes" if user else "no",
            _GROUP_FLAG: group,
            "photo_anchor": "yes",
        },
        snap=False,
    )
    text = value if value and value != UNKNOWN else UNKNOWN
    number = "" if text == UNKNOWN else text
    label = UNKNOWN if text == UNKNOWN else ""
    taken = {wire.id for page in session.project.sheets for wire in page.wires}
    seq = int(getattr(session, "_wire_seq", 0) or 0)
    seq += 1
    wid = f"w{seq}"
    while wid in taken:
        seq += 1
        wid = f"w{seq}"
    session._wire_seq = seq
    wire = Wire.model_validate(
        {"id": wid, "from": f"{comp.id}.{pin}", "to": f"{anchor}.A", "label": label, "number": number}
    )
    wire.number_locked = True
    sheet.wires.append(wire)


def _ensure_strip_sheet(session: Any, key: str, *, photo_id: str, group: str, user: bool) -> Any:
    for sheet in session.project.sheets:
        if sheet.kind == "terminal" and sheet.strip == key and str(sheet.properties.get("photo_id") or "") == photo_id:
            return sheet
    stem = _sheet_stem(key)
    sid = stem
    n = 2
    existing = {sheet.id for sheet in session.project.sheets}
    while sid in existing:
        sid = f"{stem}-{n}"
        n += 1
    sheet = session.add_sheet(sid, title=f"Terminal strip {key}", kind="terminal", make_active=False)
    sheet.strip = key
    sheet.strip_part = 1
    sheet.properties["photo_terminal_sheet"] = "yes"
    sheet.properties["photo_id"] = photo_id
    sheet.properties[_GROUP_FLAG] = group
    sheet.properties[_USER_FLAG] = "yes" if user else "no"
    return sheet


def _purge_items(store: Reconstruction, photo_id: str, *, group: str | None, auto_only: bool) -> None:
    if not group and not auto_only:
        return
    labels = {BLOCK_LABEL, PANEL_SIDE, FIELD_SIDE}
    kept: list[ObservedItem] = []
    for item in store.items:
        if item.photo_id != photo_id or not _owned_observation(item, labels):
            kept.append(item)
            continue
        if group:
            if item.id == group or item.rail_id == group:
                continue
            kept.append(item)
            continue
        if float(item.confidence or 0) >= 0.99:
            kept.append(item)
            continue
    store.items = kept


def _owned_observation(item: ObservedItem, labels: set[str]) -> bool:
    label = str(item.label_text or "")
    return label in labels or label.startswith(STRIP_LABEL)


def _purge_model(session: Any, photo_id: str, *, group: str | None, auto_only: bool) -> None:
    if not group and not auto_only:
        _drop_empty_photo_sheets(session, photo_id, group=group, auto_only=auto_only)
        return
    drop_ids: set[str] = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if not _photo_component(comp, photo_id):
                continue
            if group and str(comp.properties.get(_GROUP_FLAG) or "") != group:
                continue
            if not group and str(comp.properties.get(_USER_FLAG) or "") == "yes":
                continue
            drop_ids.add(comp.id)
    if not drop_ids:
        _drop_empty_photo_sheets(session, photo_id, group=group, auto_only=auto_only)
        return
    for sheet in session.project.sheets:
        sheet.components = [comp for comp in sheet.components if comp.id not in drop_ids]
        sheet.wires = [
            wire
            for wire in sheet.wires
            if wire.frm.split(".", 1)[0] not in drop_ids and wire.to.split(".", 1)[0] not in drop_ids
        ]
    _drop_empty_photo_sheets(session, photo_id, group=group, auto_only=auto_only)


def _drop_empty_photo_sheets(session: Any, photo_id: str, *, group: str | None, auto_only: bool) -> None:
    kept = []
    for sheet in session.project.sheets:
        props = sheet.properties or {}
        if (
            sheet.kind == "terminal"
            and str(props.get("photo_terminal_sheet") or "") == "yes"
            and str(props.get("photo_id") or "") == photo_id
            and not sheet.components
            and not sheet.wires
        ):
            if group and str(props.get(_GROUP_FLAG) or "") != group:
                kept.append(sheet)
                continue
            if auto_only and str(props.get(_USER_FLAG) or "") == "yes":
                kept.append(sheet)
                continue
            if session.active_sheet_id == sheet.id and session.project.sheets:
                session.active_sheet_id = next(
                    (other.id for other in session.project.sheets if other.id != sheet.id),
                    sheet.id,
                )
            continue
        kept.append(sheet)
    session.project.sheets = kept


def _photo_component(comp: Any, photo_id: str) -> bool:
    props = comp.properties or {}
    return str(props.get(_PHOTO_FLAG) or "") == "yes" and str(props.get("photo_id") or "") == photo_id


def _covered_by_user(hit: StripHit, user_strips: list[ObservedItem]) -> bool:
    for item in user_strips:
        if _overlap_ratio(hit, item.region) >= 0.45:
            return True
    return False


def _overlap_ratio(hit: StripHit, region: PhotoRegion) -> float:
    ax0, ay0, ax1, ay1 = hit.x, hit.y, hit.x + hit.w, hit.y + hit.h
    bx0, by0 = float(region.x), float(region.y)
    bx1, by1 = bx0 + float(region.w), by0 + float(region.h)
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    if inter <= 0 or hit.w <= 0 or hit.h <= 0:
        return 0.0
    other = max(1.0, float(region.w) * float(region.h))
    return inter / min(float(hit.w * hit.h), other)


def _regions_overlap(left: PhotoRegion, right: PhotoRegion) -> float:
    return _overlap_ratio(
        StripHit(
            x=int(round(left.x)),
            y=int(round(left.y)),
            w=max(1, int(round(left.w))),
            h=max(1, int(round(left.h))),
        ),
        right,
    )


def _drop_overlapping_suggestions(
    session: Any,
    store: Reconstruction,
    photo_id: str,
    region: PhotoRegion,
    *,
    keep: str | None,
) -> None:
    """A drawn box replaces an overlapping '?' suggestion. An accepted strip stays."""

    drop: set[str] = set()
    for item in store.items:
        if item.photo_id != photo_id or not is_strip_box(item):
            continue
        if keep and item.id == keep:
            continue
        overlap = _regions_overlap(item.region, region)
        # A new box replaces a suggestion it overlaps. An accepted strip is
        # replaced only when the new box covers most of it, so a second strip
        # beside it is kept and four tries on the same strip do not stack.
        if float(item.confidence or 0) >= 0.99:
            if overlap < 0.5:
                continue
        elif overlap < 0.3:
            continue
        drop.add(item.id)
    if not drop:
        return
    store.items = [item for item in store.items if item.id not in drop and item.rail_id not in drop]
    for group in drop:
        _purge_model(session, photo_id, group=group, auto_only=False)


def _seam_hits_in_region(path: Path | str, region: PhotoRegion) -> list[StripHit]:
    """Count blocks inside a box the colour scan could not break apart.

    A person already said this rectangle is a strip, so a regular run of narrow
    divisions is enough. Screws are not required: a tight crop often cuts them off.
    """

    image = load_bgr(path)
    if image is None:
        return []
    height, width = image.shape[:2]
    x0 = max(0, int(round(region.x)))
    y0 = max(0, int(round(region.y)))
    x1 = min(width, int(round(region.x + region.w)))
    y1 = min(height, int(round(region.y + region.h)))
    if x1 - x0 < 16 or y1 - y0 < 20:
        return []
    crop = image[y0:y1, x0:x1]
    hits = _seam_hits(crop, min_count=3, require_fastener=False)
    for hit in hits:
        _shift(hit, x0, y0)
    return hits


def _auto_seam_strips(image: np.ndarray, *, existing: list[StripHit]) -> list[StripHit]:
    """Pitched rows the housing colour missed. Still needs a fastener in each block."""

    height = int(image.shape[0])
    if height < 36:
        return []
    groups: list[list[BlockHit]] = []
    for band_h in (72, 104, 140):
        stride = max(18, band_h // 3)
        y = 0
        while y < height - 28:
            y1 = min(height, y + band_h)
            if y1 - y < 36:
                break
            for group in _seam_groups(image[y:y1], min_count=_MIN_BLOCKS):
                for block in group:
                    block.y += y
                if _group_overlaps_hits(group, existing) or _group_overlaps_groups(group, groups):
                    continue
                peeled = _peeled_terminals(group)
                if peeled and _terminal_row(image, peeled):
                    groups.append(group)
            y += stride
    if not groups:
        return []
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hits: list[StripHit] = []
    for group in groups:
        hit = _finish_from_group(image, hsv, group)
        if hit is not None and hit.blocks:
            hits.append(hit)
    return hits


def _seam_hits(image: np.ndarray, *, min_count: int, require_fastener: bool) -> list[StripHit]:
    groups = list(_seam_groups(image, min_count=min_count))
    if not groups and int(image.shape[0]) >= 70:
        groups = _seam_groups_in_bands(image, min_count=min_count)
    periodic = False
    if not groups and not require_fastener:
        # Grey, low-contrast, or blurry housings. The person already drew the box.
        groups = _periodic_groups(image, min_count=min_count)
        periodic = bool(groups)
    if not groups:
        return []
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hits: list[StripHit] = []
    for group in groups:
        peeled = _peeled_terminals(group)
        if require_fastener:
            if not peeled or not _terminal_row(image, peeled):
                continue
        elif periodic:
            if not _periodic_group_ok(peeled, min_count=min_count):
                continue
        elif not _pitched_row(group, min_count=min_count, min_aspect=2.0):
            continue
        hit = _finish_from_group(image, hsv, group)
        if hit is not None and hit.blocks:
            hits.append(hit)
    return _dedupe_strip_hits(hits)


def _seam_groups_in_bands(image: np.ndarray, *, min_count: int) -> list[list[BlockHit]]:
    """A loose box: the strip is one band inside the rectangle, not the whole box."""

    height = int(image.shape[0])
    found: list[list[BlockHit]] = []
    for fraction in (0.62, 0.45):
        band_h = int(height * fraction)
        if band_h < 28:
            continue
        stride = max(12, band_h // 4)
        y = 0
        while y + band_h <= height:
            for group in _seam_groups(image[y : y + band_h], min_count=min_count):
                for block in group:
                    block.y += y
                found.append(group)
            if found:
                return found
            y += stride
    return found


def _seam_groups(image: np.ndarray, *, min_count: int) -> list[list[BlockHit]]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    if height < 24 or width < 30:
        return []
    y0 = max(0, int(height * 0.08))
    y1 = min(height, max(y0 + 16, int(height * 0.92)))
    band = gray[y0:y1]
    luma = band.astype(np.float32).mean(axis=0)
    if float(luma.max() - luma.min()) < 12.0:
        return []
    smooth = cv2.GaussianBlur(luma.reshape(1, -1), (5, 1), 0).ravel()
    body_h = y1 - y0
    groups: list[list[BlockHit]] = []
    for sign in (1.0, -1.0):
        spans = _threshold_spans(smooth * sign)
        blocks: list[BlockHit] = []
        for start, end in spans:
            bw = end - start
            if bw < 5 or bw > width * 0.5:
                continue
            # A wire-number label or a line of text is a short band. A housing fills the window.
            if not _span_fills_band(band, start, end):
                continue
            blocks.append(BlockHit(x=int(start), y=int(y0), w=int(bw), h=int(body_h)))
        for group in _split_block_gaps(blocks):
            if _pitched_row(group, min_count=min_count, min_aspect=2.0):
                groups.append(group)
    return groups


def _span_fills_band(band: np.ndarray, start: int, end: int) -> bool:
    """True when this column differs from the gap beside it for most of the window.

    A wire-number label or a line of text differs for only a few rows. A housing
    differs for the whole row.
    """

    height, width = band.shape[:2]
    x0 = max(0, int(start))
    x1 = min(width, int(end))
    if x1 - x0 < 2 or height < 8:
        return False
    column = band[:, x0:x1].astype(np.float32).mean(axis=1)
    sides: list[np.ndarray] = []
    if x0 > 1:
        sides.append(band[:, max(0, x0 - 2) : x0].astype(np.float32).mean(axis=1))
    if x1 < width - 1:
        sides.append(band[:, x1 : min(width, x1 + 2)].astype(np.float32).mean(axis=1))
    if not sides:
        return True
    background = sides[0] if len(sides) == 1 else (sides[0] + sides[1]) / 2.0
    changed = np.abs(column - background) >= 18.0
    best = 0
    run = 0
    for value in changed.tolist():
        run = run + 1 if value else 0
        best = max(best, run)
    return best >= 0.62 * float(height)


def _pad_region(region: PhotoRegion, photo: Photo) -> PhotoRegion:
    pad_y = max(24.0, float(region.h) * 0.45)
    pad_x = max(6.0, float(region.w) * 0.04)
    width = float(photo.width_px or 0)
    height = float(photo.height_px or 0)
    x = max(0.0, float(region.x) - pad_x)
    y = max(0.0, float(region.y) - pad_y)
    x1 = float(region.x) + float(region.w) + pad_x
    y1 = float(region.y) + float(region.h) + pad_y
    if width > 0:
        x1 = min(width, x1)
    if height > 0:
        y1 = min(height, y1)
    return PhotoRegion(x=x, y=y, w=max(1.0, x1 - x), h=max(1.0, y1 - y))


def _periodic_groups(image: np.ndarray, *, min_count: int) -> list[list[BlockHit]]:
    """Block pitch from repeating vertical edges and marker-card edges.

    Used only inside a box a person drew. Autocorrelation still sees a pitch
    when the housings are grey, soft, or low contrast.
    """

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (0, 0), 1.1)
    found = _periodic_on_gray(gray, min_count=min_count)
    height, width = gray.shape[:2]
    if height <= width * 1.15:
        return found
    turned = _periodic_on_gray(np.ascontiguousarray(gray.T), min_count=min_count)
    swapped: list[list[BlockHit]] = []
    for group in turned:
        for block in group:
            block.x, block.y, block.w, block.h = block.y, block.x, block.h, block.w
        swapped.append(group)
    if swapped and (not found or len(swapped[0]) > len(found[0])):
        return swapped
    return found


def _periodic_on_gray(gray: np.ndarray, *, min_count: int) -> list[list[BlockHit]]:
    height, width = gray.shape[:2]
    if height < 16 or width < 24:
        return []
    slices = (
        (0, height),
        (int(height * 0.08), int(height * 0.92)),
        (int(height * 0.05), int(height * 0.5)),
        (int(height * 0.15), int(height * 0.7)),
    )
    best: tuple[float, int, np.ndarray] | None = None
    for y0, y1 in slices:
        if y1 - y0 < 8:
            continue
        band = gray[y0:y1]
        for profile in (_edge_profile(band), _card_profile(band)):
            found = _fundamental_pitch(profile, min_count=min_count)
            if found is None:
                continue
            pitch, score = found
            if best is None or score > best[0]:
                best = (score, pitch, profile)
    if best is None:
        return []
    _score, pitch, profile = best
    blocks = _blocks_from_pitch(profile, pitch, height, min_count=min_count)
    if len(blocks) < min_count:
        return []
    return [blocks]


def _edge_profile(band: np.ndarray) -> np.ndarray:
    gradient = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0, ksize=3))
    return gradient.mean(axis=0)


def _card_profile(band: np.ndarray) -> np.ndarray:
    """Edges of marker cards: the light rectangles repeating on grey housings."""

    width = int(band.shape[1])
    if width < 16:
        return np.zeros(width, np.float32)
    kernel = max(9, int(width / 8) | 1)
    blur = cv2.GaussianBlur(band, (kernel, 1), 0)
    residual = band.astype(np.float32) - blur.astype(np.float32)
    return np.abs(cv2.Sobel(residual, cv2.CV_32F, 1, 0, ksize=3)).mean(axis=0)


def _shift_corr(signal: np.ndarray, lag: int) -> float:
    if lag <= 0 or lag >= len(signal) - 4:
        return 0.0
    left = signal[:-lag]
    right = signal[lag:]
    denom = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denom < 1e-6:
        return 0.0
    return float(np.dot(left, right) / denom)


def _fundamental_pitch(profile: np.ndarray, *, min_count: int) -> tuple[int, float] | None:
    signal = profile.astype(np.float64)
    signal = signal - float(signal.mean())
    signal = np.convolve(signal, np.array([1.0, 2.0, 1.0]) / 4.0, mode="same")
    energy = float(np.dot(signal, signal))
    if energy < 1e-3:
        return None
    corr = np.correlate(signal, signal, mode="full")
    corr = corr[len(signal) - 1 :] / energy
    width = len(signal)
    limit = max(6, width // max(min_count, 3))
    peaks: list[int] = []
    for index in range(4, min(len(corr) - 1, limit)):
        if corr[index] >= corr[index - 1] and corr[index] >= corr[index + 1] and corr[index] > 0.2:
            peaks.append(index)
    best: tuple[int, float] | None = None
    for lag in peaks:
        # A smooth fade is high at every small lag. A real pitch rises above the lag halfway to it.
        half = float(corr[max(1, lag // 2)])
        if float(corr[lag]) < half + 0.08:
            continue
        first = _shift_corr(signal, lag)
        second = _shift_corr(signal, 2 * lag)
        if first < 0.45 or second < 0.35:
            continue
        score = min(first, second)
        if best is None or score > best[1]:
            best = (lag, score)
    return best


def _blocks_from_pitch(profile: np.ndarray, pitch: int, height: int, *, min_count: int) -> list[BlockHit]:
    signal = np.abs(profile.astype(np.float64) - float(np.median(profile)))
    best_phase = 0
    best_score = -1.0
    for phase in range(int(pitch)):
        indexes = np.arange(phase, len(signal), pitch)
        score = float(signal[indexes].sum()) if len(indexes) else 0.0
        if score > best_score:
            best_score = score
            best_phase = phase
    edges = list(range(best_phase, len(signal) + 1, pitch))
    if not edges:
        return []
    if edges[0] >= int(pitch * 0.55):
        edges = [0, *edges]
    if len(signal) - edges[-1] >= int(pitch * 0.55):
        edges.append(len(signal))
    spans: list[tuple[int, int, float]] = []
    for start, end in zip(edges, edges[1:], strict=False):
        width = end - start
        if width < pitch * 0.55 or width > pitch * 1.45:
            continue
        spans.append((int(start), int(width), float(np.mean(profile[start:end]))))
    if len(spans) < min_count:
        return []
    # A margin past the last housing still holds one seam. Its interval is quieter.
    typical = float(np.median([energy for _start, _width, energy in spans]))
    blocks = [
        BlockHit(x=start, y=0, w=width, h=max(1, int(height)))
        for start, width, energy in spans
        if energy >= 0.55 * typical
    ]
    if len(blocks) < min_count:
        return []
    return blocks


def _periodic_group_ok(blocks: list[BlockHit], *, min_count: int) -> bool:
    if len(blocks) < min_count:
        return False
    mean_w = float(np.mean([block.w for block in blocks]))
    mean_h = float(np.mean([block.h for block in blocks]))
    return mean_w > 0 and mean_h >= mean_w * 1.15


def _parse_block_count(value: Any) -> int | None:
    if isinstance(value, bool):
        raise ValueError("block count must be a number")
    if value is None or value == "" or value == 0:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("block count must be a number") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError("block count must be a whole number")
    count = int(number)
    if count < 1 or count > 400:
        raise ValueError("block count must be between 1 and 400")
    return count


def _even_blocks(region: PhotoRegion, count: int, *, name: str) -> StripHit:
    """Equal blocks across a box whose pitch could not be read. Markers stay '?'."""

    x = int(round(region.x))
    y = int(round(region.y))
    width = max(count, int(round(region.w)))
    height = max(8, int(round(region.h)))
    blocks: list[BlockHit] = []
    for index in range(count):
        start = x + int(round(index * width / count))
        end = x + int(round((index + 1) * width / count))
        blocks.append(BlockHit(x=start, y=y, w=max(1, end - start), h=height))
    return StripHit(
        x=x,
        y=y,
        w=width,
        h=height,
        name=name or UNKNOWN,
        blocks=blocks,
        confidence=1.0,
    )


def _threshold_spans(signal: np.ndarray) -> list[tuple[int, int]]:
    lo = float(np.min(signal))
    hi = float(np.max(signal))
    if hi - lo < 8.0:
        return []
    thresh = lo + 0.42 * (hi - lo)
    on = (signal >= thresh).astype(np.float32)
    return _on_spans(on, on=0.5, off=0.5, min_width=5)


def _split_block_gaps(blocks: list[BlockHit]) -> list[list[BlockHit]]:
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda block: block.x)
    widths = sorted(block.w for block in ordered)
    median = float(widths[len(widths) // 2])
    limit = max(10.0, median * 1.6)
    groups: list[list[BlockHit]] = [[ordered[0]]]
    for block in ordered[1:]:
        prev = groups[-1][-1]
        gap = block.x - (prev.x + prev.w)
        if gap > limit:
            groups.append([block])
        else:
            groups[-1].append(block)
    return [group for group in groups if len(group) >= 2]


def _pitched_row(blocks: list[BlockHit], *, min_count: int, min_aspect: float) -> bool:
    terminals = _peeled_terminals(blocks)
    if len(terminals) < min_count:
        return False
    widths = np.array([block.w for block in terminals], dtype=float)
    mean_w = float(widths.mean())
    if mean_w <= 0 or float(widths.std() / mean_w) > 0.32:
        return False
    height = float(np.median([block.h for block in terminals]))
    aspect = height / mean_w
    if aspect < min_aspect or aspect > 18.0:
        return False
    centers = np.array([block.x + block.w / 2.0 for block in terminals], dtype=float)
    pitch = np.diff(centers)
    if pitch.size < min_count - 1:
        return False
    mean_p = float(pitch.mean())
    if mean_p <= 0 or float(pitch.std() / mean_p) > 0.30:
        return False
    if mean_p > mean_w * 2.1 or mean_p < mean_w * 0.8:
        return False
    return True


def _peeled_terminals(blocks: list[BlockHit]) -> list[BlockHit]:
    _left, _right, terminals = _peel_end_plates(list(blocks))
    return terminals


def _finish_from_group(image: np.ndarray, hsv: np.ndarray, group: list[BlockHit]) -> StripHit | None:
    measured = [_measure_block(image, hsv, (block.x, block.y, block.w, block.h)) for block in group]
    plates_left, plates_right, terminals = _peel_end_plates(measured)
    if not terminals:
        return None
    return _finish_strip(image, hsv, terminals, plates_left, plates_right, row_gap_stops=[])


def _finish_strip(
    image: np.ndarray,
    hsv: np.ndarray,
    terminals: list[BlockHit],
    plates_left: list[BlockHit],
    plates_right: list[BlockHit],
    *,
    row_gap_stops: list[tuple[int, int]],
) -> StripHit:
    uncertain = _width_uncertain(terminals) or _seam_uncertain(hsv, terminals)
    jumpers = _jumper_spans(hsv, terminals)
    for block in terminals:
        block.marker = _read_marker(image, block)
    name = _assign_labels(image, hsv, terminals)
    pieces = plates_left + terminals + plates_right
    x0 = min(piece.x for piece in pieces)
    y0 = min(piece.y for piece in pieces)
    x1 = max(piece.x + piece.w for piece in pieces)
    y1 = max(piece.y + piece.h for piece in pieces)
    return StripHit(
        x=x0,
        y=y0,
        w=x1 - x0,
        h=y1 - y0,
        name=name,
        blocks=terminals,
        end_plate_left=bool(plates_left),
        end_plate_right=bool(plates_right),
        end_stop_left=_touches_stop(x0, row_gap_stops, left=True),
        end_stop_right=_touches_stop(x1, row_gap_stops, left=False),
        jumpers=jumpers,
        count_uncertain=uncertain,
        confidence=_row_confidence(name, terminals, uncertain),
    )


def _group_box(blocks: list[BlockHit]) -> tuple[int, int, int, int]:
    x0 = min(block.x for block in blocks)
    y0 = min(block.y for block in blocks)
    x1 = max(block.x + block.w for block in blocks)
    y1 = max(block.y + block.h for block in blocks)
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def _group_overlaps_hits(group: list[BlockHit], hits: list[StripHit]) -> bool:
    x, y, w, h = _group_box(group)
    probe = StripHit(x=x, y=y, w=w, h=h)
    return any(_overlap_ratio(probe, _region(hit.x, hit.y, hit.w, hit.h)) >= 0.35 for hit in hits)


def _group_overlaps_groups(group: list[BlockHit], others: list[list[BlockHit]]) -> bool:
    x, y, w, h = _group_box(group)
    probe = StripHit(x=x, y=y, w=w, h=h)
    for other in others:
        ox, oy, ow, oh = _group_box(other)
        if _overlap_ratio(probe, _region(ox, oy, ow, oh)) >= 0.35:
            return True
    return False


def _dedupe_strip_hits(hits: list[StripHit]) -> list[StripHit]:
    ordered = sorted(hits, key=lambda hit: (-len(hit.blocks), -hit.confidence, hit.y, hit.x))
    kept: list[StripHit] = []
    for hit in ordered:
        if any(_overlap_ratio(hit, _region(other.x, other.y, other.w, other.h)) >= 0.4 for other in kept):
            continue
        kept.append(hit)
    return kept


def _housing_mask(hsv: np.ndarray) -> np.ndarray:
    light = cv2.inRange(hsv, (0, 0, 168), (179, 60, 255))
    # Pastel housings (salmon, pink, blue-gray) that a beige mask misses.
    # Saturation stays under a comb or a sticker, so those are not blocks.
    pastel = cv2.inRange(hsv, (0, 28, 130), (179, 150, 255))
    green = cv2.inRange(hsv, (35, 50, 35), (95, 255, 255))
    yellow = cv2.inRange(hsv, (18, 70, 120), (42, 255, 255))
    mask = light
    for extra in (pastel, green, yellow):
        mask = cv2.bitwise_or(mask, extra)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return mask


def _housing_rects(mask: np.ndarray, width: int, height: int) -> list[tuple[int, int, int, int]]:
    contours, _hier = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rects: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h > 0.3 * width * height:
            continue
        if h >= 24 and w > h * 1.7:
            rects.extend(_split_wide(mask[y : y + h, x : x + w], x, y))
            continue
        if _block_shape(w, h, width, height) and _solid(contour, w, h):
            rects.append((x, y, w, h))
    return rects


def _split_wide(roi: np.ndarray, origin_x: int, origin_y: int) -> list[tuple[int, int, int, int]]:
    if roi.size == 0:
        return []
    fill = (roi > 0).mean(axis=0)
    spans = _on_spans(fill, on=0.35, off=0.18, min_width=6)
    rects: list[tuple[int, int, int, int]] = []
    height = roi.shape[0]
    uncertain_wide = False
    for start, end in spans:
        w = end - start
        if w < 6 or height < 24:
            continue
        if w > height * 1.15:
            uncertain_wide = True
            rects.append((origin_x + start, origin_y, w, height))
            continue
        if _block_shape(w, height, 10_000, 10_000):
            rects.append((origin_x + start, origin_y, w, height))
    if uncertain_wide:
        # The caller flags uncertainty from width spread. Keeping the wide
        # piece as one block does not invent a second terminal.
        return rects
    return rects


def _on_spans(fill: np.ndarray, *, on: float, off: float, min_width: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for index, value in enumerate(fill.tolist()):
        if value >= on:
            if start is None:
                start = index
            gap = 0
            continue
        if start is None:
            continue
        if value < off:
            gap += 1
        else:
            gap = 0
        if gap >= 2:
            end = index - gap + 1
            if end - start >= min_width:
                spans.append((start, end))
            start = None
            gap = 0
    if start is not None and len(fill) - start >= min_width:
        spans.append((start, len(fill)))
    return spans


def _block_shape(w: int, h: int, image_w: int, image_h: int) -> bool:
    if h < 22 or w < 6:
        return False
    if w > image_w * 0.45 or h > image_h * 0.75:
        return False
    ratio = h / float(w)
    # Terminal housings are narrow. A module, breaker, or label is not.
    return _MIN_ASPECT <= ratio <= _MAX_ASPECT


def _solid(contour: np.ndarray, w: int, h: int) -> bool:
    area = float(cv2.contourArea(contour))
    return area >= 0.45 * float(w * h)


def _cluster_rows(rects: list[tuple[int, int, int, int]]) -> list[list[tuple[int, int, int, int]]]:
    ordered = sorted(rects, key=lambda item: (item[1], item[0]))
    rows: list[list[tuple[int, int, int, int]]] = []
    for rect in ordered:
        placed = False
        for row in rows:
            if _same_row(rect, row):
                row.append(rect)
                placed = True
                break
        if not placed:
            rows.append([rect])
    for row in rows:
        row.sort(key=lambda item: item[0])
    return rows


def _same_row(rect: tuple[int, int, int, int], row: list[tuple[int, int, int, int]]) -> bool:
    _x, y, _w, h = rect
    ry = sorted(item[1] for item in row)[len(row) // 2]
    rh = sorted(item[3] for item in row)[len(row) // 2]
    overlap = min(y + h, ry + rh) - max(y, ry)
    if overlap <= 0.55 * min(h, rh):
        return False
    return abs(h - rh) / float(max(h, rh)) <= 0.4


def _split_on_gaps(row: list[tuple[int, int, int, int]]) -> list[list[tuple[int, int, int, int]]]:
    if not row:
        return []
    widths = sorted(item[2] for item in row)
    median = float(widths[len(widths) // 2])
    limit = max(12.0, median * 1.5)
    groups: list[list[tuple[int, int, int, int]]] = [[row[0]]]
    for rect in row[1:]:
        prev = groups[-1][-1]
        gap = rect[0] - (prev[0] + prev[2])
        if gap > limit:
            groups.append([rect])
        else:
            groups[-1].append(rect)
    return [group for group in groups if len(group) >= 2]


def _stops_between(hsv: np.ndarray, row: list[tuple[int, int, int, int]]) -> list[tuple[int, int]]:
    """X ranges of dark end stops sitting in the gaps of one row. Empty when none."""

    if len(row) < 2:
        return []
    y0 = min(item[1] for item in row)
    y1 = max(item[1] + item[3] for item in row)
    dark = cv2.inRange(hsv, (0, 0, 0), (179, 90, 50))
    found: list[tuple[int, int]] = []
    for prev, nxt in zip(row, row[1:], strict=False):
        gap_x0 = prev[0] + prev[2]
        gap_x1 = nxt[0]
        if gap_x1 - gap_x0 < 8:
            continue
        roi = dark[y0:y1, gap_x0:gap_x1]
        if roi.size == 0 or float(roi.mean()) < 20:
            continue
        contours, _hier = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if h >= 0.4 * (y1 - y0) and w >= 6:
                found.append((gap_x0 + x, gap_x0 + x + w))
                break
    return found


def _terminal_row(image: np.ndarray, blocks: list[BlockHit]) -> bool:
    """True only for a pitched row of narrow housings with fastener points.

    Colour alone never qualifies. A short row is left for the tech to draw.
    """

    if len(blocks) < _MIN_BLOCKS:
        return False
    aspects = [block.h / float(block.w) for block in blocks if block.w > 0]
    if len(aspects) != len(blocks) or any(aspect < _MIN_ASPECT or aspect > _MAX_ASPECT for aspect in aspects):
        return False
    widths = np.array([block.w for block in blocks], dtype=float)
    if float(widths.mean()) <= 0 or float(widths.std() / widths.mean()) > _MAX_WIDTH_CV:
        return False
    centers = np.array([block.x + block.w / 2.0 for block in blocks], dtype=float)
    pitch = np.diff(centers)
    if pitch.size < _MIN_BLOCKS - 1 or float(pitch.mean()) <= 0:
        return False
    if float(pitch.std() / pitch.mean()) > _MAX_PITCH_CV:
        return False
    # Neighbouring blocks on one rail, not devices scattered along a duct.
    if float(pitch.mean()) > float(widths.mean()) * 1.85 or float(pitch.mean()) < float(widths.mean()) * 0.85:
        return False
    median_h = float(np.median([block.h for block in blocks]))
    bottoms = [block.y + block.h for block in blocks]
    if max(bottoms) - min(bottoms) > 0.2 * median_h:
        return False
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    fasteners = sum(1 for block in blocks if _has_fastener(gray, block))
    return fasteners >= _FASTENER_SHARE * len(blocks)


def _has_fastener(gray: np.ndarray, block: BlockHit) -> bool:
    """A screw head or spring slot near the top or bottom of a narrow housing."""

    def zone(y0: int, y1: int) -> bool:
        y0 = max(0, y0)
        y1 = min(gray.shape[0], y1)
        x0 = max(0, block.x + int(block.w * 0.15))
        x1 = min(gray.shape[1], block.x + int(block.w * 0.85))
        if y1 - y0 < 4 or x1 - x0 < 4:
            return False
        roi = gray[y0:y1, x0:x1]
        median = float(np.median(roi))
        dark = (roi < min(median - 40.0, 110.0)).astype(np.uint8) * 255
        if float(dark.mean()) < 8.0:
            return False
        _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(dark, 8)
        zone_h = y1 - y0
        roi_w = x1 - x0
        for index in range(1, len(stats)):
            bw = int(stats[index, cv2.CC_STAT_WIDTH])
            bh = int(stats[index, cv2.CC_STAT_HEIGHT])
            area = int(stats[index, cv2.CC_STAT_AREA])
            if area < 4:
                continue
            if bw > roi_w * 0.9 and bh > zone_h * 0.9:
                continue
            if 2 <= bw <= roi_w * 0.75 and 2 <= bh <= zone_h:
                return True
        return False

    top_h = max(6, int(block.h * 0.28))
    bot_h = max(6, int(block.h * 0.28))
    return zone(block.y, block.y + top_h) or zone(block.y + block.h - bot_h, block.y + block.h)


def _row_confidence(name: str, blocks: list[BlockHit], uncertain: bool) -> float:
    """Geometry alone is a suggestion. Read tags raise it. A guess never does."""

    if uncertain or not blocks:
        return _SUGGESTION_CONFIDENCE
    readable = 1 if name != UNKNOWN else 0
    slots = 1
    for block in blocks:
        slots += 3
        readable += sum(1 for value in (block.marker, block.panel_wire, block.field_wire) if value != UNKNOWN)
    if name != UNKNOWN or readable / slots >= 0.5:
        return 0.92
    return _SUGGESTION_CONFIDENCE


def _classify_group(
    image: np.ndarray,
    hsv: np.ndarray,
    group: list[tuple[int, int, int, int]],
    *,
    row_gap_stops: list[tuple[int, int]],
) -> StripHit | None:
    blocks = [_measure_block(image, hsv, rect) for rect in group]
    plates_left, plates_right, terminals = _peel_end_plates(blocks)
    if not _terminal_row(image, terminals):
        return None
    return _finish_strip(
        image,
        hsv,
        terminals,
        plates_left,
        plates_right,
        row_gap_stops=row_gap_stops,
    )


def _measure_block(image: np.ndarray, hsv: np.ndarray, rect: tuple[int, int, int, int]) -> BlockHit:
    x, y, w, h = rect
    roi = hsv[y : y + h, x : x + w]
    pe = False
    if roi.size:
        green = cv2.inRange(roi, (35, 50, 35), (95, 255, 255))
        yellow = cv2.inRange(roi, (18, 70, 120), (42, 255, 255))
        colored = cv2.bitwise_or(green, yellow)
        pe = float(colored.mean()) >= 28.0
    return BlockHit(x=x, y=y, w=w, h=h, pe=pe)


def _width_uncertain(blocks: list[BlockHit]) -> bool:
    if len(blocks) < 2:
        return False
    widths = sorted(block.w for block in blocks)
    median = float(widths[len(widths) // 2])
    if median <= 0:
        return False
    return any(block.w > median * 1.65 for block in blocks)


def _seam_uncertain(hsv: np.ndarray, blocks: list[BlockHit]) -> bool:
    """A strong internal seam means the count may be short. Do not split it."""

    for block in blocks:
        roi = hsv[block.y : block.y + block.h, block.x : block.x + block.w]
        if roi.size == 0 or block.w < 12:
            continue
        housing = _housing_mask(roi)
        fill = (housing > 0).mean(axis=0)
        left = max(1, int(block.w * 0.28))
        right = min(block.w - 1, int(block.w * 0.72))
        if right <= left:
            continue
        valley = float(fill[left:right].min()) if right > left else 1.0
        if valley < 0.22 and float(fill[:left].mean()) > 0.5 and float(fill[right:].mean()) > 0.5:
            return True
    return False


def _peel_end_plates(blocks: list[BlockHit]) -> tuple[list[BlockHit], list[BlockHit], list[BlockHit]]:
    if len(blocks) < 2:
        return [], [], blocks
    body = [block for block in blocks if not block.pe]
    widths = sorted((block.w for block in body), reverse=False)
    if not widths:
        return [], [], blocks
    median = float(widths[len(widths) // 2])

    def thin(block: BlockHit) -> bool:
        return (not block.pe) and block.w <= median * 0.62 and block.w <= median - 3

    left: list[BlockHit] = []
    right: list[BlockHit] = []
    middle = list(blocks)
    while len(middle) > 1 and thin(middle[0]):
        plate = middle.pop(0)
        plate.end_plate = True
        left.append(plate)
    while len(middle) > 1 and thin(middle[-1]):
        plate = middle.pop()
        plate.end_plate = True
        right.append(plate)
    right.reverse()
    return left, right, middle


def _jumper_spans(hsv: np.ndarray, blocks: list[BlockHit]) -> list[tuple[int, int]]:
    if len(blocks) < 2:
        return []
    marks: list[int | None] = []
    for block in blocks:
        marks.append(_comb_y(hsv, block))
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(len(blocks)):
        linked = False
        if index > 0 and marks[index] is not None and marks[index - 1] is not None:
            if abs(int(marks[index]) - int(marks[index - 1])) <= 6 and _gap_has_comb(
                hsv, blocks[index - 1], blocks[index], int(marks[index])
            ):
                linked = True
        if linked:
            if start is None:
                start = index - 1
            continue
        if start is not None:
            spans.append((start, index - 1))
            start = None
    if start is not None:
        spans.append((start, len(blocks) - 1))
    return [(lo, hi) for lo, hi in spans if hi > lo]


def _comb_y(hsv: np.ndarray, block: BlockHit) -> int | None:
    # The comb often covers the top edge, so the housing contour starts below it.
    y0 = max(0, block.y - max(4, int(block.h * 0.28)))
    y1 = min(hsv.shape[0], block.y + max(6, int(block.h * 0.45)))
    roi = hsv[y0:y1, block.x : block.x + block.w]
    if roi.size == 0:
        return None
    comb = _comb_mask(roi)
    best: tuple[float, int] | None = None
    for row_index in range(comb.shape[0]):
        fraction = float(comb[row_index].mean()) / 255.0
        if fraction < 0.55:
            continue
        if best is None or fraction > best[0]:
            best = (fraction, row_index)
    if best is None:
        return None
    return y0 + best[1]


def _gap_has_comb(hsv: np.ndarray, left: BlockHit, right: BlockHit, y: int) -> bool:
    x0 = left.x + left.w
    x1 = right.x
    if x1 <= x0:
        return True
    y0 = max(0, y - 2)
    y1 = min(hsv.shape[0], y + 3)
    roi = hsv[y0:y1, x0:x1]
    if roi.size == 0:
        return False
    return float(_comb_mask(roi).mean()) >= 40.0


def _comb_mask(hsv: np.ndarray) -> np.ndarray:
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    colored = (sat > 90) & (val > 70)
    orange_red = (hue <= 16) | (hue >= 170)
    blue = (hue >= 100) & (hue <= 140)
    return np.where(colored & (orange_red | blue), 255, 0).astype(np.uint8)


def _touches_stop(edge: int, stops: list[tuple[int, int]], *, left: bool) -> bool:
    for x0, x1 in stops:
        if left and x1 <= edge + 6 and edge - x1 <= 48:
            return True
        if (not left) and x0 >= edge - 6 and x0 - edge <= 48:
            return True
    return False


def _assign_labels(image: np.ndarray, hsv: np.ndarray, blocks: list[BlockHit]) -> str:
    """Read light marker tags around the housings. A flat housing is not a tag."""

    if not blocks:
        return UNKNOWN
    median_h = float(np.median([block.h for block in blocks]))
    x0 = max(0, min(block.x for block in blocks) - 10)
    x1 = min(image.shape[1], max(block.x + block.w for block in blocks) + 10)
    y0 = max(0, int(min(block.y for block in blocks) - median_h))
    y1 = min(image.shape[0], int(max(block.y + block.h for block in blocks) + median_h * 0.55))
    light = cv2.inRange(hsv[y0:y1, x0:x1], (0, 0, 190), (179, 90, 255))
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(light, 8)
    blobs: list[tuple[int, int, int, int, str]] = []
    for index in range(1, len(stats)):
        bx, by, bw, bh, area = (int(v) for v in stats[index])
        if bh < 10 or bh > 40 or bw < 14 or bw > 180 or area < 80:
            continue
        text = _ocr_blob(image[y0 + by : y0 + by + bh, x0 + bx : x0 + bx + bw])
        if not text:
            continue
        blobs.append((x0 + bx, y0 + by, bw, bh, text))
    claimed: set[int] = set()
    for block in blocks:
        above = [
            index
            for index, blob in enumerate(blobs)
            if _blob_overlaps_block(blob, block) and blob[1] + blob[3] <= block.y + 6
        ]
        below = [
            index
            for index, blob in enumerate(blobs)
            if _blob_overlaps_block(blob, block) and blob[1] >= block.y + block.h - 6
        ]
        if above:
            pick = max(above, key=lambda index: blobs[index][1])
            block.panel_wire = blobs[pick][4]
            claimed.add(pick)
        if below:
            pick = min(below, key=lambda index: blobs[index][1])
            block.field_wire = blobs[pick][4]
            claimed.add(pick)
    names = [
        blob
        for index, blob in enumerate(blobs)
        if index not in claimed and blob[1] + blob[3] <= min(block.y for block in blocks) + 8
    ]
    if not names:
        return UNKNOWN
    names.sort(key=lambda blob: (blob[1], blob[0]))
    return names[0][4]


def _blob_overlaps_block(blob: tuple[int, int, int, int, str], block: BlockHit) -> bool:
    bx, _by, bw, _bh, _text = blob
    return min(bx + bw, block.x + block.w) - max(bx, block.x) >= min(bw, block.w) * 0.4


def _ocr_blob(bgr: np.ndarray) -> str:
    if bgr is None or bgr.size == 0 or bgr.shape[0] < 8 or bgr.shape[1] < 8:
        return ""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < 12.0:
        return ""
    up = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    return _accepted_ocr(up)


def _read_marker(image: np.ndarray, block: BlockHit) -> str:
    pad_x = max(1, int(block.w * 0.12))
    pad_y = max(1, int(block.h * 0.18))
    gray = cv2.cvtColor(
        image[block.y + pad_y : block.y + block.h - pad_y, block.x + pad_x : block.x + block.w - pad_x],
        cv2.COLOR_BGR2GRAY,
    )
    return _read_glyph(_glyph_crop(gray, nearest="largest"))


def _glyph_crop(gray: np.ndarray, *, nearest: str) -> np.ndarray | None:
    """The ink closest to the housing, or the largest ink cluster in a name band."""

    if gray.size == 0:
        return None
    # A text row varies across the line. A flat housing edge or a solid comb does not.
    row_score = gray.std(axis=1)
    rows = row_score > 35.0
    ink = rows[:, None] & (np.abs(gray.astype(np.int16) - gray.mean(axis=1, keepdims=True).astype(np.int16)) > 25)
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, on in enumerate(rows.tolist()):
        if on and start is None:
            start = index
        elif not on and start is not None:
            if index - start >= 6:
                spans.append((start, index))
            start = None
    if start is not None and len(rows) - start >= 6:
        spans.append((start, len(rows)))
    if not spans:
        return None
    height = gray.shape[0]
    # A comb or housing edge sits against the crop boundary. The marker is the
    # next line of ink in toward the open side.
    if nearest == "bottom":
        inner = [span for span in spans if span[1] < height - 2]
        chosen = (inner or spans)[-1]
    elif nearest == "top":
        inner = [span for span in spans if span[0] > 2]
        chosen = (inner or spans)[0]
    else:
        chosen = max(spans, key=lambda span: int(ink[span[0] : span[1]].sum()))
    y0, y1 = chosen
    cols = np.where(ink[y0:y1].any(axis=0))[0]
    if len(cols) < 2:
        return None
    pad = 3
    return gray[max(0, y0 - pad) : min(gray.shape[0], y1 + pad), max(0, int(cols[0]) - pad) : min(gray.shape[1], int(cols[-1]) + 1 + pad)]


def _read_glyph(gray: np.ndarray | None) -> str:
    if gray is None or gray.size == 0 or gray.shape[0] < 6 or gray.shape[1] < 4:
        return UNKNOWN
    if float(gray.std()) < 8.0:
        return UNKNOWN
    up = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    _thr, bw = cv2.threshold(up, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(bw.mean()) < 127:
        bw = 255 - bw
    bw = cv2.copyMakeBorder(bw, 18, 18, 18, 18, cv2.BORDER_CONSTANT, value=255)
    return _accepted_ocr(bw) or UNKNOWN


def _accepted_ocr(image: np.ndarray) -> str:
    """A tag or wire number both PSMs agree on, or one read above the confidence floor.

    A zero confidence score is not agreement. Words that are not tags stay unknown.
    """

    try:
        from panelcad.importer.raster import configure_tesseract
        import pytesseract
    except ImportError:
        return ""
    if not configure_tesseract():
        return ""
    config = "--psm {psm} -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-"

    def once(psm: int) -> tuple[str, float]:
        try:
            heard = pytesseract.image_to_string(image, config=config.format(psm=psm))
            data = pytesseract.image_to_data(
                image,
                output_type=pytesseract.Output.DICT,
                config=config.format(psm=psm),
            )
        except Exception:  # noqa: BLE001
            return "", 0.0
        token = ""
        for piece in str(heard or "").replace("\n", " ").split():
            token = _plausible_tag(piece)
            if token:
                break
        if not token:
            return "", 0.0
        scores: list[float] = []
        for raw, conf in zip(data.get("text") or [], data.get("conf") or [], strict=False):
            if _plausible_tag(raw) != token:
                continue
            try:
                scores.append(float(conf))
            except (TypeError, ValueError):
                continue
        return token, max(scores) if scores else 0.0

    first, first_score = once(7)
    second, second_score = once(8)
    if first and first == second:
        return first
    if first and first_score >= 60:
        return first
    if second and second_score >= 60:
        return second
    return ""


def _plausible_tag(value: Any) -> str:
    token = _readable(value)
    if token and _TAG_RE.match(token):
        return token
    return ""


def _readable(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.lower() in _BLANK_TEXT:
        return ""
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch == "-").strip("-")
    if not cleaned or len(cleaned) > 16:
        return ""
    return cleaned


def _strip_has_unknown(hit: StripHit, read_name: str) -> bool:
    if read_name == UNKNOWN or hit.count_uncertain or not hit.blocks:
        return True
    return any(
        block.marker == UNKNOWN or block.panel_wire == UNKNOWN or block.field_wire == UNKNOWN for block in hit.blocks
    )


def _matching_extract(
    hit: StripHit,
    reported: list[dict[str, Any]],
    width: float,
    height: float,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in reported:
        region = item.get("region")
        score = 0.0
        if region is not None and float(getattr(region, "w", 0) or 0) > 0 and float(getattr(region, "h", 0) or 0) > 0:
            score = _overlap_ratio(hit, region)
        else:
            for term in item["terminals"]:
                point = _center_from(term, width, height)
                if point is None:
                    continue
                if hit.x <= point[0] <= hit.x + hit.w and hit.y <= point[1] <= hit.y + hit.h:
                    score = 0.5
                    break
        if score > best_score:
            best = item
            best_score = score
    if best_score <= 0:
        return None
    return best


def _matching_terminal(
    block: BlockHit | None,
    terminals: list[dict[str, Any]],
    width: float,
    height: float,
) -> dict[str, Any] | None:
    if block is None:
        return None
    from panelcad.reconstruct.service import _region

    best = None
    best_score = 0.0
    for term in terminals:
        region = _region(term, width, height)
        if region is None or float(region.w or 0) <= 0:
            cx = _center_from(term, width, height)
            if cx is None:
                continue
            if block.x <= cx[0] <= block.x + block.w and block.y <= cx[1] <= block.y + block.h:
                return term
            continue
        score = _rect_overlap(block, region)
        if score > best_score:
            best = term
            best_score = score
    return best if best_score > 0 else None


def _block_at_wire(hit: StripHit, wire: dict[str, Any], width: float, height: float) -> BlockHit | None:
    point = _center_from(wire, width, height)
    if point is None:
        return None
    for block in hit.blocks:
        if block.x - 4 <= point[0] <= block.x + block.w + 4:
            return block
    return None


def _center_from(raw: dict[str, Any], width: float, height: float) -> tuple[float, float] | None:
    from panelcad.reconstruct.service import _region, _safe_float

    region = _region(raw, width, height)
    if region is not None and (region.w or region.h):
        return (region.x + region.w / 2.0, region.y + region.h / 2.0)
    nx = _safe_float(raw.get("nx"), None)
    ny = _safe_float(raw.get("ny"), None)
    if nx is None or ny is None:
        return None
    if nx <= 1.5 and ny <= 1.5:
        return (nx * width, ny * height)
    return (nx, ny)


def _rect_overlap(block: BlockHit, region: PhotoRegion) -> float:
    return _overlap_ratio(
        StripHit(x=block.x, y=block.y, w=block.w, h=block.h),
        region,
    )


def _wire_band(block: BlockHit, side: str, photo: Photo) -> PhotoRegion:
    band_h = max(8, int(block.h * 0.35))
    if side == PANEL_SIDE:
        y = max(0, block.y - band_h)
    else:
        y = block.y + block.h
    limit = int(photo.height_px or 0)
    if limit and y + band_h > limit:
        band_h = max(4, limit - y)
    return _region(block.x, y, block.w, band_h)


def _region(x: int, y: int, w: int, h: int) -> PhotoRegion:
    return PhotoRegion(x=float(x), y=float(y), w=float(max(1, w)), h=float(max(1, h)))


def _shift(hit: StripHit, dx: int, dy: int) -> None:
    hit.x += dx
    hit.y += dy
    for block in hit.blocks:
        block.x += dx
        block.y += dy


def _photo_path(session: Any, photo: Photo) -> Path | None:
    base = getattr(session, "path", None)
    resolved = resolve_path(photo.path, base)
    if resolved.is_file():
        return resolved
    source = Path(photo.source_path) if photo.source_path else None
    if source is not None and source.is_file():
        return source
    direct = Path(photo.path)
    if direct.is_file():
        return direct
    return None


def _unique_strip_name(session: Any, store: Reconstruction, wanted: str) -> str:
    used = _used_strip_names(session, store)
    name = wanted or UNKNOWN
    if name not in used:
        return name
    if name == UNKNOWN:
        number = 2
        while f"?-{number}" in used:
            number += 1
        return f"?-{number}"
    number = 2
    while f"{name}-{number}" in used:
        number += 1
    return f"{name}-{number}"


def _used_strip_names(session: Any, store: Reconstruction) -> set[str]:
    used: set[str] = set()
    for sheet in session.project.sheets:
        if str(sheet.strip or "").strip():
            used.add(str(sheet.strip))
        for comp in sheet.components:
            token = str((comp.properties or {}).get("strip") or "").strip()
            if token:
                used.add(token)
    for item in store.items:
        if is_strip_box(item) and str(item.tag or "").strip():
            used.add(str(item.tag))
    return used


def _unique_component_id(session: Any, stem: str) -> str:
    used = {comp.id for sheet in session.project.sheets for comp in sheet.components}
    base = "".join(ch for ch in stem if ch.isalnum() or ch in "-_") or "T"
    if base not in used:
        return base
    number = 2
    while f"{base}-{number}" in used or f"{base}{number}" in used:
        number += 1
    candidate = f"{base}{number}" if base[:1].isalpha() else f"{base}-{number}"
    if candidate in used:
        candidate = f"{base}-{number}"
    return candidate


def _ref_stem(key: str, index: int) -> str:
    if key == UNKNOWN or key.startswith("?-"):
        suffix = "1" if key == UNKNOWN else key.split("-", 1)[1]
        return f"U{suffix}"
    cleaned = "".join(ch for ch in key if ch.isalnum())
    return cleaned or f"U{index}"


def _sheet_stem(key: str) -> str:
    if key == UNKNOWN:
        return "strip-unknown"
    if key.startswith("?-"):
        return f"strip-unknown-{key.split('-', 1)[1]}"
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in key).strip("-")
    return f"strip-{cleaned or 'unknown'}"


def _next_jumper_id(session: Any) -> str:
    used: set[str] = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            token = str((comp.properties or {}).get("jumper") or "").strip()
            if token:
                used.add(token)
    number = 1
    while f"J{number}" in used:
        number += 1
    return f"J{number}"
