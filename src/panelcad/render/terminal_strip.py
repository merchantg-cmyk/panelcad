"""Physical terminal-strip picture.

One renderer for every sheet that draws a terminal strip (print package,
PDF, SVG, DXF, and the terminal sheet on the canvas). Blocks, order, tags,
and wire numbers come from ``layout_terminal_strip``. The picture uses a
uniform block so the tags stay readable, wraps a long strip onto another
row, and continues on the next sheet when the project already has one.

The wiring list and the wire-by-wire sheet do not use this module.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.models import Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.render.painter import Painter
from panelcad.render.titleblock import working_rect

# Sheet kinds whose body is this picture. Anything else (including the
# wiring list) keeps its own renderer.
STRIP_SHEET_KINDS = ("terminal",)

TERMINAL_FILL = "#fbfaf3"
PE_FILL = "#d5edc8"
KIND_FILL = {
    "terminal": TERMINAL_FILL,
    "end_plate": "#eeeae0",
    "end_stop": "#e3dccf",
    "partition": "#ddd6c8",
}
RAIL_FILL = "#c5c8cc"
RAIL_EDGE = "#5c636a"
INK = "#1a1a1a"
SCREW = "#222222"
JUMPER = "#c45c26"
PE_MARK = "#2e5a32"
STOP_MARK = "#4a4a4a"
_KIND_WORD = {"end_plate": "END", "end_stop": "STOP", "partition": "PART"}

# Preferred block, millimetres at scale 1. A real strip is a row of narrow
# blocks; the scale search shrinks the whole picture until it fits the sheet.
BLOCK_W = 8.8
BLOCK_H = 32.0
TITLE_SIZE = 3.5
WIRE_SIZE = 2.2
TAG_SIZE = 2.3
# A continuation sheet is used before the tag is drawn smaller than this.
MIN_TAG_MM = 2.0
SIDE_SIZE = 2.25
LEGEND_SIZE = 2.15
RAIL_H = 4.6
RAIL_LIP = 1.15
RAIL_LABEL = 2.15
GROUP_GAP_X = 7.0
GROUP_GAP_Y = 5.5
SCREW_R = 1.0

LEGEND = (
    "Mounting order, left to right. Top = panel-side wire number. Inside = terminal tag. Bottom = field-side wire number.",
    "Green = PE block. Bar under each strip = DIN rail.",
)

_EMPTY_NAMED = "No terminals on this sheet."
_EMPTY_OPEN = "Open a strip to draw the rail and the blocks."


def draw_terminal_sheet(painter: Painter, sheet: Sheet, project: Project | None, catalog: Catalog) -> None:
    """Entry used by the sheet body (SVG, PDF, DXF, print package)."""

    if project is None:
        project = Project(name=sheet.title or sheet.id, sheets=[sheet])
    draw_physical_terminal_sheet(painter, sheet, project, catalog)


def draw_physical_terminal_sheet(
    painter: Painter,
    sheet: Sheet,
    project: Project,
    catalog: Catalog,
) -> None:
    scene = physical_scene_for_sheet(project, catalog, sheet)
    draw_physical_scene(painter, scene)


def physical_scene_for_sheet(project: Project, catalog: Catalog, sheet: Sheet) -> dict[str, Any]:
    """The picture for this sheet. ``strip_part`` selects a continuation page.

    Continuation sheets are created first, so a block past page 1 is drawn
    on a later sheet instead of being left off the print.
    """

    ensure_terminal_pages(project, catalog)
    pages = layout_physical_pages(project, catalog, sheet, fit_pages=None)
    if not pages:
        return _empty_scene(project, sheet, _EMPTY_OPEN)
    index = max(0, int(sheet.strip_part or 1) - 1)
    if index < len(pages):
        return pages[index]
    # More catalog sheets than picture pages: do not redraw page 1.
    return _empty_scene(project, sheet, _EMPTY_NAMED)


_ENSURING: set[int] = set()


def ensure_terminal_pages(project: Project, catalog: Catalog) -> None:
    """Give every drawn strip a sheet for each readable page.

    Called on load, on export, and before a sheet is painted. Idempotent.
    """

    key = id(project)
    if key in _ENSURING:
        return
    _ENSURING.add(key)
    try:
        _ensure_terminal_pages(project, catalog)
    finally:
        _ENSURING.discard(key)


def drawn_block_refs(project: Project, catalog: Catalog) -> list[str]:
    """Refs painted on every terminal sheet, after continuation sheets exist."""

    ensure_terminal_pages(project, catalog)
    refs: list[str] = []
    for sheet in project.sheets:
        if sheet.kind != "terminal":
            continue
        scene = physical_scene_for_sheet(project, catalog, sheet)
        refs.extend(str(block.get("ref") or "") for block in scene.get("blocks") or [])
    return refs


def model_block_refs(project: Project, catalog: Catalog) -> list[str]:
    """Every block the terminal-strip model would draw, in strip order."""

    refs: list[str] = []
    for name in _strip_names(project, catalog):
        items, _jumpers = _load_strip(project, catalog, name, None)
        refs.extend(str(item.get("ref") or "") for item in items)
    return refs


def readable_page_count(project: Project, catalog: Catalog, sheet: Sheet) -> int:
    """How many sheets this strip needs while tags stay at least ``MIN_TAG_MM``."""

    return max(1, len(layout_physical_pages(project, catalog, sheet, fit_pages=None)))


def terminal_strip_caption(name: str, terminals: int, page: int, pages: int) -> str:
    """Status line. A continued strip names its sheet."""

    continued = f"  ·  sheet {page}" if int(pages) > 1 else ""
    return f"terminal strip {name}  ·  {int(terminals)} terminals{continued}"


def min_readable_scale() -> float:
    return MIN_TAG_MM / TAG_SIZE


def layout_physical_pages(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    fit_pages: int | None = 1,
) -> list[dict[str, Any]]:
    """Paginated pictures.

    The scale stays large enough that terminal tags are at least
    ``MIN_TAG_MM``. A strip that still does not fit continues on the next page.
    """

    groups = _collect_groups(project, catalog, sheet)
    if not groups:
        return [_empty_scene(project, sheet, _empty_message(sheet))]
    floor = min_readable_scale()
    scale = 1.0
    pages = _build(groups, project, sheet, scale)
    while scale > floor + 1e-4 and not _pages_fit(pages, sheet):
        scale = max(floor, round(scale * 0.92, 4))
        pages = _build(groups, project, sheet, scale)
        if abs(scale - floor) < 1e-4:
            break
    if fit_pages is not None and len(pages) > fit_pages and scale > floor + 1e-4:
        # A caller that asked for fewer sheets may still shrink, but not
        # below the readable tag size. Extra blocks move to the next page.
        best = pages
        lo = floor
        hi = scale
        for _ in range(10):
            mid = (lo + hi) / 2.0
            trial = _build(groups, project, sheet, mid)
            if len(trial) <= fit_pages and _pages_fit(trial, sheet):
                best = trial
                lo = mid
            else:
                hi = mid
        pages = best
    for index, page in enumerate(pages):
        page["page_index"] = index + 1
        page["page_count"] = len(pages)
    return pages


def draw_physical_scene(painter: Painter, scene: dict[str, Any]) -> None:
    """Ink one picture. Callers do not draw blocks any other way."""

    for rail in scene.get("rails") or []:
        _ink_rail(painter, rail)
    for block in scene.get("blocks") or []:
        _ink_block(painter, block)
    for jumper in scene.get("jumpers") or []:
        _ink_jumper(painter, jumper)
    for label in scene.get("texts") or []:
        _ink_text(painter, label)


def text_rects(scene: dict[str, Any]) -> list[tuple[float, float, float, float]]:
    """Ink boxes for every label on the picture, in sheet millimetres."""

    rects: list[tuple[float, float, float, float]] = []
    for label in scene.get("texts") or []:
        rect = label.get("rect")
        if not rect or not str(label.get("text") or "").strip():
            continue
        rects.append(tuple(float(v) for v in rect))  # type: ignore[arg-type]
    return rects


def rects_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    gap: float = 0.0,
) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return False
    return ax < bx + bw + gap and bx < ax + aw + gap and ay < by + bh + gap and by < ay + ah + gap


def _empty_message(sheet: Sheet) -> str:
    if str(sheet.strip or "").strip():
        return _EMPTY_NAMED
    return _EMPTY_OPEN


def _rail_name(project: Project) -> str:
    for page in project.sheets:
        for rail in page.rails:
            kind = str(rail.type or "").strip()
            if kind:
                return kind.upper()
    return "TS35"


def _strip_names(project: Project, catalog: Catalog) -> list[str]:
    from panelcad.core.electrical import LAYOUT_KINDS, _device, terminal_identity
    from panelcad.core.terminal_strip import list_terminal_strips

    order: list[str] = []
    seen: set[str] = set()
    for page in project.sheets:
        if page.kind in LAYOUT_KINDS:
            continue
        for comp in page.components:
            ident = terminal_identity(comp, _device(catalog, comp))
            if ident is None or ident[0] in seen:
                continue
            seen.add(ident[0])
            order.append(ident[0])
    for row in list_terminal_strips(project, catalog):
        name = str(row.get("strip") or "")
        if name and name not in seen:
            seen.add(name)
            order.append(name)
    return order


def _description(project: Project, catalog: Catalog, name: str) -> str:
    from panelcad.core.jumper import strip_terminals

    found: list[str] = []
    for _page, comp in strip_terminals(project, catalog, name):
        text = ""
        for key in ("description", "function", "function_label"):
            text = str(comp.properties.get(key) or "").strip()
            if text:
                break
        if text:
            found.append(text)
    if found and len(set(found)) == 1:
        return found[0]
    return ""


def _terminal_sheets(project: Project) -> list[Sheet]:
    return [sheet for sheet in project.sheets if sheet.kind == "terminal"]


def _home_sheet(project: Project) -> Sheet | None:
    sheets = _terminal_sheets(project)
    if not sheets:
        return None
    primaries = [sheet for sheet in sheets if int(sheet.strip_part or 1) == 1]
    return primaries[0] if primaries else sheets[0]


def _family_sheets(project: Project, key: str) -> list[Sheet]:
    pages = [sheet for sheet in _terminal_sheets(project) if str(sheet.strip or "") == key]
    pages.sort(key=lambda sheet: (int(sheet.strip_part or 1), sheet.id))
    return pages


def _board_key(project: Project) -> str | None:
    """Family that draws strips which do not have a sheet of their own.

    An unnamed terminal sheet is that board. Otherwise the first terminal
    sheet takes those strips, so a strip is never left off the print.
    """

    for sheet in _terminal_sheets(project):
        if int(sheet.strip_part or 1) == 1 and not str(sheet.strip or "").strip():
            return ""
    home = _home_sheet(project)
    if home is None:
        return None
    return str(home.strip or "")


def _family_strips(project: Project, catalog: Catalog, key: str) -> list[str]:
    """Strips drawn by every page of one sheet family."""

    names = _strip_names(project, catalog)
    dedicated = {
        str(sheet.strip or "").strip()
        for sheet in _terminal_sheets(project)
        if str(sheet.strip or "").strip()
    }
    board = _board_key(project)
    picked: list[str] = []
    if key and (key == board or key in dedicated or key in names):
        picked.append(key)
    if board is not None and key == board:
        for name in names:
            if name == key or name in dedicated:
                continue
            picked.append(name)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in picked:
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _assignments(project: Project, catalog: Catalog, sheet: Sheet) -> list[tuple[str, int | None]]:
    """Strips drawn on this sheet's family. Every page of the family uses the same list."""

    key = str(sheet.strip or "")
    return [(name, None) for name in _family_strips(project, catalog, key)]


def _unique_sheet_id(project: Project, base: str) -> str:
    used = {sheet.id for sheet in project.sheets}
    sid = base
    n = 2
    while sid in used:
        sid = f"{base}-{n}"
        n += 1
    return sid


def _ensure_terminal_pages(project: Project, catalog: Catalog) -> None:
    families: list[str] = []
    seen: set[str] = set()
    for sheet in _terminal_sheets(project):
        key = str(sheet.strip or "")
        if key in seen:
            continue
        seen.add(key)
        families.append(key)
    for key in families:
        pages = _family_sheets(project, key)
        if not pages:
            continue
        primary = next((sheet for sheet in pages if int(sheet.strip_part or 1) == 1), pages[0])
        count = max(1, len(layout_physical_pages(project, catalog, primary, fit_pages=None)))
        existing = {int(sheet.strip_part or 1) for sheet in pages}
        for part in range(2, count + 1):
            if part in existing:
                continue
            base = f"{primary.id}-{part}" if primary.id else f"terminals-{part}"
            title = (
                f"Terminal strip {key} ({part})"
                if key
                else f"{primary.title or 'Terminals'} ({part})"
            )
            project.sheets.append(
                Sheet(
                    id=_unique_sheet_id(project, base),
                    title=title,
                    kind="terminal",
                    page=primary.page.model_copy(deep=True),
                    strip=key,
                    strip_part=part,
                )
            )
            existing.add(part)


