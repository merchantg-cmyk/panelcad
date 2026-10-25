"""Shared schematic / panel body drawing used by SVG, PDF, DXF, and print."""

from __future__ import annotations

from pathlib import Path

from panelcad.core.layout import is_generated_io_sheet, is_io_sheet, is_layout_sheet, is_network_sheet
from panelcad.core.models import Sheet
from panelcad.library.catalog import Catalog
from panelcad.render.io_ladder import draw_io_sheet
from panelcad.render.layout import draw_panel_sheet
from panelcad.render.network import draw_network_sheet
from panelcad.render.painter import Painter, draw_device
from panelcad.render.sheet import pin_world


def draw_sheet_body(
    painter: Painter,
    sheet: Sheet,
    catalog: Catalog,
    *,
    base_dir: Path | None = None,
    coil_style: str = "circle",
    drawing_style: str = "",
    project=None,
    sheet_no: int = 1,
) -> None:
    from panelcad.core.layout import is_wire_by_wire_sheet

    if is_wire_by_wire_sheet(sheet):
        from panelcad.render.panel_wiring_render import draw_wire_by_wire_sheet

        draw_wire_by_wire_sheet(painter, sheet, catalog, project)
        return
    if is_layout_sheet(sheet):
        draw_panel_sheet(painter, sheet, catalog, base_dir=base_dir, project=project)
        return
    if is_network_sheet(sheet):
        draw_network_sheet(painter, sheet, catalog, project=project)
        return
    # A generated I/O sheet is a ladder diagram (kind io, style ladder). The
    # older card painter is only for a hand-drawn I/O page.
    generated_io = is_generated_io_sheet(sheet)
    if is_io_sheet(sheet) and not generated_io:
        draw_io_sheet(painter, sheet, catalog, coil_style=coil_style, project=project)
        return
    if sheet.kind == "terminal":
        from panelcad.render.terminal_strip import draw_terminal_sheet

        draw_terminal_sheet(painter, sheet, project, catalog)
        return
    from panelcad.core.pins import pin_caption
    from panelcad.render.sheet import pin_side_world
    from panelcad.render.titleblock import INK

    ladder = drawing_style == "ladder" and (sheet.kind == "schematic" or generated_io)
    column = drawing_style == "iec" and sheet.kind == "schematic"
    elementary = ladder or column
    iec_numbers = column or str(getattr(sheet, "standard", "") or "") == "IEC"
    from panelcad.core.ink import stacked_power_tuck

    tag_at: dict = {}
    resolved = None
    if sheet.kind == "schematic" and project is not None:
        from panelcad.render.label_avoid import resolve_labels

        try:
            resolved = resolve_labels(
                project, catalog, sheet, ladder=elementary, sheet_no=sheet_no
            )
        except (KeyError, ValueError, IndexError):
            resolved = None
    if resolved is not None:
        tag_at = resolved.tags
    elif sheet.kind == "schematic":
        from panelcad.render.sheet_labels import place_device_tags

        tag_at = place_device_tags(
            sheet, catalog, ladder=elementary, project=project, sheet_no=sheet_no
        )
    power_pins: list[tuple[float, float]] = []
    if not elementary:
        for comp in sheet.components:
            try:
                device = catalog.get(comp.library_id)
            except KeyError:
                continue
            for pin in device.pins:
                power_pins.append(pin_world(comp, pin))

    from panelcad.core.panels import schematic_location_caption, schematic_tag_text

    for comp in sheet.components:
        device = catalog.get(comp.library_id)
        painter.set_layer("DEVICES")
        tag_text = schematic_tag_text(project, sheet, comp) if project is not None else None
        location_text = schematic_location_caption(project, sheet, comp) if project is not None else ""
        draw_device(
            painter,
            device,
            comp,
            coil_style=coil_style,
            ref_align="middle" if elementary else "start",
            tag_at=tag_at.get(comp.id),
            tag_text=tag_text,
            location_text=location_text,
        )
        if comp.properties.get("ladder_card") or comp.properties.get("ladder_coil"):
            continue
        drawn_text = {str(prim.text).strip() for prim in device.primitives if prim.type == "text" and prim.text}
        hidden_pins = {str(pin_id) for pin_id in (comp.properties.get("hidden_pins") or [])}
        selector = "selector" in (device.category or "").lower()
        for pin in device.pins:
            if pin.id in hidden_pins:
                continue
            cap = pin_caption(pin, comp)
            override = bool(cap) and cap != pin.id
            show_id = iec_numbers and cap == pin.id and pin.id not in drawn_text
            if column and selector and cap in {"H", "A"}:
                continue
            if ladder and not override:
                continue
            if not cap or (cap == pin.id and not show_id):
                continue
            wx, wy = pin_world(comp, pin)
            if not elementary:
                painter.set_layer("DEVICES")
                painter.set_fill("#111111")
                painter.circle(wx, wy, 0.7)
                painter.set_fill(None)
            side = pin_side_world(comp, pin)
            painter.set_layer("TEXT")
            painter.set_stroke("#111111", 0.3)
            painter.set_fill("#111111")
            size = 3.2 if ladder else 1.9
            from panelcad.core.ink import pin_label_anchor

            tuck = (not elementary) and stacked_power_tuck(side, wx, wy, power_pins)
            tx, ty, align = pin_label_anchor(
                wx, wy, side, ladder=ladder, column=column, caption=cap, tuck=tuck
            )
            painter.text(tx, ty, cap, size, align=align)
            painter.set_fill(None)
    painter.set_layer("WIRES")
    painter.set_fill(None)
    from panelcad.core.phase_color import wire_phase_pens
    from panelcad.render.painter import draw_wire_caption
    from panelcad.render.wire_labels import bundle_spec_plan, place_wire_marks
    from panelcad.render.wiring import route_sheet_wires

    routes = resolved.routes if resolved is not None else route_sheet_wires(sheet, catalog, project=project)
    if generated_io:
        from panelcad.render.hops import label_routes, rendered_wire_paths

        paths = rendered_wire_paths(sheet, catalog, routes)
        mark_routes = label_routes(routes, paths)
    else:
        paths = None
        mark_routes = routes
    marks = (
        resolved.marks
        if resolved is not None
        else place_wire_marks(sheet, catalog, mark_routes, ladder=elementary, project=project)
    )
    specs = bundle_spec_plan(sheet, mark_routes, ladder=elementary)
    default_stroke = INK if elementary else "#0b4ea2"
    default_width = 0.55 if elementary else 0.45
    pens = wire_phase_pens(project, sheet, catalog) if project is not None else {}
    if project is not None:
        from panelcad.render.phase_ink import line_side_callouts, line_side_runs, pole_runs

        # Paper under the phase color hides the black symbol ink: the supply
        # lead, and each pole's own blade, heater, and load lead. The trip bar
        # is not in this list.
        runs = [*line_side_runs(project, sheet, catalog), *pole_runs(project, sheet, catalog)]
        # All paper first. A run's paper laid after its neighbour's colour cut a
        # white seam into every joint of a heater drawn as arcs.
        painter.set_layer("WIRES")
        for run in runs:
            painter.set_stroke("#ffffff", 0.75)
            painter.polyline(run.points)
        for run in runs:
            painter.set_layer("WIRES")
            if run.pen.outline:
                painter.set_stroke(run.pen.outline, run.pen.outline_width)
                painter.polyline(run.points)
            painter.set_stroke(run.pen.stroke, run.pen.width)
            painter.polyline(run.points)
        for callout in line_side_callouts(project, sheet, catalog):
            painter.set_layer("WIRES")
            if callout.cap is not None:
                if callout.pen.outline:
                    painter.set_stroke(callout.pen.outline, callout.pen.outline_width)
                    painter.polyline(list(callout.cap))
                painter.set_stroke(callout.pen.stroke, callout.pen.width)
                painter.polyline(list(callout.cap))
            c_color = callout.pen.stroke if not callout.pen.outline else INK
            painter.set_stroke(c_color, 0.3)
            painter.set_fill(c_color)
            painter.text(
                callout.x,
                callout.y,
                callout.text,
                size=callout.size,
                align=callout.align,
            )
            painter.set_fill(None)
    if generated_io:
        for wire in sheet.wires:
            subs = (paths or {}).get(wire.id) or []
            painter.set_layer("WIRES")
            pen = pens.get(wire.id)
            for pts in subs:
                drawn = [(p.x, p.y) for p in pts]
                if pen is not None and pen.outline:
                    painter.set_stroke(pen.outline, pen.outline_width)
                    painter.polyline(drawn)
                painter.set_stroke(
                    pen.stroke if pen is not None else default_stroke,
                    pen.width if pen is not None else default_width,
                )
                _stroke_wire(painter, drawn, inferred=getattr(wire, "inferred", False))
            host = subs[0] if subs else []
            draw_wire_caption(
                painter,
                wire,
                host,
                ladder=elementary,
                at=marks.get(wire.id),
                spec=specs.get(wire.id, ""),
            )
    else:
        from panelcad.render.crossings import hop_routes

        hopped = hop_routes(routes)
        from panelcad.render.overlap import ink_polylines

        # A swap that ends on another phase's drop does not repaint that drop.
        visible = ink_polylines(hopped)
        for wire in sheet.wires:
            pts = routes.get(wire.id) or []
            strokes = visible.get(wire.id) or []
            # Captions switch to WIRE_NUMBERS. Put the conductor back on WIRES
            # so each phase stays a wire entity with its own color.
            painter.set_layer("WIRES")
            pen = pens.get(wire.id)
            for drawn in strokes:
                if pen is not None and pen.outline:
                    painter.set_stroke(pen.outline, pen.outline_width)
                    painter.polyline(drawn)
                painter.set_stroke(
                    pen.stroke if pen is not None else default_stroke,
                    pen.width if pen is not None else default_width,
                )
                _stroke_wire(painter, drawn, inferred=getattr(wire, "inferred", False))
            draw_wire_caption(
                painter,
                wire,
                pts,
                ladder=elementary,
                at=marks.get(wire.id),
                spec=specs.get(wire.id, ""),
            )
    from panelcad.render.junctions import junction_points

    pin_pts: list[tuple[float, float, str]] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        hidden_pins = {str(pin_id) for pin_id in (comp.properties.get("hidden_pins") or [])}
        for pin in device.pins:
            if pin.id in hidden_pins:
                continue
            px, py = pin_world(comp, pin)
            pin_pts.append((px, py, comp.id))
    painter.set_layer("WIRES")
    painter.set_stroke("#111111", 0.2)
    painter.set_fill("#111111")
    dots = junction_points(routes, pin_pts)
    for jx, jy in dots:
        painter.circle(jx, jy, 0.8)
    from panelcad.core.ladder import rail_landing_points

    for jx, jy in rail_landing_points(sheet, routes, project):
        if any(abs(jx - x) <= 0.45 and abs(jy - y) <= 0.45 for x, y in dots):
            continue
        painter.circle(jx, jy, 0.8)
    painter.set_fill(None)
    if resolved is not None:
        from panelcad.render.xref import draw_annotation

        painter.set_layer("XREF")
        painter.set_stroke("#111111", 0.25)
        painter.set_fill(None)
        for ann, x, y, align in resolved.xrefs:
            draw_annotation(painter, ann, x, y, align)
    elif project is not None and sheet.kind in {"schematic", "io"}:
        from panelcad.render.xref import draw_cross_references

        draw_cross_references(painter, project, sheet, catalog, ladder=elementary)
    if project is not None and sheet.kind in {"schematic", "io"}:
        from panelcad.render.xref import draw_reference_marks

        draw_reference_marks(painter, project, sheet, catalog)


def _stroke_wire(painter: Painter, points: list[tuple[float, float]], *, inferred: bool) -> None:
    if not inferred or len(points) < 2:
        painter.polyline(points)
        return
    dash, gap = 3.2, 2.2
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        dx, dy = x1 - x0, y1 - y0
        length = (dx * dx + dy * dy) ** 0.5
        if length <= 0:
            continue
        ux, uy = dx / length, dy / length
        walked = 0.0
        draw = True
        while walked < length:
            take = min(dash if draw else gap, length - walked)
            if draw:
                sx, sy = x0 + ux * walked, y0 + uy * walked
                ex, ey = x0 + ux * (walked + take), y0 + uy * (walked + take)
                painter.line(sx, sy, ex, ey)
            walked += take
            draw = not draw
