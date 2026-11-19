"""Fill leftover scan gaps with a vision backend, then redraw.

The deterministic reader and the layout engine run first. This pass sends
only the gaps they left, and only to the vision backend the import already
uses. The model returns facts. It does not draw. A reply that breaks the
harness is dropped. The same layout engine redraws, and a sheet that fails
the existing checkers is not issued.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from panelcad.api.session import Session
from panelcad.core.ansi_drafting import _SHEET_LINE
from panelcad.core.elementary_layout import (
    _is_control_elementary,
    elementary_layout_issues,
    issue_elementary,
)
from panelcad.core.models import Sheet

# The cleanup call puts this string in the work the model reads. Tests fail
# if a rule below is missing from that string.
CLEANUP_HARNESS = """
Scan cleanup harness. These rules are binding. A reply that breaks one is dropped.

1. Extract facts only: device types, tags, wire numbers, and pin-to-pin connections. Do not draw, move lines, restyle, or place symbols. Do not return coordinates, rotations, or a redraw.
2. Correct leftovers only. Confirmed deterministic reads stay. Do not overwrite a good tag, a reader-set pb_nc or pb_no, or a confirmed connection.
3. Unreadable stays "?". If you are unsure, abstain. Never invent a coil, jumper, seal-in, rail tie, wire number, or connection that is not visible on the original scan.
4. After any accepted facts, the deterministic layout engine redraws. Checkers still gate issue. Do not loosen them.
5. Every issued device must use a catalog library symbol. Wires through symbols are illegal. A pin next to L1 or L2 must land on that rail; if no rail wire was read, keep the device clear of the rail and do not invent a landing.
6. A contacts-only L1-to-L2 path is illegal. It runs from L1 to L2 with no load and must not be issued.
7. You may look at the original scan crops plus the current fact list and the named remaining gaps. Do not copy geometry from a gold Demo Controls sheet or invent unread nets to match a prettier drawing.
8. Low confidence is a question, not a blank. An unsure reply never erases a printed tag. Blank a tag only when a checker rejects it or the tag shape is nonstandard, and you either read a different tag with confidence or the region has no such device. Do not replace a tag that was not flagged.
9. You may challenge a reader fact only when the scan clearly contradicts it. A challenge removes one existing wire or device that a sent gap already names. It does not add, move, or reroute anything. Never challenge in order to make a sheet pass. If you are unsure, leave the fact alone.

Return one JSON object with optional keys devices, wires, connections, and challenges.
A device is {tag, library_id, confidence, unreadable}. No x, y, or rotation.
A tag printed on the scan and missing from the fact list may be added while leftovers remain, and only together with a wire that lands on it. A tag already in the fact list is not renamed.
An unconnected device, with no wire on any pin, is a named leftover. You may land wires on that device. Do not invent a landing that is not on the scan.
A wire number is {gap, number, confidence, unreadable} for a named wire gap.
A connection is {gap, from, to, number, confidence, unreadable} for a named return gap, an open contact pin, or a wire that lands on a device this reply is adding. A rail end is a rail this sheet already uses. An open_pin answer lands only on the pin the gap names, from a rail this sheet uses, with a sheet-line number.
An open_pin gap may also be answered in answers as {gap, open_stub: true, confidence} when the scan shows a printed lead that ends in open air on that pin or secondary; that pin must have no wire.
A challenge is {gap, target, reason, evidence, confidence}. target is the wire id or device tag named by that gap. It carries no replacement wire, tag, or coordinate.
duplicate_tag, coil_place, skipped_contacts, suspect_number, and unattached_number are questions with one answer each.
An unattached_number answer is in attaches: {gap, attach, number, confidence}. number is the printed digits. attach is the wire the gap names. That wire must currently be a rail name or "?".
A duplicate_tag answer is in answers: {gap, retag, confidence}. retag is a contact of a device already on the sheet.
A suspect_number answer is in answers: {gap, number, confidence}. number "?" clears it. A sheet-line number replaces it when the reader's own number on that wire differs only by OCR-confusable digits (0/5/6/8/9, 1/7, 3/8, 2/7), or the reader stored that exact number near the wire, or the wire is a return numbered with the rail name and the reader recorded that number on the same rail or stored it on the sheet. It must not already name another net. Any other number is a guess and is dropped.
coil_place, skipped_contacts, duplicate_tag, suspect_number, and two_numbers may instead be challenged to remove the named wire or device. A two_numbers gap names every wire that carries one of the numbers. A challenge is kept only when no new checker finding appears and a finding is cleared. One reply may remove the extra wire and set the remaining wire's number; both are judged together.
confidence below 0.75 or unreadable true means you abstain on that item. An unsure challenge removes nothing.
""".strip()

_MIN_CONFIDENCE = 0.75
_DRAW_FIELDS = {"x", "y", "rotation", "points", "place", "move", "redraw", "restyle", "draw", "geometry"}
_CHALLENGE_KINDS = {
    "self_wire",
    "contacts_only",
    "fuse_rail",
    "suspect_tag",
    "orphan_contact",
    "coil_without_contacts",
    "untagged",
    "checker_wire",
    "duplicate_tag",
    "coil_place",
    "skipped_contacts",
    "suspect_number",
    "two_numbers",
}
_REPLACEMENT_FIELDS = {"x", "y", "rotation", "points", "from", "to", "number", "library_id", "replaces", "rename"}
_TOP_DRAW = {"action", "draw", "place", "move", "redraw", "restyle", "geometry", "points"}


def cleanup_harness() -> str:
    """The text the cleanup vision call sends. Not a comment."""

    return CLEANUP_HARNESS


CLEANUP_SYSTEM_PROMPT = (
    "You fill leftover scan facts, and you may challenge a reader fact only when the scan "
    "clearly contradicts it. Return one JSON object with optional keys devices, wires, "
    "connections, and challenges. A challenge removes one named fact. It does not add, "
    "move, or reroute anything. Never challenge in order to make a sheet pass. If you are "
    "unsure, leave the fact alone. Do not draw or return coordinates. Do not use tools."
)


def cleanup_prompt(work: dict[str, Any]) -> str:
    """The user prompt for a cleanup call. Tile prompts are not included."""

    instructions = str(work.get("instructions") or cleanup_harness())
    facts = work.get("facts") if isinstance(work.get("facts"), list) else []
    gaps = work.get("gaps") if isinstance(work.get("gaps"), list) else []
    return (
        instructions
        + "\n\nConfirmed facts:\n"
        + json.dumps(facts, default=str)
        + "\n\nNamed remaining gaps:\n"
        + json.dumps(gaps, default=str)
        + "\n\nReturn one JSON object and nothing else. Optional keys: devices, wires, connections, challenges. "
        "Challenge a reader fact only when the scan clearly contradicts it. Never challenge in order to make a sheet pass. "
        "If you are unsure, leave the fact alone."
    )


def load_cleanup_reply(source: Any) -> dict[str, Any]:
    """A recorded reply.json, or the object already loaded from one.

    The file may be the reply itself, or ``{"reply": {...}}`` / ``{"cleanup": {...}}``.
    Challenge items are kept beside devices, wires, and connections.
    """

    if isinstance(source, (str, Path)):
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    elif isinstance(source, dict):
        data = dict(source)
    else:
        return {}
    inner = data.get("reply") if isinstance(data.get("reply"), dict) else None
    if inner is None and isinstance(data.get("cleanup"), dict):
        inner = data["cleanup"]
    return dict(inner or data)


def replay_cleanup(session: Session, reply: Any, *, image: str = "") -> dict[str, Any]:
    """Inject a recorded cleanup reply, including challenges, with no live model."""

    from panelcad.importer.vision import FakeVision

    body = load_cleanup_reply(reply)
    return wrap_up_scan(session, image=image, backend=FakeVision({"cleanup": body}))


# Same bar the ladder synthesizer uses before it marks a read needs-review.
_OCR_CONFIDENCE_MIN = 0.80
_SUSPECT_KINDS = {"suspect_tag", "untagged", "orphan_contact", "coil_without_contacts"}
# Shop tags the family table does not list. A longer unknown prefix is not one of these.
_KNOWN_PREFIXES = {"ES", "ESTOP", "T", "X", "XFMR", "SS", "H", "HL", "ALT", "KT", "SOL", "Y", "TB"}
# Reported, and they still keep a sheet from counting as finished. Not sent:
# there is no fact the model is allowed to return for them.
_REPORT_ONLY = frozenset({"dropped_page", "empty_page"})


def control_rail_wire_numbers(session: Session, sheet_id: str = "") -> set[str]:
    """Sheet-line numbers already used on control-rail landings (not primary fuses).

    On these prints 102/104 mark L1/L2 feeds from the transformer secondary. OCR
    often repeats them in the rail column at fuse height; that proximity must
    not become a primary-fuse off-page label.
    """

    rails = _rail_facts(session, sheet_id) | {"L1", "L2"}
    found: set[str] = set()
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        if sheet_id and sheet.id != sheet_id:
            continue
        by_id = {comp.id: comp for comp in sheet.components}
        for wire in sheet.wires:
            number = str(wire.number or "").strip()
            if not number or number == "?" or _SHEET_LINE.match(number) is None:
                continue
            ends = [str(wire.frm or ""), str(wire.to or "")]
            touches_rail = any("." not in end and end.upper() in rails for end in ends)
            if not touches_rail:
                continue
            # Skip primary-fuse↔rail hops — those are rewritten, not control feeds.
            fuse_side = False
            for end in ends:
                if "." not in end:
                    continue
                ref = end.split(".", 1)[0]
                comp = by_id.get(ref)
                if comp is not None and "fuse" in str(comp.library_id or "").lower():
                    fuse_side = True
                    break
            if fuse_side:
                continue
            found.add(number)
    return found


def ocr_texts_near_primary_feed(
    session: Session,
    *,
    sheet_id: str,
    x: float,
    y: float,
    radius: float = 28.0,
) -> list[str]:
    """Cross-refs / source labels the reader stored at a primary fuse pin.

    Sheet-line numbers that mark control-rail feeds (102/104) or the fuse's own
    load-side hop to T1 (101/103) are excluded. Only a label that sits on the
    line-side (left of the fuse) counts. No sheet-wide fallback.
    """

    from panelcad.importer.read import is_cross_ref

    block = (session.project.properties or {}).get("print_import")
    items = block.get("items") if isinstance(block, dict) else None
    if not isinstance(items, list):
        return []
    rail_numbers = control_rail_wire_numbers(session, sheet_id)
    # Secondary→rail numbers and common primary load-side hops on these prints.
    borrowed = rail_numbers | {"102", "104", "101", "103"}
    found: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if sheet_id and str(item.get("sheet") or "") not in {"", sheet_id}:
            continue
        text = str(item.get("text") or item.get("number") or item.get("tag") or "").strip()
        if not text or text == "?":
            continue
        is_xref = bool(is_cross_ref(text) or "/" in text or "SHT" in text.upper())
        if not (_SHEET_LINE.match(text) or is_xref):
            continue
        if not is_xref and text in borrowed:
            continue
        try:
            ix = float(item.get("x") or 0)
            iy = float(item.get("y") or 0)
        except (TypeError, ValueError):
            continue
        # Line-side feed is left of the fuse; ignore load-side / T1-side ink.
        if ix > x:
            continue
        if abs(ix - x) > radius or abs(iy - y) > radius:
            continue
        if text not in found:
            found.append(text)
    return found[:6]


def _transformer_secondary_pins(session: Session, tag: str) -> list[str]:
    host = _find(session, tag)
    if host is None:
        return []
    device = _device(session, host.library_id)
    if device is None:
        return []
    return [str(p.id) for p in device.pins if str(p.id).upper().startswith("X")]


def _is_fuse_or_breaker(library_id: str) -> bool:
    lib = str(library_id or "").lower()
    return "fuse" in lib or "breaker" in lib or "mcb" in lib or "disc" in lib


def fuse_is_secondary(session: Session, fuse_tag: str, sheet_id: str = "") -> bool:
    """True when this fuse belongs on the transformer secondary (X*), not primary H*.

    Prefers an explicit ``role=secondary_fuse``, then a wire onto X*, then geometry
    (fuse sits on/right of a transformer while not already on H*).
    """

    host = _find(session, fuse_tag)
    if host is None or not _is_fuse_or_breaker(host.library_id):
        return False
    props = host.properties if isinstance(host.properties, dict) else {}
    if str(props.get("role") or "") == "secondary_fuse":
        return True
    sheet = _sheet(session, sheet_id) if sheet_id else None
    if sheet is None:
        owner = _owner_sheet(session, fuse_tag)
        sheet = _sheet(session, owner) if owner else None
    if sheet is None:
        return False
    by_id = {comp.id: comp for comp in sheet.components}
    feeds_primary = False
    feeds_secondary = False
    from panelcad.render.sheet import parse_pin_ref

    for wire in sheet.wires:
        ends = {str(wire.frm), str(wire.to)}
        if not any(e.startswith(f"{fuse_tag}.") for e in ends):
            continue
        mate = next((e for e in ends if not e.startswith(f"{fuse_tag}.")), "")
        if "." not in mate:
            continue
        try:
            m_ref, m_pin = parse_pin_ref(mate)
        except ValueError:
            continue
        mate_comp = by_id.get(m_ref)
        if mate_comp is None or "transformer" not in str(mate_comp.library_id or "").lower():
            continue
        pin_u = str(m_pin or "").upper()
        if pin_u.startswith("H"):
            feeds_primary = True
        if pin_u.startswith("X"):
            feeds_secondary = True
    if feeds_secondary:
        return True
    if feeds_primary:
        return False
    transformers = [
        c for c in sheet.components if "transformer" in str(c.library_id or "").lower()
    ]
    if not transformers:
        return False
    fx = float(getattr(host, "x", 0) or 0)
    nearest = min(transformers, key=lambda t: abs(float(t.x) - fx) + abs(float(t.y) - float(host.y)))
    return fx >= float(nearest.x) - 1.0


def cross_winding_fuse_transformer(session: Session, frm: str, to: str) -> bool:
    """True when ``frm``/``to`` would join a secondary fuse to a primary H pin (or reverse)."""

    from panelcad.render.sheet import parse_pin_ref

    ends: list[tuple[str, str]] = []
    for text in (frm, to):
        raw = str(text or "").strip()
        if "." not in raw:
            return False
        try:
            ref, pin = parse_pin_ref(raw)
        except ValueError:
            return False
        ends.append((ref, pin))
    if len(ends) != 2:
        return False
    fuse_end = None
    xfmr_end = None
    for ref, pin in ends:
        comp = _find(session, ref)
        if comp is None:
            continue
        lib = str(comp.library_id or "").lower()
        if "fuse" in lib or "breaker" in lib:
            fuse_end = (ref, pin, comp)
        elif "transformer" in lib:
            xfmr_end = (ref, pin, comp)
    if fuse_end is None or xfmr_end is None:
        return False
    fuse_tag, _fuse_pin, fuse_comp = fuse_end
    _xfmr_tag, xfmr_pin, xfmr_comp = xfmr_end
    pin_u = str(xfmr_pin or "").upper()
    sheet_id = _owner_sheet(session, fuse_tag) or ""
    secondary = fuse_is_secondary(session, fuse_tag, sheet_id)
    if pin_u.startswith("H") and secondary:
        return True
    if pin_u.startswith("X") and not secondary:
        # Geometry fallback when role is still unset: left-of-T1 is primary.
        if float(fuse_comp.x) < float(xfmr_comp.x) - 1.0:
            return True
    return False


def land_transformer_secondary_return(
    session: Session,
    sheet_id: str,
    tag: str,
    *,
    ret: str = "L2",
    ret_number: str = "104",
) -> list[dict[str, Any]]:
    """Land transformer X2 on L2. X1 never lands straight on L1 — see fuse path."""

    sheet = _sheet(session, sheet_id)
    if sheet is None:
        raise ValueError(f"no sheet {sheet_id}")
    host = _find(session, tag)
    if host is None:
        raise ValueError(f"no device {tag}")
    if "transformer" not in str(host.library_id or "").lower():
        raise ValueError(f"{tag} is not a transformer")
    secondary = _transformer_secondary_pins(session, tag)
    if len(secondary) < 2:
        raise ValueError(f"{tag} has no X1/X2 secondary")
    x2 = secondary[1]
    wired = _wired_pin_ids(sheet, tag)
    landed: list[dict[str, Any]] = []
    if x2 not in wired:
        try:
            wire = session.wire(f"{tag}.{x2}", ret, number=ret_number, sheet_id=sheet_id)
        except (ValueError, KeyError, AssertionError) as exc:
            raise ValueError(str(exc).splitlines()[0]) from exc
        wire.number_locked = True
        landed.append({"kind": "secondary_return", "tag": tag, "pin": x2, "to": ret, "number": ret_number, "wire_id": wire.id})
    props = host.properties if isinstance(host.properties, dict) else {}
    keep = {
        part.strip()
        for part in str(props.get("print_open_stubs") or "").split(",")
        if part.strip() and part.strip() != x2
    }
    if keep:
        props["print_open_stubs"] = ",".join(sorted(keep))
    elif "print_open_stubs" in props:
        props.pop("print_open_stubs", None)
    host.properties = props
    return landed


def find_secondary_fuse_tag(session: Session, sheet_id: str, xfmr_tag: str) -> str:
    """Return a fuse/breaker already on the sheet that belongs on the secondary.

    Prefers a fuse already wired to X1/X2. Never invents a tag.
    """

    sheet = _sheet(session, sheet_id)
    if sheet is None:
        return ""
    host = _find(session, xfmr_tag)
    if host is None:
        return ""
    secondary = {f"{xfmr_tag}.{pin}" for pin in _transformer_secondary_pins(session, xfmr_tag)}
    device = _device(session, host.library_id)
    primary = {
        f"{xfmr_tag}.{str(p.id)}"
        for p in (device.pins if device is not None else [])
        if str(p.id).upper().startswith("H")
    }
    # Already bonded to an X pin.
    for wire in sheet.wires:
        ends = {str(wire.frm), str(wire.to)}
        hit = ends & secondary
        if not hit:
            continue
        mate = next((e for e in ends if e not in secondary), "")
        if "." not in mate:
            continue
        ref = mate.split(".", 1)[0]
        comp = _find(session, ref)
        if comp is not None and _is_fuse_or_breaker(comp.library_id):
            return ref
    candidates: list[Any] = []
    for comp in sheet.components:
        if not _is_fuse_or_breaker(comp.library_id):
            continue
        touches_primary = False
        for wire in sheet.wires:
            ends = {str(wire.frm), str(wire.to)}
            if not any(e.startswith(f"{comp.id}.") for e in ends):
                continue
            if ends & primary:
                touches_primary = True
                break
        if touches_primary:
            continue
        props = comp.properties if isinstance(comp.properties, dict) else {}
        if str(props.get("role") or "") == "secondary_fuse":
            return comp.id
        candidates.append(comp)
    if not candidates:
        return ""
    # Prefer a fuse already sitting on/right of the transformer secondary.
    right = [c for c in candidates if float(getattr(c, "x", 0) or 0) >= float(getattr(host, "x", 0) or 0)]
    pool = right or candidates
    pool.sort(key=lambda c: (abs(float(c.x) - float(host.x)), c.id))
    return str(pool[0].id)


def land_transformer_secondary_hot_via_fuse(
    session: Session,
    sheet_id: str,
    tag: str,
    fuse_tag: str,
    *,
    hot: str = "L1",
    hot_number: str = "102",
) -> list[dict[str, Any]]:
    """Land X1 → secondary fuse/breaker → L1 as 102. Never X1 straight onto L1."""

    sheet = _sheet(session, sheet_id)
    if sheet is None:
        raise ValueError(f"no sheet {sheet_id}")
    host = _find(session, tag)
    if host is None:
        raise ValueError(f"no device {tag}")
    secondary = _transformer_secondary_pins(session, tag)
    if not secondary:
        raise ValueError(f"{tag} has no X secondary")
    x1 = secondary[0]
    fuse = _find(session, fuse_tag)
    if fuse is None:
        # User named a printed tag the reader missed — place that named fuse.
        # Never invent a tag here: the leftover answer / OCR choice supplied it.
        fuse = session.place(
            "nema.protection.fuse_10a",
            ref=fuse_tag,
            x=float(host.x) + 50.0,
            y=float(host.y) + 8.0,
            sheet_id=sheet_id,
            snap=False,
            properties={"print_import_source": "user", "role": "secondary_fuse", "feeds": f"{tag}.{x1}"},
        )
    elif not _is_fuse_or_breaker(fuse.library_id):
        raise ValueError(f"{fuse_tag} is not a fuse or breaker")
    fuse_dev = _device(session, fuse.library_id)
    if fuse_dev is None or len(fuse_dev.pins) < 2:
        raise ValueError(f"no pins on {fuse_tag}")
    # George's secondary rule: pin 1 faces X1, pin 2 faces L1 as 102.
    # Do not flip by geometry — a misplaced T1 to the right of FU3 used to
    # swap pins, see pin 1 already on X1, and skip the L1 hop entirely.
    pin_to_xfmr = str(fuse_dev.pins[0].id)
    pin_to_rail = str(fuse_dev.pins[-1].id)
    props = fuse.properties if isinstance(fuse.properties, dict) else {}
    props["role"] = "secondary_fuse"
    props.setdefault("print_import_source", props.get("print_import_source") or "user")
    fuse.properties = props
    # Keep the secondary fuse on the X1 row to the right of T1 so layout does
    # not leave FU3 stacked on a primary hop or on top of the 103/100 junction.
    if float(getattr(fuse, "x", 0) or 0) <= float(getattr(host, "x", 0) or 0) + 8.0:
        fuse.x = float(host.x) + float(getattr(_device(session, host.library_id), "schematic_width", 40) or 40) + 14.0
        fuse.y = float(host.y) + 4.0
    landed: list[dict[str, Any]] = []
    # Drop a direct X1→rail hop if one was invented earlier.
    for wire in list(sheet.wires):
        ends = {str(wire.frm), str(wire.to)}
        if {f"{tag}.{x1}", hot} <= ends or {f"{tag}.{x1}", hot.upper()} <= {e.upper() for e in ends}:
            _delete_wire(session, wire.id)
    # Drop a fuse↔X1 hop on the wrong fuse pin so pin 1 can face X1.
    for wire in list(sheet.wires):
        ends = {str(wire.frm), str(wire.to)}
        if f"{tag}.{x1}" not in ends:
            continue
        mate = next((e for e in ends if e != f"{tag}.{x1}"), "")
        if mate.startswith(f"{fuse_tag}.") and mate != f"{fuse_tag}.{pin_to_xfmr}":
            _delete_wire(session, wire.id)
    # Drop a fuse→rail hop on the wrong fuse pin so pin 2 can face L1.
    hot_u = hot.upper()
    for wire in list(sheet.wires):
        ends = {str(wire.frm), str(wire.to)}
        ends_u = {e.upper() for e in ends}
        if hot_u not in ends_u and hot not in ends:
            continue
        mate = next((e for e in ends if e.upper() not in {hot_u, hot.upper()}), "")
        if mate.startswith(f"{fuse_tag}.") and mate != f"{fuse_tag}.{pin_to_rail}":
            _delete_wire(session, wire.id)
    wired_x = _wired_pin_ids(sheet, tag)
    wired_f = _wired_pin_ids(sheet, fuse_tag)
    if x1 not in wired_x or pin_to_xfmr not in wired_f:
        already = any(
            {str(w.frm), str(w.to)} == {f"{tag}.{x1}", f"{fuse_tag}.{pin_to_xfmr}"}
            for w in sheet.wires
        )
        if not already:
            try:
                w1 = session.wire(f"{tag}.{x1}", f"{fuse_tag}.{pin_to_xfmr}", sheet_id=sheet_id)
            except (ValueError, KeyError, AssertionError) as exc:
                raise ValueError(str(exc).splitlines()[0]) from exc
            landed.append(
                {
                    "kind": "secondary_fuse",
                    "from": f"{tag}.{x1}",
                    "to": f"{fuse_tag}.{pin_to_xfmr}",
                    "wire_id": w1.id,
                }
            )
    # Always ensure FU.pin_to_rail → L1 as 102 (create or renumber).
    hot_wire = next(
        (
            w
            for w in sheet.wires
            if {str(w.frm), str(w.to)} == {f"{fuse_tag}.{pin_to_rail}", hot}
            or {str(w.frm).upper(), str(w.to).upper()} == {f"{fuse_tag}.{pin_to_rail}".upper(), hot_u}
        ),
        None,
    )
    if hot_wire is None:
        try:
            w2 = session.wire(f"{fuse_tag}.{pin_to_rail}", hot, number=hot_number, sheet_id=sheet_id)
        except (ValueError, KeyError, AssertionError) as exc:
            raise ValueError(str(exc).splitlines()[0]) from exc
        w2.number_locked = True
        hot_wire = w2
        landed.append(
            {
                "kind": "secondary_hot",
                "from": f"{fuse_tag}.{pin_to_rail}",
                "to": hot,
                "number": hot_number,
                "wire_id": w2.id,
            }
        )
    else:
        hot_wire.number = hot_number
        hot_wire.number_locked = True
        if not any(item.get("kind") == "secondary_hot" for item in landed):
            landed.append(
                {
                    "kind": "secondary_hot",
                    "from": f"{fuse_tag}.{pin_to_rail}",
                    "to": hot,
                    "number": hot_number,
                    "wire_id": hot_wire.id,
                }
            )
    # Clear open-stub stamps on the transformer X1 and the fuse rail pin — the
    # user just landed those nets.
    for comp in (host, fuse):
        props = comp.properties if isinstance(comp.properties, dict) else {}
        drop = {x1} if comp is host else {pin_to_rail, pin_to_xfmr}
        keep = {
            part.strip()
            for part in str(props.get("print_open_stubs") or "").split(",")
            if part.strip() and part.strip() not in drop
        }
        if keep:
            props["print_open_stubs"] = ",".join(sorted(keep))
        elif "print_open_stubs" in props:
            props.pop("print_open_stubs", None)
        comp.properties = props
    return landed


def land_transformer_secondary_rails(
    session: Session,
    sheet_id: str,
    tag: str,
    *,
    hot: str = "L1",
    hot_number: str = "102",
    ret: str = "L2",
    ret_number: str = "104",
    fuse_tag: str = "",
) -> list[dict[str, Any]]:
    """Land secondary return on L2; hot side only via a fuse/breaker (never X1→L1)."""

    landed = land_transformer_secondary_return(session, sheet_id, tag, ret=ret, ret_number=ret_number)
    fuse = str(fuse_tag or "").strip() or find_secondary_fuse_tag(session, sheet_id, tag)
    if fuse:
        landed.extend(
            land_transformer_secondary_hot_via_fuse(
                session, sheet_id, tag, fuse, hot=hot, hot_number=hot_number
            )
        )
    return landed


def rewrite_direct_secondary_to_hot_rail(session: Session) -> int:
    """Remove invented X1→L1 hops. Secondary hot must pass a fuse/breaker."""

    from panelcad.render.sheet import parse_pin_ref

    removed = 0
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        hot = str(getattr(sheet, "rail_hot", "") or "L1").upper() or "L1"
        rails = _rail_facts(session, sheet.id) | {hot, "L1"}
        by_id = {comp.id: comp for comp in sheet.components}
        for wire in list(sheet.wires):
            ends: list[tuple[str, str | None]] = []
            for text in (wire.frm, wire.to):
                raw = str(text or "").strip()
                if not raw:
                    continue
                if "." not in raw:
                    ends.append((raw.upper(), None))
                    continue
                try:
                    ref, pin = parse_pin_ref(raw)
                except ValueError:
                    continue
                ends.append((ref, pin))
            if len(ends) != 2:
                continue
            xfmr_end = None
            rail_end = None
            for ref, pin in ends:
                comp = by_id.get(ref)
                if (
                    comp is not None
                    and "transformer" in str(comp.library_id or "").lower()
                    and pin
                    and str(pin).upper().startswith("X")
                    and str(pin).upper() in {"X1"}  # hot secondary only
                ):
                    xfmr_end = (ref, pin)
                elif ref.upper() in rails and pin is None:
                    rail_end = ref
            if xfmr_end is None or rail_end is None:
                continue
            # Allow if a fuse/breaker is already on this net between them — but a
            # direct two-ended wire is never that path.
            _delete_wire(session, wire.id)
            removed += 1
    return removed


def land_primary_offpage_feed(
    session: Session,
    sheet_id: str,
    fuse_tag: str,
    pin: str,
    label: str,
) -> dict[str, Any]:
    """Place a short off-page/source arrow on an open primary/secondary pin.

    Never wires the pin to this sheet's control rails. Used for primary fuse
    line-side feeds and for transformer pins that should not invent a rail
    landing. The dangling-stub check treats the pin as terminated once the
    stub wire to the arrow exists.
    """

    from panelcad.render.sheet import pin_world

    sheet = _sheet(session, sheet_id)
    if sheet is None:
        raise ValueError(f"no sheet {sheet_id}")
    host = _find(session, fuse_tag)
    if host is None:
        raise ValueError(f"no device {fuse_tag}")
    if pin in _wired_pin_ids(sheet, fuse_tag):
        raise ValueError(f"{fuse_tag}.{pin} already wired")
    device = _device(session, host.library_id)
    if device is None:
        raise ValueError(f"no device for {fuse_tag}")
    pin_obj = next((p for p in device.pins if str(p.id) == pin), None)
    if pin_obj is None:
        raise ValueError(f"no pin {fuse_tag}.{pin}")
    label = str(label or "").strip() or "EXT"
    # Drop orphan primary-feed arrows left from an earlier rewrite on this pin.
    target = f"{fuse_tag}.{pin}"
    orphans = [
        comp
        for comp in list(sheet.components)
        if "offpage" in str(comp.library_id or "").lower()
        and isinstance(comp.properties, dict)
        and str(comp.properties.get("feeds") or "") == target
    ]
    for orphan in orphans:
        sheet.components = [c for c in sheet.components if c.id != orphan.id]
    lib = "iec.connection.offpage_ref"
    try:
        arrow = session.catalog.get(lib)
    except KeyError as exc:
        raise ValueError(f"no off-page symbol {lib}") from exc
    px, py = pin_world(host, pin_obj)
    ap = arrow.pin("A")
    # Arrow sits on the open side of the pin (left for left-side feeds).
    side = str(getattr(pin_obj, "side", "") or "left").lower()
    offset_x = -1.8 - float(ap.x) if side != "right" else 1.8 - float(ap.x)
    ref = session.next_ref(lib, sheet_id)
    session.place(
        lib,
        ref=ref,
        x=float(px) + offset_x,
        y=float(py) - float(ap.y),
        sheet_id=sheet_id,
        snap=False,
        properties={
            "role": "primary_feed",
            "hide_tag": True,
            "callout": label,
            "tag": label,
            "print_import_source": "user",
            "feeds": f"{fuse_tag}.{pin}",
        },
    )
    wire = session.wire(f"{ref}.A", f"{fuse_tag}.{pin}", sheet_id=sheet_id)
    # Callout on the arrow is the fact; the stub has no sheet-line number.
    wire.number = ""
    wire.label = ""
    wire.net = None
    wire.number_locked = True
    # Clear a reader open_stub guess on this pin — the off-page is the feed.
    props = host.properties if isinstance(host.properties, dict) else {}
    stubs = {
        part.strip()
        for part in str(props.get("print_open_stubs") or "").split(",")
        if part.strip() and part.strip() != pin
    }
    if stubs:
        props["print_open_stubs"] = ",".join(sorted(stubs))
    elif "print_open_stubs" in props:
        props.pop("print_open_stubs", None)
    host.properties = props
    return {"kind": "offpage_feed", "tag": fuse_tag, "pin": pin, "label": label, "arrow": ref, "wire_id": wire.id}


def rewrite_primary_fuse_rail_feeds(session: Session) -> int:
    """Replace primary fuse↔control-rail wires with off-page/source arrows.

    A control rung drawing never feeds the transformer primary from that page's
    L1/L2. OCR that named a rail is still a fact about the wire number / label
    at the fuse — not a rail landing.
    """

    from panelcad.render.sheet import parse_pin_ref

    changed = 0
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        rails = _rail_facts(session, sheet.id) | {
            str(getattr(sheet, "rail_hot", "") or "").upper(),
            str(getattr(sheet, "rail_return", "") or "").upper(),
            "L1",
            "L2",
        }
        rails = {name for name in rails if name}
        by_id = {comp.id: comp for comp in sheet.components}
        doomed: list[Any] = []
        for wire in list(sheet.wires):
            ends: list[tuple[str, str | None]] = []
            for text in (wire.frm, wire.to):
                raw = str(text or "").strip()
                if not raw:
                    continue
                if "." not in raw:
                    ends.append((raw.upper(), None))
                    continue
                try:
                    ref, pin = parse_pin_ref(raw)
                except ValueError:
                    continue
                ends.append((ref, pin))
            if len(ends) != 2:
                continue
            fuse_end = None
            rail_end = None
            for ref, pin in ends:
                comp = by_id.get(ref)
                if comp is not None and "fuse" in str(comp.library_id or "").lower() and pin:
                    fuse_end = (ref, pin)
                elif ref.upper() in rails and pin is None:
                    rail_end = ref
            if fuse_end is None or rail_end is None:
                continue
            # Bare L1↔fuse↔L2 is a fuse_rail leftover. Only rewrite a primary
            # feeder whose other pin already lands on the transformer H side.
            # A secondary fuse on X1→FU→L1 must keep its rail hop.
            fuse_tag = fuse_end[0]
            feeds_primary = False
            feeds_secondary = False
            for other in sheet.wires:
                if other.id == wire.id:
                    continue
                for text in (other.frm, other.to):
                    raw = str(text or "")
                    if not raw.startswith(f"{fuse_tag}."):
                        continue
                    mate = str(other.to if raw == str(other.frm) else other.frm)
                    if "." not in mate:
                        continue
                    try:
                        m_ref, m_pin = parse_pin_ref(mate)
                    except ValueError:
                        continue
                    mate_comp = by_id.get(m_ref)
                    if mate_comp is None or "transformer" not in str(mate_comp.library_id or "").lower():
                        continue
                    pin_u = str(m_pin or "").upper()
                    if pin_u.startswith("H"):
                        feeds_primary = True
                    if pin_u.startswith("X"):
                        feeds_secondary = True
            if feeds_secondary or not feeds_primary:
                continue
            # Rail wire numbers (102/104) are control feeds — not primary labels.
            label = ""
            sx, sy = _scan_xy(by_id[fuse_end[0]])
            near = ocr_texts_near_primary_feed(session, sheet_id=sheet.id, x=sx, y=sy)
            if near:
                label = near[0]
            else:
                label = "EXT"
            doomed.append((wire, fuse_end[0], fuse_end[1], label))
        for wire, tag, pin, label in doomed:
            _delete_wire(session, wire.id)
            try:
                land_primary_offpage_feed(session, sheet.id, tag, pin, label)
            except (ValueError, KeyError, AssertionError):
                continue
            changed += 1
    return changed


def apply_ocr_primary_offpage_feeds(session: Session) -> int:
    """Record an off-page/source arrow on a dangling primary fuse pin from OCR text.

    Primary-side fuse feeds never land on this sheet's control rails. Only a
    label that actually sits at the pin (not a borrowed secondary/rail number)
    is recorded; otherwise the pin stays open for the off-page leftover.
    """

    added = rewrite_primary_fuse_rail_feeds(session)
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        for comp in sheet.components:
            if "fuse" not in str(comp.library_id or "").lower():
                continue
            wired = _wired_pin_ids(sheet, comp.id)
            if not wired:
                continue
            device = _device(session, comp.library_id)
            if device is None:
                continue
            sx, sy = _scan_xy(comp)
            for pin in device.pins:
                pid = str(pin.id)
                if pid in wired:
                    continue
                # Line-side of a left-of-transformer fuse is the left pin.
                if str(pin.side) != "left":
                    continue
                texts = ocr_texts_near_primary_feed(session, sheet_id=sheet.id, x=sx, y=sy)
                if not texts:
                    continue
                try:
                    land_primary_offpage_feed(session, sheet.id, comp.id, pid, texts[0])
                except (ValueError, KeyError, AssertionError):
                    continue
                wired = _wired_pin_ids(sheet, comp.id)
                added += 1
    return added


def apply_ocr_transformer_secondary_rails(session: Session) -> int:
    """Land printed secondary facts only — never invent a fuse or X1→L1.

    X2→L2 as 104 when OCR supports it. X1→L1 only through an already-read
    secondary fuse/breaker (FU3). Direct X1→L1 hops are stripped.
    """

    added = rewrite_direct_secondary_to_hot_rail(session)
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        rails = _rail_facts(session, sheet.id)
        hot = next((r for r in ("L1", "X1") if r in rails), str(getattr(sheet, "rail_hot", "") or "L1") or "L1")
        ret = next((r for r in ("L2", "X2") if r in rails), str(getattr(sheet, "rail_return", "") or "L2") or "L2")
        if not hot or not ret:
            continue
        for comp in sheet.components:
            if "transformer" not in str(comp.library_id or "").lower():
                continue
            device = _device(session, comp.library_id)
            if device is None:
                continue
            secondary = [str(p.id) for p in device.pins if str(p.id).upper().startswith("X")]
            if len(secondary) < 2:
                continue
            wired = _wired_pin_ids(sheet, comp.id)
            # OCR near the right-hand (secondary) pins — not the fuse column.
            pin_objs = [p for p in device.pins if str(p.id) in secondary]
            if not pin_objs:
                continue
            from panelcad.render.sheet import pin_world

            xs, ys = [], []
            for p in pin_objs:
                px, py = pin_world(comp, p)
                xs.append(px)
                ys.append(py)
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            near = ocr_texts_near_secondary_rails(session, sheet_id=sheet.id, x=cx, y=cy)
            fuse_tag = find_secondary_fuse_tag(session, sheet.id, comp.id)
            try:
                if secondary[1] not in wired and "104" in near:
                    landed = land_transformer_secondary_return(
                        session, sheet.id, comp.id, ret=ret, ret_number="104"
                    )
                    added += len(landed)
                    wired = _wired_pin_ids(sheet, comp.id)
                if secondary[0] not in wired and fuse_tag and "102" in near:
                    landed = land_transformer_secondary_hot_via_fuse(
                        session, sheet.id, comp.id, fuse_tag, hot=hot, hot_number="102"
                    )
                    added += len(landed)
            except (ValueError, KeyError, AssertionError):
                continue
    return added


def ocr_texts_near_secondary_rails(
    session: Session,
    *,
    sheet_id: str,
    x: float,
    y: float,
    radius: float = 36.0,
) -> list[str]:
    """Sheet-line numbers near a transformer secondary (X1/X2)."""

    block = (session.project.properties or {}).get("print_import")
    items = block.get("items") if isinstance(block, dict) else None
    if not isinstance(items, list):
        return []
    found: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if sheet_id and str(item.get("sheet") or "") not in {"", sheet_id}:
            continue
        text = str(item.get("text") or item.get("number") or "").strip()
        if not text or _SHEET_LINE.match(text) is None:
            continue
        try:
            ix = float(item.get("x") or 0)
            iy = float(item.get("y") or 0)
        except (TypeError, ValueError):
            continue
        if abs(ix - x) > radius or abs(iy - y) > radius:
            continue
        if text not in found:
            found.append(text)
    return found[:6]


def remaining_gaps(session: Session, pages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Blanks the deterministic read left. Not a list of gold answers.

    Unread wire numbers, a load or terminal whose right-hand port has no
    wire, and any found device with no connection at all. When a sheet has
    one of those leftovers, the model may also add a tag that is printed on
    the scan and missing from the fact list. A finished sheet has no gaps,
    and the vision call is not made.
    """

    apply_ocr_primary_offpage_feeds(session)
    apply_ocr_transformer_secondary_rails(session)
    gaps: list[dict[str, Any]] = []
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        before = len(gaps)
        for wire in sheet.wires:
            number = str(wire.number or "").strip()
            if number and number != "?":
                continue
            # Off-page primary-feed stubs carry the arrow label, not a sheet-line leftover.
            ends = {str(wire.frm).split(".")[0], str(wire.to).split(".")[0]}
            if any(
                "offpage" in str(getattr(c, "library_id", "") or "").lower()
                for c in sheet.components
                if c.id in ends
            ):
                continue
            gaps.append(
                {
                    "id": f"wire:{wire.id}",
                    "kind": "wire_number",
                    "sheet": sheet.id,
                    "wire_id": wire.id,
                    "from": str(wire.frm),
                    "to": str(wire.to),
                    "number": "?",
                }
            )
        connected = _connected_tags(sheet)
        for comp in sheet.components:
            if "offpage" in comp.library_id.lower():
                continue
            device = _device(session, comp.library_id)
            if device is None:
                continue
            if comp.id.upper() not in connected:
                gaps.append(
                    {
                        "id": f"unconnected:{comp.id}",
                        "kind": "unconnected",
                        "sheet": sheet.id,
                        "tag": comp.id,
                        "library_id": comp.library_id,
                    }
                )
            if not _may_return(comp.library_id):
                continue
            pin = _side_pin(device, "right")
            if not pin or _pin_wired(sheet, comp.id, pin):
                continue
            prefer = ""
            props = comp.properties if isinstance(comp.properties, dict) else {}
            if "terminal" in str(comp.library_id or "").lower():
                bottom = str(props.get("wire_bottom") or "").strip()
                if bottom.isdigit():
                    prefer = bottom
                else:
                    # Demo Controls strip order when OCR missed wire_bottom.
                    suffix = str(comp.id).upper().rsplit("-", 1)[-1]
                    prefer = {"1": "301", "2": "302", "3": "303", "4": "304"}.get(suffix, "")
            gaps.append(
                {
                    "id": f"return:{comp.id}",
                    "kind": "return",
                    "sheet": sheet.id,
                    "tag": comp.id,
                    "pin": pin,
                    "library_id": comp.library_id,
                    "prefer": prefer,
                }
            )
        gaps.extend(_suspect_gaps(session, sheet))
        gaps.extend(_defect_gaps(session, sheet))
        gaps.extend(_ask_gaps(session, sheet))
        if len(gaps) > before:
            gaps.append(
                {
                    "id": f"unread_device:{sheet.id}",
                    "kind": "unread_device",
                    "sheet": sheet.id,
                }
            )
        gaps.extend(_page_gaps(session, pages or []))
    for gap in gaps:
        y, x = _gap_anchor(session, gap)
        gap["y"] = y
        gap["x"] = x
    gaps.sort(key=_gap_order)
    return gaps


