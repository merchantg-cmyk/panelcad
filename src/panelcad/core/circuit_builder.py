"""Insert a motor circuit (DOL, VFD, reversing, star-delta, soft starter, PLC) onto a schematic sheet.

This is the configurable inserter (Circuit Builder). Control-versus-power
wiring checks stay in ``panelcad.core.circuit``.

Symbols follow the project (or sheet) drawing standard. The default is
NEMA/ANSI — George's shop prints, and the same default as ``DESIGN.md`` and
``TitleBlock.standard``. An explicit IEC project or sheet keeps the original
``iec.*`` parts and pin pairs. NEMA / ANSI / JIC / NFPA resolve to ``nema.*``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from panelcad.core.ink import schematic_ink_rect
from panelcad.core.refs import resolve_drawing_standard, resolve_ref_prefix
from panelcad.core.route import component_aabb
from panelcad.library.catalog import normalize_standard
from panelcad.render.titleblock import working_rect

# Vertical power stack and the horizontal control rung. Inside the 4–8 mm band.
GAP = 6.0
# Close gaps this far before sliding the column (keeps the tag out of the zone strip).
MIN_GAP = 4.0
# Last-resort spacing when MIN_GAP still leaves the stack taller than the sheet.
TIGHT_GAP = 2.0
# Keep the insert this far inside working_rect (above the title block).
WORKING_PAD = 1.0
# Clearance when a second circuit has to slide off an existing symbol.
CLEAR = 8.0
HP_TO_KW = 0.746

# DESIGN.md and models.TitleBlock.standard: start NEMA/ANSI (George's prints).
DEFAULT_STANDARD = "NEMA/ANSI"

DOOR_ROLES = frozenset(
    {
        "estop",
        "stop",
        "start",
        "fwd_pb",
        "rev_pb",
        "selector",
        "hoa",
        "pilot",
        "pilot_run",
        "pilot_fault",
        "pilot_fwd",
        "pilot_rev",
        "fwd_rev",
    }
)

MCB_3P = "iec.breakers.mcb_3p_32a"
MCCB_3P = "iec.breakers.mccb_3p_100a"
MCB_1P = "iec.breakers.mcb_1p_16a"
DISCONNECT = "iec.protection.disconnect_3p"
CONTACTOR_9A = "iec.contactors.ac3_9a"
CONTACTOR_32A = "iec.contactors.ac3_32a"
OVERLOAD = "iec.protection.overload_9a"
MOTOR = "iec.loads.motor_3ph"
PSU = "iec.power.psu_24v_5a"
ESTOP = "iec.operators.estop"
PB_NC = "iec.operators.pb_nc"
PB_NO = "iec.operators.pb_no"

KINDS = ("dol", "vfd", "reversing", "star_delta", "soft_starter", "plc_motor")
KIND_ALIASES = {
    "rev": "reversing",
    "reversing_starter": "reversing",
    "stardelta": "star_delta",
    "star-delta": "star_delta",
    "wye_delta": "star_delta",
    "wye-delta": "star_delta",
    "wyedelta": "star_delta",
    "softstarter": "soft_starter",
    "soft-starter": "soft_starter",
    "plc-motor": "plc_motor",
    "plc_run": "plc_motor",
}
# "softstart" and "plc" stay unknown so existing tests and typos still fail closed.
BRANCHES = ("breaker", "disconnect")
CONTROLS = ("none", "local")
CONTROL_WIRINGS = ("2wire", "3wire")
STARTERS = ("dol", "vfd")
EXTRA_KINDS = frozenset({"reversing", "star_delta", "soft_starter", "plc_motor"})
OVERLOAD_PLACEMENTS = ("windings", "line")
DEFAULT_TIMER_SECONDS = 6.0
DEFAULT_IO_RUN = "I0_0"
DEFAULT_IO_OL = "I0_1"
DEFAULT_IO_AUTO = "I0_2"
DEFAULT_IO_OUT = "Q0_0"
PLC_MODELS = ("nema.plc.compact_16dio", "iec.plc.compact_16dio")

# Structured tool schema for the AI agent (and the GUI/Session contract).
INSERT_CIRCUIT_SCHEMA: dict[str, Any] = {
    "op": "insert_circuit",
    "description": (
        "Insert a motor circuit on a schematic sheet. Adds only the new in-cabinet "
        "devices to an existing panel_layout (creates Enclosure if needed) without "
        "moving placed parts; local operators go on a door_layout sheet. Local DOL "
        "control uses the same Motor control ladder as reversing, star-delta, soft "
        "starter, and PLC. One undo removes it. Does not replace "
        "the project. Symbols follow the sheet standard, else the title-block standard "
        "(default NEMA/ANSI). Explicit IEC keeps iec.* parts."
    ),
    "kinds": {
        "dol": {
            "description": (
                "Direct-on-line starter: branch, contactor, overload, and motor. "
                "Local control is a Motor control ladder: supply, E-stop NC, stop NC, "
                "start NO with a seal-in, overload NC, coil, return."
            ),
        },
        "vfd": {
            "description": (
                "VFD: branch, optional line contactor, drive, and motor. "
                "No overload on the motor leads."
            ),
        },
        "reversing": {
            "description": (
                "FWD/REV contactors with mechanical and electrical interlock, "
                "overload, and FWD/REV/STOP. Optional pilots."
            ),
        },
        "star_delta": {
            "description": (
                "Main, star, and delta contactors with an on-delay timer and "
                "interlocks. Overload in the windings at 0.58×FLA (default) or "
                "in the line at 1.0×FLA."
            ),
        },
        "soft_starter": {
            "description": (
                "Branch, soft starter, optional bypass contactor, and 2-wire or "
                "3-wire control. Optional pilots."
            ),
        },
        "plc_motor": {
            "description": (
                "DOL (default) or VFD starter with an interposing CR. Optional HOA "
                "(Hand = local 3-wire, Off = neither, Auto = PLC). PLC model and "
                "I/O addresses for run feedback, OL trip, Auto, and the CR output."
            ),
        },
    },
    "args": {
        "kind": {
            "type": "string",
            "enum": list(KINDS),
            "required": True,
            "description": "Circuit kind to insert.",
        },
        "drive": {
            "type": "string",
            "enum": ["generic_1k5", "gs20_0p5", "es1_0p75"],
            "description": "VFD catalog key (required for kind=vfd; plc_motor when starter=vfd).",
            "kinds": ["vfd", "plc_motor"],
        },
        "branch": {
            "type": "string",
            "enum": list(BRANCHES),
            "description": "Line-side protective device: circuit breaker or disconnect.",
        },
        "line_contactor": {
            "type": "boolean",
            "description": "VFD: place a contactor ahead of the drive.",
            "kinds": ["vfd", "plc_motor"],
        },
        "control": {
            "type": "string",
            "enum": list(CONTROLS),
            "description": "none = power only. local = start-stop (and per-kind extras) on Motor control.",
        },
        "control_wiring": {
            "type": "string",
            "enum": list(CONTROL_WIRINGS),
            "description": "Soft starter: 3-wire start/stop with seal-in, or 2-wire maintained.",
            "kinds": ["soft_starter"],
        },
        "bypass": {
            "type": "boolean",
            "description": "Soft starter: add a bypass contactor across the SS.",
            "kinds": ["soft_starter"],
        },
        "starter": {
            "type": "string",
            "enum": list(STARTERS),
            "description": "PLC motor power circuit: DOL (overload) or VFD (no motor-lead overload).",
            "kinds": ["plc_motor"],
        },
        "pilot_lights": {
            "type": "boolean",
            "description": "Add run / direction / fault pilots on extra kinds.",
            "kinds": ["reversing", "star_delta", "soft_starter", "plc_motor"],
        },
        "timer_seconds": {
            "type": "number",
            "description": (
                "Star-delta on-delay in seconds (typically 2–15). Default 6. "
                "Written on the timer as timer_seconds / setting."
            ),
            "kinds": ["star_delta"],
        },
        "overload_placement": {
            "type": "string",
            "enum": list(OVERLOAD_PLACEMENTS),
            "description": (
                "Star-delta overload: windings (0.58×FLA, IEC/common default) or "
                "line (1.0×FLA, NEMA alternative)."
            ),
            "kinds": ["star_delta"],
        },
        "hoa": {
            "type": "boolean",
            "description": (
                "PLC motor: include a Hand-Off-Auto selector. Default true when "
                "control=local. Hand is local 3-wire; Auto is the interposing CR."
            ),
            "kinds": ["plc_motor"],
        },
        "plc_model": {
            "type": "string",
            "enum": list(PLC_MODELS),
            "description": (
                "PLC library id. Default nema.plc.compact_16dio or iec.plc.compact_16dio "
                "from the drawing standard. Generic compact 16DI/16DO only."
            ),
            "kinds": ["plc_motor"],
        },
        "io_run": {
            "type": "string",
            "description": "PLC input address for run feedback (M aux). Default I0_0.",
            "kinds": ["plc_motor"],
        },
        "io_ol": {
            "type": "string",
            "description": "PLC input address for overload trip. Default I0_1.",
            "kinds": ["plc_motor"],
        },
        "io_auto": {
            "type": "string",
            "description": "PLC input address for HOA Auto. Default I0_2.",
            "kinds": ["plc_motor"],
        },
        "io_out": {
            "type": "string",
            "description": "PLC output address to the interposing CR coil. Default Q0_0.",
            "kinds": ["plc_motor"],
        },
        "kw": {"type": "number", "description": "Motor kilowatts. Default 1.5 kW for DOL, or the drive rating."},
        "hp": {"type": "number", "description": "Motor horsepower. Used with NEC Table 430.250 when FLA is omitted."},
        "voltage": {"type": "number", "description": "Line voltage. Default 480 V NEMA / 400 V IEC."},
        "control_voltage": {
            "type": "number",
            "description": "Control voltage. 24 V uses the PSU; 120 V uses the control transformer.",
        },
        "fla": {"type": "number", "description": "Nameplate FLA. Default is NEC Table 430.250."},
        "manufacturer": {
            "type": "string",
            "description": (
                "Preferred manufacturer. After the symbols are placed, assign that "
                "vendor's catalog SKUs (contactor, overload, drive, PLC, PSU, pilots). "
                "Examples: AutomationDirect, LS Electric, Yaskawa, Allen-Bradley."
            ),
        },
        "secondary_manufacturer": {
            "type": "string",
            "description": (
                "Fill categories the preferred manufacturer does not publish. "
                "Omit it to use the enclosure maker when that name is a parts brand, "
                "otherwise AutomationDirect. none or off leaves those symbols generic. "
                "Fallback rows are marked catalog_fallback=yes."
            ),
        },
        "sheet": {"type": "string", "description": "Sheet id to insert on. Alias: sheet_id."},
        "new_sheet": {"type": "boolean", "description": "Create a Motor power schematic instead of using the open sheet."},
        "x": {"type": "number", "description": "Power-column origin X in millimetres. Default 40."},
        "y": {"type": "number", "description": "Power-column origin Y in millimetres. Default 22."},
        "phases": {
            "type": "number",
            "description": (
                "3-phase only. Omit it or pass 3. phases=1 raises ValueError: "
                "single-phase circuits are not drawn yet. A non-integer such as 3.6 or \"3.6\" "
                "raises ValueError: phases must be an integer. 3, 3.0, \"3\", and \"3.0\" draw. "
                "Single-phase sizing stays on size_motor_circuit and the Motor sizing panel."
            ),
        },
        "place_layout": {
            "type": "boolean",
            "description": (
                "Place in-cabinet devices on the panel layout and operators on the door. "
                "Default true. False draws the schematic only and leaves panel and door sheets alone."
            ),
        },
        "shared_supply": {
            "type": "boolean",
            "description": (
                "Reuse the panel's existing control supply (PSU or control transformer). "
                "Default true. False adds a supply for this circuit. A warning is recorded "
                "when the estimated coil load is over 80% of that supply."
            ),
        },
        "shared_estop": {
            "type": "boolean",
            "description": (
                "One E-stop for the panel, wired into this circuit. Default true. "
                "False adds an E-stop for this motor."
            ),
        },
        "separate_control_rail": {
            "type": "boolean",
            "description": (
                "Put timer, relays, the PSU, and the PLC on their own DIN rail. "
                "Default false: those devices sit on the right of the power rail when they fit."
            ),
        },
        "standard": {
            "type": "string",
            "enum": ["IEC", "NEMA/ANSI"],
            "description": (
                "Drawing standard for this insert. Omit to follow the sheet, then the "
                "title block (default NEMA/ANSI). IEC reversing, star-delta, soft starter, "
                "and PLC motor control use vertical columns between L+ and 0V (or X1 and X2)."
            ),
        },
    },
}


def insert_circuit_help() -> str:
    """One-line agent help built from INSERT_CIRCUIT_SCHEMA."""

    kinds = ", ".join(INSERT_CIRCUIT_SCHEMA["kinds"])
    bits: list[str] = []
    for name, spec in INSERT_CIRCUIT_SCHEMA["args"].items():
        mark = "" if spec.get("required") else "?"
        enum = spec.get("enum")
        bits.append(f"{name}{mark}={'|'.join(str(v) for v in enum)}" if enum else f"{name}{mark}")
    notes = " ".join(
        f"{name}: {spec['description']}" for name, spec in INSERT_CIRCUIT_SCHEMA["kinds"].items()
    )
    return (
        f"Insert a motor circuit on a schematic sheet ({kinds}). "
        f"{INSERT_CIRCUIT_SCHEMA['description']} "
        f"Args: {', '.join(bits)}. {notes} "
        "kw<=4 selects the 9A contactor; kw>15 selects the 100A MCCB. "
        "FLA defaults to NEC Table 430.250. Result includes standard, layout, and warnings. "
        "Devices land on the spacing grid (minimum horizontal and vertical clearance) and "
        "spread across the usable sheet width. Result.density is page fullness; when a page "
        "is over that area limit, split_sheet apply=true continues it with off-page references."
    )


# Line pins are the real device terminals. 1-phase drives are never L1/L2/L3.
DRIVES: dict[str, dict[str, Any]] = {
    "generic_1k5": {
        "library_id": "iec.drives.vfd_1k5",
        "phases": 3,
        "line": ("L1", "L2", "L3"),
        "motor": ("U", "V", "W"),
        "run": "DI1",
        "common": "COM",
        "kw": 1.5,
        "hp": None,
    },
    "gs20_0p5": {
        "library_id": "adc.durapulse.gs21_20p5",
        "phases": 1,
        "line": ("L", "N"),
        "motor": ("U", "V", "W"),
        "run": "DI1",
        "common": "DCM",
        "kw": None,
        "hp": 0.5,
    },
    "es1_0p75": {
        "library_id": "adc.ironhorse.es1_3a_n4",
        "phases": 1,
        "line": ("AC1", "AC2"),
        "motor": ("U", "V", "W"),
        "run": "P7-8",
        "common": "P7-7",
        "kw": None,
        "hp": 0.75,
    },
}

_PHASE_BOTTOM = ("2", "4", "6")
_CONTACTOR_IN = ("L1", "L2", "L3")
_CONTACTOR_OUT = ("T1", "T2", "T3")
_LINE_NETS = ("L1", "L2", "L3")
_LOAD_NETS = ("T1", "T2", "T3")
_OL_IN = ("1", "3", "5")
_OL_OUT = ("2", "4", "6")
_MOTOR_PINS = ("U1", "V1", "W1")
_MOTOR_NETS = ("U1", "V1", "W1")
_DRIVE_MOTOR_NETS = ("U", "V", "W")


@dataclass(frozen=True)
class PinScheme:
    """Power and control pin ids for one drawing standard."""

    branch_align: str
    branch_out: tuple[str, ...]
    disc_align: str
    disc_out: tuple[str, ...]
    disc_1ph_out: tuple[str, ...]
    ol_align: str
    ol_in: tuple[str, ...]
    ol_out: tuple[str, ...]
    motor_align: str
    motor: tuple[str, ...]
    motor_nets: tuple[str, ...]
    psu_plus: str
    psu_minus: str
    estop_in: str
    estop_out: str
    stop_in: str
    stop_out: str
    start_in: str
    start_out: str
    aux_no_in: str = "13"
    aux_no_out: str = "14"
    aux_nc_in: str = "21"
    aux_nc_out: str = "22"
    ol_nc_in: str = "95"
    ol_nc_out: str = "96"
    ol_no_in: str = "97"
    ol_no_out: str = "98"
    timer_no_in: str = "15"
    timer_no_out: str = "18"
    selector_com: str = "11"
    selector_hand: str = "12"
    selector_auto: str = "14"
    pilot_in: str = "X1"
    pilot_out: str = "X2"
    motor_end: tuple[str, ...] = ("W2", "U2", "V2")
    motor_end_nets: tuple[str, ...] = ("W2", "U2", "V2")
    relay_no_in: str = "11"
    relay_no_out: str = "14"
    ss_com: str = "COM"
    ss_run: str = "ST"
    ss_run_aux_in: str = "13"
    ss_run_aux_out: str = "14"
    ss_fault_in: str = "95"
    ss_fault_out: str = "96"
    cpt_hot: str = "X1"
    cpt_ret: str = "X2"


IEC_PINS = PinScheme(
    branch_align="1",
    branch_out=_PHASE_BOTTOM,
    disc_align="1",
    disc_out=_PHASE_BOTTOM,
    disc_1ph_out=("2", "4"),
    ol_align="1",
    ol_in=_OL_IN,
    ol_out=_OL_OUT,
    motor_align="U1",
    motor=_MOTOR_PINS,
    motor_nets=_MOTOR_NETS,
    psu_plus="plus",
    psu_minus="minus",
    estop_in="11",
    estop_out="12",
    stop_in="11",
    stop_out="12",
    start_in="13",
    start_out="14",
)

# Existing nema.yaml overload / motor / disconnect / operators keep their pins.
# New NEMA breakers, power contactors, VFD, and PSU use IEC-style power pins.
NEMA_PINS = PinScheme(
    branch_align="1",
    branch_out=_PHASE_BOTTOM,
    disc_align="L1",
    disc_out=("T1", "T2", "T3"),
    disc_1ph_out=("T1", "T2"),
    ol_align="T1",
    ol_in=("T1", "T2", "T3"),
    ol_out=_OL_OUT,
    motor_align="T1",
    motor=("T1", "T2", "T3"),
    motor_nets=("T1", "T2", "T3"),
    psu_plus="plus",
    psu_minus="minus",
    estop_in="1",
    estop_out="2",
    stop_in="1",
    stop_out="2",
    start_in="3",
    start_out="4",
    aux_no_in="3",
    aux_no_out="4",
    aux_nc_in="1",
    aux_nc_out="2",
    timer_no_in="1",
    timer_no_out="3",
    selector_com="1",
    selector_hand="2",
    selector_auto="3",
    motor_end=("T6", "T4", "T5"),
    motor_end_nets=("T6", "T4", "T5"),
)


@dataclass(frozen=True)
class SymbolSet:
    standard: str
    mcb_3p: str
    mccb_3p: str
    mcb_1p: str
    disconnect: str
    contactor_9a: str
    contactor_32a: str
    overload: str
    motor: str
    psu: str
    estop: str
    pb_nc: str
    pb_no: str
    coil: str
    aux_no: str
    aux_nc: str
    ol_contact: str
    timer: str
    relay: str
    plc: str
    selector: str
    pilot: str
    softstarter: str
    motor_6lead: str
    transformer: str
    pins: PinScheme

    def contactor(self, rating_kw: float) -> str:
        if rating_kw <= 4.0:
            return self.contactor_9a
        return self.contactor_32a

    def branch(self, phases: int, branch: str, rating_kw: float) -> str:
        if branch == "disconnect":
            return self.disconnect
        if phases == 1:
            return self.mcb_1p
        if rating_kw > 15.0:
            return self.mccb_3p
        return self.mcb_3p

    def branch_align(self, branch: str) -> str:
        return self.pins.disc_align if branch == "disconnect" else self.pins.branch_align

    def branch_out(self, branch: str) -> tuple[str, ...]:
        return self.pins.disc_out if branch == "disconnect" else self.pins.branch_out


IEC_SYMBOLS = SymbolSet(
    standard="IEC",
    mcb_3p=MCB_3P,
    mccb_3p=MCCB_3P,
    mcb_1p=MCB_1P,
    disconnect=DISCONNECT,
    contactor_9a=CONTACTOR_9A,
    contactor_32a=CONTACTOR_32A,
    overload=OVERLOAD,
    motor=MOTOR,
    psu=PSU,
    estop=ESTOP,
    pb_nc=PB_NC,
    pb_no=PB_NO,
    coil="iec.contactors.coil",
    aux_no="iec.contactors.aux_no",
    aux_nc="iec.contactors.aux_nc",
    ol_contact="iec.protection.ol_contact",
    timer="iec.timers.on_delay",
    relay="iec.relays.k_4co_24vdc",
    plc="iec.plc.compact_16dio",
    selector="iec.operators.selector_3pos",
    pilot="iec.operators.pilot_light",
    softstarter="iec.drives.softstarter",
    motor_6lead="iec.loads.motor_3ph_6lead",
    transformer="iec.power.control_transformer",
    pins=IEC_PINS,
)

NEMA_SYMBOLS = SymbolSet(
    standard="NEMA/ANSI",
    mcb_3p="nema.breakers.mcb_3p_32a",
    mccb_3p="nema.breakers.mccb_3p_100a",
    mcb_1p="nema.breakers.mcb_1p_16a",
    disconnect="nema.protection.disconnect_3p",
    contactor_9a="nema.contactors.ac3_9a",
    contactor_32a="nema.contactors.ac3_32a",
    overload="nema.protection.overload",
    motor="nema.loads.motor_3ph",
    psu="nema.power.psu_24v_5a",
    estop="nema.operators.estop",
    pb_nc="nema.operators.pb_nc",
    pb_no="nema.operators.pb_no",
    coil="nema.contactors.coil",
    aux_no="nema.contactors.aux_no",
    aux_nc="nema.contactors.aux_nc",
    ol_contact="nema.protection.ol_contact",
    timer="nema.timers.on_delay",
    relay="nema.relays.k_4co_24vdc",
    plc="nema.plc.compact_16dio",
    selector="nema.operators.selector_3pos",
    pilot="nema.operators.pilot_light",
    softstarter="nema.drives.softstarter",
    motor_6lead="nema.loads.motor_3ph_6lead",
    transformer="nema.power.control_transformer",
    pins=NEMA_PINS,
)


@dataclass
class _Part:
    library_id: str
    role: str
    x: float
    y: float
    align_pin: str = ""
    ref: str = ""
    page: str = "power"
    family: str = ""
    suffix: str = ""
    skip_layout: bool = False
    sheet_override: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    scale: float = 1.0
    reuse: bool = False
    rotation: int = 0


def _normalize_overload_placement(value: Any) -> str:
    text = str(value or "windings").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "delta_windings": "windings",
        "winding": "windings",
        "in_windings": "windings",
        "line_side": "line",
        "lineside": "line",
    }
    text = aliases.get(text, text)
    if text not in OVERLOAD_PLACEMENTS:
        raise ValueError(f"unknown overload_placement '{value}'")
    return text


def _normalize_io_pin(value: Any, default: str) -> str:
    text = str(value if value not in (None, "") else default).strip()
    if not text:
        raise ValueError(f"empty I/O address (default {default})")
    return text


def check_circuit_args(
    kind: Any,
    drive: Any,
    branch: Any,
    control: Any,
    control_wiring: Any = "3wire",
    starter: Any = "dol",
    overload_placement: Any = "windings",
    plc_model: Any = None,
) -> dict[str, Any]:
    """Normalize public options. Unknown kind, branch, control, or drive is a ValueError."""

    kind_s = str(kind or "").strip().lower().replace(" ", "_")
    kind_s = KIND_ALIASES.get(kind_s, kind_s)
    branch_s = str(branch or "").strip().lower()
    control_s = str(control or "").strip().lower()
    wiring_s = str(control_wiring or "3wire").strip().lower().replace("-", "").replace("_", "")
    if wiring_s in {"2wire", "2"}:
        wiring_s = "2wire"
    elif wiring_s in {"3wire", "3"}:
        wiring_s = "3wire"
    starter_s = str(starter or "dol").strip().lower()
    if kind_s not in KINDS:
        raise ValueError(f"unknown kind '{kind}'")
    if branch_s not in BRANCHES:
        raise ValueError(f"unknown branch '{branch}'")
    if control_s not in CONTROLS:
        raise ValueError(f"unknown control '{control}'")
    if wiring_s not in CONTROL_WIRINGS:
        raise ValueError(f"unknown control_wiring '{control_wiring}'")
    if starter_s not in STARTERS:
        raise ValueError(f"unknown starter '{starter}'")
    place_s = _normalize_overload_placement(overload_placement)
    drive_s = _drive_key(drive)
    if kind_s == "vfd" and drive_s not in DRIVES:
        raise _drive_error(drive, kind_s)
    if kind_s == "plc_motor" and starter_s == "vfd":
        drive_s = drive_s or "generic_1k5"
        if drive_s not in DRIVES:
            raise _drive_error(drive, kind_s)
    if kind_s in {"dol", "reversing", "star_delta", "soft_starter"}:
        drive_s = None
    if kind_s == "plc_motor" and starter_s == "dol":
        drive_s = None
    plc_s = _normalize_plc_model(plc_model)
    return {
        "kind": kind_s,
        "drive": drive_s,
        "branch": branch_s,
        "control": control_s,
        "control_wiring": wiring_s,
        "starter": starter_s,
        "overload_placement": place_s,
        "plc_model": plc_s,
    }


def _normalize_plc_model(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    aliases = {
        "default": None,
        "compact_16dio": None,
        "nema.plc.compact_16dio": "nema.plc.compact_16dio",
        "iec.plc.compact_16dio": "iec.plc.compact_16dio",
    }
    key = text.lower()
    if key in aliases:
        return aliases[key]
    if text in PLC_MODELS:
        return text
    raise ValueError(f"unknown plc_model '{value}'")


def _phase_count(phases: Any) -> int:
    """Integer phase count. 3.6 and ``\"3.6\"`` are refused; 3, 3.0, ``\"3\"``, and ``\"3.0\"`` pass."""

    if isinstance(phases, bool):
        return int(phases)
    if isinstance(phases, int):
        return phases
    if isinstance(phases, str):
        text = phases.strip()
        try:
            phases = float(text)
        except ValueError:
            raise ValueError("phases must be an integer") from None
    if isinstance(phases, float):
        if not phases.is_integer():
            raise ValueError("phases must be an integer")
        return int(phases)
    raise ValueError("phases must be an integer")


