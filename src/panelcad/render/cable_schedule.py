"""Cable schedule PDF: one schedule page, then the termination list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.core.cables import gland_text, pull_length, pull_rows, shield_label, termination_rows
from panelcad.core.models import Cable, Project
from panelcad.render.page import configure_full_page
from panelcad.render.qt_painter import QtPainter
from panelcad.version import __version__

PAGE_W = 279.4
PAGE_H = 215.9
MARGIN_X = 12.0
MARGIN_Y = 12.0
HEADER_H = 16.0
ROW_H = 7.2
FOOTER_H = 10.0


def write_cable_schedule_pdf(session: Any, path: Path | str) -> Path:
    """Landscape letter PDF. Page 1 is the schedule and pull list. Later pages list terminations."""

    from PySide6.QtGui import QPainter, QPdfWriter

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    project: Project = session.project
    writer = QPdfWriter(str(dest))
    writer.setResolution(300)
    writer.setTitle(f"{project.name} cable schedule")
    if hasattr(writer, "setCreator"):
        writer.setCreator(f"PanelCAD {__version__}")
    configure_full_page(writer, PAGE_W, PAGE_H)
    painter = QPainter(writer)
    try:
        pages = _pages(project)
        for index, paint in enumerate(pages):
            if index:
                writer.newPage()
            dpi_x = float(writer.logicalDpiX() or 72)
            dpi_y = float(writer.logicalDpiY() or 72)
            painter.resetTransform()
            painter.scale(dpi_x / 25.4, dpi_y / 25.4)
            qp = QtPainter(painter, print_paper=True)
            paint(qp, project, index + 1, len(pages))
    finally:
        if painter.isActive():
            painter.end()
    return dest


def _pages(project: Project):
    schedule = [cable for cable in project.cables if cable.kind != "interpanel"]
    pulls = pull_rows(project)
    terms = termination_rows(project)
    pages = [lambda qp, project, number, count: _paint_schedule(qp, project, schedule, pulls, number, count)]
    usable = PAGE_H - 2 * MARGIN_Y - HEADER_H - FOOTER_H - ROW_H
    per_page = max(1, int(usable / ROW_H))
    if not terms:
        return pages
    for start in range(0, len(terms), per_page):
        chunk = terms[start : start + per_page]
        pages.append(
            lambda qp, project, number, count, chunk=chunk: _paint_terminations(qp, project, chunk, number, count)
        )
    return pages


def _paint_schedule(
    qp: QtPainter,
    project: Project,
    cables: list[Cable],
    pulls: list[dict[str, str]],
    page: int,
    pages: int,
) -> None:
    _frame(qp)
    y = _header(qp, "CABLE SCHEDULE", project, "Field cables leaving the panel", page, pages)
    columns = [
        ("TAG", 16.0),
        ("FROM", 46.0),
        ("TO", 16.0),
        ("CABLE TYPE", 50.0),
        ("CND", 10.0),
        ("GAUGE", 14.0),
        ("SHIELD", 16.0),
        ("LENGTH", 38.0),
        ("GLAND", 32.0),
        ("ROUTE", 0.0),
    ]
    widths = _widths(columns)
    y = _row(qp, y, widths, [name for name, _w in columns], header=True)
    if not cables:
        y = _row(qp, y, widths, ["—", "", "", "No field cables", "", "", "", "", "", ""], header=False)
    for cable in cables:
        y = _row(
            qp,
            y,
            widths,
            [
                cable.tag,
                _from_text(cable),
                cable.to_device,
                cable.cable_type,
                str(cable.conductors or ""),
                cable.gauge,
                shield_label(cable),
                _length_text(cable),
                gland_text(cable),
                cable.route,
            ],
            header=False,
        )
    y += 4.0
    if pulls:
        qp.text(MARGIN_X, y + 4.0, "PULL LIST", 3.2, align="start")
        y += 6.0
        pull_cols = [
            ("CABLE TYPE", 90.0),
            ("GAUGE", 22.0),
            ("CABLES", 18.0),
            ("RUN", 28.0),
            ("PULL", 28.0),
            ("UNIT", 16.0),
            ("MISSING", 0.0),
        ]
        pw = _widths(pull_cols)
        y = _row(qp, y, pw, [name for name, _w in pull_cols], header=True)
        for row in pulls:
            y = _row(
                qp,
                y,
                pw,
                [
                    row["cable_type"],
                    row["gauge"],
                    row["cables"],
                    row["run_length"],
                    row["pull_length"],
                    row["unit"],
                    row["missing_length"],
                ],
                header=False,
            )
    cite = _shield_cite(cables)
    if cite:
        qp.text(MARGIN_X, PAGE_H - MARGIN_Y - 9.4, cite, 2.0, align="start")
    note = (
        "Pull length is the entered length plus slack. Part numbers and conductor colors "
        "stay blank unless entered or taken from the part library with a source URL."
    )
    qp.text(MARGIN_X, PAGE_H - MARGIN_Y - 6.5, note, 2.0, align="start")
    _footer(qp, project, page, pages)


def _paint_terminations(
    qp: QtPainter,
    project: Project,
    rows: list[dict[str, str]],
    page: int,
    pages: int,
) -> None:
    _frame(qp)
    y = _header(qp, "CABLE TERMINATIONS", project, "Conductor to terminal, both ends", page, pages)
    columns = [
        ("CABLE", 18.0),
        ("COND", 12.0),
        ("COLOR", 24.0),
        ("WIRE", 16.0),
        ("PANEL TERMINAL", 72.0),
        ("FIELD TERMINAL", 58.0),
        ("ROLE", 16.0),
        ("SPARE", 0.0),
    ]
    widths = _widths(columns)
    y = _row(qp, y, widths, [name for name, _w in columns], header=True)
    for row in rows:
        y = _row(
            qp,
            y,
            widths,
            [
                row["cable"],
                row["conductor"],
                row["color"],
                row["wire_number"],
                row["panel_terminal"],
                row["field_terminal"],
                row["role"],
                row["spare"],
            ],
            header=False,
        )
    _footer(qp, project, page, pages)


def _from_text(cable: Cable) -> str:
    terminals = ",".join(cable.from_terminals)
    if terminals:
        return terminals
    if cable.from_device:
        return cable.from_device
    return cable.from_strip


def _shield_cite(cables: list[Cable]) -> str:
    parts: list[str] = []
    if any(cable.shield_bond == "both" and cable.shield_source for cable in cables):
        parts.append("VFD shield: both ends, Rockwell 520-UM001")
    if any(
        cable.to_kind == "instrument" and cable.shield_bond == "panel" and cable.shield_source
        for cable in cables
    ):
        parts.append("Instrument shield: panel end only, Rockwell 1756-IN063")
    if not parts:
        return ""
    return ". ".join(parts) + "."


def _length_text(cable: Cable) -> str:
    if cable.length is None:
        return ""
    unit = cable.length_unit or "ft"
    run = _num(cable.length)
    pull = pull_length(cable)
    if cable.slack_pct:
        return f"{run} {unit} +{ _num(cable.slack_pct)}% ({_num(pull)} pull)"
    return f"{run} {unit}"


def _num(value: float | None) -> str:
    if value is None:
        return ""
    rounded = round(float(value), 2)
    if abs(rounded - int(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _widths(columns: list[tuple[str, float]]) -> list[float]:
    usable = PAGE_W - 2 * MARGIN_X
    fixed = sum(width for _name, width in columns)
    flex = max(12.0, usable - fixed)
    return [flex if width == 0.0 else width for _name, width in columns]


def _frame(qp: QtPainter) -> None:
    qp.set_stroke("#222222", 0.4)
    qp.set_fill("#ffffff")
    qp.rect(6.0, 6.0, PAGE_W - 12.0, PAGE_H - 12.0)


def _header(qp: QtPainter, title: str, project: Project, subtitle: str, page: int, pages: int) -> float:
    y = MARGIN_Y
    qp.set_stroke("#222222", 0.3)
    qp.set_fill(None)
    qp.text(MARGIN_X, y + 5.2, title, 5.0, align="start")
    standard = str(project.title_block.standard or "NEMA/ANSI")
    meta = f"{project.name}   {standard}   {page} of {pages}"
    qp.text(PAGE_W - MARGIN_X, y + 5.0, meta, 2.4, align="end")
    qp.text(MARGIN_X, y + 10.5, subtitle, 2.4, align="start")
    qp.line(MARGIN_X, y + HEADER_H, PAGE_W - MARGIN_X, y + HEADER_H)
    return y + HEADER_H


def _footer(qp: QtPainter, project: Project, page: int, pages: int) -> None:
    y = PAGE_H - MARGIN_Y - 1.5
    qp.set_stroke("#222222", 0.25)
    qp.line(MARGIN_X, y - 3.2, PAGE_W - MARGIN_X, y - 3.2)
    left = project.title_block.drawing_number or project.name
    qp.text(MARGIN_X, y, f"{left}   PanelCAD {__version__}", 2.1, align="start")
    qp.text(PAGE_W - MARGIN_X, y, f"{page} / {pages}", 2.1, align="end")


def _row(qp: QtPainter, y: float, widths: list[float], values: list[str], *, header: bool) -> float:
    from PySide6.QtCore import QRectF

    x = MARGIN_X
    qp.set_stroke("#222222", 0.25)
    qp.set_fill("#e6e6e6" if header else None)
    height = ROW_H
    for width, value in zip(widths, values):
        qp.rect(x, y, width, height)
        raw = qp.qp
        raw.save()
        raw.setClipRect(QRectF(x + 0.4, y + 0.2, max(1.0, width - 0.8), height - 0.4))
        qp.text(x + 1.0, y + height / 2.0 + 0.9, str(value or ""), 2.2 if not header else 2.05, align="start")
        raw.restore()
        x += width
    qp.set_fill(None)
    return y + height