def _load_strip(project: Project, catalog: Catalog, name: str, part: int | None) -> tuple[list[dict], list[dict]]:
    from panelcad.core.terminal_strip import layout_terminal_strip

    layout = layout_terminal_strip(project, catalog, name)
    items = []
    for item in layout.get("items") or []:
        if part is not None and int(item.get("part") or 1) != part:
            continue
        items.append(
            {
                "ref": str(item.get("ref") or ""),
                "kind": str(item.get("kind") or "terminal"),
                "pe": bool(item.get("pe")),
                "internal": str(item.get("internal") or ""),
                "external": str(item.get("external") or ""),
                "jumper": str(item.get("jumper") or ""),
            }
        )
    jumpers = []
    for jumper in layout.get("jumpers") or []:
        if part is not None and int(jumper.get("part") or 1) != part:
            continue
        refs = [str(ref) for ref in (jumper.get("refs") or []) if str(ref)]
        if len(refs) < 2:
            continue
        label = str(jumper.get("label") or jumper.get("id") or "")
        jumpers.append(
            {
                "id": str(jumper.get("id") or ""),
                "label": label,
                "refs": refs,
                "part_number": str(jumper.get("part_number") or ""),
                "catalog": str(jumper.get("catalog") or ""),
            }
        )
    return items, jumpers


def _collect_groups(project: Project, catalog: Catalog, sheet: Sheet) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for name, part in _assignments(project, catalog, sheet):
        items, jumpers = _load_strip(project, catalog, name, part)
        if not items:
            continue
        groups.append(
            {
                "strip": name,
                "part": part,
                "description": _description(project, catalog, name),
                "items": items,
                "jumpers": jumpers,
            }
        )
    return groups


def _group_title(name: str, part: int | None, description: str, *, continued: bool) -> str:
    """Strip name, plus a function only when the terminals themselves carry one."""

    head = f"{name} {description}".strip() if description else f"Terminal strip {name}"
    if part is not None and int(part) > 1:
        return f"{head} (continued {int(part)})"
    if continued:
        return f"{head} (continued)"
    return head


def _adv(text: str, size: float) -> float:
    if not text:
        return 0.0
    from panelcad.render.qt_painter import text_advance_mm

    return float(text_advance_mm(text, max(0.9, float(size))))


