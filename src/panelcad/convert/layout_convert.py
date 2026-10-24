"""Move a ladder sheet onto IEC columns, or a column sheet onto a NEMA ladder.

Power schematics are not moved. Control and PLC I/O sheets that are already
``ladder`` or ``iec`` are placed with the Circuit Builder layout
(``layout_rungs`` / ``layout_iec_control`` / ``layout_plc_io``).
"""

from __future__ import annotations

from typing import Any

from panelcad.convert.table import nema_alias_for, rail_pair_for, target_standard
from panelcad.core.circuit_builder import _Part
from panelcad.core.refs import sheet_drawing_standard

_AC_HOT = {"L1", "X1", "H1", "120", "120V", "120VAC"}
_AC_RET = {"L2", "N", "X2", "H2", "NEUTRAL"}
# Spellings already used for one rail. 24V and +24V are the same NEMA hot.
_HOT_WORDS = {
    "24V": ("24V", "+24V", "24VDC", "+24VDC"),
    "+24V": ("24V", "+24V", "24VDC", "+24VDC"),
    "L+": ("L+",),
    "X1": ("X1",),
    "L1": ("L1",),
}
_RET_WORDS = {
    "0V": ("0V", "0VDC"),
    "M": ("M",),
    "X2": ("X2",),
    "N": ("N",),
    "L2": ("L2",),
}
_IO_TITLES = ("i/o", "plc i")


_POWER_LIB_TOKENS = (
    "motors.",
    "loads.motor",
    "breakers.mcb_3p",
    "breakers.mccb",
    "contactors.ac3",
    "contactors.three_pole",
    "contactors.power",
    "protection.overload_3p",
    "protection.overload_9a",
    "protection.overload",
    "protection.thermal_ol",
    "drives.",
    "starters.",
    "disconnect",
)
_POWER_ROLES = frozenset(
    {
        "motor",
        "contactor",
        "overload",
        "breaker",
        "drive",
        "softstarter",
        "star",
        "delta",
        "main_contactor",
        "fwd",
        "rev",
        "bypass",
    }
)


def is_power_sheet(sheet: Any, parts: list[_Part] | None = None) -> bool:
    """True when a schematic sheet contains 3-phase power feeder components."""

    roles = {part.role for part in (parts or []) if getattr(part, "role", None)}
    if roles & _POWER_ROLES:
        return True
    for comp in getattr(sheet, "components", []) or []:
        role = str(comp.properties.get("role") or "").strip().lower()
        if role in _POWER_ROLES:
            return True
        lid = str(getattr(comp, "library_id", "") or "").lower()
        if lid.endswith((".ol_contact", ".aux_no", ".aux_nc", ".coil")):
            continue
        if any(tok in lid for tok in _POWER_LIB_TOKENS):
            return True
    return False


def needs_column_or_ladder(sheet: Any, parts: list[_Part] | None = None) -> bool:
    """Control ladders and Circuit Builder I/O sheets change layout. Power schematics do not.

    A sheet titled "PLC I/O" with no Circuit Builder roles (the demo I/O page)
    keeps its positions. Only ``drawing_style`` ladder/iec, or an I/O sheet that
    actually has PLC rungs, is placed again.
    """

    if is_power_sheet(sheet, parts):
        return False
    if str(getattr(sheet, "drawing_style", "") or "") in {"ladder", "iec"}:
        return True
    roles = {part.role for part in (parts or [])}
    if str(getattr(sheet, "kind", "") or "") == "io" and roles.intersection(
        {"plc", "relay", "run_fb", "ol_sense", "hoa_auto_fb"}
    ):
        return True
    return False


def rail_kind(sheet: Any, parts: list[_Part]) -> str:
    title = str(getattr(sheet, "title", "") or "").lower()
    roles = {part.role for part in parts}
    if str(getattr(sheet, "kind", "") or "") == "io" or any(token in title for token in _IO_TITLES):
        return "io"
    if "plc" in roles and "relay" in roles and "start" not in roles and "starter_coil" not in roles:
        return "io"
    hot = str(getattr(sheet, "rail_hot", "") or "").strip()
    ret = str(getattr(sheet, "rail_return", "") or "").strip()
    if hot in _AC_HOT or ret in _AC_RET:
        return "ac"
    return "dc"


def _one(parts: list[_Part], role: str) -> _Part | None:
    found = [part for part in parts if part.role == role]
    return found[0] if found else None


def _keep(parts: list[_Part | None]) -> list[_Part]:
    return [part for part in parts if part is not None]


def _selector_auto(session: Any, part: _Part) -> str:
    device = session.catalog.get(part.library_id)
    rights = [pin for pin in device.pins if pin.side == "right"]
    if not rights:
        return ""
    return str(max(rights, key=lambda pin: pin.y).id)


def _plc_pin(session: Any, sheet: Any, part: _Part, plc: _Part) -> str:
    """PLC address this contact lands on, read from the existing wire."""

    prefix = f"{part.ref}."
    plc_prefix = f"{plc.ref}."
    for wire in sheet.wires:
        ends = (str(wire.frm), str(wire.to))
        if any(end.startswith(prefix) for end in ends) and any(end.startswith(plc_prefix) for end in ends):
            for end in ends:
                if end.startswith(plc_prefix):
                    return end.split(".", 1)[1]
    return ""


