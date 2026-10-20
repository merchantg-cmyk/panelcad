"""Revision compare report: CSV and a one-or-more page PDF."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from panelcad.core.models import Project

PAGE_W = 215.9
PAGE_H = 279.4
MARGIN = 12.0
HEADER_H = 24.0
FOOTER_H = 10.0
ROW_H = 6.2
BANNER_H = 6.4
CELL_SIZE = 2.2
CELL_INSET = 1.6
LINE_PITCH = 3.15
RULE = "#2a2a2a"
HEADER_FILL = "#ece8dc"
BAND = "#e7e2d4"
PAPER = "#ffffff"

CSV_FIELDS = ["section", "change", "sheet", "tag", "field", "before", "after", "detail"]

_SECTION_LABEL = {
    "device": "DEVICES",
    "wire": "WIRES",
    "terminal": "TERMINALS",
    "bom": "BILL OF MATERIALS",
    "layout": "LAYOUT",
}
_SECTION_ORDER = ("device", "wire", "terminal", "bom", "layout")
_CHANGE_LABEL = {
    "added": "ADDED",
    "removed": "REMOVED",
    "changed": "CHANGED",
    "renumbered": "RENUMBERED",
    "qty": "QTY",
    "part": "PART",
    "moved": "MOVED",
    "none": "NONE",
}
_FIELD_LABEL = {
    "library_id": "library",
    "part_number": "part number",
    "catalog": "catalog",
    "manufacturer": "manufacturer",
    "rating": "rating",
    "tag": "tag",
    "position": "position",
    "number": "wire number",
    "wire": "wire",
    "terminal": "terminal",
    "qty": "qty",
    "level": "level",
    "jumper": "jumper",
    "strip": "strip",
}


def write_compare_csv(path: Path | str, report: dict[str, Any]) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so csv's CRLF is not doubled by Windows text mode.
    with dest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\r\n")
        writer.writeheader()
        for row in report.get("rows") or []:
            writer.writerow({key: row.get(key, "") for key in CSV_FIELDS})
    return dest


def _summary(report: dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    parts: list[str] = []

    def bits(name: str, keys: tuple[str, ...], words: tuple[str, ...]) -> None:
        nums = [int(counts.get(key) or 0) for key in keys]
        if not any(nums):
            return
        shown = [f"{word} {nums[i]}" for i, word in enumerate(words) if nums[i]]
        parts.append(f"{name} " + ", ".join(shown))

    bits(
        "devices",
        ("devices_added", "devices_removed", "devices_changed"),
        ("added", "removed", "changed"),
    )
    bits(
        "wires",
        ("wires_added", "wires_removed", "wires_renumbered"),
        ("added", "removed", "renumbered"),
    )
    bits(
        "terminals",
        ("terminals_added", "terminals_removed", "terminals_changed"),
        ("added", "removed", "changed"),
    )
    if int(counts.get("bom") or 0):
        parts.append(f"BOM {int(counts['bom'])}")
    if int(counts.get("layout") or 0):
        parts.append(f"layout moves {int(counts['layout'])}")
    return "   ".join(parts) if parts else "No differences"


def _field_label(field: str) -> str:
    key = str(field or "").strip()
    return _FIELD_LABEL.get(key, key.replace("_", " "))


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def display_item(row: dict[str, Any]) -> str:
    """Shop wording for the ITEM column. Structured fields stay in the CSV."""

    section = str(row.get("section") or "")
    change = str(row.get("change") or "")
    tag = _clean(row.get("tag"))
    field = _clean(row.get("field"))
    detail = _clean(row.get("detail"))
    name = _clean(row.get("name"))
    before = _clean(row.get("before"))
    after = _clean(row.get("after"))
    if change == "none":
        return detail or "No differences"
    if section == "device" and change == "added":
        kind = f" ({name})" if name else ""
        return f"{tag} added{kind}".strip()
    if section == "device" and change == "removed":
        kind = f" ({name})" if name else ""
        return f"{tag} removed{kind}".strip()
    if section == "device" and change == "changed" and field == "tag":
        return f"{before} retagged {after}".strip()
    if section == "device" and change == "changed":
        return f"{tag} {_field_label(field)}".strip()
    if section == "wire":
        ends = detail.replace("->", "→")
        verb = change if change in {"added", "removed", "renumbered"} else change
        return f"{ends} {verb}".strip() or tag
    if section == "terminal" and change in {"added", "removed"}:
        return f"{tag} {change}".strip()
    if section == "terminal":
        return f"{tag} {_field_label(field)}".strip()
    if section == "bom" and change == "qty":
        label = name or tag or "Item"
        rating = _clean(row.get("rating"))
        if rating and rating.casefold() not in label.casefold():
            label = f"{label} {rating}"
        extras: list[str] = []
        lib = _clean(row.get("library_id"))
        if lib:
            extras.append(lib)
        if detail and detail not in extras and detail != label:
            extras.append(detail)
        return " ".join([label, "qty", *extras])
    if section == "bom":
        return f"{tag} {_field_label(field)}".strip()
    if section == "layout":
        return f"{tag} moved".strip()
    return " ".join(part for part in (tag, _field_label(field), detail) if part) or detail


def _with_extra(primary: str, extra: str) -> str:
    primary = _clean(primary)
    extra = _clean(extra)
    if not extra or extra == primary:
        return primary
    if not primary:
        return extra
    return f"{primary}  {extra}"


def display_cells(row: dict[str, Any]) -> list[str]:
    """CHANGE, SHEET, ITEM, BEFORE, AFTER as drawn on the compare sheet."""

    change = str(row.get("change") or "")
    section = str(row.get("section") or "")
    before = _clean(row.get("before"))
    after = _clean(row.get("after"))
    detail = _clean(row.get("detail"))
    if section == "device" and change == "added":
        after = _with_extra(after, detail)
    elif section == "device" and change == "removed":
        before = _with_extra(before, detail)
    elif section == "terminal" and change == "added":
        after = _with_extra(after, detail)
    elif section == "terminal" and change == "removed":
        before = _with_extra(before, detail)
    label = _CHANGE_LABEL.get(change, change.upper())
    return [label, _clean(row.get("sheet")), display_item(row), before, after]


def wrap_text(text: str, width_mm: float, size: float) -> list[str]:
    """Break ``text`` on spaces, then dots, so a cell keeps every character."""

    from panelcad.render.qt_painter import text_width_mm

    raw = _clean(text)
    if not raw:
        return [""]
    limit = max(4.0, float(width_mm))

    def fits(value: str) -> bool:
        return text_width_mm(value, size) <= limit

    if fits(raw):
        return [raw]

    def pieces_of(token: str) -> list[str]:
        if fits(token):
            return [token]
        if "." in token.strip("."):
            parts = token.split(".")
            chunks: list[str] = []
            buf = ""
            for index, part in enumerate(parts):
                piece = part + ("." if index < len(parts) - 1 else "")
                if not piece:
                    continue
                if not fits(piece):
                    if buf:
                        chunks.append(buf)
                        buf = ""
                    chunks.extend(_hard_split(piece))
                    continue
                trial = buf + piece
                if buf and fits(trial):
                    buf = trial
                else:
                    if buf:
                        chunks.append(buf)
                    buf = piece
            if buf:
                chunks.append(buf)
            if chunks and "".join(chunks) == token:
                return chunks
        return _hard_split(token)

    def _hard_split(token: str) -> list[str]:
        lines: list[str] = []
        start = 0
        while start < len(token):
            lo, hi, best = 1, len(token) - start, 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if fits(token[start : start + mid]):
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            lines.append(token[start : start + best])
            start += best
        return lines or [token]

    lines: list[str] = []
    current = ""
    for word in raw.split(" "):
        for piece in pieces_of(word):
            trial = piece if not current else f"{current} {piece}"
            if fits(trial):
                current = trial
            else:
                if current:
                    lines.append(current)
                current = piece
    if current:
        lines.append(current)
    return lines or [raw]


def display_lines(report: dict[str, Any]) -> list[tuple[str, Any]]:
    rows = list(report.get("rows") or [])
    lines: list[tuple[str, Any]] = []
    for section in _SECTION_ORDER:
        group = [row for row in rows if row.get("section") == section]
        if not group:
            continue
        lines.append(("banner", _SECTION_LABEL[section]))
        for row in group:
            lines.append(("row", row))
    if not lines:
        lines.append(
            (
                "row",
                {
                    "section": "",
                    "change": "none",
                    "sheet": "",
                    "tag": "",
                    "field": "",
                    "before": "",
                    "after": "",
                    "detail": "No differences",
                },
            )
        )
    return lines


def _columns(width: float) -> list[float]:
    usable = width - 2 * MARGIN
    change, sheet, before, after = 28.0, 28.0, 38.0, 42.0
    item = usable - (change + sheet + before + after)
    return [change, sheet, max(36.0, item), before, after]


def _meta_text(project: Project, report: dict[str, Any]) -> str:
    tb = project.title_block
    left = str(report.get("left") or "")
    right = str(report.get("right") or "")
    arrow = f"{left}  →  {right}" if left or right else ""
    return "   ".join(part for part in (project.name, tb.drawing_number, arrow) if part)


def _header_layout(
    project: Project,
    report: dict[str, Any],
    width: float,
) -> tuple[float, list[tuple[float, str]], list[tuple[float, str]]]:
    usable = width - 2 * MARGIN
    meta = wrap_text(_meta_text(project, report), usable, 2.6)
    summary = wrap_text(_summary(report), usable, 2.3)
    meta_rows: list[tuple[float, str]] = []
    y = 11.4
    for line in meta:
        meta_rows.append((y, line))
        y += 3.5
    y += 0.8
    sum_rows: list[tuple[float, str]] = []
    for line in summary:
        sum_rows.append((y, line))
        y += 3.35
    header_h = max(HEADER_H, y + 2.2)
    return header_h, meta_rows, sum_rows


def _row_height(wrapped: list[list[str]]) -> float:
    count = max((len(lines) for lines in wrapped), default=1)
    if count <= 1:
        return ROW_H
    return 1.3 + count * LINE_PITCH


def prepare_compare(
    project: Project,
    report: dict[str, Any],
    width: float = PAGE_W,
) -> tuple[float, list[tuple[str, Any, float, list[list[str]]]]]:
    header_h, _meta, _summary_rows = _header_layout(project, report, width)
    widths = _columns(width)
    prepared: list[tuple[str, Any, float, list[list[str]]]] = []
    for kind, payload in display_lines(report):
        if kind == "banner":
            prepared.append(("banner", payload, BANNER_H, []))
            continue
        values = display_cells(payload)
        wrapped = [
            wrap_text(value, max(8.0, col - 2 * CELL_INSET), CELL_SIZE)
            for value, col in zip(values, widths, strict=True)
        ]
        prepared.append(("row", payload, _row_height(wrapped), wrapped))
    return header_h, prepared


def paginate_compare(
    prepared: list[tuple[str, Any, float, list[list[str]]]],
    height: float,
    header_h: float,
) -> list[list[tuple[str, Any, float, list[list[str]]]]]:
    usable = height - 2 * MARGIN - header_h - FOOTER_H - ROW_H
    pages: list[list[tuple[str, Any, float, list[list[str]]]]] = []
    current: list[tuple[str, Any, float, list[list[str]]]] = []
    used = 0.0
    index = 0
    while index < len(prepared):
        kind, _payload, need, _wrapped = prepared[index]
        extra = 0.0
        if kind == "banner" and index + 1 < len(prepared):
            extra = prepared[index + 1][2]
        if current and used + need + extra > usable:
            pages.append(current)
            current = []
            used = 0.0
            continue
        current.append(prepared[index])
        used += need
        index += 1
    if current:
        pages.append(current)
    return pages or [[]]


def paint_compare_page(
    qp: Any,
    project: Project,
    report: dict[str, Any],
    lines: list[tuple[str, Any, float, list[list[str]]]],
    *,
    page_index: int,
    page_count: int,
    width: float = PAGE_W,
    height: float = PAGE_H,
) -> None:
    from panelcad.render.painter import Painter

    if not isinstance(qp, Painter):
        raise TypeError("paint_compare_page expects a Painter")
    tb = project.title_block
    header_h, meta_rows, sum_rows = _header_layout(project, report, width)
    qp.set_stroke(RULE, 0.4)
    qp.set_fill(PAPER)
    qp.rect(MARGIN - 2, MARGIN - 2, width - 2 * MARGIN + 4, height - 2 * MARGIN + 4)
    qp.set_fill(None)
    x = MARGIN
    y = MARGIN
    qp.text(x, y + 5.2, "REVISION COMPARE", 5.0, align="start")
    for offset, line in meta_rows:
        qp.text(x, y + offset, line, 2.6, align="start")
    for offset, line in sum_rows:
        qp.text(x, y + offset, line, 2.3, align="start")
    qp.set_stroke(RULE, 0.35)
    qp.line(x, y + header_h - 1.5, width - MARGIN, y + header_h - 1.5)

    widths = _columns(width)
    headers = ["CHANGE", "SHEET", "ITEM", "BEFORE", "AFTER"]
    y = MARGIN + header_h
    header_lines = [
        wrap_text(value, col - 2 * CELL_INSET, 2.15)
        for value, col in zip(headers, widths, strict=True)
    ]
    _paint_cells(qp, x, y, widths, header_lines, ROW_H, header=True)
    y += ROW_H
    for kind, payload, row_h, wrapped in lines:
        if kind == "banner":
            qp.set_stroke(RULE, 0.25)
            qp.set_fill(BAND)
            qp.rect(x, y, sum(widths), BANNER_H)
            qp.set_fill(None)
            qp.text(x + 1.8, y + BANNER_H / 2.0 + 0.9, str(payload), 2.3, align="start")
            y += BANNER_H
            continue
        _paint_cells(qp, x, y, widths, wrapped, row_h, header=False)
        y += row_h

    foot_y = height - MARGIN
    qp.set_stroke(RULE, 0.25)
    qp.line(MARGIN, foot_y - 5.0, width - MARGIN, foot_y - 5.0)
    left_foot = "  ".join(part for part in (tb.company, tb.drawing_number) if part) or project.name
    qp.text(MARGIN, foot_y, left_foot, 2.2, align="start")
    qp.text(width - MARGIN, foot_y, f"{page_index + 1} / {page_count}", 2.2, align="end")


def _baselines(y: float, h: float, count: int) -> list[float]:
    if count <= 1:
        return [y + h / 2.0 + 0.72]
    block = count * LINE_PITCH
    top = y + (h - block) / 2.0
    return [top + (index + 0.78) * LINE_PITCH for index in range(count)]


def _paint_cells(
    qp: Any,
    x: float,
    y: float,
    widths: list[float],
    wrapped: list[list[str]],
    h: float,
    *,
    header: bool,
) -> None:
    cx = x
    size = 2.15 if header else CELL_SIZE
    for width, lines in zip(widths, wrapped, strict=True):
        qp.set_stroke(RULE, 0.22)
        qp.set_fill(HEADER_FILL if header else "#fbfaf3")
        qp.rect(cx, y, width, h)
        qp.set_fill(None)
        shown = lines or [""]
        for baseline, line in zip(_baselines(y, h, len(shown)), shown, strict=True):
            if line:
                qp.text(cx + CELL_INSET, baseline, line, size, align="start")
        cx += width


def _ensure_qt() -> None:
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication([])


def write_compare_pdf(path: Path | str, report: dict[str, Any], project: Project) -> Path:
    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.render.page import configure_full_page
    from panelcad.render.qt_painter import QtPainter

    _ensure_qt()
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    header_h, prepared = prepare_compare(project, report)
    pages = paginate_compare(prepared, PAGE_H, header_h)
    writer = QPdfWriter(str(dest))
    writer.setResolution(150)
    writer.setTitle(f"{project.name} revision compare")
    configure_full_page(writer, PAGE_W, PAGE_H)
    painter = QPainter(writer)
    try:
        for index, page in enumerate(pages):
            if index:
                writer.newPage()
            dpi = float(writer.logicalDpiX() or 150)
            painter.resetTransform()
            painter.scale(dpi / 25.4, dpi / 25.4)
            paint_compare_page(
                QtPainter(painter, print_paper=True),
                project,
                report,
                page,
                page_index=index,
                page_count=len(pages),
            )
    finally:
        if painter.isActive():
            painter.end()
    return dest


def render_compare_image(report: dict[str, Any], project: Project, dpi: float = 160.0):
    """Rasterize every compare page, stacked, for a screenshot."""

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    from panelcad.render.qt_painter import QtPainter

    _ensure_qt()
    header_h, prepared = prepare_compare(project, report)
    pages = paginate_compare(prepared, PAGE_H, header_h)
    scale = float(dpi) / 25.4
    gap = int(8 * scale)
    page_w = max(1, int(PAGE_W * scale))
    page_h = max(1, int(PAGE_H * scale))
    image = QImage(
        page_w,
        page_h * len(pages) + gap * (len(pages) - 1),
        QImage.Format.Format_ARGB32,
    )
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    try:
        for index, page in enumerate(pages):
            painter.save()
            painter.translate(0, index * (page_h + gap))
            painter.scale(scale, scale)
            paint_compare_page(
                QtPainter(painter, print_paper=True),
                project,
                report,
                page,
                page_index=index,
                page_count=len(pages),
            )
            painter.restore()
    finally:
        if painter.isActive():
            painter.end()
    return image
