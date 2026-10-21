"""Sheet zone grid — the same ticks drawn on the title-block chrome.

Column numbers run left to right (1…); row letters run top to bottom (A…).
NEMA ladder line numbers are rung-based: ``sheet * 100 + rung_index`` at a
configurable pitch (default 0.5 in / 12.7 mm), not the coarse zone rows.
IEC locations stay ``/sheet.column``.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass

from panelcad.core.models import Project, Sheet
from panelcad.render.titleblock import working_rect

# JIC/NEMA half-inch rung. Matches 0.5 in; the 2.5 mm snap lands nearby.
DEFAULT_RUNG_PITCH_MM = 12.7
# First rung sits below the L1/L2 rail captions (rails start at wy + 10).
RUNG_ORIGIN_INSET_MM = 16.0
MAX_RUNG_INDEX = 99


@dataclass(frozen=True)
class ZoneGrid:
    """Drawable area carved into the printed zone cells."""

    wx: float
    wy: float
    ww: float
    wh: float
    cols: int
    rows: int
    cw: float
    rh: float

    def col_at(self, x: float) -> int:
        if self.cols <= 0 or self.cw <= 0:
            return 1
        raw = int((float(x) - self.wx) / self.cw) + 1
        return max(1, min(self.cols, raw))

    def row_index_at(self, y: float) -> int:
        if self.rows <= 0 or self.rh <= 0:
            return 1
        raw = int((float(y) - self.wy) / self.rh) + 1
        return max(1, min(self.rows, raw))

    def row_letter(self, index: int) -> str:
        n = max(1, int(index))
        return chr(ord("A") + n - 1)

    def row_center_y(self, index: int) -> float:
        n = max(1, min(self.rows, int(index)))
        return self.wy + (n - 0.5) * self.rh

    def col_center_x(self, index: int) -> float:
        n = max(1, min(self.cols, int(index)))
        return self.wx + (n - 0.5) * self.cw

    def rung_origin(self) -> float:
        return self.wy + RUNG_ORIGIN_INSET_MM

    def rung_index_at(self, y: float, pitch: float = DEFAULT_RUNG_PITCH_MM) -> int:
        step = float(pitch) if float(pitch) > 0.5 else DEFAULT_RUNG_PITCH_MM
        raw = int(round((float(y) - self.rung_origin()) / step)) + 1
        return max(1, min(MAX_RUNG_INDEX, raw))

    def rung_y(self, index: int, pitch: float = DEFAULT_RUNG_PITCH_MM) -> float:
        step = float(pitch) if float(pitch) > 0.5 else DEFAULT_RUNG_PITCH_MM
        n = max(1, min(MAX_RUNG_INDEX, int(index)))
        return self.rung_origin() + (n - 1) * step

    def max_rung_index(self, pitch: float = DEFAULT_RUNG_PITCH_MM) -> int:
        step = float(pitch) if float(pitch) > 0.5 else DEFAULT_RUNG_PITCH_MM
        last_y = self.wy + self.wh - 8.0
        raw = int((last_y - self.rung_origin()) / step) + 1
        return max(1, min(MAX_RUNG_INDEX, raw))

    def nema_line(self, y: float, sheet_no: int, pitch: float = DEFAULT_RUNG_PITCH_MM) -> int:
        """Sheet/line number from rung pitch (101 on sheet 1, first rung)."""

        return nema_line_from_index(sheet_no, self.rung_index_at(y, pitch))


def zone_axes(ww: float, wh: float) -> tuple[int, int, float, float]:
    """Column count, row count, column width, row height. Shared with the title block."""

    cols = max(8, int(float(ww) / 26.0))
    rows = max(4, int(float(wh) / 32.0))
    cw = float(ww) / cols if cols else float(ww)
    rh = float(wh) / rows if rows else float(wh)
    return cols, rows, cw, rh


def zone_grid(sheet: Sheet) -> ZoneGrid:
    """Same column/row counts ``_draw_zones`` uses so refs match the printed ticks."""

    wx, wy, ww, wh = working_rect(sheet)
    cols, rows, cw, rh = zone_axes(ww, wh)
    return ZoneGrid(wx=wx, wy=wy, ww=ww, wh=wh, cols=cols, rows=rows, cw=cw, rh=rh)


def header_columns(sheet: Sheet) -> list[tuple[int, float, float, float]]:
    """Printed column cells: 1-based index, left, right, label center.

    The label is centred in the cell. A contact at x = 239.9 mm on an A3
    sheet (1919 px at 8 px/mm) falls in column 9, whose label is near 236 mm.
    """

    grid = zone_grid(sheet)
    return [
        (
            index,
            grid.wx + (index - 1) * grid.cw,
            grid.wx + index * grid.cw,
            grid.col_center_x(index),
        )
        for index in range(1, grid.cols + 1)
    ]


def column_from_header(sheet: Sheet, x: float) -> int:
    """Column whose printed cell contains *x*. Matches ``ZoneGrid.col_at``."""

    cells = header_columns(sheet)
    if not cells:
        return 1
    point = float(x)
    for index, _x0, x1, _center in cells:
        if point < x1:
            return index
    return cells[-1][0]


# Print package sheet order. Empty means callers use the list they passed
# (the saved project order). Arrows and cross-references read this so a
# regrouped package agrees with the sheet numbers on the title block.
_sheet_order: contextvars.ContextVar[tuple[str, ...] | None] = contextvars.ContextVar(
    "panelcad_printed_sheet_order",
    default=None,
)


@contextmanager
def use_sheet_order(sheets: list[Sheet] | None):
    """Number sheets as *sheets* for arrows, line numbers, and cross-references."""

    ids = tuple(sheet.id for sheet in sheets) if sheets else None
    token = _sheet_order.set(ids)
    try:
        yield
    finally:
        _sheet_order.reset(token)


def sheet_number(project_sheets: list[Sheet], sheet_id: str) -> int:
    override = _sheet_order.get()
    if override is not None:
        try:
            return override.index(sheet_id) + 1
        except ValueError:
            return 0
    for index, sheet in enumerate(project_sheets, start=1):
        if sheet.id == sheet_id:
            return index
    return 0


def zone_at(sheet: Sheet, x: float, y: float) -> tuple[str, int, int]:
    """(row letter, column number, 1-based row index) for a millimetre point."""

    grid = zone_grid(sheet)
    row_i = grid.row_index_at(y)
    col = grid.col_at(x)
    return (grid.row_letter(row_i), col, row_i)


def rung_pitch_mm(project: Project | None = None) -> float:
    if project is None:
        return DEFAULT_RUNG_PITCH_MM
    raw = getattr(project, "cross_reference", None)
    try:
        pitch = float(getattr(raw, "rung_pitch_mm", DEFAULT_RUNG_PITCH_MM) or DEFAULT_RUNG_PITCH_MM)
    except (TypeError, ValueError):
        return DEFAULT_RUNG_PITCH_MM
    return pitch if pitch > 0.5 else DEFAULT_RUNG_PITCH_MM


def rung_origin_y(sheet: Sheet) -> float:
    return zone_grid(sheet).rung_origin()


def rung_index_at(sheet: Sheet, y: float, *, project: Project | None = None, pitch: float | None = None) -> int:
    step = DEFAULT_RUNG_PITCH_MM if pitch is None else float(pitch)
    if pitch is None and project is not None:
        step = rung_pitch_mm(project)
    return zone_grid(sheet).rung_index_at(y, step)


def rung_y(sheet: Sheet, index: int, *, project: Project | None = None, pitch: float | None = None) -> float:
    step = DEFAULT_RUNG_PITCH_MM if pitch is None else float(pitch)
    if pitch is None and project is not None:
        step = rung_pitch_mm(project)
    return zone_grid(sheet).rung_y(index, step)


def nema_line_from_index(sheet_no: int, index: int) -> int:
    page = max(1, int(sheet_no))
    return page * 100 + max(1, min(MAX_RUNG_INDEX, int(index)))


def nema_line_at(
    sheet: Sheet,
    y: float,
    sheet_no: int,
    *,
    project: Project | None = None,
    pitch: float | None = None,
) -> int:
    return nema_line_from_index(sheet_no, rung_index_at(sheet, y, project=project, pitch=pitch))


def iec_ref_at(sheet: Sheet, x: float, sheet_no: int) -> str:
    col = zone_grid(sheet).col_at(x)
    return f"/{max(1, int(sheet_no))}.{col}"
