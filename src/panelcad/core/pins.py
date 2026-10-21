"""Pin / port display captions (OEM terminal text vs internal id)."""

from __future__ import annotations

from panelcad.core.models import Component, PinDef

# Internal id → how the OEM prints the terminal on the block.
SCHEMES: dict[str, dict[str, str]] = {
    "keyence.x": {f"I{i}": f"X{i}" for i in range(16)},
    "keyence.y": {f"Q{i}": f"Y{i}" for i in range(16)},
    "smc.out": {f"Q{i}": f"OUT{i}" for i in range(32)},
    "gc.si": {
        "Si0": "Si0-Si1",
        "Si2": "Si2-Si3",
        "Si4": "Si4-Si5",
        "Si8": "Si8-Si9",
        "Si10": "Si10-Si11",
        "So0": "So0",
        "So1": "So1",
        "So2": "So2",
        "SOUTA": "S-OUT A",
        # compact_16dio fallback (old 4-pin dump)
        "I0_0": "Si4-Si5",
        "I0_1": "Si2-Si3",
        "I0_2": "Si0-Si1",
        "I0_3": "Si8-Si9",
        "Q0_0": "So0",
        "Q0_1": "So1",
        "Q0_2": "So2",
        "Q0_3": "S-OUT A",
    },
}


def pin_caption(pin: PinDef, component: Component | None = None) -> str:
    """Text drawn on the terminal: instance override, then library label, then id."""
    if component is not None:
        ov = component.pin_overrides.get(pin.id)
        if ov is not None and getattr(ov, "label", None):
            return str(ov.label)
        raw = component.properties.get("pin_labels")
        if isinstance(raw, dict) and pin.id in raw:
            text = str(raw[pin.id]).strip()
            if text:
                return text
    label = (pin.label or "").strip()
    return label or pin.id


def shadowed_terminal_texts(device, component: Component | None, reach: float = 8.0) -> set[int]:
    """Indexes of static symbol texts that would contradict this component's pin captions.

    A symbol may print its own terminal markings (``H2``, ``X1``) beside the pins.
    When the instance relabels a pin, the printed id beside that pin is stale, and a
    printed marking equal to another pin's new caption would show the same name twice.
    Only a text that names a pin and sits within ``reach`` mm of that pin counts as a
    terminal marking, so lens letters or legends inside a body are left alone.
    """
    if component is None:
        return set()
    pins = {str(pin.id): pin for pin in getattr(device, "pins", None) or []}
    captions = {pid: pin_caption(pin, component) for pid, pin in pins.items()}
    relabelled = {pid for pid, cap in captions.items() if cap and cap != pid}
    if not relabelled:
        return set()
    taken = {captions[pid] for pid in relabelled}
    out: set[int] = set()
    for index, prim in enumerate(getattr(device, "primitives", None) or []):
        if getattr(prim, "type", "") != "text":
            continue
        text = str(getattr(prim, "text", "") or "").strip()
        pin = pins.get(text)
        if pin is None or not (text in relabelled or text in taken):
            continue
        # Symbol texts and pins share the symbol's own frame.
        if abs(float(prim.x) - float(pin.x)) <= reach and abs(float(prim.y) - float(pin.y)) <= 3.0:
            out.add(index)
    return out


def scheme_labels(name: str) -> dict[str, str]:
    return dict(SCHEMES.get(name, {}))


def extra_captions(component: Component) -> list[str]:
    """Lines drawn above a schematic symbol besides the ref (tag, Si pair)."""
    lines: list[str] = []
    lid = (component.library_id or "").lower()
    tag = str(component.properties.get("tag") or "").strip()
    # Off-page arrows already say TO/FROM; xref draws the destination. Don't stack a net tag.
    if tag and tag != component.id and "offpage" not in lid:
        lines.append(tag)
    si = str(component.properties.get("Si") or "").strip()
    if si:
        lines.append(si if si.upper().startswith("SI") else f"Si {si}")
    described = str(component.properties.get("io_description") or "").strip()
    # A 3-wire prox is 28 mm tall on a 36 mm pitch. The blue return leaves the
    # bottom pin, so a second line above the next sensor sits on that wire.
    # The card already prints the same description beside the point.
    if "prox_3wire" in lid:
        described = ""
    if described and described.lower() != "spare" and described not in lines:
        lines.append(described)
    return lines


