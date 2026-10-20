"""Paint a cutout sheet in page millimetres (Y down).

Geometry follows ``scale_denom`` so a sheet that fits is 1:1. The hole table,
title block, and dimension text stay in page millimetres so they stay readable
when a large panel is reduced to fit the page. DXF export is always 1:1.
"""

from __future__ import annotations

from pathlib import Path

from panelcad.core.cutout import (
    MM_PER_INCH,
    TITLE_LINE_MM,
    CutFeature,
    SurfaceDrawing,
    d_cut_paths,
    local_holes,
    local_profile,
    opening_reach_mm,
    opens_as_circle,
    surface_title,
    transform_local,
    view_point,
)
from panelcad.core.geometry import rotate_point
from panelcad.core.models import Project
from panelcad.render.qt_painter import QtPainter, text_width_mm

_CUT = "#9f1239"
_DRILL = "#1d4ed8"
_INK = "#111111"
_RULE = "#444444"
_PAPER = "#ffffff"
_BAND = "#f3f1ea"
_WARN = "#9a3412"


# 6 pt is 25.4/72*6 mm. Package title and hole-table text stay at least this tall.
MIN_PAPER_TEXT_MM = 2.4


def needs_package_legend(fit_scale: float) -> bool:
    """A print-package squeeze below this makes the sheet's own title about 6 pt."""

    return fit_scale < 0.9


def legend_reserves(drawing: SurfaceDrawing, page_width: float | None = None) -> tuple[float, float]:
    """Top and bottom millimetres a reduced package page keeps for readable chrome.

    Sources are extra lines under the title. They use the full page width so a
    long citation stays readable, and the bottom band grows with them. The 1:1
    sheet does not use this reserve.
    """

    table_h = 8.0 + max(1, len(drawing.rows)) * 6.2
    extra = 0.0
    if page_width and drawing.sources:
        extra = package_source_block_mm(drawing.sources, page_width)
    return table_h + 6.0, 40.0 + extra


def package_source_block_mm(sources: list[str], page_width: float) -> float:
    """Height of the wrapped citation block on a package page."""

    count = _citation_line_count(sources, max(40.0, page_width - 16.0), MIN_PAPER_TEXT_MM)
    if count <= 0:
        return 0.0
    return 2.0 + count * (MIN_PAPER_TEXT_MM + 1.15)


def _citation_line_count(sources: list[str], width: float, size: float) -> int:
    return sum(1 for kind, _text, _offset in _citation_rows(sources, width, size) if kind != "follow")


def _citation_rows(sources: list[str], width: float, size: float) -> list[tuple[str, str, float]]:
    """Rows of ``(kind, text, x_offset)``.

    ``mark`` is the bold word UNVERIFIED. ``follow`` is the URL on that same
    baseline. ``text`` is its own line.
    """

    rows: list[tuple[str, str, float]] = []
    for source in sources:
        if source.startswith("UNVERIFIED "):
            url = source[len("UNVERIFIED ") :]
            word_size = max(size, min(2.6, size + 0.5))
            word_w = text_width_mm("UNVERIFIED  ", word_size, bold=True)
            rows.append(("mark", "UNVERIFIED", 0.0))
            if url and word_w + 8.0 < width and text_width_mm(url, size) <= max(8.0, width - word_w):
                rows.append(("follow", url, word_w))
            else:
                for chunk in _wrap_mm(url, width, size):
                    rows.append(("text", chunk, 0.0))
            continue
        for chunk in _wrap_mm("SOURCE  " + source, width, size):
            rows.append(("text", chunk, 0.0))
    return rows


def _paint_citations(
    qp: QtPainter, x: float, y: float, width: float, sources: list[str], size: float, pitch: float
) -> None:
    """Draw citations. A ``follow`` row shares the previous baseline."""

    cursor = y
    rows = _citation_rows(sources, width, size)
    index = 0
    while index < len(rows):
        kind, text, offset = rows[index]
        if kind == "follow":
            index += 1
            continue
        if kind == "mark":
            word_size = max(size, min(2.6, size + 0.5))
            qp.text(x, cursor, text, word_size, align="start", bold=True, color=_WARN)
            if index + 1 < len(rows) and rows[index + 1][0] == "follow":
                qp.text(x + rows[index + 1][2], cursor, rows[index + 1][1], size, align="start")
                index += 1
        else:
            qp.text(x + offset, cursor, text, size, align="start")
        cursor += pitch
        index += 1


