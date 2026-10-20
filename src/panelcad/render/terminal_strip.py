"""Draw a terminal sheet as a to-scale DIN-rail strip, not as a schematic."""

from __future__ import annotations

from panelcad.core.models import Project, Sheet
from panelcad.core.terminal_strip import CALLOUT, layout_terminal_strip
from panelcad.library.catalog import Catalog
from panelcad.render.painter import Painter

_FILL = {
    "terminal": "#f7f4ea",
    "end_plate": "#eeeae0",
    "end_stop": "#d9d3c5",
    "partition": "#c8c2b4",
}
_PE = "#e5f0dc"
_WORD = {"end_plate": "END", "end_stop": "STOP", "partition": "PART"}


def draw_terminal_sheet(painter: Painter, sheet: Sheet, project: Project | None, catalog: Catalog) -> None:
    if project is None:
        project = Project(name=sheet.title or sheet.id, sheets=[sheet])
    name = str(sheet.strip or "").strip()
    painter.set_stroke("#1a1a1a", 0.35)
    painter.set_fill(None)
    if not name:
        painter.text(36, 36, "Terminal strip", 4.5, align="start")
        painter.text(36, 46, "Open a strip to draw the rail and the blocks.", 2.6, align="start")
        return
    layout = _for_part(layout_terminal_strip(project, catalog, name), int(sheet.strip_part or 1))
    items = layout["items"]
    part = int(layout.get("part") or 1)
    title = f"Terminal strip {name}" if part <= 1 else f"Terminal strip {name} (continued {part})"
    painter.text(36, 28, title, 4.2, align="start")
    if not items:
        painter.text(36, 42, "No terminals on this sheet.", 2.6, align="start")
        return
    _draw_rails(painter, items)
    for item in items:
        _draw_block(painter, item)
    for jumper in layout["jumpers"]:
        _draw_jumper(painter, jumper)


def _for_part(layout: dict, part: int) -> dict:
    return {
        **layout,
        "part": part,
        "items": [item for item in layout["items"] if int(item.get("part") or 1) == part],
        "jumpers": [jumper for jumper in layout["jumpers"] if int(jumper.get("part") or 1) == part],
    }


def _draw_rails(painter: Painter, items: list[dict]) -> None:
    rows: dict[int, list[dict]] = {}
    for item in items:
        rows.setdefault(int(item.get("row") or 0), []).append(item)
    first = min(items, key=lambda item: (float(item["y"]), float(item["x"])))
    painter.text(float(first["x"]) - 1.0, float(first["y"]) - 3.0, "Internal", 2.2, align="end")
    painter.text(
        float(first["x"]) - 1.0,
        float(first["y"]) + float(first["h"]) + CALLOUT - 2.0,
        "Field",
        2.2,
        align="end",
    )
    for row_items in rows.values():
        x0 = min(float(item["x"]) for item in row_items) - 1.0
        x1 = max(float(item["x"]) + float(item["w"]) for item in row_items) + 1.0
        bottom = max(float(item["y"]) + float(item["h"]) for item in row_items)
        rail_y = bottom + CALLOUT
        painter.set_fill("#c5c8cc")
        painter.set_stroke("#5c636a", 0.45)
        painter.rect(x0, rail_y, x1 - x0, 7.0)
        painter.rect(x0, rail_y - 1.6, x1 - x0, 1.6)
        painter.rect(x0, rail_y + 7.0, x1 - x0, 1.6)
        painter.set_fill(None)
        painter.text((x0 + x1) / 2.0, rail_y + 12.0, "TS35", 2.4, align="middle")


def _draw_block(painter: Painter, item: dict) -> None:
    x, y, w, h = float(item["x"]), float(item["y"]), float(item["w"]), float(item["h"])
    fill = _PE if item.get("pe") else _FILL.get(item["kind"], "#f7f4ea")
    painter.set_fill(fill)
    painter.set_stroke("#222222", 0.55 if item["kind"] == "end_stop" else 0.4)
    painter.rect(x, y, w, h)
    painter.set_fill(None)
    if item["kind"] == "end_stop":
        painter.set_stroke("#4a4a4a", 0.7)
        painter.line(x + w * 0.35, y + 4, x + w * 0.35, y + h - 4)
        painter.line(x + w * 0.65, y + 4, x + w * 0.65, y + h - 4)
    elif item.get("pe"):
        painter.set_stroke("#2e5a32", 0.45)
        mid = x + w / 2.0
        painter.line(mid - min(3.2, w * 0.35), y + h - 14, mid + min(3.2, w * 0.35), y + h - 14)
        painter.line(mid - min(2.2, w * 0.25), y + h - 11.5, mid + min(2.2, w * 0.25), y + h - 11.5)
        painter.line(mid - min(1.2, w * 0.15), y + h - 9, mid + min(1.2, w * 0.15), y + h - 9)
    if item["kind"] == "terminal":
        radius = min(1.05, w * 0.16)
        painter.set_fill("#333333")
        painter.set_stroke("#333333", 0.3)
        painter.circle(x + w / 2.0, y + min(8.0, h * 0.18), radius)
        painter.circle(x + w / 2.0, y + h - min(8.0, h * 0.18), radius)
        painter.set_fill(None)
    painter.set_stroke("#1a1a1a", 0.3)
    size = max(1.2, min(2.2, w * 0.62))
    if item["kind"] == "terminal":
        painter.text(x + w / 2.0, y, str(item.get("internal") or ""), 2.1, align="start", rotate=-90)
        painter.text(x + w / 2.0, y + h, str(item.get("external") or ""), 2.1, align="start", rotate=90)
        painter.text(x + w / 2.0, y + h / 2.0, item["ref"], size, align="middle", rotate=-90)
    else:
        painter.text(x + w / 2.0, y, item["ref"], size, align="start", rotate=-90)
        painter.text(x + w / 2.0, y + h / 2.0, _WORD.get(item["kind"], ""), size, align="middle", rotate=-90)


def _draw_jumper(painter: Painter, jumper: dict) -> None:
    y = float(jumper["y"])
    x1 = float(jumper["x1"])
    x2 = float(jumper["x2"])
    painter.set_stroke("#c45c26", 0.9)
    painter.set_fill(None)
    painter.line(x1, y, x2, y)
    for tick in jumper.get("ticks") or []:
        painter.line(float(tick), y, float(tick), y + 4.0)
    label = str(jumper.get("label") or jumper.get("id") or "")
    lx = float(jumper["label_x"]) if jumper.get("label_x") is not None else (x1 + x2) / 2.0
    ly = float(jumper["label_y"]) if jumper.get("label_y") is not None else y - 4.0
    size = float(jumper.get("label_size") or 2.0)
    painter.text(lx, ly, label, size, align="middle")
