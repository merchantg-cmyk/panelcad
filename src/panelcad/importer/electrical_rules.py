"""Centralized shop electrical rules for control transformer and rail wiring.

Single source of truth for the hard shop invariants:
- A control transformer's primary (H1/H2/H3/H4) always goes off-page (external
  supply), never to control rails L1/L2.
- Secondary X1 feeds L1 through a secondary fuse (e.g. FU3) as wire ``102``;
  X1 must never land straight on L1 without the secondary fuse.
- Secondary X2 feeds L2 directly as wire ``104``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TRANSFORMER_PRIMARY_PINS: frozenset[str] = frozenset({"H1", "H2", "H3", "H4"})
TRANSFORMER_SECONDARY_HOT_PINS: frozenset[str] = frozenset({"X1"})
TRANSFORMER_SECONDARY_RETURN_PINS: frozenset[str] = frozenset({"X2"})
CONTROL_SECONDARY_HOT_WIRE: str = "102"
CONTROL_SECONDARY_RETURN_WIRE: str = "104"
DEFAULT_CONTROL_HOT_RAIL: str = "L1"
DEFAULT_CONTROL_RETURN_RAIL: str = "L2"


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


def is_offpage_library(library_id: str | None) -> bool:
    low = str(library_id or "").strip().lower()
    return "offpage" in low or "off_page" in low or "external" in low


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

    Returns an empty list when every transformer on ``sheet`` obeys the shop
    rules (or when the sheet has no control transformer). Never mutates ``sheet``.
    """

    hot = str(hot_rail or DEFAULT_CONTROL_HOT_RAIL).strip().upper()
    ret = str(return_rail or DEFAULT_CONTROL_RETURN_RAIL).strip().upper()
    rails = {hot, ret}
    by_id = {str(comp.id): comp for comp in (getattr(sheet, "components", None) or [])}
    transformers = {
        cid for cid, comp in by_id.items() if is_transformer_library(getattr(comp, "library_id", ""))
    }
    if not transformers:
        return []

    violations: list[TransformerRuleViolation] = []
    # Track fuses directly connected to a transformer primary pin (H1..H4).
    primary_fuses: dict[str, tuple[str, str]] = {}
    rail_wires_on_fuse: dict[str, list[tuple[str, str]]] = {}
    for wire in getattr(sheet, "wires", None) or []:
        wid = str(getattr(wire, "id", "") or "")
        a_ref, a_pin = _split_end(getattr(wire, "frm", ""))
        b_ref, b_pin = _split_end(getattr(wire, "to", ""))
        for t_ref, t_pin, other_ref, other_pin in (
            (a_ref, a_pin, b_ref, b_pin),
            (b_ref, b_pin, a_ref, a_pin),
        ):
            if t_ref in by_id and is_fuse_library(getattr(by_id[t_ref], "library_id", "")):
                other_upper = other_ref.upper()
                if not other_pin and other_upper in rails:
                    rail_wires_on_fuse.setdefault(t_ref, []).append((wid, other_ref))
            if t_ref not in transformers:
                continue
            other_upper = other_ref.upper()
            is_rail = not other_pin and other_upper in rails
            if is_transformer_primary_pin(t_pin) and other_ref in by_id and is_fuse_library(
                getattr(by_id[other_ref], "library_id", "")
            ):
                primary_fuses.setdefault(other_ref, (t_ref, t_pin))
            if is_transformer_primary_pin(t_pin) and is_rail:
                violations.append(
                    TransformerRuleViolation(
                        code="transformer_primary_on_control_rail",
                        device=t_ref,
                        wire_id=wid,
                        message=(
                            f"{t_ref}.{t_pin} (primary) must go off-page, not to control rail {other_ref}."
                        ),
                    )
                )
            if is_transformer_secondary_hot_pin(t_pin) and is_rail and other_upper == hot:
                violations.append(
                    TransformerRuleViolation(
                        code="transformer_x1_direct_to_l1",
                        device=t_ref,
                        wire_id=wid,
                        message=(
                            f"{t_ref}.{t_pin} must go through a secondary fuse to {hot_rail} "
                            f"(as {CONTROL_SECONDARY_HOT_WIRE}), never straight to {hot_rail}."
                        ),
                    )
                )
            if is_transformer_secondary_return_pin(t_pin) and is_rail and other_upper == hot:
                violations.append(
                    TransformerRuleViolation(
                        code="transformer_x2_on_hot_rail",
                        device=t_ref,
                        wire_id=wid,
                        message=(
                            f"{t_ref}.{t_pin} must land on return rail {return_rail} "
                            f"(as {CONTROL_SECONDARY_RETURN_WIRE}), not hot rail {hot_rail}."
                        ),
                    )
                )
    for fuse_ref, (t_ref, t_pin) in primary_fuses.items():
        for wid, rail_ref in rail_wires_on_fuse.get(fuse_ref, ()):
            violations.append(
                TransformerRuleViolation(
                    code="transformer_primary_on_control_rail",
                    device=t_ref,
                    wire_id=wid,
                    message=(
                        f"{t_ref}.{t_pin} (primary) through {fuse_ref} must go off-page, "
                        f"not to control rail {rail_ref}."
                    ),
                )
            )
    return violations