def refuse_non_three_phase(phases: Any) -> None:
    """Circuit Builder draws 3-phase schematics. Single-phase sizing stays off this path."""

    if phases is None or phases == "":
        return
    count = _phase_count(phases)
    if count == 3:
        return
    if count == 1:
        raise ValueError("single-phase circuits are not drawn yet")
    raise ValueError("Circuit Builder draws 3-phase circuits only")


def validate_insert_circuit_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Validate an insert_circuit tool call against INSERT_CIRCUIT_SCHEMA.

    Unknown kinds and illegal enum values raise ValueError. Returns the
    normalized options from ``check_circuit_args``.
    """

    if not isinstance(args, dict):
        raise ValueError("insert_circuit args must be an object")
    kind_raw = args.get("kind") or "dol"
    kind_s = str(kind_raw).strip().lower().replace(" ", "_")
    kind_s = KIND_ALIASES.get(kind_s, kind_s)
    if kind_s not in INSERT_CIRCUIT_SCHEMA["kinds"]:
        raise ValueError(f"unknown kind '{kind_raw}'")
    refuse_non_three_phase(args.get("phases"))
    for name, spec in INSERT_CIRCUIT_SCHEMA["args"].items():
        if name not in args or args[name] in (None, ""):
            continue
        enum = spec.get("enum")
        if not enum or name in {
            "kind",
            "drive",
            "branch",
            "control",
            "control_wiring",
            "starter",
            "overload_placement",
            "plc_model",
        }:
            continue
        allowed = {str(item).strip().lower() for item in enum}
        if name == "standard":
            explicit_standard(args[name])
            continue
        if str(args[name]).strip().lower() not in allowed:
            raise ValueError(f"unknown {name} '{args[name]}'")
    return check_circuit_args(
        kind_s,
        args.get("drive"),
        args.get("branch") or "breaker",
        args.get("control") or "none",
        args.get("control_wiring") or "3wire",
        args.get("starter") or "dol",
        overload_placement=args.get("overload_placement") or "windings",
        plc_model=args.get("plc_model"),
    )


def explicit_standard(value: Any) -> str | None:
    """IEC or NEMA/ANSI. Empty means follow the project. Anything else is an error."""

    if value is None or str(value).strip() == "":
        return None
    token = "".join(ch.lower() for ch in str(value) if ch.isalnum())
    if token in {"nema", "ansi", "jic", "nfpa", "nemaansi"}:
        return "NEMA/ANSI"
    if token == "iec":
        return "IEC"
    raise ValueError(f"unknown standard '{value}'")


def resolve_circuit_standard(session: Any, sheet_id: str | None = None) -> str:
    """Drawing standard for this insert: sheet, then title block, then NEMA/ANSI."""

    return resolve_drawing_standard(session, sheet_id)


def symbols_for(standard: str) -> SymbolSet:
    if normalize_standard(standard) == "NEMA/ANSI":
        return NEMA_SYMBOLS
    return IEC_SYMBOLS


def build_motor_circuit(
    session: Any,
    *,
    kind: str = "dol",
    drive: str | None = None,
    branch: str = "breaker",
    line_contactor: bool = False,
    control: str = "none",
    kw: float | None = None,
    hp: float | None = None,
    sheet_id: str | None = None,
    new_sheet: bool = False,
    x: float = 40,
    y: float = 22,
    voltage: float | None = None,
    control_voltage: float | None = None,
    fla: float | None = None,
    pilot_lights: bool = False,
    bypass: bool = False,
    control_wiring: str = "3wire",
    starter: str = "dol",
    timer_seconds: float | None = None,
    overload_placement: str = "windings",
    hoa: bool = True,
    plc_model: str | None = None,
    io_run: str | None = None,
    io_ol: str | None = None,
    io_auto: str | None = None,
    io_out: str | None = None,
    place_layout: bool = True,
    shared_supply: bool = True,
    shared_estop: bool = True,
    standard: str | None = None,
) -> dict[str, Any]:
    """Place and wire one motor circuit. Caller owns the undo checkpoint.

    New in-cabinet devices are added into free space on an existing
    panel_layout (or a new Enclosure sheet). Local start/stop/e-stop go on a
    door_layout sheet. Existing layout parts stay put. Same tags on schematic,
    layout, BOM, and electrical reports. Local DOL control, and reversing,
    star-delta, soft starter, and PLC motor, put power on the power sheet and
    the holding circuit on a Motor control sheet.
    """

    opts = check_circuit_args(
        kind,
        drive,
        branch,
        control,
        control_wiring,
        starter,
        overload_placement=overload_placement,
        plc_model=plc_model,
    )
    kind_s = opts["kind"]
    drive_key = opts["drive"]
    branch_s = opts["branch"]
    control_s = opts["control"]
    wiring_s = opts["control_wiring"]
    starter_s = opts["starter"]
    place_s = opts["overload_placement"]
    plc_s = opts["plc_model"]
    line = _as_bool(line_contactor) and (kind_s == "vfd" or (kind_s == "plc_motor" and starter_s == "vfd"))
    kw_v = _optional_float(kw)
    hp_v = _optional_float(hp)
    volt_v = _optional_float(voltage)
    ctrl_v = _optional_float(control_voltage)
    fla_v = _optional_float(fla)
    timer_v = _optional_float(timer_seconds)
    if timer_v is None:
        timer_v = DEFAULT_TIMER_SECONDS
    origin_x = float(x)
    origin_y = float(y)
    pilots = _as_bool(pilot_lights)
    bypass_b = _as_bool(bypass)
    hoa_b = _as_bool(hoa)
    io_run_s = _normalize_io_pin(io_run, DEFAULT_IO_RUN)
    io_ol_s = _normalize_io_pin(io_ol, DEFAULT_IO_OL)
    io_auto_s = _normalize_io_pin(io_auto, DEFAULT_IO_AUTO)
    io_out_s = _normalize_io_pin(io_out, DEFAULT_IO_OUT)

    sheet = _target_sheet(session, sheet_id, _as_bool(new_sheet))
    forced = explicit_standard(standard)
    if forced:
        session.sheet(sheet).standard = forced
    symbols = symbols_for(resolve_circuit_standard(session, sheet))
    use_columns = symbols.standard == "IEC" and kind_s in EXTRA_KINDS
    if use_columns:
        session.sheet(sheet).standard = "IEC"
    spec = _resolve_drive(DRIVES[drive_key], symbols) if drive_key else None
    phases = 3 if spec is None else int(spec["phases"])
    rating_kw = _rating_kw(kind_s, spec, kw_v, hp_v)
    motor_props = _motor_props(
        kind_s,
        spec,
        kw_v,
        hp_v,
        voltage=volt_v,
        fla=fla_v,
        standard=symbols.standard,
        phases=phases,
    )
    from panelcad.core.motor_apply import layout_kw_for, prepare_sizing

    ol_factor = 0.58 if kind_s == "star_delta" and place_s != "line" else 1.0
    project_props = getattr(session.project, "properties", {}) or {}
    raw_term = project_props.get("termination_c", motor_props.get("termination_c"))
    try:
        term_c = None if raw_term in (None, "") else int(raw_term)
    except (TypeError, ValueError):
        term_c = None
    sizing = prepare_sizing(
        standard=symbols.standard,
        phases=phases,
        hp=motor_props.get("hp"),
        kw=motor_props.get("kW"),
        volts=motor_props.get("voltage"),
        nameplate=fla_v,
        overload_factor=ol_factor,
        termination_c=term_c,
    )
    layout_kw = layout_kw_for(rating_kw, sizing)

    extra = {
        "control_wiring": wiring_s,
        "starter": starter_s,
        "pilot_lights": pilots,
        "bypass": bypass_b,
        "voltage": volt_v,
        "control_voltage": ctrl_v,
        "fla": motor_props.get("FLA"),
        "motor_props": motor_props,
        "timer_seconds": timer_v,
        "overload_placement": place_s,
        "hoa": hoa_b,
        "plc_model": plc_s or symbols.plc,
        "io_run": io_run_s,
        "io_ol": io_ol_s,
        "io_auto": io_auto_s,
        "io_out": io_out_s,
    }
    parts = _layout(
        session,
        symbols,
        kind=kind_s,
        spec=spec,
        phases=phases,
        branch=branch_s,
        line_contactor=line,
        control=control_s,
        rating_kw=layout_kw,
        x=origin_x,
        y=origin_y,
        extra=extra,
    )
    share_warnings = _share_panel_controls(
        session,
        parts,
        extra,
        symbols,
        shared_supply=_as_bool(shared_supply),
        shared_estop=_as_bool(shared_estop),
    )
    power_parts = [part for part in parts if part.page == "power"]
    control_parts = [part for part in parts if part.page == "control"]
    io_parts = [part for part in parts if part.page == "io"]
    _park_control(session, power_parts)
    control_sheet = None
    io_sheet = None
    if control_parts:
        if use_columns:
            from panelcad.core.circuit_iec import iec_rail_pair, prepare_iec_sheet

            hot, ret = iec_rail_pair(extra)
            control_sheet = _control_target_sheet(session, style="iec", hot=hot, ret=ret)
            prepare_iec_sheet(session, control_sheet, extra)
        else:
            from panelcad.core.circuit_ladder import prepare_ladder_sheet, rail_pair

            hot, ret = rail_pair(extra)
            control_sheet = _control_target_sheet(session, style="ladder", hot=hot, ret=ret)
            prepare_ladder_sheet(session, control_sheet, extra)
        _clear_existing(session, control_sheet, control_parts)
    if io_parts:
        io_sheet = _add_named_sheet(session, "plc-io", "PLC I/O", "PLC inputs and interposing coil")
        if use_columns:
            from panelcad.core.circuit_iec import prepare_iec_sheet

            prepare_iec_sheet(session, io_sheet, extra, io=True)
        else:
            from panelcad.core.circuit_ladder import prepare_ladder_sheet

            prepare_ladder_sheet(session, io_sheet, extra)
        for part in io_parts:
            part.sheet_override = io_sheet

    _assign_refs(session, parts, sheet)
    from panelcad.core.motor_apply import planned_rating_lines, reserve_rating_gutter

    reserve_rating_gutter(
        session,
        sheet,
        power_parts,
        lambda part: planned_rating_lines(part, motor_props, sizing),
    )
    from panelcad.core.spacing import distribute_power_columns

    column_warnings = distribute_power_columns(session, sheet, power_parts)
    staying = [part for part in power_parts if not getattr(part, "sheet_override", "")]
    fit_warnings = list(column_warnings)
    fit_warnings.extend(_fit_with_overflow(session, sheet, staying))
    for dest_id in sorted({part.sheet_override for part in power_parts if part.sheet_override}):
        fit_warnings.extend(
            _fit_with_overflow(
                session,
                dest_id,
                [part for part in power_parts if part.sheet_override == dest_id],
            )
        )
    if control_sheet and control_parts:
        from panelcad.core.circuit_extra import arrange_control_ladder

        arrange_control_ladder(session, control_sheet, control_parts, extra)
    if io_sheet and io_parts:
        from panelcad.core.circuit_extra import arrange_control_ladder

        arrange_control_ladder(session, io_sheet, io_parts, extra, key="_io_rungs")

    motor_ref = next((part.ref for part in parts if part.role == "motor" and part.ref), "")
    for part in parts:
        dest = part.sheet_override or (
            control_sheet if part.page == "control" and control_sheet else sheet
        )
        props: dict[str, Any] = {"role": part.role}
        if part.skip_layout:
            # Elementary coil and aux contacts are the same device as the power
            # symbol. They must not add a second DIN footprint to the cabinet.
            props["in_panel"] = False
            props["incabinet"] = False
            props["mount"] = "none"
            props["din_rail"] = False
        if motor_ref:
            props["sizing_group"] = motor_ref
        if part.role == "motor":
            props.update(motor_props)
        if part.family:
            parent = next((other for other in parts if other.role == part.family and not other.family), None)
            if parent is not None:
                props["parent"] = parent.ref
        if part.properties:
            props.update(part.properties)
        session.place(
            part.library_id,
            ref=part.ref,
            x=part.x,
            y=part.y,
            sheet_id=dest,
            properties=props,
            snap=False,
            scale=part.scale,
            rotation=int(part.rotation or 0),
        )

    wires = _wire(
        session,
        sheet,
        parts,
        symbols,
        kind=kind_s,
        spec=spec,
        phases=phases,
        branch=branch_s,
        control_sheet=control_sheet,
        extra=extra,
    )
    from panelcad.core.motor_apply import stamp_inserted_circuit

    stamp_inserted_circuit(session, sizing, motor_ref, wires, parts)
    _unstack_placed(session, sheet)
    _clamp_placed(session, sheet)
    if kind_s == "vfd" and control_s == "local":
        _repark_vfd_operators(session, sheet)
    if kind_s in EXTRA_KINDS or kind_s == "vfd":
        from panelcad.core.circuit_extra import finish_power_routes

        finish_power_routes(session, sheet, parts, kind_s)
    layout = _pack_layouts(session, parts, kind=kind_s, place_layout=_as_bool(place_layout))
    if fit_warnings:
        layout.setdefault("warnings", []).extend(fit_warnings)
    if share_warnings:
        layout.setdefault("warnings", []).extend(share_warnings)
    load_warning = _supply_load_warning(session, extra)
    if load_warning:
        layout.setdefault("warnings", []).append(load_warning)
    if extra.get("shared_supply"):
        layout["shared_supply"] = extra["shared_supply"]
    if extra.get("shared_estop"):
        layout["shared_estop"] = extra["shared_estop"]
    result = {
        "kind": kind_s,
        "sheet": sheet,
        "standard": symbols.standard,
        "components": [
            {
                "id": part.ref,
                "library_id": part.library_id,
                "role": part.role,
                "sheet": part.sheet_override
                or (control_sheet if part.page == "control" and control_sheet else sheet),
            }
            for part in parts
        ],
        "wires": wires,
        "layout": layout,
        "sizing": {key: value for key, value in sizing.items() if key != "citations"},
    }
    if control_sheet:
        result["control_sheet"] = control_sheet
    if io_sheet:
        result["io_sheet"] = io_sheet
    if motor_props.get("FLA") is not None:
        result["fla"] = motor_props["FLA"]
    if kind_s == "star_delta":
        result["timer_seconds"] = timer_v
        if place_s == "line":
            result["overload_placement"] = "line"
            result["overload_note"] = (
                "Overload is in the line (NEMA alternative). Set to 1.0 × nameplate FLA."
            )
        else:
            result["overload_placement"] = "delta_windings"
            result["overload_note"] = (
                "Overload is in the motor windings (after the main contactor). "
                "Set to 0.58 × line FLA (winding current)."
            )
    if kind_s == "plc_motor":
        result["hoa"] = hoa_b
        result["plc_model"] = extra["plc_model"]
        result["io"] = {
            "run": io_run_s,
            "ol": io_ol_s,
            "auto": io_auto_s,
            "out": io_out_s,
        }
    if extra.get("shared_supply"):
        result["shared_supply"] = extra["shared_supply"]
    if extra.get("shared_estop"):
        result["shared_estop"] = extra["shared_estop"]
    # Session, JSONL, and the CLI all read this. Layout warnings used to be
    # nested only, so a second insert that missed the sheet reported None.
    result["warnings"] = list((layout or {}).get("warnings") or [])
    from panelcad.core.spacing import page_fullness

    seen: list[str] = []
    density: list[dict[str, Any]] = []
    for part in parts:
        sid = part.sheet_override or (
            control_sheet if part.page == "control" and control_sheet else sheet
        )
        if not sid or sid in seen:
            continue
        seen.append(sid)
        try:
            row = page_fullness(session.sheet(sid), session.catalog)
        except (KeyError, AttributeError):
            continue
        density.append(row)
    result["density"] = density
    return result


def _drive_error(drive: Any, kind: str) -> ValueError:
    """Missing and unknown drives name the drives Circuit Builder actually has."""

    valid = ", ".join(DRIVES)
    if _drive_key(drive) is None:
        return ValueError(f"missing drive: kind={kind} needs a drive. Valid drives: {valid}")
    return ValueError(f"unknown drive '{drive}'. Valid drives: {valid}")


_COIL_ROLES = frozenset(
    {"contactor", "fwd", "rev", "main", "star", "delta", "bypass", "relay", "timer", "softstarter"}
)
_COIL_W_ESTIMATE = 7.0


def _existing_control(session: Any, roles: set[str]) -> Any | None:
    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        for comp in sheet.components:
            if str(comp.properties.get("role") or "") in roles:
                return comp
    return None


def _standin(comp: Any, role: str) -> _Part:
    return _Part(
        library_id=comp.library_id,
        role=role,
        x=float(comp.x),
        y=float(comp.y),
        ref=comp.id,
        reuse=True,
        properties=dict(comp.properties),
    )


def _pin_on(session: Any, part: _Part, *candidates: str) -> str:
    """A pin this device actually has. Prefer the caller's name, then the usual NC pair."""

    try:
        device = session.catalog.get(part.library_id)
    except KeyError:
        return candidates[0] if candidates else ""
    ids = {pin.id for pin in device.pins}
    for pin in candidates:
        if pin and pin in ids:
            return pin
    return next(iter(ids)) if ids else (candidates[0] if candidates else "")


