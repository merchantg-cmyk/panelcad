"""Review flags for one converted chunk. Deterministic. Never raises into the conversion."""

from __future__ import annotations

from typing import Any


def suggest_cleanup(issues: list[dict[str, Any]]) -> str:
    """Plain notes for a flagged chunk. No network and no API key."""

    if not issues:
        return ""
    lines: list[str] = []
    for issue in issues:
        detail = str(issue.get("detail") or "").strip()
        if detail:
            lines.append(detail)
    if not lines:
        return ""
    lines.append("Accept this part, run it again, or put it back.")
    return "\n".join(lines)


def _issue(flag: str, detail: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"flag": flag, "detail": detail}
    row.update(extra)
    return row


def sheet_flags(session: Any, sheet_id: str) -> list[dict[str, Any]]:
    """Overflow, crossings, text overlap, component/rail collision, and collinear wire overlap."""

    try:
        sheet = session.sheet(sheet_id)
    except KeyError:
        return []
    found: list[dict[str, Any]] = []
    found.extend(_overflow(session, sheet))
    found.extend(_component_and_rail_collisions(session, sheet))
    found.extend(_wire_overlaps(session, sheet))
    found.extend(_crossings(session, sheet))
    found.extend(_text_and_mirrors(session, sheet))
    return found


def _overflow(session: Any, sheet: Any) -> list[dict[str, Any]]:
    from panelcad.core.ink import schematic_ink_rect
    from panelcad.render.titleblock import title_block_rect, working_rect
    from panelcad.render.wiring import component_obstacle, route_sheet_wires

    wx, wy, ww, wh = working_rect(sheet)
    tx, ty, tw, _th = title_block_rect(sheet)
    frame = (wx - 1.0, wy - 1.0, wx + ww + 1.0, wy + wh + 1.0)
    rows: list[dict[str, Any]] = []
    for comp in sheet.components:
        if str(comp.properties.get("role") or "") in {"to", "from"}:
            continue
        try:
            box = component_obstacle(sheet, session.catalog, comp, pad=0.0)
        except Exception:  # noqa: BLE001
            continue
        if box[0] < frame[0] or box[1] < frame[1] or box[2] > frame[2] or box[3] > frame[3]:
            rows.append(
                _issue(
                    "layout_overflow",
                    f"{comp.id} sits outside the sheet frame",
                    ref=comp.id,
                    sheet=sheet.id,
                )
            )
        try:
            dev = session.catalog.get(comp.library_id)
            ink = schematic_ink_rect(dev, comp.x, comp.y, comp.id, component=comp)
            if ink[3] > ty + 0.2 and ink[2] > tx + 0.2 and ink[0] < tx + tw - 0.2:
                rows.append(
                    _issue(
                        "title_block_overlap",
                        f"{comp.id} overlaps the title block",
                        ref=comp.id,
                        sheet=sheet.id,
                    )
                )
        except Exception:  # noqa: BLE001
            continue
    try:
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        for wire in sheet.wires:
            pts = routes.get(wire.id) or []
            if any(
                max(a.y, b.y) > ty + 0.2 and max(a.x, b.x) > tx + 0.2 and min(a.x, b.x) < tx + tw - 0.2
                for a, b in zip(pts, pts[1:])
            ):
                rows.append(
                    _issue(
                        "title_block_overlap",
                        f"{wire.id} enters the title block",
                        ref=wire.id,
                        sheet=sheet.id,
                    )
                )
    except Exception:  # noqa: BLE001
        pass
    return rows


def _crossings(session: Any, sheet: Any) -> list[dict[str, Any]]:
    from math import hypot

    from panelcad.render.wiring import route_sheet_wires

    try:
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    except Exception:  # noqa: BLE001
        return []
    segs: list[tuple[Any, Any, Any]] = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        for a, b in zip(pts, pts[1:]):
            if hypot(b.x - a.x, b.y - a.y) < 0.4:
                continue
            segs.append((wire, a, b))
    count = 0
    for index, (wa, a1, a2) in enumerate(segs):
        for wb, b1, b2 in segs[index + 1 :]:
            if wa.id == wb.id:
                continue
            if _cross(a1, a2, b1, b2):
                count += 1
    limit = _crossing_limit(sheet)
    if count > limit:
        return [
            _issue(
                "crossings",
                f"{count} wire crossings (limit {limit})",
                sheet=sheet.id,
            )
        ]
    return []


