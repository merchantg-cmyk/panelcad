"""Customer revision control stored in the project file.

Issuing a revision snapshots the drawing (no git repository), bumps the
title-block rev, and can draw clouds around what changed since the previous
issue. Snapshots omit nested snapshots so the file stays finite.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from panelcad.core.route import component_aabb

Scheme = Literal["letter", "number"]
Box = tuple[float, float, float, float]

MERGE_GAP = 12.0
MOVE_MM = 0.5
RETAG_MM = 40.0
RATING_KEYS = (
    "rating",
    "In",
    "amps",
    "amps_min",
    "amps_max",
    "voltage",
    "fla",
    "hp",
    "kw",
    "poles",
    "coil",
)
_WORKING = {"working", "current", "head", "now"}
_COUNT_KEYS = (
    "devices_added",
    "devices_removed",
    "devices_changed",
    "wires_added",
    "wires_removed",
    "wires_renumbered",
    "terminals_added",
    "terminals_removed",
    "terminals_changed",
    "bom",
    "layout",
)


class RevisionCloud(BaseModel):
    """A revision cloud on one sheet. Coordinates are sheet millimetres, top-left origin."""

    id: str
    sheet: str
    x: float
    y: float
    w: float
    h: float
    rev: str = ""
    manual: bool = False
    note: str = ""


class IssuedRevision(BaseModel):
    """One issued snapshot. ``snapshot`` is the project without nested snapshots."""

    rev: str
    date: str = ""
    description: str = ""
    drawn: str = ""
    checked: str = ""
    approved: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)


class RevisionControl(BaseModel):
    """Offline revision history kept inside the ``.pcad.yaml`` file."""

    scheme: Scheme = "letter"
    clear_clouds_on_issue: bool = True
    issued: list[IssuedRevision] = Field(default_factory=list)
    clouds: list[RevisionCloud] = Field(default_factory=list)


def normalize_scheme(value: str | None) -> Scheme:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
    if token in {"number", "numbers", "numeric", "digit", "digits"}:
        return "number"
    if token in {"letter", "letters", "alpha", "alphabetic"}:
        return "letter"
    raise ValueError("revision scheme must be 'letter' (A, B, C) or 'number' (0, 1, 2)")


def ensure_control(project: Any) -> RevisionControl:
    if project.revision_control is None:
        project.revision_control = RevisionControl()
    return project.revision_control


def _today() -> str:
    return date.today().isoformat()


def bump_letters(token: str) -> str:
    chars = list(token.upper())
    if not chars or any(not ch.isalpha() for ch in chars):
        return "A"
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "Z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "A"
        i -= 1
    return "A" + "".join(chars)


def _rev_rank(token: str, scheme: Scheme) -> tuple[int, str] | None:
    text = str(token or "").strip()
    if scheme == "number":
        if not text.isdigit():
            return None
        return (int(text), "")
    if not text.isalpha():
        return None
    folded = text.upper()
    return (len(folded), folded)


def highest_rev(issued: list[str], scheme: Scheme) -> str | None:
    """The highest token that fits the scheme. Letter rank is length, then A–Z."""

    best: tuple[int, str] | None = None
    found = ""
    for token in issued:
        rank = _rev_rank(token, scheme)
        if rank is None:
            continue
        if best is None or rank > best:
            best = rank
            found = str(token).strip().upper() if scheme == "letter" else str(token).strip()
    return found or None


def next_rev_token(issued: list[str], scheme: Scheme) -> str:
    """The token after the highest existing one. An empty list starts at A or 0."""

    top = highest_rev(issued, scheme)
    if top is None:
        return "0" if scheme == "number" else "A"
    if scheme == "number":
        return str(int(top) + 1)
    return bump_letters(top)


def _fits_scheme(token: str, scheme: Scheme) -> bool:
    if scheme == "number":
        return token.isdigit()
    return token.isalpha()


def _title_revision_rows(project: Any) -> list[Any]:
    return [row for row in list(getattr(project.title_block, "revisions", []) or []) if str(getattr(row, "rev", "") or "").strip()]


def existing_revision_tokens(project: Any, scheme: Scheme) -> list[str]:
    """Issued revisions plus title-block rows that fit this scheme, oldest first."""

    store = project.revision_control
    issued = [str(row.rev) for row in store.issued] if store is not None else []
    tokens: list[str] = []
    seen: set[str] = set()
    for token in issued:
        folded = token.lower()
        if folded in seen or not _fits_scheme(token, scheme):
            continue
        seen.add(folded)
        tokens.append(token.upper() if scheme == "letter" and token.isalpha() else token)
    # Title-block rows are newest-first. Walk them oldest-first so a typed A
    # stays ahead of a later B.
    for row in reversed(_title_revision_rows(project)):
        token = str(row.rev).strip()
        folded = token.lower()
        if folded in seen or not _fits_scheme(token, scheme):
            continue
        seen.add(folded)
        tokens.append(token.upper() if scheme == "letter" and token.isalpha() else token)
    return tokens


def resolve_revision_token(
    project: Any,
    rev: str | None = None,
    scheme: str | None = None,
) -> str:
    """The rev an issue would use. Does not modify the project.

    A letter already on the title block counts as existing. The next issue is
    the letter after the highest one, and it is appended.
    """

    store = project.revision_control
    use: Scheme = store.scheme if store is not None else "letter"
    if scheme:
        use = normalize_scheme(scheme)
    issued = [row.rev for row in store.issued] if store is not None else []
    issued_fold = {row.lower() for row in issued}
    title_fold = {str(row.rev).strip().lower() for row in _title_revision_rows(project)}
    explicit = str(rev or "").strip()
    if explicit:
        if use == "letter" and explicit.isalpha():
            explicit = explicit.upper()
        if explicit.lower() in issued_fold:
            raise ValueError(f"revision '{explicit}' is already issued")
        if explicit.lower() in title_fold:
            raise ValueError(f"revision '{explicit}' is already on the title block")
        return explicit
    return next_rev_token(existing_revision_tokens(project, use), use)


def preview_next_rev(project: Any, scheme: str | None = None) -> str:
    return resolve_revision_token(project, scheme=scheme)


def snapshot_body(project: Any) -> dict[str, Any]:
    """Project JSON without nested revision snapshots."""

    data = project.model_dump(mode="json", by_alias=True, exclude_none=True)
    data.pop("activity", None)
    data.pop("last_saved", None)
    control = data.get("revision_control")
    if isinstance(control, dict):
        control.pop("issued", None)
        if not control:
            data.pop("revision_control", None)
    return data


def project_from_snapshot(data: dict[str, Any]) -> Any:
    from panelcad.core.models import Project

    payload = dict(data)
    control = payload.get("revision_control")
    if isinstance(control, dict):
        cleaned = dict(control)
        cleaned.pop("issued", None)
        if cleaned:
            payload["revision_control"] = cleaned
        else:
            payload.pop("revision_control", None)
    return Project.model_validate(payload)


def _box_dict(box: Box) -> dict[str, float]:
    return {
        "x": round(float(box[0]), 3),
        "y": round(float(box[1]), 3),
        "w": round(float(box[2]), 3),
        "h": round(float(box[3]), 3),
    }


def _union(a: Box, b: Box) -> Box:
    x0 = min(a[0], b[0])
    y0 = min(a[1], b[1])
    x1 = max(a[0] + a[2], b[0] + b[2])
    y1 = max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


def _near(a: Box, b: Box, gap: float) -> bool:
    return (
        a[0] - gap < b[0] + b[2]
        and b[0] - gap < a[0] + a[2]
        and a[1] - gap < b[1] + b[3]
        and b[1] - gap < a[1] + a[3]
    )


def merge_boxes(boxes: list[Box], gap: float = MERGE_GAP) -> list[Box]:
    """Union boxes whose edges are within ``gap`` millimetres."""

    return [box for box, _note in _merge_noted([(box, "") for box in boxes], gap)]


def _merge_noted(items: list[tuple[Box, str]], gap: float = MERGE_GAP) -> list[tuple[Box, str]]:
    """Union nearby boxes. A note is kept only when every merged box has one."""

    rects = [(box, str(note or "").strip()) for box, note in items if box[2] > 0.5 and box[3] > 0.5]
    changed = True
    while changed and len(rects) > 1:
        changed = False
        out: list[tuple[Box, str]] = []
        used = [False] * len(rects)
        for i, (raw, note) in enumerate(rects):
            if used[i]:
                continue
            acc = raw
            notes = [note]
            used[i] = True
            for j in range(i + 1, len(rects)):
                if used[j]:
                    continue
                other, other_note = rects[j]
                if not _near(acc, other, gap):
                    continue
                acc = _union(acc, other)
                notes.append(other_note)
                used[j] = True
                changed = True
            if any(not item for item in notes):
                joined = ""
            else:
                seen: list[str] = []
                for item in notes:
                    if item not in seen:
                        seen.append(item)
                joined = "; ".join(seen)
            out.append((acc, joined))
        rects = out
    return rects


def _is_layout(kind: str) -> bool:
    return kind in {"panel_layout", "door_layout"}


def _device(catalog: Any, library_id: str) -> Any:
    try:
        return catalog.get(library_id)
    except KeyError:
        return None


def _merged_props(comp: Any, device: Any) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if device is not None and getattr(device, "properties", None):
        props.update(device.properties)
    if getattr(comp, "properties", None):
        props.update(comp.properties)
    return props


def _text_prop(props: dict[str, Any], key: str) -> str:
    return str(props.get(key) or "").strip()


def _rating(props: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in RATING_KEYS:
        val = props.get(key)
        if val is None or str(val).strip() == "":
            continue
        parts.append(f"{key}={val}")
    return " ".join(parts)


def _is_terminal(comp: Any, device: Any) -> bool:
    if device is not None:
        from panelcad.core.electrical import symbol_role

        if symbol_role(device) == "terminal":
            return True
    lib = str(comp.library_id or "").lower()
    return ".terminals." in lib or lib.endswith(".terminal")


def _component_box(sheet: Any, comp: Any, device: Any) -> Box:
    scale = comp.visual_scale() if hasattr(comp, "visual_scale") else 1.0
    if _is_layout(sheet.kind) and device is not None:
        from panelcad.library.panel import display_footprint_size

        width, height = display_footprint_size(device, comp)
        if comp.rotation in {90, 270}:
            width, height = height, width
        return (float(comp.x), float(comp.y), max(width, 1.0), max(height, 1.0))
    if device is not None:
        width = float(device.schematic_width) * scale
        height = float(device.schematic_height) * scale
    else:
        width, height = 24.0 * scale, 40.0 * scale
    x0, y0, x1, y1 = component_aabb(
        float(comp.x),
        float(comp.y),
        max(width, 1.0),
        max(height, 1.0),
        int(comp.rotation or 0),
        bool(comp.mirror),
    )
    return (x0, y0, max(x1 - x0, 1.0), max(y1 - y0, 1.0))


@dataclass
class CompRec:
    sheet: str
    tag: str
    library_id: str
    part_number: str
    catalog: str
    manufacturer: str
    rating: str
    x: float
    y: float
    kind: str
    terminal: bool
    strip: str
    level: str
    jumper: str
    box: Box
    name: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.sheet, self.tag)


def _comp_rec(sheet: Any, comp: Any, catalog: Any) -> CompRec:
    device = _device(catalog, comp.library_id)
    props = _merged_props(comp, device)
    terminal = _is_terminal(comp, device)
    strip = ""
    level = ""
    if terminal and device is not None:
        from panelcad.core.electrical import terminal_identity

        ident = terminal_identity(comp, device)
        if ident:
            strip, level = ident
    if terminal and not strip:
        strip = _text_prop(props, "strip") or comp.id
    if terminal and not level:
        level = _text_prop(props, "terminal") or "1"
    return CompRec(
        sheet=sheet.id,
        tag=comp.id,
        library_id=comp.library_id,
        part_number=_text_prop(props, "part_number"),
        catalog=_text_prop(props, "catalog"),
        manufacturer=_text_prop(props, "manufacturer"),
        rating=_rating(props),
        x=float(comp.x),
        y=float(comp.y),
        kind=sheet.kind,
        terminal=terminal,
        strip=strip,
        level=level,
        jumper=_text_prop(props, "jumper"),
        box=_component_box(sheet, comp, device),
        name=str(getattr(device, "name", "") or "").strip() if device is not None else "",
    )


def _components(project: Any, catalog: Any) -> list[CompRec]:
    rows: list[CompRec] = []
    for sheet in project.sheets:
        for comp in sheet.components:
            rows.append(_comp_rec(sheet, comp, catalog))
    return rows


@dataclass
class WireRec:
    sheet: str
    id: str
    frm: str
    to: str
    number: str
    box: Box | None


def _wire_box(sheet: Any, wire: Any, catalog: Any) -> Box | None:
    boxes: list[Box] = []
    for ref in (wire.frm, wire.to):
        tag = str(ref).split(".", 1)[0]
        comp = next((item for item in sheet.components if item.id == tag), None)
        if comp is None:
            continue
        boxes.append(_component_box(sheet, comp, _device(catalog, comp.library_id)))
    points = list(getattr(wire, "points", []) or [])
    if points:
        xs = [float(p.x) for p in points]
        ys = [float(p.y) for p in points]
        boxes.append((min(xs), min(ys), max(max(xs) - min(xs), 1.0), max(max(ys) - min(ys), 1.0)))
    if not boxes:
        return None
    acc = boxes[0]
    for box in boxes[1:]:
        acc = _union(acc, box)
    return acc


def _wires(project: Any, catalog: Any) -> list[WireRec]:
    rows: list[WireRec] = []
    for sheet in project.sheets:
        for wire in sheet.wires:
            rows.append(
                WireRec(
                    sheet=sheet.id,
                    id=wire.id,
                    frm=str(wire.frm),
                    to=str(wire.to),
                    number=str(wire.number or "").strip(),
                    box=_wire_box(sheet, wire, catalog),
                )
            )
    return rows


def _zero_counts() -> dict[str, int]:
    return {key: 0 for key in _COUNT_KEYS}


def _empty_report(left: str, right: str) -> dict[str, Any]:
    return {
        "left": left,
        "right": right,
        "devices": {"added": [], "removed": [], "changed": []},
        "wires": {"added": [], "removed": [], "renumbered": []},
        "terminals": {"added": [], "removed": [], "changed": []},
        "bom": [],
        "layout": [],
        "rows": [],
        "areas": [],
        "counts": _zero_counts(),
    }


def _area(areas: list[dict[str, Any]], sheet: str, box: Box | None, note: str = "") -> None:
    if box is None or box[2] <= 0 or box[3] <= 0:
        return
    areas.append({"sheet": sheet, "note": str(note or "").strip(), **_box_dict(box)})


def _field_map(old: CompRec, new: CompRec, *, force_tag: bool) -> dict[str, dict[str, str]]:
    fields: dict[str, dict[str, str]] = {}
    if force_tag or old.tag != new.tag:
        fields["tag"] = {"from": old.tag, "to": new.tag}
    for name in ("part_number", "catalog", "manufacturer", "rating", "library_id"):
        before = getattr(old, name)
        after = getattr(new, name)
        if before != after:
            fields[name] = {"from": before, "to": after}
    return fields


def _pair_retags(removed: list[CompRec], added: list[CompRec]) -> list[tuple[CompRec, CompRec]]:
    candidates: list[tuple[float, int, int]] = []
    for i, old in enumerate(removed):
        for j, new in enumerate(added):
            if old.sheet != new.sheet or old.library_id != new.library_id:
                continue
            dist = math.hypot(old.x - new.x, old.y - new.y)
            if dist <= RETAG_MM:
                candidates.append((dist, i, j))
    candidates.sort()
    used_old: set[int] = set()
    used_new: set[int] = set()
    pairs: list[tuple[CompRec, CompRec]] = []
    for _dist, i, j in candidates:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        pairs.append((removed[i], added[j]))
    return pairs


def _pos(item: CompRec) -> str:
    return f"{item.x:g},{item.y:g}"


def _comp_public(item: CompRec) -> dict[str, Any]:
    return {
        "sheet": item.sheet,
        "tag": item.tag,
        "library_id": item.library_id,
        "part_number": item.part_number,
        "catalog": item.catalog,
        "manufacturer": item.manufacturer,
        "rating": item.rating,
        "x": item.x,
        "y": item.y,
        "strip": item.strip,
        "level": item.level,
        "jumper": item.jumper,
        "box": _box_dict(item.box),
        "name": item.name,
    }


def _compare_components(
    left: list[CompRec],
    right: list[CompRec],
    areas: list[dict[str, Any]],
) -> tuple[dict[str, list], dict[str, list], list[dict[str, Any]]]:
    devices: dict[str, list] = {"added": [], "removed": [], "changed": []}
    terminals: dict[str, list] = {"added": [], "removed": [], "changed": []}
    layout: list[dict[str, Any]] = []
    left_map = {item.key: item for item in left}
    right_map = {item.key: item for item in right}
    matched: set[tuple[str, str]] = set()

    def emit_pair(old: CompRec, new: CompRec, *, force_tag: bool) -> None:
        fields = _field_map(old, new, force_tag=force_tag)
        moved = abs(old.x - new.x) > MOVE_MM or abs(old.y - new.y) > MOVE_MM
        if _is_layout(new.kind) and moved:
            layout.append(
                {
                    "sheet": new.sheet,
                    "tag": new.tag,
                    "x_from": old.x,
                    "y_from": old.y,
                    "x_to": new.x,
                    "y_to": new.y,
                    "box": _box_dict(new.box),
                    "old_box": _box_dict(old.box),
                }
            )
            _area(areas, new.sheet, new.box)
            _area(areas, old.sheet, old.box, note=f"{old.tag} moved")
        elif moved:
            fields["position"] = {"from": _pos(old), "to": _pos(new)}
        if old.terminal or new.terminal:
            term_fields = {
                name: fields[name]
                for name in ("tag", "part_number", "catalog", "manufacturer", "rating", "library_id", "position")
                if name in fields
            }
            for name in ("strip", "level", "jumper"):
                before = getattr(old, name)
                after = getattr(new, name)
                if before != after:
                    term_fields[name] = {"from": before, "to": after}
            if term_fields:
                terminals["changed"].append(
                    {
                        "sheet": new.sheet,
                        "tag": new.tag,
                        "was": old.tag,
                        "fields": term_fields,
                        "box": _box_dict(new.box),
                    }
                )
                _area(areas, new.sheet, new.box)
            return
        if fields:
            devices["changed"].append(
                {
                    "sheet": new.sheet,
                    "tag": new.tag,
                    "was": old.tag,
                    "fields": fields,
                    "box": _box_dict(new.box),
                }
            )
            _area(areas, new.sheet, new.box)

    for key, old in left_map.items():
        new = right_map.get(key)
        if new is None:
            continue
        matched.add(key)
        emit_pair(old, new, force_tag=False)

    unmatched_old = [item for key, item in left_map.items() if key not in matched]
    unmatched_new = [item for key, item in right_map.items() if key not in matched]
    paired_old: set[tuple[str, str]] = set()
    paired_new: set[tuple[str, str]] = set()
    for old, new in _pair_retags(unmatched_old, unmatched_new):
        paired_old.add(old.key)
        paired_new.add(new.key)
        emit_pair(old, new, force_tag=True)

    for item in unmatched_old:
        if item.key in paired_old:
            continue
        bucket = terminals if item.terminal else devices
        bucket["removed"].append(_comp_public(item))
        _area(areas, item.sheet, item.box, note=f"{item.tag} deleted")
    for item in unmatched_new:
        if item.key in paired_new:
            continue
        bucket = terminals if item.terminal else devices
        bucket["added"].append(_comp_public(item))
        _area(areas, item.sheet, item.box)
    return devices, terminals, layout


def _compare_wires(
    left: list[WireRec],
    right: list[WireRec],
    areas: list[dict[str, Any]],
) -> dict[str, list]:
    wires: dict[str, list] = {"added": [], "removed": [], "renumbered": []}
    left_by_id = {(item.sheet, item.id): item for item in left}
    right_by_id = {(item.sheet, item.id): item for item in right}
    consumed_left: set[tuple[str, str]] = set()
    consumed_right: set[tuple[str, str]] = set()

    def emit_renumber(old: WireRec, new: WireRec) -> None:
        wires["renumbered"].append(
            {
                "sheet": new.sheet,
                "id": new.id,
                "from": new.frm,
                "to": new.to,
                "number_from": old.number,
                "number_to": new.number,
                "box": _box_dict(new.box) if new.box else None,
            }
        )
        _area(areas, new.sheet, new.box or old.box)

    def emit_add(item: WireRec) -> None:
        wires["added"].append(
            {
                "sheet": item.sheet,
                "id": item.id,
                "from": item.frm,
                "to": item.to,
                "number": item.number,
                "box": _box_dict(item.box) if item.box else None,
            }
        )
        _area(areas, item.sheet, item.box)

    def emit_remove(item: WireRec) -> None:
        wires["removed"].append(
            {
                "sheet": item.sheet,
                "id": item.id,
                "from": item.frm,
                "to": item.to,
                "number": item.number,
                "box": _box_dict(item.box) if item.box else None,
            }
        )
        _area(areas, item.sheet, item.box)

    for key, old in left_by_id.items():
        new = right_by_id.get(key)
        if new is None:
            continue
        consumed_left.add(key)
        consumed_right.add(key)
        same_ends = old.frm == new.frm and old.to == new.to
        if same_ends and old.number != new.number:
            emit_renumber(old, new)
        elif not same_ends:
            emit_remove(old)
            emit_add(new)

    left_open = [item for key, item in left_by_id.items() if key not in consumed_left]
    right_open = [item for key, item in right_by_id.items() if key not in consumed_right]
    right_by_ends: dict[tuple[str, str, str], list[WireRec]] = {}
    for item in right_open:
        right_by_ends.setdefault((item.sheet, item.frm, item.to), []).append(item)
    for old in left_open:
        bucket = right_by_ends.get((old.sheet, old.frm, old.to)) or []
        if not bucket:
            continue
        new = bucket.pop(0)
        consumed_left.add((old.sheet, old.id))
        consumed_right.add((new.sheet, new.id))
        if old.number != new.number:
            emit_renumber(old, new)
    for item in left_open:
        if (item.sheet, item.id) not in consumed_left:
            emit_remove(item)
    for item in right_open:
        if (item.sheet, item.id) not in consumed_right:
            emit_add(item)
    return wires


def _bom_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        str(row.get("library_id") or ""),
        str(row.get("manufacturer") or ""),
        str(row.get("part_number") or ""),
        str(row.get("catalog") or ""),
    )


def _qty(row: dict[str, str] | None) -> int:
    if not row:
        return 0
    try:
        return int(float(row.get("qty") or 0))
    except (TypeError, ValueError):
        return 0


def _describe_device(device: Any, props: dict[str, Any]) -> str:
    """Library name with an instance rating written in when it differs from the catalog."""

    explicit = str(props.get("description") or "").strip()
    if explicit:
        return explicit
    name = str(getattr(device, "name", "") or "").strip()
    defaults = getattr(device, "properties", None) or {}
    for key in ("In", "amps", "rating", "voltage"):
        current = str(props.get(key) or "").strip()
        library = str(defaults.get(key) or "").strip()
        if current and library and current != library and library in name:
            return name.replace(library, current, 1)
    return name


def _bom_faces(project: Any, catalog: Any) -> dict[tuple[str, str, str, str], dict[str, str]]:
    """Description and rating for each BOM key, from that snapshot's own instances."""

    from panelcad.core.jumper import is_jumper_device

    faces: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            device = _device(catalog, comp.library_id)
            if is_jumper_device(device):
                continue
            props = _merged_props(comp, device)
            key = (
                str(comp.library_id or ""),
                _text_prop(props, "manufacturer"),
                _text_prop(props, "part_number"),
                _text_prop(props, "catalog"),
            )
            if key in faces:
                continue
            faces[key] = {
                "name": _describe_device(device, props),
                "rating": _rating(props),
            }
    return faces