def _hrect(x: float, y: float, text: str, size: float, align: str) -> tuple[float, float, float, float]:
    size = max(0.9, float(size))
    width = _adv(text, size)
    top = y - size * 0.80
    height = size * 1.05
    if align == "middle":
        left = x - width / 2.0
    elif align == "end":
        left = x - width
    else:
        left = x
    return (left, top, width, height)


def _vrect(cx: float, cy: float, text: str, size: float) -> tuple[float, float, float, float]:
    size = max(0.9, float(size))
    length = _adv(text, size)
    ink = size * 1.05
    return (cx - ink / 2.0, cy - length / 2.0, ink, length)


def _wrap(text: str, size: float, max_w: float) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if current and _adv(trial, size) > max_w:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _legend_layout(scale: float, width: float) -> tuple[float, list[tuple[str, float]]]:
    size = max(1.35, LEGEND_SIZE * scale)
    max_w = max(12.0, width - 2.0)
    lines: list[str] = []
    for sentence in LEGEND:
        lines.extend(_wrap(sentence, size, max_w) or [sentence])
    guard = 0
    while len(lines) > 4 and size > 1.15 and guard < 8:
        guard += 1
        size = max(1.15, size - 0.1)
        lines = []
        for sentence in LEGEND:
            lines.extend(_wrap(sentence, size, max_w) or [sentence])
    return size, [(line, size) for line in lines]


def _wire_metrics(labels: list[str], scale: float, block_w: float) -> tuple[float, float]:
    size = max(0.9, WIRE_SIZE * scale)
    limit = max(1.2, block_w - 0.55)
    while size > 0.9 and size * 1.05 > limit:
        size = max(0.9, size - 0.05)
    longest = 0.0
    for label in labels:
        if label:
            longest = max(longest, _adv(label, size))
    cap = 18.0 * scale
    while longest > cap and size > 0.9:
        size = max(0.9, size - 0.05)
        longest = 0.0
        for label in labels:
            if label:
                longest = max(longest, _adv(label, size))
    band = max(5.2 * scale, longest + 1.15 * max(scale, 0.45))
    return size, band


def _tag_size(tags: list[str], scale: float, block_h: float, screw_clear: float, block_w: float) -> float:
    floor = MIN_TAG_MM if scale + 1e-6 >= min_readable_scale() else 0.9
    size = min(max(floor, TAG_SIZE * scale), max(floor, block_w * 0.62))
    room = max(floor, block_h - 2.0 * screw_clear - 0.8 * scale)
    longest = max(tags, key=len) if tags else "X"
    while size > floor + 1e-6 and (_adv(longest, size) > room or size * 1.05 > block_w - 0.45):
        size = max(floor, size - 0.05)
    return size


