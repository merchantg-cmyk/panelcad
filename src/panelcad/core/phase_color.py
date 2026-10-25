"""Phase colors for 3-phase power conductors.

Control sheets stay the ordinary ink. On a power schematic the stroke follows
the phase through breakers, contactors, and overloads. A reversing swap keeps
the color of the phase that landed on that pole. A star-delta winding end takes
the color of the phase it joins in delta (W2 follows U1, U2 follows V1, V2
follows W1). The NEMA neutral is white/grey. The IEC neutral default is
blue (default light blue #5eb0e8). IEC 60445 §6.2.2 is unverified from an
official source. A motor star bridge is not a system neutral and stays the
ordinary power ink. Ground is green (IEC green with a yellow edge). Every
phase uses the same stroke width as a normal power wire. Grey and yellow
are solid mid tones with no black outline. Brown, black, and grey are a
permitted IEC default, not a required L1/L2/L3 order. Black/red/blue and
brown/orange/yellow are a US shop convention.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from panelcad.core.models import PhaseColors, PhasePalette, Project, Sheet
from panelcad.library.catalog import Catalog

PHASE_COLOR_SCHEMA: dict[str, object] = {
    "op": "set_phase_colors",
    "args": {
        "enabled": "true/false. Default true.",
        "system_voltage": "Volts used to pick the NEMA palette. Omit to follow the drawing.",
        "nema_high_voltage": "NEMA uses the 480 V palette at or above this voltage. Default 300.",
        "nema_low": "208/240 V colors L1,L2,L3. US shop convention: black,red,blue. A map or 'black,red,blue'.",
        "nema_high": "480 V colors L1,L2,L3. US shop convention: brown,orange,yellow.",
        "iec": "IEC colors L1,L2,L3. Brown, black, and grey are a permitted default, not a required order.",
        "neutral": "NEMA neutral stroke. Default white/grey.",
        "iec_neutral": (
            "IEC neutral stroke. Blue (default light blue #5eb0e8). "
            "IEC 60445 §6.2.2 is unverified from an official source."
        ),
        "ground": "Ground stroke. Default green. IEC draws a yellow edge.",
    },
}

_NAMES = {
    "black": "111111",
    "red": "c42020",
    "blue": "1d4e9e",
    "brown": "6b3e23",
    "orange": "e06a12",
    "yellow": "c9a000",
    "grey": "7f7f7f",
    "gray": "7f7f7f",
    "white": "f2f2f2",
    "green": "1e8a34",
    "lightblue": "5eb0e8",
}

_SEED = {"L1": "L1", "L2": "L2", "L3": "L3", "1": "L1", "3": "L2", "5": "L3"}
_POLES = (
    ("L1", "T1"),
    ("L2", "T2"),
    ("L3", "T3"),
    ("1", "2"),
    ("3", "4"),
    ("5", "6"),
    ("T1", "2"),
    ("T2", "4"),
    ("T3", "6"),
    ("L1", "U"),
    ("L2", "V"),
    ("L3", "W"),
    ("R", "U"),
    ("S", "V"),
    ("T", "W"),
    # Feed-through terminal: the phase does not stop at the block.
    ("A", "B"),
)
_WINDINGS = (
    ("U1", "U2"),
    ("V1", "V2"),
    ("W1", "W2"),
    ("T1", "T4"),
    ("T2", "T5"),
    ("T3", "T6"),
)
_END_PINS = {"U2", "V2", "W2", "T4", "T5", "T6"}
# When two phases reach one pin in the same number of hops, the pin keeps the
# phase its name already means (T1 stays L1). That is the forward path. The
# reversing contactor still holds the swapped color on its own poles.
_CANONICAL_PHASE = {
    "1": "L1",
    "2": "L1",
    "L1": "L1",
    "T1": "L1",
    "U": "L1",
    "R": "L1",
    "U1": "L1",
    "3": "L2",
    "4": "L2",
    "L2": "L2",
    "T2": "L2",
    "V": "L2",
    "S": "L2",
    "V1": "L2",
    "5": "L3",
    "6": "L3",
    "L3": "L3",
    "T3": "L3",
    "W": "L3",
    "W1": "L3",
    "T": "L3",
}
_LINE_PINS = ("L1", "L2", "L3")
_STAR_LOAD = {"T1", "T2", "T3"}
_GROUND_NETS = {"PE", "GND", "GROUND", "EARTH"}
_NEUTRAL_NETS = {"N", "NEUTRAL", "PEN"}


@dataclass(frozen=True)
class PhasePen:
    """One conductor stroke. ``outline`` is a wider under-stroke for pale colors."""

    stroke: str
    phase: str
    outline: str | None = None
    width: float = 0.45
    outline_width: float = 1.15


def normalize_color(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in _NAMES:
        text = _NAMES[text]
    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"color must be a name or #rrggbb, got '{value}'")
    return f"#{text}"


def phase_color_settings(project: Project) -> PhaseColors:
    raw = getattr(project, "phase_colors", None)
    if raw is None:
        return PhaseColors()
    if isinstance(raw, PhaseColors):
        return raw
    return PhaseColors.model_validate(raw)


def _palette_dict(value: object, current: PhasePalette) -> dict[str, str]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace("/", ",").split(",") if part.strip()]
        if len(parts) != 3:
            raise ValueError("phase palette needs three colors: L1,L2,L3")
        value = {"l1": parts[0], "l2": parts[1], "l3": parts[2]}
    if not isinstance(value, dict):
        raise ValueError("phase palette must be L1,L2,L3 or a map")
    merged = current.model_dump()
    merged.update({str(k).lower(): v for k, v in value.items()})
    return {
        "l1": normalize_color(str(merged["l1"])),
        "l2": normalize_color(str(merged["l2"])),
        "l3": normalize_color(str(merged["l3"])),
    }


def update_phase_colors(project: Project, **fields: object) -> dict[str, object]:
    """Merge a project phase-color edit. Unknown keys raise KeyError."""

    current = phase_color_settings(project)
    data = current.model_dump()
    allowed = {
        "enabled",
        "system_voltage",
        "nema_high_voltage",
        "nema_low",
        "nema_high",
        "iec",
        "neutral",
        "iec_neutral",
        "ground",
    }
    for key, value in fields.items():
        if key not in allowed:
            raise KeyError(f"unknown phase-color field '{key}'")
        if value is None:
            continue
        if key in {"nema_low", "nema_high", "iec"}:
            data[key] = _palette_dict(value, PhasePalette.model_validate(data[key]))
        elif key in {"neutral", "iec_neutral", "ground"}:
            data[key] = normalize_color(str(value))
        elif key == "enabled":
            token = str(value).strip().lower()
            data[key] = value if isinstance(value, bool) else token not in {"0", "false", "no", "off"}
        elif key == "system_voltage":
            token = str(value).strip().lower()
            data[key] = None if token in {"", "auto", "none"} else float(value)
        else:
            data[key] = float(value)
    project.phase_colors = PhaseColors.model_validate(data)
    return phase_color_payload(project)


def phase_color_payload(project: Project) -> dict[str, object]:
    settings = phase_color_settings(project)
    return {
        "enabled": settings.enabled,
        "system_voltage": settings.system_voltage,
        "nema_high_voltage": settings.nema_high_voltage,
        "nema_low": settings.nema_low.model_dump(),
        "nema_high": settings.nema_high.model_dump(),
        "iec": settings.iec.model_dump(),
        "neutral": settings.neutral,
        "iec_neutral": settings.iec_neutral,
        "ground": settings.ground,
        "warnings": phase_color_warnings(project),
        "defaults": PhaseColors().model_dump(),
    }


def _looks_blue(color: str) -> bool:
    """True for a blue line color. Grey, white, and black are not blue."""

    try:
        red, green, blue = _rgb(color)
    except ValueError:
        return False
    if max(red, green, blue) - min(red, green, blue) < 28:
        return False
    return blue >= 70 and blue >= red + 24 and blue + 8 >= green


def _project_has_neutral(project: Project) -> bool:
    for sheet in project.sheets:
        for wire in sheet.wires:
            names = [str(wire.net or ""), str(wire.number or ""), str(wire.label or "")]
            for end in (wire.frm, wire.to):
                names.append(str(end))
                if "." in str(end):
                    names.append(str(end).rsplit(".", 1)[-1])
            if any(name.strip().upper() in _NEUTRAL_NETS for name in names):
                return True
    return False


def phase_color_warnings(project: Project) -> list[str]:
    """Warn when an IEC line color is blue and the drawing has a neutral.

    Blue identifies the neutral. A blue line color on the same drawing makes
    the two conductors look alike.
    """

    settings = phase_color_settings(project)
    if not any(_looks_blue(color) for color in (settings.iec.l1, settings.iec.l2, settings.iec.l3)):
        return []
    if not _project_has_neutral(project):
        return []
    return [
        "An IEC line color is blue and this drawing has a neutral. "
        "Keep blue for the neutral and pick another color for that line."
    ]


def _rgb(color: str) -> tuple[int, int, int]:
    raw = normalize_color(color).lstrip("#")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def legible_pen(color: str, *, phase: str) -> PhasePen:
    """Same width as a power wire. Near-white becomes a visible grey; nothing is outlined."""

    raw = normalize_color(color)
    red, green, blue = _rgb(raw)
    lum = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    stroke = "#a6a6a6" if lum >= 230 else raw
    return PhasePen(stroke=stroke, phase=phase, width=0.45)


def _sheet_is_iec(project: Project, sheet: Sheet) -> bool:
    from panelcad.core.refs import sheet_drawing_standard

    return sheet_drawing_standard(project, sheet) == "IEC"


def _infer_voltage(project: Project, sheet: Sheet) -> float:
    settings = phase_color_settings(project)
    if settings.system_voltage is not None:
        return float(settings.system_voltage)
    found: list[float] = []
    pages = [sheet, *[page for page in project.sheets if page.id != sheet.id]]
    for page in pages:
        for comp in page.components:
            raw = comp.properties.get("voltage")
            if raw is None or raw == "":
                continue
            try:
                found.append(float(raw))
            except (TypeError, ValueError):
                continue
    if found:
        return max(found)
    return 400.0 if _sheet_is_iec(project, sheet) else 480.0


def palette_for(project: Project, sheet: Sheet) -> tuple[str, PhasePalette] | None:
    """Which named palette this sheet draws, or None when phase color is off."""

    settings = phase_color_settings(project)
    if not settings.enabled:
        return None
    if _sheet_is_iec(project, sheet):
        return "iec", settings.iec
    voltage = _infer_voltage(project, sheet)
    if voltage >= float(settings.nema_high_voltage):
        return "nema_high", settings.nema_high
    return "nema_low", settings.nema_low


def pen_for_phase(project: Project, sheet: Sheet, phase: str) -> PhasePen | None:
    chosen = palette_for(project, sheet)
    if chosen is None:
        return None
    name, palette = chosen
    settings = phase_color_settings(project)
    if phase == "L1":
        return legible_pen(palette.l1, phase=phase)
    if phase == "L2":
        return legible_pen(palette.l2, phase=phase)
    if phase == "L3":
        return legible_pen(palette.l3, phase=phase)
    if phase == "N":
        color = settings.iec_neutral if name == "iec" else settings.neutral
        return legible_pen(color, phase=phase)
    if phase == "PE":
        green = legible_pen(settings.ground, phase=phase)
        if name == "iec":
            # Green core, yellow edge: the IEC protective-earth marking.
            return PhasePen(
                stroke=green.stroke,
                outline="#a68400",
                phase="PE",
                width=0.45,
                outline_width=1.25,
            )
        return green
    return None


def _control_sheet(sheet: Sheet) -> bool:
    style = str(getattr(sheet, "drawing_style", "") or "")
    return style in {"ladder", "iec"} or sheet.kind not in {"schematic", "io"}


def _split(ref: str) -> tuple[str, str] | None:
    if "." not in ref:
        return None
    comp, pin = ref.split(".", 1)
    if not comp or not pin:
        return None
    return comp, pin


def wire_phase_pens(project: Project, sheet: Sheet, catalog: Catalog) -> dict[str, PhasePen]:
    """wire id → stroke. Empty on control sheets and when the setting is off."""

    if palette_for(project, sheet) is None or _control_sheet(sheet):
        return {}
    try:
        traced, _pins = _trace_phase_maps(sheet, catalog)
    except (KeyError, ValueError, AttributeError):
        traced = {}
    pens: dict[str, PhasePen] = {}
    for wire in sheet.wires:
        phase = traced.get(wire.id)
        if phase is None:
            phase = _special_phase(wire)
        if phase is None:
            continue
        pen = pen_for_phase(project, sheet, phase)
        if pen is not None:
            pens[wire.id] = pen
    return pens


def pin_phase_map(sheet: Sheet, catalog: Catalog) -> dict[str, str]:
    """``comp.pin`` → phase. Includes pole load pins and star-point pins."""

    try:
        _wires, pins = _trace_phase_maps(sheet, catalog)
    except (KeyError, ValueError, AttributeError):
        return {}
    return pins


def _special_phase(wire: object) -> str | None:
    net = str(getattr(wire, "net", "") or "").strip().upper()
    label = str(getattr(wire, "label", "") or "").strip().upper()
    token = net or label
    if token in _GROUND_NETS:
        return "PE"
    if token in _NEUTRAL_NETS:
        return "N"
    return None


def _trace_phases(sheet: Sheet, catalog: Catalog) -> dict[str, str]:
    wires, _pins = _trace_phase_maps(sheet, catalog)
    return wires


def _trace_phase_maps(sheet: Sheet, catalog: Catalog) -> tuple[dict[str, str], dict[str, str]]:
    adj: dict[str, list[tuple[str, str]]] = {}
    devices: dict[str, object] = {}
    for comp in sheet.components:
        try:
            devices[comp.id] = catalog.get(comp.library_id)
        except KeyError:
            continue
    for wire in sheet.wires:
        ends = (_split(wire.frm), _split(wire.to))
        if ends[0] is None or ends[1] is None:
            continue
        left = f"{ends[0][0]}.{ends[0][1]}"
        right = f"{ends[1][0]}.{ends[1][1]}"
        adj.setdefault(left, []).append((right, wire.id))
        adj.setdefault(right, []).append((left, wire.id))

    # The star contactor's load side is the neutral bridge, so its poles do not
    # carry a phase. The delta contactor does: T1-L1 brings U1's phase to W2.
    starred = {comp_id for comp_id, device in devices.items() if _is_star_contactor(comp_id, device, adj)}
    poles: dict[str, list[str]] = {}
    for comp in sheet.components:
        device = devices.get(comp.id)
        if device is None or comp.id in starred:
            continue
        cat = str(getattr(device, "category", "") or "")
        if cat.startswith("load.motor"):
            continue
        overrides = getattr(comp, "pin_overrides", None) or {}
        ids = {pin.id for pin in device.pins} | set(overrides.keys())
        for pin in device.pins:
            for ov_id, ov_pt in overrides.items():
                if abs(float(pin.x) - float(ov_pt.x)) < 0.2 and abs(float(pin.y) - float(ov_pt.y)) < 0.2:
                    poles.setdefault(f"{comp.id}.{pin.id}", []).append(f"{comp.id}.{ov_id}")
                    poles.setdefault(f"{comp.id}.{ov_id}", []).append(f"{comp.id}.{pin.id}")
        for src, dst in _POLES:
            if src in ids and dst in ids:
                poles.setdefault(f"{comp.id}.{src}", []).append(f"{comp.id}.{dst}")
                poles.setdefault(f"{comp.id}.{dst}", []).append(f"{comp.id}.{src}")

    pin_phase: dict[str, str] = {}
    pin_hop: dict[str, int] = {}
    queue: deque[str] = deque()

    def paint(ref: str, phase: str, hop: int) -> None:
        old = pin_phase.get(ref)
        if old is None:
            pin_phase[ref] = phase
            pin_hop[ref] = hop
            queue.append(ref)
            return
        if phase == old:
            return
        old_hop = pin_hop[ref]
        if hop > old_hop:
            return
        if hop < old_hop:
            pin_phase[ref] = phase
            pin_hop[ref] = hop
            queue.append(ref)
            return
        # Same length: a shared motor lead keeps T1=L1, T2=L2, T3=L3.
        # The swapped phase stays on the reversing pole that carried it.
        want = _CANONICAL_PHASE.get(ref.rsplit(".", 1)[-1])
        if want is not None and phase == want and old != want:
            pin_phase[ref] = phase
            pin_hop[ref] = hop
            queue.append(ref)

    for comp in sheet.components:
        device = devices.get(comp.id)
        if device is None:
            continue
        pin_ids = {pin.id for pin in device.pins}
        if not ({"1", "3", "5"} <= pin_ids or {"L1", "L2", "L3"} <= pin_ids):
            continue
        for pin in device.pins:
            phase = _SEED.get(pin.id)
            if phase is None:
                continue
            ref = f"{comp.id}.{pin.id}"
            if adj.get(ref):
                continue
            paint(ref, phase, 0)

    if len({phase for phase in pin_phase.values()}) < 2:
        return {}, {}

    def drain() -> None:
        while queue:
            ref = queue.popleft()
            phase = pin_phase[ref]
            hop = pin_hop[ref]
            for other, _wid in adj.get(ref, []):
                paint(other, phase, hop + 1)
            for other in poles.get(ref, []):
                paint(other, phase, hop + 1)

    drain()

    for comp in sheet.components:
        device = devices.get(comp.id)
        if device is None:
            continue
        ids = {pin.id for pin in device.pins}
        for src, dst in _WINDINGS:
            if src not in ids or dst not in ids:
                continue
            a = f"{comp.id}.{src}"
            b = f"{comp.id}.{dst}"
            if a in pin_phase and b not in pin_phase:
                paint(b, pin_phase[a], pin_hop[a] + 1)
            elif b in pin_phase and a not in pin_phase:
                paint(a, pin_phase[b], pin_hop[b] + 1)
    # U2/V2/W2 pick up the line phase above. Carry it out to K2 and K3.
    # The star bridge (K3 T1–T3) is a winding star point, not a system
    # neutral, so it is left off the phase map and stays ordinary power ink.
    drain()

    traced: dict[str, str] = {}
    for wire in sheet.wires:
        if _is_star_bridge(wire):
            continue
        ends = (_split(wire.frm), _split(wire.to))
        if ends[0] is None or ends[1] is None:
            continue
        left = f"{ends[0][0]}.{ends[0][1]}"
        right = f"{ends[1][0]}.{ends[1][1]}"
        phase = _wire_phase(left, right, pin_phase, pin_hop)
        if phase:
            traced[wire.id] = phase
    return traced, pin_phase


def _wire_phase(
    left: str,
    right: str,
    pin_phase: dict[str, str],
    pin_hop: dict[str, int],
) -> str:
    if left not in pin_phase and right not in pin_phase:
        return ""
    if left not in pin_phase:
        return pin_phase[right]
    if right not in pin_phase:
        return pin_phase[left]
    if pin_hop[left] <= pin_hop[right]:
        return pin_phase[left]
    return pin_phase[right]


def _is_star_contactor(comp_id: str, device: object, adj: dict[str, list[tuple[str, str]]]) -> bool:
    """True when the load pins are tied together. That bridge is the star point."""

    ids = {pin.id for pin in getattr(device, "pins", [])}
    if not _STAR_LOAD <= ids:
        return False
    for pin in _STAR_LOAD:
        for other, _wid in adj.get(f"{comp_id}.{pin}", []):
            parsed = _split(other)
            if parsed is not None and parsed[0] == comp_id and parsed[1] in _STAR_LOAD:
                return True
    return False


def _is_star_bridge(wire: object) -> bool:
    ends = (_split(str(getattr(wire, "frm", ""))), _split(str(getattr(wire, "to", ""))))
    if ends[0] is None or ends[1] is None:
        return False
    return ends[0][0] == ends[1][0] and {ends[0][1], ends[1][1]} <= _STAR_LOAD