def _wrap_mm(text: str, width: float, size: float) -> list[str]:
    if not text:
        return []
    if text_width_mm(text, size) <= width:
        return [text]
    lines: list[str] = []
    chunk = ""
    for char in text:
        trial = chunk + char
        if chunk and text_width_mm(trial, size) > width:
            lines.append(chunk)
            chunk = char
        else:
            chunk = trial
    if chunk:
        lines.append(chunk)
    return lines or [text]


def paint_cutout(
    qp: QtPainter,
    drawing: SurfaceDrawing,
    project: Project | None = None,
    *,
    fit_scale: float = 1.0,
    chrome: bool = True,
) -> None:
    """Draw one surface sheet. ``qp`` is already in page millimetres, Y down.

    ``fit_scale`` is an extra reduction applied by a print package that squeezed
    this sheet onto a smaller page. The title block scale note uses it.
    ``chrome`` is false when that package draws the title and hole table itself,
    at paper size, so the type does not shrink with the panel.
    """

    qp.set_fill(_PAPER)
    qp.set_stroke(_PAPER, 0.2)
    qp.rect(0.0, 0.0, drawing.page_width_mm, drawing.page_height_mm)
    qp.set_fill(None)
    if drawing.outline_set:
        _outline(qp, drawing)
    for feature in drawing.features:
        _feature(qp, drawing, feature)
    if drawing.outline_set:
        _dimensions(qp, drawing)
    if chrome:
        _table(qp, drawing)
        _title(qp, drawing, project, fit_scale=fit_scale)


def printed_scale_note(scale_denom: float, fit_scale: float = 1.0) -> str:
    """Title-block scale. A non-integer squeeze is not a shop scale — use the DXF."""

    denom = scale_denom if scale_denom else 1.0
    fit = fit_scale if fit_scale and fit_scale > 1e-6 else 1.0
    if abs(fit - 1.0) <= 0.01:
        return f"1:{denom:g}"
    effective = denom / fit
    nearest = round(effective)
    if nearest >= 1 and abs(effective - nearest) <= 0.02:
        return f"1:{nearest:g}"
    return "NOT TO SCALE – use DXF"


def render_cutout_qimage(drawing: SurfaceDrawing, project: Project | None = None, *, px_per_mm: float = 4.0):
    """Full-page raster of one sheet. ``px_per_mm`` is the pixels per page millimetre."""

    from PySide6.QtGui import QColor, QImage, QPainter

    scale = max(0.5, float(px_per_mm))
    width = max(1, int(round(drawing.page_width_mm * scale)))
    height = max(1, int(round(drawing.page_height_mm * scale)))
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(_PAPER))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(scale, scale)
    paint_cutout(QtPainter(painter, print_paper=True), drawing, project)
    painter.end()
    return image


def write_cutout_pdf(path: Path | str, drawings: list[SurfaceDrawing], project: Project | None = None) -> Path:
    """One PDF page per surface, at the sheet size from the fit (1:1 when it fits)."""

    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.render.page import configure_full_page

    if not drawings:
        raise ValueError("no cutout sheets to export")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(dest))
    writer.setResolution(150)
    title = project.name if project is not None and project.name else "Cutouts"
    writer.setTitle(title)
    configure_full_page(writer, drawings[0].page_width_mm, drawings[0].page_height_mm)
    painter = QPainter(writer)
    try:
        for index, drawing in enumerate(drawings):
            if index:
                configure_full_page(writer, drawing.page_width_mm, drawing.page_height_mm)
                writer.newPage()
            dpi = float(writer.logicalDpiX() or 72)
            painter.resetTransform()
            painter.scale(dpi / 25.4, dpi / 25.4)
            paint_cutout(QtPainter(painter, print_paper=True), drawing, project)
    finally:
        if painter.isActive():
            painter.end()
    return dest


