"""Sheet frame: zone coordinates + bottom information boxes.

Shared by the GUI canvas and SVG/PDF/DXF export so prints match the editor.
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from panelcad.core.models import Project, Revision, Sheet, TitleBlock
from panelcad.render.painter import Painter
from panelcad.version import __version__

log = logging.getLogger("panelcad.titleblock")

# One nameplate lookup for every sheet in a print or PDF pass. Cleared when the pass ends.
_nameplate_cache: ContextVar[dict[int, list[str]] | None] = ContextVar(
    "panelcad_title_nameplate", default=None
)

MARGIN = 8.0
ZONE = 7.0
TITLE_H = 40.0
INK = "#1a1a1a"
RULE = "#2a2a2a"
FILL = "#f7f4ea"
CELL = "#fbfaf3"
HEADER = "#ece8dc"
LOGO_W = 34.0
LOGO_PAD = 1.8


def sheet_scale_label(sheet: Sheet, tb: TitleBlock) -> str:
    """SCALE cell. A laid-out enclosure uses its draw ratio; other sheets use the title block."""

    enc = sheet.enclosure
    if enc is not None and float(enc.draw_scale or 0) > 0 and (enc.width_mm > 1 or enc.height_mm > 1):
        from panelcad.core.layout import format_draw_ratio

        return format_draw_ratio(float(enc.draw_scale))
    return tb.scale or "NTS"


def title_block_rect(sheet: Sheet) -> tuple[float, float, float, float]:
    """Bottom information strip only (not the whole page)."""
    pw, ph = sheet.page.dimensions()
    return (MARGIN, ph - MARGIN - TITLE_H, pw - 2 * MARGIN, TITLE_H)


def body_hits_title_block(sheet: Sheet, x: float, y: float, width: float, height: float) -> bool:
    """True when a symbol body covers the title-block strip."""

    if width <= 0 or height <= 0:
        return False
    tx, ty, tw, th = title_block_rect(sheet)
    return x < tx + tw - 0.2 and x + width > tx + 0.2 and y < ty + th - 0.2 and y + height > ty + 0.2


# Schematic and I/O sheets snap to this grid. A lift has to land on it too.
SCHEMATIC_SNAP_MM = 2.5


def _snap_down(value: float, step: float = SCHEMATIC_SNAP_MM) -> float:
    """Next grid line at or below ``value``. Smaller y sits further above the title block."""

    if step <= 0:
        return value
    return math.floor((float(value) + 1e-9) / step) * step


def lift_off_title_block(
    sheet: Sheet, x: float, y: float, width: float, height: float
) -> tuple[float, float]:
    """Move a symbol up so its body sits above the title block, on the snap grid."""

    if not body_hits_title_block(sheet, x, y, width, height):
        return (x, y)
    _tx, ty, _tw, _th = title_block_rect(sheet)
    lifted = ty - height - 0.6
    return (x, _snap_down(lifted))


def working_rect(sheet: Sheet) -> tuple[float, float, float, float]:
    """Drawable schematic area above the title block, inside the zone band."""
    pw, ph = sheet.page.dimensions()
    x = MARGIN + ZONE
    y = MARGIN + ZONE
    w = pw - 2 * MARGIN - 2 * ZONE
    h = ph - 2 * MARGIN - ZONE - TITLE_H
    return (x, y, w, h)


def company_logo_rect(sheet: Sheet) -> tuple[float, float, float, float]:
    """Logo cell inside the left title-block company box (mm)."""
    _pw, ph = sheet.page.dimensions()
    x = MARGIN + LOGO_PAD
    y = ph - MARGIN - TITLE_H + LOGO_PAD
    h = TITLE_H - 2 * LOGO_PAD
    return (x, y, LOGO_W, h)


def resolve_logo(logo: str, base_dir: Path | None) -> Path | None:
    if not logo:
        return None
    raw = Path(logo)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        if base_dir is not None:
            candidates.append(base_dir / raw)
        candidates.append(Path.cwd() / raw)
    for path in candidates:
        if path.is_file():
            return path
    return None


def draw_sheet_chrome(
    painter: Painter,
    sheet: Sheet,
    *,
    drawing_style: str = "",
    catalog=None,
    project: Project | None = None,
    sheet_index: int = 0,
) -> None:
    """Border and zone ticks only — sits behind devices."""
    pw, ph = sheet.page.dimensions()
    painter.set_layer("OUTLINE")
    painter.set_stroke(INK, 0.45)
    painter.set_fill(None)
    painter.rect(MARGIN, MARGIN, pw - 2 * MARGIN, ph - 2 * MARGIN)
    wx, wy, ww, wh = working_rect(sheet)
    painter.set_stroke("#888888", 0.2)
    painter.rect(wx, wy, ww, wh)
    nema_lines = False
    if project is not None and sheet.kind in {"schematic", "io"}:
        from panelcad.core.xref import xref_style

        nema_lines = xref_style(project, sheet) == "nema"
    _draw_zones(painter, wx, wy, ww, wh)
    if drawing_style == "ladder" and sheet.kind == "schematic":
        draw_ladder_rails(painter, sheet, project)
        if catalog is not None:
            draw_ladder_rung_lines(painter, sheet, catalog)
    elif drawing_style == "ladder" and sheet.kind == "io":
        # Generated I/O sheets use the ladder rails. Rung ticks stay off; the module sets the pitch.
        draw_ladder_rails(painter, sheet, project)
    if drawing_style == "iec" and sheet.kind == "schematic":
        draw_iec_rails(painter, sheet, project)
    if nema_lines and project is not None:
        draw_nema_rung_numbers(painter, project, sheet, catalog, sheet_no=sheet_index + 1)


def draw_ladder_rails(painter: Painter, sheet: Sheet, project: Project | None = None) -> None:
    """Hot (left) and return (right) rails. Labels follow the sheet, not a fixed L1/L2."""

    from panelcad.core.ladder import RAIL_INSET, ladder_rail_ys, rail_names

    wx, _wy, ww, _wh = working_rect(sheet)
    left = wx + RAIL_INSET
    right = wx + ww - RAIL_INSET
    top, bot = ladder_rail_ys(sheet, project)
    hot, ret = rail_names(sheet, project)
    painter.set_stroke(INK, 1.35)
    painter.set_fill(None)
    painter.line(left, top, left, bot)
    painter.line(right, top, right, bot)
    painter.set_fill(INK)
    painter.text(left, top - 1.6, hot, 3.2, align="middle")
    painter.text(right, top - 1.6, ret, 3.2, align="middle")
    painter.set_fill(None)


def draw_iec_rails(painter: Painter, sheet: Sheet, project: Project | None = None) -> None:
    """Hot rail across the top, return rail across the bottom."""

    from panelcad.core.ladder import RAIL_INSET, iec_rail_ys, rail_names

    wx, _wy, ww, _wh = working_rect(sheet)
    left = wx + RAIL_INSET
    right = wx + ww - RAIL_INSET
    top, bot = iec_rail_ys(sheet)
    hot, ret = rail_names(sheet, project)
    painter.set_stroke(INK, 1.35)
    painter.set_fill(None)
    painter.line(left, top, right, top)
    painter.line(left, bot, right, bot)
    painter.set_fill(INK)
    painter.text(left + 1.2, top - 1.2, hot, 3.2, align="start")
    painter.text(left + 1.2, bot + 3.4, ret, 3.2, align="start")
    painter.set_fill(None)


def draw_ladder_rung_lines(painter: Painter, sheet: Sheet, catalog) -> None:
    """No-op. Rungs are the wires (pin to pin). Do not paint a bus through symbols."""

    return


def draw_title_block_only(
    painter: Painter,
    project: Project,
    sheet: Sheet,
    sheet_index: int = 0,
    sheet_count: int = 1,
    base_dir: Path | None = None,
    catalog=None,
) -> None:
    pw, ph = sheet.page.dimensions()
    _draw_title_block(painter, project, sheet, sheet_index, sheet_count, pw, ph, base_dir, catalog)


def draw_sheet_frame(
    painter: Painter,
    project: Project,
    sheet: Sheet,
    sheet_index: int = 0,
    sheet_count: int = 1,
    base_dir: Path | None = None,
    catalog=None,
) -> None:
    from panelcad.core.ladder import sheet_drawing_style

    style = sheet_drawing_style(sheet, project)
    draw_sheet_chrome(
        painter,
        sheet,
        drawing_style=style,
        catalog=catalog,
        project=project,
        sheet_index=sheet_index,
    )
    draw_title_block_only(
        painter, project, sheet, sheet_index, sheet_count, base_dir, catalog=catalog
    )


def _draw_zones(
    painter: Painter,
    wx: float,
    wy: float,
    ww: float,
    wh: float,
) -> None:
    from panelcad.core.zones import zone_axes

    cols, rows, cw, rh = zone_axes(ww, wh)
    painter.set_layer("OUTLINE")
    painter.set_stroke("#b5b09f", 0.18)
    painter.set_fill(None)
    for i in range(1, cols):
        x = wx + i * cw
        painter.line(x, wy - ZONE, x, wy)
        painter.line(x, wy + wh, x, wy + wh + ZONE)
    for j in range(1, rows):
        y = wy + j * rh
        painter.line(wx - ZONE, y, wx, y)
        painter.line(wx + ww, y, wx + ww + ZONE, y)

    painter.set_layer("TITLEBLOCK")
    for i in range(cols):
        label = str(i + 1)
        cx = wx + (i + 0.5) * cw
        painter.text(cx, wy - 2.2, label, 2.2, align="middle")
        painter.text(cx, wy + wh + 4.8, label, 2.2, align="middle")
    for j in range(rows):
        letter = chr(ord("A") + j)
        cy = wy + (j + 0.5) * rh + 1.0
        painter.text(wx - ZONE / 2, cy, letter, 2.2, align="middle")
        painter.text(wx + ww + ZONE / 2, cy, letter, 2.2, align="middle")


def _io_sheet_rung_ys(sheet: Sheet) -> list[float] | None:
    """Signal-pin Ys on a generated I/O ladder, so the line numbers sit on the rungs."""

    if getattr(sheet, "kind", "") != "io" or getattr(sheet, "drawing_style", "") != "ladder":
        return None
    if not any(
        comp.properties.get("io_module") or comp.properties.get("io_continuation") for comp in sheet.components
    ):
        return None
    ys: set[float] = set()
    for comp in sheet.components:
        if not comp.properties.get("ladder_card"):
            continue
        for ov in comp.pin_overrides.values():
            if getattr(ov, "side", "") in {"left", "right"} and float(ov.y) >= 20.0:
                ys.add(round(float(comp.y) + float(ov.y), 1))
    return sorted(ys)


def draw_nema_rung_numbers(
    painter: Painter,
    project: Project,
    sheet: Sheet,
    catalog=None,
    *,
    sheet_no: int = 1,
) -> None:
    """Line numbers at the left rail: sheet*100 + rung, at 0.5 in pitch."""

    from panelcad.core.ladder import RAIL_INSET
    from panelcad.core.xref import occupied_rung_indices, resolve_settings
    from panelcad.core.zones import nema_line_from_index, rung_pitch_mm, rung_y, zone_grid

    settings = resolve_settings(project)
    pitch = rung_pitch_mm(project)
    grid = zone_grid(sheet)
    left = grid.wx + RAIL_INSET
    io_ys = _io_sheet_rung_ys(sheet)
    if io_ys is not None:
        painter.set_layer("TITLEBLOCK")
        painter.set_fill(INK)
        for index, yy in enumerate(io_ys, start=1):
            line = sheet_no * 100 + index
            painter.text(left - 2.1, yy + 0.7, str(line), 2.0, align="end")
        painter.set_fill(None)
        return
    if str(getattr(settings, "rung_numbers", "used")) == "all":
        indexes = list(range(1, grid.max_rung_index(pitch) + 1))
    else:
        indexes = sorted(occupied_rung_indices(project, sheet, catalog))
    painter.set_layer("TITLEBLOCK")
    painter.set_fill(INK)
    for index in indexes:
        line = nema_line_from_index(sheet_no, index)
        yy = rung_y(sheet, index, pitch=pitch)
        painter.text(left - 2.1, yy + 0.7, str(line), 2.0, align="end")
    painter.set_fill(None)


def _draw_title_block(
    painter: Painter,
    project: Project,
    sheet: Sheet,
    sheet_index: int,
    sheet_count: int,
    pw: float,
    ph: float,
    base_dir: Path | None = None,
    catalog=None,
) -> None:
    tb: TitleBlock = project.title_block
    x = MARGIN
    y = ph - MARGIN - TITLE_H
    w = pw - 2 * MARGIN
    h = TITLE_H

    painter.set_layer("TITLEBLOCK")
    painter.set_stroke(RULE, 0.4)
    painter.set_fill(FILL)
    painter.rect(x, y, w, h)

    company_w = 92.0
    meta_w = 168.0
    center_w = w - company_w - meta_w
    _company_box(painter, x, y, company_w, h, tb, sheet, base_dir, _nameplate_lines(project, catalog))
    _center_box(painter, x + company_w, y, center_w, h, project, sheet, tb)
    _meta_box(painter, x + company_w + center_w, y, meta_w, h, sheet, tb, sheet_index, sheet_count)


@contextmanager
def share_title_nameplate():
    """Reuse one title-block nameplate lookup for every sheet in a paint pass."""

    if _nameplate_cache.get() is not None:
        yield
        return
    token = _nameplate_cache.set({})
    try:
        yield
    finally:
        _nameplate_cache.reset(token)


def _nameplate_lines(project: Project, catalog) -> list[str]:
    if catalog is None:
        return []
    cache = _nameplate_cache.get()
    key = id(project)
    if cache is not None and key in cache:
        return cache[key]
    lines = _load_nameplate_lines(project, catalog)
    if cache is not None:
        cache[key] = lines
    return lines


def _load_nameplate_lines(project: Project, catalog) -> list[str]:
    """Nameplate text for the title block. A build failure leaves the block blank."""

    try:
        from panelcad.electrical.nameplate import title_block_lines

        return [line for line in title_block_lines(project, catalog) if line][:2]
    except Exception:
        log.debug("title-block nameplate lines skipped", exc_info=True)
        return []


def _company_box(
    painter: Painter,
    x: float,
    y: float,
    w: float,
    h: float,
    tb: TitleBlock,
    sheet: Sheet,
    base_dir: Path | None,
    nameplate_lines: list[str] | None = None,
) -> None:
    _outline(painter, x, y, w, h, FILL)
    lx, ly, lw, lh = company_logo_rect(sheet)
    logo_path = resolve_logo(tb.logo, base_dir)
    painter.set_stroke("#8a8678", 0.25)
    painter.set_fill(CELL)
    painter.rect(lx, ly, lw, lh)
    painter.set_fill(None)
    if logo_path is not None:
        painter.image(str(logo_path), lx + 0.8, ly + 0.8, lw - 1.6, lh - 1.6)
    else:
        painter.text(lx + lw / 2.0, ly + lh / 2.0 + 1.2, "LOGO", 2.0, align="middle")
    tx = x + LOGO_PAD + LOGO_W + 2.5
    painter.text(tx, y + 4.8, "COMPANY", 2.0, align="start")
    painter.text(tx, y + 13.2, tb.company or "", 3.4, align="start")
    painter.text(tx, y + 20.2, tb.address or "", 2.2, align="start")
    text_w = max(12.0, w - (tx - x) - 1.6)
    lines = [line for line in (nameplate_lines or []) if line][:2]
    if len(lines) == 1:
        painter.text(tx, y + h - 8.4, _ellipsize(lines[0], text_w, 1.55), 1.55, align="start")
    elif len(lines) == 2:
        painter.text(tx, y + h - 12.0, _ellipsize(lines[0], text_w, 1.5), 1.5, align="start")
        painter.text(tx, y + h - 8.2, _ellipsize(lines[1], text_w, 1.5), 1.5, align="start")
    painter.text(tx, y + h - 3.8, f"PanelCAD {__version__}", 1.8, align="start")


def _center_box(
    painter: Painter,
    x: float,
    y: float,
    w: float,
    h: float,
    project: Project,
    sheet: Sheet,
    tb: TitleBlock,
) -> None:
    _outline(painter, x, y, w, h, FILL)
    row = h / 5.0
    _info_row(painter, x, y + 0 * row, w, row, "PROJECT", project.name)
    _info_row(painter, x, y + 1 * row, w, row, "TITLE", sheet.title or sheet.id)
    _info_row(painter, x, y + 2 * row, w, row, "DESCRIPTION", sheet.description)
    half = w / 2.0
    _info_row(painter, x, y + 3 * row, half, row, "DWG No.", tb.drawing_number)
    _info_row(painter, x + half, y + 3 * row, half, row, "STANDARD", tb.standard)
    _info_row(painter, x, y + 4 * row, half, row, "CUSTOMER", tb.customer)
    _info_row(painter, x + half, y + 4 * row, half, row, "LOCATION", tb.location)


def _meta_box(
    painter: Painter,
    x: float,
    y: float,
    w: float,
    h: float,
    sheet: Sheet,
    tb: TitleBlock,
    sheet_index: int,
    sheet_count: int,
) -> None:
    _outline(painter, x, y, w, h, FILL)
    table_h = 24.0
    _revision_table(painter, x, y, w, table_h, tb)
    by = y + table_h
    bh = h - table_h
    page = sheet.page
    fmt = f"{page.size.upper()} {page.orientation[:4].upper()}"
    third = w / 3.0
    half_h = bh / 2.0
    _info_row(painter, x, by, third, half_h, "SHEET", f"{sheet_index + 1} / {sheet_count}")
    _info_row(painter, x + third, by, third, half_h, "SIZE", fmt)
    _info_row(painter, x + 2 * third, by, third, half_h, "SCALE", sheet_scale_label(sheet, tb))
    _info_row(painter, x, by + half_h, third, half_h, "DRAWN", tb.drawn_by)
    _info_row(painter, x + third, by + half_h, third, half_h, "CHECKED", tb.checked_by)
    _info_row(painter, x + 2 * third, by + half_h, third, half_h, "APPROVED", tb.approved_by)


def draw_revision_table(
    painter: Painter, x: float, y: float, w: float, h: float, tb: TitleBlock
) -> None:
    """REV / DATE / DESCRIPTION / DRAWN / CHK / APP. Same cells as the sheet title block."""

    _revision_table(painter, x, y, w, h, tb)


def _revision_table(painter: Painter, x: float, y: float, w: float, h: float, tb: TitleBlock) -> None:
    cols = [("REV", 16.0), ("DATE", 28.0), ("DESCRIPTION", 0.0), ("DRAWN", 26.0), ("CHK", 22.0), ("APP", 22.0)]
    fixed = sum(c[1] for c in cols)
    desc_w = w - fixed
    widths = [desc_w if name == "DESCRIPTION" else ww for name, ww in cols]
    row_h = h / 4.0
    headers = [c[0] for c in cols]
    _row_cells(painter, x, y, widths, row_h, headers, header=True)
    revs = list(tb.revisions[:3])
    while len(revs) < 3:
        revs.append(Revision())
    for i, rev in enumerate(revs):
        values = [rev.rev, rev.date, rev.description, rev.drawn, rev.checked, rev.approved]
        _row_cells(painter, x, y + (i + 1) * row_h, widths, row_h, values, header=False)


def _ellipsize(text: str, width_mm: float, size_mm: float) -> str:
    """Keep title-block copy inside its cell (approx. Arial advance)."""
    raw = (text or "").replace("\n", " ").strip()
    if not raw:
        return ""
    n = max(3, int(width_mm / max(size_mm, 1.0) / 0.52))
    if len(raw) <= n:
        return raw
    return raw[: max(1, n - 1)] + "…"


def _row_cells(
    painter: Painter,
    x: float,
    y: float,
    widths: list[float],
    h: float,
    values: list[str],
    header: bool,
) -> None:
    cx = x
    for w, val in zip(widths, values, strict=True):
        _outline(painter, cx, y, w, h, HEADER if header else CELL)
        size = 2.0 if header else 2.3
        painter.text(cx + w / 2.0, y + h / 2.0 + 1.1, _ellipsize(val or "", w - 2.0, size), size, align="middle")
        cx += w


def _info_row(painter: Painter, x: float, y: float, w: float, h: float, label: str, value: str) -> None:
    _outline(painter, x, y, w, h, CELL)
    # Label near the top of the cell; value near the bottom. Keep a clear gap so
    # AutoCAD TEXT (grows +Y from the baseline) does not sit on the next label.
    painter.text(x + 1.6, y + 2.2, label, 1.55, align="start")
    painter.text(x + 1.6, y + h - 1.15, _ellipsize(value or "", w - 3.2, 2.35), 2.35, align="start")


def _outline(painter: Painter, x: float, y: float, w: float, h: float, fill: str) -> None:
    painter.set_stroke(RULE, 0.28)
    painter.set_fill(fill)
    painter.rect(x, y, w, h)
    painter.set_fill(None)
