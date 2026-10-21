"""Primary jumpers on a dual-winding control transformer.

A control transformer with two primary windings (H1-H2 and H3-H4) is set for its
supply by jumpers. The higher rated voltage puts the windings in series (H2-H3).
The lower one puts them in parallel (H1-H3 and H2-H4). The jumper set is only
drawn when both the rated primary pair and the applied voltage are known; anything
else is reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from panelcad.core.pins import pin_caption

SERIES = (("H2", "H3"),)
PARALLEL = (("H1", "H3"), ("H2", "H4"))
_TERMINALS = ("H1", "H2", "H3", "H4")
_TOL = 0.06  # 6 % covers 480 vs 460 and 240 vs 230 nameplates.


@dataclass(frozen=True)
class JumperPlan:
    mode: str  # "series", "parallel", or "unknown"
    links: tuple[tuple[str, str], ...]  # pairs of pin ids to tie
    reason: str = ""


def _volts(text: Any) -> list[float]:
    raw = str(text or "")
    head = raw.split("(", 1)[0]
    found = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s*(?=V|/|x|X|\s*V)", head)]
    if not found:
        found = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", head)]
    return [v for v in found if 24.0 <= v <= 1000.0]


def is_dual_primary(device: Any) -> bool:
    if not (device.category or "").lower().startswith("power.transformer"):
        return False
    ids = {str(pin.id) for pin in device.pins}
    return set(_TERMINALS) <= ids


def rated_primaries(device: Any, component: Any) -> list[float]:
    text = component.properties.get("primary") or (device.properties or {}).get("primary")
    return sorted(set(_volts(text)), reverse=True)


def applied_primary(component: Any) -> float | None:
    for key in ("primary_voltage", "supply_voltage"):
        vals = _volts(component.properties.get(key))
        if vals:
            return vals[0]
    return None


def _pin_by_caption(device: Any, component: Any) -> dict[str, str] | None:
    """Physical terminal name -> pin id. None when a name is missing or repeated."""
    seen: dict[str, list[str]] = {}
    for pin in device.pins:
        cap = pin_caption(pin, component)
        seen.setdefault(cap, []).append(str(pin.id))
    out: dict[str, str] = {}
    for name in _TERMINALS:
        ids = seen.get(name) or []
        relabelled = [pid for pid in ids if pid != name]
        if len(ids) == 1:
            out[name] = ids[0]
        elif relabelled and len(relabelled) == 1:
            # The instance renamed another pin to this terminal; that rename wins.
            out[name] = relabelled[0]
        else:
            return None
    if len(set(out.values())) != len(out):
        return None
    return out


def jumper_plan(device: Any, component: Any) -> JumperPlan | None:
    if component is None or not is_dual_primary(device):
        return None
    rated = rated_primaries(device, component)
    applied = applied_primary(component)
    if len(rated) < 2:
        return JumperPlan("unknown", (), "the part's dual primary rating (for example 480/240 V) is not given")
    if applied is None:
        return JumperPlan("unknown", (), "the applied primary voltage is not set (primary_voltage)")
    high, low = rated[0], rated[-1]
    if abs(applied - high) <= high * _TOL:
        mode, wanted = "series", SERIES
    elif abs(applied - low) <= low * _TOL:
        mode, wanted = "parallel", PARALLEL
    else:
        return JumperPlan("unknown", (), f"{applied:g} V matches neither primary rating ({high:g} V / {low:g} V)")
    pins = _pin_by_caption(device, component)
    if pins is None:
        return JumperPlan("unknown", (), "terminals H1-H4 cannot each be matched to one pin")
    return JumperPlan(mode, tuple((pins[a], pins[b]) for a, b in wanted))


def jumper_findings(project: Any, catalog: Any) -> list[Any]:
    from panelcad.core.checks import LAYOUT_KINDS, Finding, _device

    out = []
    for sheet in project.sheets:
        if sheet.kind in LAYOUT_KINDS or sheet.kind == "terminal":
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None:
                continue
            plan = jumper_plan(device, comp)
            if plan is None or plan.mode != "unknown":
                continue
            out.append(
                Finding(
                    check="transformer_jumpers_unknown",
                    severity="warning",
                    message=f"{comp.id}: primary jumpers not shown because {plan.reason}. "
                    "Set the dual primary rating and the applied voltage, then the series (H2-H3) "
                    "or parallel (H1-H3, H2-H4) links are drawn.",
                    sheet=sheet.id,
                    ref=comp.id,
                    x=comp.x,
                    y=comp.y,
                )
            )
    return out


def jumper_primitives(device: Any, component: Any) -> list[Any]:
    """Links drawn in the symbol's own frame, between the pin ends and the H labels.

    Each link leaves its terminal sideways, runs vertically on its own track, and has a
    dot at both terminals so it does not read as a connection where it only crosses.
    """
    from panelcad.core.primitives import Primitive

    plan = jumper_plan(device, component)
    if plan is None or not plan.links:
        return []
    pins = {str(pin.id): pin for pin in device.pins}
    tracks = (2.2, 4.4)
    out: list[Any] = []
    for index, (a, b) in enumerate(plan.links):
        pa, pb = pins[a], pins[b]
        x = float(pa.x) + (tracks[index % 2] if float(pa.x) <= device.schematic_width / 2 else -tracks[index % 2])
        out.append(Primitive(type="line", x=x, y=float(pa.y), x2=x, y2=float(pb.y)))
        for pin in (pa, pb):
            out.append(Primitive(type="circle", x=x, y=float(pin.y), r=0.55, fill=True))
    return out