def paint_cutouts_on_printer(printer, drawings: list[SurfaceDrawing], project: Project | None = None) -> None:
    """Paint every surface into a QPrinter (print preview or a real printer)."""

    from PySide6.QtGui import QPainter

    from panelcad.render.page import configure_full_page

    if not drawings:
        return
    configure_full_page(printer, drawings[0].page_width_mm, drawings[0].page_height_mm)
    painter = QPainter(printer)
    try:
        for index, drawing in enumerate(drawings):
            if index:
                configure_full_page(printer, drawing.page_width_mm, drawing.page_height_mm)
                printer.newPage()
            dpi = float(printer.logicalDpiX() or 72)
            painter.resetTransform()
            painter.scale(dpi / 25.4, dpi / 25.4)
            paint_cutout(QtPainter(painter, print_paper=True), drawing, project)
    finally:
        if painter.isActive():
            painter.end()


def _scale(drawing: SurfaceDrawing) -> float:
    return drawing.scale_denom if drawing.scale_denom else 1.0


def _page(drawing: SurfaceDrawing, x_mm: float, y_mm: float) -> tuple[float, float]:
    scale = _scale(drawing)
    return (drawing.panel_origin_x + x_mm / scale, drawing.panel_origin_y + y_mm / scale)


def _outline(qp: QtPainter, drawing: SurfaceDrawing) -> None:
    scale = _scale(drawing)
    qp.set_stroke(_INK, 0.5)
    qp.set_fill(None)
    qp.rect(
        drawing.panel_origin_x,
        drawing.panel_origin_y,
        drawing.panel_width_mm / scale,
        drawing.panel_height_mm / scale,
    )


def _feature(qp: QtPainter, drawing: SurfaceDrawing, feature: CutFeature) -> None:
    width = drawing.panel_width_mm
    vx, vy = feature.view_center(width, drawing.view)
    px, py = _page(drawing, vx, vy)
    if feature.cutout.unknown or not feature.cutout.shape:
        _unknown(qp, px, py, feature.ref)
        return
    scale = _scale(drawing)
    qp.set_fill(None)
    if opens_as_circle(feature.cutout, feature.include_key):
        radius = float(feature.cutout.diameter_mm or 0.0) / 2.0 / scale
        qp.set_stroke(_CUT, 0.45)
        qp.circle(px, py, radius)
    elif feature.cutout.cuts_key(feature.include_key) and feature.cutout.diameter_mm:
        qp.set_stroke(_CUT, 0.45)
        _d_hole(qp, drawing, feature)
    else:
        points = transform_local(
            local_profile(feature.cutout, include_key=feature.include_key),
            cx=feature.cx,
            cy=feature.cy,
            rotation=feature.rotation,
            panel_width=width,
            view=drawing.view,
        )
        qp.set_stroke(_CUT, 0.45)
        qp.polyline([_page(drawing, x, y) for x, y in points])
    qp.set_stroke(_DRILL, 0.32)
    for hx, hy, diameter in local_holes(feature.cutout):
        rx, ry = rotate_point(hx, hy, feature.rotation)
        hvx, hvy = view_point(feature.cx + rx, feature.cy + ry, width, drawing.view)
        hx_p, hy_p = _page(drawing, hvx, hvy)
        qp.circle(hx_p, hy_p, (diameter / 2.0) / scale)
    _tag(qp, drawing, feature, px, py)


def _unknown(qp: QtPainter, x: float, y: float, ref: str) -> None:
    arm = 5.0
    qp.set_stroke(_WARN, 0.35)
    qp.set_fill(None)
    qp.line(x - arm, y - arm, x + arm, y + arm)
    qp.line(x - arm, y + arm, x + arm, y - arm)
    qp.text(x, y - arm - 1.2, ref, 2.8, align="middle")
    qp.text(x, y + arm + 3.4, "CUTOUT UNKNOWN", 2.4, align="middle")


def _tag(qp: QtPainter, drawing: SurfaceDrawing, feature: CutFeature, px: float, py: float) -> None:
    above = _tag_rise_mm(feature, downward=False) / _scale(drawing)
    baseline = py - above
    if baseline < drawing.panel_origin_y + 5.0:
        below = _tag_rise_mm(feature, downward=True) / _scale(drawing)
        baseline = py + below + 3.2
    qp.text(px, baseline, feature.ref, 3.0, align="middle")


