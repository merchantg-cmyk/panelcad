"""Elementary / PLC-style ladder: two vertical rails, horizontal rungs.

Rail names are not fixed. Common pairs: L1/L2, 120/N, 24VDC/0VDC.
Applies to schematic sheets with drawing_style=ladder only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from panelcad.core.geometry import Point
from panelcad.render.titleblock import working_rect

if TYPE_CHECKING:
    from panelcad.core.models import Project, Sheet

RAIL_INSET = 8.0


def parse_rail_pair(value: str | None) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for sep in ("/", ",", ";", "|"):
        if sep in raw:
            left, right = raw.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    parts = raw.split()
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None


def rail_names(sheet: Sheet, project: Project | None = None) -> tuple[str, str]:
    hot = str(getattr(sheet, "rail_hot", "") or "").strip()
    ret = str(getattr(sheet, "rail_return", "") or "").strip()
    if project is not None:
        tb = project.title_block
        hot = hot or str(getattr(tb, "rail_hot", "") or "").strip()
        ret = ret or str(getattr(tb, "rail_return", "") or "").strip()
    return hot or "L1", ret or "L2"


# Per-sheet styles that a title-block stamp must not overwrite.
SHEET_OWN_STYLES: frozenset[str] = frozenset({"ladder", "iec", "freeform"})


def is_three_phase_power_sheet(sheet: Sheet) -> bool:
    """True when ``sheet`` is a 3-phase power feeder schematic."""

    if getattr(sheet, "kind", "") != "schematic":
        return False
    from panelcad.convert.layout_convert import is_power_sheet

    return is_power_sheet(sheet)


def _rails_disabled(sheet: Sheet) -> bool:
    """True when ``sheet`` disables 2-rail single-phase/control ladder rails."""

    if is_three_phase_power_sheet(sheet):
        return True
    style = str(getattr(sheet, "drawing_style", "") or "").strip().lower()
    if style in {"freeform", "schematic", "power", "point_to_point"}:
        return True
    hot = str(getattr(sheet, "rail_hot", "") or "").strip().lower()
    ret = str(getattr(sheet, "rail_return", "") or "").strip().lower()
    if hot == "none" or ret == "none":
        return True
    return False


def stamp_drawing_style(project: Project) -> None:
    """Copy the title-block style onto schematic sheets so the router can see it."""

    style = str(getattr(project.title_block, "drawing_style", "") or "")
    for sheet in project.sheets:
        if sheet.kind != "schematic":
            continue
        if is_three_phase_power_sheet(sheet):
            old_hot = str(getattr(sheet, "rail_hot", "") or "").strip()
            old_ret = str(getattr(sheet, "rail_return", "") or "").strip()
            if str(getattr(sheet, "drawing_style", "") or "").strip().lower() in {"", "ladder", "iec", "schematic"}:
                sheet.drawing_style = "freeform"  # type: ignore[assignment]
            if not _wires_use_rail(sheet, old_hot, old_ret):
                sheet.rail_hot = ""
                sheet.rail_return = ""
            continue
        own = str(getattr(sheet, "drawing_style", "") or "")
        # A blank title block means "not chosen yet". It never erases a style
        # the sheet already has, or the next save drops that sheet's style.
        if not style and own:
            continue
        # A sheet that chose its own layout (ladder, IEC columns, freeform)
        # keeps it when the title block differs.
        if own in SHEET_OWN_STYLES and own != style:
            continue
        sheet.drawing_style = style  # type: ignore[assignment]


def sync_sheet_rails(project: Project) -> None:
    """Copy the title-block rails onto sheets that do not already name their own.

    A motor-control sheet wires to 24V/0V. Replacing those names with L1/L2
    left the wires pointing at a rail that is no longer on the sheet, and the
    page then drew with no wires. A sheet that already has rail names, and
    wires that use them, keeps those names. A rename that does happen rewrites
    the wire ends so nothing is left on the old name.
    """

    hot = str(getattr(project.title_block, "rail_hot", "") or "").strip() or "L1"
    ret = str(getattr(project.title_block, "rail_return", "") or "").strip() or "L2"
    for sheet in project.sheets:
        if sheet.kind != "schematic":
            continue
        if is_three_phase_power_sheet(sheet):
            old_hot = str(getattr(sheet, "rail_hot", "") or "").strip()
            old_ret = str(getattr(sheet, "rail_return", "") or "").strip()
            if not _wires_use_rail(sheet, old_hot, old_ret):
                sheet.rail_hot = ""
                sheet.rail_return = ""
            continue
        old_hot = str(getattr(sheet, "rail_hot", "") or "").strip()
        old_ret = str(getattr(sheet, "rail_return", "") or "").strip()
        # A control sheet that already names both rails (24V and 0V) keeps
        # them when the title block is L1/L2. A one-rail rename still follows
        # the title, so X1/X2 updates the wires in that same undo.
        if (
            old_hot
            and old_ret
            and old_hot != hot
            and old_ret != ret
            and _wires_use_rail(sheet, old_hot, old_ret)
        ):
            continue
        if old_hot and old_hot != hot:
            _rewrite_rail_end(sheet, old_hot, hot)
        if old_ret and old_ret != ret:
            _rewrite_rail_end(sheet, old_ret, ret)
        sheet.rail_hot = hot
        sheet.rail_return = ret
    stamp_drawing_style(project)


def _wires_use_rail(sheet: Sheet, hot: str, ret: str) -> bool:
    for wire in sheet.wires:
        for end in (wire.frm, wire.to):
            if is_rail_ref(end, hot, ret):
                return True
    return False


def _rewrite_rail_end(sheet: Sheet, old: str, new: str) -> None:
    for wire in sheet.wires:
        for attr in ("frm", "to"):
            end = str(getattr(wire, attr) or "")
            if is_rail_ref(end, old, old):
                setattr(wire, attr, new)


def rail_snapshot(project: Project) -> list[tuple[Any, str, str]]:
    """Sheet rail names before a rename. Schematic sheets only."""

    rows: list[tuple[Any, str, str]] = []
    for sheet in project.sheets:
        if sheet.kind != "schematic":
            continue
        hot, ret = rail_names(sheet, project)
        rows.append((sheet, hot, ret))
    return rows


def rewrite_rail_end(end: str, mapping: dict[str, str]) -> str:
    """Rename a bare rail endpoint. A device pin such as L1.X2 stays a pin."""

    text = str(end or "").strip()
    if not text or "." in text:
        return end
    new = mapping.get(text.upper())
    if not new or new.upper() == text.upper():
        return end
    return new


def rename_rail_endpoints(project: Project, old_names: list[tuple[Any, str, str]]) -> None:
    """Point wires at the rails' new names. One call, so one undo covers it.

    ``old_names`` is ``(sheet, old_hot, old_return)`` from before the rename.
    Names that swap (L1 becomes L2 while L2 becomes L1) do not clobber each other.
    """

    for sheet, old_hot, old_ret in old_names:
        new_hot = str(getattr(sheet, "rail_hot", "") or "").strip()
        new_ret = str(getattr(sheet, "rail_return", "") or "").strip()
        mapping: dict[str, str] = {}
        if old_hot and new_hot and old_hot.upper() != new_hot.upper():
            mapping[old_hot.upper()] = new_hot
        if old_ret and new_ret and old_ret.upper() != new_ret.upper():
            mapping[old_ret.upper()] = new_ret
        if not mapping:
            continue
        for wire in sheet.wires:
            wire.frm = rewrite_rail_end(wire.frm, mapping)
            wire.to = rewrite_rail_end(wire.to, mapping)


HOLD_DROP = 28.0


def is_aux_library(library_id: str) -> bool:
    lib = str(library_id or "").lower()
    return "aux_no" in lib or "aux_nc" in lib or ".aux" in lib


def is_lamp_library(library_id: str) -> bool:
    """Pilot / indicator — a load, not a series contact on the coil rung."""

    lib = str(library_id or "").lower()
    return "pilot" in lib or "pilot_light" in lib or ".light" in lib or lib.endswith("light")


def sheet_drawing_style(sheet: Sheet, project: Project | None = None) -> str:
    """Per-sheet style wins so Motor control can be a ladder next to schematic power."""

    if is_three_phase_power_sheet(sheet):
        return "freeform"
    own = str(getattr(sheet, "drawing_style", "") or "")
    if own.strip().lower() in {"point_to_point", "power"}:
        return "freeform"
    if own:
        return own
    if project is not None:
        return str(getattr(project.title_block, "drawing_style", "") or "")
    return ""


def is_iec_column_sheet(sheet: Sheet, project: Project | None = None) -> bool:
    """Vertical IEC columns: hot rail on top, return rail on the bottom."""

    if sheet.kind not in {"schematic", "io"}:
        return False
    return sheet_drawing_style(sheet, project) == "iec"


def is_ladder_sheet(sheet: Sheet, project: Project | None = None) -> bool:
    """True only for an elementary diagram. Rail names alone are not a ladder.

    Every titled schematic gets L1/L2 labels. Treating that as a ladder made
    power sheets use a rung elbow that cut through symbols.
    """

    if sheet.kind != "schematic":
        return False
    return sheet_drawing_style(sheet, project) == "ladder"


def _head(ref: str) -> str:
    raw = str(ref or "").strip()
    if "." in raw:
        return raw.split(".", 1)[0].strip()
    return raw


def is_rail_ref(ref: str, hot: str, ret: str) -> bool:
    """True when *ref* is exactly a rail name. ``L1.X2`` is a device pin, not L1."""

    token = str(ref or "").strip()
    if not token or "." in token:
        return False
    folded = token.upper()
    return folded == str(hot).strip().upper() or folded == str(ret).strip().upper()


def rail_side(ref: str, hot: str, ret: str) -> Literal["left", "right"]:
    token = _head(ref)
    if token.upper() == str(ret).upper():
        return "right"
    return "left"


def series_rung_ys(sheet: Sheet, catalog) -> list[float]:
    """Pin Y of rail-to-rail conductors: coil row, and a lamp row when a lamp is placed.

    Aux hold-in is not a conductor of its own — it sits on the lamp row, or as a
    U around Start when there is no lamp.
    """

    from panelcad.core.cleanup import _is_power_load, is_offpage
    from panelcad.render.sheet import pin_world

    ys: list[float] = []
    used: list[float] = []
    for comp in sheet.components:
        if (
            is_offpage(comp.library_id)
            or _is_power_load(comp.library_id)
            or is_aux_library(comp.library_id)
        ):
            continue
        device = catalog.get(comp.library_id)
        pins = [p for p in device.pins if p.side in ("left", "right")]
        if not pins:
            continue
        _x, y = pin_world(comp, pins[0])
        ys.append(y)
    for y in sorted(ys):
        if used and abs(y - used[-1]) < 6.0:
            continue
        used.append(y)
    return used


def on_series_rung(points: list[Point], series_ys: list[float], tol: float = 3.0) -> bool:
    if not points or not series_ys:
        return False
    return all(any(abs(p.y - y) < tol for y in series_ys) for p in points)


def ladder_route(ax: float, ay: float, bx: float, by: float) -> list[Point]:
    """Axis-aligned rung: through contacts, vertical only for a parallel branch."""

    if abs(ay - by) < 0.6:
        return [Point(x=ax, y=ay), Point(x=bx, y=by)]
    if abs(ax - bx) < 0.6:
        return [Point(x=ax, y=ay), Point(x=bx, y=by)]
    return [Point(x=ax, y=ay), Point(x=ax, y=by), Point(x=bx, y=by)]


def rail_xs(sheet: Sheet) -> tuple[float, float]:
    wx, _wy, ww, _wh = working_rect(sheet)
    return wx + RAIL_INSET, wx + ww - RAIL_INSET


def rail_point(
    sheet: Sheet, ref: str, other_y: float, hot: str, ret: str
) -> tuple[float, float, Literal["left", "right"]]:
    left, right = rail_xs(sheet)
    y = float(other_y)
    if rail_side(ref, hot, ret) == "left":
        return left, y, "right"
    return right, y, "left"


def iec_rail_ys(sheet: Sheet) -> tuple[float, float]:
    """Hot rail Y, return rail Y. Horizontal rails across the working area."""

    _wx, wy, _ww, wh = working_rect(sheet)
    return wy + RAIL_INSET, wy + wh - 6.0


def iec_rail_point(
    sheet: Sheet, ref: str, other_x: float, hot: str, ret: str
) -> tuple[float, float, Literal["top", "bottom"]]:
    """Land a rail drop on the device pin's X. Hot is the top rail."""

    top, bot = iec_rail_ys(sheet)
    x = float(other_x)
    if rail_side(ref, hot, ret) == "left":
        return x, top, "bottom"
    return x, bot, "top"