def _crossing_limit(sheet: Any) -> int:
    roles = {str(comp.properties.get("role") or "") for comp in sheet.components}
    if "star" in roles and "delta" in roles:
        return 4
    if str(sheet.drawing_style or "") in {"ladder", "iec"} or str(sheet.kind or "") == "io":
        return 12
    return 8


def _cross(a1: Any, a2: Any, b1: Any, b2: Any) -> bool:
    denom = (a2.x - a1.x) * (b2.y - b1.y) - (a2.y - a1.y) * (b2.x - b1.x)
    if abs(denom) < 1e-6:
        return False
    t = ((b1.x - a1.x) * (b2.y - b1.y) - (b1.y - a1.y) * (b2.x - b1.x)) / denom
    u = ((b1.x - a1.x) * (a2.y - a1.y) - (b1.y - a1.y) * (a2.x - a1.x)) / denom
    return 0.04 < t < 0.96 and 0.04 < u < 0.96


def _text_and_mirrors(session: Any, sheet: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet
        from panelcad.render.titleblock import title_block_rect, working_rect
        from panelcad.render.wire_labels import boxes_overlap
        from panelcad.render.wiring import route_sheet_wires
        from panelcad.render.xref import annotation_box, place_xref_labels

        ladder = is_ladder_sheet(sheet) or is_iec_column_sheet(sheet)
        placed = place_xref_labels(session.project, session.catalog, sheet, ladder=ladder)
        wx, wy, ww, wh = working_rect(sheet)
        frame = (wx, wy, wx + ww, wy + wh)
        tx, ty, tw, th = title_block_rect(sheet)
        title = (tx, ty, tx + tw, ty + th)
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        wire_boxes: list[tuple[float, float, float, float]] = []
        for pts in routes.values():
            for a, b in zip(pts, pts[1:]):
                wire_boxes.append((min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y)))
        mirrors: list[tuple[str, tuple[float, float, float, float]]] = []
        for ann, x, y, align in placed:
            if ann.kind != "mirror":
                continue
            box = annotation_box(ann, x, y, align)
            outside = box[0] < frame[0] - 0.2 or box[1] < frame[1] - 0.2 or box[2] > frame[2] + 0.2 or box[3] > frame[3] + 0.2
            stacked = any(boxes_overlap(box, other, pad=0.2) for _owner, other in mirrors)
            on_title = boxes_overlap(box, title, pad=0.2)
            on_wire = any(boxes_overlap(box, wire_box, pad=0.15) for wire_box in wire_boxes)
            if outside or stacked or on_title or on_wire:
                rows.append(
                    _issue(
                        "text_overlap",
                        f"contact mirror for {ann.owner} overlaps the frame, a wire, or another mirror",
                        ref=ann.owner,
                        sheet=sheet.id,
                    )
                )
            mirrors.append((ann.owner, box))
    except Exception:  # noqa: BLE001
        return rows
    return rows


