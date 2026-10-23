"""Graphical terminal strip: one schematic record, drawn to catalog scale.

Schematic reading order (sheet, then top to bottom, then left to right) is the
terminal order. The strip is drawn in that order at catalog block width. A long
strip continues on the next row, then on the next sheet. It is never scaled down.

Reordering on the strip moves those symbols into the new order, writes levels
1..n, and retags to ``STRIP-n``. ``sequence_terminal_strip`` reads the schematic
and writes the same levels (and the same tags when ``rename`` is set). After a
reorder, sequence changes nothing. Retag uses that same schematic order.

End stops, end plates, and partitions are real hardware on the panel DIN rail
(category ``connection.accessory``), packed with the terminal footprints. A
jumper is still ``properties.jumper`` on the terminal (so old files load). The
catalog pick for that span is a real BOM part, drawn on the bar.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.electrical import (
    LAYOUT_KINDS,
    SCHEMATIC_KINDS,
    _apply_rename_plan,
    _device,
    _is_spare,
    _lands,
    is_pe_terminal,
    terminal_identity,
    terminal_lands,
)
from panelcad.core.layout import clip_offset, footprint_size
from panelcad.core.models import Component, Project, Rail, Sheet, Wire
from panelcad.library.catalog import Catalog
from panelcad.render.titleblock import working_rect

ACCESSORIES: dict[str, tuple[str, str]] = {
    "end_plate": ("iec.accessories.end_plate", "XP"),
    "end_stop": ("iec.accessories.end_stop", "XS"),
    "partition": ("iec.accessories.partition", "XD"),
}
_KIND_ALIAS = {
    "endplate": "end_plate",
    "end_plate": "end_plate",
    "plate": "end_plate",
    "endstop": "end_stop",
    "end_stop": "end_stop",
    "stop": "end_stop",
    "partition": "partition",
    "divider": "partition",
    "separator": "partition",
}

# Fallback millimetres when a symbol has no panel footprint.
WIDTHS = {"terminal": 5.2, "end_plate": 2.5, "end_stop": 9.5, "partition": 2.0}
BLOCK_H = 47.0
HEADER = 14.0
CALLOUT = 18.0
RAIL_BAND = 16.0
ROW_GAP = 8.0


def _require_name(strip: str) -> str:
    name = str(strip or "").strip()
    if not name:
        raise ValueError("strip name is required")
    return name


def _normalize_kind(kind: str) -> str:
    token = str(kind or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _KIND_ALIAS.get(token, token)


def _end_ref(end: str) -> str:
    return str(end or "").split(".", 1)[0]


def _used_ids(project: Project) -> set[str]:
    return {comp.id for sheet in project.sheets for comp in sheet.components}


def _next_id(project: Project, prefix: str) -> str:
    used = _used_ids(project)
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def _schematic_terminals(project: Project, catalog: Catalog, strip: str) -> list[tuple[Sheet, Component]]:
    found: list[tuple[int, float, float, str, Sheet, Component]] = []
    for index, sheet in enumerate(project.sheets):
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            if str(comp.properties.get("accessory") or "").strip():
                continue
            ident = terminal_identity(comp, _device(catalog, comp))
            if ident is None or ident[0] != strip:
                continue
            found.append((index, float(comp.y), float(comp.x), comp.id, sheet, comp))
    found.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    return [(sheet, comp) for _index, _y, _x, _ref, sheet, comp in found]


def _accessories(project: Project, strip: str) -> list[tuple[Sheet, Component]]:
    found: list[tuple[Sheet, Component]] = []
    for sheet in project.sheets:
        for comp in sheet.components:
            if str(comp.properties.get("strip") or "") != strip:
                continue
            if not str(comp.properties.get("accessory") or "").strip():
                continue
            found.append((sheet, comp))
    return found


def _caption(wires: list[Wire]) -> str:
    parts: list[str] = []
    for wire in wires:
        head = str(wire.number or wire.net or wire.label or "").strip()
        spec = wire.spec_text()
        text = " ".join(part for part in (head, spec) if part)
        if text and text not in parts:
            parts.append(text)
    return ", ".join(parts)


def _strip_pages(project: Project, name: str) -> list[Sheet]:
    pages = [sheet for sheet in project.sheets if sheet.kind == "terminal" and sheet.strip == name]
    pages.sort(key=lambda sheet: (int(sheet.strip_part or 1), sheet.id))
    return pages


def _page_sheet(project: Project, name: str) -> Sheet:
    pages = _strip_pages(project, name)
    for sheet in pages:
        if int(sheet.strip_part or 1) == 1:
            return sheet
    if pages:
        return pages[0]
    return Sheet(id="strip", kind="terminal", strip=name)


def _ensure_strip_sheet(session: Any, name: str) -> Sheet:
    for sheet in _strip_pages(session.project, name):
        if int(sheet.strip_part or 1) == 1:
            return sheet
    from panelcad.api.session import _slug

    base = f"strip-{_slug(name)}"
    sid = base
    n = 2
    existing = {sheet.id for sheet in session.project.sheets}
    while sid in existing:
        sid = f"{base}-{n}"
        n += 1
    sheet = session.add_sheet(sid, title=f"Terminal strip {name}", kind="terminal", make_active=False)
    sheet.strip = name
    sheet.strip_part = 1
    return sheet


def _ensure_panel(session: Any) -> Sheet:
    for sheet in session.project.sheets:
        if sheet.kind == "panel_layout":
            if not sheet.rails:
                sheet.rails = [Rail(id="r1", x=40.0, y=80.0, length_mm=400.0)]
            return sheet
    existing = {sheet.id for sheet in session.project.sheets}
    sid = "panel"
    n = 2
    while sid in existing:
        sid = f"panel-{n}"
        n += 1
    return session.add_sheet(sid, title="Panel", kind="panel_layout", make_active=False)


def _block_mm(catalog: Catalog, comp: Component | None, kind: str) -> tuple[float, float]:
    if comp is not None:
        device = _device(catalog, comp)
        if device is not None:
            width, height = footprint_size(device, comp)
            if width > 0 and height > 0:
                return (float(width), float(height))
    return (WIDTHS.get(kind, 5.2), BLOCK_H)


def _ordered_parts(
    terminals: list[tuple[Sheet, Component]],
    accessories: list[Component],
) -> list[tuple[str, Component, Sheet | None]]:
    known = {comp.id for _sheet, comp in terminals}

    def acc(kind: str, end: str = "") -> list[Component]:
        rows: list[Component] = []
        for comp in accessories:
            if str(comp.properties.get("accessory") or "") != kind:
                continue
            if end and str(comp.properties.get("end") or "") != end:
                continue
            rows.append(comp)
        return rows

    by_after: dict[str, list[Component]] = {}
    loose: list[Component] = []
    for comp in acc("partition"):
        after = str(comp.properties.get("after") or "")
        if after in known:
            by_after.setdefault(after, []).append(comp)
        else:
            loose.append(comp)

    sequence: list[tuple[str, Component, Sheet | None]] = []
    for comp in acc("end_stop", "left"):
        sequence.append(("end_stop", comp, None))
    for comp in acc("end_plate", "left"):
        sequence.append(("end_plate", comp, None))
    for sheet, comp in terminals:
        sequence.append(("terminal", comp, sheet))
        for part in by_after.get(comp.id, []):
            sequence.append(("partition", part, None))
    for comp in loose:
        sequence.append(("partition", comp, None))
    for comp in acc("end_plate", "right"):
        sequence.append(("end_plate", comp, None))
    for comp in acc("end_stop", "right"):
        sequence.append(("end_stop", comp, None))
    return sequence


def _place_accessory(
    session: Any,
    sheet: Sheet,
    name: str,
    kind: str,
    *,
    end: str = "",
    after: str = "",
) -> str:
    library_id, prefix = ACCESSORIES[kind]
    ref = _next_id(session.project, prefix)
    props: dict[str, str] = {"strip": name, "accessory": kind}
    if end:
        props["end"] = end
    if after:
        props["after"] = after
    session.place(library_id, ref=ref, x=0.0, y=0.0, sheet_id=sheet.id, properties=props, snap=False)
    return ref


def _ends_present(project: Project, name: str) -> dict[str, set[str]]:
    have: dict[str, set[str]] = {"end_stop": set(), "end_plate": set()}
    for _sheet, comp in _accessories(project, name):
        kind = str(comp.properties.get("accessory") or "")
        end = str(comp.properties.get("end") or "")
        if kind in have and end in {"left", "right"}:
            have[kind].add(end)
    return have


def _ensure_ends(session: Any, name: str) -> None:
    have = _ends_present(session.project, name)
    missing = [
        (kind, end)
        for kind in ("end_stop", "end_plate")
        for end in ("left", "right")
        if end not in have[kind]
    ]
    if not missing:
        return
    sheet = _ensure_strip_sheet(session, name)
    for kind, end in missing:
        _place_accessory(session, sheet, name, kind, end=end)


def _write_levels(project: Project, catalog: Catalog, name: str) -> None:
    for level, (_sheet, comp) in enumerate(_schematic_terminals(project, catalog, name), start=1):
        comp.properties["strip"] = name
        comp.properties["terminal"] = str(level)


def _pinned_sheet(sheet: Sheet, comp: Component, moving: set[str]) -> str | None:
    for wire in sheet.wires:
        refs = {_end_ref(wire.frm), _end_ref(wire.to)}
        if comp.id not in refs:
            continue
        for other in refs - {comp.id}:
            if other not in moving:
                return sheet.id
    return None


def _transfer_wires(src: Sheet, dest: Sheet, ref: str, dest_of: dict[str, str]) -> None:
    if src.id == dest.id:
        return
    keep: list[Wire] = []
    moved: list[Wire] = []
    for wire in src.wires:
        refs = {_end_ref(wire.frm), _end_ref(wire.to)}
        if ref not in refs:
            keep.append(wire)
            continue
        others = refs - {ref}
        if others and all(dest_of.get(other) == dest.id for other in others):
            moved.append(wire)
        else:
            keep.append(wire)
    src.wires = keep
    dest.wires.extend(moved)


def _permute_slots(project: Project, catalog: Catalog, name: str, ordered_refs: list[str]) -> None:
    """Put ``ordered_refs`` into the schematic slots the strip already occupies."""

    found = _schematic_terminals(project, catalog, name)
    by_id = {comp.id: comp for _sheet, comp in found}
    located = {comp.id: sheet for sheet, comp in found}
    slots = [(sheet.id, float(comp.x), float(comp.y)) for sheet, comp in found]
    if len(slots) != len(ordered_refs) or any(ref not in by_id for ref in ordered_refs):
        raise ValueError(f"reorder lost a terminal on {name}")
    moving = set(ordered_refs)
    dest_of: dict[str, str] = {}
    for (sid, _x, _y), ref in zip(slots, ordered_refs, strict=True):
        dest_of[ref] = _pinned_sheet(located[ref], by_id[ref], moving) or sid
    for ref, sheet in located.items():
        sheet.components = [comp for comp in sheet.components if comp.id != ref]
    for (sid, x, y), ref in zip(slots, ordered_refs, strict=True):
        del sid
        comp = by_id[ref]
        src = located[ref]
        dest = project.sheet(dest_of[ref])
        comp.x = float(x)
        comp.y = float(y)
        if src.id != dest.id:
            _transfer_wires(src, dest, ref, dest_of)
        dest.components.append(comp)


def _home_sheet(project: Project, terminals: list[tuple[Sheet, Component]]) -> Sheet | None:
    counts: dict[str, int] = {}
    for sheet, _comp in terminals:
        if sheet.kind == "terminal":
            continue
        counts[sheet.id] = counts.get(sheet.id, 0) + 1
    best: Sheet | None = None
    best_n = 0
    for sheet in project.sheets:
        n = counts.get(sheet.id, 0)
        if n > best_n:
            best = sheet
            best_n = n
    if best is not None:
        return best
    for sheet in project.sheets:
        if sheet.kind in SCHEMATIC_KINDS:
            return sheet
    return None


def _retag(session: Any, name: str) -> list[dict[str, str]]:
    """Rename to STRIP-n in schematic order. Levels, wires, and panel tags follow."""

    project = session.project
    catalog = session.catalog
    ordered = _schematic_terminals(project, catalog, name)
    if not ordered:
        raise ValueError(f"no terminals on {name}")
    mapping: dict[str, str] = {}
    plan: list[tuple[Sheet, str, str]] = []
    for level, (sheet, comp) in enumerate(ordered, start=1):
        new = f"{name}-{level}"
        mapping[comp.id] = new
        comp.properties["strip"] = name
        comp.properties["terminal"] = str(level)
        if comp.id != new:
            plan.append((sheet, comp.id, new))
        for page in project.sheets:
            if page.kind not in LAYOUT_KINDS:
                continue
            for foot in page.components:
                if foot.id != comp.id:
                    continue
                foot.properties["strip"] = name
                foot.properties["terminal"] = str(level)
                if foot.id != new:
                    plan.append((page, foot.id, new))
    renamed = _apply_rename_plan(project, plan)
    for _sheet, comp in _accessories(project, name):
        after = str(comp.properties.get("after") or "")
        mapped = mapping.get(after)
        if mapped and mapped != after:
            comp.properties["after"] = mapped
    return renamed


def sync_strip_panel(session: Any, strip: str) -> None:
    """Pack this strip's terminals and end hardware on the panel DIN rail."""

    name = _require_name(strip)
    project = session.project
    catalog = session.catalog
    terminals = _schematic_terminals(project, catalog, name)
    pairs = _accessories(project, name)
    if not terminals and not pairs:
        return
    records = [comp.model_copy(deep=True) for _sheet, comp in pairs]
    acc_ids = {comp.id for comp in records}
    term_ids = {comp.id for _sheet, comp in terminals}
    from panelcad.library.panel import is_layout_ready, occupies_panel_layout

    ready: list[tuple[str, Component, Sheet | None]] = []
    for kind, src, home in _ordered_parts(terminals, records):
        device = catalog.get(src.library_id)
        if occupies_panel_layout(device, src) and not is_layout_ready(device, src):
            continue
        ready.append((kind, src, home))
    existing_panels = [sheet for sheet in project.sheets if sheet.kind in LAYOUT_KINDS]
    has_occupant = any(
        occupies_panel_layout(catalog.get(src.library_id), src) for _kind, src, _home in ready
    )
    if not has_occupant and not existing_panels:
        return
    if existing_panels:
        panel = existing_panels[0]
        if not panel.rails:
            panel.rails = [Rail(id="r1", x=40.0, y=80.0, length_mm=400.0)]
    else:
        panel = _ensure_panel(session)
    copied_acc: set[str] = {src.id for _kind, src, _home in ready if src.id in acc_ids}
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS:
            sheet.components = [
                comp
                for comp in sheet.components
                if comp.id not in term_ids
                and comp.id not in acc_ids
                and str(comp.properties.get("strip") or "") != name
            ]
        else:
            # Keep unready accessories on the strip sheet so the drawing / BOM
            # still lists them. Only the copies that landed on the panel leave.
            sheet.components = [comp for comp in sheet.components if comp.id not in copied_acc]
    from panelcad.core.layout import rects_overlap

    blocked: list[tuple[float, float, float, float]] = []
    for comp in panel.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        width, height = footprint_size(device, comp)
        if width > 0 and height > 0:
            blocked.append((comp.x, comp.y, width, height))
    rail = panel.rails[0]
    cursor = float(rail.x)
    for _kind, src, _sheet in ready:
        device = catalog.get(src.library_id)
        width, height = footprint_size(device, src)
        comp = src.model_copy(deep=True)
        comp.properties["strip"] = name
        y = float(rail.y) - clip_offset(device, src)
        step = max(float(width), 0.0)
        guard = 0
        while guard < 40 and height > 0 and any(
            rects_overlap(cursor, y, step, height, *box, tol=0.2) for box in blocked
        ):
            guard += 1
            hits = [box for box in blocked if rects_overlap(cursor, y, max(step, 0.1), height, *box, tol=0.2)]
            cursor = max(box[0] + box[2] for box in hits)
        comp.x = cursor
        comp.y = y
        panel.components.append(comp)
        if step > 0 and height > 0:
            blocked.append((comp.x, comp.y, step, height))
        cursor += step
    if cursor - float(rail.x) > float(rail.length_mm):
        rail.length_mm = cursor - float(rail.x) + 2.0