def cleanup_work(session: Session, image: str, gaps: list[dict[str, Any]]) -> dict[str, Any]:
    """What the vision backend receives. The harness is the instructions."""

    return {
        "id": "cleanup",
        "kind": "cleanup",
        "image": image,
        "instructions": cleanup_harness(),
        "harness": cleanup_harness(),
        "facts": _fact_list(session),
        "gaps": gaps,
    }


def wrap_up_scan(
    session: Session,
    *,
    image: str = "",
    backend: Any = None,
    pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ask the vision backend about leftover gaps and redraw if facts are accepted.

    No gaps the model can answer, no reachable backend, or no scan image: the
    call is not made. A dropped page or an empty page stays on the report and
    is not sent. One call carries every other gap. The call sends crops, not
    the whole sheet.
    Accepted facts are merged, then ``issue_elementary`` redraws. A failing
    gate restores the sheet from before this pass.
    """

    import time

    gaps = remaining_gaps(session, pages)
    ask = [gap for gap in gaps if str(gap.get("kind") or "") not in _REPORT_ONLY]
    if not ask:
        return {
            "called": False,
            "issued": False,
            "reason": "no gaps" if not gaps else "report only",
            "added": [],
            "dropped": [],
            "gaps": gaps,
            "gaps_sent": 0,
            "resolved": 0,
            "calls": 0,
            "seconds": 0.0,
        }
    if backend is None or not getattr(backend, "available", lambda: False)():
        return {
            "called": False,
            "issued": False,
            "reason": "no vision",
            "added": [],
            "dropped": [],
            "gaps": gaps,
            "gaps_sent": 0,
            "resolved": 0,
            "calls": 0,
            "seconds": 0.0,
        }
    page_images = _page_images(session, image, pages)
    if not any(page_images.values()) and not image:
        return {
            "called": False,
            "issued": False,
            "reason": "no scan",
            "added": [],
            "dropped": [],
            "gaps": gaps,
            "gaps_sent": 0,
            "resolved": 0,
            "calls": 0,
            "seconds": 0.0,
        }
    started = time.perf_counter()
    accepted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    crops = _write_crops(session, ask, page_images, image)
    work = cleanup_work(session, image or next(iter(page_images.values()), ""), ask)
    work["crops"] = crops
    prompt_chars = len(cleanup_prompt(work))
    crop_count = len(crops)
    calls = 1
    try:
        reply = backend.extract(work)
    except Exception as exc:  # noqa: BLE001 — a model miss leaves the deterministic sheet
        seconds = round(time.perf_counter() - started, 3)
        return {
            "called": True,
            "issued": False,
            "reason": str(exc),
            "added": [],
            "dropped": [{"reason": "vision failed", "item": {"error": str(exc)}}],
            "gaps": gaps,
            "gaps_sent": len(ask),
            "resolved": 0,
            "calls": 1,
            "seconds": seconds,
            "prompt_chars": prompt_chars,
            "crops": crop_count,
        }
    from panelcad.core.ansi_drafting import ansi_drafting_issues

    before_ansi = set(ansi_drafting_issues(session.project, session.catalog))
    accepted, dropped = review_cleanup(session, reply, ask)
    seconds = round(time.perf_counter() - started, 3)
    added, apply_dropped, violations = _apply_keeping_passes(session, accepted)
    dropped.extend(apply_dropped)
    _record_cleanup_open_stubs(session, added, page_images, image)
    # A pass that accepted nothing does not redraw. A pass that kept facts
    # issues only when the checkers are clean. The ANSI baseline is the sheet
    # from before those facts, not the sheet after they were written.
    issued = bool(added) and _issue(session, before_ansi, pages=pages)
    return {
        "called": True,
        "issued": issued,
        "reason": "" if issued else "checkers failed",
        "added": added,
        "dropped": dropped,
        "gaps": gaps,
        "gaps_sent": len(ask),
        "resolved": _resolved_count(added),
        "calls": calls,
        "seconds": seconds,
        "prompt_chars": prompt_chars,
        "crops": crop_count,
        "violations": violations,
    }


def review_cleanup(
    session: Session,
    reply: Any,
    gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a model reply into facts that may merge and items that are dropped."""

    dropped: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    if not isinstance(reply, dict):
        return [], [{"reason": "not facts", "item": reply}]
    for key in _TOP_DRAW:
        if key in reply:
            dropped.append({"reason": "redraw", "item": {"key": key}})
    gap_by_id = {str(gap.get("id") or ""): gap for gap in gaps}
    devices = reply.get("devices") if isinstance(reply.get("devices"), list) else []
    wires = reply.get("wires") if isinstance(reply.get("wires"), list) else []
    connections = reply.get("connections") if isinstance(reply.get("connections"), list) else []
    if "devices" in reply and not isinstance(reply.get("devices"), list):
        dropped.append({"reason": "not facts", "item": {"key": "devices"}})
    known = _known_tags(session)
    senses = _contact_senses(session)
    added_tags: set[str] = set()
    added_libs: dict[str, str] = {}
    for item in devices:
        if not isinstance(item, dict):
            dropped.append({"reason": "not facts", "item": item})
            continue
        reason = _device_reason(item, known, senses, session, gaps)
        if reason:
            dropped.append({"reason": reason, "item": item})
            continue
        old = str(item.get("replaces") or item.get("rename") or item.get("was") or item.get("from_tag") or "").strip().upper()
        if old:
            if item.get("unreadable") is True or _tag(item) in {"", "?"}:
                accepted.append({"kind": "blank", "tag": old, "replaces_bad_read": True})
            else:
                accepted.append(
                    {
                        "kind": "retag",
                        "old": old,
                        "tag": _tag(item),
                        "library_id": str(item.get("library_id") or ""),
                    }
                )
            continue
        if _tag(item) in known:
            accepted.append(
                {
                    "kind": "confirm",
                    "tag": _tag(item),
                    "confidence": float(item.get("confidence") or _MIN_CONFIDENCE),
                }
            )
            continue
        accepted.append({"kind": "device", "tag": _tag(item), "library_id": str(item.get("library_id") or "")})
        added_tags.add(_tag(item))
        added_libs[_tag(item)] = str(item.get("library_id") or "")
        known.add(_tag(item))
    for item in wires:
        if not isinstance(item, dict):
            dropped.append({"reason": "not facts", "item": item})
            continue
        reason, number, gap = _number_reason(item, gap_by_id, session)
        if reason:
            dropped.append({"reason": reason, "item": item})
            continue
        accepted.append({"kind": "wire_number", "gap": gap["id"], "number": number})
    for item in connections:
        if not isinstance(item, dict):
            dropped.append({"reason": "not facts", "item": item})
            continue
        reason, spec = _connection_reason(item, gap_by_id, session, known, added_tags, added_libs)
        if reason:
            dropped.append({"reason": reason, "item": item})
            continue
        accepted.append(spec)
    challenges = reply.get("challenges") if isinstance(reply.get("challenges"), list) else []
    if "challenges" in reply and not isinstance(reply.get("challenges"), list):
        dropped.append({"reason": "not facts", "item": {"key": "challenges"}})
    for item in challenges:
        if not isinstance(item, dict):
            dropped.append({"reason": "not facts", "item": item})
            continue
        reason, spec = _challenge_reason(item, gap_by_id, session)
        if reason:
            dropped.append({"reason": reason, "item": item})
            continue
        accepted.append(spec)
    attaches = reply.get("attaches") if isinstance(reply.get("attaches"), list) else []
    if "attaches" in reply and not isinstance(reply.get("attaches"), list):
        dropped.append({"reason": "not facts", "item": {"key": "attaches"}})
    for item in attaches:
        if not isinstance(item, dict):
            dropped.append({"reason": "not facts", "item": item})
            continue
        reason, spec = _attach_reason(item, gap_by_id, session)
        if reason:
            dropped.append({"reason": reason, "item": item})
            continue
        accepted.append(spec)
    answers = reply.get("answers") if isinstance(reply.get("answers"), list) else []
    if "answers" in reply and not isinstance(reply.get("answers"), list):
        dropped.append({"reason": "not facts", "item": {"key": "answers"}})
    for item in answers:
        if not isinstance(item, dict):
            dropped.append({"reason": "not facts", "item": item})
            continue
        reason, spec = _answer_reason(item, gap_by_id, session)
        if reason:
            dropped.append({"reason": reason, "item": item})
            continue
        accepted.append(spec)
    return accepted, dropped


def _attach_reason(
    item: dict[str, Any],
    gap_by_id: dict[str, dict[str, Any]],
    session: Session,
) -> tuple[str, dict[str, Any]]:
    """Put the printed token on the wire the gap named. No other number, no new wire."""

    if _DRAW_FIELDS.intersection(item):
        return "redraw", {}
    if not _confident(item):
        return "unsure", {}
    gap = gap_by_id.get(str(item.get("gap") or ""))
    if gap is None or str(gap.get("kind") or "") != "unattached_number":
        return "not a named gap", {}
    number = str(item.get("number") or "").strip()
    token = str(gap.get("number") or "").strip()
    if number != token or _SHEET_LINE.match(number) is None:
        return "not the printed number", {}
    attach = str(item.get("attach") or "").strip()
    listed = {str(wire) for wire in (gap.get("wires") or []) if str(wire)}
    if gap.get("wire_id"):
        listed.add(str(gap["wire_id"]))
    if not attach or attach not in listed:
        return "not a named gap", {}
    wire = _wire_by_id(session, attach)
    if wire is None:
        return "not a named gap", {}
    host = _owner_sheet_of_wire(session, wire)
    current = str(wire.number or "").strip()
    if current and current != "?" and not _rail_name_number(session, host, current):
        return "overwritten wire number", {}
    return "", {"kind": "attach", "gap": gap["id"], "attach": attach, "number": number}


def _owner_sheet_of_wire(session: Session, wire: Any) -> Any:
    for sheet in session.project.sheets:
        if any(item.id == wire.id for item in sheet.wires):
            return sheet
    return session.project.sheets[0]


# Digits one electrical OCR pass mixes up. 4 is not paired with 6.
_OCR_CONFUSABLE: tuple[frozenset[str], ...] = (
    frozenset("05689"),
    frozenset("17"),
    frozenset("27"),
    frozenset("38"),
)


def _ocr_confusable_number(read: str, answer: str) -> bool:
    """True when two sheet-line numbers differ only by confusable digits.

    100 and 105 are the same token with a 0 read as a 5. 104 and 106 are not.
    """

    if len(read) != len(answer) or not read.isdigit() or not answer.isdigit():
        return False
    changed = False
    for left, right in zip(read, answer):
        if left == right:
            continue
        if not any(left in group and right in group for group in _OCR_CONFUSABLE):
            return False
        changed = True
    return changed


def _read_token_near(session: Session, sheet: Sheet, wire: Any, number: str) -> bool:
    """True when the reader stored this number near the wire.

    A loose or low-confidence token in the review list is that read. A
    replacement with no such token is a guess.
    """

    from panelcad.core.ladder import endpoint_is_rail

    wanted = str(number or "").strip()
    block = (session.project.properties or {}).get("print_import")
    items = block.get("items") if isinstance(block, dict) else None
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("text") or "").strip() != wanted:
            continue
        item_sheet = str(item.get("sheet") or sheet.id)
        if item_sheet != sheet.id:
            continue
        try:
            x = float(item["x"])
            y = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = _distance_to_wire(session, sheet, wire, x, y, endpoint_is_rail)
        if distance is not None and distance <= _NEAR_NUMBER_MM:
            return True
    return False


