"""Centralized shop electrical rules for control transformer and rail wiring.

Single source of truth for the hard shop invariants:
- A control transformer's primary (H1/H2/H3/H4) always goes off-page (external
  supply), never to control rails L1/L2.
- Secondary X1 feeds L1 through a secondary fuse (e.g. FU3) as wire ``102``;
  X1 must never land straight on L1 without the secondary fuse.
- Secondary X2 feeds L2 directly as wire ``104``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

TRANSFORMER_PRIMARY_PINS: frozenset[str] = frozenset({"H1", "H2", "H3", "H4"})
TRANSFORMER_SECONDARY_HOT_PINS: frozenset[str] = frozenset({"X1"})
TRANSFORMER_SECONDARY_RETURN_PINS: frozenset[str] = frozenset({"X2"})
CONTROL_SECONDARY_HOT_WIRE: str = "102"
CONTROL_SECONDARY_RETURN_WIRE: str = "104"
DEFAULT_CONTROL_HOT_RAIL: str = "L1"
DEFAULT_CONTROL_RETURN_RAIL: str = "L2"

_MULTIPOLE_PIN_MATES: dict[str, str] = {
    "1": "2",
    "2": "1",
    "3": "4",
    "4": "3",
    "5": "6",
    "6": "5",
    "11": "12",
    "12": "11",
    "13": "14",
    "14": "13",
    "21": "22",
    "22": "21",
    "L1": "T1",
    "T1": "L1",
    "L2": "T2",
    "T2": "L2",
    "L3": "T3",
    "T3": "L3",
}

_CATALOG_LOAD_CATEGORIES: frozenset[str] = frozenset(
    {
        "power.load",
        "operator.light",
        "operator.horn",
        "switching.relay",
        "switching.alternating",
        "switching.safety",
        "timing.timer",
    }
)


def _derive_catalog_sets(catalog: Any) -> tuple[frozenset[str], frozenset[str]]:
    """Derive ``(load_library_ids, multipole_estop_library_ids)`` from a Catalog."""

    load_ids: set[str] = set()
    multipole_estop_ids: set[str] = set()
    devices = getattr(catalog, "devices", None) or {}
    for lid, dev in devices.items():
        low = str(lid or "").strip().lower()
        if not low:
            continue
        cat_low = str(getattr(dev, "category", "") or "").strip().lower()
        pin_ids = {
            str(getattr(p, "id", "")).strip().upper()
            for p in (getattr(dev, "pins", None) or [])
        }
        has_3ph_poles = ({"L1", "L2", "L3"} <= pin_ids and {"T1", "T2", "T3"} <= pin_ids) or (
            {"1", "3", "5"} <= pin_ids and {"2", "4", "6"} <= pin_ids
        )
        if (
            cat_low.startswith("load.")
            or cat_low in _CATALOG_LOAD_CATEGORIES
            or (cat_low == "switching.contactor" and {"A1", "A2"} <= pin_ids and not has_3ph_poles)
        ):
            load_ids.add(low)
        if cat_low == "operator.estop" and {"11", "12", "21", "22"} <= pin_ids:
            multipole_estop_ids.add(low)
    return frozenset(load_ids), frozenset(multipole_estop_ids)


@lru_cache(maxsize=1)
def _default_catalog_sets() -> tuple[frozenset[str], frozenset[str]]:
    from panelcad.library.catalog import default_catalog

    return _derive_catalog_sets(default_catalog())


@dataclass(frozen=True)
class TransformerRuleViolation:
    """One hard shop-rule violation on a control transformer's connections."""

    code: str
    device: str
    wire_id: str
    message: str


def is_transformer_library(library_id: str | None) -> bool:
    low = str(library_id or "").strip().lower()
    return "transformer" in low or ".cpt" in low


def is_fuse_library(library_id: str | None) -> bool:
    low = str(library_id or "").strip().lower()
    return "fuse" in low or "protection.fu" in low


def is_breaker_library(library_id: str | None) -> bool:
    low = str(library_id or "").strip().lower()
    return any(tok in low for tok in ("breaker", ".mcb", ".mccb", ".mpcb"))


def is_ocpd_library(library_id: str | None) -> bool:
    """True for overcurrent protective devices (fuses or circuit breakers)."""

    return is_fuse_library(library_id) or is_breaker_library(library_id)