def _build(groups: list[dict[str, Any]], project: Project, sheet: Sheet, scale: float) -> list[dict[str, Any]]:
    left, top, width, height = working_rect(sheet)
    legend_size, legend_lines = _legend_layout(scale, width)
    legend_h = legend_size * 1.28 * max(1, len(legend_lines)) + 1.6
    side_size = max(1.2, SIDE_SIZE * scale)
    gutter = _adv("Internal", side_size) + 2.6
    x0 = left + gutter
    x1 = left + width - 1.0
    y0 = top + 1.0
    y1 = top + height - legend_h
    content_w = max(4.0, x1 - x0)
    bw = BLOCK_W * scale
    bh = BLOCK_H * scale
    screw_r = min(SCREW_R * scale, bw * 0.16)
    screw_inset = max(screw_r + 0.7 * scale, bh * 0.15)
    labels: list[str] = []
    tags: list[str] = []
    for group in groups:
        for item in group["items"]:
            tags.append(item["ref"])
            labels.append(item["internal"])
            labels.append(item["external"])
    wire_size, wire_band = _wire_metrics(labels, scale, bw)
    tag_size = _tag_size(tags, scale, bh, screw_inset + screw_r, bw)
    title_size = max(1.25, TITLE_SIZE * scale)
    title_gap = 1.35 * scale
    any_jumper = any(group["jumpers"] for group in groups)
    multi_jumper = any(len(group["jumpers"]) >= 2 for group in groups)
    jumper_size = max(MIN_TAG_MM, 2.05 * scale) if any_jumper else 0.0
    lanes = 2 if multi_jumper else 1
    jumper_band = (lanes * (jumper_size + 0.85 * scale)) if any_jumper else 0.0
    rail_h = RAIL_H * scale
    rail_lip = RAIL_LIP * scale
    rail_label_size = max(1.15, RAIL_LABEL * scale)
    rail_band = rail_lip * 2.0 + rail_h + rail_label_size + 1.15 * scale
    nmax = max(1, int(content_w / bw)) if bw > 0 else 1
    title_max_w = max(content_w, width - 2.0)

    segments: list[dict[str, Any]] = []
    for group in groups:
        items = group["items"]
        chunks = [items[index : index + nmax] for index in range(0, len(items), nmax)] or [[]]
        for index, chunk in enumerate(chunks):
            if not chunk:
                continue
            title = _group_title(
                group["strip"],
                group["part"],
                group["description"],
                continued=index > 0,
            )
            lines = _wrap(title, title_size, title_max_w) or [title]
            line_w = max((_adv(line, title_size) for line in lines), default=0.0)
            blocks_w = len(chunk) * bw
            box_w = max(blocks_w, min(line_w, title_max_w))
            title_lines_h = len(lines) * title_size * 1.15 + title_gap
            box_h = title_lines_h + jumper_band + wire_band + bh + wire_band + rail_band
            refs = {item["ref"] for item in chunk}
            jumpers = []
            for jumper in group["jumpers"]:
                hit = [ref for ref in jumper["refs"] if ref in refs]
                if len(hit) >= 2:
                    jumpers.append({**jumper, "refs": hit})
            segments.append(
                {
                    "strip": group["strip"],
                    "title": title,
                    "title_lines": lines,
                    "items": chunk,
                    "jumpers": jumpers,
                    "box_w": box_w,
                    "box_h": box_h,
                    "blocks_w": blocks_w,
                    "title_lines_h": title_lines_h,
                }
            )

    gap_x = GROUP_GAP_X * scale
    gap_y = GROUP_GAP_Y * scale
    pages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    row: list[dict[str, Any]] = []
    cursor = x0
    row_y = y0
    row_h = 0.0

    def finish_row() -> None:
        nonlocal row, cursor, row_y, row_h
        if not row:
            return
        current.extend(row)
        row_y += row_h + gap_y
        row = []
        cursor = x0
        row_h = 0.0

    for segment in segments:
        if row and cursor + segment["box_w"] > x1 + 0.3:
            finish_row()
        if (current or row) and row_y + segment["box_h"] > y1 + 0.3:
            finish_row()
            if current:
                pages.append(current)
                current = []
            row_y = y0
            cursor = x0
        segment["x"] = cursor
        segment["y"] = row_y
        row.append(segment)
        cursor += segment["box_w"] + gap_x
        row_h = max(row_h, segment["box_h"])
    finish_row()
    if current:
        pages.append(current)
    if not pages:
        return [_empty_scene(project, sheet, _empty_message(sheet))]

    rail_name = _rail_name(project)
    scenes = [
        _emit(
            placed,
            project,
            sheet,
            scale=scale,
            left=left,
            top=top,
            width=width,
            height=height,
            x0=x0,
            y0=y0,
            y1=y1,
            bw=bw,
            bh=bh,
            screw_r=screw_r,
            screw_inset=screw_inset,
            wire_size=wire_size,
            wire_band=wire_band,
            tag_size=tag_size,
            title_size=title_size,
            jumper_size=jumper_size,
            jumper_band=jumper_band,
            rail_h=rail_h,
            rail_lip=rail_lip,
            rail_label_size=rail_label_size,
            side_size=side_size,
            rail_name=rail_name,
            legend_lines=legend_lines,
            legend_size=legend_size,
            legend_h=legend_h,
        )
        for placed in pages
    ]
    return scenes


def _label(
    texts: list[dict[str, Any]],
    x: float,
    y: float,
    text: str,
    size: float,
    align: str,
    rotate: float,
    role: str,
    rect: tuple[float, float, float, float],
) -> None:
    if not str(text or "").strip():
        return
    texts.append(
        {
            "x": x,
            "y": y,
            "text": text,
            "size": size,
            "align": align,
            "rotate": rotate,
            "role": role,
            "rect": rect,
        }
    )


