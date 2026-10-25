"""Color the breaker line side, from the incoming supply down through the leads.

Pins 1/3/5 and L1/L2/L3 are unwired, so the phase trace starts at the pin and
the black symbol leads above it stay uncolored. These runs overpaint that
lead and add a short tail up to the supply. They are not wires.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import hypot

from panelcad.core.geometry import rotate_point
from panelcad.core.models import Component, Device, PinDef, Project, Sheet
from panelcad.core.phase_color import PhasePen, palette_for, pen_for_phase
from panelcad.library.catalog import Catalog

_SEED = {
    "L1": "L1",
    "L2": "L2",
    "L3": "L3",
    "1L1": "L1",
    "2L2": "L2",
    "3L3": "L3",
    "1": "L1",
    "3": "L2",
    "5": "L3",
}


@dataclass(frozen=True)
class SupplyRun:
    """One colored stroke that is not a wire. ``points`` are in sheet millimetres."""

    points: list[tuple[float, float]]
    pen: PhasePen
    ref: str = ""


@dataclass(frozen=True)
class SupplyCallout:
    """Incoming 3-phase supply callout (``L1``, ``L2``, ``L3``) at the top of a line-side lead."""

    text: str
    x: float
    y: float
    size: float
    align: str
    pen: PhasePen
    cap: tuple[tuple[float, float], tuple[float, float]] | None = None
    ref: str = ""


def line_side_runs(project: Project, sheet: Sheet, catalog: Catalog) -> list[SupplyRun]:
    """Unwired breaker (and contactor) line pins, colored from above the symbol."""

    if palette_for(project, sheet) is None or _control_sheet(sheet):
        return []
    wired = {wire.frm for wire in sheet.wires} | {wire.to for wire in sheet.wires}
    try:
        from panelcad.render.titleblock import working_rect

        _wx, wy, _ww, _wh = working_rect(sheet)
    except (KeyError, AttributeError, ValueError):
        wy = 15.0
    runs: list[SupplyRun] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        pin_labels = (comp.properties or {}).get("pin_labels") or {}
        pin_ids = {str(pin.id) for pin in device.pins} | {str(v) for v in pin_labels.values()}
        if not (
            {"1", "3", "5"} <= pin_ids
            or {"L1", "L2", "L3"} <= pin_ids
            or {"1L1", "2L2", "3L3"} <= pin_ids
        ):
            continue
        overrides = getattr(comp, "pin_overrides", None) or {}
        for pin in device.pins:
            phase = _SEED.get(str(pin.id)) or _SEED.get(str(pin_labels.get(pin.id, "")))
            if phase is None:
                continue
            ref = f"{comp.id}.{pin.id}"
            if ref in wired or any(
                f"{comp.id}.{ov_id}" in wired
                and abs(float(pin.x) - float(ov_pt.x)) < 0.2
                and abs(float(pin.y) - float(ov_pt.y)) < 0.2
                for ov_id, ov_pt in overrides.items()
            ):
                continue
            pen = pen_for_phase(project, sheet, phase)
            if pen is None:
                continue
            points = _lead_polyline(comp, device, pin, wy)
            if len(points) >= 2:
                runs.append(SupplyRun(points=points, pen=pen, ref=ref))
    return runs


def pole_runs(project: Project, sheet: Sheet, catalog: Catalog) -> list[SupplyRun]:
    """Color each pole's own leads, blades, and heaters.

    The phase trace already follows 1-2 / L1-T1 / U1-U2 through the net.
    The symbol still draws those conductors in black. These runs overpaint
    that ink. The mechanical trip bar, the coil, and auxiliary contacts stay
    black. A reversed pole keeps the phase that landed on it. A star-point
    load pin is neutral.
    """

    if palette_for(project, sheet) is None or _control_sheet(sheet):
        return []
    from panelcad.core.phase_color import pin_phase_map

    try:
        phases = pin_phase_map(sheet, catalog)
    except (KeyError, ValueError, AttributeError):
        return []
    if not phases:
        return []
    runs: list[SupplyRun] = []
    for comp in sheet.components:
        try:
            device = catalog.get(comp.library_id)
        except KeyError:
            continue
        runs.extend(_device_pole_runs(project, sheet, comp, device, phases))
    return runs


def _control_sheet(sheet: Sheet) -> bool:
    if sheet.kind not in {"schematic", "io"}:
        return True
    if sheet.kind == "io":
        return True
    from panelcad.core.ladder import is_three_phase_power_sheet

    if is_three_phase_power_sheet(sheet):
        return False
    style = str(getattr(sheet, "drawing_style", "") or "")
    return style in {"ladder", "iec"}


def _lead_depth(device: Device, pin: PinDef) -> float:
    """How far the symbol's own lead runs down from this pin, in local mm."""

    px, py = float(pin.x), float(pin.y)
    best = 0.0
    for prim in getattr(device, "primitives", []) or []:
        if getattr(prim, "type", "") != "line":
            continue
        if abs(float(prim.x) - float(prim.x2)) > 0.45:
            continue
        ends = ((float(prim.x), float(prim.y)), (float(prim.x2), float(prim.y2)))
        if not any(abs(x - px) <= 0.45 and abs(y - py) <= 0.45 for x, y in ends):
            continue
        other_y = ends[1][1] if abs(ends[0][1] - py) <= 0.45 else ends[0][1]
        best = max(best, other_y - py)
    return best