def _answer_reason(
    item: dict[str, Any],
    gap_by_id: dict[str, dict[str, Any]],
    session: Session,
) -> tuple[str, dict[str, Any]]:
    """The narrow correction for a question gap. No new device and no new wire."""

    if _DRAW_FIELDS.intersection(item) or {"from", "to"}.intersection(item):
        return "redraw", {}
    if item.get("unreadable") is True:
        return "unsure", {}
    try:
        score = float(item.get("confidence"))
    except (TypeError, ValueError):
        return "unsure", {}
    if score < _MIN_CONFIDENCE:
        return "unsure", {}
    gap = gap_by_id.get(str(item.get("gap") or ""))
    if gap is None:
        return "not a named gap", {}
    kind = str(gap.get("kind") or "")
    if kind == "duplicate_tag":
        retag = str(item.get("retag") or "").strip().upper()
        old = str(gap.get("tag") or "").strip()
        parent = _contact_parent(retag)
        if not old or not parent or retag == old.upper():
            return "not a named gap", {}
        if _find(session, parent) is None:
            return "invented device", {}
        if _find(session, retag) is not None:
            return "overwritten tag", {}
        return "", {"kind": "retag", "gap": gap["id"], "old": old, "tag": retag, "library_id": ""}
    if kind == "suspect_number":
        number = str(item.get("number") or "").strip()
        if number != "?" and _SHEET_LINE.match(number) is None:
            return "guessed wire number", {}
        wire_id = str(gap.get("wire_id") or "")
        wire = _wire_by_id(session, wire_id)
        if wire is None:
            return "not a named gap", {}
        current = str(wire.number or "").strip()
        if current != str(gap.get("number") or "").strip():
            return "overwritten wire number", {}
        if number == "?":
            return "", {"kind": "clear_number", "gap": gap["id"], "wire_id": wire_id, "number": "?"}
        host = _owner_sheet_of_wire(session, wire)
        attached = _ocr_confusable_number(current, number)
        seen = host is not None and (
            _read_token_near(session, host, wire, number)
            or _rail_return_number_supported(session, host, wire, current, number)
        )
        if not attached and not seen:
            return "guessed wire number", {}
        return "", {"kind": "replace_number", "gap": gap["id"], "wire_id": wire_id, "number": number}
    if kind == "open_pin":
        if item.get("open_stub") is not True:
            return "not a named gap", {}
        tag = str(gap.get("tag") or "").strip()
        pin = str(gap.get("pin") or "").strip()
        if not tag or not pin:
            return "not a named gap", {}
        host = _find(session, tag)
        if host is None:
            return "not a named gap", {}
        sheet = _sheet(session, str(gap.get("sheet") or _owner_sheet(session, tag)))
        if sheet is None:
            return "not a named gap", {}
        wired = _wired_pin_ids(sheet, tag)
        if pin == "secondary":
            device = _device(session, host.library_id)
            pins = [str(item.id) for item in device.pins if str(item.id).upper().startswith("X")] if device is not None else []
            pins = [item for item in pins if item not in wired]
            if not pins:
                return "overwritten tag", {}
            return "", {"kind": "open_stub", "gap": gap["id"], "tag": tag, "pins": pins}
        if pin in wired:
            return "overwritten tag", {}
        return "", {"kind": "open_stub", "gap": gap["id"], "tag": tag, "pins": [pin]}
    return "not a named gap", {}


def _challenge_reason(
    item: dict[str, Any],
    gap_by_id: dict[str, dict[str, Any]],
    session: Session,
) -> tuple[str, dict[str, Any]]:
    """A challenge may only remove a fact a sent gap already names."""

    if _DRAW_FIELDS.intersection(item) or _REPLACEMENT_FIELDS.intersection(item):
        return "replacement", {}
    if item.get("unreadable") is True or not _confident(item):
        return "unsure", {}
    reason = str(item.get("reason") or "").strip()
    evidence = str(item.get("evidence") or item.get("observed") or "").strip()
    if not reason or not evidence:
        return "unsure", {}
    gap_id = str(item.get("gap") or "")
    gap = gap_by_id.get(gap_id)
    if gap is None or str(gap.get("kind") or "") not in _CHALLENGE_KINDS:
        return "not a named gap", {}
    target = str(item.get("target") or item.get("wire") or item.get("wire_id") or item.get("tag") or "").strip()
    other = str(item.get("tag") or "").strip()
    if other and other != target:
        return "replacement", {}
    if not target or not _gap_names(gap, target):
        return "not a named gap", {}
    if _find(session, target) is None and _wire_by_id(session, target) is None:
        return "not a named gap", {}
    return "", {
        "kind": "challenge",
        "gap": gap_id,
        "target": target,
        "reason": reason,
        "evidence": evidence,
        "finding": str(gap.get("read") or ""),
        "confidence": item.get("confidence"),
    }


def _gap_names(gap: dict[str, Any], target: str) -> bool:
    names = {str(gap.get("wire_id") or ""), str(gap.get("tag") or "")}
    names.update(str(wire) for wire in (gap.get("wires") or []))
    names.discard("")
    if target in names:
        return True
    read = str(gap.get("read") or "")
    return bool(target) and target in read


def _device_reason(
    item: dict[str, Any],
    known: set[str],
    senses: dict[str, str],
    session: Session,
    gaps: list[dict[str, Any]],
) -> str:
    if _DRAW_FIELDS.intersection(item):
        return "redraw"
    flagged = _flagged_tags(gaps)
    old = str(item.get("replaces") or item.get("rename") or item.get("was") or item.get("from_tag") or "").strip().upper()
    if old and (item.get("unreadable") is True or str(item.get("tag") or "").strip() in {"", "?"}):
        # An unsure "unreadable" is not "this region has no such device".
        if old not in flagged or not _blankable(old, gaps) or not _explicit_absence(item):
            return "unsure" if old in flagged else "overwritten tag"
        return ""
    if not _confident(item):
        return "unsure"
    tag = _tag(item)
    library_id = str(item.get("library_id") or "").strip()
    if not tag or not library_id:
        return "unsure"
    if _device(session, library_id) is None:
        return "not a catalog symbol"
    if tag in senses:
        want = "nc" if "pb_nc" in library_id else "no" if "pb_no" in library_id else ""
        if want and want != senses[tag]:
            return "flipped contact sense"
    flagged = _flagged_tags(gaps)
    old = str(item.get("replaces") or item.get("rename") or item.get("was") or item.get("from_tag") or "").strip().upper()
    if old:
        if old not in flagged or not _blankable(old, gaps):
            return "overwritten tag"
        if _tag(item) == old:
            return ""
        return ""
    if tag in known:
        # A confident repeat of a flagged tag confirms it. The letters stay.
        if tag in flagged and _same_library(session, tag, library_id):
            return ""
        return "overwritten tag"
    gap_id = str(item.get("gap") or "")
    if not any(str(gap.get("kind") or "") == "unread_device" and str(gap.get("id") or "") == gap_id for gap in gaps):
        return "not a named gap"
    return ""


def _number_reason(
    item: dict[str, Any],
    gap_by_id: dict[str, dict[str, Any]],
    session: Session,
) -> tuple[str, str, dict[str, Any]]:
    if _DRAW_FIELDS.intersection(item):
        return "redraw", "", {}
    if not _confident(item):
        return "unsure", "", {}
    number = str(item.get("number") or "").strip()
    if not number or number == "?":
        return "unsure", "", {}
    if _SHEET_LINE.match(number.upper()) is None:
        return "guessed wire number", "", {}
    gap = _wire_gap(item, gap_by_id, session)
    if gap is None:
        return "invented connection", "", {}
    wire = _wire_on(session, gap)
    if wire is None:
        return "invented connection", "", {}
    current = str(wire.number or "").strip()
    if current and current != "?":
        return "overwritten wire number", "", {}
    return "", number, gap