def _strip_rung_roles(extra: dict[str, Any], roles: set[str]) -> None:
    for key in ("_rungs", "_io_rungs"):
        for rung in extra.get(key) or []:
            contacts = rung.get("contacts")
            if contacts:
                rung["contacts"] = [item for item in contacts if getattr(item, "role", "") not in roles]


def _share_panel_controls(
    session: Any,
    parts: list[_Part],
    extra: dict[str, Any],
    symbols: SymbolSet,
    *,
    shared_supply: bool,
    shared_estop: bool,
) -> list[str]:
    """Drop a second supply or E-stop when the panel already has one."""

    warnings: list[str] = []
    if shared_supply:
        existing = _existing_control(session, {"psu", "transformer"})
        if existing is not None and any(part.role in {"psu", "transformer"} for part in parts):
            role = str(existing.properties.get("role") or "psu")
            standin = _standin(existing, role)
            extra["supply_part"] = standin
            extra["shared_supply"] = standin.ref
            parts[:] = [part for part in parts if part.role not in {"psu", "transformer"}]
            _strip_rung_roles(extra, {"psu", "transformer"})
            ctrl = extra.get("control_voltage")
            wanted = "transformer" if ctrl is not None and float(ctrl) >= 100 else "psu"
            if role != wanted and ctrl is not None:
                warnings.append(
                    f"Reusing control supply {standin.ref} ({role}); this circuit asked for {wanted}."
                )
    if shared_estop:
        existing = _existing_control(session, {"estop"})
        if existing is not None and any(part.role == "estop" for part in parts):
            standin = _standin(existing, "estop")
            extra["estop_part"] = standin
            extra["shared_estop"] = standin.ref
            parts[:] = [part for part in parts if part.role != "estop"]
            for key in ("_rungs", "_io_rungs"):
                for rung in extra.get(key) or []:
                    contacts = list(rung.get("contacts") or [])
                    if not any(getattr(item, "role", "") == "estop" for item in contacts):
                        continue
                    rung["contacts"] = [item for item in contacts if getattr(item, "role", "") != "estop"]
                    if not rung.get("tap"):
                        rung["tap"] = standin
                        rung["tap_out"] = _pin_on(session, standin, symbols.pins.estop_out, "12", "2")
    return warnings


