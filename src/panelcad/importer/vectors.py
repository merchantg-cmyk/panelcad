"""Read vector ink and attach each tag to the symbol that was actually drawn.

A tag prefix is not a symbol. The strokes under the tag have to match a
library symbol's geometry. Wire numbers follow the traced conductor to the
pins at its ends. An end that could be two pins stays unconnected.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

PT_TO_MM = 25.4 / 72.0
MATCH_MIN = 0.72
PIN_SNAP_MM = 2.4
# A conductor is split only where a vertex actually sits on a terminal.
# The snap radius is wider, and using it here cuts the run a couple of
# millimetres past the pin, leaving a leaf between two terminals.
PIN_BREAK_MM = 0.8
LABEL_MM = 16.0


def strokes_from_pdf(path: Path | str, page_index: int) -> list[dict[str, Any]]:
    import pymupdf

    from panelcad.importer.read import release_mupdf_store

    doc = pymupdf.open(str(path))
    try:
        return strokes_from_page(doc[page_index])
    finally:
        doc.close()
        release_mupdf_store()


def strokes_from_page(page: Any) -> list[dict[str, Any]]:
    """Vector ink from an already-open page. The caller closes the document."""

    width = float(page.rect.width) * PT_TO_MM
    height = float(page.rect.height) * PT_TO_MM
    strokes: list[dict[str, Any]] = []
    raw_lines: list[dict[str, Any]] = []
    for drawing in page.get_drawings():
        role = _role(drawing.get("color"), drawing.get("width"))
        width_pt = _width_pt(drawing.get("width"))
        color = drawing.get("color")
        for item in drawing.get("items") or []:
            kind = item[0]
            if kind == "l":
                a = _pt(item[1])
                b = _pt(item[2])
                line_role = stroke_role(color, drawing.get("width"))
                raw_lines.append(
                    {
                        "a": a,
                        "b": b,
                        "role": line_role,
                        "pt": width_pt,
                        "color": color,
                        "span": _span(a, b),
                    }
                )
            elif kind == "re":
                rect = item[1]
                x0, y0 = float(rect.x0) * PT_TO_MM, float(rect.y0) * PT_TO_MM
                x1, y1 = float(rect.x1) * PT_TO_MM, float(rect.y1) * PT_TO_MM
                if omit_border_rect(abs(x1 - x0), abs(y1 - y0)):
                    continue
                rx, ry = min(x0, x1), min(y0, y1)
                rw, rh = abs(x1 - x0), abs(y1 - y0)
                strokes.append({"kind": "rect", "role": role, "x": _r(rx), "y": _r(ry), "w": _r(rw), "h": _r(rh)})
                corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                for p, q in zip(corners, corners[1:] + corners[:1], strict=True):
                    aa = [_r(p[0]), _r(p[1])]
                    bb = [_r(q[0]), _r(q[1])]
                    raw_lines.append(
                        {
                            "a": aa,
                            "b": bb,
                            "role": role,
                            "pt": width_pt,
                            "color": color,
                            "span": _span(aa, bb),
                            "from_rect": True,
                        }
                    )
            elif kind == "c":
                points = [_pt(p) for p in item[1:]]
                if points:
                    strokes.append({"kind": "curve", "role": role, "points": points})
    _promote_conductors(raw_lines)
    _promote_orthogonal(raw_lines)
    for line in raw_lines:
        # Sheet borders are long. A conductor across the sheet is long too,
        # and dropping it splits the net in the middle of the run.
        if omit_border_line(line["role"], line["a"], line["b"]):
            continue
        item = {"kind": "line", "role": line["role"], "a": line["a"], "b": line["b"]}
        if line.get("bus"):
            item["bus"] = True
        if line.get("geom"):
            item["geom"] = True
        strokes.append(item)
    _ = (width, height)
    return strokes


def strokes_from_dxf(path: Path | str) -> list[dict[str, Any]]:
    """Linework from the first DXF layout, in the same space as ``tokens_from_dxf``.

    ``_dxf_page`` stores the resolved lineweight and promotes conductors, so this
    is that page's strokes. Every layout gets the same treatment.
    """

    from panelcad.importer.read import dxf_pages

    pages = dxf_pages(path)
    if not pages:
        return []
    return list(pages[0].get("strokes") or [])


def resolve_lineweight_mm(entity: Any, *, doc: Any = None, insert_weight: float | None = None) -> float:
    """Millimetres from the entity, then its layer, then the INSERT, then 0.25 mm.

    DXF stores a lineweight in hundredths of a millimetre. -1 is BYLAYER and
    -2 is BYBLOCK. ``$LWDEFAULT`` is not a header variable, so a weight that
    is still unset falls back to 0.25 mm.
    """

    try:
        weight = int(entity.dxf.lineweight)
    except (AttributeError, TypeError, ValueError):
        weight = -3
    if weight == -1 and doc is not None:
        layer_name = str(getattr(entity.dxf, "layer", "") or "0")
        try:
            layer = doc.layers.get(layer_name)
            weight = int(layer.dxf.lineweight)
        except (AttributeError, KeyError, TypeError, ValueError):
            weight = -3
    if weight == -2:
        if insert_weight is not None and insert_weight > 0:
            return float(insert_weight)
        weight = -3
    if weight < 0:
        default = 25
        if doc is not None:
            try:
                default = int(doc.header.get("$LWDEFAULT", 25) or 25)
            except (TypeError, ValueError):
                default = 25
        weight = default if default > 0 else 25
    return max(weight, 0) / 100.0


def _width_pt(width: Any) -> float:
    try:
        return float(width)
    except (TypeError, ValueError):
        return 0.0


def _dark_ink(color: Any) -> bool:
    if not color or not isinstance(color, (list, tuple)) or isinstance(color[0], (list, tuple)):
        return False
    red, green, blue = (float(color[0]), float(color[1]), float(color[2]))
    # Grey border ink is darker than a frame tint and lighter than a conductor.
    return red < 0.14 and green < 0.14 and blue < 0.14


def _promote_conductors(lines: list[dict[str, Any]]) -> None:
    """A dark stroke thicker than the page's symbol ink is a conductor.

    The thin cluster is the symbol. A stroke about a fifth thicker is the
    wire. A stroke about twice that, and long, is a rail. The cut is the
    page's own widths, so a 0.45 mm wire and a 0.55 mm ladder wire both count
    without a number taken from one drawing.
    """

    dark = [
        float(line["pt"])
        for line in lines
        if line.get("pt") and not line.get("from_rect") and _dark_ink(line.get("color"))
    ]
    if not dark:
        return
    clusters: dict[float, int] = {}
    for width in dark:
        key = round(width, 2)
        clusters[key] = clusters.get(key, 0) + 1
    # The symbol is the stroke that appears most. A thinner hairline (a pin
    # tick, a text underline) must not pull every symbol line into the wires.
    base = max(clusters, key=lambda width: (clusters[width], -width))
    if clusters[base] < 2 or base <= 0:
        return
    cutoff = base * 1.22
    bus_cutoff = base * 2.2
    for line in lines:
        if line.get("from_rect") or not _dark_ink(line.get("color")):
            continue
        width = float(line.get("pt") or 0)
        if width < cutoff:
            continue
        line["role"] = "wire"
        if width >= bus_cutoff and float(line.get("span") or 0) >= 40.0:
            line["bus"] = True
    _demote_outer_border(lines)


def _line_ends(line: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    return (
        (float(line["a"][0]), float(line["a"][1])),
        (float(line["b"][0]), float(line["b"][1])),
    )


def _orthogonal_line(line: dict[str, Any]) -> bool:
    """A straight horizontal or vertical run. Symbol ticks are short and often diagonal."""

    start, end = _line_ends(line)
    dx, dy = abs(end[0] - start[0]), abs(end[1] - start[1])
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1.0:
        return False
    return min(dx, dy) <= max(0.8, 0.08 * length)


def _page_span(lines: list[dict[str, Any]]) -> float:
    xs: list[float] = []
    ys: list[float] = []
    for line in lines:
        if "a" not in line or "b" not in line:
            continue
        start, end = _line_ends(line)
        xs.extend((start[0], end[0]))
        ys.extend((start[1], end[1]))
    if len(xs) < 2:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


class _PointGrid:
    """Endpoints bucketed by cell, so a query does not walk every stroke."""

    def __init__(self, points: list[tuple[float, float]], cell: float = 8.0) -> None:
        self.cell = cell
        self.buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
        for point in points:
            key = (int(point[0] // cell), int(point[1] // cell))
            self.buckets.setdefault(key, []).append(point)

    def near(self, x0: float, y0: float, x1: float, y1: float, pad: float = 0.0) -> list[tuple[float, float]]:
        cell = self.cell
        left, right = min(x0, x1) - pad, max(x0, x1) + pad
        bottom, top = min(y0, y1) - pad, max(y0, y1) + pad
        ix0, ix1 = int(left // cell), int(right // cell)
        iy0, iy1 = int(bottom // cell), int(top // cell)
        found: list[tuple[float, float]] = []
        seen: set[int] = set()
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                for point in self.buckets.get((ix, iy), ()):
                    marker = id(point)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    found.append(point)
        return found


def _endpoint_grid(wires: list[dict[str, Any]]) -> _PointGrid:
    points: list[tuple[float, float]] = []
    for wire in wires:
        if "a" not in wire or "b" not in wire:
            continue
        points.extend(_line_ends(wire))
    return _PointGrid(points)


def _tee_count(line: dict[str, Any], wires: list[dict[str, Any]], grid: _PointGrid | None = None) -> int:
    """How many other conductors meet this run along its length, not only at its ends."""

    start, end = _line_ends(line)
    length = _dist(start, end)
    if length < 8.0:
        return 0
    if grid is None:
        grid = _endpoint_grid(wires)
    margin = max(2.0, length * 0.04)
    tees = 0
    for point in grid.near(start[0], start[1], end[0], end[1], pad=0.8):
        if _point_seg(point, start, end) > 0.8:
            continue
        if _dist(point, start) <= margin or _dist(point, end) <= margin:
            continue
        tees += 1
    return tees


def _promote_orthogonal(lines: list[dict[str, Any]]) -> None:
    """Long straight runs are conductors, whatever their colour and width.

    Colour and the page's stroke widths are a hint, applied first. When that
    hint did not find a ladder, the long orthogonal ink is the conductors.
    A long run that a rung meets is the rail. The sheet rectangle stays a frame.
    """

    plain = [line for line in lines if not line.get("from_rect")]
    # Colour or the page's own widths already named the conductors. A long
    # thin stroke is then the border, not another wire.
    if any(line.get("role") == "wire" and not line.get("geom") for line in plain):
        return
    span = _page_span(plain)
    if span <= 1.0:
        return
    cutoff = max(12.0, span * 0.035)
    promoted = False
    for line in plain:
        if line.get("role") in {"wire", "frame"}:
            continue
        length = float(line.get("span") or 0)
        if length < cutoff or not _orthogonal_line(line):
            continue
        line["role"] = "wire"
        line["geom"] = True
        promoted = True
    if not promoted:
        return
    _demote_outer_border(lines)
    pool = [line for line in plain if line.get("role") == "wire"]
    grid = _endpoint_grid(pool)
    for line in plain:
        if not line.get("geom") or line.get("role") != "wire":
            continue
        length = float(line.get("span") or 0)
        if length >= max(40.0, span * 0.22) and _tee_count(line, pool, grid) >= 1:
            line["bus"] = True


def _demote_outer_border(lines: list[dict[str, Any]]) -> None:
    """The sheet rectangle is the outer edge of the drawing, not a rail."""

    points: list[tuple[float, float]] = []
    for line in lines:
        if "a" not in line or "b" not in line:
            continue
        points.append((float(line["a"][0]), float(line["a"][1])))
        points.append((float(line["b"][0]), float(line["b"][1])))
    if len(points) < 2:
        return
    min_x = min(point[0] for point in points)
    max_x = max(point[0] for point in points)
    min_y = min(point[1] for point in points)
    max_y = max(point[1] for point in points)
    span = max(max_x - min_x, max_y - min_y)
    if span <= 1.0:
        return
    tol = span * 0.015
    outer: list[tuple[dict[str, Any], bool, bool]] = []
    for line in lines:
        if line.get("role") != "wire":
            continue
        ax, ay = float(line["a"][0]), float(line["a"][1])
        bx, by = float(line["b"][0]), float(line["b"][1])
        length = float(line.get("span") or _dist((ax, ay), (bx, by)))
        if length < span * 0.45:
            continue
        on_x = (abs(ax - min_x) <= tol and abs(bx - min_x) <= tol) or (
            abs(ax - max_x) <= tol and abs(bx - max_x) <= tol
        )
        on_y = (abs(ay - min_y) <= tol and abs(by - min_y) <= tol) or (
            abs(ay - max_y) <= tol and abs(by - max_y) <= tol
        )
        if on_x or on_y:
            outer.append((line, on_x, on_y))
    # A sheet frame has both directions. A ladder's rails are the outermost
    # verticals and are not a frame by themselves. A page whose conductors
    # were already separated by colour or width keeps the older rule, so a
    # border stroke is not left in the net.
    geom = any(line.get("geom") for line in lines)
    if geom:
        if sum(1 for _line, on_x, on_y in outer if on_x) < 1:
            return
        if sum(1 for _line, on_x, on_y in outer if on_y) < 1:
            return
        if len(outer) < 3:
            return
    for line, _on_x, _on_y in outer:
        line["role"] = "frame"
        line["bus"] = False


def finish_conductor_strokes(strokes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Promote conductors from the page's own widths, then drop sheet borders.

    A line may carry ``pt``, ``color``, ``span``, and ``from_rect`` for that
    pass. Those keys are not part of the stroke the rest of the importer sees.
    """

    raw: list[dict[str, Any]] = []
    for stroke in strokes:
        if stroke.get("kind") != "line" or "a" not in stroke or "b" not in stroke:
            continue
        a, b = stroke["a"], stroke["b"]
        raw.append(
            {
                "a": a,
                "b": b,
                "role": str(stroke.get("role") or "ink"),
                "pt": float(stroke.get("pt") or 0),
                "color": stroke.get("color"),
                "span": float(stroke.get("span") or _span(a, b)),
                "from_rect": bool(stroke.get("from_rect")),
                "_stroke": stroke,
            }
        )
    if raw:
        _promote_conductors(raw)
        _promote_orthogonal(raw)
        for line in raw:
            stroke = line["_stroke"]
            stroke["role"] = line["role"]
            if line.get("bus"):
                stroke["bus"] = True
            else:
                stroke.pop("bus", None)
            if line.get("geom"):
                stroke["geom"] = True
            else:
                stroke.pop("geom", None)
    hidden = {"pt", "color", "span", "from_rect"}
    kept: list[dict[str, Any]] = []
    for stroke in strokes:
        if (
            stroke.get("kind") == "line"
            and "a" in stroke
            and "b" in stroke
            and omit_border_line(str(stroke.get("role") or ""), stroke["a"], stroke["b"])
        ):
            continue
        kept.append({key: value for key, value in stroke.items() if key not in hidden})
    return kept


def _promote_by_geometry(
    lines: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    catalog: Any,
    curves: list[dict[str, Any]] | None = None,
) -> None:
    """When every stroke is the same weight, wires are the lines outside the symbols.

    A page whose conductors are as thin as the symbol ink still has terminals.
    Lines inside a matched symbol, or inside an unmatched tag's ink cluster,
    stay ink. The long runs that remain are rails. A short stub that leaves a
    cluster and meets a conductor is a wire too, so the run can land.
    """

    plain = [line for line in lines if line.get("kind", "line") == "line" and not line.get("from_rect")]
    if len(plain) < 2:
        return
    geom = any(line.get("geom") for line in plain)
    wires = [line for line in plain if line.get("role") == "wire"]
    boxes = _symbol_boxes(devices, catalog)
    for cluster in _ink_clusters(plain, devices, curves or []):
        boxes.append(cluster["box"])
        cluster["device"]["ink_box"] = list(cluster["box"])
    # Stroke width already separated the conductors. Leave that classification,
    # including which runs are rails. Geometry still records unmatched clusters
    # above, and pulls in stubs only when this page was classified by shape.
    if len(wires) >= 4 and not geom:
        return
    if len(wires) < 4 and boxes:
        promoted: list[dict[str, Any]] = []
        for line in plain:
            if line.get("role") == "wire":
                continue
            ax, ay = float(line["a"][0]), float(line["a"][1])
            bx, by = float(line["b"][0]), float(line["b"][1])
            if _midpoint_inside(ax, ay, bx, by, boxes):
                continue
            line["role"] = "wire"
            line["geom"] = True
            promoted.append(line)
        pool = [line for line in plain if line.get("role") == "wire"]
        grid = _endpoint_grid(pool)
        for line in promoted:
            if _collects_drops(line, pool, grid):
                line["bus"] = True
    if not geom and not any(line.get("geom") for line in plain):
        return
    pool = [line for line in plain if line.get("role") == "wire"]
    bodies = _SegIndex([_line_ends(wire) for wire in pool if "a" in wire and "b" in wire])
    for line in plain:
        if line.get("role") == "wire" or line.get("from_rect"):
            continue
        if not _orthogonal_line(line):
            continue
        ax, ay = float(line["a"][0]), float(line["a"][1])
        bx, by = float(line["b"][0]), float(line["b"][1])
        if boxes and _midpoint_inside(ax, ay, bx, by, boxes):
            continue
        if _touches_indexed(line, bodies, 1.4) and _end_near_box(line, boxes, 6.0):
            line["role"] = "wire"
            line["geom"] = True


def _touches_indexed(line: dict[str, Any], bodies: Any, tol: float) -> bool:
    start, end = _line_ends(line)
    for other_a, other_b in bodies.near((start, end), tol):
        if min(_point_seg(start, other_a, other_b), _point_seg(end, other_a, other_b)) <= tol:
            return True
    return False


def _end_near_box(
    line: dict[str, Any],
    boxes: list[tuple[float, float, float, float]],
    tol: float,
) -> bool:
    if not boxes:
        return False
    for point in _line_ends(line):
        for box in boxes:
            if _dist_to_box(point, box) <= tol:
                return True
    return False