def io_terminal_caption(
    component: Component,
    width: float,
    height: float,
    scale: float = 1.0,
) -> tuple[float, float, str, float, str] | None:
    """Maker terminal number under an I/O field terminal.

    The unique ref (XIO3-2) stays in the model. Drawing it above the symbol
    puts it on the rung. The screw number sits under the block instead.
    """

    if str(component.properties.get("io_role") or "") != "terminal":
        return None
    number = str(component.properties.get("terminal") or "").strip()
    if not number:
        return None
    s = scale if scale > 0 else 1.0
    return (
        component.x + float(width) * s * 0.5,
        component.y + float(height) * s + 2.4 * s,
        "middle",
        2.0,
        number,
    )


def _num_text(value: object) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _amp_text(value: object) -> str:
    text = str(value or "").strip()
    if text.lower().endswith("a") and not text.endswith(" A"):
        return text[:-1].strip() + " A"
    return text


def rating_captions(component: Component) -> list[str]:
    """Horsepower, voltage, FLA, and a sized breaker or overload rating.

    Drawn to the right of the symbol.
    """

    props = component.properties
    role = str(props.get("role") or "")
    lid = (component.library_id or "").lower()
    lines: list[str] = []
    is_motor = role == "motor" or "motor" in lid
    if is_motor:
        bits: list[str] = []
        if props.get("hp") not in (None, ""):
            bits.append(f"{_num_text(props.get('hp'))} hp")
        elif props.get("kW") not in (None, ""):
            bits.append(f"{_num_text(props.get('kW'))} kW")
        volts = props.get("voltage_label")
        if volts in (None, ""):
            volts = props.get("voltage")
        if volts not in (None, ""):
            bits.append(f"{_num_text(volts)} V")
        if bits:
            lines.append(" ".join(bits))
        fla = props.get("FLA", props.get("fla"))
        if fla not in (None, ""):
            lines.append(f"FLA {_num_text(fla)} A")
        nameplate = props.get("nameplate_fla")
        table = props.get("table_flc")
        try:
            differs = (
                nameplate not in (None, "")
                and table not in (None, "")
                and abs(float(nameplate) - float(table)) > 0.05
            )
        except (TypeError, ValueError):
            differs = False
        if differs:
            lines.append(f"nameplate {_num_text(nameplate)} A")
    sized = str(props.get("sized_by") or "") == "nec-430"
    if sized and (role == "breaker" or "breaker" in lid or "mcb" in lid or "mccb" in lid):
        inn = props.get("In")
        if inn not in (None, ""):
            lines.append(_amp_text(inn))
    if sized and (role == "overload" or ("overload" in lid and "contact" not in lid and role != "motor")):
        span = _adjustable_range(props)
        if span is not None:
            lines.append(f"{_num_text(span[0])}–{_num_text(span[1])} A")
        setting = props.get("setting")
        if setting not in (None, ""):
            lines.append(_amp_text(setting))
    return lines


def _adjustable_range(props: dict) -> tuple[float, float] | None:
    """Selected-part range. A single point such as 7.6-7.6 A is not shown."""

    from panelcad.core.checks import parse_amp_range

    rng = parse_amp_range(props.get("range"))
    if rng is None or abs(float(rng[1]) - float(rng[0])) < 0.05:
        return None
    return (float(rng[0]), float(rng[1]))


RATING_SIZE = 2.2
RATING_STEP = 3.1


def rating_caption_positions(
    component: Component,
    height: float,
    scale: float = 1.0,
    width: float = 0.0,
) -> list[tuple[float, float, str]]:
    """World coordinates (x, baseline y, text) to the right of the symbol."""

    lines = rating_captions(component)
    if not lines:
        return []
    step = scale if scale > 0 else 1.0
    span = float(width) if width else float(height)
    y0 = float(component.y) + float(height) * step * 0.55
    x = float(component.x) + span * step + 2.2 * step
    return [(x, y0 + index * RATING_STEP * step, line) for index, line in enumerate(lines)]


def caption_is_oem(pin: PinDef, component: Component | None = None) -> bool:
    """True when the drawn terminal text is an OEM caption, not the internal id."""
    cap = pin_caption(pin, component)
    return bool(cap) and cap != pin.id
