from __future__ import annotations

from panelcad.core.geometry import Point, rotate_point
from panelcad.core.models import Component, PinDef, Sheet, Wire
from panelcad.library.catalog import Catalog


def parse_pin_ref(ref: str) -> tuple[str, str]:
    if "." not in ref:
        raise ValueError(f"pin ref must be Component.Pin, got '{ref}'")
    comp, pin = ref.split(".", 1)
    return comp, pin


def named_pin_ids(component: Component) -> list[str]:
    """Pins a trace or a print named on this component, in a stable order."""

    props = getattr(component, "properties", None) or {}
    raw = f"{props.get('reconstruct_named_pins') or ''},{props.get('print_import_pins') or ''}"
    names: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if token and token not in names:
            names.append(token)
    return names


def resolve_pin(device: object, component: Component, pin_id: str) -> PinDef:
    """Library pin, or a stored name with a stable spot on the symbol.

    A measured envelope has no terminals. A trace can still name ``24V``.
    That name is drawn on the symbol edge instead of raising.
    """

    try:
        return device.pin(pin_id)
    except KeyError:
        pass
    names = named_pin_ids(component)
    if pin_id not in names:
        library_id = str(getattr(device, "library_id", "") or "")
        raise KeyError(f"{library_id} has no pin '{pin_id}'")
    index = names.index(pin_id)
    width = float(getattr(device, "schematic_width", 0) or 28)
    height = float(getattr(device, "schematic_height", 0) or 18)
    count = max(len(names), 1)
    span = max(height - 4.0, 4.0)
    y = 2.0 + span * (index + 0.5) / count
    return PinDef(id=pin_id, x=width, y=y, side="right", label=pin_id)


def pin_world(component: Component, pin: PinDef) -> tuple[float, float]:
    s = component.visual_scale()
    px, py = component.pin_xy(pin)
    x, y = rotate_point(px * s, py * s, component.rotation)
    if component.mirror:
        x = -x
    return (component.x + x, component.y + y)


def pin_side_world(component: Component, pin: PinDef) -> str:
    sides = ["left", "top", "right", "bottom"]
    order = {s: i for i, s in enumerate(sides)}
    idx = (order[component.pin_side(pin)] + (component.rotation // 90)) % 4
    if component.mirror and sides[idx] in ("left", "right"):
        idx = (idx + 2) % 4
    return sides[idx]


def iter_wire_points(sheet: Sheet, catalog: Catalog, wire: Wire, project=None) -> list[Point]:
    from panelcad.render.wiring import route_sheet_wires

    return route_sheet_wires(sheet, catalog, project=project)[wire.id]
