"""I/O ladder sheets: PLC/IO cards as channel rungs, field left, loads right."""

from __future__ import annotations

from panelcad.core.models import Component, Device, PinDef, Sheet
from panelcad.library.catalog import Catalog
from panelcad.render.painter import Painter, draw_device
from panelcad.render.sheet import parse_pin_ref, pin_world

RUNG = 14.0
HDR = 16.0
FOOT = 8.0
CARD_W = 68.0
LEFT_X = 28.0
OUT_CARD_X = 88.0
CARD_X = 165.0
IN_CARD_X = 250.0
RIGHT_X = 328.0
TOP_Y = 32.0


def io_bus_xs() -> tuple[float, float]:
    """X of the painted supply bus (left) and 0V bus (right) on a packed I/O sheet."""

    return (LEFT_X - 10.0, min(RIGHT_X + 48.0, 392.0))


def is_io_module(device: Device) -> bool:
    cat = device.category.lower()
    if cat.startswith(("drive", "load", "operator", "switching", "connection", "protection", "power")):
        return False
    if "relay" in cat:
        return False
    if cat.startswith("control.plc"):
        return True
    blob = f"{device.name} {device.library_id} {' '.join(device.tags)}".lower()
    if any(k in blob for k in ("click", "dio", "input", "output", "plc")):
        ins, outs = io_channels(device)[:2]
        if ins or outs:
            return True
    ins, outs, _pwr = io_channels(device)
    return len(ins) >= 2 or len(outs) >= 2


def io_channels(device: Device) -> tuple[list[PinDef], list[PinDef], list[PinDef]]:
    power: list[PinDef] = []
    inputs: list[PinDef] = []
    outputs: list[PinDef] = []
    power_ids = {"lplus", "l+", "24v", "c0", "c1", "m", "com", "minus", "plus", "pe", "n", "l"}
    for p in device.pins:
        pid = p.id.lower()
        if p.electrical == "power" or pid in power_ids:
            power.append(p)
        elif p.electrical == "input" or pid.startswith(("x", "i", "di")):
            inputs.append(p)
        elif p.electrical == "output" or pid.startswith(("y", "q", "do")):
            outputs.append(p)
        elif p.electrical == "input":
            inputs.append(p)
        elif p.electrical == "output":
            outputs.append(p)
    return inputs, outputs, power


def io_card_size(device: Device, component: Component | None = None) -> tuple[float, float]:
    ins, outs, _p = io_channels(device)
    n = max(len(ins), len(outs), 1)
    h = HDR + n * RUNG + FOOT
    w = CARD_W
    s = component.visual_scale() if component is not None else 1.0
    return (w * s, h * s)


def io_pin_is_input(device: Device, pin_id: str) -> bool | None:
    ins, outs, _p = io_channels(device)
    if any(p.id == pin_id for p in ins):
        return True
    if any(p.id == pin_id for p in outs):
        return False
    return None


def io_module_kind(device: Device) -> str:
    """input cards sit on the right, output cards on the left, combo in the middle."""
    ins, outs, _p = io_channels(device)
    has_in = bool(ins)
    has_out = bool(outs)
    if has_in and has_out:
        return "combo"
    if has_out and not has_in:
        return "output"
    return "input"


def io_card_column(device: Device) -> float:
    kind = io_module_kind(device)
    if kind == "output":
        return OUT_CARD_X
    if kind == "input":
        return IN_CARD_X
    return CARD_X


def io_field_is_left(device: Device, to_input: bool | None = None) -> bool:
    """Field in on the left, loads / solenoids / lights out on the right."""
    lid = device.library_id.lower()
    if "offpage_from" in lid:
        return True
    if "connection.offpage" in lid:
        return False
    if to_input is True:
        return True
    if to_input is False:
        return False
    cat = device.category.lower()
    if cat.startswith("operator.light") or cat.startswith("load") or cat.startswith("drive"):
        return False
    if "relay" in cat or cat.startswith("switching"):
        return False
    if cat.startswith("operator") or cat.startswith("protection"):
        return True
    return False


def io_pin_layout(device: Device) -> dict[str, tuple[float, float]]:
    ins, outs, power = io_channels(device)
    n = max(len(ins), len(outs), 1)
    w, h = CARD_W, HDR + n * RUNG + FOOT
    laid: dict[str, tuple[float, float]] = {}
    for i, p in enumerate(ins):
        laid[p.id] = (0.0, HDR + i * RUNG)
    for i, p in enumerate(outs):
        laid[p.id] = (w, HDR + i * RUNG)
    for i, p in enumerate(power):
        laid[p.id] = (w * (i + 1) / (len(power) + 1), 0.0)
    for p in device.pins:
        if p.id not in laid:
            laid[p.id] = (w / 2.0, h)
    return laid


def io_endpoint(sheet: Sheet, catalog: Catalog, ref: str) -> tuple[float, float]:
    cid, pid = parse_pin_ref(ref)
    comp = sheet.component(cid)
    device = catalog.get(comp.library_id)
    s = comp.visual_scale()
    if is_io_module(device):
        laid = io_pin_layout(device)
        lx, ly = comp.pin_xy_id(pid, laid.get(pid, (CARD_W / 2.0, HDR)))
        return (comp.x + lx * s, comp.y + ly * s)
    try:
        return pin_world(comp, device.pin(pid))
    except KeyError:
        return (comp.x, comp.y)


