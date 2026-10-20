"""One-page SCCR report: component table, the limiting devices, and the panel rating."""

from __future__ import annotations

from pathlib import Path

from panelcad.electrical.sccr import SccrResult, format_ka

PAGE_W = 215.9
PAGE_H = 279.4
_INK = "#1a1a1a"
_RULE = "#2a2a2a"
_HEADER = "#ece8dc"
_LIMIT = "#f6d6d6"
_PAPER = "#ffffff"


def _ensure_app() -> None:
    import os

    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QApplication([])


def paint_sccr_report(painter, result: SccrResult, project_name: str = "") -> None:
    """Draw the report in millimetres on a US Letter page."""

    painter.set_fill(_PAPER)
    painter.set_stroke(_PAPER, 0.2)
    painter.rect(0, 0, PAGE_W, PAGE_H)
    painter.set_fill(_INK)
    painter.set_stroke(_RULE, 0.3)
    painter.text(14, 14, "SHORT-CIRCUIT CURRENT RATING", 5.2, align="start")
    painter.text(
        14,
        20,
        "UL 508A Supplement SB4 — the panel rating is the lowest power-circuit result",
        2.4,
        align="start",
    )
    title = project_name or "Panel"
    painter.text(14, 26, title, 3.2, align="start")
    painter.text(14, 34, f"Panel SCCR  {result.panel_text}", 4.4, align="start")
    limit = ", ".join(result.limiting_refs) if result.limiting_refs else "none"
    painter.text(14, 40, f"Limited by  {limit}", 2.8, align="start")
    voltage = "not set" if result.voltage is None else f"{format_ka(result.voltage)} V"
    painter.text(14, 45, f"Voltage  {voltage}  ({result.voltage_source})", 2.3, align="start")
    if result.available_fault_ka is not None:
        note = "below available fault" if result.below_available else "meets available fault"
        painter.text(
            14,
            50,
            f"Available fault  {format_ka(result.available_fault_ka)} kA  —  {note}",
            2.3,
            align="start",
        )
    headers = ("Ref", "Device", "Circuit", "Base kA", "Effective", "Limits")
    widths = (22, 62, 24, 24, 28, 22)
    top = 56
    _table_header(painter, 12, top, widths, headers)
    y = top + 7
    row_h = 6.2
    for part in result.components:
        if y > PAGE_H - 36:
            painter.text(14, y + 3, "Additional components are omitted from this page.", 2.2, align="start")
            break
        if part.limits:
            painter.set_fill(_LIMIT)
            painter.set_stroke(_LIMIT, 0.1)
            painter.rect(12, y - 4.2, sum(widths), row_h)
        painter.set_fill(_INK)
        cells = (
            part.ref,
            _clip(part.name, 28),
            part.circuit or part.kind,
            format_ka(part.base_ka) or "—",
            "exempt" if part.exempt else (format_ka(part.effective_ka) or "—"),
            "YES" if part.limits else ("exempt" if part.exempt else ""),
        )
        x = 14
        for text, width in zip(cells, widths, strict=True):
            painter.text(x, y, text, 2.3, align="start")
            x += width
        y += row_h
    y += 4
    painter.set_stroke(_RULE, 0.25)
    painter.line(12, y, PAGE_W - 12, y)
    y += 6
    painter.set_fill(_INK)
    painter.text(14, y, "Notes", 2.8, align="start")
    y += 5
    notes = list(result.flags)
    if result.incomplete and result.known_minimum_ka is not None:
        notes.insert(
            0,
            f"Rating is incomplete. Lowest known value is {format_ka(result.known_minimum_ka)} kA.",
        )
    if not notes:
        notes = ["No extra flags. Unmarked components use Table SB4.1."]
    for note in notes[:8]:
        painter.text(14, y, _clip(note, 110), 2.0, align="start")
        y += 4
    painter.text(
        14,
        PAGE_H - 16,
        "Sources: Mersen TT-SCN1 Tables SB4.1 and SB4.2; Schneider CPTG005_EN;",
        1.8,
        align="start",
    )
    painter.text(
        14,
        PAGE_H - 12,
        "Rockwell SCCR-AT002; Eaton Bussmann SPD (2008); Siemens UL SCCR white paper (Feb 2018).",
        1.8,
        align="start",
    )
    painter.text(
        14,
        PAGE_H - 8,
        "User-entered where a reprint does not give a number. See the citation table.",
        1.8,
        align="start",
    )


def _table_header(painter, x: float, y: float, widths: tuple[int, ...], headers: tuple[str, ...]) -> None:
    painter.set_fill(_HEADER)
    painter.set_stroke(_RULE, 0.3)
    painter.rect(x, y - 4.4, sum(widths), 7)
    painter.set_fill(_INK)
    cursor = x + 2
    for text, width in zip(headers, widths, strict=True):
        painter.text(cursor, y, text, 2.3, align="start")
        cursor += width


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def write_sccr_pdf(path: Path | str, result: SccrResult, project_name: str = "") -> Path:
    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.render.page import configure_full_page
    from panelcad.render.qt_painter import QtPainter

    _ensure_app()
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(dest))
    writer.setResolution(144)
    writer.setTitle(f"{project_name} SCCR" if project_name else "SCCR")
    configure_full_page(writer, PAGE_W, PAGE_H)
    painter = QPainter(writer)
    try:
        dpi_x = float(writer.logicalDpiX() or 72)
        dpi_y = float(writer.logicalDpiY() or 72)
        painter.scale(dpi_x / 25.4, dpi_y / 25.4)
        paint_sccr_report(QtPainter(painter), result, project_name)
    finally:
        if painter.isActive():
            painter.end()
    return dest


def write_sccr_png(
    path: Path | str,
    result: SccrResult,
    project_name: str = "",
    *,
    scale: float = 8.0,
) -> Path:
    from PySide6.QtGui import QColor, QImage, QPainter

    from panelcad.render.qt_painter import QtPainter

    _ensure_app()
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(int(PAGE_W * scale), int(PAGE_H * scale), QImage.Format.Format_RGB32)
    image.fill(QColor(_PAPER))
    painter = QPainter(image)
    try:
        painter.scale(scale, scale)
        paint_sccr_report(QtPainter(painter), result, project_name)
    finally:
        if painter.isActive():
            painter.end()
    image.save(str(dest))
    return dest
