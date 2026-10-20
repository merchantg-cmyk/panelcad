"""One letter page per control supply: loads, totals, and margin."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from panelcad.core.power_budget import SupplyBudget
from panelcad.render.print_package import MARGIN_X, MARGIN_Y
from panelcad.render.qt_painter import QtPainter

PAGE_W = 215.9
PAGE_H = 279.4

_COLUMNS = (
    ("Ref", 24.0),
    ("Sheet", 32.0),
    ("Kind", 22.0),
    ("Steady", 28.0),
    ("Inrush", 34.0),
    ("Source", 0.0),
)


def write_power_budget_pdf(path: Path | str, budgets: list[SupplyBudget], *, title: str = "") -> Path:
    """Vector PDF. One page for each supply."""

    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.render.page import PRINT_PAPER, configure_full_page

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pages = list(budgets) or []
    writer = QPdfWriter(str(dest))
    writer.setResolution(300)
    writer.setTitle(title or "Control power budget")
    configure_full_page(writer, PAGE_W, PAGE_H)
    painter = QPainter(writer)
    try:
        if not pages:
            _blank(painter, title or "Control power budget")
        for index, budget in enumerate(pages):
            if index:
                writer.newPage()
            dpi_x = float(writer.logicalDpiX() or 72)
            dpi_y = float(writer.logicalDpiY() or 72)
            painter.resetTransform()
            painter.scale(dpi_x / 25.4, dpi_y / 25.4)
            painter.fillRect(QRectF(0, 0, PAGE_W, PAGE_H), PRINT_PAPER)
            paint_supply_page(
                QtPainter(painter, print_paper=True),
                budget,
                title=title,
                page_index=index,
                page_count=max(1, len(pages)),
            )
    finally:
        if painter.isActive():
            painter.end()
    return dest


def paint_supply_page(
    qp: QtPainter,
    budget: SupplyBudget,
    *,
    title: str = "",
    page_index: int = 0,
    page_count: int = 1,
    width: float = PAGE_W,
    height: float = PAGE_H,
) -> None:
    qp.rect(MARGIN_X, MARGIN_Y, width - 2 * MARGIN_X, height - 2 * MARGIN_Y)
    y = MARGIN_Y + 8.0
    qp.text(width / 2.0, y, "CONTROL POWER BUDGET", 4.2)
    y += 6.0
    if title:
        qp.text(width / 2.0, y, title, 2.6)
        y += 5.0
    kind = "24 V DC supply" if budget.kind == "dc" else "AC control transformer"
    catalog = f"  {budget.catalog}" if budget.catalog else ""
    qp.text(MARGIN_X + 4.0, y, f"{budget.ref}  {kind}{catalog}  sheet {budget.sheet}", 2.8, align="start")
    y += 5.0
    rating = _rating_line(budget)
    qp.text(MARGIN_X + 4.0, y, rating, 2.4, align="start")
    y += 4.5
    if budget.kind == "ac" and budget.method_note:
        qp.text(MARGIN_X + 4.0, y, budget.method_note, 2.2, align="start")
        y += 4.5
    y += 2.0
    usable = width - 2 * MARGIN_X - 8.0
    widths = _widths(usable)
    x0 = MARGIN_X + 4.0
    y = _header_row(qp, x0, y, widths)
    for load in budget.loads:
        y = _load_row(qp, x0, y, widths, load)
        if y > height - 48.0:
            break
    if not budget.loads:
        qp.text(x0, y + 6.0, "No loads traced on this supply.", 2.4, align="start")
        y += 8.0
    y += 4.0
    qp.line(x0, y, x0 + sum(widths), y)
    y += 5.0
    totals = _totals_line(budget)
    qp.text(x0, y, totals, 2.5, align="start")
    y += 5.0
    y = _utilization_bar(qp, x0, y, min(90.0, sum(widths)), budget)
    margin_pct = int(round(budget.margin * 100))
    state = "within" if budget.ok_margin else "OVER"
    qp.text(x0, y, f"Steady load is {state} the {margin_pct}% margin.", 2.4, align="start")
    y += 4.5
    if budget.kind == "ac":
        if budget.inrush_limit_va is not None:
            flag = "within" if budget.ok_inrush else "OVER"
            qp.text(
                x0,
                y,
                f"Simultaneous inrush {_num(budget.inrush_va)} VA is {flag} the chart limit {_num(budget.inrush_limit_va)} VA.",
                2.3,
                align="start",
            )
        elif budget.method_missing:
            qp.text(x0, y, "No published inrush method for this transformer. Nameplate VA is not an inrush rating.", 2.3, align="start")
        y += 4.5
    text_width = sum(widths)
    if budget.protection:
        for row in budget.protection:
            y = _paragraph(qp, x0, y, str(row.get("message") or ""), 2.2, text_width, height)
    elif budget.protection_note:
        y = _paragraph(qp, x0, y, budget.protection_note, 2.2, text_width, height)
    if budget.boost_note:
        qp.text(x0, y, budget.boost_note, 2.2, align="start")
        y += 4.2
    if budget.suggestion:
        qp.text(x0, y, _suggestion_line(budget), 2.4, align="start")
        y += 4.5
    estimates = [load.ref for load in budget.loads if load.estimate]
    if estimates:
        qp.text(x0, y, "Estimate (not a manufacturer rating): " + ", ".join(estimates), 2.2, align="start")
    qp.text(width / 2.0, height - MARGIN_Y - 4.0, f"Page {page_index + 1} of {page_count}", 2.2)


def _blank(painter: Any, title: str) -> None:
    from PySide6.QtCore import QRectF

    from panelcad.render.page import PRINT_PAPER
    from panelcad.render.qt_painter import QtPainter

    dpi_x = 300.0
    dpi_y = 300.0
    painter.scale(dpi_x / 25.4, dpi_y / 25.4)
    painter.fillRect(QRectF(0, 0, PAGE_W, PAGE_H), PRINT_PAPER)
    qp = QtPainter(painter, print_paper=True)
    qp.text(PAGE_W / 2.0, 40.0, title, 4.0)
    qp.text(PAGE_W / 2.0, 50.0, "No control supply on this drawing.", 2.6)


def _rating_line(budget: SupplyBudget) -> str:
    bits = [f"{_num(budget.voltage)} V"]
    if budget.rating_w is not None:
        bits.append(f"{_num(budget.rating_w)} W")
    if budget.rating_a is not None:
        bits.append(f"{_num(budget.rating_a)} A")
    if budget.rating_va is not None:
        bits.append(f"{_num(budget.rating_va)} VA")
    limit = budget.limit_w if budget.kind == "dc" else budget.limit_va
    unit = "W" if budget.kind == "dc" else "VA"
    if limit is not None:
        bits.append(f"steady limit {_num(limit)} {unit} at {int(round(budget.margin * 100))}%")
    return "Rating  " + "   ".join(bits)


def _totals_line(budget: SupplyBudget) -> str:
    from panelcad.core.power_budget import utilization_text

    text = utilization_text(budget)
    loads = f"{len(budget.loads)} load" if len(budget.loads) == 1 else f"{len(budget.loads)} loads"
    if budget.kind == "dc":
        amps = f"   {_num(budget.steady_a)} A"
        return f"{text}{amps}   {loads}" if text else f"Total  {_num(budget.steady_w)} W{amps}   {loads}"
    sealed = f"{_num(budget.steady_va)} VA sealed"
    if text:
        return f"{text}   {sealed}   {loads}"
    return f"Total  {sealed}   {_num(budget.steady_w)} W   {loads}"


def _utilization_bar(qp: QtPainter, x: float, y: float, width: float, budget: SupplyBudget) -> float:
    from panelcad.core.power_budget import _loading

    fraction = _loading(budget)
    if fraction is None:
        return y
    track_h = 3.4
    qp.set_stroke("#222222", 0.25)
    qp.set_fill("#e6e6e6")
    qp.rect(x, y, width, track_h)
    used = max(0.0, min(1.0, float(fraction)))
    fill = "#8b1a1a" if float(fraction) > budget.margin + 1e-9 else "#1a1a1a"
    qp.set_fill(fill)
    qp.rect(x, y, max(0.4, used * width), track_h)
    tick = max(0.0, min(1.0, budget.margin)) * width
    qp.set_fill(None)
    qp.set_stroke("#1a1a1a", 0.45)
    qp.line(x + tick, y - 0.8, x + tick, y + track_h + 0.8)
    qp.set_stroke("#111111", 0.35)
    qp.set_fill(None)
    return y + track_h + 3.2


def _paragraph(
    qp: QtPainter,
    x: float,
    y: float,
    text: str,
    size: float,
    width: float,
    page_h: float,
) -> float:
    """Draw wrapped sentences. Stops above the page footer."""

    limit = page_h - MARGIN_Y - 8.0
    # Cap height is `size` mm. A typical glyph is a bit over half of that.
    count = max(36, int(width / (size * 0.56)))
    for raw in str(text or "").splitlines():
        for line in _wrap(raw, count):
            if y > limit:
                return y
            qp.text(x, y, line, size, align="start")
            y += size + 1.5
    return y


def _wrap(text: str, count: int) -> list[str]:
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if len(trial) <= count:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _suggestion_line(budget: SupplyBudget) -> str:
    suggestion = budget.suggestion or {}
    catalog = str(suggestion.get("catalog") or "").strip()
    maker = str(suggestion.get("manufacturer") or "").strip()
    if catalog:
        return f"Next size up: {maker} {catalog}".strip()
    return str(suggestion.get("reason") or "")


def _widths(usable: float) -> list[float]:
    fixed = sum(width for _label, width in _COLUMNS if width)
    flex = max(36.0, usable - fixed)
    return [flex if width == 0.0 else width for _label, width in _COLUMNS]


def _header_row(qp: QtPainter, x: float, y: float, widths: list[float]) -> float:
    cursor = x
    for (label, _width), width in zip(_COLUMNS, widths, strict=True):
        qp.rect(cursor, y, width, 6.0)
        qp.text(cursor + 1.2, y + 4.0, label, 2.1, align="start")
        cursor += width
    return y + 6.0


def _load_row(qp: QtPainter, x: float, y: float, widths: list[float], load: Any) -> float:
    if load.kind in {"coil", "relay"} and load.steady_va and load.estimate is False:
        steady = f"{_num(load.steady_va)} VA"
    else:
        steady = f"{_num(load.steady_w)} W"
    if load.inrush_published and load.inrush_va:
        inrush = f"{_num(load.inrush_va)} VA"
    else:
        inrush = load.inrush_note or "not published"
    source = "estimate" if load.estimate else (load.catalog or "catalog")
    values = [load.ref, load.sheet, load.kind, steady, inrush, source]
    cursor = x
    for value, width in zip(values, widths, strict=True):
        qp.rect(cursor, y, width, 6.2)
        qp.text(cursor + 1.2, y + 4.1, str(value), 2.05, align="start")
        cursor += width
    return y + 6.2


def _num(value: Any) -> str:
    if value is None or value == "":
        return "—"
    number = float(value)
    if abs(number) >= 100:
        return f"{number:.0f}"
    if abs(number) >= 10:
        return f"{number:.1f}"
    return f"{number:.3f}".rstrip("0").rstrip(".")