def _world(comp: Component, lx: float, ly: float) -> tuple[float, float]:
    scale = comp.visual_scale()
    rx, ry = rotate_point(lx * scale, ly * scale, int(comp.rotation or 0))
    if comp.mirror:
        rx = -rx
    return (comp.x + rx, comp.y + ry)


def _device_pole_runs(
    project: Project,
    sheet: Sheet,
    comp: Component,
    device: Device,
    phases: dict[str, str],
) -> list[SupplyRun]:
    from panelcad.core.phase_color import pen_for_phase

    phased = [pin for pin in device.pins if f"{comp.id}.{pin.id}" in phases]
    if not phased:
        return []
    quiet = [pin for pin in device.pins if f"{comp.id}.{pin.id}" not in phases]
    graphics = [prim for prim in (getattr(device, "primitives", []) or []) if _graphic_points(prim)]
    xs = sorted({round(float(pin.x), 2) for pin in phased})
    pitch = min((b - a for a, b in zip(xs, xs[1:]) if b - a > 0.8), default=10.0)
    # A bar that lands on a terminal is that pole's lead. The mechanism bar
    # ties the poles together and does not end on a pin.
    mechanism = {
        i
        for i, prim in enumerate(graphics)
        if _is_trip_bar(_graphic_points(prim), xs, pitch)
        and not _touches_pin(_graphic_points(prim), phased)
    }
    owned: dict[int, str] = {}
    for index, prim in enumerate(graphics):
        if index in mechanism or _touches_pin(_graphic_points(prim), quiet):
            continue
        points = _graphic_points(prim)
        for pin in phased:
            if any(_near(pt, (float(pin.x), float(pin.y)), 0.9) for pt in (points[0], points[-1])):
                owned[index] = pin.id
                break
    for _ in range(10):
        grew = False
        for index, prim in enumerate(graphics):
            if index in owned or index in mechanism or _touches_pin(_graphic_points(prim), quiet):
                continue
            points = _graphic_points(prim)
            for other, pin_id in owned.items():
                if _graphics_touch(points, _graphic_points(graphics[other])):
                    owned[index] = pin_id
                    grew = True
                    break
        if not grew:
            break
    runs: list[SupplyRun] = []
    for index, pin_id in owned.items():
        phase = phases.get(f"{comp.id}.{pin_id}")
        if not phase:
            continue
        pen = pen_for_phase(project, sheet, phase)
        if pen is None:
            continue
        world = [_world(comp, x, y) for x, y in _graphic_points(graphics[index])]
        if len(world) >= 2:
            runs.append(SupplyRun(points=world, pen=pen, ref=f"{comp.id}.{pin_id}"))
    return runs


def _graphic_points(prim: object) -> list[tuple[float, float]]:
    kind = getattr(prim, "type", "")
    if kind == "line":
        return [(float(prim.x), float(prim.y)), (float(prim.x2), float(prim.y2))]
    if kind == "polyline":
        return [(float(x), float(y)) for x, y in (getattr(prim, "points", None) or [])]
    if kind == "arc":
        from panelcad.core.primitives import arc_polyline

        return arc_polyline(
            float(prim.x),
            float(prim.y),
            float(prim.r),
            float(getattr(prim, "start", 0.0)),
            float(getattr(prim, "sweep", 0.0)),
        )
    if kind == "circle" and float(getattr(prim, "r", 99.0)) <= 1.5:
        from panelcad.core.primitives import arc_polyline

        return arc_polyline(
            float(prim.x),
            float(prim.y),
            float(prim.r),
            90.0,
            360.0,
        )
    return []