def rungs_for(session: Any, sheet: Any, parts: list[_Part]) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Rebuild the rung list Circuit Builder would have passed to the layout."""

    extra: dict[str, Any] = {}
    plc = _one(parts, "plc")
    relay = _one(parts, "relay")
    if plc is not None and relay is not None and _one(parts, "start") is None and _one(parts, "starter_coil") is None:
        inputs: list[tuple[_Part, str]] = []
        for role, default in (("run_fb", "I0_0"), ("ol_sense", "I0_1"), ("hoa_auto_fb", "I0_2")):
            part = _one(parts, role)
            if part is None:
                continue
            pin = _plc_pin(session, sheet, part, plc) or default
            inputs.append((part, pin))
        out = "Q0_0"
        for wire in sheet.wires:
            ends = (str(wire.frm), str(wire.to))
            if any(end.startswith(f"{relay.ref}.") for end in ends):
                for end in ends:
                    if end.startswith(f"{plc.ref}."):
                        out = end.split(".", 1)[1]
        extra["_plc"] = plc
        extra["_relay"] = relay
        extra["_io_inputs"] = inputs
        extra["io_out"] = out
        rungs = []
        run_fb = _one(parts, "run_fb")
        ol_sense = _one(parts, "ol_sense")
        if run_fb is not None:
            rungs.append({"contacts": [run_fb], "output": relay, "output_pin": "A1"})
        if ol_sense is not None:
            rungs.append({"contacts": [ol_sense], "output": relay, "output_pin": "A1"})
        return rungs, "_io_rungs", extra

    estop = _one(parts, "estop")
    stop = _one(parts, "stop")
    start = _one(parts, "start")
    fwd_coil = _one(parts, "fwd_coil")
    rev_coil = _one(parts, "rev_coil")
    if fwd_coil is not None and rev_coil is not None:
        olc = _one(parts, "ol_contact")
        rungs = [
            {
                "contacts": _keep([estop, stop, olc, _one(parts, "fwd_pb"), _one(parts, "rev_ilock")]),
                "output": fwd_coil,
                "seal": _one(parts, "fwd_seal"),
                "seal_under": _one(parts, "fwd_pb"),
                "out_net": "FWD",
            },
            {
                "contacts": _keep([estop, stop, olc, _one(parts, "rev_pb"), _one(parts, "fwd_ilock")]),
                "output": rev_coil,
                "seal": _one(parts, "rev_seal"),
                "seal_under": _one(parts, "rev_pb"),
                "tap": olc,
                "out_net": "REV",
            },
        ]
        if _one(parts, "pilot_fwd") is not None:
            rungs.append(
                {
                    "contacts": _keep([_one(parts, "fwd_run_aux")]),
                    "output": _one(parts, "pilot_fwd"),
                    "output_pin": "X1",
                    "out_net": "LT_FWD",
                }
            )
        if _one(parts, "pilot_rev") is not None:
            rungs.append(
                {
                    "contacts": _keep([_one(parts, "rev_run_aux")]),
                    "output": _one(parts, "pilot_rev"),
                    "output_pin": "X1",
                    "out_net": "LT_REV",
                }
            )
        return rungs, "_rungs", extra

    star_coil = _one(parts, "star_coil")
    delta_coil = _one(parts, "delta_coil")
    if star_coil is not None and delta_coil is not None:
        # Circuit Builder role is main_aux_tr (the K-14 contact that feeds the timer).
        main_tr = _one(parts, "main_aux_tr")
        rungs = [
            {
                "contacts": _keep([estop, stop, _one(parts, "ol_contact"), start]),
                "output": _one(parts, "main_coil"),
                "seal": _one(parts, "main_seal"),
                "seal_under": start,
                "out_net": "RUN",
            },
            {"contacts": _keep([main_tr]), "output": _one(parts, "timer"), "out_net": "TR"},
            {
                "contacts": _keep([main_tr, _one(parts, "tr_nc"), _one(parts, "delta_ilock")]),
                "output": star_coil,
                "tap": main_tr,
                "out_net": "STAR",
            },
            {
                "contacts": _keep([main_tr, _one(parts, "tr_no"), _one(parts, "star_ilock")]),
                "output": delta_coil,
                "tap": main_tr,
                "out_net": "DELTA",
            },
        ]
        if _one(parts, "pilot_run") is not None:
            rungs.append(
                {
                    "contacts": _keep([_one(parts, "main_run_aux")]),
                    "output": _one(parts, "pilot_run"),
                    "output_pin": "X1",
                    "out_net": "LT_RUN",
                }
            )
        return rungs, "_rungs", extra

    ss_coil = _one(parts, "ss_coil")
    if ss_coil is not None:
        rungs = []
        if _one(parts, "selector") is not None and start is None:
            rungs.append({"contacts": _keep([estop, _one(parts, "selector")]), "output": ss_coil})
        else:
            rungs.append(
                {
                    "contacts": _keep([estop, stop, start]),
                    "output": ss_coil,
                    "seal": _one(parts, "ss_seal"),
                    "seal_under": start,
                }
            )
        if _one(parts, "bypass_coil") is not None:
            rungs.append({"contacts": _keep([_one(parts, "ss_run_aux")]), "output": _one(parts, "bypass_coil")})
        if _one(parts, "pilot_run") is not None:
            rungs.append(
                {
                    "contacts": _keep([_one(parts, "ss_run_lt")]),
                    "output": _one(parts, "pilot_run"),
                    "output_pin": "X1",
                    "out_net": "LT_RUN",
                }
            )
        if _one(parts, "pilot_fault") is not None:
            rungs.append(
                {
                    "contacts": _keep([_one(parts, "ss_fault")]),
                    "output": _one(parts, "pilot_fault"),
                    "output_pin": "X1",
                    "out_net": "LT_FLT",
                }
            )
        return rungs, "_rungs", extra

    coil = _one(parts, "starter_coil")
    if coil is not None or _one(parts, "hoa") is not None or _one(parts, "cr_no") is not None:
        hoa = _one(parts, "hoa")
        olc = _one(parts, "ol_contact")
        hand = _keep([estop, stop, olc, hoa, start])
        rungs = []
        if coil is not None:
            rungs.append(
                {
                    "contacts": hand,
                    "output": coil,
                    "seal": _one(parts, "run_seal"),
                    "seal_under": start,
                    "out_net": "HAND",
                }
            )
        else:
            rungs.append({"contacts": hand, "out_net": "HAND", "sink_role": "drive"})
        auto_contacts = _keep([hoa, _one(parts, "cr_no")])
        auto: dict[str, Any] = {"contacts": auto_contacts, "out_net": "AUTO"}
        if hoa is not None:
            auto["tap"] = hoa
            auto["tap_out"] = _selector_auto(session, hoa)
        if coil is not None:
            auto["output"] = coil
            auto["join"] = start
        else:
            auto["sink_role"] = "drive"
        rungs.append(auto)
        return rungs, "_rungs", extra

    plain_coil = _one(parts, "coil")
    if plain_coil is not None:
        # A plain coil rung (DOL and hand-drawn starters). The contact order is
        # read from the wires, so the relayout keeps the netlist it was given.
        seal = _one(parts, "seal")
        contacts = wired_chain(sheet, parts, plain_coil, "A1", skip=seal)
        rung: dict[str, Any] = {"contacts": contacts, "output": plain_coil, "out_net": "RUN"}
        if seal is not None:
            under = _parallel_to(sheet, seal, contacts)
            if under is not None:
                rung["seal"] = seal
                rung["seal_under"] = under
        return [rung], "_rungs", extra

    chain = _keep([estop, stop, start, _one(parts, "selector")])
    if chain:
        return [{"contacts": chain, "output": None}], "_rungs", extra
    return [], "_rungs", extra


def _split_end(end: str) -> tuple[str, str]:
    text = str(end or "")
    if "." not in text:
        return text, ""
    ref, pin = text.rsplit(".", 1)
    return ref, pin


def _pin_graph(sheet: Any) -> dict[tuple[str, str], set[tuple[str, str]]]:
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for wire in sheet.wires:
        a = _split_end(wire.frm)
        b = _split_end(wire.to)
        if not a[1] or not b[1]:
            continue
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)
    # Pins on one net are joined through each other (a seal-in shares both nets).
    changed = True
    while changed:
        changed = False
        for node, near in list(graph.items()):
            grown = set(near)
            for other in near:
                grown |= graph.get(other, set())
            grown.discard(node)
            if grown != near:
                graph[node] = grown
                changed = True
    return graph


def wired_chain(
    sheet: Any, parts: list[_Part], output: _Part, output_pin: str, *, skip: _Part | None = None
) -> list[_Part]:
    """Series contacts feeding ``output``, hot side first, walked along the drawn wires."""

    refs = {part.ref: part for part in parts}
    graph = _pin_graph(sheet)
    pins_of: dict[str, set[str]] = {}
    for ref, pin in graph:
        pins_of.setdefault(ref, set()).add(pin)
    seen = {output.ref}
    if skip is not None:
        seen.add(skip.ref)
    chain: list[_Part] = []
    cur = (output.ref, output_pin)
    while True:
        nxt = sorted(
            node for node in graph.get(cur, set()) if node[0] in refs and node[0] not in seen
        )
        if len(nxt) != 1:
            break
        ref, pin = nxt[0]
        chain.insert(0, refs[ref])
        seen.add(ref)
        others = pins_of.get(ref, set()) - {pin}
        if len(others) != 1:
            break
        cur = (ref, next(iter(others)))
    return chain


def _parallel_to(sheet: Any, seal: _Part, contacts: list[_Part]) -> _Part | None:
    graph = _pin_graph(sheet)
    seal_nets = [
        {node[0] for node in near} for (ref, _pin), near in graph.items() if ref == seal.ref
    ]
    for part in contacts:
        if len(seal_nets) >= 2 and all(part.ref in net for net in seal_nets):
            return part
    return None


def unplaced_wired(sheet: Any, parts: list[_Part], rungs: list[dict[str, Any]]) -> list[str]:
    """Role parts that share a wire with the rungs but are not in any rung."""

    used: set[str] = set()
    for rung in rungs:
        for part in rung.get("contacts") or []:
            used.add(part.ref)
        for key in ("output", "seal", "seal_under", "tap", "join"):
            part = rung.get(key)
            if part is not None:
                used.add(part.ref)
    if not used:
        return []
    graph = _pin_graph(sheet)
    left: list[str] = []
    for part in parts:
        if part.ref in used:
            continue
        near = {node[0] for (ref, _pin), nodes in graph.items() if ref == part.ref for node in nodes}
        if near & used:
            left.append(part.ref)
    return left


def parts_on_sheet(session: Any, sheet: Any) -> list[_Part]:
    parts: list[_Part] = []
    for comp in sheet.components:
        role = str(comp.properties.get("role") or "")
        if not role or role in {"to", "from"}:
            continue
        parts.append(
            _Part(
                library_id=comp.library_id,
                role=role,
                x=float(comp.x),
                y=float(comp.y),
                ref=comp.id,
                scale=float(comp.visual_scale()),
                rotation=int(comp.rotation or 0),
                properties=dict(comp.properties),
                page="io" if rail_kind(sheet, []) == "io" else "control",
            )
        )
    return parts


def _write_back(sheet: Any, parts: list[_Part]) -> None:
    by_ref = {comp.id: comp for comp in sheet.components}
    keep = (
        "ladder_card",
        "card_h",
        "pin_labels",
        "tag_below",
        "ladder_coil",
        "hidden_pins",
        "ref_side",
        "ref_dx",
        "ref_dy",
    )
    for part in parts:
        comp = by_ref.get(part.ref)
        if comp is None:
            continue
        comp.x = float(part.x)
        comp.y = float(part.y)
        comp.rotation = int(part.rotation or 0)  # type: ignore[assignment]
        comp.scale = float(part.scale or 1.0)
        for key in keep:
            if key in part.properties:
                comp.properties[key] = part.properties[key]


def _clear_points(sheet: Any) -> None:
    for wire in sheet.wires:
        wire.points = []


def _sheet_text_boxes(
    session: Any, sheet: Any
) -> list[tuple[str, str, str, tuple[float, float, float, float]]]:
    """Tags, terminal names, and wire numbers the geometry check refuses to cross.

    The boxes match ``_assert_wires_clear_text`` so a route that survives here
    survives that check.
    """

    from panelcad.core.geometry import rotate_point
    from panelcad.core.ink import device_ref_layout, pin_label_anchor, text_box
    from panelcad.core.ladder import is_iec_column_sheet, is_ladder_sheet
    from panelcad.core.pins import pin_caption
    from panelcad.render.sheet import pin_side_world, pin_world
    from panelcad.render.wire_labels import mark_box, place_wire_marks
    from panelcad.render.wiring import route_sheet_wires

    column = is_iec_column_sheet(sheet)
    ladder_only = is_ladder_sheet(sheet)
    ladder = ladder_only or column
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    marks = place_wire_marks(sheet, session.catalog, routes, ladder=ladder, project=session.project)
    texts: list[tuple[str, str, str, tuple[float, float, float, float]]] = []

    def _world(comp: Any, lx: float, ly: float) -> tuple[float, float]:
        scale = comp.visual_scale()
        rx, ry = rotate_point(lx * scale, ly * scale, int(comp.rotation or 0))
        if comp.mirror:
            rx = -rx
        return (comp.x + rx, comp.y + ry)

    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        scale = comp.visual_scale()
        for prim in device.primitives:
            if prim.type != "text" or not str(prim.text).strip():
                continue
            wx, wy = _world(comp, prim.x, prim.y)
            box = text_box(wx, wy, str(prim.text), max(1.2, prim.size * scale), "middle")
            if box is not None:
                texts.append(("prim", comp.id, str(prim.text), box))
        layout = device_ref_layout(device, comp, ref_align="middle" if ladder else "start")
        if layout is not None:
            tx, ty, talign, size = layout
            tag = text_box(tx, ty, comp.id, size, talign)
            if tag is not None:
                texts.append(("tag", comp.id, comp.id, tag))
        if not ladder:
            continue
        drawn = {
            str(prim.text).strip()
            for prim in device.primitives
            if prim.type == "text" and prim.text
        }
        iec_numbers = column or str(getattr(sheet, "standard", "") or "") == "IEC"
        for pin in device.pins:
            cap = pin_caption(pin, comp)
            override = bool(cap) and cap != pin.id
            show_id = iec_numbers and cap == pin.id and cap not in drawn
            if ladder_only and not override:
                continue
            if not cap or (cap == pin.id and not show_id):
                continue
            wx, wy = pin_world(comp, pin)
            side = pin_side_world(comp, pin)
            size = 3.2 if ladder_only else 1.9
            tx, ty, align = pin_label_anchor(
                wx, wy, side, ladder=ladder_only, column=column, caption=cap
            )
            box = text_box(tx, ty, cap, size, align)
            if box is not None:
                texts.append(("pin", comp.id, cap, box))
    for wire in sheet.wires:
        at = marks.get(wire.id)
        mark = wire.drawn_mark(ladder=ladder)
        if at and mark:
            texts.append(
                (
                    "mark",
                    wire.id,
                    mark,
                    mark_box(at[0], at[1], mark, align=at[2], spec=wire.spec_text()),
                )
            )
    return texts


def _segment_inside(a: Any, b: Any, rect: tuple[float, float, float, float]) -> float:
    from math import hypot

    x0, y0, x1, y1 = rect
    dx, dy = b.x - a.x, b.y - a.y
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a.x - x0), (dx, x1 - a.x), (-dy, a.y - y0), (dy, y1 - a.y)):
        if abs(p) < 1e-9:
            if q < 0:
                return 0.0
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return 0.0
            t0 = max(t0, t)
        else:
            if t < t0:
                return 0.0
            t1 = min(t1, t)
    if t1 <= t0:
        return 0.0
    return (t1 - t0) * hypot(dx, dy)


def _wire_text_hits(
    session: Any,
    sheet: Any,
    texts: list[tuple[str, str, str, tuple[float, float, float, float]]],
) -> list[tuple[Any, Any, Any, tuple[float, float, float, float]]]:
    from math import hypot

    from panelcad.render.sheet import pin_world
    from panelcad.render.wiring import route_sheet_wires

    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    found: list[tuple[Any, Any, Any, tuple[float, float, float, float]]] = []
    for wire in sheet.wires:
        pts = routes.get(wire.id) or []
        pins: list[tuple[float, float]] = []
        for end in (wire.frm, wire.to):
            if "." not in str(end):
                continue
            ref, pin_id = str(end).split(".", 1)
            try:
                comp = sheet.component(ref)
                pins.append(pin_world(comp, session.catalog.get(comp.library_id).pin(pin_id)))
            except (KeyError, ValueError):
                continue
        for a, b in zip(pts, pts[1:]):
            mid_x = (a.x + b.x) * 0.5
            mid_y = (a.y + b.y) * 0.5
            for kind, owner, _label, box in texts:
                if kind == "mark" and owner == wire.id:
                    continue
                if _segment_inside(a, b, box) < 0.8:
                    continue
                near_pin = kind == "pin" and pins and any(
                    hypot(mid_x - px, mid_y - py) <= 4.0 for px, py in pins
                )
                if near_pin:
                    continue
                found.append((wire, a, b, box))
    return found


def _clear_routed_text(session: Any, sheet_id: str) -> None:
    """Slide a converted horizontal route off a tag or a wide pin caption.

    The NEMA supply hop is lifted off the PSU tag. An 8 mm ladder stub still
    cuts a four-character PLC address, so that run jogs over the caption.
    A vertical supply drop is left on the fresh-insert X: moving it slides
    the wire number into the next caption.
    """

    from panelcad.core.geometry import Point
    from panelcad.render.titleblock import working_rect
    from panelcad.render.wiring import clear_route_cache, route_sheet_wires

    try:
        sheet = session.sheet(sheet_id)
    except KeyError:
        return
    _wx, wy, _ww, wh = working_rect(sheet)
    for _ in range(8):
        clear_route_cache()
        texts = _sheet_text_boxes(session, sheet)
        hits = _wire_text_hits(session, sheet, texts)
        # A vertical supply drop shares its X with the fresh insert. Sliding
        # that X moves the wire number into the next caption (HOA "Auto").
        hits = [hit for hit in hits if abs(float(hit[1].x) - float(hit[2].x)) > 0.6]
        if not hits:
            break
        wire, a, b, box = hits[0]
        if not wire.points:
            break
        # Route points are the stored Point objects. Snapshot before mutating.
        _ax, ay, _bx, by = float(a.x), float(a.y), float(b.x), float(b.y)
        routes = route_sheet_wires(sheet, session.catalog, project=session.project)
        route = routes.get(wire.id) or []
        if abs(ay - by) > 0.6:
            break
        pin_run = (
            len(wire.points) == 2
            and abs(wire.points[0].y - wire.points[1].y) <= 0.6
            and "." in str(wire.frm)
            and "." in str(wire.to)
            and len(route) >= 2
        )
        if pin_run:
            src, dest = route[0], route[-1]
            cx = (box[0] + box[2]) * 0.5
            cy = (box[1] + box[3]) * 0.5
            if abs(dest.x - cx) + abs(dest.y - cy) > abs(src.x - cx) + abs(src.y - cy):
                src, dest = dest, src
            sign = 1.0 if dest.x >= src.x else -1.0
            jog_y = box[1] - 1.6
            if jog_y < wy + 3.0:
                jog_y = box[3] + 1.6
            if not (wy + 3.0 <= jog_y <= wy + wh - 3.0):
                break
            before = box[0] - 1.6 if sign > 0 else box[2] + 1.6
            stub = src.x + 8.0 * sign
            if sign > 0:
                stub = min(stub, before)
            else:
                stub = max(stub, before)
            end_x = dest.x if abs(dest.x - (box[2] if sign > 0 else box[0])) <= 12.0 else (
                box[2] + 1.6 if sign > 0 else box[0] - 1.6
            )
            points = [Point(x=stub, y=src.y)]
            if abs(before - stub) > 0.5:
                points.append(Point(x=before, y=src.y))
            points.append(Point(x=before, y=jog_y))
            points.append(Point(x=end_x, y=jog_y))
            if abs(end_x - dest.x) > 0.5:
                points.append(Point(x=end_x, y=src.y))
            wire.points = points
            continue
        new_y = box[1] - 1.6 if abs(ay - box[1]) <= abs(ay - box[3]) else box[3] + 1.6
        if new_y < wy + 3.0 or new_y > wy + wh - 3.0:
            break
        moved = False
        for point in wire.points:
            if abs(float(point.y) - ay) <= 0.6:
                point.y = new_y
                moved = True
        if not moved:
            break
    clear_route_cache()


def _route_specials(session: Any, sheet_id: str, parts: list[_Part], rungs: list[dict[str, Any]], target: str) -> None:
    from panelcad.core.circuit_ladder import (
        _route_drop_across,
        _route_iec_supply,
        _route_join_tee,
        _route_seal_u,
        _route_supply_clear_of_body,
    )

    sheet = session.sheet(sheet_id)
    hot = str(sheet.rail_hot or "")
    ret = str(sheet.rail_return or "")
    supply = next((part for part in parts if part.role in {"psu", "transformer"}), None)
    if supply is not None:
        _reposition_supply_refs(session, sheet, supply)
        plus = "X1" if supply.role == "transformer" else "plus"
        minus = "X2" if supply.role == "transformer" else "minus"
        if target_standard(target) == "IEC":
            _route_iec_supply(session, sheet_id, supply, plus, minus, hot, ret)
        else:
            _route_supply_clear_of_body(session, sheet_id, supply, plus, hot)
    for rung in rungs:
        seal = rung.get("seal")
        under = rung.get("seal_under")
        if seal is not None and under is not None:
            _route_seal_u(session, sheet_id, under, seal, _pin(session, under, "in"), _pin(session, under, "out"), _pin(session, seal, "in"), _pin(session, seal, "out"))
        tap = rung.get("tap")
        fresh = [part for part in (rung.get("contacts") or []) if part is not tap]
        if tap is not None and rung.get("tap_out") and fresh:
            _route_drop_across(session, sheet_id, tap, str(rung["tap_out"]), fresh[0], _pin(session, fresh[0], "in"))
        if rung.get("join") is not None and fresh:
            join = rung["join"]
            last = fresh[-1]
            _route_join_tee(session, sheet_id, last, _pin(session, last, "out"), join, _pin(session, join, "out"))
    from panelcad.core.circuit_ladder import route_rail_captions

    route_rail_captions(session, sheet_id)
    _clear_routed_text(session, sheet_id)




def _reposition_supply_refs(session: Any, sheet: Any, supply: _Part) -> None:
    """Keep L/N/PE off-page supply arrows docked 1.8 mm left of the supply input pins."""

    from panelcad.render.sheet import pin_world

    try:
        sup_comp = sheet.component(supply.ref)
        sup_dev = session.catalog.get(sup_comp.library_id)
    except KeyError:
        return
    sup_pins = {p.id: p for p in sup_dev.pins}
    for wire in sheet.wires:
        a, b = str(wire.frm or ""), str(wire.to or "")
        sup_end = a if a.startswith(f"{supply.ref}.") else (b if b.startswith(f"{supply.ref}.") else "")
        other_end = b if sup_end == a else a
        if not sup_end or "." not in other_end:
            continue
        pin_id = sup_end.split(".", 1)[1]
        if pin_id not in sup_pins:
            continue
        op_ref, op_pid = other_end.split(".", 1)
        try:
            op_comp = sheet.component(op_ref)
            op_dev = session.catalog.get(op_comp.library_id)
            op_pin = op_dev.pin(op_pid)
        except (KeyError, ValueError):
            continue
        if str(op_comp.properties.get("role") or "") != "supply_ref" and "offpage" not in str(op_comp.library_id or ""):
            continue
        px, py = pin_world(sup_comp, sup_pins[pin_id])
        op_comp.rotation = 0  # type: ignore[assignment]
        op_comp.scale = 1.0
        op_comp.x = px - 1.8 - float(op_pin.x)
        op_comp.y = py - float(op_pin.y)
        wire.points = []


def _pin(session: Any, part: _Part, which: str) -> str:
    device = session.catalog.get(part.library_id)
    if which == "in":
        found = next((pin.id for pin in device.pins if pin.side == "left"), "")
    else:
        found = next((pin.id for pin in device.pins if pin.side == "right"), "")
    return str(found or (device.pins[0].id if device.pins else ""))


def _rail_rename(old_hot: str, old_ret: str, hot: str, ret: str) -> dict[str, str]:
    """Old rail token → target token, including +24V / 24VDC spellings of the same rail."""

    rename: dict[str, str] = {}
    for token in _HOT_WORDS.get(old_hot, (old_hot,)):
        if token and token != hot:
            rename[token] = hot
    for token in _RET_WORDS.get(old_ret, (old_ret,)):
        if token and token != ret and token not in rename:
            rename[token] = ret
    return rename


# Roles that Circuit Builder draws as IEC columns. A DOL or VFD ladder stays a
# ladder on an IEC sheet (24V/0V), the same as insert_circuit.
_COLUMN_ROLES = frozenset(
    {
        "fwd",
        "rev",
        "fwd_coil",
        "rev_coil",
        "star",
        "delta",
        "star_coil",
        "delta_coil",
        "main",
        "main_coil",
        "ss",
        "ss_coil",
        "bypass",
        "bypass_coil",
        "timer",
        "starter_coil",
        "hoa",
        "cr_no",
        "run_seal",
    }
)
_LADDER_RAILS = {("24V", "0V"), ("+24V", "0V"), ("X1", "X2"), ("L1", "L2")}


def _uses_iec_columns(sheet: Any, parts: list[_Part], stored: dict | None = None) -> bool:
    if str(getattr(sheet, "drawing_style", "") or "") == "iec":
        return True
    # A previous conversion to a ladder remembers L+/L1 when the sheet was columns.
    if stored and str(stored.get("from_hot") or "") in {"L+", "L1"} and str(stored.get("kind") or "") != "io":
        return True
    return any(part.role in _COLUMN_ROLES for part in parts)


def _stored_rails(session: Any, sheet_id: str) -> dict:
    block = session.project.properties.get("convert_standard") or {}
    if not isinstance(block, dict):
        return {}
    rails = block.get("rails") or {}
    if not isinstance(rails, dict):
        return {}
    found = rails.get(sheet_id) or {}
    return found if isinstance(found, dict) else {}


def _nema_alias(session: Any, sheet: Any, target: str, old_hot: str, old_ret: str) -> str:
    if target_standard(target) != "NEMA/ANSI":
        return ""
    stored = _stored_rails(session, str(sheet.id))
    alias = str(stored.get("alias") or "")
    if not alias:
        alias = nema_alias_for(old_hot, old_ret)
    if not alias and (old_hot, old_ret) == ("L1", "N"):
        if stored.get("from_hot") == "L1" and stored.get("from_ret") == "L2":
            alias = "L1/L2"
    if not alias and old_hot == "L+" and stored.get("from_hot") == "+24V":
        alias = "+24V/0V"
    return alias


def _sheet_columns(session: Any, sheet: Any, target: str, parts: list[_Part]) -> bool:
    if target_standard(target) != "IEC" or rail_kind(sheet, parts) == "io":
        return False
    return _uses_iec_columns(sheet, parts, _stored_rails(session, str(sheet.id)))


def _control_sheet_for_io(session: Any, io_id: str) -> Any | None:
    """Motor-control sheet this I/O page belongs to. The nearest one above it."""

    sheets = list(session.project.sheets)

    def _is_control(page: Any) -> bool:
        if str(getattr(page, "kind", "") or "") == "io":
            return False
        blob = f"{getattr(page, 'id', '')} {getattr(page, 'title', '') or ''}".lower()
        return "control" in blob

    index = next((i for i, page in enumerate(sheets) if page.id == io_id), None)
    if index is not None:
        for page in reversed(sheets[:index]):
            if _is_control(page):
                return page
    for page in sheets:
        if page.id != io_id and _is_control(page):
            return page
    return None


def _target_rail_pair(
    session: Any,
    sheet: Any,
    target: str,
    parts: list[_Part],
    *,
    columns: bool,
    follow_control: bool = True,
) -> tuple[str, str]:
    """Hot and return after conversion.

    PLC I/O uses the motor-control pair for the target standard. AC control is
    X1/X2 either way, so the rail is not supply phase L1. The card pin stays
    M; the wire number is that return (0V or X2). With no control sheet, I/O
    takes the DC pair, not a separate L+/M rail.
    """

    kind = rail_kind(sheet, parts)
    old_hot = str(sheet.rail_hot or "")
    old_ret = str(sheet.rail_return or "")
    std = target_standard(target)
    if kind == "io" and follow_control:
        control = _control_sheet_for_io(session, str(sheet.id))
        if control is not None:
            control_parts = parts_on_sheet(session, control)
            return _target_rail_pair(
                session,
                control,
                target,
                control_parts,
                columns=_sheet_columns(session, control, target, control_parts),
                follow_control=False,
            )
        kind = "dc"
    stored = _stored_rails(session, str(sheet.id))
    alias = _nema_alias(session, sheet, target, old_hot, old_ret)
    if std == "IEC" and not columns and kind != "io":
        if str(stored.get("from_hot") or "") in {"L+", "24V", "+24V", "X1", "L1"}:
            return str(stored["from_hot"]), str(stored.get("from_ret") or old_ret or "0V")
        # A DOL / VFD control ladder keeps its rail words. Column circuits use L+/0V.
        if (old_hot, old_ret) in _LADDER_RAILS:
            return old_hot, old_ret
        if kind == "ac":
            return "X1", "X2"
        return "24V", "0V"
    return rail_pair_for(kind, target, nema_alias=alias)


def apply_sheet_rails(
    session: Any,
    sheet_id: str,
    target: str,
    parts: list[_Part],
    *,
    columns: bool = True,
) -> dict[str, str]:
    """Rename this sheet's control rails and the wire ends that land on them.

    Empty rail fields are left alone: a power schematic must not treat a
    missing rail as 24V and rewrite phase nets. A stored alias restores
    L1/L2 or +24V/0V after a round trip.
    """

    sheet = session.sheet(sheet_id)
    kind = rail_kind(sheet, parts)
    old_hot = str(sheet.rail_hot or "")
    old_ret = str(sheet.rail_return or "")
    tb = getattr(session.project, "title_block", None)
    tb_hot = str(getattr(tb, "rail_hot", "") or "") if tb is not None else ""
    tb_ret = str(getattr(tb, "rail_return", "") or "") if tb is not None else ""
    if str(getattr(sheet, "drawing_style", "") or "") in {"ladder", "iec"}:
        if not old_hot and tb_hot in {"L+", "24V", "+24V", "X1", "L1"}:
            old_hot = tb_hot
        if not old_ret and tb_ret in {"0V", "M", "X2", "N", "L2"}:
            old_ret = tb_ret
        sheet.rail_hot = old_hot
        sheet.rail_return = old_ret
    std = target_standard(target)
    alias = _nema_alias(session, sheet, target, old_hot, old_ret)
    hot, ret = _target_rail_pair(session, sheet, target, parts, columns=columns)
    leaving = nema_alias_for(old_hot, old_ret) if std == "IEC" else (alias or nema_alias_for(hot, ret))
    sheet.rail_hot = hot
    sheet.rail_return = ret
    if tb is not None and str(getattr(sheet, "drawing_style", "") or "") in {"ladder", "iec"}:
        if tb_hot and tb_hot == old_hot and old_hot in {"L+", "24V", "+24V", "X1", "L1"}:
            tb.rail_hot = hot
        if tb_ret and tb_ret == old_ret and old_ret in {"0V", "M", "X2", "N", "L2"}:
            tb.rail_return = ret
    rename = _rail_rename(old_hot, old_ret, hot, ret)
    if rename:
        for wire in sheet.wires:
            if wire.frm in rename:
                wire.frm = rename[wire.frm]
            if wire.to in rename:
                wire.to = rename[wire.to]
            if wire.net in rename:
                wire.net = rename[wire.net]
            if str(wire.label or "") in rename:
                wire.label = rename[str(wire.label)]
            # Elementary sheets draw the wire number. A conventional rail name
            # (24V, L+) was copied into the number and has to move with the net.
            if str(wire.number or "") in rename:
                wire.number = rename[str(wire.number)]
    return {
        "from_hot": old_hot,
        "from_ret": old_ret,
        "to_hot": hot,
        "to_ret": ret,
        "kind": kind,
        "alias": leaving,
    }


def relayout_sheet(session: Any, sheet_id: str, target: str) -> dict[str, str]:
    """Place the sheet in the target standard's control layout. Returns the rail record."""

    from panelcad.core.circuit_extra import arrange_control_ladder
    from panelcad.core.circuit_ladder import apply_plc_io_pins
    from panelcad.render.wiring import clear_route_cache

    sheet = session.sheet(sheet_id)
    if is_power_sheet(sheet):
        sheet.standard = target_standard(target)
        return {
            "from_hot": str(sheet.rail_hot or ""),
            "from_ret": str(sheet.rail_return or ""),
            "to_hot": str(sheet.rail_hot or ""),
            "to_ret": str(sheet.rail_return or ""),
            "kind": "power",
            "alias": "",
        }
    # prepare_* writes its own rail pair. Capture the names the sheet uses now
    # so apply_sheet_rails can rename wire ends that land on those rails.
    old_hot = str(sheet.rail_hot or "")
    old_ret = str(sheet.rail_return or "")
    std = target_standard(target)
    sheet.standard = std
    parts = parts_on_sheet(session, sheet)
    kind = rail_kind(sheet, parts)
    # I/O stays a horizontal ladder so every PLC pin stays on the card and the
    # coil sits between the output and the return rail. The rail words are the
    # control pair (L+/0V or 24V/0V, and X1/X2 for AC). Fresh IEC inserts keep
    # the column layout; only conversion comes through here.
    stored = ((session.project.properties.get("convert_standard") or {}).get("rails") or {}).get(sheet_id) or {}
    columns = std == "IEC" and kind != "io" and _uses_iec_columns(sheet, parts, stored)
    if not columns:
        from panelcad.core.circuit_ladder import prepare_ladder_sheet

        prepare_ladder_sheet(session, sheet_id, {"rail_voltage_hint": 120.0 if kind == "ac" else 24.0})
    else:
        from panelcad.core.circuit_iec import prepare_iec_sheet

        prepare_iec_sheet(session, sheet_id, {"rail_voltage_hint": 120.0 if kind == "ac" else 24.0}, io=False)
    sheet.rail_hot = old_hot
    sheet.rail_return = old_ret
    record = apply_sheet_rails(session, sheet_id, std, parts, columns=columns)
    sheet.rail_hot = record["to_hot"]
    sheet.rail_return = record["to_ret"]
    rungs, key, extra = rungs_for(session, sheet, parts)
    extra[key] = rungs
    left = unplaced_wired(sheet, parts, rungs)
    if left:
        record["unplaced"] = left
    if not parts or not rungs:
        record.pop("unplaced", None)
        _relayout_generic_sheet(session, sheet, std)
        clear_route_cache()
        return record
    arrange_control_ladder(session, sheet_id, parts, extra, key)
    _write_back(sheet, parts)
    if key == "_io_rungs":
        apply_plc_io_pins(session, sheet_id, extra)
    _clear_points(sheet)
    _route_specials(session, sheet_id, parts, rungs, std)
    clear_route_cache()
    return record