def _compare_bom(left: Any, right: Any, catalog: Any) -> list[dict[str, Any]]:
    from panelcad.io.bom import grouped_rows

    before = {_bom_key(row): row for row in grouped_rows(left, catalog)}
    after = {_bom_key(row): row for row in grouped_rows(right, catalog)}
    before_faces = _bom_faces(left, catalog)
    after_faces = _bom_faces(right, catalog)
    rows: list[dict[str, Any]] = []
    for key in list(dict.fromkeys([*before.keys(), *after.keys()])):
        old = before.get(key)
        new = after.get(key)
        q0, q1 = _qty(old), _qty(new)
        if q0 == q1:
            continue
        sample = new or old or {}
        part = str(
            sample.get("part_number") or sample.get("catalog") or sample.get("library_id") or ""
        )
        added = q0 == 0 and q1 > 0
        face = (after_faces if added else before_faces).get(key) or {}
        rows.append(
            {
                "change": "qty",
                "sheet": str(sample.get("sheet") or ""),
                "tag": str(sample.get("ref") or ""),
                "field": "qty",
                "before": str(q0),
                "after": str(q1),
                "detail": part,
                "library_id": key[0],
                "manufacturer": key[1],
                "part_number": key[2],
                "catalog": key[3],
                "name": str(face.get("name") or sample.get("description") or ""),
                "rating": str(face.get("rating") or ""),
            }
        )
    return rows


