"""PDF of one troubleshooting session for the job file.

The page opens with the lockout and arc-flash text. Values are the ones the
guide already took from the drawing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.core.troubleshoot_guide import session_rows

PAGE_W = 215.9
PAGE_H = 279.4
_LEFT = 12.0
_RIGHT = PAGE_W - 12.0
_BOTTOM = PAGE_H - 14.0


def write_session_pdf(
    path: Path | str,
    payload: dict[str, Any],
    *,
    who: str = "",
    note: str = "",
    project_name: str = "",
) -> Path:
    """Vector PDF. Readings and causes are the rows of the session."""

    from PySide6.QtGui import QPainter, QPdfWriter
    from PySide6.QtWidgets import QApplication

    from panelcad.render.page import configure_full_page

    if QApplication.instance() is None:
        QApplication([])
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = session_rows(payload, who=who, note=note)
    writer = QPdfWriter(str(dest))
    writer.setResolution(144)
    title = project_name or str(payload.get("ref") or "Troubleshooting")
    writer.setTitle(f"{title} troubleshooting")
    configure_full_page(writer, PAGE_W, PAGE_H)
    painter = QPainter(writer)
    try:
        state = _Page(writer, painter)
        safety = payload.get("safety") or {}
        state.heading("TROUBLESHOOTING LOG")
        if project_name:
            state.line(project_name, 3.2)
        symptom = str(payload.get("symptom_title") or "")
        ref = str(payload.get("ref") or "")
        state.line(f"{symptom}  ·  {ref}".strip(" ·"), 3.4)
        state.line(str(safety.get("loto") or ""), 2.6)
        state.line(str(safety.get("arc_flash") or ""), 2.6)
        state.line(str(safety.get("drawing_disclaimer") or ""), 2.6)
        state.gap(1.5)
        state.line(str(payload.get("how") or ""), 2.5)
        path_line = str(payload.get("path") or "")
        if path_line:
            state.gap(1.0)
            state.line("Path", 3.0)
            state.line(path_line, 2.4)
        where = payload.get("where") or payload.get("cross_refs") or []
        if where:
            state.gap(1.0)
            state.line("Cross-references", 3.0)
            for item in list(where)[:12]:
                state.line(str(item), 2.3)
        state.gap(1.0)
        state.line(str(payload.get("instruction") or ""), 2.8)
        fault = str(payload.get("fault_text") or "")
        if fault:
            state.line(fault, 2.8)
        state.gap(1.5)
        state.line("Checks", 3.2)
        checks = [row for row in rows if row.get("row_kind") == "check"]
        if not checks:
            state.line("No checks for this device and symptom.", 2.4)
        for row in checks:
            result = row.get("result") or "—"
            expected = row.get("expected") or ""
            place = " ".join(
                part
                for part in (
                    row.get("device") or "",
                    f"terminal {row['terminal']}" if row.get("terminal") else "",
                    f"wire {row['wire']}" if row.get("wire") else "",
                    row.get("page_zone") or "",
                )
                if part
            )
            state.line(
                f"{row.get('order')}. {row.get('energized')}  {place}  {result}",
                2.5,
            )
            detail = " ".join(
                part
                for part in (
                    row.get("meter_leads") or "",
                    f"Expected {expected}." if expected else "",
                )
                if part
            )
            if detail:
                state.line(detail, 2.3)
        causes = [row for row in rows if row.get("row_kind") == "cause" and row.get("cause")]
        if causes:
            state.gap(1.5)
            state.line("Likely causes", 3.2)
            for row in causes[:12]:
                state.line(f"{row.get('order')}. {row.get('cause')}", 2.4)
        if who.strip() or note.strip():
            state.gap(1.5)
            state.line("Log", 3.2)
            if who.strip():
                state.line(f"Who: {who.strip()}", 2.4)
            if note.strip():
                state.line(note.strip(), 2.4)
    finally:
        if painter.isActive():
            painter.end()
    return dest


class _Page:
    def __init__(self, writer: Any, painter: Any) -> None:
        from PySide6.QtCore import QRectF

        from panelcad.render.page import PRINT_PAPER

        self.writer = writer
        self.painter = painter
        self.paper = PRINT_PAPER
        self.rect = QRectF
        self.y = 16.0
        self._start_page()

    def _start_page(self) -> None:
        dpi_x = float(self.writer.logicalDpiX() or 72)
        dpi_y = float(self.writer.logicalDpiY() or 72)
        self.painter.resetTransform()
        self.painter.scale(dpi_x / 25.4, dpi_y / 25.4)
        self.painter.fillRect(self.rect(0, 0, PAGE_W, PAGE_H), self.paper)
        from panelcad.render.qt_painter import QtPainter

        self.qp = QtPainter(self.painter, print_paper=True)
        self.y = 16.0

    def heading(self, text: str) -> None:
        self.line(text, 4.4)

    def gap(self, mm: float) -> None:
        self.y += mm

    def line(self, text: str, size: float) -> None:
        for piece in _wrap(text, 92):
            if self.y > _BOTTOM:
                self.writer.newPage()
                self._start_page()
            self.qp.text(_LEFT, self.y, piece, size, align="start", color="#111111")
            self.y += size + 1.6


def _wrap(text: str, width: int) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if len(trial) > width and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines
