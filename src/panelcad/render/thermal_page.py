"""Enclosure thermal report page. Letter PDF, and the print-package section.

The painter reads a report from ``compute_heat_load``. It does not resize
the enclosure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.core.models import Project
from panelcad.version import __version__


def _metrics():
    from panelcad.render.print_package import (
        LINE_H,
        MARGIN_X,
        MARGIN_Y,
        ROW_H,
        ROW_PAD,
        TITLE_H,
        wrap_lines,
        wrapped_row_height,
    )

    return {
        "LINE_H": LINE_H,
        "MARGIN_X": MARGIN_X,
        "MARGIN_Y": MARGIN_Y,
        "ROW_H": ROW_H,
        "ROW_PAD": ROW_PAD,
        "TITLE_H": TITLE_H,
        "wrap_lines": wrap_lines,
        "wrapped_row_height": wrapped_row_height,
    }


TEXT_SIZE = 2.15


def _content_top() -> float:
    m = _metrics()
    return m["MARGIN_Y"] + m["TITLE_H"] + 4.0


def _content_bottom(page_h: float) -> float:
    m = _metrics()
    return page_h - m["MARGIN_Y"] - 8.0


def _widths(page_w: float) -> list[float]:
    m = _metrics()
    usable = page_w - 2 * m["MARGIN_X"]
    fixed = [14.0, 30.0, 14.0, 20.0, 16.0, 18.0]
    flex = max(28.0, usable - sum(fixed))
    return [14.0, flex, 30.0, 14.0, 20.0, 16.0, 18.0]


def _headers() -> list[str]:
    return ["REF", "DESCRIPTION", "CATALOG", "W", "BASIS", "MAX °C", "MARGIN"]


def _cell_note(row: dict[str, Any]) -> str:
    limit = row.get("max_ambient_c")
    if limit is None:
        return ""
    if row.get("max_ambient_basis") == "published":
        return f"{float(limit):g}"
    return f"{float(limit):g} user"


def _margin_text(row: dict[str, Any]) -> str:
    from panelcad.core.thermal import format_margin

    margin = row.get("margin_k")
    if margin is None:
        return ""
    return format_margin(float(margin))


def _row_colors(row: dict[str, Any]) -> list[str | None]:
    colors: list[str | None] = [None, None, None, None, None, None, None]
    margin = row.get("margin_k")
    if margin is not None and float(margin) < 0:
        colors[6] = "#9b1c1c"
    return colors


def _row_values(row: dict[str, Any]) -> list[str]:
    return [
        str(row.get("ref") or ""),
        str(row.get("description") or ""),
        str(row.get("catalog") or ""),
        f"{float(row.get('watts') or 0):.2f}",
        str(row.get("source_label") or ""),
        _cell_note(row),
        _margin_text(row),
    ]


def _row_heights(report: dict[str, Any], page_w: float) -> list[float]:
    m = _metrics()
    widths = _widths(page_w)
    heights: list[float] = []
    for row in report.get("devices") or []:
        heights.append(m["wrapped_row_height"](_row_values(row), widths, TEXT_SIZE))
    return heights


def _box_line(report: dict[str, Any]) -> str:
    enc = report.get("enclosure") or {}
    if not enc:
        return "Needs info: no enclosure size is set. A page outline is not used."
    rating = enc.get("nema") or "—"
    rating_text = rating if str(rating).startswith("IP") else f"NEMA {rating}"
    return (
        f"{float(enc.get('width_mm') or 0):g} × {float(enc.get('height_mm') or 0):g} × "
        f"{float(enc.get('depth_mm') or 0):g} mm"
        f"    {enc.get('material_label') or enc.get('material') or ''}"
        f"    mount {enc.get('mount') or ''}"
        f"    {rating_text}"
    )


def _summary_columns(report: dict[str, Any]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    left = [
        ("Total", _fmt(report.get("watts_total"), " W")),
        ("Published", _fmt(report.get("watts_published"), " W")),
        ("Estimated", _fmt(report.get("watts_estimate"), " W")),
        ("Area", _fmt(report.get("surface_m2"), " m²", 3)),
        ("k", _fmt(report.get("k_w_m2k"), " W/m²K", 1)),
    ]
    right = [
        ("Ambient", _fmt(report.get("ambient_c"), " °C", 1)),
        ("Max internal", _fmt(report.get("max_internal_c"), " °C", 1)),
        ("Rise", _fmt(report.get("rise_k"), " K", 1)),
        ("Uncontrolled", _fmt(report.get("internal_c"), " °C", 1)),
        ("Design", _fmt(report.get("design_c"), " °C", 1)),
    ]
    return left, right


_PAIR_H = 4.3
_REC_SIZE = 2.55
_REC_STEP = 4.4


def _summary_height(report: dict[str, Any], page_w: float) -> float:
    m = _metrics()
    width = page_w - 2 * m["MARGIN_X"]
    wrap = m["wrap_lines"]
    height = 2.0
    height += max(1, len(wrap(_box_line(report), width, 2.4))) * 4.2 + 1.2
    height += 5 * _PAIR_H + 1.6
    rec = str((report.get("recommendation") or {}).get("summary") or "")
    height += max(1, len(wrap(rec, width, _REC_SIZE, bold=True))) * _REC_STEP + 1.0
    findings = list(report.get("findings") or [])[:4]
    if findings:
        height += 4.4
        for item in findings:
            height += max(1, len(wrap(str(item.get("message") or ""), width, 2.2))) * 3.6
    return height + 2.0


def _assumption_height(report: dict[str, Any], page_w: float) -> float:
    m = _metrics()
    width = page_w - 2 * m["MARGIN_X"]
    height = 8.0
    for line in report.get("assumptions") or []:
        wrapped = m["wrap_lines"](str(line), width, 2.25)
        height += max(1, len(wrapped)) * 3.35
    return height + 2.0


def thermal_spans(report: dict[str, Any], page_w: float, page_h: float) -> list[tuple[int, int]]:
    """(start, count) row windows. The last window keeps room for assumptions."""

    devices = list(report.get("devices") or [])
    n = len(devices)
    heights = _row_heights(report, page_w)
    assume = _assumption_height(report, page_w)
    summary_h = _summary_height(report, page_w)
    top = _content_top()
    bottom = _content_bottom(page_h)
    header = _metrics()["ROW_H"]
    if n == 0:
        return [(0, 0)]

    def take(start: int, budget: float) -> int:
        used = 0.0
        index = start
        while index < n and used + heights[index] <= budget + 1e-6:
            used += heights[index]
            index += 1
        if index == start:
            index = start + 1
        return index

    one_budget = bottom - top - summary_h - header - assume
    if sum(heights) <= one_budget + 1e-6:
        return [(0, n)]

    pages: list[tuple[int, int]] = []
    first_budget = bottom - top - summary_h - header
    end = take(0, first_budget)
    pages.append((0, end))
    full_budget = bottom - top - header
    last_budget = max(header, full_budget - assume)
    while end < n:
        remaining = sum(heights[end:])
        if remaining <= last_budget + 1e-6:
            pages.append((end, n))
            break
        nxt = take(end, full_budget)
        if nxt >= n:
            nxt = take(end, last_budget)
        if nxt <= end:
            nxt = min(n, end + 1)
        pages.append((end, nxt))
        if nxt <= end:
            break
        end = nxt
    return [(start, stop - start) for start, stop in pages]


def _fmt(value: Any, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}{suffix}"


def paint_thermal_page(
    qp,
    project: Project,
    report: dict[str, Any],
    *,
    page_w: float,
    page_h: float,
    page_index: int,
    page_count: int,
    row_start: int,
    row_count: int,
) -> None:
    """Draw one thermal page. ``qp`` is a ``QtPainter`` in millimetres."""

    from panelcad.render.print_package import (
        PackagePage,
        _footer,
        _frame,
        _header,
        _resolved_widths,
        _row_cells,
        wrap_lines,
    )

    page = PackagePage("thermal", "Enclosure Thermal", page_w, page_h, report="thermal")
    _frame(qp, page_w, page_h)
    y = _header(qp, "ENCLOSURE THERMAL", project, page, page_index, page_count)
    devices = list(report.get("devices") or [])
    last = row_start + row_count >= len(devices)
    if row_start == 0:
        y = _paint_summary(qp, report, y, page_w)
    widths = _resolved_widths(_widths(page_w), page_w)
    x = _metrics()["MARGIN_X"]
    _row_cells(qp, x, y, widths, _metrics()["ROW_H"], _headers(), header=True)
    y += _metrics()["ROW_H"]
    window = devices[row_start : row_start + row_count]
    if not window and row_start == 0:
        qp.text(x, y + 6.0, "No in-cabinet watt loss.", 2.4, align="start")
        y += 8.0
    for row in window:
        values = _row_values(row)
        height = _metrics()["wrapped_row_height"](values, widths, TEXT_SIZE)
        _row_cells(qp, x, y, widths, height, values, header=False, colors=_row_colors(row))
        y += height
    if last:
        y += 3.0
        qp.text(x, y + 4.0, "Assumptions", 2.8, align="start")
        y += 7.0
        width = page_w - 2 * _metrics()["MARGIN_X"]
        for line in report.get("assumptions") or []:
            for wrapped in wrap_lines(str(line), width, 2.25):
                if y > _content_bottom(page_h) - 1.0:
                    break
                qp.text(x, y, wrapped, 2.25, align="start")
                y += 3.35
    _footer(qp, project, page, page_index, page_count)


def _paint_summary(qp, report: dict[str, Any], y: float, page_w: float) -> float:
    from panelcad.render.print_package import wrap_lines

    m = _metrics()
    x = m["MARGIN_X"]
    width = page_w - 2 * m["MARGIN_X"]
    for wrapped in wrap_lines(_box_line(report), width, 2.4):
        qp.text(x, y + 3.2, wrapped, 2.4, align="start")
        y += 4.2
    y += 1.2
    left, right = _summary_columns(report)
    col_w = width / 2.0
    label_w = 28.0
    top = y
    for index, (label, value) in enumerate(left):
        yy = top + index * _PAIR_H
        qp.text(x, yy + 3.2, label, 2.35, align="start")
        qp.text(x + label_w, yy + 3.2, value, 2.35, align="start")
    for index, (label, value) in enumerate(right):
        yy = top + index * _PAIR_H
        qp.text(x + col_w, yy + 3.2, label, 2.35, align="start")
        qp.text(x + col_w + label_w, yy + 3.2, value, 2.35, align="start")
    y = top + max(len(left), len(right)) * _PAIR_H + 1.6
    rec = str((report.get("recommendation") or {}).get("summary") or "")
    for wrapped in wrap_lines(rec, width, _REC_SIZE, bold=True):
        qp.text(x, y + 3.4, wrapped, _REC_SIZE, align="start", bold=True)
        y += _REC_STEP
    y += 1.0
    findings = report.get("findings") or []
    if findings:
        qp.text(x, y + 3.2, f"Design checks: {len(findings)}", 2.45, align="start")
        y += 4.4
        for item in findings[:4]:
            for wrapped in wrap_lines(str(item.get("message") or ""), width, 2.2):
                qp.text(x, y + 3.0, wrapped, 2.2, align="start")
                y += 3.6
    return y + 2.0


def write_thermal_pdf(session: Any, path: Path | str, report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Standalone letter PDF of the thermal report."""

    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.core.thermal import compute_heat_load
    from panelcad.render.page import configure_full_page
    from panelcad.render.print_package import PAPER_SIZES_MM
    from panelcad.render.qt_painter import QtPainter

    if report is None:
        report = compute_heat_load(session.project, session.catalog, getattr(session, "parts", None))
    page_w, page_h = PAPER_SIZES_MM["letter"]
    spans = thermal_spans(report, page_w, page_h)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(dest))
    writer.setResolution(300)
    writer.setTitle(f"{session.project.name or 'PanelCAD'} enclosure thermal")
    if hasattr(writer, "setCreator"):
        writer.setCreator(f"PanelCAD {__version__}")
    configure_full_page(writer, page_w, page_h)
    painter = QPainter(writer)
    try:
        for index, (start, count) in enumerate(spans):
            if index:
                writer.newPage()
            dpi_x = float(writer.logicalDpiX() or 72)
            dpi_y = float(writer.logicalDpiY() or 72)
            painter.resetTransform()
            painter.scale(dpi_x / 25.4, dpi_y / 25.4)
            from PySide6.QtCore import QRectF

            from panelcad.render.page import PRINT_PAPER

            painter.fillRect(QRectF(0, 0, page_w, page_h), PRINT_PAPER)
            qp = QtPainter(painter, print_paper=True)
            paint_thermal_page(
                qp,
                session.project,
                report,
                page_w=page_w,
                page_h=page_h,
                page_index=index,
                page_count=len(spans),
                row_start=start,
                row_count=count,
            )
    finally:
        if painter.isActive():
            painter.end()
    out = dict(report)
    out["path"] = str(dest)
    out["pages"] = len(spans)
    return out


def thermal_page_count(report: dict[str, Any], page_w: float, page_h: float) -> int:
    return len(thermal_spans(report, page_w, page_h))