def _emit(placed: list[dict[str, Any]], project: Project, sheet: Sheet, **m: Any) -> dict[str, Any]:
    del project
    bw = float(m["bw"])
    bh = float(m["bh"])
    texts: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    rails: list[dict[str, Any]] = []
    jumpers: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    rows: dict[float, list[dict[str, Any]]] = {}
    for segment in placed:
        rows.setdefault(round(float(segment["y"]), 2), []).append(segment)

    for segment in placed:
        sx = float(segment["x"])
        sy = float(segment["y"])
        box_w = float(segment["box_w"])
        blocks_w = float(segment["blocks_w"])
        bx0 = sx + max(0.0, (box_w - blocks_w) / 2.0)
        title_h = float(segment["title_lines_h"])
        cursor_y = sy
        for line_index, line in enumerate(segment["title_lines"]):
            baseline = cursor_y + m["title_size"] * (0.90 + line_index * 1.15)
            _label(
                texts,
                sx + box_w / 2.0,
                baseline,
                line,
                m["title_size"],
                "middle",
                0.0,
                "title",
                _hrect(sx + box_w / 2.0, baseline, line, m["title_size"], "middle"),
            )
        cursor_y += title_h
        jumper_top = cursor_y
        cursor_y += float(m["jumper_band"])
        internal_top = cursor_y
        internal_mid = internal_top + float(m["wire_band"]) / 2.0
        cursor_y += float(m["wire_band"])
        block_top = cursor_y
        cursor_y += bh
        external_top = cursor_y
        external_mid = external_top + float(m["wire_band"]) / 2.0
        cursor_y += float(m["wire_band"])
        rail_top = cursor_y
        by_ref: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(segment["items"]):
            x = bx0 + index * bw
            screws: list[tuple[float, float, float]] = []
            radius = float(m["screw_r"])
            inset = float(m["screw_inset"])
            if item["kind"] == "terminal":
                cx = x + bw / 2.0
                screws = [(cx, block_top + inset, radius), (cx, block_top + bh - inset, radius)]
            ground = (
                _ground_lines(x, block_top, bw, bh, m["scale"], inset, radius) if item["pe"] else []
            )
            marks = _stop_lines(x, block_top, bw, bh, m["scale"]) if item["kind"] == "end_stop" else []
            block = {
                "ref": item["ref"],
                "kind": item["kind"],
                "strip": segment["strip"],
                "pe": item["pe"],
                "x": x,
                "y": block_top,
                "w": bw,
                "h": bh,
                "fill": PE_FILL if item["pe"] else KIND_FILL.get(item["kind"], TERMINAL_FILL),
                "internal": item["internal"],
                "external": item["external"],
                "jumper": item["jumper"],
                "screws": screws,
                "ground": ground,
                "marks": marks,
            }
            blocks.append(block)
            by_ref[item["ref"]] = block
            cx = x + bw / 2.0
            word = _KIND_WORD.get(item["kind"], "")
            tag_cy = block_top + bh / 2.0
            if item["pe"] and ground:
                top_mark = min(line[1] for line in ground)
                half = _adv(item["ref"], m["tag_size"]) / 2.0
                tag_cy = min(tag_cy, top_mark - half - 0.45 * m["scale"])
            elif word:
                tag_cy = block_top + bh * 0.36
            if item["internal"]:
                _label(
                    texts,
                    cx,
                    internal_mid,
                    item["internal"],
                    m["wire_size"],
                    "middle",
                    -90.0,
                    "panel",
                    _vrect(cx, internal_mid, item["internal"], m["wire_size"]),
                )
            if item["external"]:
                _label(
                    texts,
                    cx,
                    external_mid,
                    item["external"],
                    m["wire_size"],
                    "middle",
                    -90.0,
                    "field",
                    _vrect(cx, external_mid, item["external"], m["wire_size"]),
                )
            _label(
                texts,
                cx,
                tag_cy,
                item["ref"],
                m["tag_size"],
                "middle",
                -90.0,
                "tag",
                _vrect(cx, tag_cy, item["ref"], m["tag_size"]),
            )
            if word:
                word_size = min(float(m["tag_size"]), max(MIN_TAG_MM, 2.0 * m["scale"]))
                word_cy = block_top + bh * 0.72
                word_rect = _vrect(cx, word_cy, word, word_size)
                tag_rect = _vrect(cx, tag_cy, item["ref"], m["tag_size"])
                guard = 0
                while guard < 8 and rects_overlap(word_rect, tag_rect, gap=0.3):
                    guard += 1
                    word_size = max(1.2, word_size - 0.1)
                    word_cy = min(block_top + bh - 1.5, word_cy + 0.4)
                    word_rect = _vrect(cx, word_cy, word, word_size)
                if not rects_overlap(word_rect, tag_rect, gap=0.2):
                    _label(texts, cx, word_cy, word, word_size, "middle", -90.0, "mark", word_rect)
        pending_jumpers: list[dict[str, Any]] = []
        for jumper in segment["jumpers"]:
            members = [by_ref[ref] for ref in jumper["refs"] if ref in by_ref]
            if len(members) < 2:
                continue
            x1 = members[0]["x"] + bw / 2.0
            x2 = members[-1]["x"] + bw / 2.0
            bar_y = members[0]["screws"][0][1] if members[0]["screws"] else block_top + float(m["screw_inset"])
            label = str(jumper.get("label") or jumper.get("id") or "")
            size = float(m["jumper_size"]) or MIN_TAG_MM
            span = max(4.0, abs(x2 - x1) + bw)
            floor = MIN_TAG_MM if m["scale"] + 1e-6 >= min_readable_scale() else 1.15
            while size > floor + 1e-6 and _adv(label, size) > span - 0.4:
                size = max(floor, size - 0.05)
            cx = (min(x1, x2) + max(x1, x2)) / 2.0
            baseline = jumper_top + float(m["jumper_band"]) - 0.45 * m["scale"]
            ticks = [float(member["x"]) + bw / 2.0 for member in members]
            record = {
                "id": str(jumper.get("id") or ""),
                "label": label,
                "x1": x1,
                "x2": x2,
                "y": bar_y,
                "ticks": ticks,
                "tick": max(2.2, 4.0 * m["scale"]),
                "part_number": str(jumper.get("part_number") or ""),
                "catalog": str(jumper.get("catalog") or ""),
                "refs": [member["ref"] for member in members],
                "label_x": cx,
                "label_y": baseline,
                "label_size": size,
            }
            jumpers.append(record)
            before = len(texts)
            _label(texts, cx, baseline, label, size, "middle", 0.0, "jumper", _hrect(cx, baseline, label, size, "middle"))
            if len(texts) > before:
                texts[-1]["jumper_index"] = len(jumpers) - 1
                pending_jumpers.append(texts[-1])
        _spread_jumper_labels(pending_jumpers, jumpers)
        lip = float(m["rail_lip"])
        body = float(m["rail_h"])
        extend = min(0.45 * m["scale"], 0.8)
        rail_x = max(m["x0"] - 0.2, bx0 - extend)
        rail_w = min(blocks_w + extend * 2.0, (sx + box_w) - rail_x)
        rail = {
            "x": rail_x,
            "y": rail_top,
            "w": max(bw, rail_w),
            "h": body,
            "lip": lip,
            "strip": segment["strip"],
            "label": m["rail_name"],
        }
        rails.append(rail)
        label_y = rail_top + lip * 2.0 + body + float(m["rail_label_size"]) * 0.95
        _label(
            texts,
            rail_x + rail["w"] / 2.0,
            label_y,
            m["rail_name"],
            m["rail_label_size"],
            "middle",
            0.0,
            "rail",
            _hrect(rail_x + rail["w"] / 2.0, label_y, m["rail_name"], m["rail_label_size"], "middle"),
        )
        groups.append(
            {
                "strip": segment["strip"],
                "title": segment["title"],
                "block_refs": [item["ref"] for item in segment["items"]],
                "x": sx,
                "y": sy,
                "w": box_w,
                "h": float(segment["box_h"]),
                "rail": (rail["x"], rail["y"], rail["w"], rail["h"]),
                "internal_mid": internal_mid,
                "external_mid": external_mid,
            }
        )

    for row in rows.values():
        first = min(row, key=lambda segment: float(segment["x"]))
        # Band centres were stored on the emitted group with the same y.
        match = next(group for group in groups if group["y"] == first["y"] and group["x"] == first["x"])
        side_x = float(m["x0"]) - 1.15 * max(m["scale"], 0.4)
        _label(
            texts,
            side_x,
            _side_baseline(match["internal_mid"], m["side_size"]),
            "Internal",
            m["side_size"],
            "end",
            0.0,
            "side",
            _hrect(side_x, _side_baseline(match["internal_mid"], m["side_size"]), "Internal", m["side_size"], "end"),
        )
        _label(
            texts,
            side_x,
            _side_baseline(match["external_mid"], m["side_size"]),
            "Field",
            m["side_size"],
            "end",
            0.0,
            "side",
            _hrect(side_x, _side_baseline(match["external_mid"], m["side_size"]), "Field", m["side_size"], "end"),
        )

    legend_top = float(m["top"]) + float(m["height"]) - float(m["legend_h"])
    for index, (line, size) in enumerate(m["legend_lines"]):
        baseline = legend_top + size * (0.92 + index * 1.28)
        _label(
            texts,
            float(m["left"]) + 1.0,
            baseline,
            line,
            size,
            "start",
            0.0,
            "legend",
            _hrect(float(m["left"]) + 1.0, baseline, line, size, "start"),
        )

    ink = [label["rect"] for label in texts] + [
        (block["x"], block["y"], block["w"], block["h"]) for block in blocks
    ]
    if ink:
        min_x = min(rect[0] for rect in ink)
        min_y = min(rect[1] for rect in ink)
        max_x = max(rect[0] + rect[2] for rect in ink)
        max_y = max(rect[1] + rect[3] for rect in ink)
    else:
        min_x, min_y, max_x, max_y = m["left"], m["top"], m["left"] + 10.0, m["top"] + 10.0
    return {
        "scale": m["scale"],
        "blocks": blocks,
        "texts": texts,
        "rails": rails,
        "jumpers": jumpers,
        "groups": groups,
        "legend": [line for line, _size in m["legend_lines"]],
        "rail_name": m["rail_name"],
        "bounds": (min_x - 1.0, min_y - 1.0, max_x - min_x + 2.0, max_y - min_y + 2.0),
        "sheet_id": sheet.id,
        "tag_size": float(m["tag_size"]),
    }