def _d_hole(qp: QtPainter, drawing: SurfaceDrawing, feature: CutFeature) -> None:
    scale = _scale(drawing)
    arc, lines = d_cut_paths(
        feature.cutout,
        cx=feature.cx,
        cy=feature.cy,
        rotation=feature.rotation,
        panel_width=drawing.panel_width_mm,
        view=drawing.view,
    )
    cx, cy = _page(drawing, arc.cx, arc.cy)
    # Qt's arc angles match the mathematical ones used here: 0° at +X, positive
    # counter-clockwise toward the top of the page. The sweep is the long arc.
    qp.arc(cx, cy, arc.radius / scale, arc.start_deg, arc.sweep_deg)
    for line in lines:
        x1, y1 = _page(drawing, line.x1, line.y1)
        x2, y2 = _page(drawing, line.x2, line.y2)
        qp.line(x1, y1, x2, y2)


def _tag_rise_mm(feature: CutFeature, *, downward: bool = False) -> float:
    """Clearance from the centre to the tag baseline, past the rotated opening."""

    reach = opening_reach_mm(
        feature.cutout,
        include_key=feature.include_key,
        rotation=feature.rotation,
        downward=downward,
    )
    return reach + 5.0


def _dimensions(qp: QtPainter, drawing: SurfaceDrawing) -> None:
    scale = _scale(drawing)
    width = drawing.panel_width_mm / scale
    height = drawing.panel_height_mm / scale
    origin_x = drawing.panel_origin_x
    origin_y = drawing.panel_origin_y
    from_right = str(drawing.datum).endswith("right")
    from_bottom = str(drawing.datum).startswith("bottom")
    left = origin_x
    right = origin_x + width
    top = origin_y
    bottom = origin_y + height
    qp.set_stroke(_RULE, 0.18)
    qp.set_fill(None)
    for mark in drawing.dims:
        gap = 9.0 + mark.stack * 7.5
        if mark.axis == "x":
            x = origin_x + mark.feature_x / scale
            x_datum = right if from_right else left
            if from_bottom:
                y_edge = bottom
                y_dim = bottom + gap
                text_y = y_dim + 2.4
            else:
                y_edge = top
                y_dim = top - gap
                text_y = y_dim - 2.4
            qp.line(x, y_edge, x, y_dim)
            qp.line(x_datum, y_dim, x, y_dim)
            _tick(qp, x_datum, y_dim, vertical=True)
            _tick(qp, x, y_dim, vertical=True)
            qp.text(x, text_y, mark.label, 2.6, align="middle")
        else:
            y = origin_y + mark.feature_y / scale
            y_datum = bottom if from_bottom else top
            if from_right:
                x_edge = right
                x_dim = right + gap
                text_x = x_dim + 3.4
            else:
                x_edge = left
                x_dim = left - gap
                text_x = x_dim - 3.4
            qp.line(x_edge, y, x_dim, y)
            qp.line(x_dim, y_datum, x_dim, y)
            _tick(qp, x_dim, y_datum, vertical=False)
            _tick(qp, x_dim, y, vertical=False)
            qp.text(text_x, y, mark.label, 2.6, align="middle", rotate=-90)


def _tick(qp: QtPainter, x: float, y: float, *, vertical: bool) -> None:
    if vertical:
        qp.line(x, y - 1.3, x, y + 1.3)
    else:
        qp.line(x - 1.3, y, x + 1.3, y)


def _table(qp: QtPainter, drawing: SurfaceDrawing) -> None:
    scale = _scale(drawing)
    panel_w = (drawing.panel_width_mm / scale) if drawing.outline_set else 200.0
    from_right = str(drawing.datum).endswith("right")
    right_clear = drawing.gutter_x if from_right else 8.0
    x = drawing.panel_origin_x + panel_w + right_clear
    y = drawing.panel_origin_y
    widths = (32.0, 48.0, 12.0, 16.0, 16.0)
    total = sum(widths)
    headers = ("TAG", "SIZE", "QTY", "X", "Y")
    row_h = 6.2
    header_h = 8.0
    body = max(1, len(drawing.rows)) * row_h
    qp.set_fill(_BAND)
    qp.set_stroke(_INK, 0.25)
    qp.rect(x, y, total, header_h + body)
    qp.set_fill(None)
    qp.line(x, y + header_h, x + total, y + header_h)
    cursor = x
    for header, width in zip(headers, widths):
        qp.text(cursor + 1.4, y + 5.6, header, 2.4, align="start")
        cursor += width
        if cursor < x + total - 0.1:
            qp.line(cursor, y, cursor, y + header_h + body)
    for index, row in enumerate(drawing.rows):
        top = y + header_h + index * row_h
        if index:
            qp.set_stroke(_RULE, 0.12)
            qp.line(x, top, x + total, top)
        baseline = top + 4.4
        qty = "—" if row.qty == 0 else str(row.qty)
        values = (
            row.tag,
            row.size,
            qty,
            _fmt(row.x_mm, drawing.units),
            _fmt(row.y_mm, drawing.units),
        )
        cursor = x
        qp.set_stroke(_INK, 0.2)
        for value, width in zip(values, widths):
            qp.text(cursor + 1.4, baseline, value, 2.3, align="start")
            cursor += width