def _sync_pages(session: Any, name: str) -> int:
    primary = _ensure_strip_sheet(session, name)
    primary.strip = name
    primary.strip_part = 1
    layout = layout_terminal_strip(session.project, session.catalog, name)
    parts = max([1, *[int(item["part"]) for item in layout["items"]]])
    existing = {int(sheet.strip_part or 1): sheet for sheet in _strip_pages(session.project, name)}
    for part in range(2, parts + 1):
        sheet = existing.get(part)
        if sheet is None:
            sid = f"{primary.id}-{part}"
            n = part
            ids = {page.id for page in session.project.sheets}
            while sid in ids:
                n += 1
                sid = f"{primary.id}-{n}"
            sheet = session.add_sheet(
                sid,
                title=f"Terminal strip {name} ({part})",
                kind="terminal",
                make_active=False,
            )
        sheet.strip = name
        sheet.strip_part = part
        sheet.page = primary.page.model_copy(deep=True)
        if part > 1 and not str(sheet.title or "").strip():
            sheet.title = f"Terminal strip {name} ({part})"
    for sheet in list(session.project.sheets):
        if sheet.kind != "terminal" or sheet.strip != name:
            continue
        if int(sheet.strip_part or 1) > parts and not sheet.components:
            session.project.sheets = [page for page in session.project.sheets if page.id != sheet.id]
            if session.active_sheet_id == sheet.id:
                session.active_sheet_id = primary.id
    return parts