MAX_RUNGS_PER_SHEET = 6

_CONTACT_SUFFIXES = (
    ".aux_no",
    ".aux_nc",
    ".ol_contact",
    ".pb_no",
    ".pb_nc",
    ".estop",
    ".estop_2nc",
    ".selector_no",
    ".selector_nc",
    ".selector_3pos",
    ".limit_no",
    ".limit_nc",
    ".float_no",
    ".float_nc",
    ".level_no",
    ".level_nc",
    ".pressure_no",
    ".pressure_nc",
    ".temperature_no",
    ".temperature_nc",
    ".flow_no",
    ".flow_nc",
    ".foot_no",
    ".foot_nc",
    ".contact_notc",
    ".contact_ncto",
    ".contact_noto",
    ".contact_nctc",
)

_CONTACT_TOKENS = (
    "protection.fuse",
    "breakers.",
    "disconnect",
    "terminals.",
    "sensors.prox",
    "thermal_overload",
)

_OUTPUT_TOKENS = (
    ".coils.",
    "pilot_light",
    "relays.",
    "timers.",
    "operators.horn",
    "loads.",
    "solenoid",
    "actuators.",
)


def _is_contact_lib(lid: str) -> bool:
    low = str(lid or "").lower()
    if low.endswith(_CONTACT_SUFFIXES):
        return True
    return any(tok in low for tok in _CONTACT_TOKENS)