def draw_io_card(
    painter: Painter, device: Device, component: Component, *, unused: set[str] | None = None
) -> None:
    ins, outs, _p = io_channels(device)
    n = max(len(ins), len(outs), 1)
    s = component.visual_scale()
    w, h = CARD_W * s, (HDR + n * RUNG + FOOT) * s
    x, y = component.x, component.y
    painter.set_stroke("#1b1b1b", 0.45)
    painter.set_fill("#fff8e1")
    painter.rect(x, y, w, h)
    painter.set_fill(None)
    painter.set_fill("#d9e4f2")
    painter.rect(x, y, w, 14.0 * s)
    painter.set_fill(None)
    tag = str(component.properties.get("catalog") or component.properties.get("tag") or device.name)
    painter.text(x + w / 2.0, y + 5.0 * s, component.id, 2.8, align="middle")
    painter.text(x + w / 2.0, y + 10.5 * s, tag[:18], 1.9, align="middle")
    laid = io_pin_layout(device)
    painter.set_stroke("#c5b48a", 0.25)
    for p in ins + outs:
        lx, ly = laid[p.id]
        py = y + ly * s
        painter.line(x, py, x + w, py)
    for p in ins + outs:
        lx, ly = laid[p.id]
        px, py = x + lx * s, y + ly * s
        painter.set_fill("#111111")
        painter.set_stroke("#1b1b1b", 0.4)
        painter.circle(px, py, 0.8)
        painter.set_fill(None)
        side = "start" if p in ins else "end"
        tx = px + (3.5 if p in ins else -3.5)
        from panelcad.core.pins import pin_caption

        cap = pin_caption(p, component)
        painter.text(tx, py - 0.6, cap, 2.0, align=side)
        if unused and p.id in unused:
            inner = px + (10.0 if p in ins else -10.0)
            painter.text(inner, py - 0.5, "spare", 1.8, align="start" if p in ins else "end")


def draw_io_sheet(
    painter: Painter,
    sheet: Sheet,
    catalog: Catalog,
    *,
    coil_style: str = "circle",
    project=None,
) -> None:
    modules = [c for c in sheet.components if is_io_module(catalog.get(c.library_id))]
    others = [c for c in sheet.components if c not in modules]
    top = min((c.y for c in sheet.components), default=TOP_Y) - 8
    bottom = TOP_Y + 80
    for c in sheet.components:
        device = catalog.get(c.library_id)
        if is_io_module(device):
            _w, h = io_card_size(device, c)
            bottom = max(bottom, c.y + h + 8)
        else:
            bottom = max(bottom, c.y + device.schematic_height * c.visual_scale() + 8)
    painter.set_stroke("#c0392b", 0.7)
    supply_x, zero_x = io_bus_xs()
    painter.line(supply_x, top, supply_x, bottom)
    painter.text(supply_x, top - 4, "24V", 2.4, align="middle")
    painter.set_stroke("#2c3e50", 0.7)
    painter.line(zero_x, top, zero_x, bottom)
    painter.text(zero_x, top - 4, "0V", 2.4, align="middle")

    wired: dict[str, set[str]] = {c.id: set() for c in modules}
    for wire in sheet.wires:
        for end in (wire.frm, wire.to):
            cid, pid = parse_pin_ref(end) if "." in end else ("", "")
            if cid in wired:
                wired[cid].add(pid)
    # Same collision pass the schematic sheets get. Without it a stacked field
    # device's tag sat on the symbol above it and wire marks sat on bodies.
    resolved = None
    if project is not None:
        from panelcad.render.label_avoid import resolve_labels

        try:
            resolved = resolve_labels(project, catalog, sheet, ladder=False, captions=False)
        except (KeyError, ValueError, IndexError):
            resolved = None
    tag_at = resolved.tags if resolved is not None else {}
    for c in modules:
        device = catalog.get(c.library_id)
        ins, outs, _p = io_channels(device)
        unused = {p.id for p in ins + outs} - wired.get(c.id, set())
        draw_io_card(painter, device, c, unused=unused)
    for c in others:
        device = catalog.get(c.library_id)
        # Wire already carries the PLC tag. Drawing it again on the coil stacks onto the next rung.
        draw_device(painter, device, c, coil_style=coil_style, captions=False, tag_at=tag_at.get(c.id))
        callout = str(c.properties.get("callout") or "")
        if project is not None and "offpage" in (c.library_id or ""):
            from panelcad.core.xref import offpage_callout

            live = offpage_callout(project, catalog, sheet, c)
            if live:
                callout = live
        s = c.visual_scale()
        if callout:
            painter.text(c.x + device.schematic_width * s + 2.0, c.y + 4.0, callout, 2.2, align="start")
        for pin in device.pins:
            wx, wy = pin_world(c, pin)
            painter.set_fill("#111111")
            painter.circle(wx, wy, 0.7)
            painter.set_fill(None)

    painter.set_stroke("#0b4ea2", 0.45)
    painter.set_fill(None)
    from panelcad.render.painter import draw_wire_caption
    from panelcad.render.wire_labels import bundle_spec_plan, place_wire_marks
    from panelcad.render.wiring import route_sheet_wires

    if resolved is not None:
        routes = dict(resolved.routes)
    else:
        try:
            routes = route_sheet_wires(sheet, catalog, project=project)
        except (KeyError, ValueError):
            routes = {}
    marks = dict(resolved.marks) if resolved is not None else place_wire_marks(sheet, catalog, routes, ladder=False)
    specs = bundle_spec_plan(sheet, routes, ladder=False)
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        if not pts:
            continue
        painter.polyline([(p.x, p.y) for p in pts])
        draw_wire_caption(
            painter, wire, pts, ladder=False, at=marks.get(wire.id), spec=specs.get(wire.id, "")
        )