def _supply_capacity_w(session: Any, part: _Part) -> float | None:
    import re

    try:
        device = session.catalog.get(part.library_id)
    except KeyError:
        return None
    props = {**device.properties, **(part.properties or {})}
    schematic = _schematic_component(session, part.ref) if part.ref else None
    if schematic is not None:
        props.update(schematic.properties)

    def _num(value: Any) -> float | None:
        if value is None or value == "":
            return None
        match = re.search(r"([0-9]+(?:[.][0-9]+)?)", str(value))
        return float(match.group(1)) if match else None

    volts = _num(props.get("Vout") or props.get("V"))
    amps = _num(props.get("Iout") or props.get("current"))
    if volts and amps:
        return volts * amps
    for key in ("VA", "va", "rating_va"):
        va = _num(props.get(key))
        if va:
            return va
    return None


def _coil_count(session: Any) -> int:
    seen: set[str] = set()
    for sheet in session.project.sheets:
        if sheet.kind not in {"schematic", "io"}:
            continue
        for comp in sheet.components:
            role = str(comp.properties.get("role") or "")
            if role in _COIL_ROLES and not str(comp.properties.get("parent") or "").strip():
                seen.add(comp.id)
    return len(seen)


def supply_load_warning(capacity_w: float, coil_count: int, ref: str) -> str | None:
    """Warn when estimated contactor-coil load exceeds 80% of the supply.

    ``7 W`` per coil is an estimate, not a catalog value.
    """

    load = coil_count * _COIL_W_ESTIMATE
    if capacity_w <= 0 or load <= 0.8 * capacity_w:
        return None
    return (
        f"Estimated coil load {load:.0f} W ({coil_count} coils × {_COIL_W_ESTIMATE:.0f} W, estimate) "
        f"is over 80% of {ref} ({capacity_w:.0f} W)."
    )


def _supply_load_warning(session: Any, extra: dict[str, Any]) -> str | None:
    supply = extra.get("supply_part")
    if supply is None:
        found = _existing_control(session, {"psu", "transformer"})
        if found is None:
            return None
        supply = _standin(found, str(found.properties.get("role") or "psu"))
    capacity = _supply_capacity_w(session, supply)
    if not capacity:
        return None
    return supply_load_warning(capacity, _coil_count(session), supply.ref)


def _drive_key(drive: Any) -> str | None:
    if drive is None:
        return None
    text = str(drive).strip()
    if text.lower() in {"", "none", "null"}:
        return None
    return text.lower()


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _rating_kw(kind: str, spec: dict[str, Any] | None, kw: float | None, hp: float | None) -> float:
    if kw is not None:
        return kw
    if hp is not None:
        return hp * HP_TO_KW
    if kind == "dol" or spec is None:
        return 1.5
    if spec.get("kw") is not None:
        return float(spec["kw"])
    return float(spec["hp"]) * HP_TO_KW