def is_offpage_library(library_id: str | None) -> bool:
    low = str(library_id or "").strip().lower()
    return "offpage" in low or "off_page" in low or "external" in low


def _is_load_library(library_id: str | None) -> bool:
    low = str(library_id or "").strip().lower()
    if not low or "aux_" in low or "ol_contact" in low or "contact_" in low:
        return False
    catalog_loads, _ = _default_catalog_sets()
    if low in catalog_loads:
        return True
    return any(
        tok in low
        for tok in (
            "coil",
            "pilot",
            "lamp",
            "horn",
            "solenoid",
            "motor",
            "relays.",
            "timers.",
            "heater",
            "loads.",
        )
    )


def _is_passthrough_library(library_id: str | None) -> bool:
    """Devices that pass a conductor through (fuses, breakers, disconnects, terminals, contacts)."""

    low = str(library_id or "").strip().lower()
    if not low or is_transformer_library(low) or is_offpage_library(low) or _is_load_library(low):
        return False
    return (
        is_ocpd_library(low)
        or "disconnect" in low
        or "terminal" in low
        or "connection.node" in low
        or "contact" in low
        or "operators.pb_" in low
        or "operators.estop" in low
        or "operators.selector" in low
        or "operators.limit" in low
        or "switch" in low
    )


def _is_multipole_estop_library(library_id: str | None, comp_pins: set[str]) -> bool:
    low = str(library_id or "").strip().lower()
    if not low:
        return False
    _, catalog_estops = _default_catalog_sets()
    if low in catalog_estops or "estop_2nc" in low:
        return True
    return "estop" in low and bool(comp_pins & {"11", "12"}) and bool(comp_pins & {"21", "22"})


def _internal_peer_pins(library_id: str | None, pin: str, comp_pins: set[str]) -> list[str]:
    """Pins on the same pass-through device reachable internally from ``pin``."""

    if not _is_passthrough_library(library_id):
        return []
    low = str(library_id or "").strip().lower()
    pin_up = pin.upper()
    if "_3p" in low or "ac3_" in low or _is_multipole_estop_library(low, comp_pins):
        mate = _MULTIPOLE_PIN_MATES.get(pin_up)
        return [mate] if mate and mate in comp_pins else []
    return sorted(other for other in comp_pins if other != pin_up)


def is_transformer_primary_pin(pin_id: str | None) -> bool:
    return str(pin_id or "").strip().upper() in TRANSFORMER_PRIMARY_PINS


def is_transformer_secondary_hot_pin(pin_id: str | None) -> bool:
    return str(pin_id or "").strip().upper() in TRANSFORMER_SECONDARY_HOT_PINS


def is_transformer_secondary_return_pin(pin_id: str | None) -> bool:
    return str(pin_id or "").strip().upper() in TRANSFORMER_SECONDARY_RETURN_PINS


def _split_end(end: str | None) -> tuple[str, str]:
    text = str(end or "").strip()
    if "." in text:
        ref, pin = text.split(".", 1)
        return ref.strip(), pin.strip()
    return text, ""