def list_terminal_strips(project: Project, catalog: Catalog) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    sheets: dict[str, str] = {}
    for sheet in project.sheets:
        if sheet.kind == "terminal" and str(sheet.strip or "").strip():
            counts.setdefault(sheet.strip, 0)
            if int(sheet.strip_part or 1) == 1:
                sheets[sheet.strip] = sheet.id
        if sheet.kind in LAYOUT_KINDS:
            for comp in sheet.components:
                acc_strip = str(comp.properties.get("strip") or "").strip()
                if acc_strip and str(comp.properties.get("accessory") or "").strip():
                    counts.setdefault(acc_strip, counts.get(acc_strip, 0))
            continue
        for comp in sheet.components:
            ident = terminal_identity(comp, _device(catalog, comp))
            if ident is not None:
                counts[ident[0]] = counts.get(ident[0], 0) + 1
            acc_strip = str(comp.properties.get("strip") or "").strip()
            if acc_strip and str(comp.properties.get("accessory") or "").strip():
                counts.setdefault(acc_strip, counts.get(acc_strip, 0))
    return [
        {"strip": name, "terminals": counts.get(name, 0), "sheet": sheets.get(name, "")}
        for name in sorted(counts)
    ]


def layout_terminal_strip(project: Project, catalog: Catalog, strip: str) -> dict[str, Any]:
    """Catalog-width blocks. Rows continue across the sheet, then onto the next sheet."""

    name = _require_name(strip)
    terminals = _schematic_terminals(project, catalog, name)
    accessories = [comp for _sheet, comp in _accessories(project, name)]
    sequence = _ordered_parts(terminals, accessories)
    page = _page_sheet(project, name)
    left, top, width, height = working_rect(page)
    right = left + width
    bottom = top + height
    content_top = top + HEADER

    raw: list[dict[str, Any]] = []
    for kind, comp, sheet in sequence:
        block_w, block_h = _block_mm(catalog, comp, kind)
        item: dict[str, Any] = {
            "kind": kind,
            "ref": comp.id,
            "library_id": comp.library_id,
            "x": 0.0,
            "y": 0.0,
            "w": block_w,
            "h": block_h,
            "level": "",
            "jumper": "",
            "spare": False,
            "pe": False,
            "internal": "",
            "external": "",
            "sheet": sheet.id if sheet is not None else "",
            "end": str(comp.properties.get("end") or ""),
            "part": 1,
            "row": 0,
        }
        if kind == "terminal" and sheet is not None:
            device = _device(catalog, comp)
            ident = terminal_identity(comp, device)
            internal, external = terminal_lands(sheet, comp, device)
            item["level"] = ident[1] if ident else ""
            item["jumper"] = str(comp.properties.get("jumper") or "")
            item["spare"] = _is_spare(comp, internal, external, device) == "yes"
            item["internal"] = _caption(internal)
            item["external"] = _caption(external)
            item["pe"] = is_pe_terminal(comp, device)
        raw.append(item)

    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    cursor = left
    for item in raw:
        if current and cursor + float(item["w"]) > right + 0.05:
            rows.append(current)
            current = []
            cursor = left
        current.append(item)
        cursor += float(item["w"])
    if current:
        rows.append(current)

    items: list[dict[str, Any]] = []
    part = 1
    row_in_part = 0
    y_cursor = content_top + CALLOUT
    for row_blocks in rows:
        block_h = max(float(block["h"]) for block in row_blocks)
        row_bottom = y_cursor + block_h + CALLOUT + RAIL_BAND
        if row_in_part > 0 and row_bottom > bottom + 0.5:
            part += 1
            row_in_part = 0
            y_cursor = content_top + CALLOUT
        x = left
        for block in row_blocks:
            block["x"] = x
            block["y"] = y_cursor + block_h - float(block["h"])
            block["part"] = part
            block["row"] = row_in_part
            items.append(block)
            x += float(block["w"])
        y_cursor += CALLOUT + block_h + CALLOUT + RAIL_BAND + ROW_GAP
        row_in_part += 1

    jumpers: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []
    run_id = ""
    run_key: tuple[int, int] | None = None

    def _flush() -> None:
        nonlocal run, run_id, run_key
        if len(run) >= 2 and run_id:
            jumpers.append(
                {
                    "id": run_id,
                    "refs": [block["ref"] for block in run],
                    "x1": run[0]["x"] + min(1.2, run[0]["w"] * 0.2),
                    "x2": run[-1]["x"] + run[-1]["w"] - min(1.2, run[-1]["w"] * 0.2),
                    "y": run[0]["y"] + min(9.0, run[0]["h"] * 0.2),
                    "ticks": [block["x"] + block["w"] / 2.0 for block in run],
                    "part": run[0]["part"],
                    "row": run[0]["row"],
                }
            )
        run = []
        run_id = ""
        run_key = None

    for item in items:
        if item["kind"] != "terminal":
            continue
        jid = str(item["jumper"] or "")
        key = (int(item["part"]), int(item["row"]))
        if jid and jid == run_id and key == run_key:
            run.append(item)
            continue
        _flush()
        if jid:
            run = [item]
            run_id = jid
            run_key = key
    _flush()

    from panelcad.core.jumper import attach_jumper_catalog

    attach_jumper_catalog(catalog, jumpers, items, project)
    sheet_id = ""
    for candidate in _strip_pages(project, name):
        if int(candidate.strip_part or 1) == 1:
            sheet_id = candidate.id
            break
    return {
        "strip": name,
        "sheet": sheet_id,
        "parts": part if items else 1,
        "items": items,
        "jumpers": jumpers,
    }


