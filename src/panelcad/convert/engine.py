"""Chunked, resumable IEC <-> NEMA conversion.

Plan writes a workspace JSON. Each chunk converts tags, one sheet's symbols,
or one sheet's ladder/column layout. A stop resumes at the next pending chunk.
The source project file is never written; the default is a save-as copy.

Tag letters come from the counterpart symbol: IEC ``ref_prefix`` on that
device, NEMA prefixes in ``ref_prefixes.yaml``. An unmapped symbol keeps its
library id and is resolved at the target standard. The symbol pairs live in
``standard_map.ENTRIES``. Nothing here invents a library symbol.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from panelcad.convert.flags import sheet_flags, suggest_cleanup
from panelcad.convert.layout_convert import (
    apply_sheet_rails,
    consolidate_control_sheets,
    is_power_sheet,
    needs_column_or_ladder,
    parts_on_sheet,
    rail_kind,
    relayout_sheet,
)
from panelcad.convert.table import (
    convert_contact_number,
    convert_contact_pin_labels,
    convert_contact_terminals,
    convert_terminal_suffix,
    counterpart,
    extra_pins,
    parse_device_tag,
    pin_map,
    role_prefix_for,
    target_standard,
)
from panelcad.core.models import Project
from panelcad.core.refs import resolve_ref_prefix, sheet_drawing_standard

_BASE = re.compile(r"^([A-Za-z]+)(\d+)(.*)$")
_VENDOR_NOTE = (
    "Leave the vendor symbol on the sheet, or pick a counterpart "
    "from the library and place it yourself."
)
_PIN_NOTE = "Leave it as drawn, or pick a counterpart that has the same terminals."
_LAYOUT_KINDS = {"panel_layout", "door_layout"}
_TEXT_KEYS = ("link", "callout", "xref")
_ROLE_RANK = {
    "breaker": 0,
    "disconnect": 0,
    "mpcb": 0,
    "contactor": 1,
    "fwd": 1,
    "rev": 1,
    "main": 1,
    "star": 1,
    "delta": 1,
    "ss": 1,
    "overload": 2,
    "motor": 3,
    "drive": 3,
    "softstarter": 3,
    "transformer": 4,
    "psu": 4,
    "plc": 5,
    "relay": 8,
    "starter_coil": 30,
    "fwd_coil": 30,
    "rev_coil": 30,
    "main_coil": 30,
    "star_coil": 30,
    "delta_coil": 30,
    "ss_coil": 30,
}
_KIND_RANK = {
    "schematic": 0,
    "io": 1,
    "network": 2,
    "terminal": 3,
    "panel_layout": 4,
    "door_layout": 5,
}


def conversion_plan(
    session: Any,
    target: str,
    *,
    sheets: list[str] | None = None,
    out: str | Path | None = None,
    workspace: str | Path | None = None,
    resume: str | Path | None = None,
    keep_numbers: bool = False,
) -> dict[str, Any]:
    """Build the sheet list, tag map, and chunks. Does not change the drawing.

    ``resume`` loads an existing workspace instead of planning again.
    """

    if resume:
        ws = _load(resume)
        session._convert_workspace = Path(ws["path"])
        return _public(ws)
    std = target_standard(target)
    orig_project: Project = session.project
    unconsolidate_on_start = False
    if std == "IEC" and _can_unconsolidate_for_iec(orig_project, sheets):
        unconsolidated = _build_unconsolidated_project(orig_project)
        session.project = unconsolidated
        unconsolidate_on_start = True
    try:
        return _build_conversion_plan(
            session,
            std,
            sheets=sheets,
            out=out,
            workspace=workspace,
            keep_numbers=keep_numbers,
            unconsolidate_on_start=unconsolidate_on_start,
        )
    finally:
        if unconsolidate_on_start:
            session.project = orig_project


def _can_unconsolidate_for_iec(project: Project, sheets: list[str] | None) -> bool:
    block = project.properties.get("convert_standard")
    if not isinstance(block, dict):
        return False
    mp = block.get("multipage")
    if not isinstance(mp, dict):
        return False
    kept = list(mp.get("kept_sheets") or [])
    split_map = mp.get("split_nema_sheets")
    if not kept or not isinstance(split_map, dict) or not split_map:
        return False
    present_ids = {s.id for s in project.sheets}
    if not set(kept).issubset(present_ids):
        return False
    if sheets is not None:
        req = {str(s).strip() for s in sheets if str(s).strip()}
        if not set(kept).issubset(req):
            return False
    return True


def _build_unconsolidated_project(project: Project) -> Project:
    from panelcad.core.models import Sheet

    copy_proj = Project.model_validate(project.model_dump(mode="json"))
    block = copy_proj.properties.get("convert_standard") or {}
    mp = block.get("multipage") or {}
    kept_set = set(mp.get("kept_sheets") or [])
    consolidated_from = list(mp.get("consolidated_from") or [])
    sheet_order = list(mp.get("sheet_order") or [])
    split_map = mp.get("split_nema_sheets") or {}

    by_existing = {s.id: s for s in copy_proj.sheets if s.id not in kept_set}
    restored_sheets = {
        sid: Sheet.model_validate(sdata)
        for sid, sdata in split_map.items()
        if isinstance(sdata, dict)
    }
    new_sheets = []
    seen: set[str] = set()
    for sid in sheet_order:
        if sid in restored_sheets and sid not in seen:
            new_sheets.append(restored_sheets[sid])
            seen.add(sid)
        elif sid in by_existing and sid not in seen:
            new_sheets.append(by_existing[sid])
            seen.add(sid)
    for sid in consolidated_from:
        if sid in restored_sheets and sid not in seen:
            new_sheets.append(restored_sheets[sid])
            seen.add(sid)
    for s in copy_proj.sheets:
        if s.id not in kept_set and s.id not in seen:
            new_sheets.append(s)
            seen.add(s.id)
    if new_sheets:
        copy_proj.sheets = new_sheets
    if isinstance(block, dict):
        block.pop("multipage", None)
    return copy_proj


def _build_conversion_plan(
    session: Any,
    std: str,
    *,
    sheets: list[str] | None = None,
    out: str | Path | None = None,
    workspace: str | Path | None = None,
    keep_numbers: bool = False,
    unconsolidate_on_start: bool = False,
) -> dict[str, Any]:
    project: Project = session.project
    if sheets is None:
        sheet_ids = [sheet.id for sheet in project.sheets]
    else:
        sheet_ids = [str(item) for item in sheets if str(item).strip()]
        if not sheet_ids:
            raise ValueError("no sheets selected")
        for sid in sheet_ids:
            try:
                project.sheet(sid)
            except KeyError as exc:
                raise ValueError(f"no sheet '{sid}'") from exc
    whole = set(sheet_ids) == {sheet.id for sheet in project.sheets}
    source_standards = [sheet_drawing_standard(project, project.sheet(sid)) for sid in sheet_ids]
    if source_standards and all(item == source_standards[0] for item in source_standards):
        source_standard = source_standards[0]
    else:
        source_standard = sheet_drawing_standard(project, None)
    dest = Path(out) if out else _default_out(getattr(session, "path", None), std, project.name)
    _refuse_source(getattr(session, "path", None), dest)
    ws_path = Path(workspace) if workspace else _workspace_for(dest)
    tag_map, bumps = _plan_tags(session, std, sheet_ids, keep_numbers=keep_numbers)
    chunks: list[dict[str, Any]] = [
        {
            "id": "tags",
            "kind": "tags",
            "sheet": "",
            "status": "pending",
            "tag_map": tag_map,
            "tag_bumps": bumps,
            "issues": [],
            "suggestion": "",
        }
    ]
    for sid in sheet_ids:
        sheet = project.sheet(sid)
        members = [comp.id for comp in sheet.components]
        chunks.append(
            {
                "id": f"{sid}:symbols",
                "kind": "symbols",
                "sheet": sid,
                "status": "pending",
                "members": members,
                "issues": [],
                "suggestion": "",
                "pin_rewrites": [],
            }
        )
        parts = parts_on_sheet(session, sheet)
        if _wants_layout(sheet, parts, std):
            chunks.append(
                {
                    "id": f"{sid}:layout",
                    "kind": "layout",
                    "sheet": sid,
                    "status": "pending",
                    "issues": [],
                    "suggestion": "",
                    "rail": {},
                }
            )
    ws = {
        "version": 1,
        "path": str(ws_path),
        "source_path": str(session.path) if getattr(session, "path", None) else "",
        "out_path": str(dest),
        "target": std,
        "source_standard": source_standard,
        "sheets": sheet_ids,
        "whole_project": whole,
        "keep_numbers": bool(keep_numbers),
        "chunks": chunks,
        "source_project": project.model_dump(mode="json"),
        "working_project": None,
        "unconsolidate_on_start": bool(unconsolidate_on_start),
        "unmapped_preview": _preview_unmapped(session, sheet_ids, std),
    }
    _save(ws)
    session._convert_workspace = ws_path
    return _public(ws)


def next_chunk(session: Any, workspace: str | Path | None = None) -> dict[str, Any]:
    """Convert the next pending chunk and save the workspace."""

    ws = _load(_ws_path(session, workspace))
    _resume_working(session, ws)
    chunk = next((item for item in ws["chunks"] if item["status"] == "pending"), None)
    if chunk is None:
        _write_out(session, ws)
        payload = _public(ws)
        payload["done"] = True
        payload["chunk"] = None
        return payload
    _execute(session, ws, chunk)
    chunk["status"] = "done"
    _stamp_working(session, ws)
    _save(ws)
    _write_out(session, ws)
    payload = _public(ws)
    payload["done"] = not any(item["status"] == "pending" for item in ws["chunks"])
    payload["chunk"] = _chunk_public(chunk)
    return payload


def apply_conversion(
    session: Any,
    workspace: str | Path | None = None,
    *,
    out: str | Path | None = None,
) -> dict[str, Any]:
    """Run every pending chunk. Writes the save-as project when finished."""

    ws = _load(_ws_path(session, workspace))
    if out:
        dest = Path(out)
        _refuse_source(ws.get("source_path") or getattr(session, "path", None), dest)
        ws["out_path"] = str(dest)
    _resume_working(session, ws)
    for chunk in ws["chunks"]:
        if chunk["status"] != "pending":
            continue
        _execute(session, ws, chunk)
        chunk["status"] = "done"
        _stamp_working(session, ws)
        _save(ws)
    _write_out(session, ws)
    payload = _public(ws)
    payload["done"] = True
    return payload


def convert_standard(
    session: Any,
    target: str,
    *,
    sheets: list[str] | None = None,
    out: str | Path | None = None,
    workspace: str | Path | None = None,
    keep_numbers: bool = False,
) -> dict[str, Any]:
    """Plan and apply in one call."""

    planned = conversion_plan(
        session,
        target,
        sheets=sheets,
        out=out,
        workspace=workspace,
        keep_numbers=keep_numbers,
    )
    return apply_conversion(session, planned["workspace"], out=planned["out_path"])


def accept_chunk(session: Any, chunk_id: str, workspace: str | Path | None = None) -> dict[str, Any]:
    ws = _load(_ws_path(session, workspace))
    chunk = _chunk(ws, chunk_id)
    if chunk["status"] not in {"done", "accepted"}:
        raise ValueError(f"chunk {chunk_id} is not ready to accept")
    chunk["status"] = "accepted"
    _save(ws)
    return _public(ws)


def revert_chunk(session: Any, chunk_id: str, workspace: str | Path | None = None) -> dict[str, Any]:
    """Drop this chunk and anything that depends on it, then rebuild from the plan."""

    ws = _load(_ws_path(session, workspace))
    chunk = _chunk(ws, chunk_id)
    for dep in _dependents(ws, chunk):
        if dep["status"] in {"done", "accepted"}:
            dep["status"] = "pending"
    before = json.loads(json.dumps(ws))
    chunk["status"] = "reverted"
    _rebuild(session, ws)
    if not any(item.get("status") == "pending" for item in ws["chunks"]):
        # A revert that leaves nothing pending is checked before anything is
        # written or removed. A block refuses this revert only: the workspace
        # stays as it was and the caller's undo step puts the drawing back.
        from panelcad.convert.from_to_check import RevertRefused

        report = connection_report(session, ws)
        if not report["ok"]:
            _save(before)
            raise RevertRefused(report, chunk_id=str(chunk_id))
    _stamp_working(session, ws)
    _save(ws)
    _write_out(session, ws)
    return _public(ws)


def rerun_chunk(session: Any, chunk_id: str, workspace: str | Path | None = None) -> dict[str, Any]:
    """Replay earlier chunks from the saved source, then run this chunk again."""

    ws = _load(_ws_path(session, workspace))
    chunk = _chunk(ws, chunk_id)
    dependents = _dependents(ws, chunk)
    previous = {item["id"]: item["status"] for item in dependents}
    for dep in dependents:
        dep["status"] = "pending"
    chunk["status"] = "pending"
    _rebuild(session, ws)
    _execute(session, ws, chunk)
    chunk["status"] = "done"
    for dep in dependents:
        if previous.get(dep["id"]) in {"done", "accepted"}:
            _execute(session, ws, dep)
            dep["status"] = previous[dep["id"]]
    _stamp_working(session, ws)
    _save(ws)
    _write_out(session, ws)
    return _public(ws)


def review(session: Any, workspace: str | Path | None = None) -> dict[str, Any]:
    ws = _load(_ws_path(session, workspace))
    return _public(ws)


def _wants_layout(sheet: Any, parts: list[Any], target: str) -> bool:
    if is_power_sheet(sheet, parts):
        return False
    if not needs_column_or_ladder(sheet, parts):
        return False
    # PLC I/O keeps a horizontal ladder. Its rails are the control pair
    # (L+/0V or 24V/0V, and X1/X2 for AC in both standards). Style alone
    # cannot tell a finished conversion from one that still needs those rails.
    if rail_kind(sheet, parts) == "io":
        return True
    style = str(getattr(sheet, "drawing_style", "") or "")
    if target_standard(target) == "IEC":
        return style != "iec"
    return style != "ladder"


def _execute(session: Any, ws: dict[str, Any], chunk: dict[str, Any]) -> None:
    kind = chunk["kind"]
    if kind == "tags":
        _apply_tags(session, ws, chunk)
    elif kind == "symbols":
        _apply_symbols(session, ws, chunk)
    elif kind == "layout":
        _apply_layout(session, ws, chunk)
    else:
        raise ValueError(f"unknown chunk kind '{kind}'")
    _refresh_xref(session)


def _apply_tags(session: Any, ws: dict[str, Any], chunk: dict[str, Any]) -> None:
    mapping = {str(old): str(new) for old, new in (chunk.get("tag_map") or {}).items() if old != new}
    _rename_all(session.project, mapping)
    prev = session.project.properties.get("convert_standard")
    prev_rails = dict(prev.get("rails") or {}) if isinstance(prev, dict) else {}
    prev_positions: dict[str, Any] = {}
    if isinstance(prev, dict) and isinstance(prev.get("positions"), dict):
        for sh_id, sh_pos in prev["positions"].items():
            if not isinstance(sh_pos, dict):
                continue
            comp_pos = dict(sh_pos.get("components") or {})
            remapped_comp_pos = dict(comp_pos)
            for old_ref, new_ref in mapping.items():
                if old_ref in comp_pos:
                    remapped_comp_pos[new_ref] = comp_pos[old_ref]
            prev_positions[str(sh_id)] = {
                "components": remapped_comp_pos,
                "wires": dict(sh_pos.get("wires") or {}),
            }
    session.project.properties["convert_standard"] = {
        "source": ws["source_standard"],
        "target": ws["target"],
        "tag_map": dict(chunk.get("tag_map") or {}),
        "rails": prev_rails,
        "positions": prev_positions,
    }
    if ws.get("whole_project"):
        session.project.title_block.standard = ws["target"]
    issues = [
        {
            "flag": "tag_collision",
            "detail": (
                f"{row['from']} could not take the next number, so it is now {row['to']}. "
                "Another device on the drawing already uses that number."
            ),
            "ref": row["from"],
        }
        for row in (chunk.get("tag_bumps") or [])
    ]
    chunk["issues"] = issues
    chunk["suggestion"] = suggest_cleanup(issues)


def _apply_symbols(session: Any, ws: dict[str, Any], chunk: dict[str, Any]) -> None:
    sheet = session.sheet(chunk["sheet"])
    sheet.standard = ws["target"]
    tag_map = _active_tag_map(ws)
    wanted = {tag_map.get(ref, ref) for ref in (chunk.get("members") or [])}
    issues: list[dict[str, Any]] = []
    rewrites: list[dict[str, str]] = []
    for page in session.project.sheets:
        if page.id != sheet.id and page.kind not in _LAYOUT_KINDS:
            continue
        for comp in list(page.components):
            if comp.id not in wanted:
                continue
            _swap_one(session, comp, page, ws["target"], issues, rewrites)
    _assign_iec_family_contact_slots(session.project, ws["target"], wanted)
    _apply_ladder_only_rails(session, ws, chunk, sheet)
    _rebuild_power_routes(session, sheet)
    has_layout = any(
        c.get("kind") == "layout" and c.get("sheet") == sheet.id
        for c in (ws.get("chunks") or [])
    )
    if not has_layout and str(getattr(sheet, "kind", "") or "") not in _LAYOUT_KINDS:
        issues.extend(sheet_flags(session, sheet.id))
    chunk["issues"] = issues
    chunk["pin_rewrites"] = rewrites
    chunk["suggestion"] = suggest_cleanup(issues)




def _apply_ladder_only_rails(session: Any, ws: dict[str, Any], chunk: dict[str, Any], sheet: Any) -> None:
    """Convert control rails on a ladder sheet that has no layout chunk (e.g. L+/0V <-> 24V/0V)."""

    has_layout = any(
        c.get("kind") == "layout" and c.get("sheet") == sheet.id
        for c in (ws.get("chunks") or [])
    )
    if has_layout:
        return
    if str(getattr(sheet, "kind", "") or "") in _LAYOUT_KINDS:
        return
    if is_power_sheet(sheet):
        _tidy_power_sheet(session, sheet)
        return
    std = target_standard(ws["target"])
    tb = getattr(session.project, "title_block", None)
    tb_hot = str(getattr(tb, "rail_hot", "") or "") if tb is not None else ""
    tb_ret = str(getattr(tb, "rail_return", "") or "") if tb is not None else ""
    s_hot = str(sheet.rail_hot or tb_hot)
    s_ret = str(sheet.rail_return or tb_ret)
    block = session.project.properties.get("convert_standard")
    stored = ((block.get("rails") or {}).get(sheet.id) or {}) if isinstance(block, dict) else {}
    wire_ends = {str(w.frm or "") for w in sheet.wires} | {str(w.to or "") for w in sheet.wires}
    if not isinstance(block, dict):
        block = {
            "source": ws["source_standard"],
            "target": ws["target"],
            "tag_map": _active_tag_map(ws),
            "rails": {},
        }
        session.project.properties["convert_standard"] = block
    is_ladder = str(getattr(sheet, "drawing_style", "") or "") == "ladder"
    needs_rail = is_ladder and (
        (std == "NEMA/ANSI" and (s_hot == "L+" or s_ret == "M" or "L+" in wire_ends or "M" in wire_ends))
        or (std == "IEC" and bool(stored))
    )
    if needs_rail:
        parts = parts_on_sheet(session, sheet)
        record = apply_sheet_rails(session, sheet.id, std, parts, columns=False)
        rails = dict(block.get("rails") or {})
        rails[sheet.id] = record
        block["rails"] = rails
        chunk["rail"] = record
    _tidy_non_layout_ladder_sheet(session, sheet, std, block)


def _tidy_power_sheet(session: Any, sheet: Any) -> None:
    """Ensure a 3-phase power sheet has no control ladder rails and de-crowd overlapping vertical feeders."""

    from panelcad.render.sheet import pin_world
    from panelcad.render.titleblock import title_block_rect, working_rect
    from panelcad.render.wire_labels import boxes_overlap
    from panelcad.render.wiring import clear_route_cache, component_obstacle

    wire_ends = {str(w.frm or "") for w in sheet.wires} | {str(w.to or "") for w in sheet.wires}
    hot = str(getattr(sheet, "rail_hot", "") or "")
    ret = str(getattr(sheet, "rail_return", "") or "")
    if str(getattr(sheet, "drawing_style", "") or "") in {"ladder", "iec"}:
        if hot not in wire_ends and ret not in wire_ends:
            sheet.drawing_style = "freeform"  # type: ignore[assignment]
            sheet.rail_hot = ""
            sheet.rail_return = ""

    power_comps = [
        c
        for c in sheet.components
        if str(c.properties.get("role") or "") not in {"to", "from"}
        and "offpage" not in str(c.library_id or "").lower()
    ]
    if len(power_comps) < 2:
        return
    boxes: list[tuple[Any, tuple[float, float, float, float]]] = []
    for c in power_comps:
        try:
            boxes.append((c, component_obstacle(sheet, session.catalog, c, pad=0.0)))
        except Exception:  # noqa: BLE001
            return
    has_overlap = any(
        boxes_overlap(ba, bb, pad=-0.5)
        for i, (_ca, ba) in enumerate(boxes)
        for _cb, bb in boxes[i + 1 :]
    )
    if not has_overlap:
        return

    xs = [float(c.x) for c in power_comps]
    if max(xs) - min(xs) > 30.0:
        return

    ordered = sorted(boxes, key=lambda item: (float(item[0].y), float(item[0].x), item[0].id))
    anchor_comp = ordered[0][0]
    try:
        anchor_dev = session.catalog.get(anchor_comp.library_id)
        anchor_pxs = [
            pin_world(anchor_comp, p)[0]
            for p in anchor_dev.pins
            if p.side in {"top", "bottom"}
        ]
        anchor_mid_x = (min(anchor_pxs) + max(anchor_pxs)) * 0.5 if anchor_pxs else float(anchor_comp.x) + 20.0
    except Exception:  # noqa: BLE001
        anchor_mid_x = float(anchor_comp.x) + 20.0

    _wx, wy, _ww, _wh = working_rect(sheet)
    _tx, ty, _tw, _th = title_block_rect(sheet)
    heights = [float(b[3] - b[1]) for _c, b in ordered]
    total_h = sum(heights)
    avail_h = (ty - 1.5) - (wy + 5.0)
    gap = 3.0
    if total_h + gap * (len(ordered) - 1) > avail_h and len(ordered) > 1:
        gap = max(1.0, (avail_h - total_h) / (len(ordered) - 1))
    stack_h = total_h + gap * (len(ordered) - 1)
    cur_y = max(wy + 5.0, min(float(ordered[0][0].y), (ty - 1.5) - stack_h))

    for comp, box in ordered:
        h = float(box[3] - box[1])
        comp.y = round(cur_y, 1)
        try:
            dev = session.catalog.get(comp.library_id)
            local_xs = [float(p.x) for p in dev.pins if p.side in {"top", "bottom"}]
            if local_xs:
                local_mid = (min(local_xs) + max(local_xs)) * 0.5
                comp.x = round(anchor_mid_x - local_mid, 1)
        except Exception:  # noqa: BLE001
            pass
        cur_y += h + gap

    for wire in sheet.wires:
        wire.points = []
    clear_route_cache()


def _tidy_non_layout_ladder_sheet(session: Any, sheet: Any, std: str, block: dict[str, Any]) -> None:
    """De-crowd same-row symbols and clear backtracking waypoints on a non-layout ladder sheet."""

    from panelcad.core.geometry import Point
    from panelcad.core.ink import schematic_ink_rect
    from panelcad.render.sheet import pin_side_world, pin_world
    from panelcad.render.titleblock import title_block_rect, working_rect
    from panelcad.render.wiring import clear_route_cache

    pos_map = block.setdefault("positions", {})
    if std == "IEC":
        saved = pos_map.get(sheet.id)
        if isinstance(saved, dict):
            comp_saved = saved.get("components") or {}
            for comp in sheet.components:
                row = comp_saved.get(comp.id)
                if isinstance(row, dict):
                    comp.x = float(row.get("x", comp.x))
                    comp.y = float(row.get("y", comp.y))
                    comp.rotation = int(row.get("rotation", 0))  # type: ignore[assignment]
                    comp.scale = float(row.get("scale", 1.0))
            wire_saved = saved.get("wires") or {}
            for wire in sheet.wires:
                pts = wire_saved.get(wire.id)
                if isinstance(pts, list):
                    wire.points = [Point(x=float(p["x"]), y=float(p["y"])) for p in pts if isinstance(p, dict)]
            clear_route_cache()
        return

    if sheet.id not in pos_map:
        pos_map[sheet.id] = {
            "components": {
                comp.id: {
                    "x": float(comp.x),
                    "y": float(comp.y),
                    "rotation": int(comp.rotation or 0),
                    "scale": float(comp.scale or 1.0),
                }
                for comp in sheet.components
            },
            "wires": {
                wire.id: [{"x": float(pt.x), "y": float(pt.y)} for pt in (wire.points or [])]
                for wire in sheet.wires
            },
        }

    wx, wy, ww, _wh = working_rect(sheet)
    _tx, ty, _tw, _th = title_block_rect(sheet)
    min_y = None
    max_y = None
    for comp in sheet.components:
        try:
            dev = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        ink = schematic_ink_rect(dev, comp.x, comp.y, comp.id, component=comp)
        min_y = ink[1] if min_y is None else min(min_y, ink[1])
        max_y = ink[3] if max_y is None else max(max_y, ink[3])
    if min_y is not None and max_y is not None:
        dy = 0.0
        if min_y < wy + 0.5:
            cand = float(int((wy + 1.0 - min_y) + 0.999))
            if max_y + cand <= ty - 2.0:
                dy = cand
        elif max_y > ty - 2.0:
            cand = -float(int((max_y - (ty - 2.0)) + 0.999))
            if min_y + cand >= wy + 0.5:
                dy = cand
        if dy != 0.0:
            for comp in sheet.components:
                comp.y = round(float(comp.y) + dy, 1)
            for wire in sheet.wires:
                if wire.points:
                    wire.points = [Point(x=float(pt.x), y=round(float(pt.y) + dy, 1)) for pt in wire.points]

    max_x = wx + ww - 20.0
    comps = sorted(sheet.components, key=lambda c: (float(c.x), float(c.y), c.id))
    for i, a in enumerate(comps):
        try:
            a_dev = session.catalog.get(a.library_id)
            a_w = float(getattr(a_dev, "schematic_width", 0.0) or 24.0)
        except KeyError:
            a_w = 24.0
        for b in comps[i + 1 :]:
            if abs(float(a.y) - float(b.y)) > 15.0:
                continue
            if float(b.x) < float(a.x) + a_w + 10.0:
                need_x = float(a.x) + a_w + 18.0
                if need_x <= max_x:
                    b.x = need_x

    for wire in sheet.wires:
        if not wire.points:
            continue
        frm = str(wire.frm or "")
        if "." not in frm:
            continue
        ref, pid = frm.split(".", 1)
        try:
            comp = sheet.component(ref)
            pin = session.catalog.get(comp.library_id).pin(pid)
        except (KeyError, ValueError):
            continue
        ax, ay = pin_world(comp, pin)
        side = pin_side_world(comp, pin)
        pt0 = wire.points[0]
        if (
            (side == "right" and float(pt0.x) < ax - 0.5)
            or (side == "left" and float(pt0.x) > ax + 0.5)
            or (side == "bottom" and float(pt0.y) < ay - 0.5)
            or (side == "top" and float(pt0.y) > ay + 0.5)
        ):
            wire.points = []
    clear_route_cache()


def _parent_is_timer(project: Project, comp_id: str) -> bool:
    parsed = parse_device_tag(comp_id)
    if parsed is None:
        return False
    base = f"{parsed[0]}{parsed[1]}"
    if parsed[0].upper() in {"KT", "TR", "T", "TD", "TDR"}:
        return True
    for sheet in project.sheets:
        for other in sheet.components:
            if other.id == base and "timer" in str(other.library_id or "").lower():
                return True
    return False


def _remap_contact_properties(project: Project, comp: Any, target: str) -> None:
    """Normalize contact terminals, pin_labels, and contact_no for the target standard."""

    lid = str(comp.library_id or "")
    is_contact = (
        lid.endswith((".aux_no", ".aux_nc", ".ol_contact"))
        or ".operators." in lid
    )
    if not is_contact:
        return
    std = target_standard(target)
    role = str(comp.properties.get("role") or "")
    delayed = str(comp.properties.get("delayed") or "").upper()
    is_nc = (
        lid.endswith((".aux_nc", ".pb_nc", ".estop", ".selector_nc"))
        or role == "tr_nc"
        or delayed == "NC"
    )
    state = "NC" if is_nc else "NO"
    if std == "NEMA/ANSI":
        if "terminals" in comp.properties:
            comp.properties["terminals"] = convert_contact_terminals(
                str(comp.properties["terminals"]),
                "NEMA/ANSI",
                state=state,
                delayed=delayed,
            )
        labels = comp.properties.get("pin_labels")
        if isinstance(labels, dict):
            comp.properties["pin_labels"] = convert_contact_pin_labels(
                labels,
                {},
                "NEMA/ANSI",
                state=state,
                delayed=delayed,
            )
        for key in ("contact_no", "contact_number"):
            if key in comp.properties:
                comp.properties[key] = convert_contact_number(
                    str(comp.properties[key]),
                    "NEMA/ANSI",
                    state=state,
                    delayed=delayed,
                )
        return

    # std == "IEC"
    if lid.endswith((".aux_no", ".aux_nc")) and (
        role in {"tr_nc", "tr_no"}
        or delayed in {"NC", "NO"}
        or comp.id.endswith(("-16", "-18"))
        or _parent_is_timer(project, comp.id)
    ):
        if is_nc:
            comp.properties["terminals"] = "15-16"
            comp.properties["pin_labels"] = {"21": "15", "22": "16"}
            comp.properties["delayed"] = "NC"
        else:
            comp.properties["terminals"] = "15-18"
            comp.properties["pin_labels"] = {"13": "15", "14": "18"}
            comp.properties["delayed"] = "NO"
        return

    if lid.endswith((".aux_no", ".aux_nc")):
        slot = 1
        if comp.id.endswith(("-43", "-44", "-41", "-42")):
            slot = 4
        elif comp.id.endswith(("-33", "-34", "-31", "-32")):
            slot = 3
        elif comp.id.endswith(("-23", "-24")) or role in {
            "main_aux_tr",
            "fwd_run_aux",
            "rev_run_aux",
            "ss_run_aux",
        }:
            slot = 2
        if slot >= 2:
            if is_nc:
                comp.properties["terminals"] = f"{slot}1-{slot}2"
                comp.properties["pin_labels"] = {"21": f"{slot}1", "22": f"{slot}2"}
            else:
                comp.properties["terminals"] = f"{slot}3-{slot}4"
                comp.properties["pin_labels"] = {"13": f"{slot}3", "14": f"{slot}4"}
            return

    if "terminals" in comp.properties:
        comp.properties["terminals"] = convert_contact_terminals(
            str(comp.properties["terminals"]),
            "IEC",
            state=state,
            delayed=delayed,
            slot=1,
        )
    labels = comp.properties.get("pin_labels")
    if isinstance(labels, dict):
        comp.properties["pin_labels"] = convert_contact_pin_labels(
            labels,
            {},
            "IEC",
            state=state,
            delayed=delayed,
            slot=1,
        )
    for key in ("contact_no", "contact_number"):
        if key in comp.properties:
            comp.properties[key] = convert_contact_number(
                str(comp.properties[key]),
                "IEC",
                state=state,
                delayed=delayed,
                slot=1,
            )


def _assign_iec_family_contact_slots(project: Project, target: str, wanted: set[str]) -> None:
    """Ensure multiple aux contacts on the same non-timer parent get sequential IEC terminal pairs."""

    if target_standard(target) != "IEC":
        return
    by_parent: dict[tuple[str, str], list[Any]] = {}
    for sheet in project.sheets:
        if sheet.kind in _LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            if comp.id not in wanted:
                continue
            lid = str(comp.library_id or "")
            if not lid.endswith((".aux_no", ".aux_nc")):
                continue
            if _parent_is_timer(project, comp.id):
                continue
            parsed = parse_device_tag(comp.id)
            if parsed is None or not parsed[2]:
                continue
            base = f"{parsed[0]}{parsed[1]}"
            state = "NC" if lid.endswith(".aux_nc") else "NO"
            by_parent.setdefault((base, state), []).append(comp)

    for (_base, state), members in by_parent.items():
        if len(members) <= 1:
            continue
        used_slots: set[int] = set()
        for comp in members:
            terms = str(comp.properties.get("terminals") or "")
            if terms in {"23-24", "21-22"} and comp.properties.get("pin_labels"):
                used_slots.add(2 if terms == "23-24" else 1)
            elif terms in {"33-34", "31-32"}:
                used_slots.add(3)
            elif terms in {"43-44", "41-42"}:
                used_slots.add(4)
        next_slot = 1
        for comp in members:
            terms = str(comp.properties.get("terminals") or "")
            if terms in {"23-24", "33-34", "43-44", "31-32", "41-42"}:
                continue
            while next_slot in used_slots:
                next_slot += 1
            used_slots.add(next_slot)
            if next_slot >= 2:
                if state == "NC":
                    comp.properties["terminals"] = f"{next_slot}1-{next_slot}2"
                    comp.properties["pin_labels"] = {"21": f"{next_slot}1", "22": f"{next_slot}2"}
                else:
                    comp.properties["terminals"] = f"{next_slot}3-{next_slot}4"
                    comp.properties["pin_labels"] = {"13": f"{next_slot}3", "14": f"{next_slot}4"}
            next_slot += 1


def _power_route_kind(parts: list[Any]) -> str | None:
    """Circuit Builder kind whose power shelves ``finish_power_routes`` stores."""

    roles = {str(getattr(part, "role", "") or "") for part in parts}
    if "motor" not in roles:
        return None
    if "softstarter" in roles:
        return "soft_starter"
    if "star" in roles:
        return "star_delta"
    if "rev" in roles:
        return "reversing"
    if "drive" in roles:
        return "vfd"
    return None


def _rebuild_power_routes(session: Any, sheet: Any) -> None:
    """Store the shelves a fresh insert of the target standard would store.

    ``_wants_layout`` is false on a power sheet, so the symbol swap never
    runs the layout chunk. The bypass shelf from the source standard is then
    left in place, and a lead with no waypoints autoroutes across another phase.
    """

    if str(getattr(sheet, "kind", "") or "") in _LAYOUT_KINDS:
        return
    parts = parts_on_sheet(session, sheet)
    kind = _power_route_kind(parts)
    if kind is None:
        return
    from panelcad.core.circuit_extra import finish_power_routes

    finish_power_routes(session, sheet.id, parts, kind)


def _consolidatable_control_sheets(session: Any, ws: dict[str, Any]) -> list[str]:
    if target_standard(ws.get("target") or "") != "NEMA/ANSI":
        return []
    src_proj_data = ws.get("source_project")
    if not isinstance(src_proj_data, dict):
        return []
    src_proj = Project.model_validate(src_proj_data)
    selected = set(ws.get("sheets") or [])
    layout_sheets = {
        str(c.get("sheet") or "")
        for c in (ws.get("chunks") or [])
        if c.get("kind") == "layout"
    }
    _power_tokens = (
        "motors.",
        "loads.motor",
        "breakers.mcb_3p",
        "breakers.mccb",
        "contactors.ac3",
        "contactors.three_pole",
        "drives.",
        "starters.",
    )
    ctrl_sids: list[str] = []
    for sh in src_proj.sheets:
        if sh.id not in selected or sh.id not in layout_sheets:
            continue
        if str(getattr(sh, "kind", "") or "") in _LAYOUT_KINDS:
            continue
        if any(
            any(tok in str(getattr(c, "library_id", "") or "").lower() for tok in _power_tokens)
            for c in sh.components
        ):
            continue
        if any(
            str(c.properties.get("role") or "") == "plc"
            or ".plc" in str(getattr(c, "library_id", "") or "").lower()
            for c in sh.components
        ):
            continue
        parts = parts_on_sheet(session, sh)
        rk = rail_kind(sh, parts)
        if rk in {"power", "io"}:
            continue
        ctrl_sids.append(sh.id)
    return ctrl_sids if len(ctrl_sids) >= 2 else []


def _apply_layout(session: Any, ws: dict[str, Any], chunk: dict[str, Any]) -> None:
    sheet_id = str(chunk["sheet"])
    record = relayout_sheet(session, sheet_id, ws["target"])
    block = session.project.properties.get("convert_standard")
    if not isinstance(block, dict):
        block = {
            "source": ws["source_standard"],
            "target": ws["target"],
            "tag_map": _active_tag_map(ws),
            "rails": {},
        }
        session.project.properties["convert_standard"] = block
    rails = dict(block.get("rails") or {})
    rails[sheet_id] = record
    block["rails"] = rails
    chunk["rail"] = record

    ctrl_sids = _consolidatable_control_sheets(session, ws)
    if sheet_id in ctrl_sids:
        pre_map = ws.setdefault("pre_consolidate_sheets", {})
        pre_map[sheet_id] = session.sheet(sheet_id).model_dump(mode="json")
        done_layouts = {
            str(c.get("sheet") or "")
            for c in (ws.get("chunks") or [])
            if c.get("kind") == "layout" and (c.get("status") in {"done", "accepted"} or c is chunk)
        }
        if set(ctrl_sids).issubset(done_layouts):
            sheet_order = [s.id for s in session.project.sheets]
            c_info = consolidate_control_sheets(session, ctrl_sids)
            block["multipage"] = {
                "consolidated_from": list(ctrl_sids),
                "kept_sheets": list(c_info["kept_sheets"]),
                "removed_sheets": list(c_info["removed_sheets"]),
                "target_for_sheet": dict(c_info.get("target_for_sheet") or {}),
                "sheet_order": sheet_order,
                "split_nema_sheets": dict(pre_map),
            }
            kept_set = set(c_info["kept_sheets"])
            for c_item in ws.get("chunks") or []:
                if c_item.get("kind") != "layout":
                    continue
                c_sid = str(c_item.get("sheet") or "")
                if c_sid not in ctrl_sids:
                    continue
                if c_sid in kept_set:
                    c_issues = sheet_flags(session, c_sid)
                    c_item["issues"] = c_issues
                    c_item["suggestion"] = suggest_cleanup(c_issues)
                else:
                    c_item["issues"] = []
                    c_item["suggestion"] = suggest_cleanup([])
            return

    issues = sheet_flags(session, sheet_id)
    for ref in record.get("unplaced") or []:
        issues.append(
            {
                "flag": "unplaced",
                "detail": f"{ref} is wired into a rung but was not placed on the ladder. Check it before accepting.",
                "ref": ref,
            }
        )
    chunk["issues"] = issues
    chunk["suggestion"] = suggest_cleanup(issues)


def _swap_one(
    session: Any,
    comp: Any,
    sheet: Any,
    target: str,
    issues: list[dict[str, Any]],
    rewrites: list[dict[str, str]],
) -> None:
    low_lib = str(comp.library_id or "").lower()
    if "offpage" in low_lib or "junction" in low_lib or "connection.node" in low_lib:
        return
    new_lib = counterpart(comp.library_id, target)
    if new_lib is None:
        issues.append(_unmapped_issue(session, comp, sheet.id))
        return
    if new_lib == comp.library_id:
        return
    try:
        device = session.catalog.get(new_lib)
    except KeyError:
        issues.append(_unmapped_issue(session, comp, sheet.id, missing=new_lib))
        return
    mapping = pin_map(comp.library_id, target)
    target_pins = {pin.id for pin in device.pins}
    wired = _wired_pins(session.project, comp.id, sheet.id)
    blocked = []
    for pin in wired:
        mapped = mapping.get(pin, pin)
        if mapped not in target_pins:
            blocked.append(pin)
    extras = extra_pins(comp.library_id, target)
    blocked.extend(pin for pin in wired if pin in extras and pin not in blocked)
    if blocked:
        name = _device_label(session, comp.library_id)
        joined = ", ".join(blocked)
        issues.append(
            {
                "flag": "unmapped_pin",
                "detail": (
                    f"{comp.id} {name} stays as drawn. The wired terminal {joined} "
                    f"is not on the other symbol. {_PIN_NOTE}"
                ),
                "ref": comp.id,
                "sheet": sheet.id,
                "library_id": comp.library_id,
            }
        )
        return
    source_lib = comp.library_id
    comp.library_id = new_lib
    if mapping:
        _remap_pins(session.project, comp, mapping, target_pins, rewrites)
    else:
        _drop_unknown_hidden(comp, target_pins)
    _remap_contact_properties(session.project, comp, target)
    _keep_supply_pin_labels(comp, device)
    _lift_off_title(comp, device, sheet)
    _flag_unwired_overload_aux(comp, source_lib, new_lib, extras, wired, sheet.id, issues)


_SUPPLY_PIN_IDS = {"L", "N", "PE"}


def _lift_off_title(comp: Any, device: Any, sheet: Any) -> None:
    """Slide one symbol up when its ink crosses into the title block.

    A NEMA motor body is the same height as the IEC one, and a stack that was
    already sitting on the title line overlaps once the tag and pin dots are
    counted. Only this symbol moves.
    """

    from panelcad.core.ink import schematic_ink_rect
    from panelcad.render.titleblock import title_block_rect

    _tx, ty, _tw, _th = title_block_rect(sheet)
    ink = schematic_ink_rect(device, comp.x, comp.y, comp.id, component=comp)
    limit = ty - 0.2
    if ink[3] > limit:
        comp.y -= ink[3] - limit


def _keep_supply_pin_labels(comp: Any, device: Any) -> None:
    """L, N, and PE are drawn on an IEC sheet because the standard shows pin ids.

    A NEMA ladder hides a caption that is only the pin id. Keep those three
    as explicit labels so the supply still shows them after the swap.
    """

    present = {pin.id for pin in device.pins}
    if not _SUPPLY_PIN_IDS.intersection(present):
        return
    labels = dict(comp.properties.get("pin_labels") or {})
    for pin_id in _SUPPLY_PIN_IDS:
        if pin_id in present and not str(labels.get(pin_id) or "").strip():
            labels[pin_id] = pin_id
    comp.properties["pin_labels"] = labels


def _flag_unwired_overload_aux(
    comp: Any,
    source_lib: str,
    new_lib: str,
    extras: set[str],
    wired: set[str],
    sheet_id: str,
    issues: list[dict[str, Any]],
) -> None:
    """The NEMA overload draws one NC aux. Say so when the IEC 97-98 pair is left behind.

    A wired 97/98 never reaches here: the swap is blocked and the contact stays.
    """

    if "overload" not in source_lib and "overload" not in new_lib:
        return
    missing = [pin for pin in sorted(extras) if pin not in wired]
    if not missing:
        return
    joined = ", ".join(missing)
    issues.append(
        {
            "flag": "unmapped_pin",
            "detail": (
                f"{comp.id} auxiliary {joined} is not on the other overload symbol. "
                "It is not wired, so the symbol was still swapped. "
                "Pick a counterpart that has the same terminals if that contact is needed."
            ),
            "ref": comp.id,
            "sheet": sheet_id,
            "library_id": comp.library_id,
        }
    )


def _device_label(session: Any, library_id: str) -> str:
    try:
        device = session.catalog.get(library_id)
    except KeyError:
        return str(library_id)
    name = str(getattr(device, "name", "") or "").strip()
    return name or str(library_id)


def _unmapped_issue(session: Any, comp: Any, sheet_id: str, *, missing: str = "") -> dict[str, str]:
    """Shop note: keep the vendor symbol, or choose a counterpart yourself."""

    name = _device_label(session, comp.library_id)
    if missing:
        detail = (
            f"{comp.id} {name} ({comp.library_id}) stays as drawn. "
            f"The other standard's symbol is not in this library. {_VENDOR_NOTE}"
        )
    else:
        detail = (
            f"{comp.id} {name} ({comp.library_id}) has no matching symbol "
            f"in the other standard. {_VENDOR_NOTE}"
        )
    return {
        "flag": "unmapped_symbol",
        "detail": detail,
        "ref": comp.id,
        "sheet": sheet_id,
        "library_id": comp.library_id,
    }


def _remap_pins(
    project: Project,
    comp: Any,
    mapping: dict[str, str],
    target_pins: set[str],
    rewrites: list[dict[str, str]],
) -> None:
    prefix = comp.id + "."
    for sheet in project.sheets:
        for wire in sheet.wires:
            wire.frm = _remap_end(wire.frm, prefix, mapping)
            wire.to = _remap_end(wire.to, prefix, mapping)
        for other in sheet.components:
            if str(other.properties.get("dest_ref") or "") != comp.id:
                continue
            dest_pin = str(other.properties.get("dest_pin") or "")
            if dest_pin in mapping:
                other.properties["dest_pin"] = mapping[dest_pin]
            link = other.properties.get("link")
            if isinstance(link, str) and link:
                other.properties["link"] = _remap_link(link, comp.id, mapping)
    link = comp.properties.get("link")
    if isinstance(link, str) and link:
        comp.properties["link"] = _remap_link(link, comp.id, mapping)
    labels = comp.properties.get("pin_labels")
    if isinstance(labels, dict):
        remapped: dict[str, Any] = {}
        for key, value in labels.items():
            new_key = mapping.get(str(key), str(key))
            if new_key in target_pins:
                remapped[new_key] = value
        comp.properties["pin_labels"] = remapped
    hidden = comp.properties.get("hidden_pins")
    if isinstance(hidden, list):
        kept = []
        for pin in hidden:
            new_pin = mapping.get(str(pin), str(pin))
            if new_pin in target_pins and new_pin not in kept:
                kept.append(new_pin)
        if kept:
            comp.properties["hidden_pins"] = kept
        else:
            comp.properties.pop("hidden_pins", None)
    overrides = dict(comp.pin_overrides or {})
    if overrides:
        moved = {}
        for key, value in overrides.items():
            new_key = mapping.get(str(key), str(key))
            if new_key in target_pins:
                moved[new_key] = value
        comp.pin_overrides = moved
    for old, new in mapping.items():
        if old != new:
            rewrites.append({"ref": comp.id, "pin": new, "was": old})


def _drop_unknown_hidden(comp: Any, target_pins: set[str]) -> None:
    hidden = comp.properties.get("hidden_pins")
    if not isinstance(hidden, list):
        return
    kept = [str(pin) for pin in hidden if str(pin) in target_pins]
    if kept:
        comp.properties["hidden_pins"] = kept
    else:
        comp.properties.pop("hidden_pins", None)


def _remap_end(end: str, prefix: str, mapping: dict[str, str]) -> str:
    text = str(end or "")
    if not text.startswith(prefix):
        return text
    pin = text[len(prefix) :]
    if pin in mapping:
        return prefix + mapping[pin]
    return text


def _remap_link(link: str, ref: str, mapping: dict[str, str]) -> str:
    parts = []
    for piece in str(link).split("|"):
        if ":" in piece:
            sheet_id, end = piece.split(":", 1)
            parts.append(f"{sheet_id}:{_remap_end(end, ref + '.', mapping)}")
        else:
            parts.append(_remap_end(piece, ref + ".", mapping))
    return "|".join(parts)


def _wired_pins(project: Project, ref: str, sheet_id: str | None = None) -> set[str]:
    """Pins this instance actually lands on.

    A coil and its contactor share one tag. Power wires on the other sheet
    belong to the contactor, not to the coil, so they must not block the swap.
    Off-page ``dest_pin`` references still count from every sheet.
    """

    pins: set[str] = set()
    prefix = ref + "."
    for sheet in project.sheets:
        if sheet_id is None or sheet.id == sheet_id:
            for wire in sheet.wires:
                for end in (wire.frm, wire.to):
                    text = str(end or "")
                    if text.startswith(prefix):
                        pins.add(text.split(".", 1)[1])
        for comp in sheet.components:
            if str(comp.properties.get("dest_ref") or "") == ref:
                dest_pin = str(comp.properties.get("dest_pin") or "")
                if dest_pin:
                    pins.add(dest_pin.split(".", 1)[0])
    return pins


def _plan_tags(
    session: Any,
    target: str,
    sheet_ids: list[str],
    *,
    keep_numbers: bool = False,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    project: Project = session.project
    selected = set(sheet_ids)
    sheet_pos = {sheet.id: index for index, sheet in enumerate(project.sheets)}
    families: dict[str, dict[str, Any]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            parsed = parse_device_tag(comp.id)
            if parsed is None:
                continue
            letters, number, suffix = parsed
            base = f"{letters}{number}"
            fam = families.setdefault(base, {"base": base, "number": number, "members": {}})
            place = (_place_rank(sheet.kind), sheet_pos[sheet.id], float(comp.x), float(comp.y))
            row = {
                "id": comp.id,
                "suffix": suffix,
                "role": str(comp.properties.get("role") or ""),
                "kind": sheet.kind,
                "library_id": comp.library_id,
                "sheet": sheet.id,
                "sheets": {sheet.id},
                "places": [place],
            }
            current = fam["members"].get(comp.id)
            if current is None:
                fam["members"][comp.id] = row
            elif _rank(row) < _rank(current):
                row["sheets"] = set(current.get("sheets") or {current["sheet"]})
                row["sheets"].add(sheet.id)
                row["places"] = list(current.get("places") or []) + [place]
                fam["members"][comp.id] = row
            else:
                current.setdefault("sheets", {current["sheet"]}).add(sheet.id)
                current.setdefault("places", []).append(place)
    inverse = _inverse_map(project, target)
    frozen = {comp.id for sheet in project.sheets for comp in sheet.components}
    in_scope = []
    for fam in families.values():
        sheets_hit = set()
        for member in fam["members"].values():
            sheets_hit.add(member["sheet"])
            sheets_hit.update(member.get("sheets") or ())
        if sheets_hit & selected:
            in_scope.append(fam)
            frozen.difference_update(fam["members"].keys())
    in_scope.sort(key=lambda fam: (_rank(min(fam["members"].values(), key=_rank)), fam["base"]))
    taken = set(frozen)
    mapping: dict[str, str] = {}
    bumps: list[dict[str, str]] = []
    pending: list[dict[str, Any]] = []
    for fam in in_scope:
        members = list(fam["members"].values())
        if (fam["base"] in inverse or all(m["id"] in inverse for m in members)) and _inverse_fits(fam, inverse, taken):
            for member in members:
                new_id = inverse[member["id"]] if member["id"] in inverse else inverse[fam["base"]] + member["suffix"]
                mapping[member["id"]] = new_id
                taken.add(new_id)
            continue
        prefix = _target_prefix(session, project, fam, target)
        fam["prefix"] = prefix
        if _is_terminal_family(fam, prefix):
            for member in members:
                member["suffix"] = convert_terminal_suffix(member["suffix"], target)
        if keep_numbers:
            _assign_kept_number(fam, prefix, taken, mapping, bumps)
        else:
            pending.append(fam)
    if pending:
        _assign_sequential(pending, taken, mapping, bumps)
    changed = {old: new for old, new in mapping.items() if old != new}
    return changed, bumps


def _place_rank(kind: str) -> int:
    if kind in {"schematic", "io"}:
        return 0
    return 1


def _family_place(fam: dict[str, Any]) -> tuple:
    places = [place for member in fam["members"].values() for place in (member.get("places") or [])]
    if not places:
        return (9, 999, 0.0, 0.0, fam["base"])
    return min(places)


def _is_terminal_family(fam: dict[str, Any], prefix: str) -> bool:
    if prefix.upper() in {"TB", "X", "XT"}:
        return True
    for member in fam["members"].values():
        lib_id = str(member.get("library_id") or "").lower()
        role = str(member.get("role") or "").lower()
        if "terminal" in lib_id or role in {"terminal", "tb", "terminal_block", "terminal_strip"}:
            return True
    return False


def _target_prefix(session: Any, project: Project, fam: dict[str, Any], target: str) -> str:
    """Letter of the symbol we are switching to.

    A NEMA device's own ref_prefix is CB/M/ES. resolve_ref_prefix only consults
    ref_prefixes.yaml for NEMA/ANSI, so looking up the source device at IEC
    leaves the tag unchanged. IEC 81346-2 letters are the counterpart's
    library ref_prefix. When a component carries an explicit ``role`` (or is an
    unmapped tag with a known role/prefix), ``role_prefix_for`` disambiguates
    shared IEC prefixes (``-K`` contactor vs relay vs timer, ``-Q`` breaker vs
    disconnect vs contactor, ``-F`` fuse vs overload, ``-S`` operator roles).
    """

    leader = min(fam["members"].values(), key=_rank)
    lib_id = str(leader["library_id"] or "")
    role = str(leader.get("role") or "")
    parsed = parse_device_tag(leader["id"])
    src_prefix = parsed[0] if parsed else ""
    if "offpage" in lib_id.lower() or "junction" in lib_id.lower() or "connection.node" in lib_id.lower():
        return src_prefix or "OP"
    mapped = counterpart(lib_id, target)
    if mapped:
        lib_id = mapped
    try:
        device = session.catalog.get(lib_id) if lib_id else None
    except KeyError:
        device = None
    overrides = getattr(project, "ref_prefixes", None)
    if device is not None and overrides:
        cat = str(getattr(device, "category", "") or "").strip()
        if cat and cat in overrides:
            return resolve_ref_prefix(
                device,
                standard=target,
                maps=getattr(session.catalog, "ref_prefix_maps", None),
                overrides=overrides,
            )
    if role:
        by_role = role_prefix_for(target, role=role, source_prefix=src_prefix)
        if by_role:
            return by_role
    if device is None:
        by_fallback = role_prefix_for(
            target,
            role=role,
            library_id=lib_id,
            source_prefix=src_prefix,
        )
        if by_fallback:
            return by_fallback
        return src_prefix or "A"
    return resolve_ref_prefix(
        device,
        standard=target,
        maps=getattr(session.catalog, "ref_prefix_maps", None),
        overrides=overrides,
    )


def _assign_kept_number(
    fam: dict[str, Any],
    prefix: str,
    taken: set[str],
    mapping: dict[str, str],
    bumps: list[dict[str, str]],
) -> None:
    members = list(fam["members"].values())
    suffixes = [member["suffix"] for member in members]
    chosen_n = int(fam["number"])
    assigned = _fit(prefix, chosen_n, suffixes, taken)
    bumped = False
    if assigned is None:
        bumped = True
        chosen_n = 1
        assigned = None
        while chosen_n < 10000:
            assigned = _fit(prefix, chosen_n, suffixes, taken)
            if assigned is not None:
                break
            chosen_n += 1
        if assigned is None:
            raise ValueError(f"no free tag for {fam['base']}")
    for member, new_id in zip(members, assigned, strict=True):
        mapping[member["id"]] = new_id
        taken.add(new_id)
    if bumped:
        bumps.append({"from": fam["base"], "to": f"{prefix}{chosen_n}", "flag": "tag_collision"})


def _assign_sequential(
    pending: list[dict[str, Any]],
    taken: set[str],
    mapping: dict[str, str],
    bumps: list[dict[str, str]],
) -> None:
    """Number each letter family 1, 2, 3… in sheet, then column, order."""

    buckets: dict[str, list[dict[str, Any]]] = {}
    for fam in pending:
        buckets.setdefault(str(fam["prefix"]), []).append(fam)
    for prefix, group in buckets.items():
        group.sort(key=lambda fam: (_family_place(fam), fam["base"]))
        number = 1
        for fam in group:
            members = list(fam["members"].values())
            suffixes = [member["suffix"] for member in members]
            skipped = False
            while _fit(prefix, number, suffixes, taken) is None:
                skipped = True
                number += 1
                if number > 10000:
                    raise ValueError(f"no free tag for {fam['base']}")
            assigned = _fit(prefix, number, suffixes, taken)
            assert assigned is not None
            for member, new_id in zip(members, assigned, strict=True):
                mapping[member["id"]] = new_id
                taken.add(new_id)
            # A normal rename (S2 -> PB1) is not a collision. Flag only a hole
            # left because some other tag already holds that number.
            if skipped:
                bumps.append(
                    {"from": fam["base"], "to": f"{prefix}{number}", "flag": "tag_collision"}
                )
            number += 1


def _inverse_map(project: Project, target: str) -> dict[str, str]:
    record = project.properties.get("convert_standard")
    if not isinstance(record, dict):
        return {}
    stored = record.get("tag_map") or {}
    if not isinstance(stored, dict) or not stored:
        return {}
    try:
        source = target_standard(str(record.get("source") or ""))
        previous = target_standard(str(record.get("target") or ""))
    except ValueError:
        return {}
    if source != target_standard(target) or previous == target_standard(target):
        return {}
    inverse: dict[str, str] = {}
    for old, new in stored.items():
        if str(old) != str(new):
            inverse[str(new)] = str(old)
    return inverse


def _inverse_fits(fam: dict[str, Any], inverse: dict[str, str], taken: set[str]) -> bool:
    new_base = inverse.get(fam["base"])
    seen: set[str] = set()
    for member in fam["members"].values():
        if member["id"] in inverse:
            new_id = inverse[member["id"]]
        elif new_base is not None:
            new_id = new_base + member["suffix"]
        else:
            return False
        if new_id in taken or new_id in seen:
            return False
        seen.add(new_id)
    return True


def _fit(prefix: str, number: int, suffixes: list[str], taken: set[str]) -> list[str] | None:
    ids = [f"{prefix}{number}{suffix}" for suffix in suffixes]
    if len(ids) != len(set(ids)):
        return None
    if any(item in taken for item in ids):
        return None
    return ids


def _rank(member: dict[str, Any]) -> tuple[int, int, str]:
    role = str(member.get("role") or "")
    role_rank = _ROLE_RANK.get(role, 40)
    if member.get("suffix"):
        role_rank += 10
    kind_rank = _KIND_RANK.get(str(member.get("kind") or ""), 9)
    return (role_rank, kind_rank, str(member.get("id") or ""))


def _rename_all(project: Project, mapping: dict[str, str]) -> None:
    changed = [(old, new) for old, new in mapping.items() if old != new]
    changed.sort(key=lambda pair: len(pair[0]), reverse=True)
    rails = _rail_names(project)
    temps: list[tuple[str, str]] = []
    for index, (old, new) in enumerate(changed):
        temp = f"__cvt{index}"
        _rename_exact(project, old, temp, rails)
        temps.append((temp, new))
    for temp, new in temps:
        _rename_exact(project, temp, new, rails)
    _rewrite_texts(project, dict(changed), rails)


def _rail_names(project: Project) -> set[str]:
    names: set[str] = set()
    for sheet in project.sheets:
        hot = str(getattr(sheet, "rail_hot", "") or "")
        ret = str(getattr(sheet, "rail_return", "") or "")
        if hot:
            names.add(hot)
        if ret:
            names.add(ret)
    return names


def _rename_exact(project: Project, old: str, new: str, rails: set[str]) -> None:
    """Rename a device refdes. Terminal names, nets, and rail names stay."""

    if old == new:
        return
    for sheet in project.sheets:
        ids = [comp.id for comp in sheet.components]
        if new in ids and old not in ids:
            raise ValueError(f"{sheet.id} already has {new}")
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id == old:
                comp.id = new
            for key in ("parent", "dest_ref", "tag"):
                if comp.properties.get(key) == old:
                    comp.properties[key] = new
        for wire in sheet.wires:
            wire.frm = _swap_end(wire.frm, old, new, rails)
            wire.to = _swap_end(wire.to, old, new, rails)


def _swap_end(end: str, old: str, new: str, rails: set[str] | None = None) -> str:
    """Rename the device half of ``REF.PIN``. A bare rail token is not a device."""

    text = str(end or "")
    prefix = old + "."
    if text.startswith(prefix):
        return new + text[len(old) :]
    if text == old and text not in (rails or set()):
        return new
    return text


def _rewrite_texts(project: Project, mapping: dict[str, str], rails: set[str] | None = None) -> None:
    rails = rails or set()
    keys = [old for old, new in mapping.items() if old != new and old not in rails]
    keys.sort(key=len, reverse=True)
    if not keys:
        return
    # A dot keeps the pin: ``M1.U1`` must not become ``M1.VFD1`` when device U1 is renamed.
    pattern = re.compile("|".join(rf"(?<![A-Za-z0-9.:-]){re.escape(old)}(?![A-Za-z0-9])" for old in keys))

    def _sub(text: str) -> str:
        return pattern.sub(lambda match: mapping[match.group(0)], text)

    for sheet in project.sheets:
        for comp in sheet.components:
            for key in _TEXT_KEYS:
                value = comp.properties.get(key)
                if isinstance(value, str) and value:
                    comp.properties[key] = _sub(value)


def _planned_tag_map(ws: dict[str, Any]) -> dict[str, str]:
    for chunk in ws["chunks"]:
        if chunk["kind"] == "tags":
            return {str(old): str(new) for old, new in (chunk.get("tag_map") or {}).items()}
    return {}


def _preview_unmapped(session: Any, sheet_ids: list[str], target: str) -> list[dict[str, str]]:
    selected = set(sheet_ids)
    rows: list[dict[str, str]] = []
    for sheet in session.project.sheets:
        if sheet.id not in selected:
            continue
        for comp in sheet.components:
            low_lib = str(comp.library_id or "").lower()
            if "offpage" in low_lib or "junction" in low_lib or "connection.node" in low_lib:
                continue
            if counterpart(comp.library_id, target) is None:
                rows.append(_unmapped_issue(session, comp, sheet.id))
    return rows


def _active_tag_map(ws: dict[str, Any]) -> dict[str, str]:
    for chunk in ws["chunks"]:
        if chunk["kind"] == "tags" and chunk["status"] in {"done", "accepted"}:
            return {str(old): str(new) for old, new in (chunk.get("tag_map") or {}).items()}
    return {}


def _refresh_xref(session: Any) -> None:
    from panelcad.core.xref import update_cross_references
    from panelcad.render.wiring import clear_route_cache

    update_cross_references(session.project, session.catalog)
    clear_route_cache()


def _dependents(ws: dict[str, Any], chunk: dict[str, Any]) -> list[dict[str, Any]]:
    index = ws["chunks"].index(chunk)
    if chunk["kind"] == "tags":
        return list(ws["chunks"][index + 1 :])
    if chunk["kind"] == "symbols":
        return [
            item
            for item in ws["chunks"][index + 1 :]
            if item["kind"] == "layout"
        ]
    if chunk["kind"] == "layout":
        return [
            item
            for item in ws["chunks"][index + 1 :]
            if item["kind"] == "layout"
        ]
    return []


def _rebuild(session: Any, ws: dict[str, Any]) -> None:
    ws.pop("pre_consolidate_sheets", None)
    _install(session, Project.model_validate(ws["source_project"]))
    for chunk in ws["chunks"]:
        if chunk["status"] not in {"done", "accepted"}:
            continue
        _execute(session, ws, chunk)


def _resume_working(session: Any, ws: dict[str, Any]) -> None:
    if any(chunk["status"] in {"done", "accepted"} for chunk in ws["chunks"]):
        if ws.get("working_project"):
            _install(session, Project.model_validate(ws["working_project"]))
        return
    if ws.get("unconsolidate_on_start") and ws.get("source_project"):
        _install(session, Project.model_validate(ws["source_project"]))


def _install(session: Any, project: Project) -> None:
    aid = str(getattr(session, "active_sheet_id", "") or "")
    session.project = project
    touch = getattr(session, "_touch_drawing", None)
    if callable(touch):
        touch()
    if aid and any(sheet.id == aid for sheet in project.sheets):
        return
    if project.sheets:
        session.active_sheet_id = project.sheets[0].id


def _stamp_working(session: Any, ws: dict[str, Any]) -> None:
    ws["working_project"] = session.project.model_dump(mode="json")


def _chunk(ws: dict[str, Any], chunk_id: str) -> dict[str, Any]:
    for chunk in ws["chunks"]:
        if chunk["id"] == chunk_id:
            return chunk
    raise ValueError(f"no chunk '{chunk_id}'")


def _public(ws: dict[str, Any]) -> dict[str, Any]:
    tag_map: dict[str, str] = {}
    pin_rewrites: list[dict[str, str]] = []
    rail_map: dict[str, dict[str, str]] = {}
    unmapped: list[dict[str, Any]] = []
    for chunk in ws["chunks"]:
        if chunk["status"] not in {"done", "accepted"}:
            continue
        tag_map.update({str(k): str(v) for k, v in (chunk.get("tag_map") or {}).items()})
        pin_rewrites.extend(chunk.get("pin_rewrites") or [])
        if chunk.get("rail"):
            rail_map[str(chunk.get("sheet") or "")] = dict(chunk["rail"])
        for issue in chunk.get("issues") or []:
            if issue.get("flag") in {"unmapped_symbol", "unmapped_pin"}:
                unmapped.append(dict(issue))
    pending = [chunk["id"] for chunk in ws["chunks"] if chunk["status"] == "pending"]
    return {
        "workspace": ws["path"],
        "out_path": ws.get("out_path") or "",
        "target": ws["target"],
        "source_standard": ws["source_standard"],
        "source_path": ws.get("source_path") or "",
        "sheets": list(ws.get("sheets") or []),
        "whole_project": bool(ws.get("whole_project")),
        "tag_map": tag_map,
        "planned_tag_map": _planned_tag_map(ws),
        "pin_rewrites": pin_rewrites,
        "unmapped_preview": list(ws.get("unmapped_preview") or []),
        "rail_map": rail_map,
        "unmapped": unmapped,
        "pending": pending,
        "done": not pending,
        "chunks": [_chunk_public(chunk) for chunk in ws["chunks"]],
        "connection_check": ws.get("connection_check") or {},
    }


def _chunk_public(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": chunk["id"],
        "kind": chunk["kind"],
        "sheet": chunk.get("sheet") or "",
        "status": chunk["status"],
        "issues": list(chunk.get("issues") or []),
        "suggestion": chunk.get("suggestion") or "",
        "tag_bumps": list(chunk.get("tag_bumps") or []),
    }


def _default_out(source: Path | None, target: str, name: str) -> Path:
    token = "iec" if target_standard(target) == "IEC" else "nema"
    if source is not None:
        source = Path(source)
        text = source.name
        if text.endswith(".pcad.yaml"):
            stem = text[: -len(".pcad.yaml")]
            return source.with_name(f"{stem}-{token}.pcad.yaml")
        return source.with_name(f"{source.stem}-{token}{source.suffix or '.yaml'}")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "project"
    return Path.cwd() / f"{slug}-{token}.pcad.yaml"


def _workspace_for(out: Path) -> Path:
    text = out.name
    if text.endswith(".pcad.yaml"):
        stem = text[: -len(".pcad.yaml")]
    else:
        stem = out.stem
    return out.with_name(stem + ".convert.json")


def _refuse_source(source: Path | str | None, dest: Path) -> None:
    if not source:
        return
    try:
        same = Path(source).resolve() == Path(dest).resolve()
    except OSError:
        same = str(source) == str(dest)
    if same:
        raise ValueError("refusing to overwrite the source project; choose a different save-as path")


def _ws_path(session: Any, workspace: str | Path | None) -> Path:
    if workspace:
        return Path(workspace)
    current = getattr(session, "_convert_workspace", None)
    if current:
        return Path(current)
    raise ValueError("no conversion workspace; call convert_standard_plan first")


def _load(path: str | Path) -> dict[str, Any]:
    dest = Path(path)
    if not dest.is_file():
        raise ValueError(f"no conversion workspace at {dest}")
    data = json.loads(dest.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "chunks" not in data:
        raise ValueError(f"{dest} is not a conversion workspace")
    data["path"] = str(dest)
    return data


def _save(ws: dict[str, Any]) -> None:
    dest = Path(ws["path"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(ws, indent=2), encoding="utf-8")


def connection_report(session: Any, ws: dict[str, Any]) -> dict[str, Any]:
    """From-To nets before and after, mapped back through the conversion maps."""

    from panelcad.convert.from_to_check import check_connections

    public = _public(ws)
    before = Project.model_validate(ws["source_project"])
    return check_connections(
        before,
        session.project,
        session.catalog,
        tag_map=public["tag_map"],
        pin_rewrites=public["pin_rewrites"],
        rail_map=public["rail_map"],
    )


def _write_out(session: Any, ws: dict[str, Any]) -> None:
    from panelcad.convert.from_to_check import ConnectionLost

    report = connection_report(session, ws)
    # Mid-way through a stepped conversion the sheets are half renamed (one
    # sheet's rails converted, the next not yet), so the nets cannot match.
    # Record the check, but only block once every chunk has run. Nothing is
    # saved until then, and a copy from an earlier run is removed, so a
    # half-converted or unchecked copy never sits at the save-as path.
    partial = any(chunk.get("status") == "pending" for chunk in ws["chunks"])
    report["partial"] = partial
    ws["connection_check"] = report
    if partial or not report["ok"]:
        _drop_written(session, ws)
        _save(ws)
        if partial:
            return
        raise ConnectionLost(report, source_project=ws["source_project"])
    _save(ws)
    raw = ws.get("out_path") or ""
    if not raw:
        return
    dest = Path(raw)
    _refuse_source(ws.get("source_path"), dest)
    dropped = str(ws.get("out_dropped") or "")
    if dropped and dest.exists() and _same_path(dropped, dest):
        # We removed our own copy here and cleared the session's disk stamp.
        # Something new now sits at that path, so it is not ours to replace.
        raise FileExistsError(
            f"{dest} was created after the earlier converted copy was removed; "
            "it was not overwritten. Move it or choose a different save-as path."
        )
    session.save(dest)
    ws.pop("out_dropped", None)
    ws["out_written"] = {"path": str(dest), "sha256": _file_hash(dest)}
    _save(ws)


def _same_path(a: Path | str, b: Path | str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def _file_hash(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _drop_written(session: Any, ws: dict[str, Any]) -> None:
    """Remove the save-as copy this workspace wrote, only if it is unchanged since.

    Saving the copy made it the session's file, so its on-disk stamp is
    cleared too; otherwise the next save to the same path is refused as
    "changed on disk".
    """

    info = ws.pop("out_written", None) or {}
    raw = str(info.get("path") or "")
    digest = str(info.get("sha256") or "")
    if not raw or not digest:
        return
    path = Path(raw)
    source = ws.get("source_path")
    try:
        if source and path.resolve() == Path(source).resolve():
            return
    except OSError:
        return
    if path.is_file() and _file_hash(path) == digest:
        try:
            path.unlink()
        except OSError:
            return
        stamped = getattr(session, "_stamped_path", None)
        try:
            same = stamped is not None and Path(stamped).resolve() == path.resolve()
        except OSError:
            same = False
        ws["out_dropped"] = str(path)
        if same:
            session._disk_stamp = ""
