from __future__ import annotations

from panelcad.core.geometry import Point
from panelcad.core.models import Sheet, Wire
from panelcad.core.pin_geometry import (
    named_pin_ids,
    parse_pin_ref,
    pin_side_world,
    pin_world,
    resolve_pin,
)
from panelcad.library.catalog import Catalog


def iter_wire_points(sheet: Sheet, catalog: Catalog, wire: Wire, project=None) -> list[Point]:
    from panelcad.render.wiring import route_sheet_wires

    return route_sheet_wires(sheet, catalog, project=project)[wire.id]


__all__ = [
    "iter_wire_points",
    "named_pin_ids",
    "parse_pin_ref",
    "pin_side_world",
    "pin_world",
    "resolve_pin",
]