def _is_output_comp(lid: str, pin_ids: list[str], on_ret: bool) -> bool:
    low = str(lid or "").lower()
    if "offpage" in low or "power.psu" in low or "transformer" in low or _is_contact_lib(low):
        return False
    pset = set(pin_ids)
    if on_ret or {"A1", "A2"} <= pset or {"X1", "X2"} <= pset:
        return True
    return any(tok in low for tok in _OUTPUT_TOKENS)


def _default_pin_y(session: Any, comp: Any) -> float:
    try:
        device = session.catalog.get(comp.library_id)
    except KeyError:
        return 0.0
    for pin in device.pins:
        if pin.side in {"left", "right"}:
            return float(pin.y)
    return float(device.pins[0].y) if device.pins else 0.0


def _comp_width(session: Any, comp: Any) -> float:
    try:
        device = session.catalog.get(comp.library_id)
    except KeyError:
        return 28.0
    xs = [float(p.x) for p in device.pins if p.side in {"left", "right"}]
    if len(xs) >= 2:
        return max(xs) - min(xs)
    return float(getattr(device, "width", 28.0) or 28.0)


def _pin_world_xy(session: Any, sheet: Any, end: str) -> tuple[float, float] | None:
    from panelcad.render.sheet import pin_world, resolve_pin

    text = str(end or "")
    if "." not in text:
        return None
    cid, pid = text.split(".", 1)
    try:
        comp = sheet.component(cid)
        dev = session.catalog.get(comp.library_id)
        pin = resolve_pin(dev, comp, pid)
    except KeyError:
        return None
    return pin_world(comp, pin)