# A click this close to the other pin's rung (or IEC column) stays a straight run.
STRAIGHT_RAIL_MM = 3.0
# Perpendicular slop for hitting the drawn rail. Pin terminals use about 4 mm.
RAIL_HIT_MM = 4.0


class RailGeometry:
    """The two rails actually painted on this sheet. Names are the sheet's own."""

    __slots__ = ("kind", "hot", "ret", "left", "right", "top", "bottom")

    def __init__(
        self,
        kind: Literal["ladder", "iec"],
        hot: str,
        ret: str,
        left: float,
        right: float,
        top: float,
        bottom: float,
    ) -> None:
        self.kind = kind
        self.hot = hot
        self.ret = ret
        self.left = left
        self.right = right
        self.top = top
        self.bottom = bottom


class RailHit:
    """A point on a drawn rail. ``name`` is the rail the wire endpoint should use."""

    __slots__ = ("name", "x", "y")

    def __init__(self, name: str, x: float, y: float) -> None:
        self.name = name
        self.x = x
        self.y = y


def ladder_rail_ys(sheet: Sheet, project: Project | None = None) -> tuple[float, float]:
    """Top and bottom of the painted vertical rails.

    Schematic rails reach the last numbered rung when that rung sits below the
    old inset, so the bottom line of the sheet is still a wire end. I/O sheets
    keep the shorter inset that meets the card's return bars.
    """

    _wx, wy, _ww, wh = working_rect(sheet)
    top = wy + 10.0
    if sheet.kind == "io":
        return top, wy + wh - 2.0
    floor = wy + wh - 8.0
    return top, max(floor, _last_ladder_rung_y(sheet, project, wy + wh))