def _title(qp: QtPainter, drawing: SurfaceDrawing, project: Project | None, *, fit_scale: float = 1.0) -> None:
    scale = _scale(drawing)
    panel_h = (drawing.panel_height_mm / scale) if drawing.outline_set else 40.0
    table_h = 8.0 + max(1, len(drawing.rows)) * 6.2
    from_bottom = str(drawing.datum).startswith("bottom")
    bottom_clear = drawing.gutter_y if from_bottom else 8.0
    top = drawing.panel_origin_y + max(panel_h + bottom_clear, table_h + 8.0)
    left = 10.0
    width = max(40.0, drawing.page_width_mm - 20.0)
    sources = list(drawing.sources)
    # Fixed height. Extra sources use the empty right column, not a taller block.
    height = 32.0
    qp.set_stroke(_INK, 0.4)
    qp.set_fill(_PAPER)
    qp.rect(left, top, width, height)
    qp.line(left, top + 8.0, left + width, top + 8.0)
    qp.line(left + width * 0.62, top, left + width * 0.62, top + 8.0)
    tb = project.title_block if project is not None else None
    company = (tb.company if tb else "") or "PanelCAD"
    number = (tb.drawing_number if tb else "") or "—"
    rev = ""
    if tb and tb.revisions:
        rev = tb.revisions[0].rev or ""
    view = "VIEW FROM BACK" if drawing.view == "back" else "VIEW FROM FRONT"
    datum = drawing.datum.replace("_", " ").upper()
    units = "INCH" if drawing.units == "inch" else "MM"
    scale_note = printed_scale_note(drawing.scale_denom, fit_scale)
    scale_field = scale_note if scale_note.startswith("NOT TO SCALE") else f"SCALE {scale_note}"
    qp.text(left + 2.0, top + 5.6, f"{surface_title(drawing.surface).upper()} CUTOUT / DRILL", 3.2, align="start")
    qp.text(left + width * 0.62 + 2.0, top + 5.6, f"{number}   REV {rev or '—'}", 2.8, align="start")
    qp.text(left + 2.0, top + 13.2, company, 2.6, align="start")
    qp.text(
        left + 2.0,
        top + 18.4,
        f"{view}    DATUM {datum}    {drawing.style.upper()}    {scale_field}    {units}",
        2.4,
        align="start",
    )
    note = "Hole table X/Y are from the datum, positive into the panel (Y down). DXF export is 1:1."
    qp.text(left + 2.0, top + 23.2, note, 2.1, align="start")
    if sources:
        col_x = left + width * 0.62 + 2.0
        col_w = max(24.0, left + width - 2.0 - col_x)
        count = max(1, _citation_line_count(sources, col_w, 1.7))
        pitch = min(TITLE_LINE_MM, max(3.2, (height - 12.5) / count))
        _paint_citations(qp, col_x, top + 12.2, col_w, sources, 1.7, pitch)
    if drawing.features and any(feature.cutout.unknown for feature in drawing.features):
        qp.text(left + 2.0, top + 28.2, "CUTOUT UNKNOWN — edit the part before manufacturing.", 2.0, align="start")