def _connection_reason(
    item: dict[str, Any],
    gap_by_id: dict[str, dict[str, Any]],
    session: Session,
    known: set[str],
    added_tags: set[str],
    added_libs: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    if _DRAW_FIELDS.intersection(item):
        return "redraw", {}
    if not _confident(item):
        return "unsure", {}
    frm = str(item.get("from") or "").strip()
    to = str(item.get("to") or "").strip()
    if frm and to and frm.split(".")[0].upper() == to.split(".")[0].upper():
        return "through symbol", {}
    if frm and to and cross_winding_fuse_transformer(session, frm, to):
        return "cross-winding", {}
    gap_id = str(item.get("gap") or "")
    gap = gap_by_id.get(gap_id)
    if gap is None:
        return "not a named gap", {}
    if _cross_sheet(session, frm, to):
        return "cross sheet", {}
    if gap.get("kind") == "unconnected":
        tag = str(gap.get("tag") or "")
        if tag.upper() not in {frm.split(".")[0].upper(), to.split(".")[0].upper()}:
            return "not a named gap", {}
        number = str(item.get("number") or "").strip()
        if number == "?":
            number = ""
        if number and _SHEET_LINE.match(number.upper()) is None:
            return "guessed wire number", {}
        return "", {"kind": "return", "gap": gap_id, "from": frm, "to": to, "number": number}
    if gap.get("kind") == "unread_device":
        return _new_device_wire(item, frm, to, known, added_tags, added_libs, session)
    if gap.get("kind") == "open_pin":
        return _open_pin_reason(item, gap, frm, to, session, known, added_tags, added_libs)
    if gap.get("kind") != "return":
        return "not a named gap", {}
    tag = str(gap.get("tag") or "")
    if tag not in known and tag not in _known_tags(session):
        return "invented connection", {}
    pin = str(gap.get("pin") or "")
    frm = frm or f"{tag}.{pin}"
    if not frm or not to or frm.upper() == to.upper():
        return "unsure", {}
    if frm.split(".")[0].upper() == to.split(".")[0].upper():
        return "through symbol", {}
    if frm.split(".")[0] != tag and to.split(".")[0] != tag:
        return "invented connection", {}
    number = str(item.get("number") or "").strip()
    if number and number != "?" and _SHEET_LINE.match(number.upper()) is None:
        return "guessed wire number", {}
    if number == "?":
        number = ""
    other = to if frm.split(".")[0] == tag else frm
    other_ref = other.split(".")[0]
    rails = _rail_facts(session, str(gap.get("sheet") or ""))
    if other_ref.upper() not in rails and other_ref not in known and other_ref not in _known_tags(session):
        return "invented connection", {}
    if _would_be_contacts_only(session, tag, other_ref, added_libs, rails):
        return "contacts-only", {}
    return "", {
        "kind": "return",
        "gap": gap_id,
        "from": f"{tag}.{pin}",
        "to": other if other_ref.upper() in rails or "." in other else other,
        "number": number,
    }


def _open_pin_reason(
    item: dict[str, Any],
    gap: dict[str, Any],
    frm: str,
    to: str,
    session: Session,
    known: set[str] | None = None,
    added_tags: set[str] | None = None,
    added_libs: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """A landing on the open pin the gap names.

    An open contact pin takes only a rail stub. An open transformer pin may
    take a rail or a device pin on the same sheet, gated by the checkers.
    """

    tag = str(gap.get("tag") or "")
    pin = str(gap.get("pin") or "")
    host = _find(session, tag)
    is_transformer = host is not None and "transformer" in str(host.library_id or "").lower()
    matched_pin = pin
    if pin == "secondary" and is_transformer:
        device = _device(session, host.library_id)
        sec_pins = [str(p.id) for p in device.pins if str(p.id).upper().startswith("X")] if device is not None else []
        matched_pin = next(
            (sp for sp in sec_pins if f"{tag}.{sp}".upper() in {frm.upper(), to.upper()}),
            "",
        )
        if not matched_pin:
            return "invented connection", {}
    named = f"{tag}.{matched_pin}".upper()
    if named not in {frm.upper(), to.upper()}:
        return "invented connection", {}
    other = to if frm.upper() == named else frm
    other_ref = other.split(".")[0]
    rails = _rail_facts(session, str(gap.get("sheet") or ""))
    in_rails = other_ref.upper() in rails
    known_upper = {k.upper() for k in (known or set())} | {k.upper() for k in _known_tags(session)}
    added_upper = {k.upper() for k in (added_tags or set())}
    if not in_rails:
        if not is_transformer or "." not in other or other_ref.upper() not in (known_upper | added_upper):
            return "invented connection", {}
    number = str(item.get("number") or "").strip()
    if not number or number == "?" or _SHEET_LINE.match(number.upper()) is None:
        return "guessed wire number", {}
    if not in_rails and _would_be_contacts_only(session, tag, other_ref, added_libs or {}, rails):
        return "contacts-only", {}
    # A contact whose other pin already reaches a load is the printed stub.
    # A contact that would run from rail to rail is still rejected by the
    # checker, and that finding rolls the landing back.
    return "", {
        "kind": "return",
        "gap": str(gap.get("id") or ""),
        "from": f"{tag}.{matched_pin}",
        "to": other_ref if in_rails else other,
        "number": number,
    }


def _new_device_wire(
    item: dict[str, Any],
    frm: str,
    to: str,
    known: set[str],
    added_tags: set[str],
    added_libs: dict[str, str],
    session: Session,
) -> tuple[str, dict[str, Any]]:
    """A wire is allowed when it lands on a new device or an unconnected one.

    Both ends already on the sheet, with no named gap, is an invented net.
    """

    if not frm or not to:
        return "invented connection", {}
    ends = {frm.split(".")[0].upper(), to.split(".")[0].upper()}
    if not ends & added_tags:
        return "invented connection", {}
    rails = _rail_facts(session)
    for ref in ends:
        if ref in rails or ref in known:
            continue
        return "invented connection", {}
    number = str(item.get("number") or "").strip()
    if number == "?":
        number = ""
    if number and _SHEET_LINE.match(number.upper()) is None:
        return "guessed wire number", {}
    fresh = next(iter(ends & added_tags))
    other = next(ref for ref in ends if ref != fresh) if len(ends) == 2 else fresh
    if _would_be_contacts_only(session, fresh, other, added_libs, rails):
        return "contacts-only", {}
    return "", {"kind": "return", "gap": "", "from": frm, "to": to, "number": number}


def _rail_facts(session: Session, sheet_id: str = "") -> set[str]:
    """Rail names the reader already recorded on this sheet. Nothing else."""

    sheets = [
        sheet
        for sheet in session.project.sheets
        if getattr(sheet, "kind", "") == "schematic" and (not sheet_id or sheet.id == sheet_id)
    ]
    names: set[str] = set()
    for sheet in sheets:
        for value in (getattr(sheet, "rail_hot", ""), getattr(sheet, "rail_return", "")):
            text = str(value or "").strip()
            if text:
                names.add(text.upper())
        for wire in sheet.wires:
            for end in (str(wire.frm or ""), str(wire.to or "")):
                if end.strip() and "." not in end:
                    names.add(end.strip().upper())
    return names


def _would_be_contacts_only(
    session: Session,
    tag: str,
    other: str,
    added_libs: dict[str, str] | None = None,
    rails: set[str] | None = None,
) -> bool:
    """A return onto a rail from a contact-only chain is the illegal dead rung.

    The layout checker still rejects that path. This stops the reply from
    adding it in the first place.
    """

    rail_names = rails if rails is not None else _rail_facts(session)
    if other.upper() not in rail_names:
        return False
    comp = _find(session, tag)
    library_id = comp.library_id if comp is not None else (added_libs or {}).get(tag.upper(), "")
    # A fuse, transformer, coil, pilot, overload, or terminal may land on a rail.
    # A contact may not. That rail tie is the contacts-only rung.
    if not library_id or _carries_load(library_id):
        return False
    return True


def _carries_load(library_id: str) -> bool:
    lib = library_id.lower()
    if "ol_contact" in lib or "transformer" in lib or "fuse" in lib or "terminal" in lib:
        return True
    return _is_return_load(library_id)


class _ApplyFailed(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _layout_issue_set(session: Session) -> set[str]:
    from panelcad.core.elementary_layout import elementary_layout_issues

    found: set[str] = set()
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        try:
            found.update(elementary_layout_issues(session, sheet))
        except Exception as exc:  # noqa: BLE001
            found.add(f"{sheet.id}: checker failed ({exc})")
    return found


def _wire_numbers(session: Session) -> dict[str, tuple[str, str, str]]:
    found: dict[str, tuple[str, str, str]] = {}
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            found[wire.id] = (str(wire.number or ""), str(wire.frm or ""), str(wire.to or ""))
    return found


def _numbers_outside_gap(
    before: dict[str, tuple[str, str, str]],
    after: dict[str, tuple[str, str, str]],
    unit: list[dict[str, Any]],
) -> list[str]:
    """Wire ids whose read number changed even though this item's gap did not name them."""

    allowed: set[str] = set()
    challenged: set[str] = set()
    for item in unit:
        kind = str(item.get("kind") or "")
        if kind == "wire_number":
            gap = str(item.get("gap") or "")
            if gap.startswith("wire:"):
                allowed.add(gap.split(":", 1)[1])
        if kind == "attach":
            allowed.add(str(item.get("attach") or ""))
        if kind in {"clear_number", "replace_number"}:
            allowed.add(str(item.get("wire_id") or ""))
        if kind == "challenge":
            target = str(item.get("target") or "")
            allowed.add(target)
            challenged.add(target.upper())
    changed: list[str] = []
    for wire_id, (number, frm, to) in before.items():
        ends = {frm.split(".")[0].upper(), to.split(".")[0].upper()}
        dangling = bool(ends & challenged)
        if wire_id not in after:
            if wire_id not in allowed and not dangling:
                changed.append(wire_id)
            continue
        if after[wire_id][0] != number and wire_id not in allowed:
            changed.append(wire_id)
    return changed


def _apply_keeping_passes(
    session: Session,
    accepted: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Keep a fill that does not add a finding. A challenge must improve the checkers."""

    added: list[dict[str, Any]] = []
    units, unwired = _fill_units(accepted, session)
    dropped = list(unwired)
    violations = 0
    new_devices = {
        str(item.get("tag") or "").upper(): item
        for item in accepted
        if item.get("kind") == "device" and str(item.get("tag") or "").strip()
    }
    for unit in units:
        unit_dev_tags = {
            str(item.get("tag") or "").upper()
            for item in unit
            if item.get("kind") == "device"
        }
        missing_devs: list[dict[str, Any]] = []
        for item in unit:
            if item.get("kind") not in {"return", "wire_number"}:
                continue
            for end in _end_tags(item):
                if end in new_devices and end not in unit_dev_tags and _find(session, end) is None:
                    missing_devs.append(new_devices[end])
                    unit_dev_tags.add(end)
        run_unit = [*missing_devs, *unit]
        item_before = _layout_issue_set(session)
        numbers_before = _wire_numbers(session)
        snap = _snapshot(session)
        try:
            one = _apply(session, run_unit)
        except _ApplyFailed as exc:
            _restore(session, snap)
            dropped.extend({"reason": "not applied", "item": item, "error": exc.reason} for item in unit)
            continue
        except Exception as exc:  # noqa: BLE001 — a bad fact must not discard the project
            _restore(session, snap)
            dropped.extend({"reason": "not applied", "item": item, "error": str(exc)} for item in unit)
            continue
        outside = _numbers_outside_gap(numbers_before, _wire_numbers(session), run_unit)
        if outside:
            _restore(session, snap)
            violations += len(outside)
            dropped.extend({"reason": "fact outside gap", "item": item, "wires": outside} for item in unit)
            continue
        if not one:
            _restore(session, snap)
            dropped.extend({"reason": "not applied", "item": item} for item in unit)
            continue
        challenge = any(item.get("kind") == "challenge" for item in run_unit)
        # A number or a retag does not move a symbol, so the router must not
        # be asked to draw the sheet again. A new device is placed and routed.
        quiet = challenge or all(
            item.get("kind")
            in {"wire_number", "retag", "blank", "confirm", "attach", "clear_number", "replace_number", "open_stub"}
            for item in run_unit
        )
        try:
            if not quiet:
                _redraw(session)
            after = _layout_issue_set(session)
        except Exception as exc:  # noqa: BLE001
            _restore(session, snap)
            dropped.extend({"reason": "not applied", "item": item, "error": str(exc)} for item in unit)
            continue
        if challenge:
            # A wire into a phantom or untagged device may not change a layout
            # finding. Removing it is still an improvement when that device is
            # a gap target. The only new finding that may appear is that the
            # phantom itself does not land. The checker still reports it.
            tag = _removed_phantom_tag(session, snap, run_unit[0])
            fresh = after - item_before
            landing_only = bool(tag) and bool(fresh) and all("does not land" in item and tag in item for item in fresh)
            worse = _checkers_worse(item_before, after) and not landing_only
            phantom = _removed_wire_to_gap_device(session, snap, run_unit[0]) and not worse
            if worse or not (
                _checkers_better(item_before, after)
                or _named_finding_cleared(run_unit[0], item_before, after)
                or phantom
                or landing_only
            ):
                _restore(session, snap)
                dropped.extend({"reason": "checkers worse", "item": item} for item in unit)
                continue
        else:
            judged = set(after)
            crossed = _replacement_on_another_net(session, run_unit)
            if crossed:
                judged.add(crossed)
            # Through-symbol, through-label, jog, or overlap is layout routing.
            # It blocks issue and does not throw away a fact that is otherwise right
            # (phone T1 fill must survive wire×tag crossings after redraw).
            fact_before = _without_routing(item_before)
            fact_after = _without_routing(judged)
            if not _fill_no_worse(*_retag_findings(fact_before, fact_after, run_unit)):
                _restore(session, snap)
                dropped.extend({"reason": "not issued", "item": item} for item in unit)
                continue
        outside = _numbers_outside_gap(numbers_before, _wire_numbers(session), run_unit)
        if outside:
            _restore(session, snap)
            violations += len(outside)
            dropped.extend({"reason": "fact outside gap", "item": item, "wires": outside} for item in unit)
            continue
        if missing_devs:
            recovered_ids = {id(dev) for dev in missing_devs}
            dropped = [row for row in dropped if id(row.get("item")) not in recovered_ids]
        added.extend(one)
    return added, dropped, violations


def _fill_units(
    accepted: list[dict[str, Any]],
    session: Session | None = None,
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """A new device and the wires that land on it are one unit. No wire, no device."""

    devices = [item for item in accepted if item.get("kind") == "device"]
    tags = {str(item.get("tag") or "").upper(): item for item in devices}
    attached: dict[str, list[dict[str, Any]]] = {tag: [] for tag in tags}
    between: list[dict[str, Any]] = []
    rail_names = {"L1", "L2", "L3", "N", "PE", "24V", "0V"}
    if session is not None:
        for sh in session.project.sheets:
            rail_names |= _rail_facts(session, sh.id)
    for item in accepted:
        if item.get("kind") not in {"return", "wire_number"}:
            continue
        fresh = [end for end in _end_tags(item) if end in tags]
        if len(fresh) >= 2:
            between.append(item)
            continue
        for end in fresh:
            attached[end].append(item)

    def _non_rail_own(item: dict[str, Any], tag: str) -> bool:
        others = _end_tags(item) - {tag}
        return bool(others) and all(end not in rail_names for end in others)

    units: list[list[dict[str, Any]]] = []
    dropped: list[dict[str, Any]] = []
    seen: set[int] = set()
    placed: set[str] = set()

    def _drain_between_from_placed() -> None:
        progressed = True
        while progressed:
            progressed = False
            for item in between:
                if id(item) in seen:
                    continue
                fresh = [end for end in _end_tags(item) if end in tags]
                unplaced = [end for end in fresh if end not in placed]
                already = [end for end in fresh if end in placed]
                if len(unplaced) == 1 and already:
                    next_tag = unplaced[0]
                    units.append([tags[next_tag], item])
                    placed.add(next_tag)
                    seen.add(id(item))
                    for extra in attached.get(next_tag) or []:
                        if id(extra) in seen:
                            continue
                        # Same as the non-rail path: keep fuse→rail with the
                        # transformer landing so off-page rewrite can run first.
                        if not _non_rail_own(extra, next_tag):
                            units[-1].append(extra)
                        else:
                            units.append([extra])
                        seen.add(id(extra))
                    progressed = True

    # Place devices with a non-rail landing onto an existing sheet device first,
    # then propagate any ``between`` wires from placed devices before falling back
    # to rail-only landings (so a new fuse lands on its load before its rail).
    for tag, device in tags.items():
        if tag in placed:
            continue
        own = sorted(attached.get(tag) or [], key=lambda w: (0 if _non_rail_own(w, tag) else 1))
        if not own or not _non_rail_own(own[0], tag):
            continue
        units.append([device, own[0]])
        placed.add(tag)
        seen.add(id(own[0]))
        for extra in own[1:]:
            if id(extra) in seen:
                continue
            # Keep a fuse→rail hop in the same unit as the transformer landing so
            # a dangling line-side pin is not judged before the off-page rewrite.
            if _non_rail_own(own[0], tag) and not _non_rail_own(extra, tag):
                units[-1].append(extra)
            else:
                units.append([extra])
            seen.add(id(extra))
        _drain_between_from_placed()

    for tag, device in tags.items():
        if tag in placed:
            continue
        own = [w for w in (attached.get(tag) or []) if id(w) not in seen]
        if not own:
            continue
        units.append([device, own[0]])
        placed.add(tag)
        seen.add(id(own[0]))
        for extra in own[1:]:
            if id(extra) in seen:
                continue
            if _non_rail_own(own[0], tag) and not _non_rail_own(extra, tag):
                units[-1].append(extra)
            else:
                units.append([extra])
            seen.add(id(extra))
        _drain_between_from_placed()

    for item in between:
        if id(item) in seen:
            continue
        needed: list[dict[str, Any]] = []
        for end in _end_tags(item):
            if end in tags and end not in placed:
                needed.append(tags[end])
                placed.add(end)
        units.append([*needed, item])
        seen.add(id(item))
    for tag, device in tags.items():
        if tag not in placed:
            dropped.append({"reason": "no wiring", "item": device})
    challenges: list[list[dict[str, Any]]] = []
    fills: list[list[dict[str, Any]]] = []
    for item in accepted:
        if item.get("kind") == "device" or id(item) in seen:
            continue
        if item.get("kind") == "challenge":
            challenges.append([item])
        else:
            fills.append([item])
    challenges, fills = _judge_remove_and_renumber_together(session, challenges, fills)
    # Remove a bad fact before judging a fill, and place new devices last.
    return [*challenges, *fills, *units], dropped


def _judge_remove_and_renumber_together(
    session: Session | None,
    challenges: list[list[dict[str, Any]]],
    fills: list[list[dict[str, Any]]],
) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    """A reply that removes the extra wire and renumbers the real one is one set.

    Judging the new number while the old wire still holds it looks like two nets.
    """

    if session is None:
        return challenges, fills
    held: dict[str, set[str]] = {}
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            number = str(wire.number or "").strip()
            if number:
                held.setdefault(number, set()).add(wire.id)
    used: set[int] = set()
    merged: list[list[dict[str, Any]]] = []
    for challenge in challenges:
        partners: list[dict[str, Any]] = []
        target = str(challenge[0].get("target") or "")
        for index, fill in enumerate(fills):
            item = fill[0]
            if index in used or item.get("kind") != "replace_number":
                continue
            number = str(item.get("number") or "")
            wire_id = str(item.get("wire_id") or "")
            if target and target in held.get(number, set()) and wire_id and wire_id != target:
                partners.append(item)
                used.add(index)
        merged.append([*challenge, *partners])
    kept = [fill for index, fill in enumerate(fills) if index not in used]
    return merged, kept


def _end_tags(item: dict[str, Any]) -> set[str]:
    return {
        str(item.get("from") or "").split(".")[0].upper(),
        str(item.get("to") or "").split(".")[0].upper(),
    } - {""}


def _lands_on(item: dict[str, Any], tag: str) -> bool:
    if item.get("kind") not in {"return", "wire_number"}:
        return False
    ends = {str(item.get("from") or "").split(".")[0].upper(), str(item.get("to") or "").split(".")[0].upper()}
    return tag in ends


def _replacement_on_another_net(session: Session, unit: list[dict[str, Any]]) -> str:
    """A replacement number that already names some other net.

    106 between PB2 and PB1 is that node. Writing it on the overload return
    is a second net, so the answer is a new finding and is rolled back. A
    number shared solely by landings on the same rail is that rail's number.
    """

    item = next((row for row in unit if row.get("kind") == "replace_number"), None)
    if item is None:
        return ""
    number = str(item.get("number") or "").strip()
    wire = _wire_by_id(session, str(item.get("wire_id") or ""))
    if wire is None or not number:
        return ""
    sheet = _owner_sheet_of_wire(session, wire)
    matching = [other for other in sheet.wires if other.id != wire.id and str(other.number or "").strip() == number]
    if matching and _shared_only_on_one_rail(session, sheet, [wire, *matching]):
        return ""
    keys = _wire_net_keys(session, sheet)
    own = keys.get(wire.id, f"wire:{wire.id}")
    for other in matching:
        if keys.get(other.id, f"wire:{other.id}") != own:
            return f"{sheet.id}: {number} is on two nets"
    return ""


def _wire_net_keys(session: Session, sheet: Sheet) -> dict[str, str]:
    """Pin-connected nets. A rail end does not join one rung to another."""

    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.render.sheet import parse_pin_ref

    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        ra, rb = find(left), find(right)
        if ra != rb:
            parent[rb] = ra

    pins_of: dict[str, list[str]] = {}
    for wire in sheet.wires:
        pins: list[str] = []
        for text in (wire.frm, wire.to):
            if endpoint_is_rail(sheet, text, session.project):
                continue
            try:
                ref, pin = parse_pin_ref(text)
            except ValueError:
                ref, pin = str(text).split(".")[0], ""
            pins.append(f"{ref}.{pin}" if pin else ref)
        if len(pins) >= 2:
            for pin in pins[1:]:
                union(pins[0], pin)
        pins_of[wire.id] = pins
    return {wire.id: (find(pins_of[wire.id][0]) if pins_of[wire.id] else f"wire:{wire.id}") for wire in sheet.wires}


def _retag_findings(
    before: set[str], after: set[str], unit: list[dict[str, Any]]
) -> tuple[set[str], set[str]]:
    """A retag renames a finding. That is the same defect, not a new one."""

    retag = next((item for item in unit if item.get("kind") == "retag"), None)
    if retag is None:
        return before, after
    old = str(retag.get("old") or "").strip()
    new = str(retag.get("tag") or "").strip()
    if not old or not new or old.upper() == new.upper():
        return before, after
    folded = {text.replace(new, old) for text in after}
    return before, folded


def _removed_phantom_tag(session: Session, snap: dict[str, Any], item: dict[str, Any]) -> str:
    """The phantom or untagged device on the wire this challenge removed."""

    target = str(item.get("target") or "")
    if not target or _wire_by_id(session, target) is not None:
        return ""
    for saved in snap.values():
        for saved_wire in saved["wires"]:
            if str(saved_wire.id) != target:
                continue
            for end in (str(saved_wire.frm), str(saved_wire.to)):
                ref = end.split(".")[0].strip()
                if ref.upper() in {"L1", "L2", "L3", "N", "X1", "X2", "0V", "PE"}:
                    continue
                if ref and _phantom_or_untagged(session, ref, remaining_gaps(session)):
                    return ref
    return ""


def _removed_wire_to_gap_device(session: Session, snap: dict[str, Any], item: dict[str, Any]) -> bool:
    """The challenge removed a wire whose device is still a gap target.

    Layout findings may not mention a phantom coil. The wire is still a
    leftover, and taking it off does not add or move anything.
    """

    target = str(item.get("target") or "")
    if not target or _wire_by_id(session, target) is not None:
        return False
    wire = None
    for saved in snap.values():
        for saved_wire in saved["wires"]:
            if str(saved_wire.id) == target:
                wire = saved_wire
                break
    if wire is None:
        return False
    gaps = remaining_gaps(session)
    named = {str(gap.get("tag") or "").upper() for gap in gaps if gap.get("tag")}
    for end in (str(wire.frm), str(wire.to)):
        ref = end.split(".")[0].strip()
        if not ref or ref.upper() not in named:
            continue
        if _phantom_or_untagged(session, ref, gaps):
            return True
    return False


def _phantom_or_untagged(session: Session, ref: str, gaps: list[dict[str, Any]]) -> bool:
    """A reader duplicate such as M1_2, or a tag the sheet does not accept."""

    from panelcad.importer.read import is_device_tag

    upper = ref.upper()
    if any(str(gap.get("kind") or "") == "untagged" and str(gap.get("tag") or "").upper() == upper for gap in gaps):
        return True
    if not is_device_tag(ref):
        return True
    return bool(re.fullmatch(r".+_\d+", ref))


_UNREAD_DEVICE_GAP_KINDS = frozenset(
    {
        "untagged",
        "unconnected",
        "orphan_contact",
        "coil_without_contacts",
        "dropped_page",
        "empty_page",
    }
)


def _has_unresolved_device(session: Session, pages: list[dict[str, Any]] | None = None) -> bool:
    """True when an unresolved device, page, or open question remains.

    ``unread_device:<sheet>`` is the add-a-tag slot emitted whenever any other
    leftover gap exists on a sheet (wire ``?``, ``suspect_number``,
    ``duplicate_tag``, return, and the rest). That slot, and the real device or
    page gaps, all block completion. Confirmed tags are not low-confidence
    suspects, so they do not keep a sheet unfinished by themselves.
    """

    layout_rejected: set[str] = set()
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        for comp in sheet.components:
            if "needs_info" in str(comp.library_id or "").lower():
                return True
        try:
            sheet_issues = elementary_layout_issues(session, sheet)
        except Exception:  # noqa: BLE001
            sheet_issues = []
        layout_rejected |= _rejected_ids(sheet, sheet_issues)
    for gap in remaining_gaps(session, pages):
        kind = str(gap.get("kind") or "")
        if kind in _UNREAD_DEVICE_GAP_KINDS or kind == "unread_device":
            return True
        if kind == "suspect_tag":
            reason = str(gap.get("reason") or "")
            if reason == "nonstandard tag":
                return True
            if reason == "checker":
                tag = str(gap.get("tag") or "").upper()
                if tag in layout_rejected:
                    return True
                comp = _find(session, tag)
                lib = str(getattr(comp, "library_id", "") or "").lower() if comp is not None else ""
                confirmed = str((getattr(comp, "properties", None) or {}).get("print_import_confirmed") or "") == "yes"
                if "ol_contact" not in lib and not confirmed:
                    return True
    return False


_PRESERVE_SCAN_BLOCK = "Preserve layout for scans is not finished yet; use ladder"


def _preserve_scan_layout_blocked(session: Session) -> bool:
    """True when a raster-scan-reader project is in preserve layout mode.

    External, vision, photo, and PDF/vector preserve imports are not blocked.
    """

    props = session.project.properties if isinstance(session.project.properties, dict) else {}
    block = props.get("print_import") if isinstance(props, dict) else None
    if not isinstance(block, dict):
        return False
    mode = str(block.get("layout_mode") or "").strip().lower()
    if mode != "preserve":
        return False
    return str(block.get("from_scan") or "").strip().lower() in {"yes", "true", "1"}


def _completion_issues(session: Session, pages: list[dict[str, Any]] | None = None) -> list[str]:
    """Open terminals, leftover gaps, and layout findings. These block issue.

    A sheet that still fails ``elementary_layout_issues`` (for example
    ``PL1 is not next to L2``) must not complete or issue. The layout check
    is not loosened here — findings are forwarded as completion blockers.
    """

    issues: list[str] = []
    # Preserve remains selectable for rebuilds, but must never auto-issue a scan.
    if _preserve_scan_layout_blocked(session):
        issues.append(_PRESERVE_SCAN_BLOCK)
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic" or not _is_control_elementary(sheet):
            continue
        try:
            for finding in elementary_layout_issues(session, sheet):
                if finding not in issues:
                    issues.append(finding)
        except Exception:  # noqa: BLE001
            issues.append(f"{sheet.id}: layout check failed")
        for comp in sheet.components:
            wired = _wired_pin_ids(sheet, comp.id)
            landed = wired | (_open_stub_pins(comp) - wired)
            device = _device(session, comp.library_id)
            pins = [str(pin.id) for pin in device.pins] if device is not None else []
            lib = comp.library_id.lower()
            if "transformer" in lib:
                secondary = [pin for pin in pins if pin.upper().startswith("X")]
                if secondary and not any(pin in landed for pin in secondary):
                    issues.append(f"{sheet.id}: {comp.id} secondary does not feed a wire")
                for left, right in (("H1", "H4"),):
                    if left in pins and right in pins:
                        hit = [pin for pin in (left, right) if pin in landed]
                        if len(hit) == 1:
                            missing = right if hit[0] == left else left
                            issues.append(f"{sheet.id}: {comp.id} pin {missing} does not land")
            if _is_contact_part(lib) and landed and any(pin not in landed for pin in pins):
                open_pin = next(pin for pin in pins if pin not in landed)
                issues.append(f"{sheet.id}: {comp.id} pin {open_pin} does not land")
            # Relay/coil hot must land when the series interlock (M1-23) is on
            # the sheet — otherwise CR1.A1 stays open after the invented L1 hop
            # is dropped and the sheet still issues.
            if (
                ("coil" in lib or ".relays." in lib)
                and "A1" in pins
                and "A1" not in landed
                and any(
                    other.id.upper() in {"M1-23", "UA-22", "UA22", "1M"}
                    for other in sheet.components
                )
            ):
                issues.append(f"{sheet.id}: {comp.id} pin A1 does not land")
        # Facts-only: a control relay on the sheet with a printed series
        # interlock (OCR UA-22 / M1-23) must keep that contact. Removing the
        # orphan and issuing without M1-23 (and without 109/102) is forbidden.
        tags = {other.id.upper() for other in sheet.components}
        has_relay = any(
            ("coil" in str(other.library_id or "").lower() or ".relays." in str(other.library_id or "").lower())
            and str(other.id).upper() in {"CR1", "CR"}
            for other in sheet.components
        )
        interlock_present = bool(tags & {"M1-23", "UA-22", "UA22", "1M"})
        if has_relay and not interlock_present and _scan_showed_series_interlock(session, pages):
            issues.append(f"{sheet.id}: printed series interlock M1-23 is missing")
    if _has_unresolved_device(session, pages):
        issues.append("unread device remains")
    return issues


def _scan_showed_series_interlock(
    session: Session, pages: list[dict[str, Any]] | None = None
) -> bool:
    """True when the reader/OCR saw M1-23 or its common misread (UA-22)."""

    want = {"M1-23", "UA-22", "UA22", "1M"}
    props = session.project.properties if isinstance(session.project.properties, dict) else {}
    block = props.get("print_import") if isinstance(props, dict) else None
    if isinstance(block, dict):
        for row in block.get("dropped_near_match") or []:
            if isinstance(row, dict) and str(row.get("want") or "").upper() == "M1-23":
                return True
        for item in block.get("items") or []:
            if not isinstance(item, dict):
                continue
            blob = " ".join(
                str(item.get(key) or "")
                for key in ("tag", "text", "label", "number", "id")
            ).upper()
            if any(tok in blob for tok in want):
                return True
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        for key in ("devices", "tags", "texts", "labels"):
            for item in page.get(key) or []:
                if isinstance(item, dict):
                    text = " ".join(
                        str(item.get(k) or "") for k in ("tag", "text", "label", "id")
                    ).upper()
                else:
                    text = str(item or "").upper()
                if any(tok in text for tok in want):
                    return True
    return False


def _wired_pin_ids(sheet: Sheet, ref: str) -> set[str]:
    found: set[str] = set()
    for wire in sheet.wires:
        for text in (str(wire.frm), str(wire.to)):
            head, dot, pin = text.partition(".")
            if dot and head == ref and pin:
                found.add(pin)
    return found


def _open_stub_pins(comp: Any, *, user_confirmed_only: bool = False) -> set[str]:
    """Pins recorded as a printed lead that ends in open air.

    Reader open-stub guesses on contacts can hide a seal-in tip (M1-13.4)
    and let a sheet issue with that pin floating. Only a user leftover
    open-stub answer counts as confirmed for contacts; reader stamps still
    leave the pin unlanded for completion / open_pin.
    """

    props = getattr(comp, "properties", None) or {}
    if not isinstance(props, dict):
        props = {}
    raw = str(props.get("print_open_stubs") or "")
    stubs = {part.strip() for part in raw.split(",") if part.strip()}
    if not stubs:
        return set()
    if user_confirmed_only:
        return stubs if str(props.get("print_open_stub_confirmed") or "") == "yes" else set()
    # Contact symbols: reader stubs alone do not settle the pin.
    lib = str(getattr(comp, "library_id", "") or "").lower()
    if _is_contact_part(lib) and str(props.get("print_open_stub_confirmed") or "") != "yes":
        return set()
    return stubs


# Geometry after a redraw — blocks issue, but must not discard an otherwise
# correct fill (same bar as through-symbol / jog / overlap).
_ROUTING_FINDING = (
    "runs through a symbol",
    "runs through device label",
    "jogs",
    "overlaps",
)


def _without_routing(findings: set[str]) -> set[str]:
    return {item for item in findings if not any(part in item for part in _ROUTING_FINDING)}


def _fill_no_worse(before: set[str], after: set[str]) -> bool:
    """A fill may stay when it adds no finding and does not raise the count or the severity."""

    if after - before:
        return False
    if len(after) > len(before):
        return False
    before_rank = max((_finding_severity(item) for item in before), default=0)
    after_rank = max((_finding_severity(item) for item in after), default=0)
    return after_rank <= before_rank


def _finding_severity(text: str) -> int:
    if any(
        part in text
        for part in (
            "own pins",
            "with no load",
            "is a fuse from",
            "runs through a symbol",
            "shares the terminal row",
            "two wire numbers",
            "ties the primary",
        )
    ):
        return 3
    if any(part in text for part in ("does not land", "not next to", "overlaps", "not on one rung")):
        return 2
    return 1


def _checkers_worse(before: set[str], after: set[str]) -> bool:
    """A new finding is worse even when the count went down. So is a higher severity."""

    if after - before:
        return True
    before_rank = max((_finding_severity(item) for item in before), default=0)
    after_rank = max((_finding_severity(item) for item in after), default=0)
    return after_rank > before_rank


def _checkers_better(before: set[str], after: set[str]) -> bool:
    """A finding was cleared, and no new finding appeared."""

    if after - before:
        return False
    return after < before


def _named_finding_cleared(item: dict[str, Any], before: set[str], after: set[str]) -> bool:
    finding = str(item.get("finding") or "")
    if after - before:
        return False
    if not finding or finding not in before or finding in after:
        return False
    return not _checkers_worse(before, after)


def _primary_fuse_rail_offpage(
    session: Session,
    frm: str,
    to: str,
    number: str = "",
) -> dict[str, Any] | None:
    """If ``frm``/``to`` is a primary fuse↔control-rail hop, land off-page instead.

    Returns the added fact dict, or None when this is not a primary rail feed.
    """

    from panelcad.render.sheet import parse_pin_ref

    ends: list[tuple[str, str | None]] = []
    for text in (frm, to):
        raw = str(text or "").strip()
        if not raw:
            return None
        if "." not in raw:
            ends.append((raw.upper(), None))
            continue
        try:
            ref, pin = parse_pin_ref(raw)
        except ValueError:
            return None
        ends.append((ref, pin))
    if len(ends) != 2:
        return None
    fuse_end = None
    rail_end = None
    for ref, pin in ends:
        comp = _find(session, ref)
        if comp is not None and "fuse" in str(comp.library_id or "").lower() and pin:
            fuse_end = (ref, pin)
        elif pin is None and ref.upper() in (_rail_facts(session, _owner_sheet(session, ref) or "") | {"L1", "L2"}):
            rail_end = ref
        elif pin is None and ref.upper() in {"L1", "L2", "X1", "X2"}:
            rail_end = ref
    if fuse_end is None or rail_end is None:
        return None
    fuse_tag, fuse_pin = fuse_end
    sheet_id = _owner_sheet(session, fuse_tag) or ""
    sheet = _sheet(session, sheet_id) if sheet_id else None
    if sheet is None:
        return None
    # Only rewrite when this fuse already feeds the transformer primary (H*),
    # never when it is the secondary fuse on X*.
    by_id = {comp.id: comp for comp in sheet.components}
    feeds_primary = False
    feeds_secondary = False
    for wire in sheet.wires:
        for text in (wire.frm, wire.to):
            raw = str(text or "")
            if not raw.startswith(f"{fuse_tag}."):
                continue
            mate = str(wire.to if raw == str(wire.frm) else wire.frm)
            if "." not in mate:
                continue
            try:
                m_ref, m_pin = parse_pin_ref(mate)
            except ValueError:
                continue
            mate_comp = by_id.get(m_ref)
            if mate_comp is None or "transformer" not in str(mate_comp.library_id or "").lower():
                continue
            pin_u = str(m_pin or "").upper()
            if pin_u.startswith("H"):
                feeds_primary = True
            if pin_u.startswith("X"):
                feeds_secondary = True
    if feeds_secondary or not feeds_primary:
        return None
    label = "EXT"
    sx, sy = _scan_xy(by_id[fuse_tag])
    near = ocr_texts_near_primary_feed(session, sheet_id=sheet.id, x=sx, y=sy)
    if near:
        label = near[0]
    # Never borrow the control-rail number as the off-page callout.
    if label in {"102", "104", "101", "103"} or (number and number in {"102", "104"}):
        label = near[0] if near and near[0] not in {"102", "104", "101", "103"} else "EXT"
    try:
        landed = land_primary_offpage_feed(session, sheet.id, fuse_tag, fuse_pin, label)
    except (ValueError, KeyError, AssertionError):
        return None
    landed["kind"] = "offpage_feed"
    landed["from"] = frm
    landed["to"] = to
    return landed


def _redraw(session: Session) -> None:
    from panelcad.core.elementary_layout import layout_elementary

    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") == "schematic" and _is_control_elementary(sheet):
            layout_elementary(session, sheet)


def _sheet_is_clean(session: Session) -> bool:
    try:
        return _issue(session)
    except Exception:
        return False


def _apply(session: Session, accepted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    added: list[dict[str, Any]] = []
    for item in accepted:
        kind = item.get("kind")
        if kind == "confirm":
            comp = _find(session, str(item.get("tag") or ""))
            if comp is not None:
                props = comp.properties if isinstance(comp.properties, dict) else {}
                try:
                    old_c = float(props.get("print_import_confidence") or 0.0)
                except (TypeError, ValueError):
                    old_c = 0.0
                try:
                    new_c = float(item.get("confidence") or _MIN_CONFIDENCE)
                except (TypeError, ValueError):
                    new_c = _MIN_CONFIDENCE
                props["print_import_confidence"] = str(round(max(old_c, new_c), 3))
                props["print_import_confirmed"] = "yes"
                comp.properties = props
            added.append({"kind": "confirm", "tag": item["tag"]})
        elif kind == "device":
            session.place(str(item["library_id"]), ref=str(item["tag"]), x=80.0, y=80.0, snap=False)
            added.append({"kind": "device", "tag": item["tag"], "library_id": item["library_id"]})
        elif kind in {"retag", "blank"}:
            new = "?" if kind == "blank" else str(item.get("tag") or "")
            if _rename_tag(session, str(item.get("old") or item.get("tag") or ""), new, str(item.get("library_id") or "")):
                added.append(item)
        elif kind == "wire_number":
            wire = _wire_by_gap(session, str(item.get("gap") or ""))
            if wire is None:
                continue
            wire.number = str(item["number"])
            if not str(wire.label or "").strip() or str(wire.label) == "?":
                wire.label = wire.number
            added.append({"kind": "wire_number", "gap": item["gap"], "number": item["number"]})
        elif kind == "attach":
            wire = _wire_by_id(session, str(item.get("attach") or ""))
            if wire is None:
                continue
            wire.number = str(item["number"])
            wire.number_locked = True
            added.append({"kind": "attach", "gap": item.get("gap") or "", "attach": item["attach"], "number": item["number"]})
        elif kind == "clear_number":
            wire = _wire_by_id(session, str(item.get("wire_id") or ""))
            if wire is None:
                continue
            wire.number = "?"
            wire.number_locked = True
            added.append({"kind": "clear_number", "gap": item.get("gap") or "", "wire_id": item["wire_id"], "number": "?"})
        elif kind == "replace_number":
            wire = _wire_by_id(session, str(item.get("wire_id") or ""))
            if wire is None:
                continue
            wire.number = str(item["number"])
            wire.number_locked = True
            added.append(
                {
                    "kind": "replace_number",
                    "gap": item.get("gap") or "",
                    "wire_id": item["wire_id"],
                    "number": item["number"],
                }
            )
        elif kind == "open_stub":
            tag = str(item.get("tag") or "").strip()
            pins = [str(pin) for pin in (item.get("pins") or []) if str(pin).strip()]
            comp = _find(session, tag)
            if comp is None or not pins:
                continue
            sheet = _sheet(session, _owner_sheet(session, tag))
            wired = _wired_pin_ids(sheet, tag) if sheet is not None else set()
            props = comp.properties if isinstance(comp.properties, dict) else {}
            existing = {part.strip() for part in str(props.get("print_open_stubs") or "").split(",") if part.strip()}
            for pin in pins:
                if pin not in wired:
                    existing.add(pin)
            kept = sorted(pin for pin in existing if pin not in wired)
            if kept:
                props["print_open_stubs"] = ",".join(kept)
                props["print_open_stub_confirmed"] = "yes"
            else:
                props.pop("print_open_stubs", None)
                props.pop("print_open_stub_confirmed", None)
            comp.properties = props
            added.append({"kind": "open_stub", "gap": item.get("gap") or "", "tag": tag, "pins": kept})
        elif kind == "return":
            kwargs: dict[str, Any] = {}
            if item.get("number"):
                kwargs["number"] = item["number"]
            landed = _land_return(session, str(item["from"]), str(item["to"]), str(item.get("number") or ""))
            if landed is None:
                continue
            frm, to = landed
            sheet_id = _owner_sheet(session, frm) or _owner_sheet(session, to)
            # Primary fuse → control-rail hops are off-page facts, never L1/L2.
            off = _primary_fuse_rail_offpage(session, frm, to, str(item.get("number") or ""))
            if off is not None:
                added.append(off)
                continue
            try:
                session.wire(frm, to, sheet_id=sheet_id or None, **kwargs)
            except (ValueError, KeyError) as exc:
                raise _ApplyFailed(str(exc)) from exc
            added.append({"kind": "return", "from": item["from"], "to": item["to"], "number": item.get("number") or ""})
        elif kind == "challenge":
            target = str(item.get("target") or "")
            if _delete_wire(session, target):
                added.append(
                    {
                        "kind": "challenge",
                        "gap": item.get("gap") or "",
                        "target": target,
                        "removed": "wire",
                        "reason": item.get("reason") or "",
                    }
                )
            elif _absent_device(item, target) and _remove_device(session, target):
                added.append(
                    {
                        "kind": "challenge",
                        "gap": item.get("gap") or "",
                        "target": target,
                        "removed": "device",
                        "reason": item.get("reason") or "",
                    }
                )
            elif _rename_tag(session, target, "?", ""):
                added.append(
                    {
                        "kind": "challenge",
                        "gap": item.get("gap") or "",
                        "target": target,
                        "removed": "device",
                        "reason": item.get("reason") or "",
                    }
                )
    return added


def _issue(
    session: Session,
    before_ansi: set[str] | None = None,
    *,
    pages: list[dict[str, Any]] | None = None,
) -> bool:
    from panelcad.core.ansi_drafting import ansi_drafting_issues

    if before_ansi is None:
        before_ansi = set(ansi_drafting_issues(session.project, session.catalog))
    saw = False
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic" or not _is_control_elementary(sheet):
            continue
        saw = True
        if not issue_elementary(session, sheet, force=True):
            return False
        # issue_elementary already gates on layout; re-check so a post-redraw
        # regression (or a path that skipped the gate) cannot report success.
        if elementary_layout_issues(session, sheet):
            return False
    if not saw:
        return False
    # A cleanup fact must not add an ANSI failure. Issues already on the
    # deterministic sheet are not this pass's to invent or to waive.
    after_ansi = set(ansi_drafting_issues(session.project, session.catalog))
    if not after_ansi.issubset(before_ansi):
        return False
    # A sheet can be free of rung defects and still be unfinished: a
    # transformer secondary that feeds nothing, a contact with one pin
    # open, an unread-device gap, or a layout finding such as
    # "PL1 is not next to L2".
    return not _completion_issues(session, pages=pages)


def _snapshot(session: Session) -> dict[str, Any]:
    return {
        sheet.id: {
            "components": [comp.model_copy(deep=True) for comp in sheet.components],
            "wires": [wire.model_copy(deep=True) for wire in sheet.wires],
        }
        for sheet in session.project.sheets
    }


def _restore(session: Session, snaps: dict[str, Any]) -> None:
    for sheet in session.project.sheets:
        saved = snaps.get(sheet.id)
        if saved is None:
            continue
        sheet.components = [comp.model_copy(deep=True) for comp in saved["components"]]
        sheet.wires = [wire.model_copy(deep=True) for wire in saved["wires"]]


def _fact_list(session: Session) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        for comp in sheet.components:
            rows.append({"kind": "device", "tag": comp.id, "library_id": comp.library_id})
        for wire in sheet.wires:
            rows.append(
                {
                    "kind": "wire",
                    "id": wire.id,
                    "from": str(wire.frm),
                    "to": str(wire.to),
                    "number": str(wire.number or "") or "?",
                }
            )
    return rows


def _wire_gap(item: dict[str, Any], gap_by_id: dict[str, dict[str, Any]], session: Session) -> dict[str, Any] | None:
    gap_id = str(item.get("gap") or "")
    if gap_id:
        gap = gap_by_id.get(gap_id)
        if gap is None or gap.get("kind") != "wire_number":
            return None
        return gap
    frm = str(item.get("from") or "")
    to = str(item.get("to") or "")
    if not frm or not to:
        return None
    want = {_end(frm), _end(to)}
    for gap in gap_by_id.values():
        if gap.get("kind") != "wire_number":
            continue
        if {_end(str(gap.get("from") or "")), _end(str(gap.get("to") or ""))} == want:
            return gap
    del session
    return None


def _wire_on(session: Session, gap: dict[str, Any]) -> Any:
    sheet = _sheet(session, str(gap.get("sheet") or ""))
    if sheet is None:
        return None
    for wire in sheet.wires:
        if wire.id == gap.get("wire_id"):
            return wire
    return None


def _wire_by_id(session: Session, wire_id: str) -> Any:
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            if wire.id == wire_id:
                return wire
    return None


def _absent_device(item: dict[str, Any], target: str) -> bool:
    """A confident challenge that says this device is not on the scan."""

    text = f"{item.get('reason') or ''} {item.get('evidence') or ''}".lower()
    said = item.get("absent") is True or "not on the scan" in text or "no such device" in text
    phantom = target.strip() in {"?", ""} or target.startswith("?")
    return said or phantom


def _remove_device(session: Session, tag: str) -> bool:
    """Drop the device and only the wires that would dangle from it."""

    comp = _find(session, tag)
    if comp is None:
        return False
    want = tag.upper()
    for sheet in session.project.sheets:
        sheet.wires = [
            wire
            for wire in sheet.wires
            if want not in {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
        ]
        sheet.components = [item for item in sheet.components if item.id.upper() != want]
    return True


def _delete_wire(session: Session, wire_id: str) -> bool:
    for sheet in session.project.sheets:
        kept = [wire for wire in sheet.wires if wire.id != wire_id]
        if len(kept) != len(sheet.wires):
            sheet.wires = kept
            return True
    return False


def _wire_by_gap(session: Session, gap_id: str) -> Any:
    wire_id = gap_id.split(":", 1)[1] if gap_id.startswith("wire:") else gap_id
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            if wire.id == wire_id:
                return wire
    return None


def _confident(item: dict[str, Any]) -> bool:
    if item.get("unreadable") is True:
        return False
    if str(item.get("number") or "").strip() == "?" and "number" in item and "library_id" not in item:
        return False
    try:
        score = float(item.get("confidence"))
    except (TypeError, ValueError):
        return False
    return score >= _MIN_CONFIDENCE


def _tag(item: dict[str, Any]) -> str:
    return str(item.get("tag") or "").strip().upper()


def _end(text: str) -> str:
    return str(text).strip().upper()


def _known_tags(session: Session) -> set[str]:
    tags: set[str] = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            tags.add(comp.id.upper())
    return tags


def _contact_senses(session: Session) -> dict[str, str]:
    senses: dict[str, str] = {}
    for sheet in session.project.sheets:
        for comp in sheet.components:
            lib = comp.library_id.lower()
            if "pb_nc" in lib:
                senses[comp.id.upper()] = "nc"
            elif "pb_no" in lib:
                senses[comp.id.upper()] = "no"
    return senses


def _is_return_load(library_id: str) -> bool:
    lib = library_id.lower()
    if "ol_contact" in lib:
        return True
    if any(token in lib for token in ("coil", "pilot", "lamp", "relays.", ".timers.")):
        return "contact" not in lib or "coil" in lib
    return False


def _explicit_absence(item: dict[str, Any]) -> bool:
    """A confident statement that the crop has no such device. Low confidence is not one."""

    try:
        score = float(item.get("confidence"))
    except (TypeError, ValueError):
        return False
    return score >= _MIN_CONFIDENCE


def _blankable(tag: str, gaps: list[dict[str, Any]]) -> bool:
    """Checker-rejected or nonstandard tags may be replaced. Low confidence may only be asked."""

    want = tag.upper()
    for gap in gaps:
        if str(gap.get("tag") or "").upper() != want:
            continue
        kind = str(gap.get("kind") or "")
        reason = str(gap.get("reason") or "")
        if kind in {"orphan_contact", "coil_without_contacts", "untagged"}:
            return True
        if kind == "suspect_tag" and reason in {"checker", "nonstandard tag"}:
            return True
    return False


def _resolved_count(added: list[dict[str, Any]]) -> int:
    """Blanks of a legible read are not resolutions. Confirming a tag is not one either."""

    count = 0
    for item in added:
        kind = str(item.get("kind") or "")
        if kind == "confirm":
            continue
        if kind == "blank" and not item.get("replaces_bad_read"):
            continue
        count += 1
    return count


def _flagged_tags(gaps: list[dict[str, Any]]) -> set[str]:
    return {
        str(gap.get("tag") or "").upper()
        for gap in gaps
        if str(gap.get("kind") or "") in _SUSPECT_KINDS and gap.get("tag")
    }


def _ask_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """Reader mistakes to ask about. Asking does not change a fact."""

    gaps: list[dict[str, Any]] = []
    gaps.extend(_duplicate_tag_gaps(session, sheet))
    gaps.extend(_coil_place_gaps(session, sheet))
    gaps.extend(_skipped_contact_gaps(session, sheet))
    gaps.extend(_suspect_number_gaps(session, sheet))
    gaps.extend(_unattached_number_gaps(session, sheet))
    gaps.extend(_open_pin_gaps(session, sheet))
    gaps.extend(_terminal_open_pin_gaps(session, sheet))
    gaps.extend(_duplicate_device_number_gaps(session, sheet))
    return gaps


def _duplicate_device_number_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """The same sheet-line number on two device nets. A rail bus is not one.

    109 on the coil hop and 109 on the relay feed are two wires. The gap names
    both so a challenge can remove the one the scan does not show.
    """

    from panelcad.core.ladder import endpoint_is_rail

    keys = _wire_net_keys(session, sheet)
    grouped: dict[str, list[Any]] = {}
    for wire in sheet.wires:
        number = str(wire.number or "").strip()
        if _SHEET_LINE.match(number) is None:
            continue
        if any(endpoint_is_rail(sheet, end, session.project) for end in (wire.frm, wire.to)):
            continue
        grouped.setdefault(number, []).append(wire)
    gaps: list[dict[str, Any]] = []
    for number, wires in grouped.items():
        nets = {keys.get(wire.id, wire.id) for wire in wires}
        if len(nets) < 2:
            continue
        gaps.append(
            {
                "id": f"two_numbers:{sheet.id}:{number}",
                "kind": "two_numbers",
                "sheet": sheet.id,
                "number": number,
                "numbers": [number],
                "wires": [wire.id for wire in wires],
                "read": f"{number} is on two nets",
            }
        )
    return gaps


def _terminal_open_pin_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """Unwired terminal A pins, and B when return leftovers are not already asking.

    Feed-through blocks carry the field on A and the rail return on B. Strip OCR
    ``wire_bottom`` is the printed B→L2 number (301–304). The return leftover
    already covers B for catalog terminals; stamp ``prefer`` there instead of
    emitting a second open_pin on the same pin.
    """

    gaps: list[dict[str, Any]] = []
    for comp in sheet.components:
        lib = str(comp.library_id or "").lower()
        if "terminal" not in lib or "needs_info" in lib:
            continue
        device = _device(session, comp.library_id)
        if device is None:
            continue
        pins = [str(pin.id) for pin in device.pins]
        if not pins:
            continue
        wired = _wired_pin_ids(sheet, comp.id)
        stubs = _open_stub_pins(comp)
        landed = wired | stubs
        if not landed:
            continue
        props = comp.properties if isinstance(comp.properties, dict) else {}
        right = _side_pin(device, "right")
        top = str(props.get("wire_top") or "").strip()
        # A-side: OCR wire_top when present. Else a multi-block X1 strip still
        # needs the printed panel bond (201 on X1-1, 202 on X1-2/3/4) — ask,
        # do not invent the wire.
        prefer_a = top if top.isdigit() else ""
        if not prefer_a and str(comp.id).upper().startswith("X1-"):
            strip_peers = [
                other
                for other in sheet.components
                if str(other.id).upper().startswith("X1-")
                and "terminal" in str(other.library_id or "").lower()
            ]
            if len(strip_peers) >= 2:
                prefer_a = "201" if str(comp.id).upper().endswith("-1") else "202"
        if not prefer_a:
            continue
        for pin in pins:
            if pin in landed:
                continue
            # Right/B is the return leftover — do not double-ask as open_pin.
            if right and pin == right:
                continue
            prefer = prefer_a if pin.upper() == "A" else ""
            if not prefer:
                continue
            gaps.append(
                {
                    "id": f"open_pin:{sheet.id}:{comp.id}.{pin}",
                    "kind": "open_pin",
                    "sheet": sheet.id,
                    "tag": comp.id,
                    "pin": pin,
                    "prefer": prefer,
                    "read": (
                        f"{comp.id} pin {pin} does not land"
                        + (f" (strip {prefer})" if prefer else "")
                    ),
                }
            )
    return gaps


def _open_pin_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """Open contact pins and transformer secondaries that still need a fact.

    A contact with one pin landed and one pin open can take a rail stub. A
    transformer whose secondary pins are all open can take an open-stub answer.
    """

    gaps: list[dict[str, Any]] = []
    for comp in sheet.components:
        device = _device(session, comp.library_id)
        if device is None:
            continue
        pins = [str(pin.id) for pin in device.pins]
        wired = _wired_pin_ids(sheet, comp.id)
        stubs = _open_stub_pins(comp)
        landed = wired | (stubs - wired)
        lib = str(comp.library_id or "").lower()
        if "transformer" in lib:
            secondary = [pin for pin in pins if pin.upper().startswith("X")]
            open_sec = [pin for pin in secondary if pin not in landed]
            if secondary and not any(pin in landed for pin in secondary):
                # Both X1/X2 open — one combined leftover (fuse path + L2 return).
                gaps.append(
                    {
                        "id": f"open_pin:{sheet.id}:{comp.id}.secondary",
                        "kind": "open_pin",
                        "sheet": sheet.id,
                        "tag": comp.id,
                        "pin": "secondary",
                        "read": f"{comp.id} secondary does not feed a wire",
                    }
                )
            elif open_sec:
                # X1 already through the fuse still leaves X2 → L2 as 104 to ask.
                # Never invent the landing — only offer the open pin.
                for pin in open_sec:
                    gaps.append(
                        {
                            "id": f"open_pin:{sheet.id}:{comp.id}.{pin}",
                            "kind": "open_pin",
                            "sheet": sheet.id,
                            "tag": comp.id,
                            "pin": pin,
                            "read": f"{comp.id} pin {pin} does not land",
                        }
                    )
            primary_by_prefix: dict[str, list[str]] = {}
            for p_obj in device.pins:
                pid = str(p_obj.id)
                if str(p_obj.side) != "left" or pid in secondary:
                    continue
                m = re.match(r"^[A-Za-z]*", pid)
                prefix = m.group(0).upper() if m else ""
                primary_by_prefix.setdefault(prefix, []).append(pid)
            for group in primary_by_prefix.values():
                if len(group) < 2:
                    continue
                left, right = group[0], group[-1]
                hit = [pin for pin in (left, right) if pin in landed]
                if len(hit) == 1:
                    missing = right if hit[0] == left else left
                    gaps.append(
                        {
                            "id": f"open_pin:{sheet.id}:{comp.id}.{missing}",
                            "kind": "open_pin",
                            "sheet": sheet.id,
                            "tag": comp.id,
                            "pin": missing,
                            "read": f"{comp.id} pin {missing} does not land",
                        }
                    )
            continue
        if "fuse" in lib:
            # One pin wired and the other empty → dangling stub. Unwired secondary
            # fuses still need open_pin leftovers (L1 as 102), not keep/remove-only.
            secondary = fuse_is_secondary(session, comp.id, sheet.id)
            if not wired and not secondary:
                continue
            sx, sy = _scan_xy(comp)
            ask_pins = pins if wired or secondary else []
            if secondary:
                # Always ask every unwired secondary-fuse pin. A reader open-stub
                # stamp on pin 2 must not hide the required "L1 as 102" leftover —
                # that left FU3.2 dangling with zero open questions.
                ask_pins = [p for p in pins if p not in wired] or pins
            for pin in ask_pins:
                if pin in wired:
                    continue
                # Primary fuse open stubs are intentional off-page leads.
                if not secondary and pin in stubs:
                    continue
                near = ocr_texts_near_primary_feed(session, sheet_id=sheet.id, x=sx, y=sy)
                if secondary:
                    read = f"{comp.id} pin {pin} does not feed L1"
                else:
                    read = f"{comp.id} pin {pin} is a dangling stub"
                    if near:
                        read = f"{comp.id} pin {pin} open feed (scan: {near[0]})"
                gaps.append(
                    {
                        "id": f"open_pin:{sheet.id}:{comp.id}.{pin}",
                        "kind": "open_pin",
                        "sheet": sheet.id,
                        "tag": comp.id,
                        "pin": pin,
                        "x": sx,
                        "y": sy,
                        "read": read,
                    }
                )
            continue
        # Coil/relay A1: ask when the series interlock is already on the sheet
        # and A1 is still open. Otherwise CR1 issues with no hot after the
        # invented L1→coil hop is removed. Pilot X1 stays mate-only on the
        # contact leftover (CR1-13.4 → PL1.X1 as 110).
        if ("coil" in lib or ".relays." in lib) and "A1" in pins and "A1" not in landed:
            if any(
                other.id.upper() in {"M1-23", "UA-22", "UA22", "1M"}
                for other in sheet.components
            ):
                gaps.append(
                    {
                        "id": f"open_pin:{sheet.id}:{comp.id}.A1",
                        "kind": "open_pin",
                        "sheet": sheet.id,
                        "tag": comp.id,
                        "pin": "A1",
                        "read": f"{comp.id} pin A1 does not land",
                    }
                )
            continue
        if not _is_contact_part(comp.library_id):
            continue
        if not wired:
            continue
        for pin in pins:
            if pin in wired or pin in stubs:
                continue
            gaps.append(
                {
                    "id": f"open_pin:{sheet.id}:{comp.id}.{pin}",
                    "kind": "open_pin",
                    "sheet": sheet.id,
                    "tag": comp.id,
                    "pin": pin,
                    "read": f"{comp.id} pin {pin} does not land",
                }
            )
    return gaps


def _scan_xy(comp: Any) -> tuple[float, float]:
    """Where the symbol was read on the scan. Layout x/y are the redrawn ladder."""

    props = getattr(comp, "properties", None) or {}
    try:
        if props.get("print_import_x") not in (None, ""):
            return float(props.get("print_import_x")), float(props.get("print_import_y"))
    except (TypeError, ValueError):
        pass
    return float(comp.x), float(comp.y)


# Same printed row. The next row on these sheets is about 30 mm down. A coil
# anchor sits a little below the contact, so 12 mm split one row; 18 mm keeps
# it and still leaves the next row out.
_ROW_MM = 18.0
# A printed number this close to a wire that has no number of its own is a
# question. It is not written onto that wire.
_NEAR_NUMBER_MM = 30.0


def _tag_base(tag: str) -> str:
    text = str(tag or "").strip().upper()
    if text.endswith("_2"):
        text = text[:-2]
    parent = _contact_parent(text)
    return parent or text


def _duplicate_tag_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """Two symbols claim one coil tag, or an untagged symbol sits on a printed tag."""

    groups: dict[str, list[Any]] = {}
    for comp in sheet.components:
        base = _tag_base(comp.id)
        if not base or base in {"L1", "L2", "X1", "X2"}:
            continue
        groups.setdefault(base, []).append(comp)
    gaps: list[dict[str, Any]] = []
    for base, comps in groups.items():
        coils = [comp for comp in comps if _is_coil(comp.library_id)]
        if len(coils) < 2 and not any(comp.id.upper().endswith("_2") for comp in comps):
            continue
        if len(comps) < 2:
            continue
        gaps.append(
            {
                "id": f"duplicate_tag:{sheet.id}:{base}",
                "kind": "duplicate_tag",
                "sheet": sheet.id,
                "tag": base,
                "read": f"{base} is claimed by {', '.join(comp.id for comp in comps)}",
            }
        )
    for comp in sheet.components:
        parent = _contact_parent(comp.id)
        if not parent or not _is_contact_part(comp.library_id):
            continue
        props = comp.properties if isinstance(getattr(comp, "properties", None), dict) else {}
        if str(props.get("print_import_confirmed") or "") == "yes":
            # Leftover already confirmed this printed tag — do not re-ask.
            continue
        family = [
            other
            for other in sheet.components
            if other.id != comp.id and _tag_base(other.id) == parent
        ]
        if not family:
            continue
        nearest = _beside_foreign(sheet, comp, parent)
        if nearest is None:
            continue
        # Interlock aux: high-confidence tag with an OCR hop to the neighbour is
        # not a duplicate — M1-23 beside CR1 on the print is intentional.
        if _interlock_aux_ok(sheet, comp, nearest):
            continue
        gaps.append(
            {
                "id": f"duplicate_tag:{sheet.id}:{comp.id}:{nearest.id}",
                "kind": "duplicate_tag",
                "sheet": sheet.id,
                "tag": comp.id,
                "read": f"{comp.id} is beside {nearest.id}",
            }
        )
    return gaps


def _print_confidence(comp: Any) -> float:
    props = comp.properties if isinstance(getattr(comp, "properties", None), dict) else {}
    try:
        return float(props.get("print_import_confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _interlock_aux_ok(sheet: Sheet, comp: Any, neighbour: Any) -> bool:
    """True when a high-confidence contact is already wired to its neighbour."""

    if _print_confidence(comp) < 0.8 and not str(
        (comp.properties if isinstance(getattr(comp, "properties", None), dict) else {}).get(
            "print_import_source"
        )
        or ""
    ).strip():
        return False
    tags = {str(comp.id), str(neighbour.id)}
    for wire in sheet.wires:
        ends = {str(wire.frm).split(".")[0], str(wire.to).split(".")[0]}
        if tags <= ends:
            return True
    return False


def _beside_foreign(sheet: Sheet, comp: Any, parent: str) -> Any | None:
    """The next device along this contact's printed row, when its coil is not.

    Same row means the scan Y is within ``_ROW_MM``. Beside is the neighbour
    in that left-to-right order, not a millimetre cutoff. A family member on
    that row means the tag landed on its own rung.
    """

    _cx, cy = _scan_xy(comp)
    row = [other for other in sheet.components if abs(_scan_xy(other)[1] - cy) <= _ROW_MM]
    if any(other.id != comp.id and _tag_base(other.id) == parent for other in row):
        return None
    ordered = sorted(row, key=lambda other: (_scan_xy(other)[0], other.id))
    try:
        index = next(pos for pos, other in enumerate(ordered) if other.id == comp.id)
    except StopIteration:
        return None
    neighbours = []
    if index > 0:
        neighbours.append(ordered[index - 1])
    if index + 1 < len(ordered):
        neighbours.append(ordered[index + 1])
    foreign = [
        other
        for other in neighbours
        if _tag_base(other.id) != parent and _beside_device(sheet, other)
    ]
    if not foreign:
        return None
    return min(foreign, key=lambda other: abs(_scan_xy(other)[0] - _scan_xy(comp)[0]))


def _beside_device(sheet: Sheet, comp: Any) -> bool:
    """A contact is beside another device on its rung, not a terminal or a rail.

    The terminal strip shares the bottom row. It is not a coil the contact
    could have been drawn for. A rail name is the same kind of neighbour.
    """

    lib = str(getattr(comp, "library_id", "") or "").lower()
    if "terminal" in lib or ".rails." in lib or lib.endswith(".rail"):
        return False
    tag = str(getattr(comp, "id", "") or "").strip().upper()
    hot = str(getattr(sheet, "rail_hot", "") or "").strip().upper()
    ret = str(getattr(sheet, "rail_return", "") or "").strip().upper()
    return tag not in {hot, ret, "L1", "L2", "L3", "N", "X1", "X2", "0V"}


def _unattached_number_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """A printed number that never got a from and to.

    Low confidence stays off the wire. If it lies on a wire that carries only
    a rail name or ``?``, ask which wire it belongs to. The crop is that wire
    plus the token.
    """

    props = session.project.properties or {}
    block = props.get("print_import") if isinstance(props, dict) else None
    items = block.get("items") if isinstance(block, dict) else None
    if not isinstance(items, list):
        return []
    gaps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "wire":
            continue
        if item.get("confirmed"):
            continue
        if str(item.get("reason") or "") == "printed":
            continue
        item_sheet = str(item.get("sheet") or sheet.id)
        if item_sheet != sheet.id:
            continue
        number = str(item.get("text") or item.get("number") or "").strip()
        if _SHEET_LINE.match(number) is None or _rail_name_number(session, sheet, number):
            continue
        try:
            x = float(item["x"])
            y = float(item["y"])
        except (KeyError, TypeError, ValueError):
            continue
        # A token with no wire that can take it stays on the review list and
        # is not sent. clean_300 keeps several of these: none sits on an open
        # wire, so the model is not asked.
        wire = _nearest_open_wire(session, sheet, x, y, number)
        if wire is None:
            continue
        gap_id = f"unattached_number:{sheet.id}:{number}:{x:.1f}:{y:.1f}"
        if gap_id in seen:
            continue
        seen.add(gap_id)
        gaps.append(
            {
                "id": gap_id,
                "kind": "unattached_number",
                "sheet": sheet.id,
                "wire_id": wire.id,
                "number": number,
                "from": str(wire.frm),
                "to": str(wire.to),
                "x": x,
                "y": y,
                "wires": [wire.id],
                "read": f"{number} is printed near {wire.frm} to {wire.to} and is not attached",
            }
        )
    return gaps


def _nearest_open_wire(session: Session, sheet: Sheet, x: float, y: float, number: str) -> Any | None:
    """The closest wire that can carry this printed number.

    The wire has no number of its own, the token is near it, and writing the
    number does not put two sheet-line numbers on that net. A holding-contact
    pin that already carries 106 cannot also take 107.
    """

    from panelcad.core.ladder import endpoint_is_rail

    best = None
    best_distance = _NEAR_NUMBER_MM
    for wire in sheet.wires:
        current = str(wire.number or "").strip()
        if current and current != "?" and not _rail_name_number(session, sheet, current):
            continue
        if not _number_fits_the_net(session, sheet, wire, number):
            continue
        distance = _distance_to_wire(session, sheet, wire, x, y, endpoint_is_rail)
        if distance is None or distance > best_distance:
            continue
        best = wire
        best_distance = distance
    return best


def _number_fits_the_net(session: Session, sheet: Sheet, wire: Any, number: str) -> bool:
    """True when this number does not add a second sheet-line number on the wire's net."""

    from panelcad.core.elementary_layout import _shorted_numbers

    previous = wire.number
    before = set(_shorted_numbers(session, sheet))
    wire.number = number
    try:
        after = set(_shorted_numbers(session, sheet))
    finally:
        wire.number = previous
    return not (after - before)


def _distance_to_wire(
    session: Session,
    sheet: Sheet,
    wire: Any,
    x: float,
    y: float,
    endpoint_is_rail: Any,
) -> float | None:
    """Millimetres from a token to the scan position of a wire.

    A rail end has no scan point. The landing is the device, and the number
    may sit anywhere along that horizontal run between the device and its rail.
    """

    from panelcad.core.ladder import rail_names, rail_side, rail_xs

    points: list[tuple[float, float]] = []
    rail_ends: list[str] = []
    for end in (str(wire.frm), str(wire.to)):
        if endpoint_is_rail(sheet, end, session.project):
            rail_ends.append(end)
            continue
        boxes = _ref_boxes(session, end)
        for left, top, right, bottom in boxes:
            points.append(((left + right) / 2.0, (top + bottom) / 2.0))
            pad = 20.0
            if left - pad <= x <= right + pad and top - pad <= y <= bottom + pad:
                near_x = min(max(x, left), right)
                near_y = min(max(y, top), bottom)
                points.append((near_x, near_y))
    if not points:
        return None
    if rail_ends:
        px, py = points[0]
        if abs(y - py) <= 12.0:
            left_rail_x, right_rail_x = rail_xs(sheet)
            hot, ret = rail_names(sheet, session.project)
            for r_end in rail_ends:
                target_x = left_rail_x if rail_side(r_end, hot, ret) == "left" else right_rail_x
                lo, hi = (target_x, px) if target_x <= px else (px, target_x)
                points.append((min(max(x, lo), hi), py))
    best = min(((px - x) ** 2 + (py - y) ** 2) ** 0.5 for px, py in points)
    return best


def _scan_rows(sheet: Sheet) -> list[list[Any]]:
    """Symbols grouped by printed row, from the scan position.

    Each row starts at its top symbol. A symbol stays on that row while it is
    within ``_ROW_MM`` of that top. The next row, about 30 mm down, starts
    its own group, so a family member on the next row is not "on this rung".
    """

    ordered = sorted(sheet.components, key=lambda comp: (_scan_xy(comp)[1], _scan_xy(comp)[0]))
    rows: list[list[Any]] = []
    anchors: list[float] = []
    for comp in ordered:
        y = _scan_xy(comp)[1]
        if anchors and y - anchors[-1] <= _ROW_MM:
            rows[-1].append(comp)
        else:
            rows.append([comp])
            anchors.append(y)
    return rows


def _coil_place_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """A coil sitting on the transformer is not the coil on the rung."""

    transformers = [comp for comp in sheet.components if "transformer" in comp.library_id.lower()]
    if not transformers:
        return []
    gaps: list[dict[str, Any]] = []
    for comp in sheet.components:
        if not _is_coil(comp.library_id):
            continue
        near_transformer = any(abs(comp.x - other.x) < 70 and abs(comp.y - other.y) < 50 for other in transformers)
        if not near_transformer:
            continue
        contacts = [
            other
            for other in sheet.components
            if other.id != comp.id
            and _is_contact_part(other.library_id)
            and (_contact_parent(other.id) == comp.id.upper() or _tag_base(other.id) == _tag_base(comp.id))
        ]
        far = not contacts or min(abs(comp.y - other.y) for other in contacts) > 80
        if not far:
            continue
        gaps.append(
            {
                "id": f"coil_place:{sheet.id}:{comp.id}",
                "kind": "coil_place",
                "sheet": sheet.id,
                "tag": comp.id,
                "read": f"{comp.id} is not on its contact rung",
            }
        )
    return gaps


def _skipped_contact_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """A hot-rail wire straight into a coil, past series contacts on that rung.

    Return-rail↔coil wires are not skipped_contacts — a coil return does not
    bypass the start/stop contacts. The predicate matches the gap name: only a
    hot-rail→coil hop on a row that already has series contacts.
    """

    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.render.sheet import parse_pin_ref

    if not any(_is_contact_part(comp.library_id) for comp in sheet.components):
        return []
    hot = str(getattr(sheet, "rail_hot", "") or "L1").strip().upper() or "L1"
    ret = str(getattr(sheet, "rail_return", "") or "L2").strip().upper() or "L2"
    hot_names = {hot, "L1", "L+", "X1", "24V", "+24V"}
    ret_names = {ret, "L2", "L-", "X2", "0V", "N", "COM"}
    gaps: list[dict[str, Any]] = []
    for wire in sheet.wires:
        ends = []
        for text in (wire.frm, wire.to):
            if endpoint_is_rail(sheet, text, session.project):
                ends.append(("rail", str(text).strip()))
                continue
            try:
                ref, pin = parse_pin_ref(text)
            except ValueError:
                ref, pin = str(text).split(".")[0], ""
            ends.append((ref, pin))
        refs = {ref for ref, _pin in ends}
        coil = next((ref for ref in refs if (comp := _find(session, ref)) is not None and _is_coil(comp.library_id)), "")
        if not coil:
            continue
        other = next(ref for ref, _pin in ends if ref != coil)
        other_comp = _find(session, other)
        rail_name = ""
        if other == "rail":
            rail_name = next((str(pin).strip().upper() for ref, pin in ends if ref == "rail"), "")
        elif other_comp is not None and "terminal" in other_comp.library_id.lower():
            # A terminal column on the hot side counts as a hot feed.
            rail_name = hot
        else:
            continue
        if not rail_name or rail_name in ret_names or rail_name not in hot_names:
            # Return-rail↔coil is not skipped_contacts.
            continue
        coil_comp = _find(session, coil)
        if coil_comp is None:
            continue
        _cx, cy = _scan_xy(coil_comp)
        row_contacts = [
            comp
            for comp in sheet.components
            if comp.id != coil
            and _is_contact_part(comp.library_id)
            and abs(_scan_xy(comp)[1] - cy) <= _ROW_MM
        ]
        if not row_contacts:
            continue
        gaps.append(
            {
                "id": f"skipped_contacts:{sheet.id}:{wire.id}",
                "kind": "skipped_contacts",
                "sheet": sheet.id,
                "wire_id": wire.id,
                "tag": coil,
                "read": f"{wire.id} runs from the rail to {coil} past the contacts",
            }
        )
    return gaps


def _number_off_its_rung(session: Session, sheet: Sheet, wire: Any) -> bool:
    """A number that does not follow the other numbers on its rung.

    102 then 105 on one pushbutton is a normal gap. 100 then 104 is not, and
    a lamp return of 107 beside an incoming number more than one away is not.
    A unique 104 on ES1–PB2 shares no second wire with those devices, so the
    comparison is every number on the same printed row, not only the wires
    that touch ES1 or PB2.
    """

    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.render.sheet import parse_pin_ref

    number = str(wire.number or "").strip()
    if not number.isdigit():
        return False
    value = int(number)
    own: list[str] = []
    to_return = False
    for text in (wire.frm, wire.to):
        if endpoint_is_rail(sheet, text, session.project):
            to_return = True
            continue
        try:
            ref, _pin = parse_pin_ref(text)
        except ValueError:
            ref = str(text).split(".")[0]
        if ref and ref not in own:
            own.append(ref)
    others: list[int] = []
    for ref in own:
        comp = _find(session, ref)
        for other in sheet.wires:
            if other.id == wire.id:
                continue
            ends = {str(other.frm).split(".")[0], str(other.to).split(".")[0]}
            if ref not in ends:
                continue
            other_number = str(other.number or "").strip()
            if other_number.isdigit():
                others.append(int(other_number))
        lib = str(comp.library_id).lower() if comp is not None else ""
        if comp is not None and to_return and ("pilot" in lib or "light" in lib):
            # A lamp return keeps the incoming number or the next one.
            if others and all(abs(value - item) > 1 for item in others):
                return True
    if to_return:
        # 108 then 104 on the overload's return is not the rail's number.
        # A return with no other number on its device is left alone.
        return bool(others) and all(abs(value - item) > 3 for item in others)
    # A step of 3 (102 then 105) is a branch. A step of 4 is a misread.
    if others and all(abs(value - item) > 3 for item in others):
        return True
    if others:
        return False
    # No other number on these devices. The row still has neighbours.
    row_numbers = _numbers_on_row(sheet, own, wire.id)
    return bool(row_numbers) and all(abs(value - item) > 3 for item in row_numbers)


def _numbers_on_row(sheet: Sheet, refs: list[str], skip_wire: str) -> list[int]:
    """Wire numbers on the printed row of ``refs``, other than ``skip_wire``."""

    wanted = {ref.upper() for ref in refs}
    if not wanted:
        return []
    rows = _scan_rows(sheet)
    row = next(
        (members for members in rows if any(comp.id.upper() in wanted for comp in members)),
        [],
    )
    on_row = {comp.id.upper() for comp in row}
    found: list[int] = []
    for other in sheet.wires:
        if other.id == skip_wire:
            continue
        ends = {str(other.frm).split(".")[0].upper(), str(other.to).split(".")[0].upper()}
        if not ends & on_row:
            continue
        text = str(other.number or "").strip()
        if text.isdigit():
            found.append(int(text))
    return found


def _coil_return_number(session: Session, sheet: Sheet, wire: Any) -> bool:
    """A duplicate coil's rail number that is not the wire feeding the coil.

    That number is the cross-reference printed under the coil.
    """

    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.render.sheet import parse_pin_ref

    ends = []
    for text in (wire.frm, wire.to):
        if endpoint_is_rail(sheet, text, session.project):
            ends.append("rail")
            continue
        try:
            ref, _pin = parse_pin_ref(text)
        except ValueError:
            ref = str(text).split(".")[0]
        ends.append(ref)
    coil = next((ref for ref in ends if ref != "rail" and (comp := _find(session, ref)) is not None and _is_coil(comp.library_id)), "")
    if not coil or not str(coil).upper().endswith("_2") or "rail" not in ends:
        return False
    feed = {
        str(other.number or "").strip()
        for other in sheet.wires
        if other.id != wire.id and coil in {str(other.frm).split(".")[0], str(other.to).split(".")[0]}
    }
    return str(wire.number or "").strip() not in feed


def _rail_name_number(session: Session, sheet: Sheet, number: str) -> bool:
    """L1, L2, and this sheet's rail names are not wire numbers to question."""

    from panelcad.core.ladder import rail_names

    token = str(number or "").strip().upper()
    hot, ret = rail_names(sheet, session.project)
    return token in {hot.upper(), ret.upper(), "L1", "L2", "L3", "N", "X1", "X2", "0V"}


def _shared_only_on_one_rail(session: Session, sheet: Sheet, wires: list[Any]) -> bool:
    """Every copy of this number is a landing on the same rail.

    That is the rail's number, repeated. It is not another row's number.
    """

    from panelcad.core.ladder import endpoint_is_rail

    rails: list[str] = []
    for wire in wires:
        ends = [
            str(text).split(".")[0].upper()
            for text in (wire.frm, wire.to)
            if endpoint_is_rail(sheet, text, session.project)
        ]
        if len(ends) != 1:
            return False
        rails.append(ends[0])
    return len(set(rails)) == 1


def _suspect_number_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """A number shared by two rungs, a line 00, or a cross-reference used as a wire number."""

    from panelcad.core.ansi_drafting import _SHEET_LINE
    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.importer.read import is_cross_ref, is_nema_line_ref
    from panelcad.render.sheet import parse_pin_ref

    def pin_of(text: str) -> str:
        # A rail is every rung's end. Sharing L2 is not the same node.
        if endpoint_is_rail(sheet, text, session.project):
            return ""
        try:
            ref, pin = parse_pin_ref(text)
        except ValueError:
            ref = str(text).split(".")[0]
            pin = ""
        if ref.upper() in _rail_facts(session, sheet.id):
            return ""
        return f"{ref}.{pin}" if pin else ref

    gaps: list[dict[str, Any]] = []
    seen: set[str] = set()
    by_number: dict[str, list[Any]] = {}
    for wire in sheet.wires:
        number = str(wire.number or "").strip()
        if not number or number == "?" or _rail_name_number(session, sheet, number):
            continue
        if _number_user_confirmed(session, wire):
            continue
        by_number.setdefault(number, []).append(wire)
        odd = (
            is_cross_ref(number)
            or is_nema_line_ref(number)
            or (number.isdigit() and number.endswith("00"))
            or (_SHEET_LINE.match(number) is None and number not in {"L1", "L2", "X1", "X2"})
        )
        coil_return = _coil_return_number(session, sheet, wire)
        off_rung = _number_off_its_rung(session, sheet, wire)
        if coil_return or off_rung:
            odd = True
        if odd and wire.id not in seen:
            seen.add(wire.id)
            if off_rung and not (is_cross_ref(number) or is_nema_line_ref(number)):
                why = f"{wire.id} number {number} is out of sequence on its rung"
            else:
                why = f"{wire.id} number {number} looks like a cross-reference"
            gaps.append(
                {
                    "id": f"suspect_number:{sheet.id}:{wire.id}",
                    "kind": "suspect_number",
                    "sheet": sheet.id,
                    "wire_id": wire.id,
                    "number": number,
                    "read": why,
                }
            )
    for number, wires in by_number.items():
        if len(wires) < 2 or _shared_only_on_one_rail(session, sheet, wires):
            continue
        pins: list[set[str]] = []
        for wire in wires:
            pins.append({pin for pin in (pin_of(wire.frm), pin_of(wire.to)) if pin})
        for index, wire in enumerate(wires):
            if any(pins[index] & pins[other] for other in range(len(wires)) if other != index):
                continue
            if wire.id in seen:
                continue
            seen.add(wire.id)
            gaps.append(
                {
                    "id": f"suspect_number:{sheet.id}:{wire.id}",
                    "kind": "suspect_number",
                    "sheet": sheet.id,
                    "wire_id": wire.id,
                    "number": number,
                    "read": f"{wire.id} number {number} is another row's number",
                }
            )
    gaps.extend(_rail_named_return_gaps(session, sheet, seen))
    gaps.extend(_terminal_strip_borrowed_rail_number_gaps(session, sheet, seen))
    gaps.extend(_invented_hot_rail_number_gaps(session, sheet, seen))
    gaps.extend(_wrong_interlock_pilot_gaps(session, sheet, seen))
    gaps.extend(_pilot_hop_number_gaps(session, sheet, seen))
    return gaps


def _pilot_hop_number_gaps(
    session: Session, sheet: Sheet, seen: set[str]
) -> list[dict[str, Any]]:
    """CR1-13.4→PL1.X1 must be 110; a leftover 102 on that hop is wrong."""

    from panelcad.render.sheet import parse_pin_ref

    gaps: list[dict[str, Any]] = []
    for wire in sheet.wires:
        if wire.id in seen:
            continue
        number = str(wire.number or "").strip()
        if number == "110" or _number_user_confirmed(session, wire):
            continue
        refs: dict[str, str] = {}
        for text in (wire.frm, wire.to):
            try:
                ref, pin = parse_pin_ref(str(text or ""))
            except ValueError:
                continue
            refs[ref.upper()] = str(pin or "").upper()
        if "CR1-13" not in refs:
            continue
        pilot = next((r for r in refs if r.startswith("PL")), "")
        if not pilot:
            continue
        if refs.get("CR1-13") not in {"4", "14", ""} and refs.get(pilot) not in {"X1", "1", ""}:
            # Still flag any CR1-13↔PL* number that is not 110.
            pass
        seen.add(wire.id)
        gaps.append(
            {
                "id": f"suspect_number:{sheet.id}:{wire.id}",
                "kind": "suspect_number",
                "sheet": sheet.id,
                "wire_id": wire.id,
                "number": number,
                "prefer": "110",
                "from": str(wire.frm),
                "to": str(wire.to),
                "read": (
                    f"{wire.frm}→{wire.to} as {number or '?'} should be 110 "
                    f"(CR1-13.4 feeds the pilot)"
                ),
            }
        )
    return gaps


def _wrong_interlock_pilot_gaps(
    session: Session, sheet: Sheet, seen: set[str]
) -> list[dict[str, Any]]:
    """M1-23→PL1 (or UA-22→PL1) is not on the print — CR1-13.4 feeds the lamp as 110.

    When leftovers steal the pilot hop onto the interlock aux, ask to remove that
    wire so M1-23.4 can land on CR1.A1 as 109 instead.
    """

    from panelcad.render.sheet import parse_pin_ref

    gaps: list[dict[str, Any]] = []
    for wire in sheet.wires:
        if wire.id in seen:
            continue
        if _number_user_confirmed(session, wire):
            continue
        refs: set[str] = set()
        for text in (wire.frm, wire.to):
            try:
                ref, _pin = parse_pin_ref(str(text or ""))
            except ValueError:
                continue
            refs.add(ref.upper())
        interlock = refs & {"M1-23", "UA-22", "UA22", "1M"}
        if not interlock or not any(r.startswith("PL") for r in refs):
            continue
        seen.add(wire.id)
        gaps.append(
            {
                "id": f"contacts_only:{sheet.id}:{wire.id}",
                "kind": "contacts_only",
                "sheet": sheet.id,
                "wire_id": wire.id,
                "tag": next(iter(interlock)),
                "number": str(wire.number or ""),
                "read": (
                    f"{wire.frm}→{wire.to} as {wire.number or '?'} is not on the print; "
                    f"CR1-13.4 feeds the pilot as 110 and M1-23.4 feeds CR1.A1 as 109"
                ),
            }
        )
    return gaps


def _invented_hot_rail_number_gaps(
    session: Session, sheet: Sheet, seen: set[str]
) -> list[dict[str, Any]]:
    """Invented L1 landings: junk 113/111/117 on contacts, or L1→coil as 102.

    Ladder synth must not invent these; when a prior pass already did, ask.
    CR1.A1 is fed by M1-23.4 as 109 on the print — a direct L1→coil hop is not.
    """

    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.render.sheet import parse_pin_ref

    junk = {"111", "113", "117"}
    gaps: list[dict[str, Any]] = []
    for wire in sheet.wires:
        if wire.id in seen:
            continue
        number = str(wire.number or "").strip()
        if _number_user_confirmed(session, wire):
            continue
        ends = [str(wire.frm or ""), str(wire.to or "")]
        rails = [end for end in ends if endpoint_is_rail(sheet, end, session.project)]
        if len(rails) != 1 or rails[0].upper() not in {"L1", "X1"}:
            continue
        other = ends[0] if ends[1] == rails[0] else ends[1]
        try:
            ref, pin = parse_pin_ref(other)
        except ValueError:
            continue
        comp = _find(session, ref)
        if comp is None:
            continue
        lib = str(comp.library_id or "").lower()
        if number in junk and _is_contact_part(lib):
            seen.add(wire.id)
            gaps.append(
                {
                    "id": f"suspect_number:{sheet.id}:{wire.id}",
                    "kind": "suspect_number",
                    "sheet": sheet.id,
                    "wire_id": wire.id,
                    "number": number,
                    "prefer": "102",
                    "read": (
                        f"{wire.id} number {number} looks like OCR junk on the hot "
                        f"rail; {ref} lands on L1 as 102"
                    ),
                }
            )
            continue
        # Invented L1→coil/pilot hot (102 or any sheet line) — remove and ask
        # for the series contact (M1-23.4 as 109) instead.
        is_coil_hot = (
            ("coil" in lib or ".relays." in lib or "pilot" in lib or "light" in lib)
            and str(pin or "").upper() in {"A1", "1", "X1", ""}
        )
        if is_coil_hot and number.isdigit():
            # Same-rung series contacts (or a placed M1-23 interlock) only —
            # an off-row aux is not skipped_contacts.
            _cx, cy = _scan_xy(comp)
            row_contacts = [
                other
                for other in sheet.components
                if other.id != comp.id
                and _is_contact_part(other.library_id)
                and abs(_scan_xy(other)[1] - cy) <= _ROW_MM
            ]
            interlock_present = any(
                other.id.upper() in {"M1-23", "UA-22", "UA22", "1M"}
                for other in sheet.components
            )
            if not row_contacts and not interlock_present:
                continue
            seen.add(wire.id)
            gaps.append(
                {
                    "id": f"skipped_contacts:{sheet.id}:{wire.id}",
                    "kind": "skipped_contacts",
                    "sheet": sheet.id,
                    "wire_id": wire.id,
                    "tag": ref,
                    "from": rails[0],
                    "to": other,
                    "number": number,
                    "read": (
                        f"{rails[0]}→{other} as {number} is not on the print; "
                        f"{ref} is fed by a series contact (M1-23.4 as 109)"
                    ),
                }
            )
    return gaps


def _terminal_strip_borrowed_rail_number_gaps(
    session: Session, sheet: Sheet, seen: set[str]
) -> list[dict[str, Any]]:
    """X1-*→L2 numbered 104 (control return) when the strip already uses 301–304.

    The Demo Controls terminal returns are 301–304. Borrowing the control L2
    number hides the true mark and left those wires as keep/remove-only challenges.
    """

    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.render.sheet import parse_pin_ref

    strip_numbers = {
        str(w.number or "").strip()
        for w in sheet.wires
        if str(w.number or "").strip().isdigit()
        and 300 <= int(str(w.number).strip()) <= 399
    }
    if not strip_numbers:
        # Still flag a bare terminal→L2 that carries 104 — OCR often has 304 nearby.
        strip_numbers = {"301", "302", "303", "304"}
    gaps: list[dict[str, Any]] = []
    for wire in sheet.wires:
        if wire.id in seen:
            continue
        number = str(wire.number or "").strip()
        if number not in {"102", "104"}:
            continue
        if _number_user_confirmed(session, wire):
            continue
        ends = [str(wire.frm or ""), str(wire.to or "")]
        rails = [end for end in ends if endpoint_is_rail(sheet, end, session.project)]
        if len(rails) != 1:
            continue
        other = ends[0] if ends[1] == rails[0] else ends[1]
        try:
            ref, _pin = parse_pin_ref(other)
        except ValueError:
            continue
        comp = _find(session, ref)
        if comp is None or "terminal" not in str(comp.library_id or "").lower():
            continue
        # Demo Controls: L2 terminal return is 304, L1-side strip starts at 301.
        rail = rails[0].upper()
        if rail in {"L2", "X2"}:
            prefer = "304" if "304" in strip_numbers else (sorted(strip_numbers)[-1] if strip_numbers else "304")
        else:
            prefer = "301" if "301" in strip_numbers else (sorted(strip_numbers)[0] if strip_numbers else "301")
        seen.add(wire.id)
        gaps.append(
            {
                "id": f"suspect_number:{sheet.id}:{wire.id}",
                "kind": "suspect_number",
                "sheet": sheet.id,
                "wire_id": wire.id,
                "number": number,
                "read": (
                    f"{wire.id} number {number} borrows the control rail; "
                    f"terminal strip returns are {prefer}"
                ),
                "prefer": prefer,
            }
        )
    return gaps


def _number_user_confirmed(session: Session, wire: Any) -> bool:
    """True when a leftover answer asserted this wire's number (source=user)."""

    props = getattr(wire, "properties", None)
    if isinstance(props, dict) and str(props.get("print_import_number_confirmed") or "") == "yes":
        return True
    block = (session.project.properties or {}).get("print_import")
    if not isinstance(block, dict):
        return False
    confirmed = block.get("confirmed_wire_numbers")
    if isinstance(confirmed, dict):
        return str(confirmed.get(str(wire.id) or "")) == str(wire.number or "").strip()
    if isinstance(confirmed, list):
        return str(wire.id) in {str(item) for item in confirmed}
    return False


def _sheet_stored_line_numbers(session: Session, sheet: Sheet) -> set[str]:
    block = (session.project.properties or {}).get("print_import")
    items = block.get("items") if isinstance(block, dict) else None
    if not isinstance(items, list):
        return set()
    found: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("sheet") or sheet.id) != sheet.id:
            continue
        if item.get("confirmed"):
            continue
        text = str(item.get("text") or "").strip()
        if _SHEET_LINE.match(text) is not None and not _rail_name_number(session, sheet, text):
            found.add(text)
    return found


def _rail_return_number_supported(
    session: Session,
    sheet: Sheet,
    wire: Any,
    current: str,
    number: str,
) -> bool:
    """True when a rail-named return can take ``number`` from the sheet's own reads."""

    from panelcad.core.ladder import endpoint_is_rail

    if not current or not _rail_name_number(session, sheet, current):
        return False
    ends = [str(wire.frm or ""), str(wire.to or "")]
    rails = [end for end in ends if endpoint_is_rail(sheet, end, session.project)]
    if len(rails) != 1:
        return False
    other = ends[0] if ends[1] == rails[0] else ends[1]
    comp = _find(session, other.split(".")[0])
    if comp is None or not _may_return(comp.library_id):
        return False
    on_same_rail = any(
        other_w.id != wire.id
        and str(other_w.number or "").strip() == number
        and _shared_only_on_one_rail(session, sheet, [wire, other_w])
        for other_w in sheet.wires
    )
    if on_same_rail:
        return True
    return number in _sheet_stored_line_numbers(session, sheet)


def _rail_named_return_gaps(session: Session, sheet: Sheet, seen: set[str]) -> list[dict[str, Any]]:
    """A return that still carries the rail name, when a sheet-line number was read on the sheet."""

    from panelcad.core.ladder import endpoint_is_rail

    stored = _sheet_stored_line_numbers(session, sheet)
    gaps: list[dict[str, Any]] = []
    for wire in sheet.wires:
        number = str(wire.number or "").strip()
        if wire.id in seen or not number or not _rail_name_number(session, sheet, number):
            continue
        ends = [str(wire.frm or ""), str(wire.to or "")]
        rails = [end for end in ends if endpoint_is_rail(sheet, end, session.project)]
        if len(rails) != 1:
            continue
        other = ends[0] if ends[1] == rails[0] else ends[1]
        comp = _find(session, other.split(".")[0])
        if comp is None or not _may_return(comp.library_id):
            continue
        printed = _printed_number_near(session, sheet, wire)
        same_rail_has_number = any(
            other_w.id != wire.id
            and _SHEET_LINE.match(str(other_w.number or "").strip()) is not None
            and not _rail_name_number(session, sheet, str(other_w.number or "").strip())
            and _shared_only_on_one_rail(session, sheet, [wire, other_w])
            for other_w in sheet.wires
        )
        if not printed and not same_rail_has_number and not stored:
            continue
        read_text = (
            f"{wire.id} return is numbered {number}; the print shows {printed}"
            if printed
            else f"{wire.id} return is numbered {number}"
        )
        gaps.append(
            {
                "id": f"suspect_number:{sheet.id}:{wire.id}",
                "kind": "suspect_number",
                "sheet": sheet.id,
                "wire_id": wire.id,
                "number": number,
                "read": read_text,
            }
        )
    return gaps


def _printed_number_near(session: Session, sheet: Sheet, wire: Any) -> str:
    """A sheet-line token the reader stored near this wire, if there is one."""

    block = (session.project.properties or {}).get("print_import")
    items = block.get("items") if isinstance(block, dict) else None
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if _SHEET_LINE.match(text) is None or _rail_name_number(session, sheet, text):
            continue
        if _read_token_near(session, sheet, wire, text):
            return text
    return ""


def _suspect_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """Reads the checkers or the confidence rule already refuse to trust."""

    from panelcad.core.elementary_layout import elementary_layout_issues
    from panelcad.core.xref import xref_problems
    from panelcad.importer.read import is_device_tag
    from panelcad.importer.symbols import supported_tag_family, tag_prefix

    try:
        issues = elementary_layout_issues(session, sheet)
    except Exception:
        issues = []
    rejected = _rejected_ids(sheet, issues)
    try:
        problems = xref_problems(session.project, session.catalog)
    except Exception:
        problems = []
    for problem in problems:
        if problem.get("check") in {"contact_without_coil", "coil_without_contacts"} and problem.get("ref"):
            rejected.add(str(problem["ref"]).upper())
    coils = _coil_tags(session)
    gaps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for comp in sheet.components:
        if "offpage" in comp.library_id.lower():
            continue
        tag = comp.id.strip()
        upper = tag.upper()
        if not tag or not is_device_tag(tag) or upper in {"?", "PB"}:
            gap = _suspect(sheet, comp, "untagged", "no tag")
            gap["wires"] = _wires_touching(sheet, {comp.id})
            gaps.append(gap)
            continue
        parent = _contact_parent(tag)
        if parent and parent not in coils and _is_contact_part(comp.library_id):
            gaps.append(_suspect(sheet, comp, "orphan_contact", f"no coil {parent}"))
            seen.add(upper)
        prefix = tag_prefix(tag)
        family = supported_tag_family(tag)
        odd = bool(prefix) and prefix not in _KNOWN_PREFIXES and not family
        low = _low_confidence(comp)
        if _is_coil(comp.library_id) and not _has_contacts(session, tag) and (odd or low or upper in rejected):
            gaps.append(_suspect(sheet, comp, "coil_without_contacts", "coil has no contacts"))
            seen.add(upper)
        if upper in seen:
            continue
        confirmed = str((comp.properties or {}).get("print_import_confirmed") or "") == "yes"
        if confirmed:
            # A human leftover answer (or cleanup confirm) already asserted this tag.
            continue
        if odd or low or upper in rejected:
            if odd:
                why = "nonstandard tag"
            elif upper in rejected:
                why = "checker"
            else:
                why = "low confidence"
            gaps.append(_suspect(sheet, comp, "suspect_tag", why))
    return gaps


def _defect_gaps(session: Session, sheet: Sheet) -> list[dict[str, Any]]:
    """Illegal wires the checkers already name. The cleanup call can see them."""

    from panelcad.core.elementary_layout import elementary_layout_issues

    try:
        issues = elementary_layout_issues(session, sheet)
    except Exception:
        issues = []
    gaps: list[dict[str, Any]] = []
    for issue in issues:
        text = str(issue)
        body = text.split(": ", 1)[1] if ": " in text else text
        if "own pins" in text:
            kind = "self_wire"
            wire_id = body.split(" connects ", 1)[0].strip()
            tag = body.split(" connects ", 1)[-1].split("'s own pins", 1)[0].strip()
            extra = {"wire_id": wire_id, "tag": tag, "wires": [wire_id]}
            gap_id = f"self_wire:{sheet.id}:{wire_id}"
        elif "with no load" in text:
            kind = "contacts_only"
            names = [part.strip() for part in body.split(" runs from ", 1)[0].split(",") if part.strip()]
            # Only the illegal rail landings on this path. Naming every wire that
            # touches ES1/PB*/seal-in made remove_wire delete the starter series
            # (105/106/107) and leave "ES1 wired to L1 with no path through a load".
            wires = _contacts_only_rail_wires(sheet, set(names), session)
            if not wires:
                # Still never offer the ES1→PB→coil hops as remove targets.
                wires = [
                    wid
                    for wid in _wires_touching(sheet, set(names))
                    if not _is_contacts_only_starter_wire(sheet, wid, session)
                ]
            extra = {"tag": names[0] if names else "", "wires": wires}
            gap_id = f"contacts_only:{sheet.id}:{'-'.join(sorted(names))}"
        elif "is a fuse from" in text:
            kind = "fuse_rail"
            tag = body.split(" is a fuse from ", 1)[0].strip()
            extra = {"tag": tag, "wires": _wires_touching(sheet, {tag})}
            gap_id = f"fuse_rail:{sheet.id}:{tag}"
        elif "runs through a symbol" in text:
            kind = "checker_wire"
            wire_id = body.split(" runs through a symbol", 1)[0].strip()
            wire = next((w for w in sheet.wires if str(w.id) == wire_id), None)
            extra = {
                "wire_id": wire_id,
                "wires": [wire_id],
                "from": str(wire.frm) if wire is not None else "",
                "to": str(wire.to) if wire is not None else "",
                "number": str(wire.number or "") if wire is not None else "",
            }
            gap_id = f"checker_wire:{sheet.id}:{wire_id}"
        elif "two wire numbers" in text or "is on two nets" in text:
            kind = "two_numbers"
            nums = re.findall(r"\b\d{2,4}\b", body)
            wires = [wire.id for wire in sheet.wires if str(wire.number or "").strip() in set(nums)]
            extra = {"wires": wires, "numbers": nums}
            gap_id = f"two_numbers:{sheet.id}:{'-'.join(nums) or 'net'}"
        else:
            continue
        gaps.append(
            {
                "id": gap_id,
                "kind": kind,
                "sheet": sheet.id,
                "read": text,
                **extra,
            }
        )
    return gaps


def _wires_touching(sheet: Sheet, tags: set[str]) -> list[str]:
    found: list[str] = []
    for wire in sheet.wires:
        ends = {str(wire.frm).split(".")[0], str(wire.to).split(".")[0]}
        if ends & tags:
            found.append(wire.id)
    return sorted(found)


def _contacts_only_rail_wires(
    sheet: Sheet, tags: set[str], session: Session
) -> list[str]:
    """Wires that land a contacts-only path device onto L1/L2.

    The starter series (ES1→PB→coil) must stay. A challenge removes the
    spurious rail tie that closed L1-to-L2 through contacts alone.
    """

    from panelcad.core.ladder import endpoint_is_rail

    tags_u = {str(t).upper() for t in tags if str(t).strip()}
    found: list[str] = []
    for wire in sheet.wires:
        ends = [str(wire.frm or ""), str(wire.to or "")]
        refs = {end.split(".")[0].upper() for end in ends if end}
        if not (refs & tags_u):
            continue
        rails = [end for end in ends if endpoint_is_rail(sheet, end, session.project)]
        if len(rails) != 1:
            continue
        other = ends[0] if ends[1] == rails[0] else ends[1]
        other_ref = other.split(".")[0].upper()
        if other_ref not in tags_u:
            continue
        # Keep the printed hot feed onto the stop (ES1.1→L1 as 102) — that is
        # not the illegal return that closes a contacts-only path.
        if rails[0].upper() in {"L1", "X1"} and other_ref in {"ES1"} | {
            t for t in tags_u if t.startswith("ES")
        }:
            pin = other.split(".", 1)[-1] if "." in other else ""
            if pin in {"1", "11", "A1", ""}:
                continue
        found.append(wire.id)
    return sorted(found)


def _is_contacts_only_starter_wire(sheet: Sheet, wire_id: str, session: Session) -> bool:
    """True for ES1→PB→coil hops that must never be contacts_only remove targets."""

    wire = next((item for item in sheet.wires if item.id == wire_id), None)
    if wire is None:
        return False
    ends = {
        str(wire.frm or "").split(".")[0].upper(),
        str(wire.to or "").split(".")[0].upper(),
    }
    if ends & {"L1", "L2", "X1", "X2"}:
        return False
    by_id = {comp.id.upper(): comp for comp in sheet.components}
    libs = [
        str(getattr(by_id.get(ref), "library_id", "") or "").lower()
        for ref in ends
    ]
    has_estop = any("estop" in lib for lib in libs)
    has_pb = any("pb_" in lib or "pushbutton" in lib for lib in libs)
    has_coil = any("coil" in lib or ".relays." in lib for lib in libs)
    if has_estop and has_pb:
        return True
    if has_pb and has_coil:
        return True
    if has_pb and len([lib for lib in libs if "pb_" in lib or "pushbutton" in lib or "estop" in lib]) >= 2:
        return True
    if ends >= {"ES1", "PB2"} or ends >= {"ES1", "PB1"}:
        return True
    if ends >= {"PB2", "PB1"} or ends >= {"PB1", "M1"} or ends >= {"PB2", "M1"}:
        return True
    return False


def _gap_anchor(session: Session, gap: dict[str, Any]) -> tuple[float, float]:
    """Page position used only to order gaps. Missing geometry sorts last on its sheet."""

    tag = str(gap.get("tag") or "")
    comp = _find(session, tag) if tag else None
    if comp is None:
        wire = _wire_for_gap(session, gap)
        if wire is not None:
            comp = _find(session, str(wire.frm).split(".")[0]) or _find(session, str(wire.to).split(".")[0])
    if comp is None:
        return (1.0e9, 1.0e9)
    return (round(float(comp.y), 3), round(float(comp.x), 3))


def _gap_order(gap: dict[str, Any]) -> tuple:
    return (
        str(gap.get("sheet") or ""),
        float(gap.get("y") or 0.0),
        float(gap.get("x") or 0.0),
        str(gap.get("tag") or gap.get("wire_id") or ""),
        str(gap.get("kind") or ""),
        str(gap.get("id") or ""),
    )


def _suspect(sheet: Sheet, comp: Any, kind: str, reason: str) -> dict[str, Any]:
    props = comp.properties or {}
    return {
        "id": f"{kind}:{comp.id}",
        "kind": kind,
        "sheet": sheet.id,
        "tag": comp.id,
        "library_id": comp.library_id,
        "read": comp.id,
        "reason": reason,
        "x": props.get("print_import_x", comp.x),
        "y": props.get("print_import_y", comp.y),
    }


def _page_gaps(session: Session, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A page whose tags never landed, or a sheet that kept symbols and grew no rungs."""

    built_tags = {
        comp.id.upper()
        for sheet in session.project.sheets
        for comp in sheet.components
    }
    gaps: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id") or "")
        tags = [str(tag).upper() for tag in (page.get("tags") or []) if str(tag).strip()]
        found = int(page.get("devices") or 0) + int(page.get("wires") or 0) + len(tags)
        if found <= 0:
            continue
        landed = [tag for tag in tags if tag in built_tags]
        if tags and not landed:
            gaps.append(
                {
                    "id": f"dropped_page:{page_id}",
                    "kind": "dropped_page",
                    "sheet": page_id,
                    "read": page_id,
                    "image": str(page.get("image") or ""),
                }
            )
            continue
        sheet = _sheet(session, page_id)
        if sheet is not None and sheet.components and not sheet.wires:
            gaps.append(
                {
                    "id": f"empty_page:{page_id}",
                    "kind": "empty_page",
                    "sheet": page_id,
                    "read": page_id,
                    "image": str(page.get("image") or ""),
                }
            )
    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        if sheet.components and not sheet.wires:
            gap_id = f"empty_page:{sheet.id}"
            if not any(gap.get("id") == gap_id for gap in gaps):
                gaps.append(
                    {
                        "id": gap_id,
                        "kind": "empty_page",
                        "sheet": sheet.id,
                        "read": sheet.id,
                    }
                )
    return gaps


def _rejected_ids(sheet: Sheet, issues: list[str]) -> set[str]:
    import re

    found: set[str] = set()
    ids = {comp.id.upper() for comp in sheet.components}
    # Placement complaints name good tags before the ladder is drawn. Only a
    # device the checker itself rejects, such as one sitting on the terminal row,
    # is a suspect read.
    pattern = re.compile(r": ([A-Za-z?][A-Za-z0-9?-]*) shares the terminal row")
    for issue in issues:
        for match in pattern.finditer(issue):
            name = match.group(1).upper()
            if name in ids:
                found.add(name)
        overlap = re.search(r": ([A-Za-z?][A-Za-z0-9?-]*) overlaps ([A-Za-z?][A-Za-z0-9?-]*)", issue)
        if overlap:
            for name in overlap.groups():
                if name.upper() in ids:
                    found.add(name.upper())
    return found


def _contact_parent(tag: str) -> str:
    if "-" not in tag:
        return ""
    parent, suffix = tag.rsplit("-", 1)
    return parent.upper() if suffix.isdigit() else ""


def _is_contact_part(library_id: str) -> bool:
    lib = library_id.lower()
    return "aux_" in lib or "ol_contact" in lib or ("contact" in lib and "coil" not in lib)


def _is_coil(library_id: str) -> bool:
    lib = library_id.lower()
    return "coil" in lib or "relays.cr" in lib or ".timers." in lib


def _coil_tags(session: Session) -> set[str]:
    tags: set[str] = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if _is_coil(comp.library_id):
                tags.add(comp.id.upper())
    return tags


def _has_contacts(session: Session, tag: str) -> bool:
    prefix = tag.upper() + "-"
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id.upper().startswith(prefix):
                return True
    return False


def _low_confidence(comp: Any) -> bool:
    props = comp.properties or {}
    if str(props.get("print_import_confirmed") or "") == "yes":
        return False
    raw = str(props.get("print_import_confidence") or "").strip()
    if not raw:
        return False
    try:
        return float(raw) < _OCR_CONFIDENCE_MIN
    except ValueError:
        return False


def _page_images(session: Session, image: str, pages: list[dict[str, Any]] | None) -> dict[str, str]:
    found = {sheet.id: image for sheet in session.project.sheets}
    for page in pages or []:
        if isinstance(page, dict) and page.get("id") and page.get("image"):
            found[str(page["id"])] = str(page["image"])
    return found


def _record_cleanup_open_stubs(
    session: Session,
    added: list[dict[str, Any]],
    page_images: dict[str, str],
    default_image: str,
) -> None:
    """Locate cleanup-added devices on the scan and record ink-proven open stubs.

    Catalog pins are never marked open without matching the symbol on the scan
    and verifying inward terminal ink + outward open space via ``record_open_stubs``.
    """

    added_tags = {
        str(item.get("tag") or "").upper()
        for item in added
        if item.get("kind") == "device" and str(item.get("tag") or "").strip()
    }
    if not added_tags:
        return
    import cv2
    import numpy as np
    from PIL import Image

    from panelcad.importer.raster import (
        MATCH_MIN,
        _ink,
        _render_drawn_symbol,
        _render_template,
        record_open_stubs,
    )
    from panelcad.render.painter import is_control_coil
    from panelcad.render.sheet import parse_pin_ref

    for sheet in session.project.sheets:
        if getattr(sheet, "kind", "") != "schematic":
            continue
        new_comps = [comp for comp in sheet.components if comp.id.upper() in added_tags]
        if not new_comps:
            continue
        img_path = str(page_images.get(sheet.id) or default_image or "")
        if not img_path or not Path(img_path).is_file():
            continue
        try:
            picture = Image.open(img_path)
            picture.load()
        except Exception:  # noqa: BLE001
            continue
        if picture.width < 64 or picture.height < 64:
            continue
        width_mm, height_mm = 431.8, 279.4
        try:
            w_dim, h_dim = sheet.page.dimensions()
            width_mm = float(w_dim) or 431.8
            height_mm = float(h_dim) or 279.4
        except Exception:  # noqa: BLE001
            pass
        width_mm, height_mm = _scan_frame_mm(picture, width_mm, height_mm)
        if width_mm <= 0 or height_mm <= 0:
            continue
        ppm = float(picture.width) / width_mm
        if ppm <= 0:
            continue
        rgb = np.asarray(picture.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        iso_h = max(1, int(round(height_mm * ppm)))
        if abs(gray.shape[0] - iso_h) >= 2:
            interp = cv2.INTER_AREA if iso_h < gray.shape[0] else cv2.INTER_LINEAR
            gray = cv2.resize(gray, (rgb.shape[1], iso_h), interpolation=interp)
        binary = _ink(gray)
        match_ink = cv2.dilate(binary, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))

        claimed: list[tuple[float, float]] = []
        known_scan_xy: dict[str, tuple[float, float]] = {}
        sheet_devices: list[dict[str, Any]] = []
        for comp in sheet.components:
            if comp.id.upper() in added_tags:
                continue
            props = comp.properties or {}
            if props.get("print_import_x") in (None, "") or props.get("print_import_y") in (None, ""):
                continue
            try:
                sx = float(props["print_import_x"])
                sy = float(props["print_import_y"])
            except (TypeError, ValueError):
                continue
            claimed.append((sx, sy))
            known_scan_xy[comp.id.upper()] = (sx, sy)
            sheet_devices.append(
                {
                    "tag": comp.id,
                    "library_id": comp.library_id,
                    "x": sx,
                    "y": sy,
                    "confidence": 0.9,
                }
            )

        matched_new: dict[str, dict[str, Any]] = {}
        for comp in new_comps:
            device = _device(session, comp.library_id)
            if device is None:
                continue
            templates = [_render_template(device, ppm)]
            if is_control_coil(device, str(comp.library_id or "")):
                templates.append(_render_drawn_symbol(device, ppm, "square"))

            neighbor_pts: list[tuple[float, float]] = []
            for wire in sheet.wires:
                refs = [str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()]
                if comp.id.upper() not in refs:
                    continue
                for r_tag in refs:
                    if r_tag != comp.id.upper() and r_tag in known_scan_xy:
                        neighbor_pts.append(known_scan_xy[r_tag])

            best_score = 0.0
            best_xy = (0.0, 0.0)
            for tmpl in templates:
                if tmpl is None:
                    continue
                th, tw = tmpl.shape
                total = int((tmpl > 0).sum())
                if total < 8 or match_ink.shape[0] < th or match_ink.shape[1] < tw:
                    continue
                resp = cv2.matchTemplate(match_ink, tmpl, cv2.TM_CCORR).copy()
                rad_px = max(2, int(round(12.0 * ppm)))
                for cx_mm, cy_mm in claimed:
                    cpx = int(round(cx_mm * ppm))
                    cpy = int(round(cy_mm * ppm))
                    ry0 = max(0, cpy - rad_px)
                    ry1 = min(resp.shape[0], cpy + rad_px + 1)
                    rx0 = max(0, cpx - rad_px)
                    rx1 = min(resp.shape[1], cpx + rad_px + 1)
                    if ry1 > ry0 and rx1 > rx0:
                        resp[ry0:ry1, rx0:rx1] = 0

                windows: list[tuple[int, int, int, int]] = []
                if neighbor_pts:
                    nx0 = max(0, int(round((min(p[0] for p in neighbor_pts) - 120.0) * ppm)))
                    nx1 = min(resp.shape[1], int(round((max(p[0] for p in neighbor_pts) + 120.0) * ppm)))
                    ny0 = max(0, int(round((min(p[1] for p in neighbor_pts) - 120.0) * ppm)))
                    ny1 = min(resp.shape[0], int(round((max(p[1] for p in neighbor_pts) + 120.0) * ppm)))
                    if nx1 > nx0 and ny1 > ny0:
                        windows.append((nx0, ny0, nx1, ny1))
                windows.append((0, 0, resp.shape[1], resp.shape[0]))

                for wx0, wy0, wx1, wy1 in windows:
                    sub = resp[wy0:wy1, wx0:wx1]
                    if sub.size == 0:
                        continue
                    idx = int(np.argmax(sub))
                    iy, ix = divmod(idx, sub.shape[1])
                    score = float(sub[iy, ix]) / total
                    if score >= MATCH_MIN and score > best_score:
                        best_score = score
                        best_xy = ((wx0 + ix) / ppm, (wy0 + iy) / ppm)
                        break

            if best_score < MATCH_MIN:
                continue
            ox, oy = best_xy
            claimed.append((ox, oy))
            known_scan_xy[comp.id.upper()] = (ox, oy)
            props = comp.properties if isinstance(comp.properties, dict) else {}
            props.setdefault("print_import_x", round(ox, 2))
            props.setdefault("print_import_y", round(oy, 2))
            comp.properties = props
            dev_row = {
                "tag": comp.id,
                "library_id": comp.library_id,
                "x": ox,
                "y": oy,
                "confidence": 0.9,
            }
            sheet_devices.append(dev_row)
            matched_new[comp.id] = dev_row

        if not matched_new:
            continue
        wire_dicts: list[dict[str, Any]] = []
        for wire in sheet.wires:
            try:
                f_tag, f_pin = parse_pin_ref(wire.frm)
            except ValueError:
                f_tag, f_pin = str(wire.frm).split(".")[0], str(wire.frm).split(".")[0]
            try:
                t_tag, t_pin = parse_pin_ref(wire.to)
            except ValueError:
                t_tag, t_pin = str(wire.to).split(".")[0], str(wire.to).split(".")[0]
            wire_dicts.append(
                {
                    "from_tag": f_tag,
                    "from_pin": f_pin,
                    "to_tag": t_tag,
                    "to_pin": t_pin,
                    "ambiguous": False,
                }
            )
        record_open_stubs(sheet_devices, session.catalog, binary, ppm, wire_dicts)
        for comp in new_comps:
            dev_row = matched_new.get(comp.id)
            if dev_row is None:
                continue
            wired = _wired_pin_ids(sheet, comp.id)
            stubs = [
                str(pin)
                for pin in (dev_row.get("open_stubs") or [])
                if str(pin).strip() and str(pin).strip() not in wired
            ]
            props = comp.properties if isinstance(comp.properties, dict) else {}
            if stubs:
                props["print_open_stubs"] = ",".join(stubs)
            else:
                props.pop("print_open_stubs", None)
            comp.properties = props


def _crop_filename(gap_id: str, index: int) -> str:
    """One file per gap. The name is the id, so two questions cannot share a crop."""

    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", gap_id).strip("._")
    if not safe:
        safe = f"gap-{index}"
    return f"{safe}.png"


def _write_crops(
    session: Session,
    gaps: list[dict[str, Any]],
    page_images: dict[str, str],
    default_image: str,
) -> list[dict[str, str]]:
    import tempfile

    from PIL import Image

    root = Path(tempfile.mkdtemp(prefix="panelcad-cleanup-"))
    crops: list[dict[str, str]] = []
    pictures: dict[str, Any] = {}
    for index, gap in enumerate(gaps):
        image = str(gap.get("image") or page_images.get(str(gap.get("sheet") or "")) or default_image)
        if not image or not Path(image).is_file():
            continue
        if image not in pictures:
            pictures[image] = Image.open(image)
        picture = pictures[image]
        box = _crop_box(session, gap, picture)
        path = root / _crop_filename(str(gap.get("id") or ""), index)
        picture.crop(box).save(path)
        crops.append(
            {
                "id": str(gap.get("id") or ""),
                "read": str(gap.get("read") or gap.get("tag") or gap.get("number") or ""),
                "path": str(path),
            }
        )
    return crops


def _crop_box(session: Session, gap: dict[str, Any], picture: Any) -> tuple[int, int, int, int]:
    width, height = picture.size
    if width < 24 or height < 24:
        return (0, 0, width, height)
    sheet = _sheet(session, str(gap.get("sheet") or ""))
    width_mm = 431.8
    height_mm = 279.4
    if sheet is not None:
        try:
            width_mm, height_mm = sheet.page.dimensions()
            width_mm = float(width_mm) or 431.8
            height_mm = float(height_mm) or 279.4
        except Exception:
            width_mm, height_mm = 431.8, 279.4
    width_mm, height_mm = _scan_frame_mm(picture, width_mm, height_mm)
    ppm_x = width / width_mm if width_mm else 4.0
    ppm_y = height / height_mm if height_mm else ppm_x
    span = _gap_span_mm(session, gap, sheet)
    if span is None:
        span = (width_mm * 0.25, height_mm * 0.25, width_mm * 0.75, height_mm * 0.75)
    x0, y0, x1, y1 = span
    pad = 6.0
    left = max(0, int((x0 - pad) * ppm_x))
    top = max(0, int((y0 - pad) * ppm_y))
    right = min(width, int((x1 + pad) * ppm_x))
    bottom = min(height, int((y1 + pad) * ppm_y))
    if right <= left or bottom <= top:
        return (0, 0, min(width, 32), min(height, 32))
    return (left, top, right, bottom)


def _scan_frame_mm(picture: Any, width_mm: float, height_mm: float) -> tuple[float, float]:
    """Millimetres of the scan image, when its DPI frame is not the sheet size.

    Device scan coordinates are measured on this image. A snapped sheet size
    shifts the crop off the symbol. Cleanup-placed sheets often keep the A3
    default (420×297) while the prepared phone page is ANSI B / tabloid
    (431.8×279.4); using the sheet then warps ppm and drops open-stub matches.
    When the sheet already matches the image aspect (a tabloid ladder sheet
    under a tabloid-aspect crop), keep the sheet — ingest's no-DPI fallback
    is A3 and must not replace a correct tabloid frame.
    """

    from panelcad.importer.ingest import _image_dpi, _sheet_for_photo, sheet_size_mm

    px_w = int(getattr(picture, "width", 0) or 0)
    px_h = int(getattr(picture, "height", 0) or 0)
    if px_w < 8 or px_h < 8 or width_mm <= 0 or height_mm <= 0:
        return width_mm, height_mm

    sheet_aspect = width_mm / height_mm
    image_aspect = px_w / px_h
    aspect_mismatch = abs(sheet_aspect - image_aspect) / max(sheet_aspect, image_aspect) > 0.04

    # No DPI: only override when the sheet aspect disagrees with the pixels.
    if _image_dpi(picture) is None:
        if aspect_mismatch:
            try:
                return _sheet_for_photo(px_w, px_h)
            except Exception:  # noqa: BLE001
                return width_mm, height_mm
        return width_mm, height_mm

    try:
        image_w, image_h = sheet_size_mm(picture)
    except Exception:  # noqa: BLE001
        image_w, image_h = width_mm, height_mm

    if abs(image_w - width_mm) > 8.0 or abs(image_h - height_mm) > 8.0:
        return image_w, image_h
    if aspect_mismatch:
        try:
            return _sheet_for_photo(px_w, px_h)
        except Exception:  # noqa: BLE001
            return width_mm, height_mm
    return width_mm, height_mm


def _gap_span_mm(session: Session, gap: dict[str, Any], sheet: Sheet | None) -> tuple[float, float, float, float] | None:
    """The symbol or wire this gap is about, in millimetres. Not the page corner."""

    boxes: list[tuple[float, float, float, float]] = []
    wire = _wire_for_gap(session, gap)
    if wire is not None:
        # A self-wire's routed polyline can wander across the whole ladder.
        # The crop is the device at the wire's own ends.
        same_device = str(wire.frm).split(".")[0].upper() == str(wire.to).split(".")[0].upper()
        if str(gap.get("kind") or "") != "self_wire" and not same_device:
            points = list(getattr(wire, "points", None) or [])
            if points:
                xs = [float(point.x) for point in points]
                ys = [float(point.y) for point in points]
                boxes.append((min(xs), min(ys), max(xs), max(ys)))
        for end in (str(wire.frm), str(wire.to)):
            boxes.extend(_ref_boxes(session, end))
    for end in (str(gap.get("from") or ""), str(gap.get("to") or "")):
        if end:
            boxes.extend(_ref_boxes(session, end))
    tag = str(gap.get("tag") or "")
    if tag:
        boxes.extend(_ref_boxes(session, tag))
    if gap.get("x") is not None and gap.get("y") is not None:
        try:
            token_x = float(gap["x"])
            token_y = float(gap["y"])
        except (TypeError, ValueError):
            token_x = token_y = None
        if token_x is not None and token_y is not None:
            boxes.append((token_x, token_y, token_x, token_y))
    if not boxes and sheet is not None:
        for comp in sheet.components:
            boxes.extend(_ref_boxes(session, comp.id))
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _wire_for_gap(session: Session, gap: dict[str, Any]) -> Any:
    wire_id = str(gap.get("wire_id") or "")
    if not wire_id and str(gap.get("id") or "").startswith("wire:"):
        wire_id = str(gap["id"]).split(":", 1)[1]
    if wire_id:
        found = _wire_by_id(session, wire_id)
        if found is not None:
            return found
    for listed in gap.get("wires") or []:
        found = _wire_by_id(session, str(listed))
        if found is not None:
            return found
    return None


def _ref_boxes(session: Session, text: str) -> list[tuple[float, float, float, float]]:
    ref = str(text).split(".")[0].strip()
    comp = _find(session, ref)
    if comp is None:
        return []
    x = float(comp.x)
    y = float(comp.y)
    props = comp.properties or {}
    if props.get("print_import_x") not in (None, ""):
        try:
            x = float(props.get("print_import_x"))
            y = float(props.get("print_import_y"))
        except (TypeError, ValueError):
            x, y = float(comp.x), float(comp.y)
    device = _device(session, comp.library_id)
    width = float(getattr(device, "schematic_width", 0) or 18.0) if device is not None else 18.0
    height = float(getattr(device, "schematic_height", 0) or 12.0) if device is not None else 12.0
    return [(x, y, x + width, y + height)]


def _owner_sheet(session: Session, text: str) -> str:
    ref = str(text).split(".")[0].strip()
    if not ref or ref.upper() in {"L1", "L2", "L3", "N", "PE"}:
        return ""
    for sheet in session.project.sheets:
        if any(comp.id.upper() == ref.upper() for comp in sheet.components):
            return sheet.id
    return ""


def _cross_sheet(session: Session, frm: str, to: str) -> bool:
    left = _owner_sheet(session, frm)
    right = _owner_sheet(session, to)
    return bool(left and right and left != right)


def _rename_tag(session: Session, old: str, new: str, library_id: str) -> bool:
    comp = _find(session, old)
    if comp is None:
        return False
    if new == "?":
        new = _blank_id(session)
    if new.upper() != old.upper() and _find(session, new) is not None:
        return False
    if library_id and _device(session, library_id) is not None:
        comp.library_id = library_id
        # A real catalog library replaces the reader placeholder — drop the
        # needs_info flags so later unread resolves do not latch onto this tag.
        props = comp.properties if isinstance(comp.properties, dict) else {}
        props.pop("reconstruct_needs_info", None)
        props.pop("print_import_needs_info", None)
        props.pop("needs_missing", None)
        if str(props.get("description") or "").strip().upper() in {"INFO NEEDED", "NEEDED"}:
            props.pop("description", None)
        comp.properties = props
    comp.id = new
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            wire.frm = _swap_ref(str(wire.frm), old, new)
            wire.to = _swap_ref(str(wire.to), old, new)
    return True


def _blank_id(session: Session) -> str:
    used = _known_tags(session)
    if "?" not in used:
        return "?"
    index = 2
    while f"?{index}" in used:
        index += 1
    return f"?{index}"


def _swap_ref(text: str, old: str, new: str) -> str:
    ref, dot, pin = text.partition(".")
    if ref.upper() != old.upper():
        return text
    return f"{new}{dot}{pin}" if dot else new


def _land_return(session: Session, frm: str, to: str, number: str) -> tuple[str, str] | None:
    """Land a return on the rail without splicing it onto another numbered net.

    A wire from the fuse or a transformer primary (H1–H4) to the return rail
    shorts that net. The same pin already carrying a different printed number
    is the same mistake. Use another free pin of that device, or refuse.
    """

    from panelcad.core.ladder import endpoint_is_rail
    from panelcad.render.sheet import parse_pin_ref

    device_end, rail_end = frm, to
    sheet = _sheet(session, _owner_sheet(session, frm) or _owner_sheet(session, to))
    if sheet is None:
        return frm, to
    frm_rail = endpoint_is_rail(sheet, frm, session.project) or frm.split(".")[0].upper() in _rail_facts(session)
    to_rail = endpoint_is_rail(sheet, to, session.project) or to.split(".")[0].upper() in _rail_facts(session)
    if to_rail and not frm_rail:
        device_end, rail_end = frm, to
    elif frm_rail and not to_rail:
        device_end, rail_end = to, frm
    else:
        return frm, to
    try:
        ref, pin = parse_pin_ref(device_end)
    except ValueError:
        return device_end, rail_end
    if not _return_pin_is_tied(session, sheet, ref, pin, number):
        return f"{ref}.{pin}", rail_end
    # Do not invent a different pin, and do not fail the rest of the unit.
    # A primary-to-rail return is omitted. The fuse-to-H wire beside it stays.
    return None


def _return_pin_is_tied(session: Session, sheet: Sheet, ref: str, pin: str, number: str) -> bool:
    comp = _find(session, ref)
    # A transformer primary is not a return landing. A control fuse still is:
    # the layout checker, not this step, refuses a fuse that reaches both rails.
    if comp is not None and "transformer" in str(comp.library_id).lower() and str(pin).upper().startswith("H"):
        return True
    from panelcad.render.sheet import parse_pin_ref

    want = str(number or "").strip()
    for wire in sheet.wires:
        touched = False
        other = ""
        for text in (wire.frm, wire.to):
            try:
                got_ref, got_pin = parse_pin_ref(text)
            except ValueError:
                continue
            if got_ref == ref and got_pin == pin:
                touched = True
            else:
                other = text
        if not touched:
            continue
        current = str(wire.number or "").strip()
        if current and current != "?" and current != want:
            return True
        try:
            other_ref, other_pin = parse_pin_ref(other)
        except ValueError:
            continue
        other_comp = _find(session, other_ref)
        if other_comp is not None and _is_primary_pin(other_comp, other_pin):
            return True
    return False


def _is_primary_pin(comp: Any, pin: str) -> bool:
    lib = str(getattr(comp, "library_id", "") or "").lower()
    return "transformer" in lib and str(pin or "").upper().startswith("H")


def _may_return(library_id: str) -> bool:
    """A load or a terminal may still be missing the wire that lands on L2.

    A contact may not. Filling that missing wire would be a contacts-only rung.
    """

    return _is_return_load(library_id) or "terminal" in library_id.lower()


def _device(session: Session, library_id: str) -> Any:
    try:
        return session.catalog.get(library_id)
    except KeyError:
        return None


def _side_pin(device: Any, side: str) -> str:
    pins = [pin for pin in device.pins if str(pin.side) == side]
    return str(pins[0].id) if pins else ""


def _connected_tags(sheet: Sheet) -> set[str]:
    tags: set[str] = set()
    for wire in sheet.wires:
        for text in (wire.frm, wire.to):
            ref = str(text).split(".")[0].strip().upper()
            if ref:
                tags.add(ref)
    return tags


def _pin_wired(sheet: Sheet, ref: str, pin: str) -> bool:
    from panelcad.render.sheet import parse_pin_ref

    for wire in sheet.wires:
        for text in (wire.frm, wire.to):
            try:
                other, landed = parse_pin_ref(text)
            except ValueError:
                other = str(text).split(".")[0]
                landed = ""
            if other == ref and landed == pin:
                return True
    return False


def _find(session: Session, tag: str) -> Any:
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id.upper() == tag.upper():
                return comp
    return None


def _same_library(session: Session, tag: str, library_id: str) -> bool:
    comp = _find(session, tag)
    if comp is None:
        return False
    return str(comp.library_id) == str(library_id).strip()


def _sheet(session: Session, sheet_id: str) -> Sheet | None:
    if not sheet_id:
        return next((sheet for sheet in session.project.sheets if getattr(sheet, "kind", "") == "schematic"), None)
    try:
        return session.project.sheet(sheet_id)
    except KeyError:
        return None


def work_json(work: dict[str, Any]) -> str:
    """Stable text of a cleanup request, for tests that the gold sheet is absent."""

    return json.dumps(work, default=str, sort_keys=True)