def open_terminal_strip(session: Any, strip: str) -> dict[str, Any]:
    name = _require_name(strip)
    sheet = _ensure_strip_sheet(session, name)
    _ensure_ends(session, name)
    sync_strip_panel(session, name)
    _sync_pages(session, name)
    session.set_active_sheet(sheet.id)
    return layout_terminal_strip(session.project, session.catalog, name)


def add_terminal(
    session: Any,
    strip: str,
    library_id: str = "iec.terminals.x_2p5",
    after: str | None = None,
) -> dict[str, Any]:
    name = _require_name(strip)
    catalog = session.catalog
    catalog.get(library_id)
    terminals = _schematic_terminals(session.project, catalog, name)
    after_ref = str(after or "").strip()
    ids = [comp.id for _sheet, comp in terminals]
    if after_ref and after_ref not in ids:
        raise ValueError(f"no terminal {after_ref} on {name}")
    home = _home_sheet(session.project, terminals)
    if home is None:
        home = _ensure_strip_sheet(session, name)
    slots = [(sheet.id, float(comp.x), float(comp.y)) for sheet, comp in terminals]
    if slots:
        last_sid, last_x, last_y = slots[-1]
        del last_sid
        extra = (home.id, last_x, last_y + 28.0)
    else:
        extra = (home.id, 40.0, 40.0)
    slots.append(extra)
    ref = _next_id(session.project, f"{name}-")
    if after_ref:
        ids.insert(ids.index(after_ref) + 1, ref)
    else:
        ids.append(ref)
    session.place(
        library_id,
        ref=ref,
        x=extra[1],
        y=extra[2],
        sheet_id=home.id,
        properties={"strip": name, "terminal": "0"},
        snap=False,
    )
    _permute_slots(session.project, catalog, name, ids)
    _write_levels(session.project, catalog, name)
    sync_strip_panel(session, name)
    _sync_pages(session, name)
    placed = None
    placed_sheet = home.id
    for sheet, comp in _schematic_terminals(session.project, catalog, name):
        if comp.id == ref:
            placed = comp
            placed_sheet = sheet.id
            break
    if placed is None:
        raise ValueError(f"could not place {ref} on {name}")
    return {
        "ref": ref,
        "strip": name,
        "terminal": str(placed.properties.get("terminal") or ""),
        "sheet": placed_sheet,
        "library_id": library_id,
    }