def _relayout_generic_sheet(session: Any, sheet: Any, std: str) -> None:
    """Relayout a control sheet that has no Circuit Builder role metadata."""

    if is_power_sheet(sheet):
        return
    from collections import deque

    from panelcad.core.geometry import Point
    from panelcad.render.titleblock import working_rect

    block = session.project.properties.get("convert_standard")
    if not isinstance(block, dict):
        block = {}
        session.project.properties["convert_standard"] = block
    pos_map = block.setdefault("positions", {})
    if std == "IEC":
        saved = pos_map.get(sheet.id)
        if isinstance(saved, dict):
            comp_saved = saved.get("components") or {}
            for comp in sheet.components:
                row = comp_saved.get(comp.id)
                if isinstance(row, dict):
                    comp.x = float(row.get("x", comp.x))
                    comp.y = float(row.get("y", comp.y))
                    comp.rotation = int(row.get("rotation", 0))  # type: ignore[assignment]
                    comp.scale = float(row.get("scale", 1.0))
            wire_saved = saved.get("wires") or {}
            for wire in sheet.wires:
                pts = wire_saved.get(wire.id)
                if isinstance(pts, list):
                    wire.points = [Point(x=float(p["x"]), y=float(p["y"])) for p in pts if isinstance(p, dict)]
        return

    if sheet.id not in pos_map:
        pos_map[sheet.id] = {
            "components": {
                comp.id: {
                    "x": float(comp.x),
                    "y": float(comp.y),
                    "rotation": int(comp.rotation or 0),
                    "scale": float(comp.scale or 1.0),
                }
                for comp in sheet.components
            },
            "wires": {
                wire.id: [{"x": float(pt.x), "y": float(pt.y)} for pt in (wire.points or [])]
                for wire in sheet.wires
            },
        }

    hot = str(sheet.rail_hot or "24V")
    ret = str(sheet.rail_return or "0V")
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for wire in sheet.wires:
        a, b = str(wire.frm or ""), str(wire.to or "")
        if a and b:
            union(a, b)

    offpage_ins: list[Any] = []
    offpage_outs: list[Any] = []
    outputs: list[Any] = []
    contacts: list[Any] = []
    for comp in sheet.components:
        lid = str(comp.library_id or "")
        low = lid.lower()
        if "offpage" in low:
            if "offpage_from" in low or str(comp.properties.get("role") or "") == "from":
                offpage_ins.append(comp)
            else:
                offpage_outs.append(comp)
            continue
        if "power.psu" in low or "transformer" in low:
            continue
        try:
            dev = session.catalog.get(lid)
        except KeyError:
            continue
        pin_ids = [str(p.id) for p in dev.pins]
        on_ret = any(find(f"{comp.id}.{pid}") == find(ret) for pid in pin_ids)
        if _is_output_comp(lid, pin_ids, on_ret):
            outputs.append(comp)
        elif len(pin_ids) == 2 or _is_contact_lib(lid):
            contacts.append(comp)

    def _out_sort_key(c: Any) -> tuple[int, float, float, str]:
        src_ord = int(c.properties.get("_src_order", 0))
        ox = float(c.properties.get("_orig_x", c.x))
        oy = float(c.properties.get("_orig_y", c.y))
        return (src_ord, ox, oy, c.id)

    def _contact_sort_key(c: Any) -> tuple[int, float, float, str]:
        src_ord = int(c.properties.get("_src_order", 0))
        ox = float(c.properties.get("_orig_x", c.x))
        oy = float(c.properties.get("_orig_y", c.y))
        return (src_ord, oy, ox, c.id)

    outputs.sort(key=_out_sort_key)
    contacts.sort(key=_contact_sort_key)
    if not outputs:
        return

    wx, wy, ww, _wh = working_rect(sheet)
    hot_net = find(hot)
    ret_net = find(ret)
    used_contacts: set[str] = set()

    def comp_nets(comp: Any) -> tuple[str, str] | None:
        try:
            dev = session.catalog.get(comp.library_id)
        except KeyError:
            return None
        if len(dev.pins) < 2:
            return None
        p0, p1 = str(dev.pins[0].id), str(dev.pins[1].id)
        return find(f"{comp.id}.{p0}"), find(f"{comp.id}.{p1}")

    net_depth: dict[str, int] = {hot_net: 0}
    net_producer: dict[str, str] = {}
    for op_in in offpage_ins:
        op_n = find(f"{op_in.id}.A")
        if op_n not in net_depth:
            net_depth[op_n] = 1

    rung_records: list[dict[str, Any]] = []
    for orig_idx, out_comp in enumerate(outputs):
        try:
            out_dev = session.catalog.get(out_comp.library_id)
            out_pins = [str(p.id) for p in out_dev.pins]
        except KeyError:
            out_pins = ["A1", "A2"]

        ret_contact: Any = None
        direct_ret_pin = next((pid for pid in out_pins if find(f"{out_comp.id}.{pid}") == ret_net), None)
        if direct_ret_pin is not None:
            in_pin = next((pid for pid in out_pins if pid != direct_ret_pin), out_pins[0] if out_pins else "A1")
        else:
            in_pin = out_pins[0] if out_pins else "A1"
            for pid in reversed(out_pins):
                p_net = find(f"{out_comp.id}.{pid}")
                for rc in contacts:
                    if rc.id in used_contacts:
                        continue
                    rc_nets = comp_nets(rc)
                    if rc_nets is not None and p_net in rc_nets and ret_net in rc_nets:
                        ret_contact = rc
                        used_contacts.add(rc.id)
                        in_pin = next((op for op in out_pins if op != pid), in_pin)
                        break
                if ret_contact is not None:
                    break

        target_net = find(f"{out_comp.id}.{in_pin}")
        seed_nets = sorted(net_depth.keys(), key=lambda n: -net_depth[n])
        queue: deque[tuple[str, str, list[Any]]] = deque((sn, sn, []) for sn in seed_nets)
        visited_nets: set[str] = set(seed_nets)
        chosen_start = hot_net
        chain: list[Any] = []
        while queue:
            start_n, curr_net, path = queue.popleft()
            if curr_net == target_net:
                chosen_start = start_n
                chain = path
                break
            for c in contacts:
                if c.id in used_contacts or c in path:
                    continue
                nets = comp_nets(c)
                if nets is None:
                    continue
                n0, n1 = nets
                nxt = n1 if n0 == curr_net else (n0 if n1 == curr_net else None)
                if nxt is not None and nxt not in visited_nets:
                    visited_nets.add(nxt)
                    queue.append((start_n, nxt, path + [c]))

        step_offset = net_depth.get(chosen_start, 0)
        parent_rung_id = net_producer.get(chosen_start)
        curr_n = chosen_start
        for step_i, c in enumerate(chain):
            used_contacts.add(c.id)
            nets = comp_nets(c)
            if nets is not None:
                n0, n1 = nets
                out_n = n1 if n0 == curr_n else n0
                if out_n not in net_depth:
                    net_depth[out_n] = step_offset + step_i + 1
                    net_producer[out_n] = out_comp.id
                curr_n = out_n

        parallels: list[tuple[Any, Any]] = []
        for under_c in chain:
            u_nets = comp_nets(under_c)
            if u_nets is None:
                continue
            u_set = frozenset(u_nets)
            for c in contacts:
                if c.id in used_contacts:
                    continue
                c_nets = comp_nets(c)
                if c_nets is not None and frozenset(c_nets) == u_set:
                    used_contacts.add(c.id)
                    parallels.append((c, under_c))

        rung_records.append(
            {
                "orig_idx": orig_idx,
                "out_comp": out_comp,
                "ret_contact": ret_contact,
                "chain": chain,
                "parallels": parallels,
                "step_offset": step_offset,
                "parent_rung_id": parent_rung_id,
                "chosen_start": chosen_start,
                "target_net": target_net,
            }
        )

    by_out_id = {r["out_comp"].id: r for r in rung_records}
    children_map: dict[str | None, list[dict[str, Any]]] = {}
    for r in rung_records:
        pid = r["parent_rung_id"]
        if pid not in by_out_id:
            pid = None
        children_map.setdefault(pid, []).append(r)

    for pid, ch_list in children_map.items():
        if pid is None:
            ch_list.sort(key=lambda r: r["orig_idx"])
        else:
            ch_list.sort(key=lambda r: (-r["step_offset"], r["orig_idx"]))

    ordered_rungs: list[dict[str, Any]] = []
    visited_rungs: set[str] = set()

    def _dfs_rung(r: dict[str, Any]) -> None:
        rid = r["out_comp"].id
        if rid in visited_rungs:
            return
        visited_rungs.add(rid)
        ordered_rungs.append(r)
        for child in children_map.get(rid, []):
            _dfs_rung(child)

    for root_r in children_map.get(None, []):
        _dfs_rung(root_r)
    for r in rung_records:
        _dfs_rung(r)

    n_rungs = len(ordered_rungs)
    has_parallels = any(bool(r["parallels"]) for r in ordered_rungs)
    if n_rungs <= 4:
        y0, y_step, branch_dy = wy + 28.0, (40.0 if has_parallels else 34.0), 20.0
    elif n_rungs == 5:
        y0, y_step, branch_dy = wy + 28.0, (38.0 if has_parallels else 34.0), 20.0
    else:
        y0, y_step, branch_dy = wy + 26.0, 34.0, 18.0

    parallel_comp_ids: set[str] = set()
    rung_y_by_net: dict[str, float] = {}
    for row_idx, r in enumerate(ordered_rungs):
        rung_y = y0 + row_idx * y_step
        r["rung_y"] = rung_y
        step_offset = r["step_offset"]
        for step_i, c in enumerate(r["chain"]):
            c.rotation = 0  # type: ignore[assignment]
            c.scale = 1.0
            c.x = wx + 46.0 + (step_offset + step_i) * 49.0
            c.y = rung_y - _default_pin_y(session, c)
            c_nets = comp_nets(c)
            if c_nets is not None:
                for cn in c_nets:
                    rung_y_by_net.setdefault(cn, rung_y)

        for pc, under_c in r["parallels"]:
            parallel_comp_ids.add(pc.id)
            pc.rotation = 0  # type: ignore[assignment]
            pc.scale = 1.0
            pc.x = under_c.x
            pc.y = (rung_y + branch_dy) - _default_pin_y(session, pc)

        out_comp = r["out_comp"]
        ret_contact = r["ret_contact"]
        out_w = _comp_width(session, out_comp)
        out_comp.rotation = 0  # type: ignore[assignment]
        out_comp.scale = 1.0
        if ret_contact is not None:
            rc_w = _comp_width(session, ret_contact)
            ret_contact.rotation = 0  # type: ignore[assignment]
            ret_contact.scale = 1.0
            ret_contact.x = wx + ww - 14.0 - rc_w
            ret_contact.y = rung_y - _default_pin_y(session, ret_contact)
            out_comp.x = ret_contact.x - 14.0 - out_w
        else:
            out_comp.x = min(wx + ww - 36.0, wx + ww - 12.0 - out_w)
        out_comp.y = rung_y - _default_pin_y(session, out_comp)
        rung_y_by_net.setdefault(r["target_net"], rung_y)
        rung_y_by_net.setdefault(r["chosen_start"], rung_y)

    for idx_in, op_in in enumerate(offpage_ins):
        op_n = find(f"{op_in.id}.A")
        ry = rung_y_by_net.get(op_n, y0 + idx_in * y_step)
        op_in.rotation = 0  # type: ignore[assignment]
        op_in.scale = 1.0
        op_in.x = wx + 18.0
        op_in.y = ry - _default_pin_y(session, op_in)

    for idx_out, op_out in enumerate(offpage_outs):
        op_n = find(f"{op_out.id}.A")
        ry = rung_y_by_net.get(op_n, y0 + idx_out * y_step)
        depth = net_depth.get(op_n, 1)
        op_out.rotation = 0  # type: ignore[assignment]
        op_out.scale = 1.0
        op_out.x = wx + 46.0 + depth * 49.0
        op_out.y = (ry + branch_dy) - _default_pin_y(session, op_out)

    _clear_points(sheet)
    _route_generic_branches_and_subbuses(session, sheet, parallel_comp_ids)


