"""Communications / network sheet drawing."""

from __future__ import annotations

from panelcad.core.geometry import Point, rotate_point
from panelcad.core.models import CommPort, Component, Device, Sheet
from panelcad.library.catalog import Catalog
from panelcad.library.ports import effective_ports, network_node_caption
from panelcad.render.painter import Painter
from panelcad.render.sheet import iter_wire_points, parse_pin_ref

NODE_W = 72.0
NODE_H = 42.0

_KIND_FILL = {
    "ethernet": "#c8e6c9",
    "serial": "#ffe0b2",
    "usb": "#e1bee7",
    "can": "#b3e5fc",
    "fieldbus": "#d1c4e9",
    "other": "#eceff1",
}
_KIND_INK = {
    "ethernet": "#1b5e20",
    "serial": "#e65100",
    "usb": "#6a1b9a",
    "mixed": "#0b4ea2",
}


def node_size(n_ports: int, has_address: bool = True) -> tuple[float, float]:
    h = max(NODE_H, 22.0 + 8.0 * max(n_ports, 1))
    if has_address:
        h += 10.0
    return (NODE_W, h)


def port_local(port: CommPort, index_on_side: int, count_on_side: int, w: float, h: float) -> tuple[float, float]:
    n = max(count_on_side, 1)
    t = (index_on_side + 1) / (n + 1)
    if port.side == "left":
        return (0.0, h * t)
    if port.side == "top":
        return (w * t, 0.0)
    if port.side == "bottom":
        return (w * t, h)
    return (w, h * t)


def port_layout(ports: list[CommPort], w: float, h: float) -> dict[str, tuple[float, float]]:
    by_side: dict[str, list[CommPort]] = {"left": [], "right": [], "top": [], "bottom": []}
    for p in ports:
        by_side.get(p.side, by_side["right"]).append(p)
    out: dict[str, tuple[float, float]] = {}
    for side, plist in by_side.items():
        for i, p in enumerate(plist):
            out[p.id] = port_local(p, i, len(plist), w, h)
    return out


def port_world(component: Component, port: CommPort, ports: list[CommPort] | None = None) -> tuple[float, float]:
    group = ports or [port]
    w, h = node_size(len(group))
    s = component.visual_scale()
    ov = component.pin_overrides.get(port.id)
    if ov is not None:
        lx, ly = ov.x * s, ov.y * s
    else:
        layout = port_layout(group, w, h)
        lx, ly = layout.get(port.id, (w, h / 2.0))
        lx, ly = lx * s, ly * s
    rx, ry = rotate_point(lx, ly, component.rotation)
    return (component.x + rx, component.y + ry)


def network_endpoint(sheet: Sheet, catalog: Catalog, ref: str) -> tuple[float, float]:
    from panelcad.render.sheet import pin_world

    cid, pid = parse_pin_ref(ref)
    comp = sheet.component(cid)
    device = catalog.get(comp.library_id)
    ports = effective_ports(device, comp)
    for p in ports:
        if p.id == pid:
            return port_world(comp, p, ports)
    try:
        return pin_world(comp, device.pin(pid))
    except KeyError:
        w, h = node_size(len(ports))
        return (comp.x + w, comp.y + h / 2.0)


def draw_network_node(painter: Painter, device: Device, component: Component, medium: str = "") -> None:
    ports = effective_ports(device, component)
    w, h = node_size(len(ports))
    s = component.visual_scale()
    w, h = w * s, h * s
    fill = _KIND_FILL.get(medium if medium != "mixed" else "", "#e3f2fd")
    painter.set_fill(fill)
    painter.set_stroke("#1b1b1b", 0.45)
    painter.rect(component.x, component.y, w, h)
    painter.set_fill(None)
    painter.text(component.x + w / 2.0, component.y + 8.0, component.id, 3.2, align="middle")
    name = str(component.properties.get("tag") or component.properties.get("catalog") or device.name)
    if len(name) > 18:
        name = name[:16] + "…"
    painter.text(component.x + w / 2.0, component.y + 16.0, name, 2.1, align="middle")
    addr = network_node_caption(component, ports)
    painter.text(
        component.x + w / 2.0,
        component.y + 25.0,
        addr or "TBD",
        2.2,
        align="middle",
    )
    layout = port_layout(ports, w, h)
    for p in ports:
        lx, ly = layout[p.id]
        px, py = component.x + lx, component.y + ly
        painter.set_fill(_KIND_FILL.get(p.kind, "#eceff1"))
        painter.circle(px, py, 2.0)
        painter.set_fill(None)
        label = (p.label or p.id)
        if p.side == "left":
            painter.text(px + 4, py + 1.2, label, 2.0, align="start")
        elif p.side == "right":
            painter.text(px - 4, py + 1.2, label, 2.0, align="end")
        else:
            painter.text(px, py - 3.5, label, 2.0, align="middle")


def draw_network_sheet(painter: Painter, sheet: Sheet, catalog: Catalog) -> None:
    medium = ""
    if sheet.network is not None:
        medium = sheet.network.medium
        title = sheet.network.name or sheet.title
        proto = sheet.network.protocol
        painter.text(20, 22, title + (f"  ·  {proto}" if proto else ""), 4.0, align="start")
    for comp in sheet.components:
        device = catalog.get(comp.library_id)
        draw_network_node(painter, device, comp, medium)
    color = _KIND_INK.get(medium, "#0b4ea2")
    painter.set_stroke(color, 0.55)
    painter.set_fill(None)
    from panelcad.render.painter import draw_wire_caption
    from panelcad.render.wire_labels import bundle_spec_plan, place_wire_marks
    from panelcad.render.wiring import route_sheet_wires

    try:
        routes = route_sheet_wires(sheet, catalog)
    except (KeyError, ValueError):
        routes = {}
    marks = place_wire_marks(sheet, catalog, routes, ladder=False)
    specs = bundle_spec_plan(sheet, routes, ladder=False)
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        if not pts:
            continue
        painter.polyline([(p.x, p.y) for p in pts])
        draw_wire_caption(
            painter, wire, pts, ladder=False, at=marks.get(wire.id), spec=specs.get(wire.id, "")
        )