def _order_and_retag(session: Any, name: str, refs: list[str]) -> dict[str, Any]:
    _permute_slots(session.project, session.catalog, name, refs)
    renamed = _retag(session, name)
    sync_strip_panel(session, name)
    _sync_pages(session, name)
    return {
        "renamed": renamed,
        "layout": layout_terminal_strip(session.project, session.catalog, name),
    }


def reorder_terminals(session: Any, strip: str, refs: list[str]) -> dict[str, Any]:
    """Move schematic symbols into ``refs`` order, then retag STRIP-n to match."""

    name = _require_name(strip)
    want = [str(ref).strip() for ref in refs if str(ref).strip()]
    have = [comp.id for _sheet, comp in _schematic_terminals(session.project, session.catalog, name)]
    if not have:
        raise ValueError(f"no terminals on {name}")
    if len(want) != len(set(want)):
        raise ValueError("reorder list has a duplicate ref")
    missing = [ref for ref in have if ref not in set(want)]
    extra = [ref for ref in want if ref not in set(have)]
    if missing or extra or len(want) != len(have):
        detail: list[str] = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("not on this strip: " + ", ".join(extra))
        raise ValueError(f"reorder must list every terminal on {name} ({'; '.join(detail)})")
    return _order_and_retag(session, name, want)["layout"]