def _last_ladder_rung_y(sheet: Sheet, project: Project | None, limit: float) -> float:
    """Lowest rung whose line number is still inside the working area."""

    from panelcad.core.zones import rung_y

    y = rung_y(sheet, 1, project=project)
    index = 1
    while index < 99:
        nxt = rung_y(sheet, index + 1, project=project)
        if nxt > float(limit) + 1e-6:
            break
        index += 1
        y = nxt
    return y


def _sheet_names_its_rails(sheet: Sheet) -> bool:
    return bool(str(getattr(sheet, "rail_hot", "") or "").strip() or str(getattr(sheet, "rail_return", "") or "").strip())


def endpoint_is_rail(sheet: Sheet, ref: str, project: Project | None = None) -> bool:
    """A wire end is a rail when it is a bare rail name.

    ``L1.X2`` is device L1 pin X2. A bare ``L1`` stays the rail on a ladder
    sheet and on a power or freeform sheet, so a wire saved that way still
    draws and exports. A sheet that stores its own rail names uses those names.
    """

    hot, ret = rail_names(sheet, project)
    return is_rail_ref(ref, hot, ret)


def _shorts_are_refused(sheet: Sheet, project: Project | None) -> bool:
    """Hot-to-return is a short only where the sheet actually has those rails."""

    return rail_geometry(sheet, project) is not None or _sheet_names_its_rails(sheet)


