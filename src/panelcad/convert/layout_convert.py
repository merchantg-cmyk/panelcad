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


def needs_column_or_ladder(sheet: Any, parts: list[_Part] | None = None) -> bool:
    """Control ladders and Circuit Builder I/O sheets change layout. Power schematics do not.

    A sheet titled "PLC I/O" with no Circuit Builder roles (the demo I/O page)
    keeps its positions. Only ``drawing_style`` ladder/iec, or an I/O sheet that
    actually has PLC rungs, is placed again.
    """

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
    alias = _nema_alias(session, sheet, target, old_hot, old_ret)
    if std == "IEC" and not columns and kind != "io":
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
    std = target_standard(target)
    alias = _nema_alias(session, sheet, target, old_hot, old_ret)
    hot, ret = _target_rail_pair(session, sheet, target, parts, columns=columns)
    leaving = nema_alias_for(old_hot, old_ret) if std == "IEC" else (alias or nema_alias_for(hot, ret))
    sheet.rail_hot = hot
    sheet.rail_return = ret
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
    if not parts:
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


def sheet_is_target(project: Any, sheet: Any, target: str) -> bool:
    return sheet_drawing_standard(project, sheet) == target_standard(target)