def _near(a: tuple[float, float], b: tuple[float, float], tol: float) -> bool:
    return hypot(a[0] - b[0], a[1] - b[1]) <= tol


def _touches_pin(points: list[tuple[float, float]], pins: list[PinDef]) -> bool:
    if len(points) < 2:
        return False
    for pin in pins:
        at = (float(pin.x), float(pin.y))
        if _near(points[0], at, 1.6) or _near(points[-1], at, 1.6):
            return True
    return False


def _anchor_points(pts: list[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    if len(pts) < 2:
        return tuple(pts)
    if len(pts) == 2:
        mid = ((pts[0][0] + pts[1][0]) * 0.5, (pts[0][1] + pts[1][1]) * 0.5)
        return (pts[0], pts[1], mid)
    return (pts[0], pts[-1], pts[len(pts) // 2])


def _graphics_touch(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    for left in _anchor_points(a):
        for right in _anchor_points(b):
            if _near(left, right, 0.75):
                return True
    return False


def _is_trip_bar(points: list[tuple[float, float]], columns: list[float], pitch: float) -> bool:
    """A flat bar that ties two pole columns together is the mechanism, not a phase."""

    if len(points) < 2 or len(columns) < 2:
        return False
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    if span_x < pitch * 0.7 or span_y > span_x * 0.4:
        return False
    covered = [col for col in columns if min(xs) - 1.2 <= col <= max(xs) + 1.2]
    return len(covered) >= 2


def _lead_polyline(comp: Component, device: Device, pin: PinDef, wy: float) -> list[tuple[float, float]]:
    px, py = comp.pin_xy(pin)
    depth = _lead_depth(device, pin)
    pin_w = _world(comp, px, py)
    if depth > 1.0:
        lead_w = _world(comp, px, py + max(0.0, depth - 0.7))
    else:
        lead_w = pin_w
    dx, dy = pin_w[0] - lead_w[0], pin_w[1] - lead_w[1]
    span = hypot(dx, dy)
    if span < 0.2:
        ux, uy = 0.0, -1.0
    else:
        ux, uy = dx / span, dy / span
    if uy < -0.5:
        # Stay inside the drawing frame. A tail that crosses wy lands in the
        # column-number strip; when the breaker has headroom, reserve space
        # above the tail cap for the L1/L2/L3 supply callout.
        if pin_w[1] >= wy + 6.0:
            room = pin_w[1] - (wy + 3.8)
            tail = min(6.0, max(2.2, room))
        else:
            room = pin_w[1] - (wy + 0.55)
            tail = min(6.0, max(0.0, room))
    else:
        tail = 4.0
    tail_w = (pin_w[0] + ux * tail, pin_w[1] + uy * tail)
    points = [tail_w, pin_w]
    if span >= 0.4:
        points.append(lead_w)
    return points


def line_side_callouts(project: Project, sheet: Sheet, catalog: Catalog) -> list[SupplyCallout]:
    """Supply callout labels (``L1``, ``L2``, ``L3``) and terminal caps for unwired line-side leads."""

    runs = line_side_runs(project, sheet, catalog)
    if not runs:
        return []
    try:
        from panelcad.render.titleblock import working_rect

        _wx, wy, _ww, _wh = working_rect(sheet)
    except (KeyError, AttributeError, ValueError):
        wy = 15.0
    callouts: list[SupplyCallout] = []
    for run in runs:
        if len(run.points) < 2:
            continue
        tx, ty = run.points[0]
        _px, py = run.points[1]
        phase = str(getattr(run.pen, "phase", "") or "").strip()
        if not phase:
            continue
        if ty < py - 0.5:
            if ty >= wy + 3.4:
                callouts.append(
                    SupplyCallout(
                        text=phase,
                        x=tx,
                        y=ty - 1.15,
                        size=2.5,
                        align="middle",
                        pen=run.pen,
                        cap=((tx - 1.8, ty), (tx + 1.8, ty)),
                        ref=run.ref,
                    )
                )
            else:
                callouts.append(
                    SupplyCallout(
                        text=phase,
                        x=tx - 0.85,
                        y=max(wy + 2.1, ty + 1.55),
                        size=1.9,
                        align="end",
                        pen=run.pen,
                        cap=None,
                        ref=run.ref,
                    )
                )
    return callouts