def paint_cutout_legend(
    qp: QtPainter,
    drawing: SurfaceDrawing,
    project: Project | None,
    *,
    page_width: float,
    page_height: float,
    fit_scale: float,
) -> None:
    """Title block and hole table in paper millimetres, at least ``MIN_PAPER_TEXT_MM``."""

    widths = (32.0, 48.0, 12.0, 16.0, 16.0)
    total = sum(widths)
    row_h = 6.2
    header_h = 8.0
    body = max(1, len(drawing.rows)) * row_h
    x = max(6.0, page_width - total - 8.0)
    y = 4.0
    qp.set_fill(_BAND)
    qp.set_stroke(_INK, 0.25)
    qp.rect(x, y, total, header_h + body)
    qp.set_fill(None)
    qp.line(x, y + header_h, x + total, y + header_h)
    cursor = x
    headers = ("TAG", "SIZE", "QTY", "X", "Y")
    for header, width in zip(headers, widths):
        qp.text(cursor + 1.4, y + 5.6, header, MIN_PAPER_TEXT_MM, align="start")
        cursor += width
        if cursor < x + total - 0.1:
            qp.line(cursor, y, cursor, y + header_h + body)
    for index, row in enumerate(drawing.rows):
        top = y + header_h + index * row_h
        if index:
            qp.set_stroke(_RULE, 0.12)
            qp.line(x, top, x + total, top)
        baseline = top + 4.6
        qty = "—" if row.qty == 0 else str(row.qty)
        values = (
            row.tag,
            row.size,
            qty,
            _fmt(row.x_mm, drawing.units),
            _fmt(row.y_mm, drawing.units),
        )
        cursor = x
        qp.set_stroke(_INK, 0.2)
        for value, width in zip(values, widths):
            qp.text(cursor + 1.4, baseline, value, MIN_PAPER_TEXT_MM, align="start")
            cursor += width

    left = 8.0
    width = max(40.0, page_width - 16.0)
    source_h = package_source_block_mm(list(drawing.sources), page_width) if drawing.sources else 0.0
    height = 34.0 + source_h
    top = page_height - height - 6.0
    qp.set_stroke(_INK, 0.4)
    qp.set_fill(_PAPER)
    qp.rect(left, top, width, height)
    qp.line(left, top + 8.0, left + width, top + 8.0)
    qp.line(left + width * 0.62, top, left + width * 0.62, top + 8.0)
    tb = project.title_block if project is not None else None
    number = (tb.drawing_number if tb else "") or "—"
    rev = ""
    if tb and tb.revisions:
        rev = tb.revisions[0].rev or ""
    view = "VIEW FROM BACK" if drawing.view == "back" else "VIEW FROM FRONT"
    datum = drawing.datum.replace("_", " ").upper()
    units = "INCH" if drawing.units == "inch" else "MM"
    scale_note = printed_scale_note(drawing.scale_denom, fit_scale)
    scale_field = scale_note if scale_note.startswith("NOT TO SCALE") else f"SCALE {scale_note}"
    qp.text(left + 2.0, top + 5.6, f"{surface_title(drawing.surface).upper()} CUTOUT / DRILL", 3.2, align="start")
    qp.text(left + width * 0.62 + 2.0, top + 5.6, f"{number}   REV {rev or '—'}", MIN_PAPER_TEXT_MM, align="start")
    qp.text(
        left + 2.0,
        top + 13.0,
        f"{view}    DATUM {datum}    {drawing.style.upper()}    {scale_field}    {units}",
        MIN_PAPER_TEXT_MM,
        align="start",
    )
    qp.text(
        left + 2.0,
        top + 19.2,
        "Hole table X/Y are from the datum, positive into the panel (Y down). DXF export is 1:1.",
        MIN_PAPER_TEXT_MM,
        align="start",
    )
    qp.text(
        left + 2.0,
        top + 25.4,
        "Print the 1:1 cutout sheet or the DXF.",
        MIN_PAPER_TEXT_MM,
        align="start",
    )
    if drawing.sources:
        _paint_citations(
            qp,
            left + 2.0,
            top + 31.0,
            max(24.0, width - 4.0),
            list(drawing.sources),
            MIN_PAPER_TEXT_MM,
            MIN_PAPER_TEXT_MM + 1.15,
        )


def _fmt(value_mm: float, units: str) -> str:
    if units == "inch":
        return f"{value_mm / MM_PER_INCH:.3f}"
    if abs(value_mm - round(value_mm)) < 1e-6:
        return f"{value_mm:.0f}"
    return f"{value_mm:.1f}"