def _motor_props(
    kind: str,
    spec: dict[str, Any] | None,
    kw: float | None,
    hp: float | None,
    voltage: float | None = None,
    fla: float | None = None,
    standard: str = "",
    phases: int = 3,
) -> dict[str, Any]:
    props: dict[str, Any] = {"phases": int(phases)}
    if kw is not None:
        props["kW"] = kw
    elif hp is None and (kind == "dol" or spec is None or spec.get("kw") is not None):
        props["kW"] = 1.5 if kind == "dol" or spec is None else float(spec["kw"])
    if hp is not None:
        props["hp"] = hp
    elif kw is None and spec is not None and spec.get("hp") is not None:
        props["hp"] = float(spec["hp"])
    if voltage is not None:
        props["voltage"] = voltage
    elif standard == "IEC":
        props["voltage"] = 400
    else:
        props["voltage"] = 480
    if fla is not None:
        props["FLA"] = fla
        props["nameplate_fla"] = fla
        props["fla_source"] = "nameplate"
        return props
    if str(standard or "").upper().startswith("IEC"):
        return props
    from panelcad.core.motor_sizing import motor_flc, nearest_table_hp

    hp_v = props.get("hp")
    if hp_v is None and props.get("kW") is not None:
        hp_v = float(props["kW"]) / HP_TO_KW
    if hp_v is None:
        return props
    volts = float(props["voltage"])
    looked = motor_flc(hp=float(hp_v), voltage=volts, phases=int(phases))
    if looked is None:
        neighbor = nearest_table_hp(float(hp_v), phases=int(phases))
        if neighbor is not None:
            looked = motor_flc(hp=neighbor, voltage=volts, phases=int(phases))
    if looked is not None:
        props["FLA"] = looked[2]
        props["table_flc"] = looked[2]
        props["fla_source"] = "nec-table"
    return props


def _resolve_drive(spec: dict[str, Any], symbols: SymbolSet) -> dict[str, Any]:
    """Swap generic IEC drives onto nema.* twins. OEM catalog ids stay put."""

    resolved = dict(spec)
    lib = str(spec.get("library_id") or "")
    if symbols.standard == "NEMA/ANSI" and lib.startswith("iec."):
        resolved["library_id"] = "nema." + lib.split(".", 1)[1]
    return resolved


def _target_sheet(session: Any, sheet_id: str | None, new_sheet: bool) -> str:
    raw = str(sheet_id).strip() if sheet_id is not None else ""
    if new_sheet:
        return _add_motor_sheet(session, raw or None)
    if raw:
        session.set_active_sheet(raw)
        return session.active_sheet_id
    current = session.sheet()
    if current.kind == "schematic":
        return current.id
    return _add_motor_sheet(session, None)


def _add_motor_sheet(session: Any, preferred: str | None) -> str:
    from panelcad.api.session import _slug

    base = _slug(preferred) if preferred else "motor-power"
    sid = base
    n = 2
    while any(sheet.id == sid for sheet in session.project.sheets):
        sid = f"{base}-{n}"
        n += 1
    sheet = session.add_sheet(sid, title="Motor power", kind="schematic")
    sheet.description = "Motor power circuit"
    return sheet.id


def _control_target_sheet(session: Any, *, style: str, hot: str, ret: str) -> str:
    """Reuse a Motor control sheet only when its style and rails already match.

    A NEMA ladder must not rewrite an IEC column sheet. The wires on L+ would
    be left pointing at a rail that no longer exists.
    """

    for sheet in session.project.sheets:
        if sheet.kind != "schematic" or "control" not in (sheet.title or "").lower():
            continue
        current_style = str(getattr(sheet, "drawing_style", "") or "")
        current_hot = str(getattr(sheet, "rail_hot", "") or "")
        current_ret = str(getattr(sheet, "rail_return", "") or "")
        unset = not current_style and not current_hot and not current_ret
        if unset or (current_style == style and current_hot == hot and current_ret == ret):
            return sheet.id
    return _add_named_sheet(session, "motor-control", "Motor control", "Motor control circuit")


def _add_named_sheet(session: Any, base: str, title: str, description: str) -> str:
    from panelcad.api.session import _slug

    sid = _slug(base)
    n = 2
    while any(sheet.id == sid for sheet in session.project.sheets):
        sid = f"{base}-{n}"
        n += 1
    sheet = session.add_sheet(sid, title=title, kind="schematic", make_active=False)
    sheet.description = description
    return sheet.id


def _assign_refs(session: Any, parts: list[_Part], sheet_id: str) -> None:
    """Unique tags per device; coil/contact children share the parent's tag."""

    used = _used_refs(session)
    families: dict[str, str] = {}
    for part in parts:
        if part.family:
            continue
        part.ref = _next_project_ref(session, part.library_id, used, sheet_id)
        used.add(part.ref)
        families[part.role] = part.ref
    for part in parts:
        if not part.family:
            continue
        parent_ref = families.get(part.family)
        if not parent_ref:
            part.ref = _next_project_ref(session, part.library_id, used, sheet_id)
            used.add(part.ref)
            continue
        part.ref = f"{parent_ref}{part.suffix}" if part.suffix else parent_ref
        used.add(part.ref)


def _wrap_overflow_x(session: Any, sheet_id: str, parts: list[_Part]) -> None:
    """Drop symbols that still stick past the right border onto the next row."""

    if not parts:
        return
    wx, _wy, ww, _wh = working_rect(session.sheet(sheet_id))
    max_x = wx + ww
    origin_x = max(min(part.x for part in parts), wx + WORKING_PAD)
    for _ in range(16):
        moved = False
        for part in sorted(parts, key=lambda item: (item.y, item.x)):
            if _ink_rect(session, part)[2] <= max_x + 0.05:
                continue
            others = [other for other in parts if other is not part]
            part.x = origin_x
            if others:
                part.y = max(_ink_rect(session, other)[3] for other in others) + GAP
            moved = True
        if not moved:
            return
    _spread_parts(session, parts)


def _fit_with_overflow(session: Any, sheet_id: str, parts: list[_Part]) -> list[str]:
    """Fit inside the border; a second column, then another sheet, if still tall."""

    if not parts:
        return []
    _wrap_overflow_x(session, sheet_id, parts)
    warnings = _fit_in_working_rect(session, sheet_id, parts)
    if not any("does not fit" in w for w in warnings):
        return warnings
    if _split_second_column(session, parts):
        warnings = _fit_in_working_rect(session, sheet_id, parts)
        if not any("does not fit" in w for w in warnings):
            return warnings
    overflow = _spill_to_next_sheet(session, sheet_id, parts)
    if overflow:
        more = _fit_in_working_rect(
            session, overflow, [p for p in parts if p.sheet_override == overflow]
        )
        warnings = [w for w in warnings if "does not fit" not in w]
        warnings.extend(more)
        warnings.append(f"continued on sheet {overflow}")
    return warnings


def _split_second_column(session: Any, parts: list[_Part]) -> bool:
    stack = sorted((p for p in parts if p.role in _STACK_ROLES), key=lambda p: (p.y, p.x))
    if len(stack) < 4:
        return False
    mid = len(stack) // 2
    left, right = stack[:mid], stack[mid:]
    left_right = max(_ink_rect(session, part)[2] for part in left)
    shift = left_right + CLEAR - min(part.x for part in right)
    if shift < 1.0:
        shift = 40.0
    top = left[0].y
    dy = top - right[0].y
    for part in right:
        part.x += shift
        part.y += dy
    return True


def _spill_to_next_sheet(session: Any, sheet_id: str, parts: list[_Part]) -> str | None:
    stack = sorted((p for p in parts if p.role in _STACK_ROLES), key=lambda p: (p.y, p.x))
    if len(stack) < 3:
        return None
    extra = _add_named_sheet(
        session,
        f"{sheet_id}-cont",
        "Motor power (continued)",
        "Motor power overflow",
    )
    mid = len(stack) // 2
    origin_y = stack[0].y
    origin_x = stack[0].x
    dy = origin_y - stack[mid].y
    dx = origin_x - stack[mid].x
    for part in stack[mid:]:
        part.x += dx
        part.y += dy
        part.sheet_override = extra
    return extra