def rail_short_message(
    sheet: Sheet, frm: str, to: str, project: Project | None = None
) -> str | None:
    """Plain refusal when a wire would join the hot rail to the return."""

    if not _shorts_are_refused(sheet, project):
        return None
    hot, ret = rail_names(sheet, project)
    if not (is_rail_ref(frm, hot, ret) and is_rail_ref(to, hot, ret)):
        return None
    if rail_side(frm, hot, ret) == rail_side(to, hot, ret):
        return None
    return f"{hot} to {ret} would short the rails — pick a device terminal"


def rail_geometry(sheet: Sheet, project: Project | None = None) -> RailGeometry | None:
    """Rails the sheet paints. Empty on a power schematic, a layout, or freeform."""

    if _rails_disabled(sheet):
        return None
    style = sheet_drawing_style(sheet, project)
    hot, ret = rail_names(sheet, project)
    if style == "ladder" and sheet.kind in {"schematic", "io"}:
        left, right = rail_xs(sheet)
        top, bottom = ladder_rail_ys(sheet, project)
        return RailGeometry("ladder", hot, ret, left, right, top, bottom)
    if style == "iec" and sheet.kind == "schematic":
        left, right = rail_xs(sheet)
        top, bottom = iec_rail_ys(sheet)
        return RailGeometry("iec", hot, ret, left, right, top, bottom)
    return None