def validate_transformer_control_rules(
    sheet: Any,
    *,
    hot_rail: str = DEFAULT_CONTROL_HOT_RAIL,
    return_rail: str = DEFAULT_CONTROL_RETURN_RAIL,
) -> list[TransformerRuleViolation]:
    """Check control transformer primary/secondary wiring rules on ``sheet``.

    Rules enforced:
    1. A transformer primary pin (H1..H4) must never land on control rail
       ``hot_rail`` or ``return_rail`` — directly or through any chain of fuses,
       breakers, disconnects, terminals, or other pass-through devices.
    2. Secondary hot pin ``X1`` must never land on ``return_rail``, must never
       reach ``hot_rail`` without passing through a secondary fuse or breaker,
       and when wired must reach ``hot_rail`` through a secondary fuse or
       breaker (not straight to a load or terminal).
    3. Secondary return pin ``X2`` must never land on ``hot_rail``.

    Returns an empty list when every transformer on ``sheet`` obeys the shop
    rules (or when the sheet has no control transformer). Never mutates ``sheet``.
    """

    hot = str(hot_rail or DEFAULT_CONTROL_HOT_RAIL).strip().upper()
    ret = str(return_rail or DEFAULT_CONTROL_RETURN_RAIL).strip().upper()
    rails = {hot, ret}
    by_id = {str(comp.id): comp for comp in (getattr(sheet, "components", None) or [])}
    transformers = sorted(
        cid for cid, comp in by_id.items() if is_transformer_library(getattr(comp, "library_id", ""))
    )
    if not transformers:
        return []

    Node = tuple[str, str]
    wire_adj: dict[Node, list[tuple[Node, str]]] = {}
    comp_pins: dict[str, set[str]] = {}
    rail_display: dict[str, str] = {hot: str(hot_rail or DEFAULT_CONTROL_HOT_RAIL), ret: str(return_rail or DEFAULT_CONTROL_RETURN_RAIL)}

    def _to_node(ref: str, pin: str) -> Node | None:
        if not ref:
            return None
        if not pin and ref.upper() in rails:
            rail_up = ref.upper()
            rail_display.setdefault(rail_up, ref)
            return ("RAIL", rail_up)
        if not pin:
            return None
        pin_up = pin.upper()
        comp_pins.setdefault(ref, set()).add(pin_up)
        return (ref, pin_up)

    for wire in getattr(sheet, "wires", None) or []:
        wid = str(getattr(wire, "id", "") or "")
        a_ref, a_pin = _split_end(getattr(wire, "frm", ""))
        b_ref, b_pin = _split_end(getattr(wire, "to", ""))
        u = _to_node(a_ref, a_pin)
        v = _to_node(b_ref, b_pin)
        if u is None or v is None or u == v:
            continue
        wire_adj.setdefault(u, []).append((v, wid))
        wire_adj.setdefault(v, []).append((u, wid))

    violations: list[TransformerRuleViolation] = []
    seen_violations: set[tuple[str, str, str, str]] = set()

    def _emit(code: str, device: str, wire_id: str, message: str, dedupe_key: str) -> None:
        key = (code, device, dedupe_key, wire_id)
        if key in seen_violations:
            return
        seen_violations.add(key)
        violations.append(
            TransformerRuleViolation(
                code=code,
                device=device,
                wire_id=wire_id,
                message=message,
            )
        )

    for t_ref in transformers:
        t_pins = sorted(comp_pins.get(t_ref, set()))

        # 1. Primary pins (H1..H4): must never reach L1/L2 directly or via any
        # pass-through chain (fuses, breakers, disconnects, terminals, etc.).
        for t_pin in t_pins:
            if not is_transformer_primary_pin(t_pin):
                continue
            start: Node = (t_ref, t_pin)
            queue: deque[tuple[Node, tuple[str, ...], str]] = deque([(start, (), "")])
            visited: set[Node] = {start}
            reported_rails: set[str] = set()
            while queue:
                cur, via, last_wid = queue.popleft()
                if cur[0] == "RAIL":
                    rail_up = cur[1]
                    if rail_up not in reported_rails:
                        reported_rails.add(rail_up)
                        rail_name = rail_display.get(rail_up, rail_up)
                        if via:
                            chain = " -> ".join(via)
                            msg = (
                                f"{t_ref}.{t_pin} (primary) through {chain} must go off-page, "
                                f"not to control rail {rail_name}."
                            )
                        else:
                            msg = (
                                f"{t_ref}.{t_pin} (primary) must go off-page, "
                                f"not to control rail {rail_name}."
                            )
                        _emit(
                            "transformer_primary_on_control_rail",
                            t_ref,
                            last_wid,
                            msg,
                            f"{t_pin}:{rail_up}",
                        )
                    continue
                # Traverse wires attached to cur.
                for nxt, wid in wire_adj.get(cur, ()):
                    if nxt in visited:
                        continue
                    visited.add(nxt)
                    queue.append((nxt, via, wid))
                # Traverse internally through pass-through devices (never through
                # any transformer, off-page arrow, or control load).
                c_ref, c_pin = cur
                if c_ref != t_ref and c_ref in by_id:
                    c_lib = getattr(by_id[c_ref], "library_id", "")
                    if is_offpage_library(c_lib) or is_transformer_library(c_lib):
                        continue
                    for peer_pin in _internal_peer_pins(c_lib, c_pin, comp_pins.get(c_ref, set())):
                        nxt = (c_ref, peer_pin)
                        if nxt in visited:
                            continue
                        visited.add(nxt)
                        next_via = via if c_ref in via else via + (c_ref,)
                        queue.append((nxt, next_via, last_wid))

        # 2. Secondary hot pin (X1): must never land on L2, must never reach L1
        # without a secondary fuse or breaker, and when wired must reach L1
        # through a secondary fuse or breaker.
        for t_pin in t_pins:
            if not is_transformer_secondary_hot_pin(t_pin):
                continue
            start = (t_ref, t_pin)
            if not wire_adj.get(start):
                continue
            # State: (node, passed_ocpd) -> (via_devs, first_wid, last_wid)
            q2: deque[tuple[Node, bool, tuple[str, ...], str, str]] = deque([(start, False, (), "", "")])
            seen2: set[tuple[Node, bool]] = {(start, False)}
            hot_with_ocpd = False
            hot_without_ocpd: tuple[tuple[str, ...], str] | None = None
            ret_hit: tuple[tuple[str, ...], str] | None = None
            unprotected_load_hits: list[tuple[str, str]] = []
            reached_any_ocpd = False
            first_wire_id = wire_adj[start][0][1]

            while q2:
                cur, has_ocpd, via, first_wid, last_wid = q2.popleft()
                if cur[0] == "RAIL":
                    rail_up = cur[1]
                    if rail_up == ret and ret_hit is None:
                        ret_hit = (via, last_wid)
                    elif rail_up == hot:
                        if has_ocpd:
                            hot_with_ocpd = True
                        elif hot_without_ocpd is None:
                            hot_without_ocpd = (via, last_wid)
                    continue
                for nxt, wid in wire_adj.get(cur, ()):
                    nxt_first = first_wid or wid
                    if nxt[0] != "RAIL" and nxt[0] != t_ref and nxt[0] in by_id:
                        nxt_lib = getattr(by_id[nxt[0]], "library_id", "")
                        if is_ocpd_library(nxt_lib):
                            reached_any_ocpd = True
                        if not has_ocpd and (
                            _is_load_library(nxt_lib)
                            or ("ac3_" in str(nxt_lib or "").lower() and nxt[1] in {"A1", "A2"})
                        ):
                            unprotected_load_hits.append((nxt[0], wid))
                    state = (nxt, has_ocpd)
                    if state not in seen2:
                        seen2.add(state)
                        q2.append((nxt, has_ocpd, via, nxt_first, wid))
                c_ref, c_pin = cur
                if c_ref != t_ref and c_ref in by_id:
                    c_lib = getattr(by_id[c_ref], "library_id", "")
                    if is_offpage_library(c_lib) or is_transformer_library(c_lib) or _is_load_library(c_lib):
                        continue
                    next_has_ocpd = has_ocpd or is_ocpd_library(c_lib)
                    if next_has_ocpd:
                        reached_any_ocpd = True
                    for peer_pin in _internal_peer_pins(c_lib, c_pin, comp_pins.get(c_ref, set())):
                        nxt = (c_ref, peer_pin)
                        state = (nxt, next_has_ocpd)
                        if state not in seen2:
                            seen2.add(state)
                            next_via = via if c_ref in via else via + (c_ref,)
                            q2.append((nxt, next_has_ocpd, next_via, first_wid, last_wid))

            if ret_hit is not None:
                via, wid = ret_hit
                ret_name = rail_display.get(ret, return_rail)
                hot_name = rail_display.get(hot, hot_rail)
                if via:
                    msg = (
                        f"{t_ref}.{t_pin} through {' -> '.join(via)} must feed hot rail {hot_name} "
                        f"through a secondary fuse or breaker (as {CONTROL_SECONDARY_HOT_WIRE}), "
                        f"not return rail {ret_name}."
                    )
                else:
                    msg = (
                        f"{t_ref}.{t_pin} must feed hot rail {hot_name} through a secondary fuse "
                        f"or breaker (as {CONTROL_SECONDARY_HOT_WIRE}), not return rail {ret_name}."
                    )
                _emit("transformer_x1_on_return_rail", t_ref, wid, msg, f"{t_pin}:ret")

            if hot_without_ocpd is not None:
                via, wid = hot_without_ocpd
                hot_name = rail_display.get(hot, hot_rail)
                if not via:
                    _emit(
                        "transformer_x1_direct_to_l1",
                        t_ref,
                        wid,
                        (
                            f"{t_ref}.{t_pin} must go through a secondary fuse or breaker to {hot_name} "
                            f"(as {CONTROL_SECONDARY_HOT_WIRE}), never straight to {hot_name}."
                        ),
                        f"{t_pin}:direct_hot",
                    )
                else:
                    _emit(
                        "transformer_x1_unprotected",
                        t_ref,
                        wid,
                        (
                            f"{t_ref}.{t_pin} reaches {hot_name} through {' -> '.join(via)} "
                            f"without a secondary fuse or breaker."
                        ),
                        f"{t_pin}:unprotected_hot",
                    )

            for load_ref, wid in unprotected_load_hits:
                hot_name = rail_display.get(hot, hot_rail)
                _emit(
                    "transformer_x1_unprotected",
                    t_ref,
                    wid,
                    (
                        f"{t_ref}.{t_pin} feeds {load_ref} without a secondary fuse or breaker "
                        f"to {hot_name}."
                    ),
                    f"{t_pin}:load:{load_ref}",
                )

            if (
                not hot_with_ocpd
                and hot_without_ocpd is None
                and ret_hit is None
                and not unprotected_load_hits
            ):
                hot_name = rail_display.get(hot, hot_rail)
                if not reached_any_ocpd:
                    _emit(
                        "transformer_x1_unprotected",
                        t_ref,
                        first_wire_id,
                        (
                            f"{t_ref}.{t_pin} must reach {hot_name} through a secondary fuse or breaker "
                            f"(as {CONTROL_SECONDARY_HOT_WIRE})."
                        ),
                        f"{t_pin}:no_ocpd",
                    )
                else:
                    _emit(
                        "transformer_x1_unprotected",
                        t_ref,
                        first_wire_id,
                        (
                            f"{t_ref}.{t_pin} secondary fuse or breaker does not reach hot rail "
                            f"{hot_name} (as {CONTROL_SECONDARY_HOT_WIRE})."
                        ),
                        f"{t_pin}:no_hot_rail",
                    )

        # 3. Secondary return pin (X2): must never land on hot rail L1.
        for t_pin in t_pins:
            if not is_transformer_secondary_return_pin(t_pin):
                continue
            start = (t_ref, t_pin)
            q3: deque[tuple[Node, tuple[str, ...], str]] = deque([(start, (), "")])
            seen3: set[Node] = {start}
            while q3:
                cur, via, last_wid = q3.popleft()
                if cur[0] == "RAIL":
                    if cur[1] == hot:
                        hot_name = rail_display.get(hot, hot_rail)
                        ret_name = rail_display.get(ret, return_rail)
                        if via:
                            msg = (
                                f"{t_ref}.{t_pin} through {' -> '.join(via)} must land on return rail "
                                f"{ret_name} (as {CONTROL_SECONDARY_RETURN_WIRE}), not hot rail {hot_name}."
                            )
                        else:
                            msg = (
                                f"{t_ref}.{t_pin} must land on return rail {ret_name} "
                                f"(as {CONTROL_SECONDARY_RETURN_WIRE}), not hot rail {hot_name}."
                            )
                        _emit("transformer_x2_on_hot_rail", t_ref, last_wid, msg, f"{t_pin}:hot")
                    continue
                for nxt, wid in wire_adj.get(cur, ()):
                    if nxt in seen3:
                        continue
                    seen3.add(nxt)
                    q3.append((nxt, via, wid))
                c_ref, c_pin = cur
                if c_ref != t_ref and c_ref in by_id:
                    c_lib = getattr(by_id[c_ref], "library_id", "")
                    if is_offpage_library(c_lib) or is_transformer_library(c_lib) or _is_load_library(c_lib):
                        continue
                    for peer_pin in _internal_peer_pins(c_lib, c_pin, comp_pins.get(c_ref, set())):
                        nxt = (c_ref, peer_pin)
                        if nxt in seen3:
                            continue
                        seen3.add(nxt)
                        next_via = via if c_ref in via else via + (c_ref,)
                        q3.append((nxt, next_via, last_wid))

    return violations