def _route_generic_branches_and_subbuses(
    session: Any,
    sheet: Any,
    parallel_comp_ids: set[str],
) -> None:
    """Assign clean L-bend waypoints for parallel branches and cross-rung sub-buses."""

    from panelcad.core.geometry import Point

    for wire in sheet.wires:
        a_ref = str(wire.frm or "")
        b_ref = str(wire.to or "")
        if "." not in a_ref or "." not in b_ref:
            continue
        pa = _pin_world_xy(session, sheet, a_ref)
        pb = _pin_world_xy(session, sheet, b_ref)
        if pa is None or pb is None:
            continue
        ax, ay = pa
        bx, by = pb
        if abs(ax - bx) <= 0.5 or abs(ay - by) <= 0.5:
            wire.points = []
            continue
        cid_a = a_ref.split(".", 1)[0]
        cid_b = b_ref.split(".", 1)[0]
        if cid_a in parallel_comp_ids and cid_b not in parallel_comp_ids:
            wire.points = [Point(x=bx, y=ay)]
        elif cid_b in parallel_comp_ids and cid_a not in parallel_comp_ids:
            wire.points = [Point(x=ax, y=by)]
        elif abs(ay - by) > 20.0:
            if ay < by:
                wire.points = [Point(x=ax, y=by)]
            else:
                wire.points = [Point(x=bx, y=ay)]