def move_terminal(session: Any, strip: str, ref: str, direction: str) -> dict[str, Any]:
    name = _require_name(strip)
    token = str(direction or "").strip().lower()
    if token in {"earlier", "left", "up", "-1"}:
        delta = -1
    elif token in {"later", "right", "down", "1", "+1"}:
        delta = 1
    else:
        raise ValueError("direction must be earlier or later")
    ids = [comp.id for _sheet, comp in _schematic_terminals(session.project, session.catalog, name)]
    if ref not in ids:
        raise ValueError(f"no terminal {ref} on {name}")
    index = ids.index(ref)
    other = index + delta
    if other < 0 or other >= len(ids):
        edge = "first" if delta < 0 else "last"
        raise ValueError(f"{ref} is already {edge}")
    ids[index], ids[other] = ids[other], ids[index]
    return _order_and_retag(session, name, ids)["layout"]


def retag_terminal_strip(session: Any, strip: str) -> dict[str, Any]:
    """Rename tags to STRIP-n in schematic order. The same rule as reorder and sequence."""

    name = _require_name(strip)
    renamed = _retag(session, name)
    sync_strip_panel(session, name)
    _sync_pages(session, name)
    return {
        "renamed": renamed,
        "layout": layout_terminal_strip(session.project, session.catalog, name),
    }


