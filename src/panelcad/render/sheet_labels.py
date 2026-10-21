"""Named text and symbol boxes as they are drawn on a schematic sheet."""

from __future__ import annotations

from panelcad.core.ink import TAG_SIZE, device_ref_layout, text_box
from panelcad.core.route import component_aabb
from panelcad.core.models import Project, Sheet
from panelcad.core.xref import xref_style
from panelcad.library.catalog import Catalog
from panelcad.render.wire_labels import MARK_SIZE, boxes_overlap, mark_box, place_wire_marks
from panelcad.render.wiring import route_sheet_wires
from panelcad.render.xref import annotation_box, place_xref_labels

# Half the schematic stroke, so clearance is measured to the ink of the conductor.
CONDUCTOR_HALF = 0.23
BODY_PAD = 0.4


def symbol_body_box(comp, device) -> tuple[float, float, float, float]:
    s = comp.visual_scale()
    w = float(device.schematic_width) * s
    h = float(device.schematic_height) * s
    return component_aabb(comp.x, comp.y, w, h, int(comp.rotation or 0), bool(comp.mirror))


def sheet_symbol_bodies(sheet: Sheet, catalog: Catalog) -> list[tuple[str, tuple[float, float, float, float]]]:
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        cat = (device.category or "").lower()
        if cat.startswith("connection.") and "terminal" not in cat:
            continue
        out.append((f"body:{comp.id}", symbol_body_box(comp, device)))
    return out