def _dist_to_box(point: tuple[float, float], box: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = box
    cx = min(max(point[0], x0), x1)
    cy = min(max(point[1], y0), y1)
    return _dist(point, (cx, cy))


def _ink_clusters(
    lines: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    curves: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """A box around the ink of a device whose symbol did not match the library."""

    ink = [
        line
        for line in lines
        if line.get("kind", "line") == "line" and line.get("role") != "wire" and not line.get("from_rect")
    ]
    guesses = [device for device in devices if not (device.get("library_id") and not device.get("guess"))]

    def anchor(device: dict[str, Any]) -> tuple[float, float]:
        if device.get("text_x") is not None and device.get("text_y") is not None:
            return float(device["text_x"]), float(device["text_y"])
        return float(device.get("x") or 0), float(device.get("y") or 0)

    clusters = []
    for device in guesses:
        x, y = anchor(device)
        points = [(x, y)]
        # Both ends have to be the symbol. A stroke that reaches the next rung is not.
        for line in ink:
            start = (float(line["a"][0]), float(line["a"][1]))
            end = (float(line["b"][0]), float(line["b"][1]))
            if _dist(start, end) > 14.0:
                continue
            if _dist(start, (x, y)) <= 12.0 and _dist(end, (x, y)) <= 12.0:
                points.append(start)
                points.append(end)
        for curve in curves or []:
            samples = [(float(point[0]), float(point[1])) for point in curve.get("points") or []]
            if not samples:
                continue
            # The tag sits above its circle. A sample next to the tag keeps that circle only.
            if min(_dist(point, (x, y)) for point in samples) <= 12.0:
                points.extend(samples)
        if len(points) < 3:
            box = (x - 6.0, y - 6.0, x + 6.0, y + 6.0)
        else:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            box = (min(xs) - 1.5, min(ys) - 1.5, max(xs) + 1.5, max(ys) + 1.5)
        clusters.append({"tag": str(device.get("tag") or ""), "box": box, "device": device})
    return clusters


def _collects_drops(line: dict[str, Any], wires: list[dict[str, Any]], grid: _PointGrid | None = None) -> bool:
    """True when other conductors meet this run along its length, not only at its ends."""

    return _tee_count(line, wires, grid) >= 2


def _symbol_boxes(devices: list[dict[str, Any]], catalog: Any) -> list[tuple[float, float, float, float]]:
    boxes = []
    for device in devices:
        if device.get("guess") or not device.get("library_id"):
            continue
        try:
            symbol = catalog.get(str(device["library_id"]))
        except KeyError:
            continue
        width, height = _frame_size(device, symbol)
        ox, oy = float(device["x"]), float(device["y"])
        boxes.append((ox, oy, ox + width, oy + height))
    return boxes


def _midpoint_inside(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    boxes: list[tuple[float, float, float, float]],
) -> bool:
    mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
    for x0, y0, x1, y1 in boxes:
        inset_x = (x1 - x0) * 0.12
        inset_y = (y1 - y0) * 0.12
        if x0 + inset_x <= mx <= x1 - inset_x and y0 + inset_y <= my <= y1 - inset_y:
            return True
    return False


def read_drawing(
    tokens: list[dict[str, Any]],
    strokes: list[dict[str, Any]],
    width_mm: float,
    height_mm: float,
    catalog: Any,
    *,
    standard: str = "",
    kind: str = "",
) -> dict[str, Any]:
    """Devices and wires from text plus the strokes underneath them."""

    from panelcad.importer.extract import facts_from_tokens
    from panelcad.importer.symbols import PREFIX_CONFIDENCE, shape_class

    facts = facts_from_tokens(tokens, width_mm, height_mm, catalog=catalog)
    if not strokes:
        for device in facts["devices"]:
            device["confidence"] = min(float(device.get("confidence") or 1), PREFIX_CONFIDENCE)
            device["guess"] = True
            device["symbol"] = ""
            device["library_id"] = ""
        for wire in facts["wires"]:
            wire["ambiguous"] = True
            wire["from_pin"] = ""
            wire["to_pin"] = ""
            wire["from_tag"] = ""
            wire["to_tag"] = ""
            wire["confidence"] = min(float(wire.get("confidence") or 1), 0.45)
        _stamp_source(facts, "vector")
        return facts
    lines = [stroke for stroke in strokes if stroke.get("kind") == "line"]
    curves = [stroke for stroke in strokes if stroke.get("kind") == "curve"]
    rects = [stroke for stroke in strokes if stroke.get("kind") == "rect"]
    claimed: list[tuple[float, float]] = []
    # One pass over the words. Repeating it per device reloads the parts library.
    tag_points = _device_tag_points(tokens, catalog)
    devices = []
    for token in facts["devices"]:
        row = _match_device(
            token,
            lines,
            curves,
            rects,
            tokens,
            catalog,
            standard,
            claimed=claimed,
            kind=kind,
            tag_points=tag_points,
        )
        devices.append(row)
        if row.get("guess") or not row.get("library_id"):
            continue
        try:
            symbol = catalog.get(str(row["library_id"]))
        except KeyError:
            continue
        ox, oy = float(row["x"]), float(row["y"])
        scale = float(row.get("scale") or 1.0)
        for pin in symbol.pins:
            claimed.append((ox + float(pin.x) * scale, oy + float(pin.y) * scale))
    if kind not in {"panel_layout", "door_layout"}:
        _fit_fed_poles(devices, list(facts["devices"]), lines, curves, catalog, standard)
    devices = _restore_tags_at_symbols(tokens, devices, lines, curves, catalog)
    # A tag the height gate dropped is matched the same way as the others once
    # its ink shows it is a symbol. Leaving it at the text drops the wires.
    restored_rows = []
    for row in devices:
        if not row.get("restored"):
            restored_rows.append(row)
            continue
        token = {
            "tag": row.get("tag") or "",
            "text": row.get("tag") or "",
            "x": row.get("x") or 0,
            "y": row.get("y") or 0,
            "w": row.get("w") or 0,
            "h": row.get("h") or 0,
        }
        restored_rows.append(
            _match_device(
                token,
                lines,
                curves,
                rects,
                tokens,
                catalog,
                standard,
                claimed=claimed,
                kind=kind,
                tag_points=tag_points,
            )
        )
    devices = restored_rows
    devices = _drop_interior_guesses(devices, catalog)
    devices = _drop_lead_marks(devices, lines)
    devices = _claim_conductor_numbers(devices, facts["wires"], lines)
    _promote_by_geometry(lines, devices, catalog, curves)
    wires = _trace_wires(facts["wires"], lines, devices, catalog, tokens, width_mm, height_mm)
    facts = {"devices": devices, "wires": wires, "xrefs": facts.get("xrefs") or [], "texts": facts.get("texts") or []}
    _stamp_source(facts, "vector")
    return facts


_LEAD_MARK = re.compile(r"^[TUVW]\d{1,2}$")


def _text_point(device: dict[str, Any]) -> tuple[float, float]:
    """Where the tag was printed. A symbol match moves the device origin."""

    if device.get("text_x") is not None and device.get("text_y") is not None:
        return float(device["text_x"]), float(device["text_y"])
    return float(device["x"]), float(device["y"])


def _lead_row(device: dict[str, Any], devices: list[dict[str, Any]]) -> bool:
    """T1 beside T2, or U1 beside V1, is a row of terminal marks."""

    text = str(device.get("tag") or "")
    if not _LEAD_MARK.match(text):
        return False
    x, y = _text_point(device)
    for other in devices:
        if other is device:
            continue
        if not _LEAD_MARK.match(str(other.get("tag") or "")):
            continue
        ox, oy = _text_point(other)
        if abs(oy - y) <= 1.5 and abs(ox - x) <= 16.0:
            return True
    return False


def _drop_interior_guesses(devices: list[dict[str, Any]], catalog: Any) -> list[dict[str, Any]]:
    """A pin label or model caption drawn inside a matched device frame is not a separate device."""

    if catalog is None:
        return devices
    kept = []
    for device in devices:
        matched = bool(device.get("library_id")) and not device.get("guess")
        if not matched:
            tx, ty = _text_point(device)
            if _inside_symbol({"x": tx, "y": ty}, devices, catalog):
                continue
        kept.append(device)
    return kept


def _drop_lead_marks(devices: list[dict[str, Any]], lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """T1 or U1 drawn on a conductor is a terminal mark, not another device.

    A matched symbol with that tag is the device (a drive named U1). A row of
    those names on the wire (T1 T2 T3, U1 V1 W1) is the motor's terminal marks,
    even when a prefix match gave them a library symbol. An unmatched caption
    sitting on the wire is a lead mark too.
    """

    conductors = [
        stroke
        for stroke in lines
        if stroke.get("role") == "wire" and stroke.get("kind", "line") == "line" and "a" in stroke
    ]
    if not conductors:
        return devices
    segments = [(tuple(stroke["a"]), tuple(stroke["b"])) for stroke in conductors]
    kept = []
    for device in devices:
        text = str(device.get("tag") or "")
        matched = bool(device.get("library_id")) and not device.get("guess")
        tx, ty = _text_point(device)
        on_wire = _LEAD_MARK.match(text) and _point_chain(tx, ty, segments) <= 5.2
        if on_wire and (not matched or _lead_row(device, devices)):
            continue
        kept.append(device)
    return kept


def _restore_tags_at_symbols(
    tokens: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    catalog: Any = None,
) -> list[dict[str, Any]]:
    """A tag at a drawn symbol stays even when its height is not the page's tallest."""

    from panelcad.importer.read import contact_tag_parts, iec_product_tag, is_device_tag, normalize_device_tag, printed_reference_tag

    # The same floor label_tokens uses. A much smaller stray word is not a part.
    heights = sorted(float(device.get("h") or 0) for device in devices if float(device.get("h") or 0) > 0)
    ref_h = heights[len(heights) // 2] if heights else 0.0
    ink = [
        stroke
        for stroke in lines
        if stroke.get("kind", "line") == "line" and stroke.get("role") != "wire" and "a" in stroke
    ]
    grid = _endpoint_grid(ink)
    conductors = [
        (tuple(stroke["a"]), tuple(stroke["b"]))
        for stroke in lines
        if stroke.get("role") == "wire" and stroke.get("kind", "line") == "line" and "a" in stroke
    ]
    placed = [(float(device["x"]), float(device["y"])) for device in devices if device.get("tag")]
    restored = list(devices)
    for tok in tokens:
        raw = str(tok.get("text") or "")
        product = iec_product_tag(raw)
        text = printed_reference_tag(raw) or raw
        if not text or not is_device_tag(text, catalog):
            continue
        identity = product or normalize_device_tag(text)
        height = float(tok.get("h") or 0)
        if ref_h and height < ref_h * 0.85 and contact_tag_parts(text, catalog) is None:
            continue
        x, y = float(tok["x"]), float(tok["y"])
        if conductors and _LEAD_MARK.match(text) and _point_chain(x, y, conductors) <= 5.2:
            continue
        # The model name printed on a symbol is not a second device. Another
        # printing of the same tag, far from this one, is the coil or the contact.
        if any(_dist((x, y), point) <= 8.0 for point in placed):
            continue
        if any(
            (iec_product_tag(str(device.get("tag") or "")) or normalize_device_tag(str(device.get("tag") or "")))
            == identity
            and _dist((x, y), _text_point(device)) <= max(12.0, height * 3.5)
            for device in restored
        ):
            continue
        if not _at_symbol_ink(x, y, grid, curves):
            continue
        restored.append(
            {
                "tag": text,
                "type": "",
                "symbol": "",
                "pins": [],
                "x": round(x, 2),
                "y": round(y, 2),
                "w": tok.get("w") or 0,
                "h": tok.get("h") or 0,
                "confidence": 0.7,
                "unreadable": False,
                "library_id": "",
                "guess": True,
                "pin_frame": "",
                "scale": 1.0,
                "restored": True,
            }
        )
    return restored


def _at_symbol_ink(x: float, y: float, grid: _PointGrid, curves: list[dict[str, Any]]) -> bool:
    near = [point for point in grid.near(x, y, x, y, pad=12.0) if _dist(point, (x, y)) <= 12.0]
    if len(near) >= 4:
        return True
    for curve in curves:
        for point in curve.get("points") or []:
            if _dist((x, y), (float(point[0]), float(point[1]))) <= 14.0:
                return True
    return False


def _claim_conductor_numbers(
    devices: list[dict[str, Any]],
    wires: list[dict[str, Any]],
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """W101 or 101A drawn on a conductor is a wire number, not a device tag."""

    from panelcad.importer.read import lettered_wire_text

    conductors = [
        stroke
        for stroke in lines
        if stroke.get("role") == "wire" and stroke.get("kind", "line") == "line"
    ]
    if not conductors:
        return devices
    segments = [(tuple(stroke["a"]), tuple(stroke["b"])) for stroke in conductors]
    kept = []
    for device in devices:
        text = str(device.get("tag") or "")
        height = float(device.get("h") or 2.5)
        # A wire number sits on the conductor or just above it, within about one line of text.
        # A device tag sits clear of the rung, beside the symbol.
        if lettered_wire_text(text) and _point_chain(float(device["x"]), float(device["y"]), segments) <= max(
            7.0, height * 1.6
        ):
            wires.append(
                {
                    "number": text,
                    "text": text,
                    "x": device["x"],
                    "y": device["y"],
                    "h": height,
                    "w": device.get("w") or 0,
                    "confidence": 0.9,
                }
            )
            continue
        kept.append(device)
    return kept


def _stamp_source(facts: dict[str, Any], source: str) -> None:
    for key in ("devices", "wires", "xrefs", "texts"):
        for item in facts.get(key) or []:
            if isinstance(item, dict) and not item.get("source"):
                item["source"] = source


def _match_device(
    token: dict[str, Any],
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    rects: list[dict[str, Any]],
    page_tokens: list[dict[str, Any]],
    catalog: Any,
    standard: str,
    claimed: list[tuple[float, float]] | None = None,
    kind: str = "",
    tag_points: list[tuple[str, float, float]] | None = None,
) -> dict[str, Any]:
    from panelcad.importer.read import contact_tag_parts, sheet_tag_names
    from panelcad.importer.symbols import PREFIX_CONFIDENCE, shape_class, tag_prefix

    tag = str(token.get("tag") or "")
    prefix = tag_prefix(tag)
    contact = contact_tag_parts(tag, catalog, parents=sheet_tag_names(page_tokens))
    if contact is not None:
        # The suffix names the terminal. The drawn symbol is that contact,
        # not the power contactor the parent letters would otherwise pick.
        candidates = _contact_symbols(catalog, standard, contact[1])
    else:
        candidates = _prefix_devices(catalog, prefix, standard)
        if prefix in {"M", "K", "CR", "CON", "MS"}:
            candidates = _with_coils(candidates, catalog, standard)
    left = float(token["x"]) - float(token.get("w") or 0) / 2.0
    base_y = float(token["y"])
    # The tag can sit left of a wide symbol. The pool has to cover that body,
    # not only a fixed radius around the text.
    widths = [float(getattr(device, "schematic_width", 0) or 0) for device in candidates]
    heights = [float(getattr(device, "schematic_height", 0) or 0) for device in candidates]
    reach = max(95.0, 28.0 + max(widths or [0.0]), 18.0 + max(heights or [0.0]))
    ink_lines = [stroke for stroke in lines if stroke.get("role") == "ink"]
    # Pin leads are drawn at the wire weight. The symbol match has to see them
    # when the tag sits in the coil or under the contact, not only above it.
    symbol_lines = [stroke for stroke in lines if stroke.get("role") in {"ink", "wire"} and not stroke.get("bus")]
    pool = _nearby(ink_lines, left, base_y, reach)
    wide_pool = _nearby(symbol_lines, left, base_y, reach)
    curve_pool = _nearby_curves(curves, left, base_y, reach)
    center = (float(token["x"]), float(token["y"]))
    others = _others_for(tag_points, tag) if tag_points is not None else _other_tag_points(page_tokens, tag, catalog)
    uncertain = False
    if contact is not None:
        chosen = _choose_contact(
            candidates,
            left,
            base_y,
            pool,
            curve_pool,
            standard,
            center=center,
            wide_lines=wide_pool,
            terminal=contact[1],
        )
        if chosen == "uncertain":
            best = None
            uncertain = True
        else:
            best = chosen if chosen is not None else None
    else:
        best = _best_ink(
            candidates,
            left,
            base_y,
            pool,
            curve_pool,
            standard,
            center=center,
            wide_lines=wide_pool,
        )
        if best is not None and best[0] >= MATCH_MIN:
            best = _raise_to_drawn_poles(
                best,
                candidates,
                wide_pool or pool,
                standard,
                prefix,
                left,
                base_y,
                pool,
                curve_pool,
                center,
                claimed=claimed,
                others=others,
            )
    row = {
        "tag": tag,
        "type": "",
        "symbol": "",
        "pins": [],
        "x": round(float(token["x"]), 2),
        "y": round(float(token["y"]), 2),
        "text_x": round(float(token["x"]), 2),
        "text_y": round(float(token["y"]), 2),
        "w": token.get("w") or 0,
        "h": token.get("h") or 0,
        "confidence": PREFIX_CONFIDENCE,
        "unreadable": False,
        "library_id": "",
        "guess": True,
        "pin_frame": "",
        "scale": 1.0,
    }
    if uncertain:
        row["contact_uncertain"] = True
        return row
    # A panel footprint is not the schematic symbol. Ink and pin landing would
    # name a different part. The layout keeps a rectangle match, or the device
    # already placed on the schematic.
    if kind in {"panel_layout", "door_layout"}:
        best = None
    if best is not None and best[0] >= MATCH_MIN:
        score, device, ox, oy, scale = best
        row.update(
            {
                "type": shape_class(device),
                "symbol": shape_class(device),
                "library_id": device.library_id,
                "x": round(ox, 2),
                "y": round(oy, 2),
                "confidence": round(min(0.97, 0.55 + 0.42 * score), 2),
                "guess": False,
                "pin_frame": "schematic",
                "scale": round(scale, 4),
            }
        )
        return row
    framed = _match_frame(token, rects, page_tokens, candidates)
    if framed is None:
        # A framed module (a PLC block) keeps the rectangle match. A symbol
        # whose body is an older drawing still meets the wires at its terminals.
        if contact is None and kind not in {"panel_layout", "door_layout"}:
            pinned = _search_pin_landing(
                candidates,
                left,
                base_y,
                wide_pool or pool,
                curve_pool,
                standard,
                center=center,
                claimed=claimed,
            )
            if pinned is not None and pinned[0] >= MATCH_MIN:
                pinned = _raise_to_drawn_poles(
                    pinned,
                    candidates,
                    wide_pool or pool,
                    standard,
                    prefix,
                    left,
                    base_y,
                    pool,
                    curve_pool,
                    center,
                    claimed=claimed,
                    others=others,
                )
                score, device, ox, oy, scale = pinned
                row.update(
                    {
                        "type": shape_class(device),
                        "symbol": shape_class(device),
                        "library_id": device.library_id,
                        "x": round(ox, 2),
                        "y": round(oy, 2),
                        "confidence": round(min(0.9, 0.55 + 0.3 * score), 2),
                        "guess": False,
                        "pin_frame": "schematic",
                        "scale": round(scale, 4),
                    }
                )
                return row
        return row
    device, rect, frame = framed
    row.update(
        {
            "type": shape_class(device),
            "symbol": shape_class(device),
            "library_id": device.library_id,
            "x": round(float(rect["x"]), 2),
            "y": round(float(rect["y"]), 2),
            "confidence": 0.9,
            "guess": False,
            "pin_frame": frame,
        }
    )
    return row


def _prefix_devices(catalog: Any, prefix: str, standard: str) -> list[Any]:
    from panelcad.library.catalog import normalize_standard

    std = normalize_standard(standard, default="") if standard else ""
    found = []
    for device in catalog.devices.values():
        if device.ref_prefix.upper() != prefix:
            continue
        lib = device.library_id.lower()
        if "notes" in lib or ("offpage" in lib and prefix != "OP"):
            continue
        found.append(device)
    # The title-block standard is a tie-break. An OEM part drawn on an IEC
    # sheet is still that part; dropping it leaves the IEC lookalike.
    found.sort(key=lambda device: (0 if not std or device.standard in {std, ""} else 1, device.library_id))
    return found


def _standard_symbol(device: Any, standard: str) -> bool:
    if not standard:
        return True
    lib = str(getattr(device, "library_id", "") or "")
    if standard == "NEMA/ANSI":
        return lib.startswith("nema.") or lib.startswith("iec.connection.offpage")
    if standard == "IEC":
        return lib.startswith("iec.")
    return True


def _with_coils(candidates: list[Any], catalog: Any, standard: str) -> list[Any]:
    """A CR or K coil is drawn as the coil symbol, whose ref prefix may differ."""

    found = list(candidates)
    seen = {str(getattr(device, "library_id", "") or "") for device in found}
    for device in catalog.devices.values():
        lib = str(getattr(device, "library_id", "") or "")
        if lib in seen or "aux" in lib.lower():
            continue
        if not lib.lower().endswith("contactors.coil"):
            continue
        if not _standard_symbol(device, standard):
            continue
        found.append(device)
        seen.add(lib)
    return found


def _family_ok(candidate: Any, wanted: str, standard: str) -> bool:
    """Stay in the tag's class and the sheet's standard. A contactor is not a motor."""

    from panelcad.importer.symbols import shape_class

    if not _standard_symbol(candidate, standard):
        return False
    shape = shape_class(candidate)
    if wanted == "contactor":
        return shape == "contactor"
    if wanted:
        return shape == wanted
    return True


def _raise_to_drawn_poles(
    best: tuple[float, Any, float, float, float],
    candidates: list[Any],
    wide_lines: list[dict[str, Any]],
    standard: str,
    prefix: str,
    left: float,
    base_y: float,
    pool: list[dict[str, Any]],
    curve_pool: list[dict[str, Any]],
    center: tuple[float, float],
    claimed: list[tuple[float, float]] | None = None,
    others: list[tuple[float, float]] | None = None,
) -> tuple[float, Any, float, float, float]:
    """A drawn row of leads picks the pole count before the wires are traced.

    The smaller symbol often scores higher because it has less ink to miss.
    Parallel leads keep the tag's own family: a contactor tag does not become
    a motor. A lead counts only inside this symbol's terminal span, and every
    added pin has to land on one. A neighbour or the rung above is not a pole.
    """

    from panelcad.importer.symbols import SUPPORTED_TAG_FAMILIES, shape_class

    _score, device, ox, oy, scale = best
    if shape_class(device) in {"coil", "pushbutton", "pilot", "limit", "selector", "relay"}:
        return best
    width = float(getattr(device, "schematic_width", 0) or 0) * float(scale or 1)
    height = float(getattr(device, "schematic_height", 0) or 0) * float(scale or 1)
    if width < 4 or height < 4:
        return best
    wanted = SUPPORTED_TAG_FAMILIES.get(prefix, shape_class(device))
    siblings = [candidate for candidate in candidates if _family_ok(candidate, wanted, standard)]
    # Leads past this symbol's edge belong to the next device or the next rung.
    box = (ox, oy, ox + width, oy + height)
    own = _parallel_side_counts(box, wide_lines, PIN_SNAP_MM, claimed=claimed, center=center, others=others)
    through = (len(own.get("top", [])) >= 2 and len(own.get("bottom", [])) >= 2) or (
        len(own.get("left", [])) >= 2 and len(own.get("right", [])) >= 2
    )
    placed: list[tuple[float, int, float, Any, float, float]] = []
    for candidate in siblings:
        if candidate is device:
            continue
        grown = _grown_sides(device, candidate)
        if not grown:
            continue
        # A pass-through symbol is not a one-sided load. A 1-phase motor's
        # box can have strokes on both sides without being a pass-through.
        if through and _one_sided(candidate) and not _one_sided(device):
            continue
        row = _place_by_landed_pins(
            candidate, grown, wide_lines, center, claimed, others, pool, curve_pool, box
        )
        if row is not None:
            placed.append(row)
    if not placed:
        return best
    # Every added pin landed on its own lead. Ink has to agree. Bare leads, with
    # no neighbour on the sheet, are the only case that has no body to score.
    inked = [row for row in placed if row[0] >= MATCH_MIN]
    if inked:
        inked.sort(key=lambda row: (-row[0], -row[1], row[2], len(row[3].pins)))
        score, _pins, _error, symbol, sox, soy = inked[0]
        return (score, symbol, sox, soy, 1.0)
    if others:
        return best
    placed.sort(key=lambda row: (-row[1], row[2], len(row[3].pins), str(row[3].library_id)))
    _score, _pins, _error, symbol, sox, soy = placed[0]
    return (MATCH_MIN + 0.02, symbol, sox, soy, 1.0)


def _parallel_side_counts(
    box: tuple[float, float, float, float],
    lines: list[dict[str, Any]],
    reach: float,
    claimed: list[tuple[float, float]] | None = None,
    center: tuple[float, float] | None = None,
    others: list[tuple[float, float]] | None = None,
) -> dict[str, list[float]]:
    """Conductors that meet one side inside this symbol. Copies of one lead stay one pole.

    A stroke that stays inside the body is ink. A lead past the terminal span,
    or one that already belongs to another device, is not a pole here.
    """

    by_side: dict[str, list[float]] = {}
    for stroke in lines:
        if stroke.get("bus") or "a" not in stroke or "b" not in stroke:
            continue
        if stroke.get("role") not in {"ink", "wire"}:
            continue
        start = (float(stroke["a"][0]), float(stroke["a"][1]))
        end = (float(stroke["b"][0]), float(stroke["b"][1]))
        for side, along in _lead_arrivals(start, end, box, reach, claimed, center, others):
            by_side.setdefault(side, []).append(along)
    return {side: _cluster_centers(coords, box, side) for side, coords in by_side.items()}


def _lead_arrivals(
    start: tuple[float, float],
    end: tuple[float, float],
    box: tuple[float, float, float, float],
    reach: float,
    claimed: list[tuple[float, float]] | None,
    center: tuple[float, float] | None = None,
    others: list[tuple[float, float]] | None = None,
) -> list[tuple[str, float]]:
    """Where a conductor meets a side, inside that side's terminal span."""

    found: list[tuple[str, float]] = []
    for point in (start, end):
        side = _touch_side(point, box, reach)
        if side is None:
            continue
        along = point[0] if side in {"top", "bottom"} else point[1]
        if not _on_side_span(along, box, side, reach):
            continue
        if _lead_belongs_elsewhere(point, box, claimed, center, others, along, side):
            continue
        # A stroke that runs along the outline is the body, not a lead.
        if _runs_along(start, end, side):
            continue
        found.append((side, along))
    for side, along, point in _side_lead_crossings(start, end, box, reach):
        if _lead_belongs_elsewhere(point, box, claimed, center, others, along, side):
            continue
        found.append((side, along))
    return found


def _on_side_span(along: float, box: tuple[float, float, float, float], side: str, reach: float) -> bool:
    """True when the meeting point is on this side, not past the symbol edge."""

    if side in {"top", "bottom"}:
        return box[0] - reach <= along <= box[2] + reach
    return box[1] - reach <= along <= box[3] + reach


def _runs_along(start: tuple[float, float], end: tuple[float, float], side: str) -> bool:
    dx = abs(end[0] - start[0])
    dy = abs(end[1] - start[1])
    if side in {"top", "bottom"}:
        return dx > max(PIN_SNAP_MM, dy * 2.0)
    return dy > max(PIN_SNAP_MM, dx * 2.0)


def _lead_belongs_elsewhere(
    point: tuple[float, float],
    box: tuple[float, float, float, float],
    claimed: list[tuple[float, float]] | None,
    center: tuple[float, float] | None,
    others: list[tuple[float, float]] | None,
    along: float,
    side: str,
) -> bool:
    """A crossing past the edge that is another device's terminal is not ours."""

    if _closer_to_claimed(point, box, claimed):
        return True
    on_segment = _on_side_span(along, box, side, 0.0)
    if on_segment or center is None:
        return False
    return _nearer_other_tag(point, center, others)


def _nearer_other_tag(
    point: tuple[float, float],
    center: tuple[float, float],
    others: list[tuple[float, float]] | None,
) -> bool:
    if not others:
        return False
    here = _dist(point, center)
    return any(_dist(point, other) + 0.6 < here for other in others)


def _device_tag_points(tokens: list[dict[str, Any]], catalog: Any) -> list[tuple[str, float, float]]:
    """Device-tag locations on the page. The catalog is the one already loaded."""

    from panelcad.importer.read import is_device_tag

    found: list[tuple[str, float, float]] = []
    for token in tokens:
        text = str(token.get("text") or "")
        if not text or not is_device_tag(text, catalog):
            continue
        found.append((text, float(token["x"]), float(token["y"])))
    return found


def _others_for(points: list[tuple[str, float, float]], tag: str) -> list[tuple[float, float]]:
    return [(x, y) for text, x, y in points if text != tag]


def _other_tag_points(tokens: list[dict[str, Any]], tag: str, catalog: Any) -> list[tuple[float, float]]:
    return _others_for(_device_tag_points(tokens, catalog), tag)


def _closer_to_claimed(
    point: tuple[float, float],
    box: tuple[float, float, float, float],
    claimed: list[tuple[float, float]] | None,
) -> bool:
    """A terminal already placed on another device is that device's lead."""

    if not claimed:
        return False
    gap = _rect_gap(point, box)
    nearest = min(_dist(point, pin) for pin in claimed)
    return nearest + 0.6 < gap


def _side_lead_crossings(
    start: tuple[float, float],
    end: tuple[float, float],
    box: tuple[float, float, float, float],
    reach: float,
) -> list[tuple[str, float, tuple[float, float]]]:
    """A lead crossing a side inside the terminal span. Outline along the side does not."""

    x0, y0, x1, y1 = box
    dx, dy = end[0] - start[0], end[1] - start[1]
    found: list[tuple[str, float, tuple[float, float]]] = []
    if abs(dy) > 1e-6 and abs(dx) <= abs(dy) * 0.45:
        for side, y, xa, xb in (("top", y0, x0, x1), ("bottom", y1, x0, x1)):
            fraction = (y - start[1]) / dy
            if fraction < -0.01 or fraction > 1.01:
                continue
            along = start[0] + fraction * dx
            if xa - reach <= along <= xb + reach:
                found.append((side, along, (along, y)))
    if abs(dx) > 1e-6 and abs(dy) <= abs(dx) * 0.45:
        for side, x, ya, yb in (("left", x0, y0, y1), ("right", x1, y0, y1)):
            fraction = (x - start[0]) / dx
            if fraction < -0.01 or fraction > 1.01:
                continue
            along = start[1] + fraction * dy
            if ya - reach <= along <= yb + reach:
                found.append((side, along, (x, along)))
    return found


def _cluster_fixed(coords: list[float], step: float) -> list[float]:
    if not coords:
        return []
    ordered = sorted(coords)
    groups = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] > step:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _cluster_centers(coords: list[float], box: tuple[float, float, float, float], side: str) -> list[float]:
    if not coords:
        return []
    ordered = sorted(coords)
    if len(ordered) == 1:
        return ordered
    gaps = [ordered[index + 1] - ordered[index] for index in range(len(ordered) - 1)]
    big = max(gaps) if gaps else 0.0
    span = (box[2] - box[0]) if side in {"top", "bottom"} else (box[3] - box[1])
    slop = max(PIN_SNAP_MM * 1.6, span * 0.12)
    if big <= slop:
        return [sum(ordered) / len(ordered)]
    step = max(slop, big * 0.45)
    groups = [[ordered[0]]]
    for value in ordered[1:]:
        if value - groups[-1][-1] > step:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _segment_crosses_box(
    start: tuple[float, float],
    end: tuple[float, float],
    box: tuple[float, float, float, float],
    reach: float,
) -> bool:
    return bool(_side_crossings(start, end, box, reach))


def _side_crossings(
    start: tuple[float, float],
    end: tuple[float, float],
    box: tuple[float, float, float, float],
    reach: float,
) -> list[tuple[str, float]]:
    """Where a straight run meets a side. A long lead counts even if its ends are far."""

    x0, y0, x1, y1 = box
    found: list[tuple[str, float]] = []
    if abs(start[0] - end[0]) <= max(reach, abs(start[1] - end[1]) * 0.3):
        along = (start[0] + end[0]) / 2.0
        y_lo, y_hi = sorted((start[1], end[1]))
        if x0 - reach <= along <= x1 + reach:
            if y_lo - reach <= y0 <= y_hi + reach:
                found.append(("top", along))
            if y_lo - reach <= y1 <= y_hi + reach:
                found.append(("bottom", along))
    if abs(start[1] - end[1]) <= max(reach, abs(start[0] - end[0]) * 0.3):
        along = (start[1] + end[1]) / 2.0
        x_lo, x_hi = sorted((start[0], end[0]))
        if y0 - reach <= along <= y1 + reach:
            if x_lo - reach <= x0 <= x_hi + reach:
                found.append(("left", along))
            if x_lo - reach <= x1 <= x_hi + reach:
                found.append(("right", along))
    return found


def _count_separated(coords: list[float], box: tuple[float, float, float, float], side: str) -> int:
    return len(_cluster_centers(coords, box, side))


def _grown_sides(current: Any, candidate: Any) -> list[str]:
    """Sides where the larger symbol has more terminals than the one we matched."""

    order = {"top": 0, "bottom": 1, "left": 2, "right": 3}
    found = []
    for side in ("top", "bottom", "left", "right"):
        now = _symbol_side_count(current, side)
        nxt = _symbol_side_count(candidate, side)
        if nxt >= 2 and nxt > now:
            found.append(side)
    found.sort(key=lambda side: (-_symbol_side_count(candidate, side), order[side]))
    return found


def _place_by_landed_pins(
    symbol: Any,
    grown: list[str],
    lines: list[dict[str, Any]],
    center: tuple[float, float],
    claimed: list[tuple[float, float]] | None,
    others: list[tuple[float, float]] | None,
    pool: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    current_box: tuple[float, float, float, float],
) -> tuple[float, int, float, Any, float, float] | None:
    """Place the symbol only when every added pin sits on a lead inside its span."""

    from itertools import combinations

    anchor = grown[0]
    places = _axis_leads(lines, center, symbol, anchor, current_box, others)
    pins = [pin for pin in symbol.pins if str(pin.side) == anchor]
    if len(places) < len(pins):
        return None
    horizontal = anchor in {"top", "bottom"}
    pins.sort(key=lambda pin: float(pin.x if horizontal else pin.y))
    width = float(symbol.schematic_width)
    height = float(symbol.schematic_height)
    cx, cy = center
    best: tuple[float, float, float] | None = None
    for chosen in combinations(places, len(pins)):
        origin = chosen[0] - float(pins[0].x if horizontal else pins[0].y)
        error = 0.0
        landed = True
        for pin, place in zip(pins, chosen, strict=True):
            gap = abs(float(pin.x if horizontal else pin.y) + origin - place)
            if gap > PIN_SNAP_MM:
                landed = False
                break
            error += gap
        if not landed:
            continue
        if horizontal:
            ox = origin
            oy = current_box[1] if anchor == "top" else current_box[3] - height
        else:
            oy = origin
            ox = current_box[0] if anchor == "left" else current_box[2] - width
        if not _tag_near_symbol(cx, cy, ox, oy, width, height):
            continue
        if best is None or error < best[0]:
            best = (error, ox, oy)
    if best is None:
        return None
    error, ox, oy = best
    ink, ox, oy = _refine_landed_origin(
        symbol, ox, oy, anchor, center, pool, curves, lines, claimed, others
    )
    if ink < 0:
        return None
    return (ink, len(pins), error, symbol, ox, oy)


def _axis_leads(
    lines: list[dict[str, Any]],
    center: tuple[float, float],
    symbol: Any,
    side: str,
    current_box: tuple[float, float, float, float],
    others: list[tuple[float, float]] | None,
) -> list[float]:
    """Lead positions that can belong to a symbol of this size drawn at this tag.

    A conductor whose middle is outside the matched symbol and nearer another
    tag is that other device. It is not a pole here.
    """

    cx, cy = center
    width = float(symbol.schematic_width)
    height = float(symbol.schematic_height)
    horizontal = side in {"top", "bottom"}
    span = width if horizontal else height
    lo = (cx if horizontal else cy) - span
    hi = (cx if horizontal else cy) + span
    values: list[float] = []
    for stroke in lines:
        if stroke.get("bus") or stroke.get("role") not in {"ink", "wire"}:
            continue
        if "a" not in stroke or "b" not in stroke:
            continue
        start = (float(stroke["a"][0]), float(stroke["a"][1]))
        end = (float(stroke["b"][0]), float(stroke["b"][1]))
        dx, dy = end[0] - start[0], end[1] - start[1]
        if horizontal:
            if abs(dx) > max(PIN_SNAP_MM, abs(dy) * 0.45):
                continue
            along = (start[0] + end[0]) / 2.0
        else:
            if abs(dy) > max(PIN_SNAP_MM, abs(dx) * 0.45):
                continue
            along = (start[1] + end[1]) / 2.0
        if along < lo or along > hi:
            continue
        mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        if _outside_box(mid, current_box, 1.0) and _nearer_other_tag(mid, center, others):
            continue
        values.append(along)
    if not values:
        return []
    # Copies of one lead sit together. The next pole is a pitch further, so a
    # distant stroke must not pull those two into one.
    return _cluster_fixed(values, PIN_SNAP_MM * 1.6)


def _refine_landed_origin(
    symbol: Any,
    ox: float,
    oy: float,
    anchor: str,
    center: tuple[float, float],
    pool: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    claimed: list[tuple[float, float]] | None,
    others: list[tuple[float, float]] | None,
) -> tuple[float, float, float]:
    """Slide along the leads. Keep a position only while every pin on that side still lands."""

    horizontal = anchor in {"top", "bottom"}
    width = float(symbol.schematic_width)
    height = float(symbol.schematic_height)
    cx, cy = center
    best: tuple[float, float, float, float] | None = None

    def consider(trial: tuple[float, float]) -> None:
        nonlocal best
        if not _tag_near_symbol(cx, cy, trial[0], trial[1], width, height):
            return
        if not _all_pins_land(symbol, trial[0], trial[1], anchor, lines, claimed, center, others):
            return
        held = None if best is None else best[0] - 0.02
        score = _ink_score(symbol, trial[0], trial[1], pool, curves, 1.0, limit=held)
        gap = _wire_pin_gap(symbol, trial[0], trial[1], lines)
        if best is None or score > best[0] + 0.02 or (score >= best[0] - 0.02 and gap + 0.4 < best[1]):
            best = (score, gap, trial[0], trial[1])

    consider((ox, oy))
    seed = oy if horizontal else ox
    for value in _steps(seed - 24.0, seed + 24.0, 1.0):
        consider((ox, value) if horizontal else (value, oy))
    if best is None:
        return (-1.0, ox, oy)
    peak = best[3] if horizontal else best[2]
    fixed = best[2] if horizontal else best[3]
    for value in _steps(peak - 1.5, peak + 1.5, 0.5):
        consider((fixed, value) if horizontal else (value, fixed))
    return (best[0], best[2], best[3])


def _all_pins_land(
    symbol: Any,
    ox: float,
    oy: float,
    side: str,
    lines: list[dict[str, Any]],
    claimed: list[tuple[float, float]] | None,
    center: tuple[float, float] | None,
    others: list[tuple[float, float]] | None,
) -> bool:
    """Every pin on this side meets its own lead, inside the symbol."""

    width = float(symbol.schematic_width)
    height = float(symbol.schematic_height)
    box = (ox, oy, ox + width, oy + height)
    places = _parallel_side_counts(
        box, lines, PIN_SNAP_MM, claimed=claimed, center=center, others=others
    ).get(side, [])
    pins = [pin for pin in symbol.pins if str(pin.side) == side]
    if len(places) < len(pins):
        return False
    horizontal = side in {"top", "bottom"}
    coords = sorted(float(pin.x if horizontal else pin.y) + (ox if horizontal else oy) for pin in pins)
    used: set[int] = set()
    for coord in coords:
        match: tuple[float, int] | None = None
        for index, place in enumerate(places):
            if index in used:
                continue
            gap = abs(place - coord)
            if gap <= PIN_SNAP_MM and (match is None or gap < match[0]):
                match = (gap, index)
        if match is None:
            return False
        used.add(match[1])
    return True


def _wire_pin_gap(symbol: Any, ox: float, oy: float, lines: list[dict[str, Any]]) -> float:
    """How far the power pins sit from a conductor end. Lower is the drawn terminal."""

    ends: list[tuple[float, float]] = []
    for stroke in lines:
        if stroke.get("role") != "wire" or stroke.get("bus") or "a" not in stroke or "b" not in stroke:
            continue
        ends.append((float(stroke["a"][0]), float(stroke["a"][1])))
        ends.append((float(stroke["b"][0]), float(stroke["b"][1])))
    pins = [pin for pin in symbol.pins if str(pin.side) in {"top", "bottom", "left", "right"}]
    if not ends or not pins:
        return 0.0
    total = 0.0
    for pin in pins:
        point = (ox + float(pin.x), oy + float(pin.y))
        total += min(_dist(point, end) for end in ends)
    return total / len(pins)


def _fit_fed_poles(
    devices: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    catalog: Any,
    standard: str,
) -> None:
    """A symbol needs as many terminals as the conductors on that side.

    N parallel conductors, or a run that leaves an N-pole device, cannot land
    on a variant with fewer terminals there. When no variant fits, the tag
    stays needs-info. A smaller symbol is not a placeholder.
    """

    from panelcad.importer.read import sheet_tag_names

    placed = _placed_boxes(devices, catalog)
    if len(placed) < 1:
        return
    parents = sheet_tag_names(tokens)
    local, bundles = _conductor_bundles(lines, placed)
    needs: dict[int, dict[str, int]] = {}
    seen_counts: dict[int, dict[str, int]] = {}
    for index, (_device, _box, _tight, _reach, symbol) in enumerate(placed):
        for side, count in local.get(index, {}).items():
            if count < 2 or _symbol_side_count(symbol, side) <= 1:
                continue
            seen_counts.setdefault(index, {})[side] = count
            if _symbol_side_count(symbol, side) < count:
                needs.setdefault(index, {})[side] = max(needs.get(index, {}).get(side, 0), count)
    for (left, left_side, right, right_side), count in bundles.items():
        if count < 2:
            continue
        for index, side in ((left, left_side), (right, right_side)):
            symbol = placed[index][4]
            # One terminal absorbs a branch. Only a multi-pin side can be short.
            if _symbol_side_count(symbol, side) <= 1:
                continue
            seen_counts.setdefault(index, {})[side] = max(seen_counts.get(index, {}).get(side, 0), count)
            if _symbol_side_count(symbol, side) < count:
                needs.setdefault(index, {})[side] = max(needs.get(index, {}).get(side, 0), count)
    for index, minimums in needs.items():
        device = placed[index][0]
        symbol = placed[index][4]
        if not _pole_family(symbol, str(device.get("tag") or ""), catalog, parents):
            continue
        if all(_symbol_side_count(symbol, side) >= count for side, count in minimums.items()):
            continue
        token = tokens[devices.index(device)] if device in devices else None
        if token is None:
            continue
        chosen = _rematch_poles(
            token,
            lines,
            curves,
            catalog,
            standard,
            minimums,
            devices,
            device,
            symbol,
            seen_counts.get(index, {}),
        )
        if chosen is None:
            # Keep the drawn symbol. The short side stays needs-info later.
            device["pole_gap_sides"] = sorted(minimums)
            continue
        score, symbol, ox, oy, scale = chosen
        from panelcad.importer.symbols import shape_class

        device.update(
            {
                "type": shape_class(symbol),
                "symbol": shape_class(symbol),
                "library_id": symbol.library_id,
                "x": round(ox, 2),
                "y": round(oy, 2),
                "confidence": round(min(0.97, 0.55 + 0.42 * score), 2),
                "guess": False,
                "pin_frame": "schematic",
                "scale": round(scale, 4),
            }
        )


def _placed_boxes(
    devices: list[dict[str, Any]], catalog: Any
) -> list[tuple[dict[str, Any], tuple[float, float, float, float], float, float, Any]]:
    placed = []
    for device in devices:
        if device.get("guess") or not device.get("library_id"):
            continue
        if str(device.get("pin_frame") or "schematic") != "schematic":
            continue
        try:
            symbol = catalog.get(str(device["library_id"]))
        except KeyError:
            continue
        width, height = _frame_size(device, symbol)
        if width < 4.0 or height < 4.0:
            continue
        ox, oy = float(device["x"]), float(device["y"])
        small = min(width, height)
        tight = max(PIN_SNAP_MM, small * 0.18)
        reach = max(tight * 2.5, small * 0.55)
        placed.append((device, (ox, oy, ox + width, oy + height), tight, reach, symbol))
    return placed


def _conductor_bundles(
    lines: list[dict[str, Any]],
    placed: list[tuple[dict[str, Any], tuple[float, float, float, float], float, float, Any]],
) -> tuple[dict[int, dict[str, int]], dict[tuple[int, str, int, str], int]]:
    """One count per conductor. A jog or a copied stroke is not a second pole."""

    boxes = [item[1] for item in placed]
    wires = [
        stroke
        for stroke in lines
        if stroke.get("role") == "wire" and not stroke.get("bus") and "a" in stroke and "b" in stroke
        and not _midpoint_in_boxes(stroke, boxes)
    ]
    local_at: dict[int, dict[str, list[float]]] = {}
    bundle_at: dict[tuple[int, str, int, str], list[float]] = {}
    for nodes in _wire_components(wires):
        touches: list[tuple[int, str, float, float]] = []
        for index, (_device, box, _tight, reach, _symbol) in enumerate(placed):
            if not any(_outside_box(node, box, 0.4) for node in nodes):
                continue
            best: tuple[float, str, float] | None = None
            for node in nodes:
                side = _touch_side(node, box, reach)
                if side is None:
                    continue
                gap = _rect_gap(node, box)
                along = node[0] if side in {"top", "bottom"} else node[1]
                if best is None or gap < best[0]:
                    best = (gap, side, along)
            if best is not None:
                touches.append((index, best[1], best[0], best[2]))
        seen: set[tuple[int, str]] = set()
        for index, side, gap, along in touches:
            if gap > placed[index][2] or (index, side) in seen:
                continue
            seen.add((index, side))
            local_at.setdefault(index, {}).setdefault(side, []).append(along)
        for left in range(len(touches)):
            for right in range(left + 1, len(touches)):
                a, side_a, _gap_a, along = touches[left]
                b, side_b, _gap_b, _along_b = touches[right]
                if a == b:
                    continue
                key = _bundle_key(a, side_a, b, side_b)
                bundle_at.setdefault(key, []).append(along)
    local = {
        index: {
            side: _distinct_landings(coords, placed[index][4], side, placed[index][1])
            for side, coords in sides.items()
        }
        for index, sides in local_at.items()
    }
    bundles = {
        key: _distinct_landings(coords, placed[key[0]][4], key[1], placed[key[0]][1])
        for key, coords in bundle_at.items()
    }
    return local, bundles


def _distinct_landings(
    coords: list[float],
    symbol: Any,
    side: str,
    box: tuple[float, float, float, float],
) -> int:
    """Landings on one terminal are one conductor. The next pole is farther."""

    if len(coords) <= 1:
        return len(coords)
    pins = [pin for pin in symbol.pins if str(pin.side) == side]
    # A single terminal cannot grow a second pole out of a branch.
    if len(pins) <= 1:
        return 1
    values = sorted(float(pin.x if side in {"top", "bottom"} else pin.y) for pin in pins)
    gaps = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    gaps = [gap for gap in gaps if gap > 0.5]
    if gaps:
        tol = min(gaps) * 0.45
    else:
        span = (box[2] - box[0]) if side in {"top", "bottom"} else (box[3] - box[1])
        tol = max(PIN_SNAP_MM, span * 0.22)
    ordered = sorted(coords)
    clusters = [ordered[0]]
    for value in ordered[1:]:
        if value - clusters[-1] > tol:
            clusters.append(value)
    return len(clusters)


def _midpoint_in_boxes(stroke: dict[str, Any], boxes: list[tuple[float, float, float, float]]) -> bool:
    """A stroke through a symbol is that symbol, not a conductor between poles."""

    ax, ay = float(stroke["a"][0]), float(stroke["a"][1])
    bx, by = float(stroke["b"][0]), float(stroke["b"][1])
    mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
    for x0, y0, x1, y1 in boxes:
        if x0 + 0.8 < mx < x1 - 0.8 and y0 + 0.8 < my < y1 - 0.8:
            return True
    return False


def _wire_components(strokes: list[dict[str, Any]]) -> list[list[tuple[float, float]]]:
    """Connected wire strokes. Joints a fraction of a millimetre apart are one run."""

    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(node: tuple[float, float]) -> tuple[float, float]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: tuple[float, float], right: tuple[float, float]) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    nodes: list[tuple[float, float]] = []

    def snap(point: Any) -> tuple[float, float]:
        key = (round(float(point[0]), 1), round(float(point[1]), 1))
        for existing in nodes:
            if _dist(existing, key) <= PIN_BREAK_MM:
                return existing
        nodes.append(key)
        parent.setdefault(key, key)
        return key

    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for stroke in strokes:
        start = snap(stroke["a"])
        end = snap(stroke["b"])
        if _dist(start, end) < 0.15:
            continue
        union(start, end)
        segments.append((start, end))
    # A perpendicular T is one run. A crossing, or two wires that only pass
    # near each other, stays two conductors.
    for start, end in segments:
        host = _direction(start, end)
        if host is None:
            continue
        for other_start, other_end in segments:
            if (start, end) == (other_start, other_end):
                continue
            branch = _direction(other_start, other_end)
            if branch is None:
                continue
            if abs(host[0] * branch[0] + host[1] * branch[1]) > 0.3:
                continue
            for node in (other_start, other_end):
                if min(_dist(node, start), _dist(node, end)) <= PIN_BREAK_MM:
                    continue
                if _point_seg(node, start, end) <= PIN_BREAK_MM:
                    union(node, start)
    groups: dict[tuple[float, float], list[tuple[float, float]]] = {}
    for node in nodes:
        groups.setdefault(find(node), []).append(node)
    return list(groups.values())


def _direction(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float] | None:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-6:
        return None
    return (dx / length, dy / length)


def _bundle_key(left: int, left_side: str, right: int, right_side: str) -> tuple[int, str, int, str]:
    if (left, left_side) <= (right, right_side):
        return (left, left_side, right, right_side)
    return (right, right_side, left, left_side)


def _touch_side(
    point: tuple[float, float], box: tuple[float, float, float, float], margin: float
) -> str | None:
    x, y = point
    x0, y0, x1, y1 = box
    spans = {
        "top": abs(y - y0) if x0 - margin <= x <= x1 + margin else 1e9,
        "bottom": abs(y - y1) if x0 - margin <= x <= x1 + margin else 1e9,
        "left": abs(x - x0) if y0 - margin <= y <= y1 + margin else 1e9,
        "right": abs(x - x1) if y0 - margin <= y <= y1 + margin else 1e9,
    }
    side = min(spans, key=spans.get)
    if spans[side] > margin:
        return None
    return side


def _outside_box(point: tuple[float, float], box: tuple[float, float, float, float], margin: float) -> bool:
    x, y = point
    x0, y0, x1, y1 = box
    return x < x0 - margin or x > x1 + margin or y < y0 - margin or y > y1 + margin


def _rect_gap(point: tuple[float, float], box: tuple[float, float, float, float]) -> float:
    x, y = point
    x0, y0, x1, y1 = box
    dx = 0.0 if x0 <= x <= x1 else (x0 - x if x < x0 else x - x1)
    dy = 0.0 if y0 <= y <= y1 else (y0 - y if y < y0 else y - y1)
    return (dx * dx + dy * dy) ** 0.5


def _symbol_side_count(symbol: Any, side: str) -> int:
    return sum(1 for pin in symbol.pins if str(pin.side) == side)


_POWER_SHAPES = {"breaker", "contactor", "overload", "motor", "disconnect", "fuse"}


def _pole_family(symbol: Any, tag: str, catalog: Any, parents: set[str]) -> bool:
    """Breakers, contactor poles, overload heaters, motors, disconnects, fuses.

    A coil, pilot, pushbutton, selector, limit, or contact keeps its symbol.
    """

    from panelcad.importer.read import contact_tag_parts
    from panelcad.importer.symbols import shape_class

    if contact_tag_parts(tag, catalog, parents=parents) is not None:
        return False
    lib = str(getattr(symbol, "library_id", "") or "").lower()
    if any(token in lib for token in ("aux_", "ol_contact", ".coil", "pb_", "pilot", "limit_", "selector")):
        return False
    return shape_class(symbol) in _POWER_SHAPES


def _one_sided(symbol: Any) -> bool:
    """A load whose leads share one side. A pass-through has the opposite side too."""

    sides = {str(pin.side) for pin in symbol.pins}
    return not (("top" in sides and "bottom" in sides) or ("left" in sides and "right" in sides))


def _continues_through(counts: dict[str, int]) -> bool:
    return (counts.get("top", 0) >= 1 and counts.get("bottom", 0) >= 1) or (
        counts.get("left", 0) >= 1 and counts.get("right", 0) >= 1
    )


def _rematch_poles(
    token: dict[str, Any],
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    catalog: Any,
    standard: str,
    minimums: dict[str, int],
    devices: list[dict[str, Any]],
    current: dict[str, Any],
    current_symbol: Any,
    side_counts: dict[str, int],
) -> tuple[float, Any, float, float, float] | None:
    from panelcad.importer.symbols import shape_class, tag_prefix

    tag = str(token.get("tag") or "")
    prefix = tag_prefix(tag)
    family = shape_class(current_symbol)
    candidates = _prefix_devices(catalog, prefix, standard)
    if prefix in {"M", "K", "CR", "CON", "MS"}:
        candidates = _with_coils(candidates, catalog, standard)
    through = _continues_through(side_counts)
    fitting = []
    for device in candidates:
        if not _standard_symbol(device, standard):
            continue
        if shape_class(device) != family:
            continue
        if through and _one_sided(device):
            continue
        if any(_symbol_side_count(device, side) < count for side, count in minimums.items()):
            continue
        fitting.append(device)
    if not fitting:
        return None
    left = float(token["x"]) - float(token.get("w") or 0) / 2.0
    base_y = float(token["y"])
    widths = [float(getattr(device, "schematic_width", 0) or 0) for device in fitting]
    heights = [float(getattr(device, "schematic_height", 0) or 0) for device in fitting]
    reach = max(95.0, 28.0 + max(widths or [0.0]), 18.0 + max(heights or [0.0]))
    ink_lines = [stroke for stroke in lines if stroke.get("role") == "ink"]
    symbol_lines = [stroke for stroke in lines if stroke.get("role") in {"ink", "wire"} and not stroke.get("bus")]
    pool = _nearby(ink_lines, left, base_y, reach)
    wide = _nearby(symbol_lines, left, base_y, reach)
    curve_pool = _nearby_curves(curves, left, base_y, reach)
    center = (float(token["x"]), float(token["y"]))
    best = _best_ink(fitting, left, base_y, pool, curve_pool, standard, center=center, wide_lines=wide)
    if best is not None and best[0] >= MATCH_MIN:
        return best
    claimed = []
    for device in devices:
        if device is current or device.get("guess") or not device.get("library_id"):
            continue
        try:
            symbol = catalog.get(str(device["library_id"]))
        except KeyError:
            continue
        ox, oy = float(device["x"]), float(device["y"])
        scale = float(device.get("scale") or 1.0)
        for pin in symbol.pins:
            claimed.append((ox + float(pin.x) * scale, oy + float(pin.y) * scale))
    pinned = _search_pin_landing(
        fitting, left, base_y, wide or pool, curve_pool, standard, center=center, claimed=claimed
    )
    if pinned is not None and pinned[0] >= MATCH_MIN:
        return pinned
    return None


def _contact_symbols(catalog: Any, standard: str, terminal: str) -> list[Any]:
    """Every aux and overload contact for this standard.

    NO or NC comes from the ink. The printed terminal breaks a tie; it does
    not choose the symbol. ``terminal`` is unused here so a suffix that happens
    to equal one pin id cannot hide the other drawing.
    """

    _ = terminal
    found = []
    for device in catalog.devices.values():
        lib = str(getattr(device, "library_id", "") or "").lower()
        if "aux_no" not in lib and "aux_nc" not in lib and "ol_contact" not in lib:
            continue
        if not _standard_symbol(device, standard):
            continue
        found.append(device)
    return found


def _choose_contact(
    candidates: list[Any],
    left: float,
    base_y: float,
    pool: list[dict[str, Any]],
    curve_pool: list[dict[str, Any]],
    standard: str,
    *,
    center: tuple[float, float],
    wide_lines: list[dict[str, Any]],
    terminal: str,
) -> tuple[float, Any, float, float, float] | str | None:
    """NO or NC from the stroke that differs, not from the whole symbol score.

    The NC slash is the blade that reaches the far plate. If that blade lies
    on the ink, the contact is NC. If it does not, the contact is NO. The
    suffix only picks which of those symbols owns the terminal. Needs-info is
    for a sheet where neither symbol can be placed.
    """

    scored: list[tuple[float, Any, float, float, float]] = []
    for device in candidates:
        best = _best_ink(
            [device],
            left,
            base_y,
            pool,
            curve_pool,
            standard,
            center=center,
            wide_lines=wide_lines,
        )
        if best is not None:
            scored.append(best)
    if not scored:
        return None
    opened = [item for item in scored if not _blade_joins(item[1])]
    closed = [item for item in scored if _blade_joins(item[1]) is True]
    if not opened or not closed:
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0]
    # The open symbol's origin is the shared body. The NC search is pulled
    # onto the plates, so it is not the place where the blade is judged.
    opened.sort(key=lambda item: item[0], reverse=True)
    anchor = opened[0]
    ox, oy, scale = anchor[2], anchor[3], anchor[4]
    ink = [stroke for stroke in pool if stroke.get("role") != "wire"]
    drawn = any(_nc_blade_drawn(item[1], ox, oy, ink) for item in closed)
    pool_choice = closed if drawn else opened
    wanted = str(terminal or "")
    chosen = pool_choice[0]
    for item in pool_choice:
        if any(str(pin.id) == wanted for pin in item[1].pins):
            chosen = item
            break
    return (max(chosen[0], MATCH_MIN), chosen[1], ox, oy, scale)


def _best_ink(
    candidates: list[Any],
    left: float,
    base_y: float,
    pool: list[dict[str, Any]],
    curve_pool: list[dict[str, Any]],
    standard: str,
    *,
    center: tuple[float, float] | None = None,
    wide_lines: list[dict[str, Any]] | None = None,
) -> tuple[float, Any, float, float, float] | None:
    if not candidates or (not pool and not curve_pool and not wide_lines):
        return None
    full = _search_ink(candidates, left, base_y, pool, curve_pool, 1.0, standard)
    if full is not None and full[0] >= MATCH_MIN:
        return full
    around = None
    if center is not None:
        around = _search_around_tag(
            candidates,
            center,
            wide_lines if wide_lines is not None else pool,
            curve_pool,
            standard,
        )
        if around is not None and around[0] >= MATCH_MIN:
            return around
    box = _ink_cluster_box(pool, left, base_y)
    scaled_devices = []
    for device in candidates:
        for scale in _fit_scales(device, box):
            scaled_devices.append((device, scale))
    scaled = _search_scaled(scaled_devices, left, base_y, pool, curve_pool, box, standard) if scaled_devices else None
    best = full
    for candidate in (around, scaled):
        if candidate is None:
            continue
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best


def _search_pin_landing(
    candidates: list[Any],
    left: float,
    base_y: float,
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    standard: str,
    *,
    center: tuple[float, float] | None = None,
    claimed: list[tuple[float, float]] | None = None,
) -> tuple[float, Any, float, float, float] | None:
    """Place a symbol whose terminals sit on the drawn conductor ends.

    The body ink can be a different generation of the same symbol. Two
    terminals have to land on the ends, and at least half the pins. Devices
    that land on the same ends are separated by the contact blade: a closed
    blade is the symbol whose slanted stroke meets the other side.
    """

    wired, targets = _terminal_targets(lines, curves)
    if claimed:
        # A terminal already used by a placed device is not free for the next tag.
        targets = [point for point in targets if not _claimed(point, claimed)]
        wired = [point for point in wired if not _claimed(point, claimed)]
    if len(targets) < 2 or not candidates:
        return None
    nearest = _nearest_target(targets)
    nearest_wire = _nearest_target(wired) if wired else None
    cx, cy = center if center is not None else (left + 2.0, base_y)
    found: list[tuple[tuple[int, int, float], float, float, Any]] = []
    for device in candidates:
        pins = list(getattr(device, "pins", []) or [])
        if len(pins) < 2:
            continue
        width = float(getattr(device, "schematic_width", 0) or 0)
        height = float(getattr(device, "schematic_height", 0) or 0)
        if width < 4.0 or height < 4.0 or width > 160.0 or height > 160.0:
            continue
        peak = _best_pin_origin(device, cx, cy, width, height, nearest, nearest_wire)
        if peak is None:
            continue
        found.append(peak)
    if not found:
        return None
    found.sort(key=lambda item: item[0], reverse=True)
    top = found[0][0]
    tied = [item for item in found if item[0] == top]
    chosen = _break_pin_tie(tied, lines, curves, standard)
    if chosen is None:
        return None
    _key, ox, oy, device = chosen
    # Above the ink floor, below a real stroke match, so ink still wins a tie.
    return (MATCH_MIN + 0.02, device, ox, oy, 1.0)


def _best_pin_origin(
    device: Any,
    cx: float,
    cy: float,
    width: float,
    height: float,
    nearest: Any,
    nearest_wire: Any = None,
) -> tuple[tuple[int, int, float], float, float, Any] | None:
    pins = list(device.pins)
    # Half the pins, and never fewer than two.
    needed = max(2, (len(pins) + 1) // 2)
    best: tuple[tuple[int, int, float], float, float] | None = None

    def consider(ox: float, oy: float) -> None:
        nonlocal best
        if not _tag_near_symbol(cx, cy, ox, oy, width, height):
            return
        wired, tight, snapped, error = _pin_fit(pins, ox, oy, nearest, nearest_wire)
        if tight < needed:
            return
        # Wired terminals first: an unwired contact dot must not steal the origin.
        # Then more terminals, a tighter fit, and the symbol nearest the tag.
        dist = _dist((cx, cy), (ox, oy))
        key = (wired, tight, snapped, -round(error, 2), -round(dist, 1))
        if best is None or key > best[0]:
            best = (key, ox, oy)

    for ox in _steps(cx - width, cx + 6.0, 2.0):
        for oy in _steps(cy - height, cy + 8.0, 2.0):
            consider(ox, oy)
    if best is None:
        return None
    px, py = best[1], best[2]
    for ox in _steps(px - 2.0, px + 2.0, 0.5):
        for oy in _steps(py - 2.0, py + 2.0, 0.5):
            consider(ox, oy)
    if best is None:
        return None
    return (best[0], best[1], best[2], device)


def _claimed(point: tuple[float, float], claimed: list[tuple[float, float]]) -> bool:
    return any(_dist(point, other) <= PIN_BREAK_MM for other in claimed)


def _tag_near_symbol(tx: float, ty: float, ox: float, oy: float, width: float, height: float) -> bool:
    """The tag sits on the symbol or just above its top-left, not on another device."""

    return (ox - 6.0) <= tx <= (ox + width + 6.0) and (oy - 8.0) <= ty <= (oy + height + 8.0)


def _pin_fit(
    pins: list[Any],
    ox: float,
    oy: float,
    nearest: Any,
    nearest_wire: Any = None,
) -> tuple[int, int, int, float]:
    wired = 0
    tight = 0
    snapped = 0
    error = 0.0
    for pin in pins:
        point = (ox + float(pin.x), oy + float(pin.y))
        dist = float(nearest(point))
        on_wire = nearest_wire is not None and float(nearest_wire(point)) <= PIN_BREAK_MM
        if dist <= PIN_SNAP_MM or on_wire:
            snapped += 1
        if dist <= PIN_BREAK_MM or on_wire:
            tight += 1
            error += dist if dist <= PIN_BREAK_MM else float(nearest_wire(point))
            if on_wire:
                wired += 1
    return wired, tight, snapped, error


def _terminal_targets(
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Conductor ends, then every stroke end and junction dot.

    A terminal that the wire actually reaches outranks a dot on an unwired contact.
    """

    wired: list[tuple[float, float]] = []
    points: list[tuple[float, float]] = []
    for stroke in lines:
        if stroke.get("role") == "frame":
            continue
        pair = ((float(stroke["a"][0]), float(stroke["a"][1])), (float(stroke["b"][0]), float(stroke["b"][1])))
        points.extend(pair)
        if stroke.get("role") == "wire":
            wired.extend(pair)
    for stroke in curves:
        pts = stroke.get("points") or []
        if len(pts) < 3:
            continue
        xs = [float(point[0]) for point in pts]
        ys = [float(point[1]) for point in pts]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span <= 2.2:
            points.append(((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0))
    return wired, points


def _nearest_target(points: list[tuple[float, float]]):
    cell = 3.0
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for point in points:
        key = (int(point[0] // cell), int(point[1] // cell))
        buckets.setdefault(key, []).append(point)

    def nearest(point: tuple[float, float]) -> float:
        # Squared distance picks the same neighbour as ``_dist`` (sqrt is increasing).
        # The returned length is still ``_dist`` of that neighbour.
        px, py = point
        ix, iy = int(px // cell), int(py // cell)
        best_d = 1e18
        best: tuple[float, float] | None = None
        for dx in (-1, 0, 1):
            col = ix + dx
            for dy in (-1, 0, 1):
                for ox, oy in buckets.get((col, iy + dy), ()):
                    ddx = px - ox
                    ddy = py - oy
                    d2 = ddx * ddx + ddy * ddy
                    if d2 < best_d:
                        best_d = d2
                        best = (ox, oy)
        if best is None:
            return 1e9
        return _dist(point, best)

    return nearest


def _break_pin_tie(
    tied: list[tuple[tuple[int, int, float], float, float, Any]],
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    standard: str,
) -> tuple[tuple[int, int, float], float, float, Any] | None:
    """One device, or the blade that matches the drawing. A dead heat stays unmatched."""

    if len(tied) == 1:
        return tied[0]
    if _same_pin_geometry(tied):
        drawn = _drawn_joins(tied[0], lines)
        voted = []
        for item in tied:
            blade = _blade_joins(item[3])
            if blade is None:
                continue
            if blade == drawn:
                voted.append(item)
        if len(voted) == 1:
            return voted[0]
        if len(voted) > 1:
            tied = voted
        elif drawn is not None and any(_blade_joins(item[3]) is not None for item in tied):
            # The drawing says open or closed and none of the symbols agree.
            return None
    scored: list[tuple[float, int, tuple[tuple[int, int, float], float, float, Any]]] = []
    for item in tied:
        _key, ox, oy, device = item
        ink = _ink_score(device, ox, oy, lines, curves, 1.0)
        scored.append((ink, -_std_rank(device, standard), item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    if len(scored) == 1:
        return scored[0][2]
    if scored[0][0] >= scored[1][0] + 0.08:
        return scored[0][2]
    if abs(scored[0][0] - scored[1][0]) < 0.08 and scored[0][1] != scored[1][1]:
        return scored[0][2]
    # Same terminal names: either symbol builds the same wire. Different names
    # would put that wire on the wrong pin, so the tag stays unmatched.
    if _same_terminals(tied):
        return scored[0][2]
    if _same_pin_geometry(tied):
        return None
    return scored[0][2]


def _same_terminals(tied: list[tuple[tuple[int, int, float], float, float, Any]]) -> bool:
    signatures = set()
    for _key, ox, oy, device in tied:
        pts = tuple(
            sorted(
                (str(pin.id), round(ox + float(pin.x), 0), round(oy + float(pin.y), 0))
                for pin in device.pins
            )
        )
        signatures.add(pts)
    return len(signatures) == 1


def _same_pin_geometry(tied: list[tuple[tuple[int, int, float], float, float, Any]]) -> bool:
    signatures = set()
    for _key, ox, oy, device in tied:
        pts = tuple(sorted((round(ox + float(pin.x), 0), round(oy + float(pin.y), 0)) for pin in device.pins))
        signatures.add(pts)
    return len(signatures) == 1


def _blade_joins(device: Any) -> bool | None:
    """Whether the slanted contact stroke meets both sides of the symbol.

    The near end of every blade sits on its own terminal. A closed contact is
    one whose far end also meets a stroke. ``None`` means there is no blade.
    """

    segs = _symbol_segments(device, 0.0, 0.0, 1.0)
    slanted = [
        seg
        for seg in segs
        if abs(seg[0][0] - seg[1][0]) >= 0.8 and abs(seg[0][1] - seg[1][1]) >= 0.8
    ]
    if not slanted:
        return None
    for seg in slanted:
        if _segment_joins(seg, segs):
            return True
    return False


def _nc_blade_drawn(device: Any, ox: float, oy: float, lines: list[dict[str, Any]]) -> bool:
    """The closed blade is on the page when its middle lies along a symbol stroke.

    The blade's ends sit on the plates, which an NO contact draws too. Endpoint
    distance therefore cannot tell them apart. The middle of the slash has to
    follow a non-wire stroke in the same direction.
    """

    segs = _symbol_segments(device, ox, oy, 1.0)
    blades = [
        seg
        for seg in segs
        if abs(seg[0][0] - seg[1][0]) >= 0.8
        and abs(seg[0][1] - seg[1][1]) >= 0.8
        and _segment_joins(seg, segs)
    ]
    if not blades:
        return False
    ink = [
        ((float(stroke["a"][0]), float(stroke["a"][1])), (float(stroke["b"][0]), float(stroke["b"][1])))
        for stroke in lines
        if stroke.get("role") != "wire"
    ]
    return any(_lies_along(blade, ink) for blade in blades)


def _segments_intersect(
    seg_a: tuple[tuple[float, float], tuple[float, float]],
    seg_b: tuple[tuple[float, float], tuple[float, float]],
) -> bool:
    def _ccw(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
        return (p2[0] - p1[0]) * (p3[1] - p1[1]) - (p2[1] - p1[1]) * (p3[0] - p1[0])

    a0, a1 = seg_a
    b0, b1 = seg_b
    if (
        max(a0[0], a1[0]) + 1e-6 < min(b0[0], b1[0])
        or max(b0[0], b1[0]) + 1e-6 < min(a0[0], a1[0])
        or max(a0[1], a1[1]) + 1e-6 < min(b0[1], b1[1])
        or max(b0[1], b1[1]) + 1e-6 < min(a0[1], a1[1])
    ):
        return False
    d1 = _ccw(a0, a1, b0)
    d2 = _ccw(a0, a1, b1)
    d3 = _ccw(b0, b1, a0)
    d4 = _ccw(b0, b1, a1)
    return (d1 * d2 <= 1e-9) and (d3 * d4 <= 1e-9)


def _segment_joins(
    seg: tuple[tuple[float, float], tuple[float, float]],
    segs: list[tuple[tuple[float, float], tuple[float, float]]],
) -> bool:
    hosts: list[set[int]] = []
    for end in seg:
        held = {id(other) for other in segs if other != seg and _point_seg(end, other[0], other[1]) <= 0.8}
        hosts.append(held)
    if hosts[0] and hosts[1] and hosts[0] != hosts[1]:
        return True
    crossed = {
        id(other)
        for other in segs
        if other != seg
        and (
            _segments_intersect(seg, other)
            or _point_seg(seg[0], other[0], other[1]) <= 0.8
            or _point_seg(seg[1], other[0], other[1]) <= 0.8
        )
    }
    return len(crossed) >= 2


def _lies_along(
    seg: tuple[tuple[float, float], tuple[float, float]],
    strokes: list[tuple[tuple[float, float], tuple[float, float]]],
) -> bool:
    """Interior points of ``seg`` follow one drawn stroke, not merely its ends."""

    ax, ay = seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]
    length = (ax * ax + ay * ay) ** 0.5
    if length < 1.0:
        return False
    samples = [((seg[0][0] + t * ax), (seg[0][1] + t * ay)) for t in (0.35, 0.5, 0.65)]
    for start, end in strokes:
        bx, by = end[0] - start[0], end[1] - start[1]
        span = (bx * bx + by * by) ** 0.5
        if span < 1.0:
            continue
        dot = abs(ax * bx + ay * by) / (length * span)
        if dot < 0.906:  # about 25 degrees
            continue
        if all(_point_seg(point, start, end) <= 1.2 for point in samples):
            return True
    return False


def _drawn_joins(
    item: tuple[tuple[int, int, float], float, float, Any],
    lines: list[dict[str, Any]],
) -> bool:
    """A symbol-weight stroke runs through two of the placed terminals."""

    _key, ox, oy, device = item
    pins = [(ox + float(pin.x), oy + float(pin.y)) for pin in device.pins]
    strokes = [
        ((float(stroke["a"][0]), float(stroke["a"][1])), (float(stroke["b"][0]), float(stroke["b"][1])))
        for stroke in lines
        if stroke.get("role") != "wire"
    ]
    for index, left in enumerate(pins):
        for right in pins[index + 1 :]:
            for start, end in strokes:
                if _point_seg(left, start, end) <= 0.9 and _point_seg(right, start, end) <= 0.9:
                    return True
    return False


def _search_around_tag(
    candidates: list[Any],
    center: tuple[float, float],
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    standard: str,
) -> tuple[float, Any, float, float, float] | None:
    """Origin search for a tag that is not sitting above-left of the symbol.

    A ladder tag is centered on the rung, a coil tag sits inside the circle,
    and an aux contact tag sits under the symbol. The window is the candidate's
    own width and height. A drawn circle proposes the origin, so the search
    does not walk every millimetre of a large symbol.
    """

    cx, cy = center
    drawn = _drawn_circles(curves)
    best: tuple[float, Any, float, float, float] | None = None
    for device in candidates:
        width = float(getattr(device, "schematic_width", 0) or 0)
        height = float(getattr(device, "schematic_height", 0) or 0)
        if width < 4.0 or height < 4.0 or width > 80.0 or height > 80.0:
            continue
        seeds = _circle_origins(device, drawn, cx, cy, width, height)
        peak_score = -1.0
        peak = (cx - width * 0.5, cy - height * 0.5)
        for ox, oy in seeds:
            score = _ink_score(
                device, ox, oy, lines, curves, 1.0, limit=peak_score if peak_score > 0 else None
            )
            if score > peak_score:
                peak_score = score
                peak = (ox, oy)
        if peak_score < MATCH_MIN:
            # Line symbols (a contact has no circle) use a grid. A small symbol
            # needs a closer step so the pin still lands on the conductor.
            # A large body that the first search missed is not worth a fine grid.
            step = 2.0 if max(width, height) <= 40.0 else 4.0
            for ox in _steps(cx - width, cx + 4.0, step):
                for oy in _steps(cy - height - 6.0, cy + 8.0, step):
                    score = _ink_score(
                        device, ox, oy, lines, curves, 1.0, limit=peak_score if peak_score > 0 else None
                    )
                    if score > peak_score:
                        peak_score = score
                        peak = (ox, oy)
        for ox in _steps(peak[0] - 2.4, peak[0] + 2.4, 0.8):
            for oy in _steps(peak[1] - 2.4, peak[1] + 2.4, 0.8):
                held = None if best is None else best[0] - 0.002
                score = _ink_score(device, ox, oy, lines, curves, 1.0, limit=held)
                best = _keep_ink(best, score, device, ox, oy, 1.0, standard)
    return best


def _drawn_circles(curves: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    """Circles implied by curve strokes. Four quadrants of one circle count once."""

    boxes: list[list[float]] = []
    for stroke in curves:
        pts = stroke.get("points") or []
        if len(pts) < 3:
            continue
        xs = [float(point[0]) for point in pts]
        ys = [float(point[1]) for point in pts]
        boxes.append([min(xs), min(ys), max(xs), max(ys)])
    changed = True
    while changed and len(boxes) > 1:
        changed = False
        merged: list[list[float]] = []
        used: set[int] = set()
        for index, box in enumerate(boxes):
            if index in used:
                continue
            x0, y0, x1, y1 = box
            for other in range(index + 1, len(boxes)):
                if other in used:
                    continue
                bx0, by0, bx1, by1 = boxes[other]
                if bx0 <= x1 + 4.0 and x0 <= bx1 + 4.0 and by0 <= y1 + 4.0 and y0 <= by1 + 4.0:
                    x0, y0, x1, y1 = min(x0, bx0), min(y0, by0), max(x1, bx1), max(y1, by1)
                    used.add(other)
                    changed = True
            used.add(index)
            merged.append([x0, y0, x1, y1])
        boxes = merged
    found: list[tuple[float, float, float]] = []
    for x0, y0, x1, y1 in boxes:
        wide, high = x1 - x0, y1 - y0
        if wide < 3.0 or high < 3.0 or abs(wide - high) > max(wide, high) * 0.45:
            continue
        found.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0, max(wide, high) / 2.0))
    return found


def _circle_origins(
    device: Any,
    drawn: list[tuple[float, float, float]],
    cx: float,
    cy: float,
    width: float,
    height: float,
) -> list[tuple[float, float]]:
    origins: list[tuple[float, float]] = []
    for lx, ly, radius in _symbol_circles(device, 0.0, 0.0, 1.0):
        for dx, dy, drawn_r in drawn:
            if abs(drawn_r - radius) > max(2.5, 0.4 * radius):
                continue
            ox, oy = dx - lx, dy - ly
            if ox < cx - width - 8.0 or ox > cx + 8.0 or oy < cy - height - 12.0 or oy > cy + 12.0:
                continue
            origins.append((ox, oy))
    return origins


def _search_ink(
    candidates: list[Any],
    left: float,
    base_y: float,
    pool: list[dict[str, Any]],
    curve_pool: list[dict[str, Any]],
    scale: float,
    standard: str,
) -> tuple[float, Any, float, float, float] | None:
    # Device tags are drawn above the symbol and stepped to its left until they
    # clear wire labels and captions. The origin is down-right of the tag's
    # left edge. A seed on the tag itself drops the symbol that was drawn.
    xs = _steps(left - 1.5, left + 26.0, 1.5)
    ys = _steps(base_y + 1.0, base_y + 16.0, 1.5)
    peaks: list[tuple[float, Any, float, float]] = []
    # Identical symbols score the same. Keep the one under this tag, not the next device.
    anchor = (left, base_y + 4.0)
    for device in candidates:
        best_score = -1.0
        best_at = (left, base_y + 3.5)
        best_dist = 1e18
        for ox in xs:
            for oy in ys:
                held = None if best_score <= 0.02 else best_score - 0.02
                score = _ink_score(device, ox, oy, pool, curve_pool, scale, limit=held)
                dist = (ox - anchor[0]) ** 2 + (oy - anchor[1]) ** 2
                # A copy one pitch away can score a hair higher. Stay with this tag
                # unless the other origin is a clearly better drawing of the symbol.
                if score > best_score + 0.02 or (score >= best_score - 0.02 and dist < best_dist):
                    best_score = score
                    best_at = (ox, oy)
                    best_dist = dist
        peaks.append((best_score, device, best_at[0], best_at[1]))
    peaks.sort(key=lambda item: item[0], reverse=True)
    short = [row for row in peaks if row[0] >= 0.3][:6] or peaks[:4]
    best: tuple[float, Any, float, float, float] | None = None
    for _score, device, px, py in short:
        for ox in _steps(px - 1.6, px + 1.6, 0.4):
            for oy in _steps(py - 1.6, py + 1.6, 0.4):
                held = None if best is None else best[0] - 0.002
                score = _ink_score(device, ox, oy, pool, curve_pool, scale, limit=held)
                best = _keep_ink(best, score, device, ox, oy, scale, standard, near=anchor)
    return best


def _steps(start: float, stop: float, step: float) -> list[float]:
    values: list[float] = []
    cursor = start
    while cursor <= stop + 1e-6:
        values.append(cursor)
        cursor += step
    return values


def _search_scaled(
    scaled_devices: list[tuple[Any, float]],
    left: float,
    base_y: float,
    pool: list[dict[str, Any]],
    curve_pool: list[dict[str, Any]],
    box: tuple[float, float, float, float] | None,
    standard: str,
) -> tuple[float, Any, float, float, float] | None:
    best: tuple[float, Any, float, float, float] | None = None
    bx = box[0] if box else left
    by = box[1] if box else base_y
    for device, scale in scaled_devices:
        for ox in (left - 1.0, left - 0.5, left, left + 0.5, bx - 0.5, bx, bx + 0.5):
            for oy in (by - 0.5, by, by + 0.5, base_y + 1.0, base_y + 1.5, base_y + 2.0):
                held = None if best is None else best[0] - 1e-6
                score = _ink_score(device, ox, oy, pool, curve_pool, scale, limit=held)
                best = _keep_ink(best, score, device, ox, oy, scale, standard)
    return best


def _keep_ink(
    best: tuple[float, Any, float, float, float] | None,
    score: float,
    device: Any,
    ox: float,
    oy: float,
    scale: float,
    standard: str,
    near: tuple[float, float] | None = None,
) -> tuple[float, Any, float, float, float] | None:
    if best is None or score > best[0] + 1e-6:
        return (score, device, ox, oy, scale)
    # Same symbol, same ink. The copy under this tag wins over the next device.
    if near is not None and device is best[1] and abs(score - best[0]) <= 0.002:
        old = (best[2] - near[0]) ** 2 + (best[3] - near[1]) ** 2
        new = (ox - near[0]) ** 2 + (oy - near[1]) ** 2
        if new + 1e-6 < old:
            return (score, device, ox, oy, scale)
    if abs(score - best[0]) <= 1e-6 and _std_rank(device, standard) < _std_rank(best[1], standard):
        return (score, device, ox, oy, scale)
    return best


def _std_rank(device: Any, standard: str) -> int:
    if not standard:
        return 0
    return 0 if getattr(device, "standard", "") in {standard, ""} else 1


def _ink_cluster_box(
    lines: list[dict[str, Any]], left: float, base_y: float
) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for stroke in lines:
        ax, ay = float(stroke["a"][0]), float(stroke["a"][1])
        bx, by = float(stroke["b"][0]), float(stroke["b"][1])
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        if left - 6.0 <= mx <= left + 40.0 and base_y - 4.0 <= my <= base_y + 36.0:
            xs.extend((ax, bx))
            ys.extend((ay, by))
    if len(xs) < 8:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _fit_scales(device: Any, box: tuple[float, float, float, float] | None) -> list[float]:
    """Scales that would make the library symbol the size of the ink under the tag.

    Width and height are tried separately. A symbol's strokes often fill its
    height and leave a margin on the width, so the average of the two is the
    wrong size.
    """

    if box is None:
        return []
    bw, bh = box[2] - box[0], box[3] - box[1]
    sw = float(getattr(device, "schematic_width", 0) or 0)
    sh = float(getattr(device, "schematic_height", 0) or 0)
    if sw < 8.0 or sh < 8.0 or bw < 3.0 or bh < 3.0:
        return []
    found: list[float] = []
    for scale in (bw / sw, bh / sh):
        if 0.12 <= scale <= 0.92 and all(abs(scale - other) > 0.008 for other in found):
            found.append(scale)
    return found


def _match_frame(
    token: dict[str, Any],
    rects: list[dict[str, Any]],
    page_tokens: list[dict[str, Any]],
    candidates: list[Any],
) -> tuple[Any, dict[str, Any], str] | None:
    """I/O cards and network nodes are rectangles, not the schematic symbol."""

    from panelcad.library.ports import effective_ports
    from panelcad.render.io_ladder import io_card_size, is_io_module
    from panelcad.render.network import node_size

    tx, ty = float(token["x"]), float(token["y"])
    boxes = []
    for rect in rects:
        if rect.get("role") == "frame":
            continue
        width, height = float(rect["w"]), float(rect["h"])
        if width < 50.0 or width > 100.0 or height < 30.0 or height > 230.0:
            continue
        if not (float(rect["x"]) <= tx <= float(rect["x"]) + width and float(rect["y"]) - 1.0 <= ty <= float(rect["y"]) + 18.0):
            continue
        if abs(tx - (float(rect["x"]) + width / 2.0)) > 14.0:
            continue
        boxes.append(rect)
    if not boxes:
        return None
    fits: list[tuple[int, float, Any, dict[str, Any], str]] = []
    for device in candidates:
        options: list[tuple[str, float, float]] = []
        if is_io_module(device):
            cw, ch = io_card_size(device)
            options.append(("io", cw, ch))
        ports = effective_ports(device)
        if ports:
            nw, nh = node_size(len(ports))
            options.append(("network", nw, nh))
        for frame, expect_w, expect_h in options:
            for rect in boxes:
                err = abs(float(rect["w"]) - expect_w) + abs(float(rect["h"]) - expect_h)
                if err > 8.0:
                    continue
                fits.append((_name_score(device, page_tokens, rect), -err, device, rect, frame))
    if not fits:
        return None
    fits.sort(key=lambda item: (item[0], item[1]), reverse=True)
    names, neg_err, device, rect, frame = fits[0]
    tied = len(fits) > 1 and fits[1][0] == names and abs(fits[1][1] - neg_err) < 0.5
    if tied or (names == 0 and len(fits) > 1):
        # Same outline, no name in the box. The printed catalog number is the name.
        return _break_frame_tie(fits, page_tokens)
    return device, rect, frame


def _name_score(device: Any, tokens: list[dict[str, Any]], rect: dict[str, Any]) -> int:
    import re

    words = set()
    raw = f"{device.name} {' '.join(device.tags)}"
    for word in re.split(r"[^A-Za-z0-9]+", raw.upper()):
        if len(word) >= 2:
            words.add(word)
    hits = 0
    x0, y0 = float(rect["x"]), float(rect["y"])
    x1, y1 = x0 + float(rect["w"]), y0 + float(rect["h"])
    for tok in tokens:
        if not (x0 - 1 <= float(tok["x"]) <= x1 + 1 and y0 - 1 <= float(tok["y"]) <= y1 + 1):
            continue
        word = re.sub(r"[^A-Za-z0-9]+", "", str(tok["text"]).upper())
        if len(word) >= 2 and word in words:
            hits += 1
    return hits


_PART_WORDS: dict[str, set[str]] | None = None


def _part_catalog_words(library_id: str) -> set[str]:
    """Catalog numbers printed inside a network node, keyed by library id."""

    global _PART_WORDS
    if _PART_WORDS is None:
        import re

        from panelcad.library.parts import default_parts_catalog

        grouped: dict[str, set[str]] = {}
        for part in default_parts_catalog().parts.values():
            word = re.sub(r"[^A-Za-z0-9]+", "", str(part.catalog or "").upper())
            if len(word) >= 4:
                grouped.setdefault(part.library_id, set()).add(word)
        _PART_WORDS = grouped
    return _PART_WORDS.get(library_id, set())


def _catalog_name_hits(device: Any, tokens: list[dict[str, Any]], rect: dict[str, Any]) -> int:
    import re

    words = _part_catalog_words(str(getattr(device, "library_id", "") or ""))
    if not words:
        return 0
    hits = 0
    x0, y0 = float(rect["x"]), float(rect["y"])
    x1, y1 = x0 + float(rect["w"]), y0 + float(rect["h"])
    for tok in tokens:
        if not (x0 - 1 <= float(tok["x"]) <= x1 + 1 and y0 - 1 <= float(tok["y"]) <= y1 + 1):
            continue
        word = re.sub(r"[^A-Za-z0-9]+", "", str(tok["text"]).upper())
        if len(word) >= 4 and word in words:
            hits += 1
    return hits


def _break_frame_tie(
    fits: list[tuple[int, float, Any, dict[str, Any], str]],
    tokens: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any], str] | None:
    """Pick the device whose catalog number is printed in the box."""

    best_names, best_err = fits[0][0], fits[0][1]
    cluster = [row for row in fits if row[0] == best_names and abs(row[1] - best_err) < 0.5]
    scored: list[tuple[int, Any, dict[str, Any], str]] = []
    for _names, _neg_err, device, rect, frame in cluster:
        scored.append((_catalog_name_hits(device, tokens, rect), device, rect, frame))
    scored.sort(key=lambda row: row[0], reverse=True)
    if not scored or scored[0][0] <= 0:
        return None
    winner = str(getattr(scored[0][1], "library_id", "") or "")
    if any(row[0] == scored[0][0] and str(getattr(row[1], "library_id", "") or "") != winner for row in scored):
        return None
    return scored[0][1], scored[0][2], scored[0][3]


def _trace_wires(
    labels: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    catalog: Any,
    page_tokens: list[dict[str, Any]] | None = None,
    width_mm: float = 0.0,
    height_mm: float = 0.0,
) -> list[dict[str, Any]]:
    pins = _pin_points(devices, catalog)
    conductors = [stroke for stroke in lines if stroke.get("role") == "wire" and stroke.get("kind", "line") == "line"]
    clusters = [
        {
            "tag": str(device.get("tag") or ""),
            "box": tuple(device["ink_box"]),
            "x": float(device["text_x"]) if device.get("text_x") is not None else float(device.get("x") or 0),
            "y": float(device["text_y"]) if device.get("text_y") is not None else float(device.get("y") or 0),
        }
        for device in devices
        if device.get("ink_box") and (device.get("guess") or not device.get("library_id"))
    ]
    labels = [label for label in labels if not _buried_label(label, devices, catalog, conductors)]
    # No conductor was traced. Every number that was read is still a wire to connect by hand.
    if not conductors:
        return [_hand_wire(label) for label in labels if _wire_question(label)]
    chains = _traced_chains(
        conductors,
        pins,
        labels,
        page_tokens or [],
        clusters,
        split_tees=any(stroke.get("geom") for stroke in conductors),
    )
    buses = [stroke for stroke in conductors if stroke.get("bus")]
    line_refs = _line_reference_keys(page_tokens or [], conductors, width_mm, height_mm)
    labels = [label for label in labels if not _pin_digit(label, pins, devices, catalog)]
    # A number in the margin is still a wire number when it sits on a conductor.
    # The line-reference column does not, so it stays out.
    labels = _numbers_on_conductors(labels, page_tokens or [], conductors, line_refs, catalog)
    labels = [label for label in labels if _label_key(label) not in line_refs]
    assigned: dict[int, dict[str, Any]] = {}
    held: list[dict[str, Any]] = []
    for label in labels:
        index = _label_chain_index(label, chains, buses)
        if index is None:
            continue
        if index in assigned or _same_device_chain(chains[index]):
            held.append(label)
            continue
        assigned[index] = label
    wires: list[dict[str, Any]] = []
    seen_labels: set[int] = set()
    covered: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    for index, label in assigned.items():
        seen_labels.add(id(label))
        wire = _wire_from_ends(label, chains[index]["ends"])
        if wire.get("connect_by_hand") and (
            not _wire_question(label) or _label_key(label) in line_refs or _rail_column_number(label, conductors)
        ):
            continue
        wires.append(wire)
        pair = _terminal_pair(wire)
        if pair is not None:
            covered.add(pair)
    for label in held:
        seen_labels.add(id(label))
        if _wire_question(label) and _label_key(label) not in line_refs and not _rail_column_number(label, conductors):
            wires.append(_hand_wire(label))
    for index, chain in enumerate(chains):
        if index in assigned:
            continue
        wire = _unnamed_wire(chain)
        if wire is None:
            continue
        pair = _terminal_pair(wire)
        if pair is None or pair in covered:
            continue
        covered.add(pair)
        wires.append(wire)
    ink = [segment for chain in chains for segment in chain["segments"]]
    for label in labels:
        if id(label) in seen_labels:
            continue
        if not _wire_question(label) or _label_key(label) in line_refs or _rail_column_number(label, conductors):
            continue
        # Text that is not sitting on a conductor is still a number that was read.
        if ink and _point_chain(float(label["x"]), float(label["y"]), ink) <= LABEL_MM:
            wires.append(_ambiguous_wire(label))
            continue
        wires.append(_hand_wire(label))
    # A terminal can carry two conductors. Dropping the second one deletes a
    # daisy chain (two ethernet drops on one port, two returns on one 0V pin).
    return wires


def _release_duplicate_pins(wires: list[dict[str, Any]], devices: list[dict[str, Any]]) -> None:
    """One conductor per terminal. The closer end keeps the pin; the other is a question.

    A rail name is shared by every rung, so those ends are left alone.
    Tracing does not call this. A terminal can carry two conductors, and
    dropping the second one deletes a daisy chain.
    """

    centers = {
        str(device.get("tag") or ""): (float(device.get("x") or 0), float(device.get("y") or 0))
        for device in devices
        if device.get("tag")
    }
    claimed: dict[tuple[str, str], list[tuple[dict[str, Any], str, str]]] = {}
    for wire in wires:
        if wire.get("ambiguous") or wire.get("connect_by_hand"):
            continue
        for tag_key, pin_key, x_key, y_key in (
            ("from_tag", "from_pin", "from_x", "from_y"),
            ("to_tag", "to_pin", "to_x", "to_y"),
        ):
            tag = str(wire.get(tag_key) or "")
            pin = str(wire.get(pin_key) or "")
            if not tag or not pin or tag == pin:
                continue
            claimed.setdefault((tag, pin), []).append((wire, x_key, y_key))
    for (tag, _pin), items in claimed.items():
        if len(items) < 2:
            continue
        center = centers.get(tag)

        def distance(item: tuple[dict[str, Any], str, str]) -> float:
            wire, x_key, y_key = item
            if center is None or wire.get(x_key) is None or wire.get(y_key) is None:
                return 1e9
            return _dist(center, (float(wire[x_key]), float(wire[y_key])))

        ranked = sorted(items, key=distance)
        for wire, _x_key, _y_key in ranked[1:]:
            if wire.get("connect_by_hand"):
                continue
            number = str(wire.get("number") or "")
            wire.update(
                {
                    "from_tag": "",
                    "to_tag": "",
                    "from_pin": "",
                    "to_pin": "",
                    "ambiguous": True,
                    "connect_by_hand": bool(number),
                    "confidence": 0.4,
                }
            )


def _numbers_on_conductors(
    labels: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    conductors: list[dict[str, Any]],
    line_refs: set[tuple[float, float, str]],
    catalog: Any = None,
) -> list[dict[str, Any]]:
    """A wire number printed on a conductor is kept even when it sits in the margin.

    The title band and a line-reference column are not conductors. A rating is
    not a wire number.
    """

    import re

    from panelcad.importer.models import CONVENTIONAL_WIRES
    from panelcad.importer.read import is_rating, is_wire_label

    if not conductors or not tokens:
        return labels
    segments = [(tuple(stroke["a"]), tuple(stroke["b"])) for stroke in conductors if "a" in stroke]
    known = {_label_key(label) for label in labels}
    extra: list[dict[str, Any]] = []
    for token in tokens:
        text = str(token.get("text") or "").strip()
        if not text or text.upper() in CONVENTIONAL_WIRES or not is_wire_label(text, catalog):
            continue
        if not re.fullmatch(r"\d{1,5}", text):
            continue
        if is_rating(token, tokens):
            continue
        try:
            x = float(token["x"])
            y = float(token["y"])
        except (KeyError, TypeError, ValueError):
            continue
        row = {"text": text, "number": text, "x": x, "y": y, "h": token.get("h") or 2.5, "w": token.get("w") or 0}
        key = _label_key(row)
        if key in known or key in line_refs:
            continue
        if _point_chain(x, y, segments) > _sit_mm(row):
            continue
        extra.append(row)
        known.add(key)
    return labels + extra


def _label_key(label: dict[str, Any]) -> tuple[float, float, str]:
    return (round(float(label["x"]), 1), round(float(label["y"]), 1), str(label.get("text") or label.get("number") or ""))


def _terminal_pair(wire: dict[str, Any]) -> tuple[tuple[str, str], tuple[str, str]] | None:
    if wire.get("ambiguous"):
        return None
    left = (str(wire.get("from_tag") or ""), str(wire.get("from_pin") or ""))
    right = (str(wire.get("to_tag") or ""), str(wire.get("to_pin") or ""))
    if not left[0] or not left[1] or not right[0] or not right[1]:
        return None
    if left == right or left[0] == right[0]:
        return None
    # A run from one rail to the other is the sheet border, not a conductor.
    if left[0] == left[1] and right[0] == right[1]:
        return None
    return tuple(sorted((left, right)))  # type: ignore[return-type]


def _same_device_chain(chain: dict[str, Any]) -> bool:
    ends = [end for end in (chain.get("ends") or []) if isinstance(end, dict)]
    if len(ends) < 2:
        return False
    left, right = ends[0], ends[1]
    if left.get("rail") or right.get("rail"):
        return False
    tag = str(left.get("tag") or "")
    return bool(tag) and tag == str(right.get("tag") or "")


def _unnamed_wire(chain: dict[str, Any]) -> dict[str, Any] | None:
    """A conductor that lands on two terminals and has no printed number."""

    segments = chain.get("segments") or []
    if not segments:
        return None
    mid = segments[len(segments) // 2]
    label = {
        "x": (mid[0][0] + mid[1][0]) / 2.0,
        "y": (mid[0][1] + mid[1][1]) / 2.0,
        "text": "",
        "number": "",
    }
    wire = _wire_from_ends(label, chain.get("ends") or [])
    if _terminal_pair(wire) is None:
        return None
    wire["confidence"] = 0.8
    return wire


def _label_segments(
    chain: dict[str, Any],
    buses: list[dict[str, Any]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A rail wire is named by the label on its own stub, not by text beside the rail."""

    segments = list(chain.get("segments") or [])
    ends = chain.get("ends") or []
    if not any(isinstance(end, dict) and end.get("rail") for end in ends):
        return segments
    kept = []
    for start, end in segments:
        mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        on_bus = False
        for bus in buses:
            ba = (float(bus["a"][0]), float(bus["a"][1]))
            bb = (float(bus["b"][0]), float(bus["b"][1]))
            reach = max(0.6, 0.02 * _dist(ba, bb))
            if _point_seg(mid, ba, bb) <= reach:
                on_bus = True
                break
        if not on_bus:
            kept.append((start, end))
    return kept or segments


def _sit_mm(label: dict[str, Any]) -> float:
    """How far a number drawn on the stroke may sit from that conductor.

    Sixteen millimetres reaches the next rung, and that rung is a different wire.
    """

    height = float(label.get("h") or 2.5)
    return max(6.5, height * 1.6)


def _beside_mm(label: dict[str, Any]) -> float:
    """How far a number nudged off a short stub may sit.

    Crowded motor leads print the number beside the phase, still closer to
    that phase than to the next one. Fourteen millimetres does not reach the
    next rung.
    """

    return max(_sit_mm(label), 14.0)


def _chains_under_label(
    label: dict[str, Any],
    chains: list[dict[str, Any]],
    buses: list[dict[str, Any]],
    limit: float | None = None,
) -> list[tuple[float, int]]:
    point = (float(label["x"]), float(label["y"]))
    sit = _beside_mm(label) if limit is None else limit
    found: list[tuple[float, int]] = []
    for index, chain in enumerate(chains):
        segments = _label_segments(chain, buses)
        if not segments:
            continue
        dist = _point_chain(point[0], point[1], segments)
        if dist <= sit and _label_sits_on(point, segments, sit):
            found.append((dist, index))
    found.sort()
    return found


def _label_chain_index(
    label: dict[str, Any],
    chains: list[dict[str, Any]],
    buses: list[dict[str, Any]],
) -> int | None:
    """The conductor this number sits on, or none when two runs are as close.

    Parallel phases share a corridor. The number belongs to the run whose end
    it sits beside. A number nudged off one stub stays on that stub when the
    next run is clearly farther. Two runs at the same distance stay unconnected.
    """

    found = _chains_under_label(label, chains, buses, limit=_beside_mm(label))
    # A run that starts and ends on one device is not a wire. It must not
    # take the number, or hide the conductor that leaves that device.
    found = [item for item in found if not _same_device_chain(chains[item[1]])]
    if not found:
        return None
    nearest_d = found[0][0]
    point = (float(label["x"]), float(label["y"]))

    def beside(item: tuple[float, int]) -> tuple[float, float]:
        segments = _label_segments(chains[item[1]], buses)
        return (_nearest_segment_end(point, segments), item[0])

    # On the stroke, the run that ends beside the number wins a close call.
    if nearest_d <= _sit_mm(label):
        close = [item for item in found if item[0] <= nearest_d + 0.5]
        return min(close, key=beside)[1]
    # Off the stroke, a long rail can be as near as the short phase the number
    # belongs to. The phase end is beside the number. The rail end is not.
    near = [item for item in found if item[0] < nearest_d + 1.5]
    ranked = sorted(near, key=beside)
    if len(ranked) == 1:
        return ranked[0][1]
    best_end = beside(ranked[0])[0]
    second_end = beside(ranked[1])[0]
    if best_end + 4.0 < second_end:
        return ranked[0][1]
    return None


def _pin_digit(
    label: dict[str, Any],
    pins: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    catalog: Any,
) -> bool:
    """A single digit drawn at a terminal is the pin mark, not a wire number."""

    import re

    text = str(label.get("text") or label.get("number") or "").strip()
    if not re.fullmatch(r"\d", text):
        return False
    if _inside_symbol(label, devices, catalog):
        return True
    if not pins:
        return False
    x, y = float(label["x"]), float(label["y"])
    nearest = min(_dist((x, y), (float(pin["x"]), float(pin["y"]))) for pin in pins)
    return nearest <= max(8.0, float(label.get("h") or 2.5) * 2.2)


def _label_sits_on(
    point: tuple[float, float],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    limit: float = LABEL_MM,
) -> bool:
    """The label's nearest point has to lie on the segment, not only at a far joint."""

    best = limit + 1.0
    for start, end in segments:
        dist = _point_seg(point, start, end)
        if dist < best:
            best = dist
    return best <= limit


def _line_reference_keys(
    tokens: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    width_mm: float,
    height_mm: float,
) -> set[tuple[float, float, str]]:
    """A real line-reference column, not every evenly spaced run of numbers.

    The column sits left of the hot rail, right of the return rail, or in the
    sheet margin. Its values are distinct and increase down the page, one per
    rung. The same number repeated on rung stubs is a wire label.
    """

    from panelcad.importer.read import in_sheet_border, is_nema_line_ref

    numbers = []
    for token in tokens:
        text = str(token.get("text") or "").strip()
        if is_nema_line_ref(text) or re.fullmatch(r"\d{1,4}", text):
            numbers.append(token)
    if len(numbers) < 3:
        return set()
    heights = [float(token.get("h") or 0) for token in numbers if float(token.get("h") or 0) > 0]
    glyph = sorted(heights)[len(heights) // 2] if heights else 2.5
    tol = max(glyph, 0.5) * 1.5
    clusters: list[list[dict[str, Any]]] = []
    for token in sorted(numbers, key=lambda item: float(item["x"])):
        x = float(token["x"])
        placed = False
        for group in clusters:
            center = sum(float(item["x"]) for item in group) / len(group)
            if abs(x - center) <= tol:
                group.append(token)
                placed = True
                break
        if not placed:
            clusters.append([token])
    marked: set[tuple[float, float, str]] = set()
    for group in clusters:
        if len(group) < 3:
            continue
        ordered = sorted(group, key=lambda item: float(item["y"]))
        values = [_line_ref_value(str(item.get("text") or "")) for item in ordered]
        if any(value is None for value in values):
            continue
        ys = [float(item["y"]) for item in ordered]
        column = sum(float(item["x"]) for item in group) / len(group)
        span = max(ys[-1] - ys[0], 1.0)
        hot_x, return_x = _rail_xs(lines, span)
        left_of_hot = hot_x is not None and column < hot_x - 0.4
        right_of_return = (
            hot_x is not None and return_x is not None and return_x > hot_x + 1.0 and column > return_x + 0.4
        )
        outside = left_of_hot or right_of_return
        if not outside:
            if len(set(values)) != len(values):
                continue
            if any(values[index] >= values[index + 1] for index in range(len(values) - 1)):
                continue
            gaps = [ys[index + 1] - ys[index] for index in range(len(ys) - 1)]
            if not gaps or min(gaps) <= 0 or not _rung_gaps(gaps):
                continue
            in_margin = any(
                in_sheet_border(float(item["x"]), float(item["y"]), width_mm, height_mm)
                for item in group
            ) if width_mm and height_mm else False
            # A short run beside the rail is the wire number. A long column
            # down the page is a line reference even when it sits just inside.
            if not in_margin and len(group) < 6:
                continue
        for item in group:
            marked.add(_label_key(item))
    return marked


def _rail_column_number(label: dict[str, Any], lines: list[dict[str, Any]]) -> bool:
    """A number to the right of the return rail is a cross-reference, not a wire.

    Coil cross-references sit there, including a single number. A wire number
    on a rung is between the rails.
    """

    import re

    text = str(label.get("text") or label.get("number") or "").strip()
    if not re.fullmatch(r"\d{1,4}", text):
        return False
    try:
        x = float(label["x"])
    except (KeyError, TypeError, ValueError):
        return False
    hot_x, return_x = _rail_xs(lines, 40.0)
    if hot_x is None:
        return False
    if x < hot_x - 0.4:
        return True
    return return_x is not None and return_x > hot_x + 1.0 and x > return_x + 0.4


def _rung_gaps(gaps: list[float]) -> bool:
    """True when the gaps are one rung, or a whole number of rungs (a skipped line)."""

    base = sorted(gaps)[len(gaps) // 2]
    if base <= 0:
        return False
    for gap in gaps:
        steps = round(gap / base)
        if steps < 1 or steps > 3:
            return False
        if abs(gap / base - steps) > 0.35:
            return False
    return True


def _line_ref_value(text: str) -> int | None:
    token = str(text or "").strip()
    if token.startswith("(") and token.endswith(")"):
        token = token[1:-1]
    if re.fullmatch(r"\d{1,4}", token):
        return int(token)
    return None


def _rail_xs(lines: list[dict[str, Any]], column_span: float) -> tuple[float | None, float | None]:
    """Hot rail is the leftmost long vertical. The return is the rightmost."""

    found: list[float] = []
    for line in lines:
        ax, ay = float(line["a"][0]), float(line["a"][1])
        bx, by = float(line["b"][0]), float(line["b"][1])
        if abs(ax - bx) > max(1.0, abs(ay - by) * 0.2):
            continue
        length = abs(ay - by)
        if length + 1.0 < column_span:
            continue
        found.append((ax + bx) / 2.0)
    if not found:
        return None, None
    return min(found), max(found)


def _ambiguous_wire(label: dict[str, Any]) -> dict[str, Any]:
    wire = _hand_wire(label)
    wire["ambiguous"] = True
    return wire


def _wire_question(label: dict[str, Any]) -> bool:
    """A read number that was not connected. Rail names and blank rows are not questions."""

    from panelcad.importer.models import CONVENTIONAL_WIRES
    from panelcad.importer.read import is_cross_ref

    text = str(label.get("text") or label.get("number") or "").strip()
    if not text or is_cross_ref(text):
        return False
    return text.upper() not in CONVENTIONAL_WIRES


def _hand_wire(label: dict[str, Any]) -> dict[str, Any]:
    """A wire number that was read and not connected. Kept for a Problems row."""

    return {
        "number": label.get("text") or label.get("number") or "",
        "x": round(float(label["x"]), 2),
        "y": round(float(label["y"]), 2),
        "from_tag": "",
        "to_tag": "",
        "from_pin": "",
        "to_pin": "",
        "confidence": 0.4,
        "ambiguous": True,
        "connect_by_hand": True,
        "unreadable": False,
    }


def _buried_label(
    label: dict[str, Any],
    devices: list[dict[str, Any]],
    catalog: Any,
    conductors: list[dict[str, Any]],
) -> bool:
    """Symbol text that happens to read like a wire number is not a conductor label."""

    if not _inside_symbol(label, devices, catalog):
        return False
    if not conductors:
        return True
    return _point_chain(float(label["x"]), float(label["y"]), [(tuple(s["a"]), tuple(s["b"])) for s in conductors]) > 4.5


def _inside_symbol(label: dict[str, Any], devices: list[dict[str, Any]], catalog: Any) -> bool:
    x, y = float(label["x"]), float(label["y"])
    for device in devices:
        lib = str(device.get("library_id") or "")
        if not lib or device.get("guess"):
            continue
        try:
            symbol = catalog.get(lib)
        except KeyError:
            continue
        width, height = _frame_size(device, symbol)
        ox, oy = float(device["x"]), float(device["y"])
        if ox + 1.2 <= x <= ox + width - 1.2 and oy + 1.2 <= y <= oy + height - 1.2:
            return True
    return False


def _frame_size(device: dict[str, Any], symbol: Any) -> tuple[float, float]:
    frame = str(device.get("pin_frame") or "schematic")
    if frame == "io":
        from panelcad.render.io_ladder import io_card_size

        return io_card_size(symbol)
    if frame == "network":
        from panelcad.library.ports import effective_ports
        from panelcad.render.network import node_size

        return node_size(len(effective_ports(symbol)))
    scale = float(device.get("scale") or 1.0)
    return (float(symbol.schematic_width) * scale, float(symbol.schematic_height) * scale)


def _continue_numbers(
    chains: list[dict[str, Any]],
    assigned: dict[int, dict[str, Any]],
    wires: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """An unlabeled run that leaves a labeled terminal keeps that wire number.

    The pins are the traced ends. The number is copied only when exactly one
    labeled conductor uses the shared terminal.
    """

    by_pin: dict[tuple[str, str], set[str]] = {}
    for wire in wires:
        if wire.get("ambiguous"):
            continue
        number = str(wire.get("number") or "")
        if not number:
            continue
        for key in ((str(wire.get("from_tag") or ""), str(wire.get("from_pin") or "")), (str(wire.get("to_tag") or ""), str(wire.get("to_pin") or ""))):
            if key[0] and key[1]:
                by_pin.setdefault(key, set()).add(number)
    extra: list[dict[str, Any]] = []
    for index, chain in enumerate(chains):
        if index in assigned:
            continue
        ends = chain["ends"]
        if not ends[0] or not ends[1]:
            continue
        if ends[0]["tag"] == ends[1]["tag"] and ends[0]["pin"] == ends[1]["pin"]:
            continue
        numbers: set[str] = set()
        for end in ends:
            numbers |= by_pin.get((end["tag"], end["pin"]), set())
        if len(numbers) != 1:
            continue
        seg = chain["segments"][len(chain["segments"]) // 2]
        extra.append(
            {
                "number": next(iter(numbers)),
                "x": round((seg[0][0] + seg[1][0]) / 2.0, 2),
                "y": round((seg[0][1] + seg[1][1]) / 2.0, 2),
                "from_tag": ends[0]["tag"],
                "to_tag": ends[1]["tag"],
                "from_pin": ends[0]["pin"],
                "to_pin": ends[1]["pin"],
                "confidence": 0.82,
                "ambiguous": False,
                "unreadable": False,
            }
        )
    return extra


def _wire_from_ends(label: dict[str, Any], ends: list[dict[str, Any] | None]) -> dict[str, Any]:
    pair = [end for end in ends[:2] if end]
    # A conductor that starts and ends on the same device is not a wire.
    # An internal jumper is a library fact, decided later, not a traced run.
    same_device = len(pair) == 2 and pair[0]["tag"] == pair[1]["tag"] and not pair[0].get("rail") and not pair[1].get("rail")
    ambiguous = len(pair) != 2 or same_device
    provisional = (not ambiguous) and any(isinstance(end, dict) and end.get("provisional") for end in pair)
    number = str(label.get("text") or label.get("number") or "")
    return {
        "number": number,
        "x": round(float(label["x"]), 2),
        "y": round(float(label["y"]), 2),
        "from_tag": "" if ambiguous else pair[0]["tag"],
        "to_tag": "" if ambiguous else pair[1]["tag"],
        "from_pin": "" if ambiguous else pair[0]["pin"],
        "to_pin": "" if ambiguous else pair[1]["pin"],
        "from_x": None if ambiguous else pair[0].get("x"),
        "from_y": None if ambiguous else pair[0].get("y"),
        "to_x": None if ambiguous else pair[1].get("x"),
        "to_y": None if ambiguous else pair[1].get("y"),
        "confidence": 0.55 if provisional else (0.4 if ambiguous else 0.9),
        "ambiguous": ambiguous,
        "provisional": provisional,
        "connect_by_hand": bool(ambiguous and number),
        "unreadable": False,
    }


def _pin_points(devices: list[dict[str, Any]], catalog: Any) -> list[dict[str, Any]]:
    points = []
    for device in devices:
        lib = str(device.get("library_id") or "")
        if not lib or device.get("guess"):
            continue
        try:
            symbol = catalog.get(lib)
        except KeyError:
            continue
        ox, oy = float(device["x"]), float(device["y"])
        text = _tag_text(device)
        frame = str(device.get("pin_frame") or "schematic")
        if frame == "io":
            from panelcad.render.io_ladder import io_pin_layout

            for pin_id, (lx, ly) in io_pin_layout(symbol).items():
                points.append(
                    {"tag": device["tag"], "pin": pin_id, "x": ox + float(lx), "y": oy + float(ly), **text}
                )
            continue
        if frame == "network":
            from panelcad.library.ports import effective_ports
            from panelcad.render.network import node_size, port_layout

            ports = effective_ports(symbol)
            width, height = node_size(len(ports))
            for pin_id, (lx, ly) in port_layout(ports, width, height).items():
                points.append(
                    {"tag": device["tag"], "pin": pin_id, "x": ox + float(lx), "y": oy + float(ly), **text}
                )
            continue
        scale = float(device.get("scale") or 1.0)
        overfed = {str(side) for side in (device.get("pole_gap_sides") or [])}
        for pin in symbol.pins:
            point = {
                "tag": device["tag"],
                "pin": pin.id,
                "x": ox + float(pin.x) * scale,
                "y": oy + float(pin.y) * scale,
                "side": str(pin.side),
                **text,
            }
            if str(pin.side) in overfed:
                point["overfed"] = True
            points.append(point)
    return points


def _land(
    point: tuple[float, float],
    pins: list[dict[str, Any]],
    buses: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    clusters: list[dict[str, Any]] | None = None,
    prev: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    """A conductor end is a device terminal, a rail, or an unmatched symbol."""

    pin = _snap(point, pins)
    # A wide symbol's terminal can sit on the next device. The end belongs to
    # the symbol whose ink it meets, not to the pin that happens to reach it.
    stolen = pin is not None and _end_meets_other_ink(point, pin, clusters or [])
    if stolen:
        pin = None
    if pin is not None:
        return pin
    bus = _bus_at(point, buses)
    if bus is not None:
        name = _bus_label(bus, tokens)
        if name:
            return {"tag": name, "pin": name, "rail": True, "x": point[0], "y": point[1]}
    # A real terminal nearby keeps the existing snap. Provisional pins are only
    # for a run that ends on an unmatched symbol and not on a library pin.
    # A pin that only reaches into another symbol is not that terminal, even
    # when it is too far to snap and only close enough to block the landing.
    if pins and not stolen:
        nearest_pin = min(pins, key=lambda item: _dist(point, (float(item["x"]), float(item["y"]))))
        nearest = _dist(point, (float(nearest_pin["x"]), float(nearest_pin["y"])))
        if nearest <= PIN_SNAP_MM * 3 and not _end_meets_other_ink(point, nearest_pin, clusters or []):
            return None
    return _provisional_land(point, clusters or [], prev)


def _provisional_land(
    point: tuple[float, float],
    clusters: list[dict[str, Any]],
    prev: tuple[float, float] | None = None,
) -> dict[str, Any] | None:
    """Land on an unmatched symbol. The pin is the side the conductor arrives on."""

    near: list[tuple[float, float, dict[str, Any]]] = []
    for cluster in clusters:
        box = cluster.get("box")
        if not box:
            continue
        dist = _dist_to_box(point, tuple(box))
        if dist > 4.5:
            continue
        if "x" in cluster and "y" in cluster:
            tag_d = _dist(point, (float(cluster["x"]), float(cluster["y"])))
        else:
            tag_d = dist
        # The end has to reach this symbol. A tag on the next rung is not it,
        # unless that tag's own ink box also reaches the end.
        if tag_d > 28.0:
            continue
        near.append((tag_d, dist, cluster))
    if not near:
        return None
    near.sort(key=lambda item: (item[0], item[1]))
    # Two symbols are close enough that the end does not say which one.
    if len(near) > 1 and near[1][0] < near[0][0] + 3.0:
        return None
    best = (near[0][1], near[0][2])
    box = tuple(best[1]["box"])
    side = _approach_side(point, prev, box)
    return {
        "tag": best[1]["tag"],
        "pin": side,
        "provisional": True,
        "x": point[0],
        "y": point[1],
    }


def _approach_side(
    point: tuple[float, float],
    prev: tuple[float, float] | None,
    box: tuple[float, float, float, float],
) -> str:
    """The side a conductor arrives on. Travel to the right lands on the left side."""

    if prev is not None:
        dx, dy = point[0] - prev[0], point[1] - prev[1]
        if abs(dx) >= abs(dy) and abs(dx) > 0.4:
            return "left" if dx > 0 else "right"
        if abs(dy) > 0.4:
            return "top" if dy > 0 else "bottom"
    return _box_side(point, box)


def _box_side(point: tuple[float, float], box: tuple[float, float, float, float]) -> str:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    dx, dy = point[0] - cx, point[1] - cy
    if abs(dx) >= abs(dy):
        return "left" if dx <= 0 else "right"
    return "top" if dy <= 0 else "bottom"


def _bus_at(point: tuple[float, float], buses: list[dict[str, Any]]) -> dict[str, Any] | None:
    best: tuple[float, dict[str, Any]] | None = None
    for bus in buses:
        dist = _point_seg(point, (float(bus["a"][0]), float(bus["a"][1])), (float(bus["b"][0]), float(bus["b"][1])))
        if dist > PIN_SNAP_MM:
            continue
        if best is None or dist < best[0]:
            best = (dist, bus)
    if best is None:
        return None
    return best[1]


def _bus_label(bus: dict[str, Any], tokens: list[dict[str, Any]]) -> str:
    """The short name drawn at the end of a rail (L1, L2, L+, 0V)."""

    from panelcad.importer.models import CONVENTIONAL_WIRES
    from panelcad.importer.read import is_device_tag

    ends = (
        (float(bus["a"][0]), float(bus["a"][1])),
        (float(bus["b"][0]), float(bus["b"][1])),
    )
    best: tuple[float, str] | None = None
    for tok in tokens:
        text = str(tok.get("text") or "").strip().upper()
        if not text or len(text) > 6 or text.isdigit():
            continue
        if text not in CONVENTIONAL_WIRES and not re.fullmatch(r"[A-Z]{1,3}\+?", text):
            continue
        if is_device_tag(text):
            continue
        try:
            x = float(tok["x"])
            y = float(tok["y"])
        except (KeyError, TypeError, ValueError):
            continue
        dist = min(_dist((x, y), end) for end in ends)
        if dist > 30.0:
            continue
        if best is None or dist < best[0]:
            best = (dist, text)
    if best is None:
        return ""
    return best[1]


def _tag_text(device: dict[str, Any]) -> dict[str, float]:
    """Where the tag was printed, and the symbol origin it was matched to."""

    ox, oy = float(device.get("x") or 0), float(device.get("y") or 0)
    row = {"ox": ox, "oy": oy}
    if device.get("text_x") is None or device.get("text_y") is None:
        return row
    row["text_x"] = float(device["text_x"])
    row["text_y"] = float(device["text_y"])
    return row


def _end_meets_other_ink(
    point: tuple[float, float],
    pin: dict[str, Any],
    clusters: list[dict[str, Any]],
) -> bool:
    """True when this end lies in another symbol and that symbol's tag is nearer.

    The contactor coil drawn on a seal-in contact puts a pin on the next
    device's terminal. That pin does not own the conductor.
    """

    if pin.get("text_x") is None or pin.get("text_y") is None:
        return False
    owner = str(pin.get("tag") or "")
    own = _dist(point, (float(pin["text_x"]), float(pin["text_y"])))
    nearest: float | None = None
    for cluster in clusters:
        if str(cluster.get("tag") or "") == owner or "box" not in cluster:
            continue
        if _dist_to_box(point, tuple(cluster["box"])) > 0.4:
            continue
        tag_d = _dist(point, (float(cluster.get("x") or 0), float(cluster.get("y") or 0)))
        if nearest is None or tag_d < nearest:
            nearest = tag_d
    if nearest is None:
        return False
    return nearest + 6.0 < own


def _snap(point: tuple[float, float], pins: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pins:
        return None

    def rank(pin: dict[str, Any]) -> tuple[float, float]:
        # Two symbols on one spot share every terminal. The tag printed on
        # that symbol owns it. The other tag belongs to a symbol that was not drawn here.
        here = _dist(point, (pin["x"], pin["y"]))
        if pin.get("text_x") is None:
            return (round(here, 2), here)
        printed = _dist((float(pin["ox"]), float(pin["oy"])), (float(pin["text_x"]), float(pin["text_y"])))
        return (round(here, 2), printed)

    ranked = sorted(pins, key=rank)
    best = ranked[0]
    best_d = _dist(point, (best["x"], best["y"]))
    if best_d > PIN_SNAP_MM:
        return None
    # Sitting on the terminal is that terminal, even when the next pin is a
    # millimetre away on a scaled symbol. A leaf in the gap between two pins is not.
    if best_d <= PIN_BREAK_MM:
        return best
    if len(ranked) > 1:
        second = _dist(point, (ranked[1]["x"], ranked[1]["y"]))
        if second < best_d + 1.1:
            return None
    return best


def _traced_chains(
    strokes: list[dict[str, Any]],
    pins: list[dict[str, Any]],
    labels: list[dict[str, Any]] | None = None,
    page_tokens: list[dict[str, Any]] | None = None,
    clusters: list[dict[str, Any]] | None = None,
    split_tees: bool = False,
) -> list[dict[str, Any]]:
    """Each chain is one conductor between two stops.

    A stop is a free end, a corner where three runs meet, or a terminal where
    two wires arrive and go no further. A straight run is not cut just because
    it passes near a pin. Two wires that share the stub off one terminal are
    split at that junction and both keep the terminal.

    A longer wire can cover a few millimetres of another and erase that ink.
    The stub that remains is bridged across the gap when it continues in a
    straight line, and the two runs that actually turn the corner are joined.
    """

    adj = _wire_graph(strokes, split_tees=split_tees)
    raw = _walk_chains(adj, pins)
    _annotate_chain_numbers(raw, labels or [])
    bridged = _bridge_clipped_gaps(raw, pins)
    joined = _join_open_corners(bridged, adj)
    buses = [stroke for stroke in strokes if stroke.get("bus")]
    return _resolve_junctions(
        joined,
        adj,
        pins,
        buses,
        page_tokens or [],
        clusters or [],
        link_bus_runs=split_tees,
    )


def _split_tees(strokes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split a conductor where another conductor ends on its span.

    A rung that butts into a rail does not share a vertex. The graph needs that
    junction or the rung never meets the rail.
    """

    wires = [stroke for stroke in strokes if stroke.get("kind", "line") == "line" and "a" in stroke]
    grid = _endpoint_grid(wires)
    split: list[dict[str, Any]] = []
    for stroke in strokes:
        if stroke.get("kind", "line") != "line" or "a" not in stroke:
            split.append(stroke)
            continue
        start, end = _line_ends(stroke)
        cuts = [start, end]
        for point in grid.near(start[0], start[1], end[0], end[1], pad=0.8):
            if _dist(point, start) <= 0.6 or _dist(point, end) <= 0.6:
                continue
            if _point_seg(point, start, end) <= 0.8:
                cuts.append(point)
        ordered = _sorted_along(start, end, cuts)
        for left, right in zip(ordered, ordered[1:], strict=False):
            if _dist(left, right) < 0.3:
                continue
            item = {
                "kind": "line",
                "role": stroke.get("role") or "wire",
                "a": [left[0], left[1]],
                "b": [right[0], right[1]],
            }
            if stroke.get("bus"):
                item["bus"] = True
            if stroke.get("geom"):
                item["geom"] = True
            split.append(item)
    return split


def _sorted_along(
    start: tuple[float, float],
    end: tuple[float, float],
    points: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = dx * dx + dy * dy

    def key(point: tuple[float, float]) -> float:
        if length <= 1e-9:
            return 0.0
        return ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length

    ordered = sorted(points, key=key)
    unique: list[tuple[float, float]] = []
    for point in ordered:
        if unique and _dist(unique[-1], point) <= 0.3:
            continue
        unique.append(point)
    return unique


def _wire_graph(strokes: list[dict[str, Any]], split_tees: bool = False) -> dict[tuple[float, float], list[tuple[float, float]]]:
    adj: dict[tuple[float, float], list[tuple[float, float]]] = {}
    nodes: list[tuple[float, float]] = []

    def snap(point: Any) -> tuple[float, float]:
        # A hop is drawn as an arc whose joints miss by a few tenths of a millimetre.
        # Snapping those joints reconnects the conductor. Parallel phases stay apart.
        key = _node_key(point)
        for existing in nodes:
            if _dist(existing, key) <= 0.45:
                return existing
        nodes.append(key)
        return key

    if split_tees:
        strokes = _split_tees(strokes)
    for stroke in strokes:
        a = snap(stroke["a"])
        b = snap(stroke["b"])
        if a == b or _dist(a, b) < 0.15:
            continue
        nbrs = adj.setdefault(a, [])
        if b not in nbrs:
            nbrs.append(b)
            adj.setdefault(b, []).append(a)
    return adj


def _node_key(point: Any) -> tuple[float, float]:
    return (round(float(point[0]), 1), round(float(point[1]), 1))


def _is_stop(node: tuple[float, float], adj: dict[tuple[float, float], list[tuple[float, float]]], pins: list[dict[str, Any]]) -> bool:
    degree = len(adj.get(node, []))
    if degree != 2:
        return True
    return _on_pin(node, pins)


def _is_junction(node: tuple[float, float], adj: dict[tuple[float, float], list[tuple[float, float]]]) -> bool:
    return len(adj.get(node, [])) >= 3


def _walk_chains(
    adj: dict[tuple[float, float], list[tuple[float, float]]],
    pins: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    used: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    chains = []
    for start, nbrs in adj.items():
        for nxt in nbrs:
            key = _edge_key(start, nxt)
            if key in used:
                continue
            nodes = _chain_nodes(adj, pins, start, nxt, used)
            if len(nodes) < 2:
                continue
            chains.append({"nodes": nodes, "segments": _segments(nodes)})
    return chains


def _chain_nodes(
    adj: dict[tuple[float, float], list[tuple[float, float]]],
    pins: list[dict[str, Any]],
    a: tuple[float, float],
    b: tuple[float, float],
    used: set[tuple[tuple[float, float], tuple[float, float]]],
) -> list[tuple[float, float]]:
    def mark(p: tuple[float, float], q: tuple[float, float]) -> None:
        used.add(_edge_key(p, q))

    def side(start: tuple[float, float], blocked: tuple[float, float]) -> list[tuple[float, float]]:
        mark(start, blocked)
        if _is_stop(start, adj, pins):
            return [start]
        path = [start]
        seen = {blocked, start}
        prev, cur = blocked, start
        while True:
            nxts = [n for n in adj[cur] if n != prev and n not in seen]
            if len(nxts) != 1:
                break
            nxt = nxts[0]
            mark(cur, nxt)
            path.append(nxt)
            seen.add(nxt)
            if _is_stop(nxt, adj, pins):
                break
            prev, cur = cur, nxt
        return path

    left = side(a, b)
    right = side(b, a)
    return list(reversed(left)) + right


def _edge_key(a: tuple[float, float], b: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
    return (a, b) if a <= b else (b, a)


def _segments(nodes: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [(nodes[index], nodes[index + 1]) for index in range(len(nodes) - 1)]


def _nearest_segment_end(
    point: tuple[float, float],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> float:
    """Distance from a label to an end of the segment it actually sits on."""

    best_body: float | None = None
    best_end = 1e9
    for start, end in segments:
        body = _point_seg(point, start, end)
        end_dist = min(_dist(point, start), _dist(point, end))
        if best_body is None or body < best_body - 1e-6:
            best_body = body
            best_end = end_dist
        elif abs(body - best_body) <= 1e-6:
            best_end = min(best_end, end_dist)
    return best_end


def _annotate_chain_numbers(chains: list[dict[str, Any]], labels: list[dict[str, Any]]) -> None:
    """Give each raw run the wire number that sits on it, one number each."""

    pairs: list[tuple[float, int, str, int]] = []
    for label in labels:
        number = str(label.get("number") or label.get("text") or "").strip()
        if not number:
            continue
        for index, chain in enumerate(chains):
            segments = chain.get("segments") or []
            if not segments:
                continue
            dist = _point_chain(float(label["x"]), float(label["y"]), segments)
            if dist <= LABEL_MM:
                pairs.append((dist, index, number, id(label)))
    pairs.sort()
    used_labels: set[int] = set()
    used_chains: set[int] = set()
    for _dist, index, number, marker in pairs:
        if marker in used_labels or index in used_chains:
            continue
        used_labels.add(marker)
        used_chains.add(index)
        chains[index]["number"] = number


def _numbers_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a = str(left.get("number") or "")
    b = str(right.get("number") or "")
    return bool(a and b and a != b)


def _chain_ends(chain: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    nodes = chain["nodes"]
    return nodes[0], nodes[-1]


def _orient_to(nodes: list[tuple[float, float]], end: tuple[float, float]) -> list[tuple[float, float]]:
    if nodes[0] == end:
        return list(nodes)
    if nodes[-1] == end:
        return list(reversed(nodes))
    return list(nodes)


def _merged_chain(left: dict[str, Any], right: dict[str, Any], nodes: list[tuple[float, float]]) -> dict[str, Any]:
    number = str(left.get("number") or right.get("number") or "")
    merged: dict[str, Any] = {"nodes": nodes, "segments": _segments(nodes)}
    if number:
        merged["number"] = number
    return merged


def _bridge_clipped_gaps(chains: list[dict[str, Any]], pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join a run to the wire that continues it across a gap of a few millimetres.

    Overlap drops the short piece of a wire that lies on a longer run, so the
    stub ends on the other net and a gap is left in front of the real continuation.
    """

    pending = list(chains)
    changed = True
    while changed:
        changed = False
        for index, chain in enumerate(pending):
            nodes = chain["nodes"]
            if len(nodes) < 2:
                continue
            for at_end in (False, True):
                end = nodes[-1] if at_end else nodes[0]
                prev = nodes[-2] if at_end else nodes[1]
                if _on_pin(end, pins):
                    continue
                vx, vy = end[0] - prev[0], end[1] - prev[1]
                vlen = (vx * vx + vy * vy) ** 0.5
                if vlen < 1e-6:
                    continue
                best: tuple[float, int, tuple[float, float]] | None = None
                for other_index, other in enumerate(pending):
                    if other is chain or _shares_endpoint(chain, other) or _numbers_conflict(chain, other):
                        continue
                    for other_end in _chain_ends(other):
                        wx, wy = other_end[0] - end[0], other_end[1] - end[1]
                        gap = (wx * wx + wy * wy) ** 0.5
                        if gap < 0.5 or gap > 6.0:
                            continue
                        dot = (vx * wx + vy * wy) / (vlen * gap)
                        cross = abs(vx * wy - vy * wx) / vlen
                        if dot < 0.85 or cross > 0.6:
                            continue
                        if best is None or gap < best[0]:
                            best = (gap, other_index, other_end)
                if best is None:
                    continue
                _gap, other_index, other_end = best
                other = pending[other_index]
                oriented = list(nodes) if at_end else list(reversed(nodes))
                rest = _orient_to(other["nodes"], other_end)
                merged = _merged_chain(chain, other, oriented + rest)
                pending = [item for item_index, item in enumerate(pending) if item_index not in {index, other_index}]
                pending.append(merged)
                changed = True
                break
            if changed:
                break
    return pending


def _shares_endpoint(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(set(_chain_ends(left)) & set(_chain_ends(right)))


def _join_open_corners(
    chains: list[dict[str, Any]],
    adj: dict[tuple[float, float], list[tuple[float, float]]],
) -> list[dict[str, Any]]:
    """Join the two runs left at a tee once the straight stub has been bridged away."""

    pending = list(chains)
    changed = True
    while changed:
        changed = False
        grouped: dict[tuple[float, float], list[dict[str, Any]]] = {}
        for chain in pending:
            nodes = chain["nodes"]
            if len(nodes) < 2:
                continue
            seen: set[tuple[float, float]] = set()
            for end in _chain_ends(chain):
                if end in seen or not _is_junction(end, adj):
                    continue
                seen.add(end)
                grouped.setdefault(end, []).append(chain)
        for junction, group in grouped.items():
            if len(adj.get(junction, [])) != 3 or len(group) != 2:
                continue
            left, right = group
            if _numbers_conflict(left, right):
                continue
            if left["nodes"][0] == junction and left["nodes"][-1] == junction:
                continue
            if right["nodes"][0] == junction and right["nodes"][-1] == junction:
                continue
            first = list(reversed(_orient_to(left["nodes"], junction)))
            second = _orient_to(right["nodes"], junction)
            if first[-1] != junction or second[0] != junction:
                continue
            merged = _merged_chain(left, right, first + second[1:])
            if merged["nodes"][0] == junction or merged["nodes"][-1] == junction:
                continue
            pending = [item for item in pending if item is not left and item is not right]
            pending.append(merged)
            changed = True
            break
    return pending


def _on_bus(point: tuple[float, float], buses: list[dict[str, Any]]) -> bool:
    for bus in buses:
        if "a" not in bus:
            continue
        if _point_seg(point, (float(bus["a"][0]), float(bus["a"][1])), (float(bus["b"][0]), float(bus["b"][1]))) <= 0.8:
            return True
    return False


def _resolve_junctions(
    chains: list[dict[str, Any]],
    adj: dict[tuple[float, float], list[tuple[float, float]]],
    pins: list[dict[str, Any]],
    buses: list[dict[str, Any]] | None = None,
    page_tokens: list[dict[str, Any]] | None = None,
    clusters: list[dict[str, Any]] | None = None,
    link_bus_runs: bool = False,
) -> list[dict[str, Any]]:
    rails = buses or []
    tokens = page_tokens or []
    groups = clusters or []
    simple: list[dict[str, Any]] = []
    spokes: dict[tuple[float, float], list[dict[str, Any]]] = {}
    for chain in chains:
        nodes = chain["nodes"]
        a, b = nodes[0], nodes[-1]
        a_prev = nodes[1] if len(nodes) > 1 else None
        b_prev = nodes[-2] if len(nodes) > 1 else None
        a_join, b_join = _is_junction(a, adj), _is_junction(b, adj)
        if a_join and b_join and link_bus_runs and _on_bus(a, rails) and _on_bus(b, rails):
            # A run between two tees. Each tee has to see it, or a rung on that
            # tee cannot take the rail the two tees belong to.
            spokes.setdefault(a, []).append({"nodes": list(nodes)})
            spokes.setdefault(b, []).append({"nodes": list(reversed(nodes))})
            continue
        if a_join ^ b_join:
            junction = a if a_join else b
            if b_join:
                nodes = list(reversed(nodes))
            spokes.setdefault(junction, []).append({"nodes": nodes})
            continue
        simple.append(
            {
                "segments": chain["segments"],
                "ends": [
                    _land(a, pins, rails, tokens, groups, a_prev),
                    _land(b, pins, rails, tokens, groups, b_prev),
                ],
            }
        )
    resolved = list(simple)
    for junction, items in spokes.items():
        resolved.extend(_pair_spokes(junction, items, pins, rails, tokens, groups, prefer_rail=link_bus_runs))
    _attach_drops(resolved, _pin_boxes(pins))
    _settle_shared_pins(resolved, pins)
    return [item for item in resolved if item["segments"]]


def _pin_boxes(pins: list[dict[str, Any]]) -> list[tuple[str, float, float, float, float]]:
    """A device box grown from its terminals, by a fraction of that terminal span."""

    grouped: dict[str, list[tuple[float, float]]] = {}
    for pin in pins:
        grouped.setdefault(str(pin["tag"]), []).append((float(pin["x"]), float(pin["y"])))
    boxes = []
    for tag, points in grouped.items():
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        pad = span * 0.2
        boxes.append((tag, min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad))
    return boxes


# A nearer pin has to be this many times closer than the next one. A fixed
# millimetre gap calls a converging phase "clear" when it is not.
_PIN_CLARITY = 1.8


def _settle_shared_pins(chains: list[dict[str, Any]], pins: list[dict[str, Any]]) -> None:
    """One conductor per pin, in the order the pins are drawn.

    Sibling wires that arrive on the same device are paired left to right
    (or top to bottom). A pin that is not clearly the nearest, or that another
    wire already owns, stays unconnected.
    """

    landings: list[dict[str, Any]] = []
    for chain in chains:
        segments = chain.get("segments") or []
        ends = chain.get("ends") or []
        if len(segments) < 1 or len(ends) < 2:
            continue
        points = (segments[0][0], segments[-1][1])
        for index in (0, 1):
            end = ends[index]
            if not isinstance(end, dict) or end.get("rail"):
                continue
            if not end.get("tag") or not end.get("pin"):
                continue
            landings.append({"chain": chain, "index": index, "point": points[index], "tag": str(end["tag"])})
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for item in landings:
        by_tag.setdefault(item["tag"], []).append(item)
    pins_by_tag: dict[str, list[dict[str, Any]]] = {}
    for pin in pins:
        pins_by_tag.setdefault(str(pin["tag"]), []).append(pin)
    for tag, group in by_tag.items():
        device_pins = pins_by_tag.get(tag) or []
        if len(device_pins) < 2:
            continue
        # A drop can name a pin it does not sit on. That landing was already
        # chosen along the rung. Only ends that actually reach this device
        # are settled here.
        spacing = min(
            _row_spacing(device_pins, lambda point: point[0]),
            _row_spacing(device_pins, lambda point: point[1]),
        )
        reach = max(PIN_SNAP_MM, spacing * 1.25)
        near = [item for item in group if _pins_near(item["point"], device_pins, reach)]
        near = _drop_overfed_side(near, device_pins, reach)
        if len(near) < 2:
            if len(near) == 1:
                _keep_clear_pin(near[0], device_pins)
            continue
        _assign_pin_row(near, device_pins)


def _drop_overfed_side(
    near: list[dict[str, Any]],
    device_pins: list[dict[str, Any]],
    reach: float,
) -> list[dict[str, Any]]:
    """More conductors than pins on a side is needs-info, not a partial snap."""

    rows = _pin_rows(device_pins)
    if not rows:
        return near
    grouped: list[list[dict[str, Any]]] = [[] for _row in rows]
    rest: list[dict[str, Any]] = []
    for item in near:
        point = item["point"]
        nearest = min(device_pins, key=lambda pin: _dist(point, (float(pin["x"]), float(pin["y"]))))
        if _dist(point, (float(nearest["x"]), float(nearest["y"]))) > reach:
            rest.append(item)
            continue
        for index, row in enumerate(rows):
            if any(pin is nearest for pin in row):
                grouped[index].append(item)
                break
        else:
            rest.append(item)
    kept = list(rest)
    for row, items in zip(rows, grouped, strict=True):
        # One terminal keeps a branch that actually lands on it. A snap that
        # names some other pin, from a point that is not on this terminal, is
        # not that wire.
        if len(row) <= 1 and not any(pin.get("overfed") for pin in row):
            if row:
                pin = row[0]
                px, py = float(pin["x"]), float(pin["y"])
                for item in items:
                    end = item["chain"]["ends"][item["index"]]
                    stored = "" if not isinstance(end, dict) else str(end.get("pin") or "")
                    if stored == str(pin.get("pin") or ""):
                        continue
                    if _dist(item["point"], (px, py)) > PIN_SNAP_MM:
                        _write_pin([item], None)
            continue
        if any(pin.get("overfed") for pin in row) or len(_landing_groups(items)) > len(row):
            for group in _landing_groups(items):
                _write_pin(group, None)
            continue
        kept.extend(items)
    return kept


def _pin_rows(pins: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if pins and all(pin.get("side") for pin in pins):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for pin in pins:
            grouped.setdefault(str(pin["side"]), []).append(pin)
        return list(grouped.values())
    unused = set(range(len(pins)))
    rows: list[list[dict[str, Any]]] = []
    while unused:
        index = next(iter(unused))
        pin = pins[index]
        same_y = [item for item in unused if abs(float(pins[item]["y"]) - float(pin["y"])) <= 1.0]
        same_x = [item for item in unused if abs(float(pins[item]["x"]) - float(pin["x"])) <= 1.0]
        chosen = same_y if len(same_y) >= len(same_x) else same_x
        for item in chosen:
            unused.discard(item)
        rows.append([pins[item] for item in chosen])
    return rows


def _keep_clear_pin(landing: dict[str, Any], device_pins: list[dict[str, Any]]) -> None:
    point = landing["point"]
    ranked = sorted(device_pins, key=lambda pin: _dist(point, (float(pin["x"]), float(pin["y"]))))
    best_d = _dist(point, (float(ranked[0]["x"]), float(ranked[0]["y"])))
    second = _dist(point, (float(ranked[1]["x"]), float(ranked[1]["y"]))) if len(ranked) > 1 else 1e9
    # Sitting on the terminal is that terminal. The ratio applies in the gap.
    if best_d <= PIN_BREAK_MM:
        landing["chain"]["ends"][landing["index"]] = _pin_copy(ranked[0])
        return
    if best_d > PIN_SNAP_MM or not _pin_is_clear(best_d, second):
        landing["chain"]["ends"][landing["index"]] = None
        return
    landing["chain"]["ends"][landing["index"]] = _pin_copy(ranked[0])


def _pin_is_clear(best_d: float, second_d: float) -> bool:
    if best_d <= 1e-6:
        return True
    return second_d > best_d * _PIN_CLARITY


def _assign_pin_row(group: list[dict[str, Any]], device_pins: list[dict[str, Any]]) -> None:
    spacing = _row_spacing(device_pins, lambda point: point[0])
    spacing = min(spacing, _row_spacing(device_pins, lambda point: point[1]))
    reach = max(PIN_SNAP_MM, spacing * 1.25)
    remaining = list(group)
    while remaining:
        seed = remaining[0]
        nearby = _pins_near(seed["point"], device_pins, reach)
        cluster = [seed]
        for item in remaining[1:]:
            item_pins = _pins_near(item["point"], device_pins, reach)
            if any(pin is other for pin in item_pins for other in nearby):
                cluster.append(item)
                for pin in item_pins:
                    if pin not in nearby:
                        nearby.append(pin)
        for item in cluster:
            remaining.remove(item)
        groups = _landing_groups(cluster)
        unique = [items[0] for items in groups]
        choices = [_nearest_choice(item, nearby or device_pins) for item in unique]
        claimed: dict[str, int] = {}
        conflict = False
        for index, (_item, pin, clear, _dist_mm) in enumerate(choices):
            key = "" if pin is None else str(pin.get("pin") or "")
            if not clear or not key or key in claimed:
                conflict = True
            else:
                claimed[key] = index
        if (
            conflict
            and len(unique) >= 2
            and len(unique) == len(nearby)
            and _spread(unique, 0) + _spread(unique, 1) > 0
            and _ordered_pins_in_reach(unique, nearby, reach)
        ):
            # The leads converge, so the nearest pin is the wrong phase. Keep
            # one wire per pin in the order the pins are drawn.
            _write_pin_order(groups, unique, nearby)
            continue
        if not conflict:
            continue
        taken: set[str] = set()
        for items, (_item, pin, clear, best_d) in zip(groups, choices, strict=True):
            key = "" if pin is None else str(pin.get("pin") or "")
            if clear and key and key not in taken and best_d <= PIN_SNAP_MM:
                taken.add(key)
                _write_pin(items, pin)
            else:
                _write_pin(items, None)


def _landing_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Overlapping copies of one stroke are one landing."""

    groups: list[list[dict[str, Any]]] = []
    for item in items:
        for group in groups:
            if _dist(item["point"], group[0]["point"]) <= 0.6:
                group.append(item)
                break
        else:
            groups.append([item])
    return groups


def _ordered_pins_in_reach(
    unique: list[dict[str, Any]],
    pins: list[dict[str, Any]],
    reach: float,
) -> bool:
    horizontal = _spread(unique, 0) >= _spread(unique, 1)
    ordered = sorted(unique, key=lambda item: item["point"][0 if horizontal else 1])
    side = sorted(pins, key=lambda pin: float(pin["x"] if horizontal else pin["y"]))
    return all(
        _dist(item["point"], (float(pin["x"]), float(pin["y"]))) <= reach
        for item, pin in zip(ordered, side, strict=True)
    )


def _write_pin_order(
    groups: list[list[dict[str, Any]]],
    unique: list[dict[str, Any]],
    pins: list[dict[str, Any]],
) -> None:
    horizontal = _spread(unique, 0) >= _spread(unique, 1)
    ordered = sorted(range(len(unique)), key=lambda index: unique[index]["point"][0 if horizontal else 1])
    side = sorted(pins, key=lambda pin: float(pin["x"] if horizontal else pin["y"]))
    by_rep = {ordered[index]: side[index] for index in range(len(ordered))}
    for index, items in enumerate(groups):
        _write_pin(items, by_rep[index])


def _write_pin(items: list[dict[str, Any]], pin: dict[str, Any] | None) -> None:
    copied = None if pin is None else _pin_copy(pin)
    for item in items:
        item["chain"]["ends"][item["index"]] = None if copied is None else dict(copied)


def _nearest_choice(
    item: dict[str, Any],
    device_pins: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None, bool, float]:
    if not device_pins:
        return item, None, False, 1e9
    ranked = sorted(device_pins, key=lambda pin: _dist(item["point"], (float(pin["x"]), float(pin["y"]))))
    best_d = _dist(item["point"], (float(ranked[0]["x"]), float(ranked[0]["y"])))
    second = _dist(item["point"], (float(ranked[1]["x"]), float(ranked[1]["y"]))) if len(ranked) > 1 else 1e9
    on_pin = best_d <= PIN_BREAK_MM
    clear = on_pin or (best_d <= PIN_SNAP_MM and _pin_is_clear(best_d, second))
    return item, ranked[0], clear, best_d


def _pins_near(point: tuple[float, float], device_pins: list[dict[str, Any]], reach: float) -> list[dict[str, Any]]:
    return [pin for pin in device_pins if _dist(point, (float(pin["x"]), float(pin["y"]))) <= reach]


def _spread(items: list[dict[str, Any]], axis: int) -> float:
    values = [float(item["point"][axis]) for item in items]
    return max(values) - min(values) if values else 0.0


def _row_spacing(pins: list[dict[str, Any]], along) -> float:
    values = sorted(along((float(pin["x"]), float(pin["y"]))) for pin in pins)
    gaps = [values[index + 1] - values[index] for index in range(len(values) - 1)]
    gaps = [gap for gap in gaps if gap > 0.5]
    if not gaps:
        return 8.0
    gaps.sort()
    return gaps[len(gaps) // 2]


def _pin_copy(pin: dict[str, Any]) -> dict[str, Any]:
    return {
        "tag": pin.get("tag"),
        "pin": pin.get("pin"),
        "x": pin.get("x"),
        "y": pin.get("y"),
        "rail": False,
    }


def _attach_drops(
    chains: list[dict[str, Any]],
    boxes: list[tuple[str, float, float, float, float]] | None = None,
) -> None:
    """A drop that ends on another conductor joins that conductor's net.

    The meeting point picks the nearer terminal. The far terminal is used only
    when it is a small fraction of the run away from a lead and the new segment
    does not cross another device. Distances are fractions of that run.
    """

    bodies = boxes or []
    for chain in chains:
        ends = chain.get("ends") or []
        segs = chain.get("segments") or []
        if len(ends) < 2 or not segs:
            continue
        if bool(ends[0]) == bool(ends[1]):
            continue
        open_index = 0 if ends[0] is None else 1
        point = segs[0][0] if open_index == 0 else segs[-1][1]
        pinned = ends[1] if open_index == 0 else ends[0]
        chosen: dict[str, Any] | None = None
        for other in chains:
            if other is chain:
                continue
            oends = other.get("ends") or []
            if len(oends) < 2 or not oends[0] or not oends[1]:
                continue
            if "x" not in oends[0] or "x" not in oends[1]:
                continue
            hit = False
            for start, end in other.get("segments") or []:
                gap = max(0.4, 0.02 * _dist(start, end))
                if _point_seg(point, start, end) <= gap and min(_dist(point, start), _dist(point, end)) > 0.4:
                    hit = True
                    break
            if not hit:
                continue
            left = (float(oends[0]["x"]), float(oends[0]["y"]))
            right = (float(oends[1]["x"]), float(oends[1]["y"]))
            span = _dist(left, right)
            da, db = _dist(point, left), _dist(point, right)
            near, far = (oends[0], oends[1]) if da <= db else (oends[1], oends[0])
            near_d, far_d = (da, db) if da <= db else (db, da)
            chosen = near
            if span > 0 and near_d <= span * 0.2 and far_d >= span * 0.55:
                origin = (float(pinned["x"]), float(pinned["y"])) if isinstance(pinned, dict) and "x" in pinned else point
                target = (float(far["x"]), float(far["y"]))
                skip = {str(near.get("tag") or ""), str(far.get("tag") or "")}
                if isinstance(pinned, dict):
                    skip.add(str(pinned.get("tag") or ""))
                if not _crosses_box(origin, target, bodies, skip):
                    chosen = far
            break
        if chosen is None:
            continue
        if pinned and pinned.get("tag") == chosen.get("tag") and pinned.get("pin") == chosen.get("pin"):
            continue
        ends[open_index] = chosen


def _crosses_box(
    start: tuple[float, float],
    end: tuple[float, float],
    boxes: list[tuple[str, float, float, float, float]],
    skip: set[str],
) -> bool:
    for tag, x0, y0, x1, y1 in boxes:
        if tag in skip:
            continue
        if _segment_hits_rect(start, end, x0, y0, x1, y1):
            return True
    return False


def _segment_hits_rect(
    start: tuple[float, float],
    end: tuple[float, float],
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> bool:
    steps = 8
    for step in range(1, steps):
        t = step / steps
        x = start[0] + (end[0] - start[0]) * t
        y = start[1] + (end[1] - start[1]) * t
        if x0 < x < x1 and y0 < y < y1:
            return True
    return False


def _pair_spokes(
    junction: tuple[float, float],
    items: list[dict[str, Any]],
    pins: list[dict[str, Any]],
    buses: list[dict[str, Any]] | None = None,
    page_tokens: list[dict[str, Any]] | None = None,
    clusters: list[dict[str, Any]] | None = None,
    prefer_rail: bool = False,
) -> list[dict[str, Any]]:
    rails = buses or []
    tokens = page_tokens or []
    groups = clusters or []
    prepared = []
    for item in items:
        nodes = item["nodes"]
        far = nodes[-1]
        hop = nodes[1] if len(nodes) > 1 else far
        prev = nodes[-2] if len(nodes) > 1 else None
        prepared.append({"nodes": nodes, "far": far, "hop": hop, "snap": _land(far, pins, rails, tokens, groups, prev)})
    pinned = [item for item in prepared if item["snap"] is not None]
    if len(pinned) >= 3:
        stubs = [
            idx
            for idx, item in enumerate(pinned)
            if not item["snap"].get("rail") and _dist(junction, item["far"]) <= 6.0
        ]
        if len(stubs) == 1:
            hub_idx = stubs[0]
            if all(_dist(junction, item["far"]) > 15.0 for idx, item in enumerate(pinned) if idx != hub_idx):
                hub_item = pinned[hub_idx]
                return [
                    {
                        "segments": _segments(list(reversed(hub_item["nodes"])) + branch["nodes"][1:]),
                        "ends": [hub_item["snap"], branch["snap"]],
                    }
                    for idx, branch in enumerate(pinned)
                    if idx != hub_idx
                ]
    used: set[int] = set()
    ranked: list[tuple[float, int, int]] = []
    for i in range(len(pinned)):
        for j in range(i + 1, len(pinned)):
            dot = _approach_dot(junction, pinned[i]["hop"], pinned[j]["hop"])
            if dot <= -0.85:
                ranked.append((dot, i, j))
    ranked.sort()
    through: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for _dot, i, j in ranked:
        if i in used or j in used:
            continue
        used.add(i)
        used.add(j)
        through.append((pinned[i], pinned[j]))
    out: list[dict[str, Any]] = []
    for left, right in through:
        nodes = list(reversed(left["nodes"])) + right["nodes"][1:]
        out.append({"segments": _segments(nodes), "ends": [left["snap"], right["snap"]]})
    for index, branch in enumerate(pinned):
        if index in used:
            continue
        hub = _shared_hub(junction, through, prefer_rail=prefer_rail)
        ends: list[dict[str, Any] | None] = [branch["snap"], hub]
        out.append({"segments": _segments(branch["nodes"]), "ends": ends})
    return out


def _shared_hub(
    junction: tuple[float, float],
    through: list[tuple[dict[str, Any], dict[str, Any]]],
    prefer_rail: bool = False,
) -> dict[str, Any] | None:
    """The terminal the branch shares is the near end of the straight-through run."""

    best: tuple[float, dict[str, Any]] | None = None
    for left, right in through:
        # Both ends of a straight rail are that rail. A rung that meets it
        # takes the rail, even when the next junction is only one rung away.
        if (
            prefer_rail
            and left["snap"].get("rail")
            and right["snap"].get("rail")
            and left["snap"].get("tag")
            and left["snap"].get("tag") == right["snap"].get("tag")
        ):
            return {
                "tag": left["snap"]["tag"],
                "pin": left["snap"]["pin"],
                "rail": True,
                "x": junction[0],
                "y": junction[1],
            }
        da = _dist(junction, (float(left["snap"]["x"]), float(left["snap"]["y"])))
        db = _dist(junction, (float(right["snap"]["x"]), float(right["snap"]["y"])))
        near, dist, other = (left["snap"], da, db) if da <= db else (right["snap"], db, da)
        if dist <= 40.0 and other >= dist * 2.5 and (best is None or dist < best[0]):
            best = (dist, near)
    if best is None:
        return None
    return best[1]


def _approach_dot(junction: tuple[float, float], hop_a: tuple[float, float], hop_b: tuple[float, float]) -> float:
    ax, ay = hop_a[0] - junction[0], hop_a[1] - junction[1]
    bx, by = hop_b[0] - junction[0], hop_b[1] - junction[1]
    na = (ax * ax + ay * ay) ** 0.5
    nb = (bx * bx + by * by) ** 0.5
    if na < 1e-6 or nb < 1e-6:
        return 1.0
    return (ax * bx + ay * by) / (na * nb)


def _on_pin(point: tuple[float, float], pins: list[dict[str, Any]]) -> bool:
    return any(_dist(point, (float(pin["x"]), float(pin["y"]))) <= PIN_BREAK_MM for pin in pins)


# Reused while one search scores the same stroke lists at many origins.
_SCORE_CACHE: dict[tuple[int, int], tuple[Any, Any, list[dict[str, Any]]]] = {}


def _score_indexes(
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
) -> tuple[Any, Any, list[dict[str, Any]]]:
    key = (id(lines), id(curves))
    cached = _SCORE_CACHE.get(key)
    if cached is not None:
        return cached
    ends = [((float(stroke["a"][0]), float(stroke["a"][1])), (float(stroke["b"][0]), float(stroke["b"][1]))) for stroke in lines]
    body = [
        ((float(stroke["a"][0]), float(stroke["a"][1])), (float(stroke["b"][0]), float(stroke["b"][1])))
        for stroke in lines
        if stroke.get("role") != "wire"
    ]
    body.extend(_straight_chords([stroke for stroke in curves if stroke.get("role") != "wire"]))
    if len(_SCORE_CACHE) > 6:
        _SCORE_CACHE.clear()
    cached = (_SegIndex(ends), _SegIndex(body), curves)
    _SCORE_CACHE[key] = cached
    return cached


class _SegIndex:
    """Strokes bucketed by the cells their bounding box touches."""

    def __init__(self, segments: list[tuple[tuple[float, float], tuple[float, float]]], cell: float = 8.0) -> None:
        self.cell = cell
        self.buckets: dict[tuple[int, int], list[tuple[tuple[float, float], tuple[float, float]]]] = {}
        for segment in segments:
            x0 = min(segment[0][0], segment[1][0])
            x1 = max(segment[0][0], segment[1][0])
            y0 = min(segment[0][1], segment[1][1])
            y1 = max(segment[0][1], segment[1][1])
            ix0, ix1 = int(x0 // cell), int(x1 // cell)
            iy0, iy1 = int(y0 // cell), int(y1 // cell)
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    self.buckets.setdefault((ix, iy), []).append(segment)

    def near(
        self,
        segment: tuple[tuple[float, float], tuple[float, float]],
        margin: float,
    ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        x0 = min(segment[0][0], segment[1][0]) - margin
        x1 = max(segment[0][0], segment[1][0]) + margin
        y0 = min(segment[0][1], segment[1][1]) - margin
        y1 = max(segment[0][1], segment[1][1]) + margin
        cell = self.cell
        found: list[tuple[tuple[float, float], tuple[float, float]]] = []
        seen: set[int] = set()
        for ix in range(int(x0 // cell), int(x1 // cell) + 1):
            for iy in range(int(y0 // cell), int(y1 // cell) + 1):
                for item in self.buckets.get((ix, iy), ()):
                    marker = id(item)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    found.append(item)
        return found

    def any_in(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        """True when a stroke cell meets this box. Same cells ``near`` would walk."""

        cell = self.cell
        buckets = self.buckets
        for ix in range(int(x0 // cell), int(x1 // cell) + 1):
            for iy in range(int(y0 // cell), int(y1 // cell) + 1):
                if buckets.get((ix, iy)):
                    return True
        return False


def _ink_score(
    device: Any,
    ox: float,
    oy: float,
    lines: list[dict[str, Any]],
    curves: list[dict[str, Any]],
    scale: float = 1.0,
    limit: float | None = None,
) -> float:
    """How well the symbol at this origin matches the ink.

    ``limit`` is a score this origin does not need to beat. Once the best
    possible remainder is under that, the search stops. The value returned
    then is still an upper bound, so a caller that keeps a strictly better
    score cannot pick a different winner.
    """

    expected = _symbol_segments(device, ox, oy, scale)
    circles = _symbol_circles(device, ox, oy, scale)
    if not expected and not circles:
        return 0.0
    # A conductor is not symbol ink. A lead may share a wire's endpoints, but
    # it must not count as drawn just because it lies along the rung.
    end_index, body_index, _curves = _score_indexes(lines, curves)
    hits = 0
    slack = 0.0
    total = len(expected) + len(circles)
    tol = 2.7 if scale >= 0.9 else max(0.8, 2.7 * scale)
    # A library lead is often the middle of a longer symbol stroke. The
    # endpoints do not coincide, but the lead still lies on that stroke.
    cover = 1.35 if scale >= 0.9 else max(0.6, 1.35 * scale)
    margin = max(tol, cover) + 0.6
    if _symbol_neighborhood_empty(end_index, body_index, expected, circles, curves, margin, scale):
        return 0.0
    segments_left = len(expected)
    for seg in expected:
        segments_left -= 1
        best: float | None = None
        for stroke in end_index.near(seg, margin):
            end_gap = _segment_gap(seg, stroke)
            if end_gap <= tol and (best is None or end_gap < best):
                best = end_gap
        for stroke in body_index.near(seg, margin):
            along = max(_point_seg(seg[0], stroke[0], stroke[1]), _point_seg(seg[1], stroke[0], stroke[1]))
            if along <= cover and (best is None or along < best):
                best = along
        if best is not None:
            hits += 1
            slack += best
        if limit is not None and _optimistic(hits, segments_left, len(circles), total, slack) < limit:
            return _optimistic(hits, segments_left, len(circles), total, slack)
    circles_left = len(circles)
    circle_tol = max(0.35, 1.8 * scale)
    for cx, cy, radius in circles:
        circles_left -= 1
        if _circle_hit(cx, cy, radius, curves, tol=circle_tol):
            hits += 1
        if limit is not None and _optimistic(hits, 0, circles_left, total, slack) < limit:
            return _optimistic(hits, 0, circles_left, total, slack)
    # A one-millimetre shift still touches every stroke. The tighter origin wins.
    return hits / max(total, 1) - min(slack, 40.0) * 0.004


def _optimistic(hits: int, segments_left: int, circles_left: int, total: int, slack: float) -> float:
    """Highest score still possible. Extra matches cannot raise the slack already counted."""

    return (hits + segments_left + circles_left) / max(total, 1) - min(slack, 40.0) * 0.004


def _symbol_neighborhood_empty(
    end_index: _SegIndex,
    body_index: _SegIndex,
    expected: list[tuple[tuple[float, float], tuple[float, float]]],
    circles: list[tuple[float, float, float]],
    curves: list[dict[str, Any]],
    margin: float,
    scale: float,
) -> bool:
    """True when this origin's score is exactly 0. No stroke can reach the symbol."""

    if expected:
        xs = [point[0] for seg in expected for point in seg]
        ys = [point[1] for seg in expected for point in seg]
        x0, x1 = min(xs) - margin, max(xs) + margin
        y0, y1 = min(ys) - margin, max(ys) + margin
        if end_index.any_in(x0, y0, x1, y1) or body_index.any_in(x0, y0, x1, y1):
            return False
    if not circles or not curves:
        return True
    tol = max(0.35, 1.8 * scale)
    return not _circles_might_hit(circles, curves, tol)


def _circles_might_hit(
    circles: list[tuple[float, float, float]],
    curves: list[dict[str, Any]],
    tol: float,
) -> bool:
    for cx, cy, radius in circles:
        reach = radius + tol
        for stroke in curves:
            points = stroke.get("points") or []
            if len(points) < 2:
                continue
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            if max(xs) < cx - reach or min(xs) > cx + reach or max(ys) < cy - reach or min(ys) > cy + reach:
                continue
            return True
    return False


def _straight_chords(
    curves: list[dict[str, Any]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """A curve that does not leave its chord is a straight stroke."""

    found = []
    for stroke in curves:
        pts = stroke.get("points") or []
        if len(pts) < 2:
            continue
        start = (float(pts[0][0]), float(pts[0][1]))
        end = (float(pts[-1][0]), float(pts[-1][1]))
        if _dist(start, end) < 0.8:
            continue
        sag = 0.0
        for point in pts[1:-1]:
            sag = max(sag, _point_seg((float(point[0]), float(point[1])), start, end))
        if sag <= 0.45:
            found.append((start, end))
    return found


def _symbol_segments(
    device: Any, ox: float, oy: float, scale: float = 1.0
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    found = []
    s = scale
    for prim in device.primitives:
        if prim.type == "line":
            found.append(((ox + prim.x * s, oy + prim.y * s), (ox + prim.x2 * s, oy + prim.y2 * s)))
        elif prim.type == "rect":
            x, y, w, h = ox + prim.x * s, oy + prim.y * s, prim.w * s, prim.h * s
            corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            found.extend(zip(corners, corners[1:] + corners[:1], strict=True))
        elif prim.type == "polyline" and len(prim.points) >= 2:
            pts = [(ox + px * s, oy + py * s) for px, py in prim.points]
            found.extend(zip(pts, pts[1:], strict=False))
        elif prim.type == "arc" and prim.r > 0 and abs(prim.sweep) > 1e-6:
            from panelcad.core.primitives import arc_polyline

            pts = [
                (ox + px * s, oy + py * s)
                for px, py in arc_polyline(prim.x, prim.y, prim.r, prim.start, prim.sweep)
            ]
            found.extend(zip(pts, pts[1:], strict=False))
    return found


def _symbol_circles(device: Any, ox: float, oy: float, scale: float = 1.0) -> list[tuple[float, float, float]]:
    return [
        (ox + prim.x * scale, oy + prim.y * scale, prim.r * scale)
        for prim in device.primitives
        if prim.type == "circle" and prim.r > 0
    ]


def _circle_hit(cx: float, cy: float, radius: float, curves: list[dict[str, Any]], tol: float = 1.8) -> bool:
    near = 0
    for stroke in curves:
        for point in stroke.get("points") or []:
            if abs(_dist((cx, cy), (float(point[0]), float(point[1]))) - radius) <= tol:
                near += 1
                if near >= 4:
                    return True
    return False


def _nearby(lines: list[dict[str, Any]], x: float, y: float, reach: float) -> list[dict[str, Any]]:
    found = []
    for stroke in lines:
        ax, ay = float(stroke["a"][0]), float(stroke["a"][1])
        bx, by = float(stroke["b"][0]), float(stroke["b"][1])
        if min(ax, bx) > x + reach or max(ax, bx) < x - reach or min(ay, by) > y + reach or max(ay, by) < y - 20:
            continue
        found.append(stroke)
    return found


def _nearby_curves(curves: list[dict[str, Any]], x: float, y: float, reach: float) -> list[dict[str, Any]]:
    found = []
    for stroke in curves:
        points = stroke.get("points") or []
        if not points:
            continue
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        if min(xs) > x + reach or max(xs) < x - reach or min(ys) > y + reach or max(ys) < y - 20:
            continue
        found.append(stroke)
    return found


def _point_chain(x: float, y: float, chain: list[tuple[tuple[float, float], tuple[float, float]]]) -> float:
    return min(_point_seg((x, y), a, b) for a, b in chain)


def _point_seg(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    if length <= 1e-9:
        return _dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return _dist(p, (a[0] + t * dx, a[1] + t * dy))


def _segment_gap(a: tuple[tuple[float, float], tuple[float, float]], b: tuple[tuple[float, float], tuple[float, float]]) -> float:
    forward = _dist(a[0], b[0]) + _dist(a[1], b[1])
    backward = _dist(a[0], b[1]) + _dist(a[1], b[0])
    return min(forward, backward)


def _ends_close(a: tuple[tuple[float, float], tuple[float, float]], b: tuple[tuple[float, float], tuple[float, float]], tol: float = 1.35) -> bool:
    return (_dist(a[0], b[0]) <= tol and _dist(a[1], b[1]) <= tol) or (_dist(a[0], b[1]) <= tol and _dist(a[1], b[0]) <= tol)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _span(a: list[float], b: list[float]) -> float:
    return _dist((a[0], a[1]), (b[0], b[1]))


def _pt(point: Any) -> list[float]:
    return [_r(float(point.x) * PT_TO_MM), _r(float(point.y) * PT_TO_MM)]


def _r(value: float) -> float:
    return round(value, 2)


# Sheet borders are long. A conductor across the sheet is long too.
_BORDER_LINE_MM = 95.0
_BORDER_RECT_MM = 250.0


def stroke_role(color: Any, width: Any) -> str:
    """Conductor role for one stroke. A black phase is the same colour as symbol ink."""

    role = _role(color, width)
    if role != "wire" and _black_phase(color, width):
        return "wire"
    return role


def omit_border_line(role: str, a: list[float], b: list[float]) -> bool:
    """A long non-conductor is a sheet border. A long wire stays."""

    return role != "wire" and _span(a, b) > _BORDER_LINE_MM


def omit_border_rect(width: float, height: float) -> bool:
    """A rectangle the size of the sheet is the frame, not a symbol."""

    return abs(width) > _BORDER_RECT_MM or abs(height) > _BORDER_RECT_MM


def _wire_width(width: Any) -> bool:
    """True for a conductor stroke. 0.45 mm is about 1.28 pt. Symbol ink is thinner."""

    try:
        pt = float(width)
    except (TypeError, ValueError):
        return False
    return 1.15 <= pt <= 1.45


def _role(color: Any, width: Any = None) -> str:
    if not color or not isinstance(color, (list, tuple)) or isinstance(color[0], (list, tuple)):
        return "ink"
    red, green, blue = (float(color[0]), float(color[1]), float(color[2]))
    if blue > 0.45 and red < 0.35 and blue > red + 0.12 and blue >= green:
        return "wire"
    # IEC neutral is a light blue. The red channel sits just above the power-blue test.
    if blue > 0.75 and 0.25 < red < 0.55 and green > 0.5 and blue > green:
        return "wire"
    if green > 0.28 and green > red + 0.12 and green > blue + 0.05 and red < 0.25:
        return "wire"
    # Serial runs are orange. A red rail (more blue, less red) is not a conductor.
    if red > 0.82 and 0.2 < green < 0.5 and blue < 0.08:
        return "wire"
    # Phase brown, red, yellow, and grey. The conductor width keeps symbol ink out.
    # A black phase is handled per line: the same ink colour is used for symbols.
    if _wire_width(width):
        if 0.30 < red < 0.55 and 0.12 < green < 0.35 and blue < 0.25 and red > green + 0.08:
            return "wire"
        if red > 0.65 and green < 0.28 and blue < 0.28:
            return "wire"
        if red > 0.70 and 0.50 < green < 0.75 and blue < 0.2 and red - green < 0.28:
            return "wire"
        if abs(red - green) < 0.04 and abs(green - blue) < 0.04 and 0.40 < red < 0.62:
            return "wire"
    if red > 0.45 and green > 0.4 and abs(red - green) < 0.18 and blue < red:
        return "frame"
    return "ink"


def _black_phase(color: Any, width: Any) -> bool:
    """A black phase stroke is the same colour as symbol ink. Width tells them apart."""

    if not _wire_width(width):
        return False
    if not color or not isinstance(color, (list, tuple)) or isinstance(color[0], (list, tuple)):
        return False
    red, green, blue = (float(color[0]), float(color[1]), float(color[2]))
    return red < 0.2 and green < 0.2 and blue < 0.2