def add_strip_accessory(
    session: Any,
    strip: str,
    kind: str,
    after: str | None = None,
) -> dict[str, Any]:
    name = _require_name(strip)
    token = _normalize_kind(kind)
    if token not in ACCESSORIES:
        raise ValueError(f"unknown accessory '{kind}'")
    after_ref = str(after or "").strip()
    end = ""
    if token == "partition":
        ids = [comp.id for _sheet, comp in _schematic_terminals(session.project, session.catalog, name)]
        if after_ref:
            if after_ref not in ids:
                raise ValueError(f"no terminal {after_ref} on {name}")
        elif ids:
            after_ref = ids[-1]
        else:
            raise ValueError("a partition needs a terminal to sit beside")
    else:
        have = _ends_present(session.project, name)
        end = "left" if "left" not in have[token] else "right"
        after_ref = ""
    sheet = _ensure_strip_sheet(session, name)
    ref = _place_accessory(session, sheet, name, token, end=end, after=after_ref)
    sync_strip_panel(session, name)
    _sync_pages(session, name)
    return {
        "ref": ref,
        "kind": token,
        "strip": name,
        "after": after_ref,
        "end": end,
        "sheet": sheet.id,
        "library_id": ACCESSORIES[token][0],
    }


def remove_strip_component(session: Any, ref: str) -> dict[str, Any]:
    """Delete ``ref`` from the schematic, the strip, and the panel. Caller checkpoints."""

    token = str(ref or "").strip()
    if not token:
        raise KeyError("no component ''")
    project = session.project
    catalog = session.catalog
    strip_name = ""
    found = False
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id != token:
                continue
            found = True
            ident = terminal_identity(comp, _device(catalog, comp))
            if ident is not None:
                strip_name = ident[0]
            elif str(comp.properties.get("strip") or "").strip():
                strip_name = str(comp.properties.get("strip") or "").strip()
    if not found:
        raise KeyError(f"no component '{token}'")
    touched: list[str] = []
    for sheet in project.sheets:
        before = len(sheet.components)
        sheet.components = [comp for comp in sheet.components if comp.id != token]
        if len(sheet.components) != before:
            touched.append(sheet.id)
        sheet.wires = [
            wire
            for wire in sheet.wires
            if _end_ref(wire.frm) != token and _end_ref(wire.to) != token
        ]
    if strip_name:
        if _schematic_terminals(project, catalog, strip_name):
            _write_levels(project, catalog, strip_name)
        sync_strip_panel(session, strip_name)
        _sync_pages(session, strip_name)
    return {"ref": token, "strip": strip_name, "sheets": touched}