def _line_number_boxes(
    project: Project,
    sheet: Sheet,
    catalog: Catalog,
    *,
    sheet_no: int,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    if xref_style(project, sheet) != "nema":
        return []
    from panelcad.core.ladder import RAIL_INSET
    from panelcad.core.xref import occupied_rung_indices, resolve_settings
    from panelcad.core.zones import nema_line_from_index, rung_pitch_mm, rung_y, zone_grid

    settings = resolve_settings(project)
    pitch = rung_pitch_mm(project)
    grid = zone_grid(sheet)
    left = grid.wx + RAIL_INSET
    if str(getattr(settings, "rung_numbers", "used")) == "all":
        indexes = list(range(1, grid.max_rung_index(pitch) + 1))
    else:
        indexes = sorted(occupied_rung_indices(project, sheet, catalog))
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for index in indexes:
        line = nema_line_from_index(sheet_no, index)
        yy = rung_y(sheet, index, pitch=pitch)
        box = text_box(left - 2.1, yy + 0.7, str(line), 2.0, "end")
        if box is not None:
            out.append((f"line:{line}", box))
    return out


def sheet_conductor_boxes(
    sheet: Sheet,
    catalog: Catalog,
    project: Project | None = None,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """One box per stroked conductor segment."""

    try:
        routes = route_sheet_wires(sheet, catalog, project=project)
    except (KeyError, ValueError, IndexError):
        return []
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    half = CONDUCTOR_HALF
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        for a, b in zip(pts, pts[1:]):
            if abs(a.x - b.x) <= 1e-6 and abs(a.y - b.y) <= 1e-6:
                continue
            out.append(
                (
                    f"conductor:{wire.id}",
                    (
                        min(a.x, b.x) - half,
                        min(a.y, b.y) - half,
                        max(a.x, b.x) + half,
                        max(a.y, b.y) + half,
                    ),
                )
            )
    return out


def sheet_terminal_dots(
    sheet: Sheet,
    catalog: Catalog,
    *,
    ladder: bool = False,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Drawn terminal dots. Ladder sheets do not ink the dots."""

    if ladder or sheet.kind != "schematic":
        return []
    from panelcad.core.ink import PIN_DOT_R
    from panelcad.core.pins import pin_caption
    from panelcad.render.sheet import pin_world

    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        for pin in device.pins:
            if not pin_caption(pin, comp):
                continue
            wx, wy = pin_world(comp, pin)
            out.append(
                (
                    f"terminal:{comp.id}.{pin.id}",
                    (wx - PIN_DOT_R, wy - PIN_DOT_R, wx + PIN_DOT_R, wy + PIN_DOT_R),
                )
            )
    return out


def caption_layouts(
    component,
    tag_layout: tuple[float, float, str, float] | None,
) -> list[tuple[float, float, str, float, str]]:
    """Extra lines (SAFE, FWD/REV) stacked on the device tag, or above the symbol."""

    from panelcad.core.ink import TEXT_ASCENT
    from panelcad.core.pins import extra_captions

    lines = extra_captions(component)
    if not lines:
        return []
    scale = component.visual_scale()
    if tag_layout is None:
        y_cap = component.y - 5.6 * scale
        out: list[tuple[float, float, str, float, str]] = []
        for line in lines:
            out.append((component.x, y_cap, "start", 2.2, line))
            y_cap -= 3.1 * scale
        return out
    x, y, align, size = tag_layout
    cap_y = y - (float(size) * TEXT_ASCENT + 0.6 * scale)
    out = []
    for line in lines:
        out.append((x, cap_y, align, 2.2, line))
        cap_y -= 3.1 * scale
    return out


def _expects_visible_tag(comp, device) -> bool:
    from panelcad.core.block import has_tag_attribute
    from panelcad.render.painter import is_control_coil

    cat = (device.category or "").lower()
    if cat.startswith("connection."):
        return False
    if has_tag_attribute(device):
        return False
    if comp.properties.get("ladder_card") or comp.properties.get("ladder_coil"):
        return False
    if is_control_coil(device, comp.library_id):
        return False
    return True


def _notes_boxes(sheet: Sheet, catalog: Catalog) -> list[tuple[float, float, float, float]]:
    from panelcad.render.painter import notes_box

    out: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        if not (device.category or "").lower().startswith("connection.notes"):
            continue
        width, height = notes_box(comp, comp.visual_scale())
        out.append((comp.x, comp.y, comp.x + width, comp.y + height))
    return out


def place_device_tags(
    sheet: Sheet,
    catalog: Catalog,
    *,
    ladder: bool = False,
    project: Project | None = None,
    sheet_no: int = 1,
    include_xref: bool = True,
) -> dict[str, tuple[float, float, str, float]]:
    """Where each device tag is drawn.

    Schematic tags sit just above the symbol and start to its left, one text
    height clear of wire labels, conductors, terminal dots, line numbers, and
    the notes block. A notes symbol does not get a tag; the painter does not draw one.
    ``include_xref`` is false when cross-reference placement is asking for the
    tag boxes, so the two passes do not call each other.
    """

    from panelcad.render.wire_labels import share_wire_marks

    with share_wire_marks():
        return _place_device_tags(
            sheet,
            catalog,
            ladder=ladder,
            project=project,
            sheet_no=sheet_no,
            include_xref=include_xref,
        )


def _place_device_tags(
    sheet: Sheet,
    catalog: Catalog,
    *,
    ladder: bool = False,
    project: Project | None = None,
    sheet_no: int = 1,
    include_xref: bool = True,
) -> dict[str, tuple[float, float, str, float]]:
    from panelcad.core.ink import TEXT_DESCENT
    from panelcad.core.ladder import is_aux_library
    from panelcad.core.pins import rating_caption_positions
    from panelcad.render.titleblock import working_rect

    layouts: dict[str, tuple[float, float, str, float]] = {}
    devices = {}
    align = "middle" if ladder else "start"
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        if (device.category or "").lower().startswith("connection.notes"):
            continue
        devices[comp.id] = device
        if comp.properties.get("tag_below") or is_aux_library(comp.library_id):
            scale = comp.visual_scale()
            layouts[comp.id] = (
                comp.x + float(device.schematic_width) * scale * 0.5,
                comp.y + float(device.schematic_height) * scale + 3.2 * scale,
                "middle",
                2.6,
            )
            continue
        layout = device_ref_layout(device, comp, ref_align=align)
        if layout is not None:
            layouts[comp.id] = layout
    if ladder or sheet.kind != "schematic":
        return layouts

    try:
        routes = route_sheet_wires(sheet, catalog, project=project)
    except (KeyError, ValueError, IndexError):
        routes = {}
    from panelcad.render.wire_labels import bundle_spec_plan

    marks = place_wire_marks(sheet, catalog, routes, ladder=False, project=project)
    specs = bundle_spec_plan(sheet, routes, ladder=False)
    wire_boxes: list[tuple[float, float, float, float]] = []
    text_obstacles: list[tuple[tuple[float, float, float, float], float]] = []
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=False)
        if not at or not mark:
            continue
        box = mark_box(at[0], at[1], mark, align=at[2], spec=specs.get(wire.id, ""))
        wire_boxes.append(box)
        text_obstacles.append((box, MARK_SIZE))
    notes_boxes = _notes_boxes(sheet, catalog)
    for comp in sheet.components:
        device = devices.get(comp.id)
        if device is None:
            continue
        scale = comp.visual_scale()
        for cx, cy, line in rating_caption_positions(
            comp, device.schematic_height, scale, device.schematic_width
        ):
            box = text_box(cx, cy, line, 2.2, "start")
            if box is not None:
                text_obstacles.append((box, 2.2))
    line_boxes: list[tuple[float, float, float, float]] = []
    if project is not None:
        for _name, box in _line_number_boxes(project, sheet, catalog, sheet_no=sheet_no):
            line_boxes.append(box)
            text_obstacles.append((box, 2.0))
        if include_xref:
            for ann, x, y, aalign in place_xref_labels(project, catalog, sheet, ladder=False):
                text_obstacles.append((annotation_box(ann, x, y, aalign), 2.2))
    geom = [box for _name, box in sheet_conductor_boxes(sheet, catalog, project)]
    geom.extend(box for _name, box in sheet_terminal_dots(sheet, catalog, ladder=False))
    bodies = {
        comp.id: symbol_body_box(comp, devices[comp.id])
        for comp in sheet.components
        if comp.id in devices and not (devices[comp.id].category or "").lower().startswith("connection.")
    }
    earth = [
        symbol_body_box(comp, devices[comp.id])
        for comp in sheet.components
        if comp.id in devices
        and (
            str(comp.properties.get("role") or "") == "pe_bar"
            or (devices[comp.id].category or "").lower().startswith("connection.ground")
        )
    ]
    wx, wy, ww, wh = working_rect(sheet)
    bounds = (wx, wy, wx + ww, wy + wh)

    def blocked(
        box: tuple[float, float, float, float],
        owner: str,
        taken: list[tuple[float, float, float, float]],
    ) -> bool:
        if box[0] < bounds[0] - 0.05 or box[1] < bounds[1] - 0.05:
            return True
        if box[2] > bounds[2] + 0.05 or box[3] > bounds[3] + 0.05:
            return True
        for other_id, body in bodies.items():
            if other_id != owner and boxes_overlap(box, body, pad=BODY_PAD):
                return True
        own = bodies.get(owner)
        if own is not None and boxes_overlap(box, own, pad=BODY_PAD):
            return True
        for prior in taken:
            if boxes_overlap(box, prior, pad=0.6):
                return True
        for wire_box in wire_boxes:
            if boxes_overlap(box, wire_box, pad=TAG_SIZE):
                return True
        for note in notes_boxes:
            if boxes_overlap(box, note, pad=TAG_SIZE):
                return True
        for obstacle, other_size in text_obstacles:
            if boxes_overlap(box, obstacle, pad=max(TAG_SIZE, other_size)):
                return True
        for obstacle in geom:
            if boxes_overlap(box, obstacle, pad=TAG_SIZE):
                return True
        for obstacle in line_boxes:
            if boxes_overlap(box, obstacle, pad=TAG_SIZE):
                return True
        return False

    from panelcad.core.panels import schematic_location_caption, schematic_tag_text

    def _label(comp) -> str:
        if project is None:
            return comp.id
        return schematic_tag_text(project, sheet, comp)

    def _location(comp) -> str:
        if project is None:
            return ""
        return schematic_location_caption(project, sheet, comp)

    taken: list[tuple[float, float, float, float]] = []
    order = sorted(sheet.components, key=lambda comp: (comp.y, comp.x, comp.id))
    for comp in order:
        label = _label(comp)
        layout = layouts.get(comp.id)
        if layout is None or layout[2] != "start":
            if layout is not None:
                box = text_box(layout[0], layout[1], label, layout[3], layout[2])
                if box is not None:
                    taken.append(box)
            continue
        if int(comp.rotation or 0) % 360 != 0 or comp.mirror:
            box = text_box(layout[0], layout[1], label, layout[3], layout[2])
            if box is not None:
                taken.append(box)
            continue
        # Circuit Builder parks Q1/F1/M1 beside the body. The search above
        # the origin puts M1 on F1's load terminals and F1 up at the junction.
        if comp.properties.get("ref_dx") not in (None, "") or comp.properties.get("ref_dy") not in (None, ""):
            layout = _clear_tag_of_earth(layout, comp.id, bodies.get(comp.id), earth)
            layouts[comp.id] = layout
            box = text_box(layout[0], layout[1], label, layout[3], layout[2])
            if box is not None:
                taken.append(box)
            continue
        scale = comp.visual_scale()
        size = float(layout[3])
        found: tuple[float, float, str, float] | None = None
        found_boxes: list[tuple[float, float, float, float]] = []
        for step in range(36):
            margin = 2.2 + step * 0.6
            for side in range(56):
                anchor = comp.x - (0.8 + side * 0.7) * scale
                baseline = comp.y - margin * scale - size * TEXT_DESCENT * scale
                box = text_box(anchor, baseline, label, size, "start")
                if box is None or box[3] > comp.y - 0.25 * scale or box[0] >= comp.x - 0.15 * scale:
                    continue
                trial = (anchor, baseline, "start", size)
                extra: list[tuple[float, float, float, float]] = []
                clear = not blocked(box, comp.id, taken)
                if clear:
                    for cx, cy, calign, csize, line in caption_layouts(comp, trial):
                        cbox = text_box(cx, cy, line, csize, calign)
                        if cbox is None or blocked(cbox, comp.id, taken):
                            clear = False
                            break
                        extra.append(cbox)
                if clear:
                    loc = _location(comp)
                    if loc:
                        lx, ly, lalign, _lsize = trial
                        lbox = text_box(lx, ly - 3.4, loc, 2.0, lalign)
                        if lbox is None or blocked(lbox, comp.id, taken):
                            clear = False
                        else:
                            extra.append(lbox)
                if not clear:
                    continue
                found = trial
                found_boxes = [box, *extra]
                break
            if found is not None:
                break
        if found is not None:
            layouts[comp.id] = found
            taken.extend(found_boxes)
        else:
            current = text_box(layout[0], layout[1], label, layout[3], layout[2])
            if current is not None:
                taken.append(current)
            for cx, cy, calign, csize, line in caption_layouts(comp, layout):
                cbox = text_box(cx, cy, line, csize, calign)
                if cbox is not None:
                    taken.append(cbox)
    return layouts


def _clear_tag_of_earth(
    layout: tuple[float, float, str, float],
    ref: str,
    body: tuple[float, float, float, float] | None,
    earth: list[tuple[float, float, float, float]],
) -> tuple[float, float, str, float]:
    """Slide an authored tag along its symbol until it clears the PE mark."""

    if not earth or body is None:
        return layout
    x, y, align, size = layout
    box = text_box(x, y, ref, size, align)
    if box is None or not _tag_reads_as_earth(box, earth):
        return layout
    for step in range(1, 36):
        for sign in (1.0, -1.0):
            trial_y = y + sign * step * 0.8
            trial = text_box(x, trial_y, ref, size, align)
            if trial is None:
                continue
            if trial[3] < body[1] - 1.0 or trial[1] > body[3] + 1.0:
                continue
            if _tag_reads_as_earth(trial, earth):
                continue
            return (x, trial_y, align, size)
    return layout


def _tag_reads_as_earth(
    box: tuple[float, float, float, float],
    earth: list[tuple[float, float, float, float]],
) -> bool:
    """True when the tag crosses the PE mark or sits directly under it.

    Clearing the ink by a fraction of a millimetre still left Q1 under the
    earth symbol. Eight millimetres of air makes the tag read as the breaker.
    """

    for other in earth:
        if boxes_overlap(box, other):
            return True
        shares_x = box[2] > other[0] + 0.6 and box[0] < other[2] - 0.6
        if shares_x and other[1] - 1.0 < box[3] and box[1] < other[3] + 8.0:
            return True
    return False


def pin_caption_boxes(
    sheet: Sheet,
    catalog: Catalog,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Pin captions as ``draw_sheet_body`` paints them."""

    from panelcad.core.ink import pin_label_anchor, stacked_power_tuck
    from panelcad.core.pins import pin_caption
    from panelcad.render.sheet import pin_side_world, pin_world

    style = str(getattr(sheet, "drawing_style", "") or "")
    column = style == "iec"
    ladder = style == "ladder"
    iec_numbers = column or str(getattr(sheet, "standard", "") or "") == "IEC"
    power_pins: list[tuple[float, float]] = []
    if not ladder and not column:
        for comp in sheet.components:
            device = catalog.devices.get(comp.library_id)
            if device is None:
                continue
            for pin in device.pins:
                power_pins.append(pin_world(comp, pin))
    items: list[tuple[str, tuple[float, float, float, float]]] = []
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        drawn_text = {str(prim.text).strip() for prim in device.primitives if prim.type == "text" and prim.text}
        hidden = {str(pin_id) for pin_id in (comp.properties.get("hidden_pins") or [])}
        selector = "selector" in (device.category or "").lower()
        for pin in device.pins:
            if pin.id in hidden:
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
            side = pin_side_world(comp, pin)
            size = 3.2 if ladder else 1.9
            tuck = (not ladder and not column) and stacked_power_tuck(side, wx, wy, power_pins)
            tx, ty, align = pin_label_anchor(
                wx, wy, side, ladder=ladder, column=column, caption=cap, tuck=tuck
            )
            box = text_box(tx, ty, cap, size, align)
            if box is not None:
                items.append((f"pin:{comp.id}:{cap}", box))
    return items


def sheet_text_boxes(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool = False,
    sheet_no: int = 1,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Tags, wire numbers, xrefs/mirrors, and rung line numbers as drawn."""

    from panelcad.core.pins import rating_caption_positions
    from panelcad.render.label_avoid import resolve_labels

    items: list[tuple[str, tuple[float, float, float, float]]] = []
    resolved = resolve_labels(project, catalog, sheet, ladder=ladder, sheet_no=sheet_no)
    from panelcad.core.panels import schematic_tag_text

    tags = resolved.tags
    for comp in sheet.components:
        device = catalog.devices.get(comp.library_id)
        if device is None:
            continue
        layout = tags.get(comp.id)
        if layout is not None:
            x, y, talign, size = layout
            box = text_box(x, y, schematic_tag_text(project, sheet, comp), size, talign)
            if box is not None:
                items.append((f"tag:{comp.id}", box))
        for cx, cy, calign, csize, line in caption_layouts(comp, layout):
            box = text_box(cx, cy, line, csize, calign)
            if box is not None:
                items.append((f"caption:{comp.id}:{line}", box))
        s = comp.visual_scale()
        for cx, cy, line in rating_caption_positions(
            comp, device.schematic_height, s, device.schematic_width
        ):
            box = text_box(cx, cy, line, 2.2, "start")
            if box is not None:
                items.append((f"rating:{comp.id}:{line}", box))

    routes = resolved.routes
    from panelcad.render.wire_labels import bundle_spec_plan, mark_box

    marks = resolved.marks
    specs = bundle_spec_plan(sheet, routes, ladder=ladder)
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=ladder)
        if not at or not mark:
            continue
        spec = specs.get(wire.id, "")
        if spec:
            box = mark_box(at[0], at[1], mark, align=at[2], spec=spec)
        else:
            box = text_box(at[0], at[1], mark, 2.3, at[2])
        if box is not None:
            items.append((f"wire:{wire.id}:{mark}", box))

    for ann, x, y, aalign in resolved.xrefs:
        items.append((f"xref:{ann.kind}:{ann.owner}", annotation_box(ann, x, y, aalign)))

    items.extend(_line_number_boxes(project, sheet, catalog, sheet_no=sheet_no))
    items.extend(pin_caption_boxes(sheet, catalog))
    return items


def device_tag_faults(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool = False,
    sheet_no: int = 1,
) -> list[str]:
    """Tags that touch ink, sit closer than one text height, or are not on the sheet.

    A placed device with no tag box, or a tag covered by the notes block, fails.
    Clearance is one text height from wire labels, conductors, dots, and line numbers.
    The old top-left MTR1 tag still crosses the U lead.
    """

    labels = sheet_text_boxes(project, catalog, sheet, ladder=ladder, sheet_no=sheet_no)
    tags = [(name, box) for name, box in labels if name.startswith("tag:")]
    wires = [(name, box) for name, box in labels if name.startswith("wire:")]
    lines = [(name, box) for name, box in labels if name.startswith("line:")]
    conductors = sheet_conductor_boxes(sheet, catalog, project)
    dots = sheet_terminal_dots(sheet, catalog, ladder=ladder)
    notes = _notes_boxes(sheet, catalog)
    faults: list[str] = []
    tagged: set[str] = set()
    for name, box in tags:
        tagged.add(name.split(":", 1)[1])
        for nbox in notes:
            if boxes_overlap(box, nbox):
                faults.append(f"{sheet.id}: {name} has no visible tag")
        for cname, cbox in conductors:
            if boxes_overlap(box, cbox):
                faults.append(f"{sheet.id}: {name} touches {cname}")
            elif boxes_overlap(box, cbox, pad=TAG_SIZE):
                faults.append(f"{sheet.id}: {name} is closer than one text height to {cname}")
        for wname, wbox in wires:
            if boxes_overlap(box, wbox, pad=TAG_SIZE):
                faults.append(f"{sheet.id}: {name} is closer than one text height to {wname}")
        for dname, dbox in dots:
            if boxes_overlap(box, dbox, pad=TAG_SIZE):
                faults.append(f"{sheet.id}: {name} is closer than one text height to {dname}")
        for lname, lbox in lines:
            if boxes_overlap(box, lbox, pad=TAG_SIZE):
                faults.append(f"{sheet.id}: {name} is closer than one text height to {lname}")
    if not ladder:
        for comp in sheet.components:
            device = catalog.devices.get(comp.library_id)
            if device is None or not _expects_visible_tag(comp, device):
                continue
            if comp.id not in tagged:
                faults.append(f"{sheet.id}: {comp.id} has no visible tag")
    return faults


def colliding_sheet_labels(
    project: Project,
    catalog: Catalog,
    sheet: Sheet,
    *,
    ladder: bool = False,
    sheet_no: int = 1,
) -> list[str]:
    """Human-readable collisions: text-vs-text and text-vs-symbol-body."""

    labels = sheet_text_boxes(project, catalog, sheet, ladder=ladder, sheet_no=sheet_no)
    bodies = sheet_symbol_bodies(sheet, catalog)
    hits: list[str] = []
    for i, (an, ab) in enumerate(labels):
        for bn, bb in labels[i + 1 :]:
            if boxes_overlap(ab, bb):
                hits.append(f"{sheet.id}: {an} overlaps {bn}")
        for bn, bb in bodies:
            owner = an.split(":")[1] if an.startswith(("tag:", "caption:", "rating:", "pin:")) else ""
            if bn == f"body:{owner}" and an.startswith(("tag:", "pin:")):
                continue
            if boxes_overlap(ab, bb):
                hits.append(f"{sheet.id}: {an} overlaps {bn}")
    return hits