def _ground_lines(
    x: float,
    y: float,
    w: float,
    h: float,
    scale: float,
    screw_inset: float,
    screw_r: float,
) -> list[tuple[float, float, float, float]]:
    """IEC earth mark: three bars, longest on top, just above the lower screw."""

    mid = x + w / 2.0
    step = max(1.5, 2.2 * scale)
    longest = min(w * 0.36, 3.2 * scale)
    middle = min(w * 0.24, 2.2 * scale)
    short = min(w * 0.14, 1.25 * scale)
    lowest = y + h - screw_inset - screw_r - 1.0 * scale
    lines: list[tuple[float, float, float, float]] = []
    for width, yy in ((longest, lowest - 2 * step), (middle, lowest - step), (short, lowest)):
        lines.append((mid - width, yy, mid + width, yy))
    return lines


def _stop_lines(
    x: float, y: float, w: float, h: float, scale: float
) -> list[tuple[float, float, float, float]]:
    """Two clamp bars on an end stop."""

    pad = max(2.4 * scale, h * 0.08)
    return [
        (x + w * 0.35, y + pad, x + w * 0.35, y + h - pad),
        (x + w * 0.65, y + pad, x + w * 0.65, y + h - pad),
    ]


def _spread_jumper_labels(labels: list[dict[str, Any]], jumpers: list[dict[str, Any]]) -> None:
    """Move a label up a lane when it would sit on the jumper beside it."""

    ordered = sorted(labels, key=lambda item: (float(item["rect"][0]), float(item["y"])))
    for index, label in enumerate(ordered):
        guard = 0
        while guard < 5 and any(
            rects_overlap(label["rect"], other["rect"], gap=0.45) for other in ordered[:index]
        ):
            guard += 1
            shift = float(label["size"]) + 0.75
            label["y"] = float(label["y"]) - shift
            rect = label["rect"]
            label["rect"] = (rect[0], rect[1] - shift, rect[2], rect[3])
        slot = label.get("jumper_index")
        if isinstance(slot, int) and 0 <= slot < len(jumpers):
            jumpers[slot]["label_x"] = float(label["x"])
            jumpers[slot]["label_y"] = float(label["y"])
            jumpers[slot]["label_size"] = float(label["size"])