def hit_rail(
    sheet: Sheet,
    x: float,
    y: float,
    project: Project | None = None,
    slop: float = RAIL_HIT_MM,
) -> RailHit | None:
    """Nearest point on a drawn rail, or None when the click misses both rails.

    Ladder rails are vertical (hot on the left). IEC column rails are horizontal
    (hot on the top). The returned point is on the rail segment.
    """

    geom = rail_geometry(sheet, project)
    if geom is None:
        return None
    best: RailHit | None = None
    best_d = float(slop)
    px, py = float(x), float(y)
    if geom.kind == "ladder":
        spans = ((geom.hot, geom.left), (geom.ret, geom.right))
        for name, rx in spans:
            if not name:
                continue
            dist = abs(px - rx)
            if dist > best_d + 1e-9:
                continue
            if py < geom.top - slop or py > geom.bottom + slop:
                continue
            cy = min(max(py, geom.top), geom.bottom)
            best = RailHit(name, rx, cy)
            best_d = dist
        return best
    spans_h = ((geom.hot, geom.top), (geom.ret, geom.bottom))
    for name, ry in spans_h:
        if not name:
            continue
        dist = abs(py - ry)
        if dist > best_d + 1e-9:
            continue
        if px < geom.left - slop or px > geom.right + slop:
            continue
        cx = min(max(px, geom.left), geom.right)
        best = RailHit(name, cx, ry)
        best_d = dist
    return best


def explicit_rail_landing(
    sheet: Sheet,
    ref: str,
    other: str,
    point: Point,
    *,
    project: Project | None = None,
    catalog=None,
) -> Point | None:
    """Sheet point where a rail end should land, or None for a straight run.

    A click within ``STRAIGHT_RAIL_MM`` of the other pin stays on that pin's
    rung (ladder) or column (IEC), which is how an existing rail wire is drawn.
    """

    if not endpoint_is_rail(sheet, ref, project):
        return None
    hot, ret = rail_names(sheet, project)
    geom = rail_geometry(sheet, project)
    x, y = float(point.x), float(point.y)
    iec = bool(geom is not None and geom.kind == "iec") or (
        geom is None and is_iec_column_sheet(sheet, project)
    )
    if geom is not None:
        if geom.kind == "ladder":
            x = geom.left if rail_side(ref, hot, ret) == "left" else geom.right
            y = min(max(y, geom.top), geom.bottom)
        else:
            y = geom.top if rail_side(ref, hot, ret) == "left" else geom.bottom
            x = min(max(x, geom.left), geom.right)
    if _near_other_pin(sheet, other, x, y, iec=iec, hot=hot, ret=ret, catalog=catalog):
        return None
    return Point(x=x, y=y)


def _near_other_pin(
    sheet: Sheet,
    other: str,
    x: float,
    y: float,
    *,
    iec: bool,
    hot: str,
    ret: str,
    catalog,
) -> bool:
    if is_rail_ref(other, hot, ret):
        return False
    if catalog is None or "." not in str(other):
        return False
    try:
        from panelcad.render.sheet import parse_pin_ref, pin_world

        cid, pid = parse_pin_ref(other)
        comp = sheet.component(cid)
        device = catalog.get(comp.library_id)
        px, py = pin_world(comp, device.pin(pid))
    except (KeyError, ValueError):
        return False
    if iec:
        return abs(float(x) - float(px)) <= STRAIGHT_RAIL_MM
    return abs(float(y) - float(py)) <= STRAIGHT_RAIL_MM


def rail_landing_points(
    sheet: Sheet,
    routes: dict,
    project: Project | None = None,
) -> list[tuple[float, float]]:
    """Where a wire meets a drawn rail. One point per distinct junction."""

    if rail_geometry(sheet, project) is None:
        return []
    found: list[tuple[float, float]] = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) if routes else None
        if not pts:
            continue
        ends = []
        if endpoint_is_rail(sheet, wire.frm, project):
            ends.append(pts[0])
        if endpoint_is_rail(sheet, wire.to, project):
            ends.append(pts[-1])
        for pt in ends:
            x, y = float(pt.x), float(pt.y)
            if any(abs(x - sx) <= 0.4 and abs(y - sy) <= 0.4 for sx, sy in found):
                continue
            found.append((x, y))
    return found