def _part_rows(changed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in changed:
        for field in ("part_number", "catalog", "manufacturer"):
            change = item["fields"].get(field)
            if not change:
                continue
            rows.append(
                {
                    "change": "part",
                    "sheet": item["sheet"],
                    "tag": item["tag"],
                    "field": field,
                    "before": change["from"],
                    "after": change["to"],
                    "detail": item.get("was") or item["tag"],
                    "library_id": "",
                    "manufacturer": "",
                    "part_number": change["to"] if field == "part_number" else "",
                    "catalog": change["to"] if field == "catalog" else "",
                }
            )
    return rows


def _flat_rows(
    devices: dict[str, list],
    wires: dict[str, list],
    terminals: dict[str, list],
    bom: list[dict[str, Any]],
    layout: list[dict[str, Any]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    def add(
        section: str,
        change: str,
        sheet: str,
        tag: str,
        field: str,
        before: str,
        after: str,
        detail: str,
        *,
        name: str = "",
        library_id: str = "",
        rating: str = "",
    ) -> None:
        row = {
            "section": section,
            "change": change,
            "sheet": sheet,
            "tag": tag,
            "field": field,
            "before": before,
            "after": after,
            "detail": detail,
        }
        if name:
            row["name"] = name
        if library_id:
            row["library_id"] = library_id
        if rating:
            row["rating"] = rating
        rows.append(row)

    for item in devices["added"]:
        add(
            "device",
            "added",
            item["sheet"],
            item["tag"],
            "library_id",
            "",
            item["library_id"],
            item["part_number"],
            name=str(item.get("name") or ""),
        )
    for item in devices["removed"]:
        add(
            "device",
            "removed",
            item["sheet"],
            item["tag"],
            "library_id",
            item["library_id"],
            "",
            item["part_number"],
            name=str(item.get("name") or ""),
        )
    for item in devices["changed"]:
        for field, change in item["fields"].items():
            add("device", "changed", item["sheet"], item["tag"], field, change["from"], change["to"], item.get("was") or "")
    for item in wires["added"]:
        add("wire", "added", item["sheet"], item["id"], "wire", "", item.get("number") or "", f"{item['from']} -> {item['to']}")
    for item in wires["removed"]:
        add("wire", "removed", item["sheet"], item["id"], "wire", item.get("number") or "", "", f"{item['from']} -> {item['to']}")
    for item in wires["renumbered"]:
        add(
            "wire",
            "renumbered",
            item["sheet"],
            item["id"],
            "number",
            item["number_from"],
            item["number_to"],
            f"{item['from']} -> {item['to']}",
        )
    for item in terminals["added"]:
        add("terminal", "added", item["sheet"], item["tag"], "terminal", "", item.get("level") or "", item.get("strip") or "")
    for item in terminals["removed"]:
        add("terminal", "removed", item["sheet"], item["tag"], "terminal", item.get("level") or "", "", item.get("strip") or "")
    for item in terminals["changed"]:
        for field, change in item["fields"].items():
            add("terminal", "changed", item["sheet"], item["tag"], field, change["from"], change["to"], item.get("was") or "")
    for item in bom:
        add(
            "bom",
            str(item.get("change") or "qty"),
            str(item.get("sheet") or ""),
            str(item.get("tag") or ""),
            str(item.get("field") or ""),
            str(item.get("before") or ""),
            str(item.get("after") or ""),
            str(item.get("detail") or ""),
            name=str(item.get("name") or ""),
            library_id=str(item.get("library_id") or ""),
            rating=str(item.get("rating") or ""),
        )
    for item in layout:
        add(
            "layout",
            "moved",
            item["sheet"],
            item["tag"],
            "position",
            f"{item['x_from']:g},{item['y_from']:g}",
            f"{item['x_to']:g},{item['y_to']:g}",
            "",
        )
    return rows


def compare_projects(left: Any, right: Any, catalog: Any, *, left_name: str, right_name: str) -> dict[str, Any]:
    areas: list[dict[str, Any]] = []
    devices, terminals, layout = _compare_components(_components(left, catalog), _components(right, catalog), areas)
    wires = _compare_wires(_wires(left, catalog), _wires(right, catalog), areas)
    bom = _compare_bom(left, right, catalog)
    bom.extend(_part_rows(devices["changed"]))
    bom.extend(_part_rows(terminals["changed"]))
    rows = _flat_rows(devices, wires, terminals, bom, layout)
    counts = _zero_counts()
    counts["devices_added"] = len(devices["added"])
    counts["devices_removed"] = len(devices["removed"])
    counts["devices_changed"] = len(devices["changed"])
    counts["wires_added"] = len(wires["added"])
    counts["wires_removed"] = len(wires["removed"])
    counts["wires_renumbered"] = len(wires["renumbered"])
    counts["terminals_added"] = len(terminals["added"])
    counts["terminals_removed"] = len(terminals["removed"])
    counts["terminals_changed"] = len(terminals["changed"])
    counts["bom"] = len(bom)
    counts["layout"] = len(layout)
    return {
        "left": left_name,
        "right": right_name,
        "devices": devices,
        "wires": wires,
        "terminals": terminals,
        "bom": bom,
        "layout": layout,
        "rows": rows,
        "areas": areas,
        "counts": counts,
    }


def _lookup_endpoint(project: Any, token: str) -> tuple[Any, str]:
    name = str(token or "").strip()
    if not name:
        raise ValueError("revision name is empty")
    if name.lower() in _WORKING:
        return project, "working"
    store = project.revision_control
    issued = list(store.issued) if store is not None else []
    for row in issued:
        if row.rev.lower() == name.lower():
            if not row.snapshot:
                raise ValueError(f"revision '{row.rev}' has no snapshot")
            return project_from_snapshot(row.snapshot), row.rev
    known = ", ".join(row.rev for row in issued) or "none"
    raise ValueError(f"unknown revision '{token}'. Issued: {known}. Use 'working' for the open drawing.")


def compare_revisions(project: Any, catalog: Any, left: str, right: str) -> dict[str, Any]:
    left_project, left_name = _lookup_endpoint(project, left)
    right_project, right_name = _lookup_endpoint(project, right)
    return compare_projects(left_project, right_project, catalog, left_name=left_name, right_name=right_name)


def _next_cloud_id(store: RevisionControl) -> str:
    taken = {cloud.id for cloud in store.clouds}
    n = 1
    while f"cloud{n}" in taken:
        n += 1
    return f"cloud{n}"


def clouds_for_areas(store: RevisionControl, areas: list[dict[str, Any]], rev: str) -> list[RevisionCloud]:
    grouped: dict[str, list[tuple[Box, str]]] = {}
    for area in areas:
        box = (float(area["x"]), float(area["y"]), float(area["w"]), float(area["h"]))
        grouped.setdefault(str(area["sheet"]), []).append((box, str(area.get("note") or "")))
    created: list[RevisionCloud] = []
    taken = {cloud.id for cloud in store.clouds}
    n = 1
    for sheet, items in grouped.items():
        for box, note in _merge_noted(items, MERGE_GAP):
            while f"cloud{n}" in taken:
                n += 1
            cid = f"cloud{n}"
            taken.add(cid)
            n += 1
            created.append(
                RevisionCloud(
                    id=cid,
                    sheet=sheet,
                    x=round(box[0], 3),
                    y=round(box[1], 3),
                    w=round(box[2], 3),
                    h=round(box[3], 3),
                    rev=rev,
                    manual=False,
                    note=note,
                )
            )
    return created


def _append_title_row(
    project: Any,
    rev: str,
    when: str,
    description: str,
    drawn: str,
    checked: str,
    approved: str,
) -> None:
    """Add a title-block row. An existing rev is left as it was."""

    from panelcad.core.models import Revision

    tb = project.title_block
    if any(str(item.rev) == rev for item in tb.revisions):
        raise ValueError(f"revision '{rev}' is already on the title block")
    tb.revisions.insert(
        0,
        Revision(
            rev=rev,
            date=when,
            description=description,
            drawn=drawn,
            checked=checked,
            approved=approved,
        ),
    )


def issue_revision(
    project: Any,
    catalog: Any,
    *,
    description: str = "",
    rev: str | None = None,
    scheme: str | None = None,
    date: str | None = None,
    drawn: str = "",
    checked: str = "",
    approved: str = "",
    clear_clouds: bool | None = None,
    auto_clouds: bool = True,
) -> dict[str, Any]:
    store = ensure_control(project)
    if scheme:
        store.scheme = normalize_scheme(scheme)
    token = resolve_revision_token(project, rev=rev)
    previous = None
    if store.issued:
        previous = project_from_snapshot(store.issued[-1].snapshot)
    if previous is None:
        report = _empty_report("", token)
    else:
        report = compare_projects(previous, project, catalog, left_name=store.issued[-1].rev, right_name=token)
    do_clear = store.clear_clouds_on_issue if clear_clouds is None else bool(clear_clouds)
    if clear_clouds is not None:
        store.clear_clouds_on_issue = bool(clear_clouds)
    if do_clear:
        store.clouds = [cloud for cloud in store.clouds if (cloud.manual and not cloud.rev) or cloud.rev == token]
    if auto_clouds and previous is not None:
        store.clouds.extend(clouds_for_areas(store, report["areas"], token))
    for cloud in store.clouds:
        if not str(cloud.rev or "").strip():
            cloud.rev = token
    when = str(date or "").strip() or _today()
    _append_title_row(project, token, when, str(description or ""), str(drawn or ""), str(checked or ""), str(approved or ""))
    store.issued.append(
        IssuedRevision(
            rev=token,
            date=when,
            description=str(description or ""),
            drawn=str(drawn or ""),
            checked=str(checked or ""),
            approved=str(approved or ""),
            snapshot=snapshot_body(project),
        )
    )
    fresh = [cloud.id for cloud in store.clouds if cloud.rev == token]
    return {
        "rev": token,
        "date": when,
        "description": str(description or ""),
        "drawn": str(drawn or ""),
        "checked": str(checked or ""),
        "approved": str(approved or ""),
        "scheme": store.scheme,
        "current": token,
        "clouds": len(fresh),
        "cloud_ids": fresh,
        "cleared_previous": do_clear,
        "changes": report["counts"],
    }


def _normalize_rect(x: float, y: float, w: float, h: float) -> Box:
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    if w < 3.0 or h < 3.0:
        raise ValueError("revision cloud must be at least 3 mm by 3 mm")
    return (x, y, w, h)


def add_revision_cloud(
    project: Any,
    sheet_id: str,
    x: float,
    y: float,
    w: float,
    h: float,
    rev: str = "",
) -> dict[str, Any]:
    box = _normalize_rect(float(x), float(y), float(w), float(h))
    store = ensure_control(project)
    cloud = RevisionCloud(
        id=_next_cloud_id(store),
        sheet=str(sheet_id),
        x=round(box[0], 3),
        y=round(box[1], 3),
        w=round(box[2], 3),
        h=round(box[3], 3),
        rev=str(rev or ""),
        manual=True,
    )
    store.clouds.append(cloud)
    return cloud.model_dump()


def clear_revision_clouds(project: Any, rev: str | None = None) -> dict[str, Any]:
    store = ensure_control(project)
    before = len(store.clouds)
    token = str(rev or "").strip()
    if token:
        store.clouds = [cloud for cloud in store.clouds if cloud.rev != token]
    else:
        store.clouds = []
    return {"removed": before - len(store.clouds), "clouds": len(store.clouds)}


def list_revisions(project: Any) -> dict[str, Any]:
    store = project.revision_control
    issued_by: dict[str, dict[str, str]] = {}
    if store is not None:
        for row in store.issued:
            issued_by[row.rev] = {
                "rev": row.rev,
                "date": row.date,
                "description": row.description,
                "drawn": row.drawn,
                "checked": row.checked,
                "approved": row.approved,
            }
    tb = _title_revision_rows(project)
    order: list[str] = []
    seen: set[str] = set()
    for row in reversed(tb):
        token = str(row.rev).strip()
        if token in seen:
            continue
        seen.add(token)
        order.append(token)
    for token in issued_by:
        if token not in seen:
            seen.add(token)
            order.append(token)
    issued: list[dict[str, str]] = []
    for token in order:
        if token in issued_by:
            issued.append(issued_by[token])
            continue
        row = next(item for item in tb if str(item.rev).strip() == token)
        issued.append(
            {
                "rev": token,
                "date": str(row.date or ""),
                "description": str(row.description or ""),
                "drawn": str(row.drawn or ""),
                "checked": str(row.checked or ""),
                "approved": str(row.approved or ""),
            }
        )
    current = str(tb[0].rev or "").strip() if tb else ""
    if not current and issued:
        current = issued[-1]["rev"]
    clouds = [cloud.model_dump() for cloud in store.clouds] if store is not None else []
    return {
        "scheme": store.scheme if store is not None else "letter",
        "current": current,
        "clear_clouds_on_issue": True if store is None else store.clear_clouds_on_issue,
        "order": "oldest_first",
        "revisions": issued,
        "clouds": clouds,
    }