def _schematic_home(session: Any, pin_ref: str) -> str | None:
    """Schematic or I/O sheet that holds this pin's device. Rails return None."""

    comp_id = str(pin_ref).split(".", 1)[0].strip()
    if not comp_id:
        return None
    hits = [
        sheet.id
        for sheet in session.project.sheets
        if sheet.kind in {"schematic", "io"} and any(comp.id == comp_id for comp in sheet.components)
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def _used_refs(session: Any) -> set[str]:
    return {comp.id for sheet in session.project.sheets for comp in sheet.components}


def _next_project_ref(session: Any, library_id: str, used: set[str], sheet_id: str | None = None) -> str:
    """Same tag on schematic and layout; never reuse a ref that is already on any sheet."""

    from panelcad.core.refs import next_tag

    device = session.catalog.get(library_id)
    prefix = resolve_ref_prefix(
        device,
        standard=resolve_circuit_standard(session, sheet_id),
        maps=getattr(session.catalog, "ref_prefix_maps", None),
        overrides=getattr(session.project, "ref_prefixes", None),
    )
    return next_tag(prefix, used)


def _ensure_kind_sheet(session: Any, kind: str, base: str, title: str) -> str:
    existing = next((sheet.id for sheet in session.project.sheets if sheet.kind == kind), None)
    if existing:
        return existing
    from panelcad.api.session import _slug

    sid = _slug(base)
    n = 2
    while any(sheet.id == sid for sheet in session.project.sheets):
        sid = f"{base}-{n}"
        n += 1
    session.add_sheet(sid, title=title, kind=kind, make_active=False)
    return sid


def _schematic_component(session: Any, ref: str) -> Any:
    for sheet in session.project.sheets:
        if sheet.kind not in ("schematic", "io"):
            continue
        try:
            return sheet.component(ref)
        except KeyError:
            continue
    return None


def _layout_ghost(session: Any, part: _Part) -> Any:
    from panelcad.core.models import Component

    sch = _schematic_component(session, part.ref)
    props = dict(sch.properties) if sch is not None else {"role": part.role}
    return Component(
        id=part.ref,
        library_id=part.library_id,
        x=0.0,
        y=0.0,
        scale=1.0,
        properties=props,
    )



def _outside_component_ids(session: Any, sheet_id: str) -> list[str]:
    """Placed in-cabinet parts whose body hangs outside the drawn enclosure."""

    from panelcad.core.layout import footprint_size
    from panelcad.library.panel import effective_footprint, is_incabinet

    sheet = session.sheet(sheet_id)
    enc = sheet.enclosure
    if enc is None:
        return []
    outside: list[str] = []
    scale = float(enc.draw_scale or 1.0) or 1.0
    right = enc.x + enc.width_mm * scale
    bottom = enc.y + enc.height_mm * scale
    for comp in sheet.components:
        device = session.catalog.devices.get(comp.library_id)
        if device is None:
            continue
        if not is_incabinet(device, comp) and effective_footprint(device, comp).mount in {"none", "field"}:
            continue
        width, height = footprint_size(device, comp)
        if width <= 0 and height <= 0:
            continue
        if (
            comp.x + 0.05 < enc.x
            or comp.y + 0.05 < enc.y
            or comp.x + width - 0.05 > right
            or comp.y + height - 0.05 > bottom
        ):
            outside.append(comp.id)
    return outside


def _devices_outside_enclosure(session: Any, sheet_id: str) -> bool:
    """True when a placed in-cabinet part hangs outside the backpanel outline."""

    return bool(_outside_component_ids(session, sheet_id))


def _remember_panel_unplaced(
    session: Any, sheet_id: str, refs: list[str], owners: list[str] | None = None
) -> None:
    """Remember in-cabinet tags the chosen box could not take, for Problems.

    ``owners`` are the schematic tags of every circuit still waiting. Problems drops the entry once
    none of them is left in the project, so deleting the circuit clears it.
    """

    stored = dict(session.project.properties.get("panel_unplaced") or {})
    if refs:
        if owners:
            prior = stored.get(sheet_id)
            earlier = list(prior.get("owners") or []) if isinstance(prior, dict) else []
            stored[sheet_id] = {"refs": list(refs), "owners": list(dict.fromkeys([*earlier, *owners]))}
        else:
            stored[sheet_id] = list(refs)
    else:
        stored.pop(sheet_id, None)
    if stored:
        session.project.properties["panel_unplaced"] = stored
    else:
        session.project.properties.pop("panel_unplaced", None)


def _kept_enclosure_warning(session: Any, sheet_id: str) -> str:
    from panelcad.core.panels import find_panel, panel_tag

    sheet = session.sheet(sheet_id)
    panel = find_panel(session.project, sheet.panel)
    tag = panel_tag(panel) if panel is not None else sheet.id
    name = ""
    if sheet.enclosure is not None:
        name = str(sheet.enclosure.catalog or sheet.enclosure.name or "").strip()
    if not name and panel is not None:
        name = str(panel.enclosure_id or "").strip()
    which = f" {name}" if name else ""
    return (
        f"{tag} keeps enclosure{which}. The circuit does not fit. "
        "Run Size Enclosure to choose a larger box."
    )


def _pack_layouts(session: Any, parts: list[_Part], *, kind: str, place_layout: bool = True) -> dict[str, Any]:
    """Add this circuit to panel and door layouts without wiping hand-placed parts.

    A pack that leaves the drawing frame, or lands on the title block, is redrawn
    in the catalog box at a standard scale so every part stays on the sheet.
    """

    from panelcad.core.circuit_layout import pack_circuit_layouts

    result = pack_circuit_layouts(session, parts, kind=kind, place_layout=place_layout)
    if not result.get("placed"):
        return result
    panel_id = result.get("sheet")
    if not panel_id:
        return result
    sheet_ids = [panel_id, result.get("door")]
    if result.get("enclosure_chosen"):
        # Keep the catalog box. Draw it at the same page scale as Size
        # Enclosure, and lay the circuit out in that outline. Drop a fit
        # warning only when every in-cabinet part landed inside the box.
        # A part that was never placed is not inside.
        from panelcad.core.circuit_layout import backplate_fit_refs

        rec = session.recommend_enclosure()
        if rec.get("enclosure"):
            written = session._write_enclosure_recommendation(
                rec, sheet_id=panel_id, place_parts=True, keep_size=True
            )
            result["draw_scale"] = written.get("draw_scale")
        sheet = session.sheet(panel_id)
        on_sheet = {comp.id for comp in sheet.components}
        outside_ids = set(_outside_component_ids(session, panel_id))
        missing: list[str] = []
        for line in result.get("warnings") or []:
            for ref in backplate_fit_refs(str(line)):
                if ref in on_sheet and ref not in outside_ids:
                    continue
                if ref not in missing:
                    missing.append(ref)
        for ref in outside_ids:
            if ref not in missing:
                missing.append(ref)
        if missing:
            kept: list[str] = []
            for line in result.get("warnings") or []:
                text = str(line)
                if "not fit" not in text:
                    kept.append(text)
                    continue
                refs = backplate_fit_refs(text)
                if refs and all(ref in on_sheet and ref not in outside_ids for ref in refs):
                    continue
                kept.append(text)
            listed: set[str] = set()
            for line in kept:
                listed.update(backplate_fit_refs(line))
            absent = [ref for ref in missing if ref not in on_sheet and ref not in listed]
            if absent:
                names = ", ".join(absent)
                verb = "does" if len(absent) == 1 else "do"
                kept.append(
                    f"{names} {verb} not fit in the free backplate; "
                    "recommend_enclosure can size a larger box"
                )
            note = _kept_enclosure_warning(session, panel_id)
            if note not in kept:
                kept.append(note)
            result["warnings"] = kept
            _remember_panel_unplaced(
                session, panel_id, missing, [str(part.ref) for part in parts if part.ref and not part.reuse]
            )
        else:
            result["warnings"] = [
                line for line in (result.get("warnings") or []) if "not fit" not in str(line)
            ]
            _remember_panel_unplaced(session, panel_id, [])
        result["enclosure_kept"] = True
        return result
    misses = any(sheet_id and _sheet_misses_frame(session, sheet_id) for sheet_id in sheet_ids)
    omitted = any("not fit" in str(line) for line in (result.get("warnings") or []))
    shrunk = any(sheet_id and _sheet_has_shrunk_device(session, sheet_id) for sheet_id in sheet_ids)
    if result.get("preexisting"):
        # A hand-placed part (a tightened box, a device dropped on the rail)
        # stays where it is. A panel that only holds earlier circuits can be
        # redrawn so the new devices land inside the backplate.
        if not omitted or not _layouts_are_circuit_owned(session, sheet_ids):
            return result
    elif not misses and not omitted and not shrunk:
        # A first insert used to refit only when a part left the frame.
        # pack_field can also shrink a block (a soft starter at 0.2) so it
        # sits inside the default box and only warns. That is still a miss.
        return result
    rec = session.recommend_enclosure()
    if not rec.get("enclosure"):
        return result
    written = session._write_enclosure_recommendation(rec, sheet_id=panel_id, place_parts=True)
    kept = [line for line in (result.get("warnings") or []) if "not fit" not in str(line)]
    kept.extend(written.get("warnings") or [])
    result["warnings"] = kept
    result["fitted_enclosure"] = (rec.get("enclosure") or {}).get("id")
    result["draw_scale"] = written.get("draw_scale")
    return result


def _layouts_are_circuit_owned(session: Any, sheet_ids: list[Any]) -> bool:
    """True when every layout component was placed by Circuit Builder.

    A tag with no layout band is something the user put on the sheet. Resizing
    must not move it.
    """

    from panelcad.core.enclosure import is_generated_rail_stop

    for sheet_id in sheet_ids:
        if not sheet_id:
            continue
        sheet = session.sheet(sheet_id)
        for comp in sheet.components:
            props = comp.properties or {}
            if (
                props.get("layout_band")
                or props.get("layout_circuit")
                or props.get("layout_furniture")
                or props.get("layout_insert")
            ):
                continue
            if is_generated_rail_stop(comp):
                continue
            return False
    return True


def _sheet_has_shrunk_device(session: Any, sheet_id: str) -> bool:
    """True when a placed device is drawn smaller than the enclosure scale.

    Furniture and generated rail stops follow the sheet on purpose. A field
    block that pack_field drew at 0.2 does not.
    """

    from panelcad.core.enclosure import is_generated_rail_stop
    from panelcad.core.layout import _draw_scale

    sheet = session.sheet(sheet_id)
    draw = _draw_scale(sheet.enclosure) if sheet.enclosure is not None else 1.0
    for comp in sheet.components:
        props = comp.properties or {}
        if props.get("layout_furniture") or is_generated_rail_stop(comp):
            continue
        scale = float(comp.scale or 1.0)
        if scale < draw * 0.99:
            return True
    return False


def _sheet_misses_frame(session: Any, sheet_id: str) -> bool:
    """True when a part, rail, or duct leaves the frame or covers the title block."""

    from panelcad.core.layout import footprint_size, rail_aabb, rects_overlap, tray_aabb
    from panelcad.render.titleblock import title_block_rect, working_rect

    sheet = session.sheet(sheet_id)
    fx, fy, fw, fh = working_rect(sheet)
    tx, ty, tw, th = title_block_rect(sheet)

    def inside(x: float, y: float, width: float, height: float) -> bool:
        if width <= 0 or height <= 0:
            return True
        if x < fx - 0.6 or y < fy - 0.6 or x + width > fx + fw + 0.6 or y + height > fy + fh + 0.6:
            return False
        return not rects_overlap(x, y, width, height, tx, ty, tw, th)

    for comp in sheet.components:
        try:
            device = session.catalog.get(comp.library_id)
        except KeyError:
            continue
        width, height = footprint_size(device, comp)
        if not inside(comp.x, comp.y, width, height):
            return True
    for rail in sheet.rails:
        if not inside(*rail_aabb(rail)):
            return True
    for tray in sheet.trays:
        if not inside(*tray_aabb(tray)):
            return True
    if sheet.enclosure is not None:
        from panelcad.core.layout import enclosure_drawn_size

        dw, dh, _scale = enclosure_drawn_size(sheet.enclosure)
        if not inside(float(sheet.enclosure.x), float(sheet.enclosure.y), dw, dh):
            return True
    return False


def _device(session: Any, library_id: str) -> Any:
    return session.catalog.get(library_id)


def _pin_x(device: Any, pin_id: str) -> float:
    return float(device.pin(pin_id).x)


def _pin_y(device: Any, pin_id: str) -> float:
    return float(device.pin(pin_id).y)


def _size(device: Any) -> tuple[float, float]:
    return float(device.schematic_width), float(device.schematic_height)


def _layout(
    session: Any,
    symbols: SymbolSet,
    *,
    kind: str,
    spec: dict[str, Any] | None,
    phases: int,
    branch: str,
    line_contactor: bool,
    control: str,
    rating_kw: float,
    x: float,
    y: float,
    extra: dict[str, Any] | None = None,
) -> list[_Part]:
    if kind in EXTRA_KINDS:
        from panelcad.core.circuit_extra import layout_extra

        return layout_extra(
            session,
            symbols,
            kind=kind,
            spec=spec,
            phases=phases,
            branch=branch,
            line_contactor=line_contactor,
            control=control,
            rating_kw=rating_kw,
            x=x,
            y=y,
            extra=extra or {},
        )
    parts: list[_Part] = []
    anchor = x
    cursor_y = y
    pins = symbols.pins

    if phases == 1 and branch == "breaker":
        device = _device(session, symbols.mcb_1p)
        width, height = _size(device)
        pin_x = _pin_x(device, pins.branch_align)
        parts.append(_Part(symbols.mcb_1p, "breaker", x, y, align_pin=pins.branch_align))
        parts.append(_Part(symbols.mcb_1p, "breaker", x + width + GAP, y, align_pin=pins.branch_align))
        anchor = x + pin_x
        cursor_y = y + height + GAP
    else:
        library_id = symbols.branch(phases, branch, rating_kw)
        device = _device(session, library_id)
        _width, height = _size(device)
        align = symbols.branch_align(branch)
        pin_x = _pin_x(device, align)
        role = "disconnect" if branch == "disconnect" else "breaker"
        parts.append(_Part(library_id, role, x, y, align_pin=align))
        anchor = x + pin_x
        cursor_y = y + height + GAP

    def add_below(library_id: str, role: str, align_pin: str) -> _Part:
        nonlocal cursor_y
        device = _device(session, library_id)
        _width, height = _size(device)
        part = _Part(library_id, role, anchor - _pin_x(device, align_pin), cursor_y, align_pin=align_pin)
        parts.append(part)
        cursor_y += height + GAP
        return part

    want_contactor = kind == "dol" or line_contactor
    if want_contactor:
        add_below(symbols.contactor(rating_kw), "contactor", "L1")
    if kind == "dol":
        add_below(symbols.overload, "overload", pins.ol_align)
    elif spec is not None:
        add_below(str(spec["library_id"]), "drive", str(spec["line"][0]))
    add_below(symbols.motor, "motor", pins.motor_align)

    if control == "local":
        power = list(parts)
        if kind == "dol":
            from panelcad.core.circuit_extra import dol_control_parts

            parts.extend(dol_control_parts(symbols, extra if extra is not None else {}))
        elif spec is not None:
            drive = next(part for part in power if part.role == "drive")
            parts.extend(_vfd_controls(session, drive, spec, symbols))
    return parts


def _dol_controls(symbols: SymbolSet) -> list[tuple[str, str, str]]:
    pins = symbols.pins
    return [
        (symbols.psu, "psu", pins.psu_plus),
        (symbols.estop, "estop", pins.estop_in),
        (symbols.pb_nc, "stop", pins.stop_in),
        (symbols.pb_no, "start", pins.start_in),
    ]


def _control_row(session: Any, power: list[_Part], symbols: list[tuple[str, str, str]]) -> list[_Part]:
    """Left-to-right rung. Contact pins share a world y; bodies stay beside the power stack."""

    power_top = min(part.y for part in power)
    rung_y = power_top
    for library_id, _role, pin_id in symbols:
        rung_y = max(rung_y, power_top + _pin_y(_device(session, library_id), pin_id))
    right = max(part.x + _size(_device(session, part.library_id))[0] for part in power)
    cursor_x = right + CLEAR
    row: list[_Part] = []
    for library_id, role, pin_id in symbols:
        device = _device(session, library_id)
        width, _height = _size(device)
        row.append(
            _Part(library_id, role, cursor_x, rung_y - _pin_y(device, pin_id), align_pin=pin_id)
        )
        cursor_x += width + GAP
    return row


def _vfd_controls(
    session: Any, drive: _Part, spec: dict[str, Any], symbols: SymbolSet
) -> list[_Part]:
    """Dry contact to the right of the drive: common, e-stop, stop, start, run."""

    device = _device(session, drive.library_id)
    width, _height = _size(device)
    common_y = drive.y + _pin_y(device, str(spec["common"]))
    pins = symbols.pins
    operators = [
        (symbols.estop, "estop", pins.estop_in),
        (symbols.pb_nc, "stop", pins.stop_in),
        (symbols.pb_no, "start", pins.start_in),
    ]
    cursor_x = drive.x + width + CLEAR
    row: list[_Part] = []
    for library_id, role, pin_id in operators:
        symbol = _device(session, library_id)
        sym_w, _sym_h = _size(symbol)
        row.append(
            _Part(library_id, role, cursor_x, common_y - _pin_y(symbol, pin_id), align_pin=pin_id)
        )
        cursor_x += sym_w + GAP
    return row


def _repark_vfd_operators(session: Any, sheet_id: str) -> None:
    """Keep the dry-contact string to the right of the drive, on the common pin.

    Column fitting can slide the drive into the operator row. A wire from a
    symbol on the left of the drive into the right-side COM/DI pins then runs
    through the drive body. Operators stay on this sheet; the run string is
    not a 24 V coil circuit.
    """

    from panelcad.render.sheet import pin_world

    sheet = session.sheet(sheet_id)
    order = {"estop": 0, "stop": 1, "start": 2}
    ops = [c for c in sheet.components if str(c.properties.get("role") or "") in order]
    power = [c for c in sheet.components if str(c.properties.get("role") or "") in _STACK_ROLES]
    if not ops or not power:
        return
    ops.sort(key=lambda comp: order[str(comp.properties.get("role"))])
    drive = next((comp for comp in power if comp.properties.get("role") == "drive"), None)
    target_y = None
    if drive is not None:
        estop = ops[0]
        for wire in sheet.wires:
            ends = (wire.frm, wire.to)
            drive_end = next((end for end in ends if end.startswith(drive.id + ".")), "")
            estop_end = next((end for end in ends if end.startswith(estop.id + ".")), "")
            if not drive_end or not estop_end:
                continue
            pin_id = drive_end.split(".", 1)[1]
            device = session.catalog.get(drive.library_id)
            target_y = pin_world(drive, device.pin(pin_id))[1]
            break
    wx, _wy, ww, _wh = working_rect(sheet)
    max_x = wx + ww
    cursor = max(_comp_body(session, comp)[2] for comp in power) + CLEAR
    placed: list[tuple[Any, float, float]] = []
    below = False
    for comp in ops:
        device = session.catalog.get(comp.library_id)
        pin = next((item for item in device.pins if item.side == "left"), device.pins[0])
        width, _height = _size(device)
        width *= comp.visual_scale()
        if target_y is None:
            target_y = pin_world(comp, pin)[1]
        new_y = target_y - float(pin.y) * comp.visual_scale()
        if cursor + width > max_x - WORKING_PAD:
            below = True
            break
        placed.append((comp, cursor, new_y))
        cursor += width + GAP
    if below or len(placed) != len(ops):
        floor = max(_comp_body(session, comp)[3] for comp in power) + CLEAR
        cursor = wx + WORKING_PAD
        placed = []
        for comp in ops:
            device = session.catalog.get(comp.library_id)
            width, _height = _size(device)
            width *= comp.visual_scale()
            placed.append((comp, cursor, floor))
            cursor += width + GAP
    for comp, new_x, new_y in placed:
        comp.x = new_x
        comp.y = new_y
    _unstack_placed(session, sheet_id)


def _rect(session: Any, part: _Part) -> tuple[float, float, float, float]:
    device = _device(session, part.library_id)
    width, height = _size(device)
    return component_aabb(part.x, part.y, width, height)


def _ink_rect(session: Any, part: _Part) -> tuple[float, float, float, float]:
    from panelcad.core.models import Component

    device = _device(session, part.library_id)
    ghost = Component(
        id=part.ref or "MTR1",
        library_id=part.library_id,
        x=part.x,
        y=part.y,
        properties=dict(part.properties),
    )
    return schematic_ink_rect(device, part.x, part.y, part.ref, component=ghost)


def _hits(a: tuple[float, float, float, float], b: tuple[float, float, float, float], pad: float = 0.4) -> bool:
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _comp_body(session: Any, comp: Any) -> tuple[float, float, float, float]:
    return component_aabb(
        comp.x,
        comp.y,
        *_size(_device(session, comp.library_id)),
        comp.rotation,
        comp.mirror,
    )


def _comp_ink(session: Any, comp: Any) -> tuple[float, float, float, float]:
    from panelcad.core.models import Component

    device = _device(session, comp.library_id)
    ghost = comp if isinstance(comp, Component) else comp
    return schematic_ink_rect(device, ghost.x, ghost.y, ghost.id, component=ghost)


def _unstack_placed(session: Any, sheet_id: str) -> None:
    """Slide overlapping placed symbols right, or down if the row is full."""

    sheet = session.sheet(sheet_id)
    wx, wy, ww, _wh = working_rect(sheet)
    max_x = wx + ww
    for _ in range(32):
        moved = False
        comps = list(sheet.components)
        for i, a in enumerate(comps):
            ra = _comp_body(session, a)
            for b in comps[i + 1 :]:
                rb = _comp_body(session, b)
                if ra[2] <= rb[0] or rb[2] <= ra[0] or ra[3] <= rb[1] or rb[3] <= ra[1]:
                    continue
                # Rotation can grow left of x (90° OL contact). Slide the body, not the origin.
                need = ra[2] + GAP - rb[0]
                if need <= 0:
                    continue
                if rb[2] + need > max_x - WORKING_PAD:
                    b.x = min(a.x, wx + WORKING_PAD)
                    b.y = ra[3] + GAP
                else:
                    b.x += need
                moved = True
        if not moved:
            break
    _clamp_placed(session, sheet_id)


def _clamp_placed(session: Any, sheet_id: str) -> None:
    """Keep placed ink inside the drawing area after rotation / unstack."""

    sheet = session.sheet(sheet_id)
    comps = list(sheet.components)
    if not comps:
        return
    wx, wy, ww, wh = working_rect(sheet)
    max_x, max_y = wx + ww, wy + wh

    def bounds() -> tuple[float, float, float, float]:
        rects = [_comp_ink(session, comp) for comp in comps]
        return (
            min(r[0] for r in rects),
            min(r[1] for r in rects),
            max(r[2] for r in rects),
            max(r[3] for r in rects),
        )

    left, top, right, bottom = bounds()
    dx = 0.0
    if right > max_x:
        dx -= min(right - max_x, max(0.0, left - wx))
    if left + dx < wx:
        dx += wx - (left + dx)
    dy = 0.0
    if bottom > max_y:
        dy -= min(bottom - max_y, max(0.0, top - wy))
    if top + dy < wy:
        dy += wy - (top + dy)
    if dx or dy:
        for comp in comps:
            comp.x += dx
            comp.y += dy

    for _ in range(24):
        overflow = [comp for comp in comps if _comp_ink(session, comp)[2] > max_x + 0.05]
        if not overflow:
            break
        victim = max(overflow, key=lambda comp: _comp_ink(session, comp)[2])
        others = [comp for comp in comps if comp is not victim]
        victim.x = wx + WORKING_PAD
        if others:
            victim.y = max(_comp_ink(session, other)[3] for other in others) + GAP

    left, top, right, bottom = bounds()
    if bottom > max_y + 0.05 and top > wy + 0.05:
        for comp in comps:
            comp.y -= min(bottom - max_y, top - wy)


def _spread_parts(session: Any, parts: list[_Part]) -> None:
    """Push later symbols down when two control (or column) bodies still overlap."""

    ordered = sorted(parts, key=lambda part: (part.y, part.x))
    for _ in range(24):
        moved = False
        for i, part in enumerate(ordered):
            for other in ordered[i + 1 :]:
                if not _hits(_rect(session, part), _rect(session, other), pad=0.6):
                    continue
                other.x = _rect(session, part)[2] + GAP
                moved = True
        if not moved:
            return


def _park_control(session: Any, parts: list[_Part]) -> None:
    """Slide operators right if a wide power symbol still cuts the rung."""

    power_roles = {
        "breaker",
        "disconnect",
        "contactor",
        "overload",
        "drive",
        "motor",
        "fwd",
        "rev",
        "main",
        "star",
        "delta",
        "softstarter",
        "bypass",
    }
    power = [part for part in parts if part.role in power_roles]
    control = [part for part in parts if part.role not in power_roles]
    for part in control:
        for _ in range(16):
            rect = _rect(session, part)
            blockers = [
                other
                for other in power + control
                if other is not part and _hits(rect, _rect(session, other))
            ]
            if not blockers:
                break
            part.x = max(_rect(session, other)[2] for other in blockers) + CLEAR


def _clear_existing(session: Any, sheet_id: str, parts: list[_Part]) -> None:
    sheet = session.sheet(sheet_id)
    existing: list[tuple[float, float, float, float]] = []
    for comp in sheet.components:
        device = _device(session, comp.library_id)
        width, height = _size(device)
        existing.append(component_aabb(comp.x, comp.y, width, height, comp.rotation, comp.mirror))
    if not existing:
        return
    for _ in range(24):
        push = 0.0
        for part in parts:
            rect = _rect(session, part)
            for ex in existing:
                if _hits(rect, ex, pad=0.8):
                    push = max(push, ex[2] - rect[0] + CLEAR)
        if push <= 0.0:
            return
        for part in parts:
            part.x += push


_STACK_ROLES = frozenset(
    {
        "breaker",
        "disconnect",
        "contactor",
        "overload",
        "drive",
        "motor",
        "fwd",
        "rev",
        "main",
        "star",
        "delta",
        "softstarter",
        "bypass",
    }
)


def _bounds(session: Any, parts: list[_Part]) -> tuple[float, float, float, float]:
    rects = [_ink_rect(session, part) for part in parts]
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def _shift_parts(parts: list[_Part], dx: float, dy: float) -> None:
    if not dx and not dy:
        return
    for part in parts:
        part.x += dx
        part.y += dy


def _fit_in_working_rect(
    session: Any, sheet_id: str, parts: list[_Part], pad: float = WORKING_PAD
) -> list[str]:
    """Keep the whole insert — tags, labels, stubs — inside the drawing area.

    Close inter-symbol gaps before sliding the column so a default DOL stack
    does not push the breaker tag into the zone strip. If the stack is still
    taller than the sheet, tighten further, then scale, then warn. There is
    no second-column / off-page overflow path on this inserter.
    """

    warnings: list[str] = []
    if not parts:
        return warnings
    wx, wy, ww, wh = working_rect(session.sheet(sheet_id))
    # Soft inset first; the working_rect edge is the hard limit (do not enter the
    # zone strip or title block). Eat the pad before closing gaps below MIN_GAP.
    soft_min_x, soft_min_y = wx + pad, wy + pad
    soft_max_x, soft_max_y = wx + ww - pad, wy + wh - pad
    hard_min_x, hard_min_y = wx, wy
    hard_max_x, hard_max_y = wx + ww, wy + wh

    left, top, right, bottom = _bounds(session, parts)
    dx = 0.0
    if right > soft_max_x:
        dx -= min(right - soft_max_x, max(0.0, left - soft_min_x))
    if left + dx < soft_min_x:
        dx += soft_min_x - (left + dx)
    if left + dx < hard_min_x:
        dx += hard_min_x - (left + dx)
    _shift_parts(parts, dx, 0.0)

    top = _bounds(session, parts)[1]
    if top < soft_min_y:
        _shift_parts(parts, 0.0, soft_min_y - top)
    top = _bounds(session, parts)[1]
    if top < hard_min_y:
        _shift_parts(parts, 0.0, hard_min_y - top)

    leftover = _bounds(session, parts)[3] - soft_max_y
    if leftover > 1e-6:
        _compress_stack(session, parts, leftover, min_gap=MIN_GAP)

    left, top, right, bottom = _bounds(session, parts)
    if bottom > soft_max_y + 1e-6:
        headroom = max(0.0, top - soft_min_y)
        _shift_parts(parts, 0.0, -min(bottom - soft_max_y, headroom))

    leftover = _bounds(session, parts)[3] - hard_max_y
    if leftover > 1e-6:
        _compress_stack(session, parts, leftover, min_gap=MIN_GAP)

    left, top, right, bottom = _bounds(session, parts)
    if bottom > hard_max_y + 1e-6:
        headroom = max(0.0, top - hard_min_y)
        _shift_parts(parts, 0.0, -min(bottom - hard_max_y, headroom))

    leftover = _bounds(session, parts)[3] - hard_max_y
    if leftover > 1e-6:
        _compress_stack(session, parts, leftover, min_gap=TIGHT_GAP)

    leftover = _bounds(session, parts)[3] - hard_max_y
    scaled = False
    if leftover > 1e-6:
        before = [part.y for part in parts]
        _scale_stack(session, parts, hard_max_y)
        scaled = any(abs(part.y - old) > 1e-6 for part, old in zip(parts, before, strict=True))

    _left, top, _right, bottom = _bounds(session, parts)
    if bottom > hard_max_y + 1e-6 or top < hard_min_y - 1e-6:
        warnings.append(
            "power column does not fit in the drawing area; tags or line stubs overlap the border"
        )
    elif scaled:
        warnings.append("power column is taller than the drawing area; spacing was tightened to fit")
    return warnings


def _compress_stack(
    session: Any, parts: list[_Part], extra: float, min_gap: float = MIN_GAP
) -> None:
    """Pull later stack symbols up by stealing from inter-symbol body gaps."""

    stack = sorted((p for p in parts if p.role in _STACK_ROLES), key=lambda p: (p.y, p.x))
    if len(stack) < 2 or extra <= 1e-6:
        return
    stolen = 0.0
    for i in range(1, len(stack)):
        need = extra - stolen
        if need <= 1e-6:
            break
        prev_bottom = _rect(session, stack[i - 1])[3]
        this_top = _rect(session, stack[i])[1]
        take = min(need, max(0.0, this_top - prev_bottom - min_gap))
        if take <= 1e-9:
            continue
        for later in stack[i:]:
            later.y -= take
        stolen += take


def _scale_stack(session: Any, parts: list[_Part], max_y: float) -> None:
    """Scale stack positions toward the top symbol. Symbol heights do not shrink."""

    stack = sorted((p for p in parts if p.role in _STACK_ROLES), key=lambda p: (p.y, p.x))
    if len(stack) < 2:
        return
    origin = stack[0].y
    lowest = max(stack, key=lambda part: _ink_rect(session, part)[3])
    ink_below = _ink_rect(session, lowest)[3] - lowest.y
    span = lowest.y - origin
    avail = max_y - origin - ink_below
    if span <= avail or span <= 1e-9 or avail <= 1e-9:
        return
    scale = avail / span
    for part in stack:
        part.y = origin + (part.y - origin) * scale


def _by_role(parts: list[_Part], role: str) -> list[_Part]:
    return [part for part in parts if part.role == role]


def _one(parts: list[_Part], role: str) -> _Part:
    found = _by_role(parts, role)
    if not found:
        raise ValueError(f"circuit is missing {role}")
    return found[0]


def _wire(
    session: Any,
    sheet: str,
    parts: list[_Part],
    symbols: SymbolSet,
    *,
    kind: str,
    spec: dict[str, Any] | None,
    phases: int,
    branch: str,
    control_sheet: str | None = None,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    wires: list[dict[str, str]] = []
    pins = symbols.pins

    def _part_home(pin_ref: str, sheet_id: str | None = None) -> str | None:
        """Sheet this insert actually placed the device on.

        The same tag can be a power symbol and a coil. A spilled column is
        the one with sheet_override. A rail name has no component.
        """

        if "." not in str(pin_ref):
            return None
        ref = str(pin_ref).split(".", 1)[0]
        matches = [part for part in parts if part.ref == ref]
        if not matches:
            return _schematic_home(session, pin_ref)

        def _home(part: _Part) -> str:
            if part.sheet_override:
                return str(part.sheet_override)
            if part.page == "control" and control_sheet:
                return control_sheet
            if part.page == "io" and part.sheet_override:
                return str(part.sheet_override)
            return sheet

        if len(matches) == 1:
            return _home(matches[0])
        if sheet_id:
            for part in matches:
                if _home(part) == sheet_id:
                    return sheet_id
        overridden = [part for part in matches if part.sheet_override]
        if len(overridden) == 1:
            return _home(overridden[0])
        return _schematic_home(session, pin_ref)

    def connect(
        frm: str,
        to: str,
        net: str,
        *,
        sheet_id: str | None = None,
        to_sheet: str | None = None,
    ) -> None:
        dest = sheet_id or sheet
        # Wire on the sheet that holds the symbol. A continuation column must
        # not be wired back on the page it spilled from.
        if not to_sheet:
            home_from = _part_home(frm, sheet_id)
            home_to = _part_home(to, sheet_id)
            if home_from and home_to and home_from != home_to:
                dest = home_from
                to_sheet = home_to
            elif home_from and home_from == home_to:
                dest = home_from
            elif home_from and not home_to:
                dest = home_from
            elif home_to and not home_from:
                dest = home_to
        if to_sheet and to_sheet != dest:
            item = session.wire(frm, to, net=net, sheet_id=dest, to_sheet=to_sheet)
        else:
            item = session.wire(frm, to, net=net, sheet_id=dest)
        if isinstance(item, dict):
            wires.append(
                {
                    "from": str(item.get("from") or frm),
                    "to": str(item.get("to") or to),
                    "net": str(item.get("net") or net),
                    "sheet": dest,
                }
            )
            return
        wires.append({"from": item.frm, "to": item.to, "net": item.net or net, "sheet": dest})

    def end(part: _Part, pin: str) -> str:
        return f"{part.ref}.{pin}"

    if kind in EXTRA_KINDS:
        from panelcad.core.circuit_extra import wire_extra

        wire_extra(
            session,
            sheet,
            parts,
            symbols,
            kind=kind,
            spec=spec,
            phases=phases,
            branch=branch,
            connect=connect,
            end=end,
            control_sheet=control_sheet,
            extra=extra or {},
        )
        return wires

    if kind == "dol":
        _wire_dol(parts, branch, pins, connect, end)
        bag = extra or {}
        if bag.get("_rungs") and control_sheet:
            from panelcad.core.circuit_extra import _wire_control_rungs

            _wire_control_rungs(session, parts, symbols, connect, end, control_sheet, bag)
        else:
            supply = bag.get("supply_part")
            estop = bag.get("estop_part")
            if _by_role(parts, "psu") or supply is not None:
                _wire_dol_control(
                    parts, pins, connect, end, supply=supply, estop=estop, session=session
                )
    else:
        assert spec is not None
        _wire_vfd(parts, spec, phases, branch, pins, connect, end)
        estop = (extra or {}).get("estop_part")
        if _by_role(parts, "estop") or estop is not None:
            _wire_vfd_control(parts, spec, pins, connect, end, estop=estop, session=session)
    return wires


def _wire_dol(parts: list[_Part], branch: str, pins: PinScheme, connect: Any, end: Any) -> None:
    branch_part = _one(parts, "disconnect" if branch == "disconnect" else "breaker")
    contactor = _one(parts, "contactor")
    overload = _one(parts, "overload")
    motor = _one(parts, "motor")
    outs = pins.disc_out if branch == "disconnect" else pins.branch_out
    # 3-phase DOL matches examples/dol.py on IEC: Q.2/4/6 → K.L1/L2/L3, K.T → F, F → M.
    for src, dst, net in zip(outs, _CONTACTOR_IN, _LINE_NETS, strict=True):
        connect(end(branch_part, src), end(contactor, dst), net)
    for src, dst, net in zip(_CONTACTOR_OUT, pins.ol_in, _LOAD_NETS, strict=True):
        connect(end(contactor, src), end(overload, dst), net)
    for src, dst, net in zip(pins.ol_out, pins.motor, pins.motor_nets, strict=True):
        connect(end(overload, src), end(motor, dst), net)


def _wire_vfd(
    parts: list[_Part],
    spec: dict[str, Any],
    phases: int,
    branch: str,
    pins: PinScheme,
    connect: Any,
    end: Any,
) -> None:
    drive = _one(parts, "drive")
    motor = _one(parts, "motor")
    line_pins = tuple(spec["line"])
    contactor = _by_role(parts, "contactor")
    upstream = contactor[0] if contactor else drive
    if phases == 1 and branch == "breaker":
        breakers = _by_role(parts, "breaker")
        _wire_two_poles(breakers, upstream, line_pins, connect, end, into_contactor=bool(contactor))
    elif phases == 1:
        disc = _one(parts, "disconnect")
        bottoms = pins.disc_1ph_out
        if contactor:
            for src, dst, pin in zip(bottoms, ("L1", "L2"), line_pins, strict=True):
                connect(end(disc, src), end(contactor[0], dst), pin)
        else:
            for src, pin in zip(bottoms, line_pins, strict=True):
                connect(end(disc, src), end(drive, pin), pin)
    else:
        branch_part = _one(parts, "disconnect" if branch == "disconnect" else "breaker")
        outs = pins.disc_out if branch == "disconnect" else pins.branch_out
        if contactor:
            for src, dst, net in zip(outs, _CONTACTOR_IN, _LINE_NETS, strict=True):
                connect(end(branch_part, src), end(contactor[0], dst), net)
        else:
            for src, pin, net in zip(outs, line_pins, _LINE_NETS, strict=True):
                connect(end(branch_part, src), end(drive, pin), net)
    if contactor:
        if phases == 1:
            for src, pin in zip(("T1", "T2"), line_pins, strict=True):
                connect(end(contactor[0], src), end(drive, pin), f"T{pin}")
        else:
            for src, pin, net in zip(_CONTACTOR_OUT, line_pins, _LOAD_NETS, strict=True):
                connect(end(contactor[0], src), end(drive, pin), net)
    for src, dst, net in zip(spec["motor"], pins.motor, _DRIVE_MOTOR_NETS, strict=True):
        connect(end(drive, src), end(motor, dst), net)


def _wire_two_poles(
    breakers: list[_Part],
    upstream: _Part,
    line_pins: tuple[str, ...],
    connect: Any,
    end: Any,
    *,
    into_contactor: bool,
) -> None:
    if len(breakers) < 2:
        raise ValueError("1-phase breaker branch needs two single-pole breakers")
    if into_contactor:
        destinations = ("L1", "L2")
        for breaker, dest, pin in zip(breakers, destinations, line_pins, strict=False):
            connect(end(breaker, "2"), end(upstream, dest), pin)
        return
    for breaker, pin in zip(breakers, line_pins, strict=False):
        connect(end(breaker, "2"), end(upstream, pin), pin)


def _pins_joined(session: Any, left: str, right: str) -> bool:
    ends = {left, right}
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            if {wire.frm, wire.to} == ends:
                return True
    return False


def _wire_dol_control(
    parts: list[_Part],
    pins: PinScheme,
    connect: Any,
    end: Any,
    *,
    supply: _Part | None = None,
    estop: _Part | None = None,
    session: Any = None,
) -> None:
    psu = _by_role(parts, "psu")
    psu_part = psu[0] if psu else supply
    estop_found = _by_role(parts, "estop")
    estop_part = estop_found[0] if estop_found else estop
    if psu_part is None or estop_part is None:
        raise ValueError("circuit is missing psu" if psu_part is None else "circuit is missing estop")
    stop = _one(parts, "stop")
    start = _one(parts, "start")
    contactor = _one(parts, "contactor")
    plus = end(psu_part, pins.psu_plus)
    estop_in = end(estop_part, pins.estop_in)
    if session is None or not _pins_joined(session, plus, estop_in):
        connect(plus, estop_in, "24V")
    estop_out = end(estop_part, pins.estop_out)
    stop_in = end(stop, pins.stop_in)
    if session is None or not _pins_joined(session, estop_out, stop_in):
        connect(estop_out, stop_in, "ES")
    connect(end(stop, pins.stop_out), end(start, pins.start_in), "STOP")
    connect(end(start, pins.start_out), end(contactor, "A1"), "START")
    connect(end(contactor, "A2"), end(psu_part, pins.psu_minus), "0V")


def _wire_vfd_control(
    parts: list[_Part],
    spec: dict[str, Any],
    pins: PinScheme,
    connect: Any,
    end: Any,
    *,
    estop: _Part | None = None,
    session: Any = None,
) -> None:
    drive = _one(parts, "drive")
    estop_found = _by_role(parts, "estop")
    estop_part = estop_found[0] if estop_found else estop
    if estop_part is None:
        raise ValueError("circuit is missing estop")
    stop = _one(parts, "stop")
    start = _one(parts, "start")
    common = str(spec["common"])
    run = str(spec["run"])
    drive_common = end(drive, common)
    estop_in = end(estop_part, pins.estop_in)
    if session is None or not _pins_joined(session, drive_common, estop_in):
        connect(drive_common, estop_in, common)
    estop_out = end(estop_part, pins.estop_out)
    stop_in = end(stop, pins.stop_in)
    if session is None or not _pins_joined(session, estop_out, stop_in):
        connect(estop_out, stop_in, "ES")
    connect(end(stop, pins.stop_out), end(start, pins.start_in), "STOP")
    connect(end(start, pins.start_out), end(drive, run), "RUN")