def _component_and_rail_collisions(session: Any, sheet: Any) -> list[dict[str, Any]]:
    from panelcad.convert.layout_convert import is_power_sheet
    from panelcad.core.ladder import RAIL_INSET, is_ladder_sheet
    from panelcad.render.titleblock import working_rect
    from panelcad.render.wire_labels import boxes_overlap
    from panelcad.render.wiring import component_obstacle

    rows: list[dict[str, Any]] = []
    if str(getattr(sheet, "kind", "") or "") not in {"schematic", "io"}:
        return rows
    boxes: list[tuple[Any, tuple[float, float, float, float]]] = []
    for comp in sheet.components:
        if str(comp.properties.get("role") or "") in {"to", "from"}:
            continue
        if "offpage" in str(comp.library_id or "").lower():
            continue
        try:
            box = component_obstacle(sheet, session.catalog, comp, pad=0.0)
            boxes.append((comp, box))
        except Exception:  # noqa: BLE001
            continue

    for idx, (ca, ba) in enumerate(boxes):
        for cb, bb in boxes[idx + 1 :]:
            if boxes_overlap(ba, bb, pad=-0.5):
                rows.append(
                    _issue(
                        "component_overlap",
                        f"{ca.id} overlaps {cb.id}",
                        ref=ca.id,
                        target=cb.id,
                        sheet=sheet.id,
                    )
                )

    if is_ladder_sheet(sheet, session.project):
        wx, _wy, ww, _wh = working_rect(sheet)
        left_x = wx + RAIL_INSET
        right_x = wx + ww - RAIL_INSET
        for comp, box in boxes:
            if (box[0] < left_x - 0.5 and box[2] > left_x + 0.5) or (
                box[0] < right_x - 0.5 and box[2] > right_x + 0.5
            ):
                rows.append(
                    _issue(
                        "rail_collision",
                        f"{comp.id} collides with the vertical ladder rail",
                        ref=comp.id,
                        sheet=sheet.id,
                    )
                )
        if is_power_sheet(sheet) and not any(
             row.get("flag") == "rail_collision" for row in rows
        ):
            first_ref = boxes[0][0].id if boxes else sheet.id
            rows.append(
                _issue(
                    "rail_collision",
                    f"3-phase power sheet {sheet.id} has vertical control ladder rails",
                    ref=first_ref,
                    sheet=sheet.id,
                )
            )
    return rows


def _wire_overlaps(session: Any, sheet: Any) -> list[dict[str, Any]]:
    from panelcad.render.wiring import route_sheet_wires

    rows: list[dict[str, Any]] = []
    if str(getattr(sheet, "kind", "") or "") not in {"schematic", "io"}:
        return rows
    try:
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    except Exception:  # noqa: BLE001
        return rows

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for w in sheet.wires:
        a, b = str(w.frm or ""), str(w.to or "")
        if a and b:
            union(a, b)
        key = str(w.net or w.number or "").strip()
        if key and a:
            union(f"net:{key}", a)

    segs: list[tuple[Any, str, Any, Any]] = []
    for w in sheet.wires:
        pts = routes.get(w.id) or []
        a_end = str(w.frm or w.to or w.id)
        net_id = find(a_end)
        for p1, p2 in zip(pts, pts[1:]):
            segs.append((w, net_id, p1, p2))

    seen_pairs: set[tuple[str, str]] = set()
    for idx, (wa, na, a1, a2) in enumerate(segs):
        for wb, nb, b1, b2 in segs[idx + 1 :]:
            if wa.id == wb.id or na == nb:
                continue
            pair = (min(wa.id, wb.id), max(wa.id, wb.id))
            if pair in seen_pairs:
                continue
            if abs(a1.y - a2.y) < 0.2 and abs(b1.y - b2.y) < 0.2 and abs(a1.y - b1.y) < 0.5:
                ov = min(max(a1.x, a2.x), max(b1.x, b2.x)) - max(min(a1.x, a2.x), min(b1.x, b2.x))
                if ov > 2.0:
                    seen_pairs.add(pair)
                    rows.append(
                        _issue(
                            "wire_overlap",
                            f"{wa.id} and {wb.id} share a collinear horizontal segment ({ov:.1f} mm)",
                            ref=wa.id,
                            target=wb.id,
                            sheet=sheet.id,
                        )
                    )
            elif abs(a1.x - a2.x) < 0.2 and abs(b1.x - b2.x) < 0.2 and abs(a1.x - b1.x) < 0.5:
                ov = min(max(a1.y, a2.y), max(b1.y, b2.y)) - max(min(a1.y, a2.y), min(b1.y, b2.y))
                if ov > 2.0:
                    seen_pairs.add(pair)
                    rows.append(
                        _issue(
                            "wire_overlap",
                            f"{wa.id} and {wb.id} share a collinear vertical segment ({ov:.1f} mm)",
                            ref=wa.id,
                            target=wb.id,
                            sheet=sheet.id,
                        )
                    )
    return rows