def consolidate_control_sheets(session: Any, ctrl_sids: list[str]) -> dict[str, Any]:
    """Consolidate multi-page IEC control sheets into NEMA ladder sheet(s)."""

    from panelcad.core.models import Wire
    from panelcad.render.wiring import clear_route_cache

    if len(ctrl_sids) < 2:
        return {"kept_sheets": list(ctrl_sids), "removed_sheets": []}

    sheets = [session.sheet(sid) for sid in ctrl_sids]
    block = session.project.properties.get("convert_standard")
    pos_map = (block.get("positions") or {}) if isinstance(block, dict) else {}

    for s_idx, sh in enumerate(sheets):
        saved_comps = ((pos_map.get(sh.id) or {}).get("components") or {}) if isinstance(pos_map, dict) else {}
        for comp in sh.components:
            comp.properties["_src_order"] = s_idx
            orig = saved_comps.get(comp.id)
            if isinstance(orig, dict):
                comp.properties["_orig_x"] = float(orig.get("x", comp.x))
                comp.properties["_orig_y"] = float(orig.get("y", comp.y))
            else:
                comp.properties["_orig_x"] = float(comp.x)
                comp.properties["_orig_y"] = float(comp.y)

    # Identify all output components across ctrl_sids in sheet order to decide partitioning
    all_outputs: list[tuple[str, Any]] = []
    for sh in sheets:
        ret = str(sh.rail_return or "0V")
        parent_s: dict[str, str] = {}

        def _find_s(x: str) -> str:
            parent_s.setdefault(x, x)
            while parent_s[x] != x:
                parent_s[x] = parent_s[parent_s[x]]
                x = parent_s[x]
            return x

        for w in sh.wires:
            if w.frm and w.to:
                ra, rb = _find_s(str(w.frm)), _find_s(str(w.to))
                if ra != rb:
                    parent_s[ra] = rb

        sh_outs: list[Any] = []
        for comp in sh.components:
            lid = str(comp.library_id or "")
            try:
                dev = session.catalog.get(lid)
            except KeyError:
                continue
            pin_ids = [str(p.id) for p in dev.pins]
            on_ret = any(_find_s(f"{comp.id}.{pid}") == _find_s(ret) for pid in pin_ids)
            if _is_output_comp(lid, pin_ids, on_ret):
                sh_outs.append(comp)
        sh_outs.sort(
            key=lambda c: (
                int(c.properties.get("_src_order", 0)),
                float(c.properties.get("_orig_x", c.x)),
                float(c.properties.get("_orig_y", c.y)),
                c.id,
            )
        )
        for out_c in sh_outs:
            all_outputs.append((sh.id, out_c))

    # Map each source sheet to a target consolidated sheet so each target sheet has <= MAX_RUNGS_PER_SHEET outputs
    sheet_out_counts = {sid: 0 for sid in ctrl_sids}
    for sid, _out_c in all_outputs:
        sheet_out_counts[sid] += 1

    target_for_sheet: dict[str, str] = {}
    kept_sids: list[str] = []
    curr_target = ctrl_sids[0]
    curr_count = 0
    kept_sids.append(curr_target)
    for sid in ctrl_sids:
        cnt = sheet_out_counts.get(sid, 0)
        if curr_count > 0 and curr_count + cnt > MAX_RUNGS_PER_SHEET:
            curr_target = sid
            curr_count = cnt
            if curr_target not in kept_sids:
                kept_sids.append(curr_target)
        else:
            curr_count += cnt
        target_for_sheet[sid] = curr_target

    # Collect offpage components across ctrl_sids and group into clusters
    offpage_by_id: dict[str, tuple[str, Any]] = {}
    for sh in sheets:
        for comp in sh.components:
            if "offpage" in str(comp.library_id or "").lower():
                offpage_by_id[comp.id] = (sh.id, comp)

    op_parent: dict[str, str] = {op_id: op_id for op_id in offpage_by_id}

    def op_find(x: str) -> str:
        op_parent.setdefault(x, x)
        while op_parent[x] != x:
            op_parent[x] = op_parent[op_parent[x]]
            x = op_parent[x]
        return x

    def op_union(a: str, b: str) -> None:
        ra, rb = op_find(a), op_find(b)
        if ra != rb:
            op_parent[ra] = rb

    # 1) Union offpage components linked via properties["link"] ("sheet:OPx.A|...")
    for op_id, (_sid, comp) in offpage_by_id.items():
        link = str(comp.properties.get("link") or "").strip()
        if link:
            for piece in link.split("|"):
                end = piece.split(":", 1)[1] if ":" in piece else piece
                other_id = end.split(".", 1)[0]
                if other_id in offpage_by_id:
                    op_union(op_id, other_id)

    # 2) Union offpage components sharing the same non-empty wire net/number or component label
    by_signal: dict[str, list[str]] = {}
    for sh in sheets:
        for w in sh.wires:
            for end in (str(w.frm or ""), str(w.to or "")):
                cid = end.split(".", 1)[0]
                if cid in offpage_by_id:
                    sig = str(w.net or w.number or "").strip()
                    if not sig:
                        sig = str(offpage_by_id[cid][1].label or "").strip()
                    if sig:
                        by_signal.setdefault(sig, []).append(cid)
    for sig_ops in by_signal.values():
        first = sig_ops[0]
        for other in sig_ops[1:]:
            op_union(first, other)

    clusters: dict[str, list[str]] = {}
    for op_id in offpage_by_id:
        clusters.setdefault(op_find(op_id), []).append(op_id)

    # Move all non-offpage components and non-offpage wires onto their target consolidated sheet
    existing_wire_ids = {w.id for s in session.project.sheets for w in s.wires}
    w_seq = 1

    def _alloc_wire_id(prefix: str = "WC") -> str:
        nonlocal w_seq
        while f"{prefix}{w_seq}" in existing_wire_ids:
            w_seq += 1
        wid = f"{prefix}{w_seq}"
        existing_wire_ids.add(wid)
        w_seq += 1
        return wid

    # Track stub wires connected to offpage pins before moving
    op_stubs: dict[str, list[tuple[str, str, Wire]]] = {op_id: [] for op_id in offpage_by_id}
    for sh in sheets:
        kept_wires: list[Wire] = []
        for w in sh.wires:
            a, b = str(w.frm or ""), str(w.to or "")
            ca = a.split(".", 1)[0]
            cb = b.split(".", 1)[0]
            if ca in offpage_by_id and cb not in offpage_by_id:
                op_stubs[ca].append((sh.id, b, w))
            elif cb in offpage_by_id and ca not in offpage_by_id:
                op_stubs[cb].append((sh.id, a, w))
            elif ca in offpage_by_id and cb in offpage_by_id:
                op_union(ca, cb)
            else:
                kept_wires.append(w)
        sh.wires = kept_wires

    for sid in ctrl_sids:
        t_sid = target_for_sheet[sid]
        if sid == t_sid:
            continue
        src_sh = session.sheet(sid)
        dst_sh = session.sheet(t_sid)
        moving_comps = [c for c in src_sh.components if c.id not in offpage_by_id]
        src_sh.components = [c for c in src_sh.components if c.id in offpage_by_id]
        dst_sh.components.extend(moving_comps)
        dst_sh.wires.extend(src_sh.wires)
        src_sh.wires = []

    # Process each offpage cluster
    same_sheet_cluster_wires: list[tuple[str, list[Wire], list[str]]] = []
    for _root, member_ops in clusters.items():
        # Gather all real endpoints (target_sid, other_end, stub_wire, op_id)
        real_items: list[tuple[str, str, Wire, str]] = []
        for op_id in member_ops:
            for src_sid, other_end, stub_w in op_stubs.get(op_id, []):
                t_sid = target_for_sheet[src_sid]
                real_items.append((t_sid, other_end, stub_w, op_id))

        if not real_items:
            for op_id in member_ops:
                s_id, op_c = offpage_by_id[op_id]
                sh_o = session.sheet(s_id)
                sh_o.components = [c for c in sh_o.components if c.id != op_c.id]
            continue

        proto = real_items[0][2]
        target_sids = {item[0] for item in real_items}

        # Check if any endpoint in the cluster is a rail (e.g. 24V / 0V / L+ / M)
        rail_ends: list[str] = []
        for t_sid, other_end, _sw, _op in real_items:
            t_sh = session.sheet(t_sid)
            if other_end in {str(t_sh.rail_hot or "24V"), str(t_sh.rail_return or "0V"), "24V", "0V", "L+", "M"}:
                rail_ends.append(other_end)

        if rail_ends:
            rail_name = rail_ends[0]
            for t_sid, other_end, stub_w, _op in real_items:
                if other_end == rail_name or "." not in other_end:
                    continue
                t_sh = session.sheet(t_sid)
                r_name = str(t_sh.rail_hot or "24V") if rail_name in {"24V", "L+"} else str(t_sh.rail_return or "0V")
                if not any({w.frm, w.to} == {r_name, other_end} for w in t_sh.wires):
                    t_sh.wires.append(
                        Wire(
                            id=_alloc_wire_id(),
                            frm=r_name,
                            to=other_end,
                            net=r_name,
                            number=r_name,
                            gauge=stub_w.gauge,
                            color=stub_w.color,
                            wire_type=stub_w.wire_type,
                        )
                    )
            for op_id in member_ops:
                s_id, op_c = offpage_by_id[op_id]
                sh_o = session.sheet(s_id)
                sh_o.components = [c for c in sh_o.components if c.id != op_c.id]
            continue

        if len(target_sids) == 1:
            # All endpoints consolidated onto a single target sheet! Remove offpage arrows and daisy-chain.
            t_sid = next(iter(target_sids))
            t_sh = session.sheet(t_sid)
            ends: list[str] = []
            for _ts, other_end, _sw, _op in real_items:
                if other_end not in ends:
                    ends.append(other_end)
            created_wires: list[Wire] = []
            for i in range(len(ends) - 1):
                nw = Wire(
                    id=_alloc_wire_id(),
                    frm=ends[i],
                    to=ends[i + 1],
                    net=proto.net,
                    number=proto.number,
                    gauge=proto.gauge,
                    color=proto.color,
                    wire_type=proto.wire_type,
                )
                t_sh.wires.append(nw)
                created_wires.append(nw)
            if len(ends) >= 2:
                same_sheet_cluster_wires.append((t_sid, created_wires, ends))
            for op_id in member_ops:
                s_id, op_c = offpage_by_id[op_id]
                sh_o = session.sheet(s_id)
                sh_o.components = [c for c in sh_o.components if c.id != op_c.id]
        else:
            # Spans multiple target consolidated sheets (continuation sheet case):
            # Group endpoints by target_sid, keep 1 offpage component per target_sid, and link them.
            by_tsid: dict[str, list[tuple[str, Wire, str]]] = {}
            for t_sid, other_end, stub_w, op_id in real_items:
                by_tsid.setdefault(t_sid, []).append((other_end, stub_w, op_id))
            kept_ops: list[tuple[str, Any]] = []
            ordered_tsids = [sid for sid in kept_sids if sid in by_tsid]
            used_op_ids: set[str] = set()
            for idx_t, t_sid in enumerate(ordered_tsids):
                items = by_tsid[t_sid]
                chosen_op_id = next((oid for _oe, _sw, oid in items if oid not in used_op_ids), None)
                if chosen_op_id is None:
                    chosen_op_id = next((oid for oid in member_ops if oid not in used_op_ids), items[0][2])
                used_op_ids.add(chosen_op_id)
                orig_sid, op_comp = offpage_by_id[chosen_op_id]
                t_sh = session.sheet(t_sid)
                if orig_sid != t_sid:
                    orig_sh = session.sheet(orig_sid)
                    orig_sh.components = [c for c in orig_sh.components if c.id != op_comp.id]
                    t_sh.components.append(op_comp)
                if idx_t == 0:
                    op_comp.library_id = "iec.connection.offpage"
                    op_comp.properties["role"] = "to"
                else:
                    op_comp.library_id = "iec.connection.offpage_from"
                    op_comp.properties["role"] = "from"
                kept_ops.append((t_sid, op_comp))
                ends_here: list[str] = [f"{op_comp.id}.A"]
                for other_end, _sw, _oid in items:
                    if other_end not in ends_here:
                        ends_here.append(other_end)
                for i in range(len(ends_here) - 1):
                    t_sh.wires.append(
                        Wire(
                            id=_alloc_wire_id(),
                            frm=ends_here[i],
                            to=ends_here[i + 1],
                            net=proto.net,
                            number=proto.number,
                            gauge=proto.gauge,
                            color=proto.color,
                            wire_type=proto.wire_type,
                        )
                    )
            for oid in member_ops:
                if oid not in used_op_ids:
                    osid, ocomp = offpage_by_id[oid]
                    osh = session.sheet(osid)
                    osh.components = [c for c in osh.components if c.id != ocomp.id]
            shared_link = next(
                (
                    str(offpage_by_id[oid][1].properties.get("link") or "").strip()
                    for oid in member_ops
                    if str(offpage_by_id[oid][1].properties.get("link") or "").strip()
                    and ":" not in str(offpage_by_id[oid][1].properties.get("link") or "")
                ),
                "",
            ) or str(proto.net or proto.number or kept_ops[0][1].id)
            for _t_sid, op_comp in kept_ops:
                op_comp.properties["link"] = shared_link

    # Remove empty secondary control sheets
    removed_sids: list[str] = []
    for sid in ctrl_sids:
        if sid not in kept_sids:
            removed_sids.append(sid)
    if removed_sids:
        session.project.sheets = [s for s in session.project.sheets if s.id not in set(removed_sids)]
        if getattr(session, "active_sheet_id", None) in set(removed_sids):
            session.active_sheet_id = target_for_sheet.get(str(session.active_sheet_id)) or (
                kept_sids[0] if kept_sids else session.project.default_sheet().id
            )

    # Relayout each kept consolidated control sheet and then order multi-drop cluster wires top-to-bottom
    for t_sid in kept_sids:
        t_sh = session.sheet(t_sid)
        _relayout_generic_sheet(session, t_sh, "NEMA/ANSI")

    for t_sid, c_wires, ends in same_sheet_cluster_wires:
        if len(c_wires) < 1:
            continue
        t_sh = session.sheet(t_sid)

        def _end_xy(e: str) -> tuple[float, float]:
            xy = _pin_world_xy(session, t_sh, e)
            if xy is None:
                return (999.0, 999.0)
            return (round(xy[1], 1), round(xy[0], 1))

        sorted_ends = sorted(ends, key=_end_xy)
        for idx_w, w in enumerate(c_wires):
            w.frm = sorted_ends[idx_w]
            w.to = sorted_ends[idx_w + 1]
        parallel_ids = {
            c.id
            for c in t_sh.components
            if _is_contact_lib(c.library_id)
            and any(
                abs(float(other.x) - float(c.x)) < 0.5 and float(c.y) > float(other.y) + 5.0
                for other in t_sh.components
                if other.id != c.id and _is_contact_lib(other.library_id)
            )
        }
        _route_generic_branches_and_subbuses(session, t_sh, parallel_ids)

    for t_sid in kept_sids:
        t_sh = session.sheet(t_sid)
        for comp in t_sh.components:
            comp.properties.pop("_src_order", None)
            comp.properties.pop("_orig_x", None)
            comp.properties.pop("_orig_y", None)

    clear_route_cache()
    return {
        "kept_sheets": kept_sids,
        "removed_sheets": removed_sids,
        "target_for_sheet": target_for_sheet,
    }


def sheet_is_target(project: Any, sheet: Any, target: str) -> bool:
    return sheet_drawing_standard(project, sheet) == target_standard(target)


consolidate_multipage_iec = consolidate_control_sheets