def _side_baseline(center: float, size: float) -> float:
    """Baseline whose ink box is centred on the wire band."""

    return center + size * 0.28


def _pages_fit(pages: list[dict[str, Any]], sheet: Sheet) -> bool:
    left, top, width, height = working_rect(sheet)
    right = left + width
    bottom = top + height
    for scene in pages:
        for label in scene.get("texts") or []:
            x, y, w, h = label["rect"]
            if x < left - 0.6 or y < top - 0.6 or x + w > right + 0.6 or y + h > bottom + 0.6:
                return False
        for block in scene.get("blocks") or []:
            if block["x"] < left - 0.6 or block["y"] < top - 0.6:
                return False
            if block["x"] + block["w"] > right + 0.6 or block["y"] + block["h"] > bottom + 0.8:
                return False
        for rail in scene.get("rails") or []:
            if rail["y"] + rail["h"] + rail.get("lip", 0.0) * 2.0 > bottom + 0.8:
                return False
    return True


def _empty_scene(project: Project, sheet: Sheet, message: str) -> dict[str, Any]:
    left, top, width, height = working_rect(sheet)
    texts: list[dict[str, Any]] = []
    _label(texts, left + 2.0, top + 8.0, "Terminal strip", 4.2, "start", 0.0, "title", _hrect(left + 2.0, top + 8.0, "Terminal strip", 4.2, "start"))
    _label(texts, left + 2.0, top + 16.0, message, 2.6, "start", 0.0, "note", _hrect(left + 2.0, top + 16.0, message, 2.6, "start"))
    legend_size, legend_lines = _legend_layout(1.0, width)
    legend_h = legend_size * 1.28 * max(1, len(legend_lines)) + 1.6
    legend_top = top + height - legend_h
    for index, (line, size) in enumerate(legend_lines):
        baseline = legend_top + size * (0.92 + index * 1.28)
        _label(
            texts,
            left + 1.0,
            baseline,
            line,
            size,
            "start",
            0.0,
            "legend",
            _hrect(left + 1.0, baseline, line, size, "start"),
        )
    return {
        "scale": 1.0,
        "blocks": [],
        "texts": texts,
        "rails": [],
        "jumpers": [],
        "groups": [],
        "legend": [line for line, _size in legend_lines],
        "rail_name": _rail_name(project),
        "bounds": (left, top, width, height),
        "sheet_id": sheet.id,
    }


def _ink_rail(painter: Painter, rail: dict[str, Any]) -> None:
    x, y, w, h = float(rail["x"]), float(rail["y"]), float(rail["w"]), float(rail["h"])
    lip = float(rail.get("lip") or 1.0)
    painter.set_fill(RAIL_FILL)
    painter.set_stroke(RAIL_EDGE, 0.4)
    painter.rect(x, y, w, h)
    painter.rect(x, y - lip, w, lip)
    painter.rect(x, y + h, w, lip)
    painter.set_fill(None)


def _ink_block(painter: Painter, block: dict[str, Any]) -> None:
    painter.set_fill(str(block.get("fill") or TERMINAL_FILL))
    painter.set_stroke(INK, 0.55 if block.get("kind") == "end_stop" else 0.45)
    painter.rect(float(block["x"]), float(block["y"]), float(block["w"]), float(block["h"]))
    if block.get("marks"):
        painter.set_fill(None)
        painter.set_stroke(STOP_MARK, 0.7)
        for x1, y1, x2, y2 in block["marks"]:
            painter.line(float(x1), float(y1), float(x2), float(y2))
    if block.get("ground"):
        painter.set_fill(None)
        painter.set_stroke(PE_MARK, 0.45)
        for x1, y1, x2, y2 in block["ground"]:
            painter.line(float(x1), float(y1), float(x2), float(y2))
    painter.set_fill(SCREW)
    painter.set_stroke(SCREW, 0.25)
    for cx, cy, radius in block.get("screws") or []:
        painter.circle(float(cx), float(cy), float(radius))
    painter.set_fill(None)
    painter.set_stroke(INK, 0.3)


def _ink_jumper(painter: Painter, jumper: dict[str, Any]) -> None:
    painter.set_stroke(JUMPER, 0.85)
    painter.set_fill(None)
    y = float(jumper["y"])
    painter.line(float(jumper["x1"]), y, float(jumper["x2"]), y)
    tick = float(jumper.get("tick") or 4.0)
    for x in jumper.get("ticks") or []:
        painter.line(float(x), y, float(x), y + tick)


def _ink_text(painter: Painter, label: dict[str, Any]) -> None:
    painter.set_stroke(INK, 0.2)
    painter.text(
        float(label["x"]),
        float(label["y"]),
        str(label.get("text") or ""),
        float(label["size"]),
        align=str(label.get("align") or "middle"),
        rotate=float(label.get("rotate") or 0.0),
    )
