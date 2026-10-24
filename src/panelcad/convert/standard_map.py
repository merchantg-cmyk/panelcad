"""One symbol and role table for IEC <-> NEMA conversion.

Each row in ``ENTRIES`` names two symbols that already exist in the PanelCAD
libraries. Pin ids are the terminals drawn on those symbols. Where the ids
differ, the pair is the same physical terminal (same side and the same local
coordinates).

Tag letters and role-based renames live in ``ROLE_TAG_TABLE`` alongside
``libraries/builtin/ref_prefixes.yaml``. Terminal tag rules (``-X1:3`` <->
``TB1-3``) live in ``TERMINAL_TAG_TABLE`` and wire-number format rules
(IEC potential/address vs. NEMA ladder line-reference or keep-as-is) live in
``WIRE_NUMBER_FORMAT_TABLE``.

Motor leads: the IEC symbol is marked U1/V1/W1 and, on the 6-lead symbol,
W2/U2/V2 (IEC 60034-8 designations already printed in
``libraries/builtin/power_terminals.yaml``). The NEMA symbol prints T1/T2/T3
and T6/T4/T5 at those same coordinates (``libraries/builtin/nema.yaml``).
That pairing is also ``PinScheme.motor`` / ``motor_end`` in
``circuit_builder``. T4 sits on U2, T5 on V2, T6 on W2 — the winding ends,
not T4→U2, T5→V2, T6→W2 in numeric order.

Control contact numbers (13-14, 11-12, 21-22, 15-18, 15-16 versus 3-4, 1-2,
1-3) are the terminals drawn on each symbol and the pairs in
``circuit_builder.PinScheme``. IEC 60617 uses those terminal numbers; the
NEMA/ANSI symbols in this repo use the shorter pin numbers. Nothing in this
table invents a symbol that is not already in the library.

Overload and PE are the only rows whose library ids differ. Circuit Builder
already treats ``nema.protection.overload`` and ``iec.protection.overload_9a``
as the same device. ``nema.terminals.pe`` and ``iec.terminals.pe_4`` are the
protective-earth terminal, both with pins A and B at the same coordinates.

Control rails are the pairs Circuit Builder already applies
(``circuit_iec.iec_rail_pair``, ``circuit_ladder.rail_pair``): 24 V is
IEC L+/0V or NEMA 24V/0V. AC control is X1/X2 in both standards, including
the PLC I/O sheet. X1/X2 is the control secondary, not supply phase L1.
The PLC pin stays M. The wire number is the control return. A sheet that
already says L1/L2 or +24V/0V keeps that NEMA wording on the way back.
"""

from __future__ import annotations

from typing import Any

# pins_nema_to_iec omits terminals whose id is already the same on both symbols.
# iec_only / nema_only are pins the other library does not draw. A wired pin
# in that set keeps the original symbol and is flagged unmapped.

ENTRIES: list[dict[str, Any]] = [
    {
        "id": "breakers.mcb_1p_16a",
        "nema": "nema.breakers.mcb_1p_16a",
        "iec": "iec.breakers.mcb_1p_16a",
    },
    {
        "id": "breakers.mcb_3p_32a",
        "nema": "nema.breakers.mcb_3p_32a",
        "iec": "iec.breakers.mcb_3p_32a",
    },
    {
        "id": "breakers.mccb_3p_100a",
        "nema": "nema.breakers.mccb_3p_100a",
        "iec": "iec.breakers.mccb_3p_100a",
    },
    {
        "id": "breakers.mpcb_3p",
        "nema": "nema.breakers.mpcb_3p",
        "iec": "iec.breakers.mpcb_3p",
        "iec_only": ["97", "98"],
        "note": "Shared power and 95/96 pins match. IEC adds the 97-98 NO pair.",
    },
    {
        "id": "contactors.ac3_32a",
        "nema": "nema.contactors.ac3_32a",
        "iec": "iec.contactors.ac3_32a",
    },
    {
        "id": "contactors.ac3_9a",
        "nema": "nema.contactors.ac3_9a",
        "iec": "iec.contactors.ac3_9a",
        "iec_only": ["13", "14"],
        "note": "NEMA power contactor has no built-in 13-14 aux. Aux is a separate symbol.",
    },
    {
        "id": "contactors.aux_nc",
        "nema": "nema.contactors.aux_nc",
        "iec": "iec.contactors.aux_nc",
        "pins_nema_to_iec": {"1": "21", "2": "22"},
    },
    {
        "id": "contactors.aux_no",
        "nema": "nema.contactors.aux_no",
        "iec": "iec.contactors.aux_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "contactors.coil",
        "nema": "nema.contactors.coil",
        "iec": "iec.contactors.coil",
    },
    {
        "id": "drives.softstarter",
        "nema": "nema.drives.softstarter",
        "iec": "iec.drives.softstarter",
    },
    {
        "id": "drives.vfd_1k5",
        "nema": "nema.drives.vfd_1k5",
        "iec": "iec.drives.vfd_1k5",
    },
    {
        "id": "loads.fan_24v",
        "nema": "nema.loads.fan_24v",
        "iec": "iec.loads.fan_24v",
    },
    {
        "id": "loads.motor_1ph",
        "nema": "nema.loads.motor_1ph",
        "iec": "iec.loads.motor_1ph",
        "pins_nema_to_iec": {"T1": "U1", "T2": "U2"},
        "note": "Single-phase leads as drawn. IEC symbol uses U1/U2 (IEC 60034-8).",
    },
    {
        "id": "loads.motor_3ph",
        "nema": "nema.loads.motor_3ph",
        "iec": "iec.loads.motor_3ph",
        "pins_nema_to_iec": {"T1": "U1", "T2": "V1", "T3": "W1"},
    },
    {
        "id": "loads.motor_3ph_6lead",
        "nema": "nema.loads.motor_3ph_6lead",
        "iec": "iec.loads.motor_3ph_6lead",
        "pins_nema_to_iec": {
            "T1": "U1",
            "T2": "V1",
            "T3": "W1",
            "T6": "W2",
            "T4": "U2",
            "T5": "V2",
        },
    },
    {
        "id": "operators.estop",
        "nema": "nema.operators.estop",
        "iec": "iec.operators.estop",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
        "iec_only": ["21", "22"],
    },
    {
        "id": "operators.estop_2nc",
        "nema": "nema.operators.estop_2nc",
        "iec": "iec.operators.estop_2nc",
    },
    {
        "id": "operators.float_nc",
        "nema": "nema.operators.float_nc",
        "iec": "iec.operators.float_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.float_no",
        "nema": "nema.operators.float_no",
        "iec": "iec.operators.float_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.flow_nc",
        "nema": "nema.operators.flow_nc",
        "iec": "iec.operators.flow_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.flow_no",
        "nema": "nema.operators.flow_no",
        "iec": "iec.operators.flow_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.foot_nc",
        "nema": "nema.operators.foot_nc",
        "iec": "iec.operators.foot_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.foot_no",
        "nema": "nema.operators.foot_no",
        "iec": "iec.operators.foot_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.horn",
        "nema": "nema.operators.horn",
        "iec": "iec.operators.horn",
    },
    {
        "id": "operators.level_nc",
        "nema": "nema.operators.level_nc",
        "iec": "iec.operators.level_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.level_no",
        "nema": "nema.operators.level_no",
        "iec": "iec.operators.level_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.limit_nc",
        "nema": "nema.operators.limit_nc",
        "iec": "iec.operators.limit_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.limit_no",
        "nema": "nema.operators.limit_no",
        "iec": "iec.operators.limit_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.pb_illum",
        "nema": "nema.operators.pb_illum",
        "iec": "iec.operators.pb_illum",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.pb_nc",
        "nema": "nema.operators.pb_nc",
        "iec": "iec.operators.pb_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.pb_no",
        "nema": "nema.operators.pb_no",
        "iec": "iec.operators.pb_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.pilot_light",
        "nema": "nema.operators.pilot_light",
        "iec": "iec.operators.pilot_light",
    },
    {
        "id": "operators.pressure_nc",
        "nema": "nema.operators.pressure_nc",
        "iec": "iec.operators.pressure_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.pressure_no",
        "nema": "nema.operators.pressure_no",
        "iec": "iec.operators.pressure_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "operators.selector_3pos",
        "nema": "nema.operators.selector_3pos",
        "iec": "iec.operators.selector_3pos",
        "pins_nema_to_iec": {"1": "11", "2": "12", "3": "14"},
    },
    {
        "id": "operators.stack_light",
        "nema": "nema.operators.stack_light",
        "iec": "iec.operators.stack_light",
    },
    {
        "id": "operators.temperature_nc",
        "nema": "nema.operators.temperature_nc",
        "iec": "iec.operators.temperature_nc",
        "pins_nema_to_iec": {"1": "11", "2": "12"},
    },
    {
        "id": "operators.temperature_no",
        "nema": "nema.operators.temperature_no",
        "iec": "iec.operators.temperature_no",
        "pins_nema_to_iec": {"3": "13", "4": "14"},
    },
    {
        "id": "plc.analog_1ai_1ao",
        "nema": "nema.plc.analog_1ai_1ao",
        "iec": "iec.plc.analog_1ai_1ao",
    },
    {
        "id": "plc.compact_16dio",
        "nema": "nema.plc.compact_16dio",
        "iec": "iec.plc.compact_16dio",
    },
    {
        "id": "power.control_transformer",
        "nema": "nema.power.control_transformer",
        "iec": "iec.power.control_transformer",
        "iec_only": ["1", "2", "21", "22"],
        "note": "Both symbols draw H1/H2/X1/X2, which is what Circuit Builder wires. IEC also prints 1/2/21/22 on top of those.",
    },
    {
        "id": "power.psu_24v_5a",
        "nema": "nema.power.psu_24v_5a",
        "iec": "iec.power.psu_24v_5a",
    },
    {
        "id": "power.receptacle",
        "nema": "nema.power.receptacle",
        "iec": "iec.power.receptacle",
    },
    {
        "id": "protection.disconnect_3p",
        "nema": "nema.protection.disconnect_3p",
        "iec": "iec.protection.disconnect_3p",
        "pins_nema_to_iec": {"L1": "1", "L2": "3", "L3": "5", "T1": "2", "T2": "4", "T3": "6"},
    },
    {
        "id": "protection.fuse",
        "nema": "nema.protection.fuse",
        "iec": "iec.protection.fuse",
    },
    {
        "id": "protection.fuse_10a",
        "nema": "nema.protection.fuse_10a",
        "iec": "iec.protection.fuse_10a",
    },
    {
        "id": "protection.ol_contact",
        "nema": "nema.protection.ol_contact",
        "iec": "iec.protection.ol_contact",
    },
    {
        "id": "protection.overload",
        "nema": "nema.protection.overload",
        "iec": "iec.protection.overload_9a",
        "pins_nema_to_iec": {"T1": "1", "T2": "3", "T3": "5"},
        "iec_only": ["97", "98"],
        "note": "Line terminals T1/T2/T3 and 1/3/5 share coordinates. 2/4/6 and 95/96 already match. Circuit Builder SymbolSet pairs these two ids.",
    },
    {
        "id": "relays.alternating",
        "nema": "nema.relays.alternating",
        "iec": "iec.relays.alternating",
    },
    {
        "id": "relays.cr",
        "nema": "nema.relays.cr",
        "iec": "iec.relays.cr",
    },
    {
        "id": "relays.k_4co_24vdc",
        "nema": "nema.relays.k_4co_24vdc",
        "iec": "iec.relays.k_4co_24vdc",
    },
    {
        "id": "relays.safety_24vdc",
        "nema": "nema.relays.safety_24vdc",
        "iec": "iec.relays.safety_24vdc",
    },
    {
        "id": "sensors.prox_24v",
        "nema": "nema.sensors.prox_24v",
        "iec": "iec.sensors.prox_24v",
    },
    {
        "id": "terminals.feed_through",
        "nema": "nema.terminals.feed_through",
        "iec": "iec.terminals.feed_through",
    },
    {
        "id": "terminals.pe",
        "nema": "nema.terminals.pe",
        "iec": "iec.terminals.pe_4",
        "note": "Protective-earth terminal in each library. Pins A and B match.",
    },
    {
        "id": "terminals.x_2p5",
        "nema": "nema.terminals.x_2p5",
        "iec": "iec.terminals.x_2p5",
    },
    {
        "id": "timers.contact_nctc",
        "nema": "nema.timers.contact_nctc",
        "iec": "iec.timers.contact_nctc",
        "pins_nema_to_iec": {"1": "15", "2": "16"},
    },
    {
        "id": "timers.contact_ncto",
        "nema": "nema.timers.contact_ncto",
        "iec": "iec.timers.contact_ncto",
        "pins_nema_to_iec": {"1": "15", "2": "16"},
    },
    {
        "id": "timers.contact_notc",
        "nema": "nema.timers.contact_notc",
        "iec": "iec.timers.contact_notc",
        "pins_nema_to_iec": {"3": "15", "4": "18"},
    },
    {
        "id": "timers.contact_noto",
        "nema": "nema.timers.contact_noto",
        "iec": "iec.timers.contact_noto",
        "pins_nema_to_iec": {"3": "15", "4": "18"},
    },
    {
        "id": "timers.off_delay",
        "nema": "nema.timers.off_delay",
        "iec": "iec.timers.off_delay",
    },
    {
        "id": "timers.on_delay",
        "nema": "nema.timers.on_delay",
        "iec": "iec.timers.on_delay",
        "pins_nema_to_iec": {"1": "15", "3": "18"},
    },
]


# Role-based tag table for IEC <-> NEMA device designations.
# Disambiguates multi-role IEC letter codes (-K contactor/relay/timer,
# -Q breaker/disconnect/contactor, -F fuse/overload, -S pushbutton/selector/limit,
# -P/-H pilot light/horn, -T transformer, -M motor, -X terminal) without code branches.
ROLE_TAG_TABLE: list[dict[str, Any]] = [
    {
        "role": "contactor",
        "iec_prefix": "K",
        "iec_aliases": ("K", "KM", "Q"),
        "nema_prefix": "M",
        "nema_aliases": ("M", "CON"),
        "categories": ("switching.contactor", "switching.aux"),
        "role_tokens": (
            "contactor",
            "fwd",
            "rev",
            "main",
            "star",
            "delta",
            "starter_coil",
            "fwd_coil",
            "rev_coil",
            "main_coil",
            "star_coil",
            "delta_coil",
        ),
        "library_tokens": ("contactors.",),
    },
    {
        "role": "relay",
        "iec_prefix": "K",
        "iec_aliases": ("K", "KA", "KF"),
        "nema_prefix": "CR",
        "nema_aliases": ("CR",),
        "categories": ("switching.relay", "switching.safety"),
        "role_tokens": ("relay", "cr", "control_relay", "interposing_relay", "safety_relay"),
        "library_tokens": ("relays.cr", "relays.k_4co", "relays.safety"),
    },
    {
        "role": "alternating_relay",
        "iec_prefix": "KA",
        "iec_aliases": ("KA", "K"),
        "nema_prefix": "AR",
        "nema_aliases": ("AR",),
        "categories": ("switching.alternating",),
        "role_tokens": ("alternating", "alternating_relay"),
        "library_tokens": ("relays.alternating",),
    },
    {
        "role": "timer",
        "iec_prefix": "KT",
        "iec_aliases": ("KT", "K"),
        "nema_prefix": "TR",
        "nema_aliases": ("TR", "TD", "TDR"),
        "categories": ("timing.timer", "timing.contact"),
        "role_tokens": (
            "timer",
            "on_delay",
            "off_delay",
            "timer_coil",
            "timer_contact",
            "notc",
            "ncto",
            "noto",
            "nctc",
        ),
        "library_tokens": ("timers.",),
    },
    {
        "role": "breaker",
        "iec_prefix": "Q",
        "iec_aliases": ("Q", "F", "QF"),
        "nema_prefix": "CB",
        "nema_aliases": ("CB",),
        "categories": ("protection.breaker",),
        "role_tokens": ("breaker", "mcb", "mccb"),
        "library_tokens": ("breakers.mcb", "breakers.mccb"),
    },
    {
        "role": "mpcb",
        "iec_prefix": "Q",
        "iec_aliases": ("Q", "QM"),
        "nema_prefix": "MCP",
        "nema_aliases": ("MCP", "CB"),
        "categories": ("protection.mpcb",),
        "role_tokens": ("mpcb",),
        "library_tokens": ("breakers.mpcb",),
    },
    {
        "role": "disconnect",
        "iec_prefix": "Q",
        "iec_aliases": ("Q", "QS"),
        "nema_prefix": "DISC",
        "nema_aliases": ("DISC", "DS"),
        "categories": ("protection.disconnect",),
        "role_tokens": ("disconnect", "disc", "ds", "isolator"),
        "library_tokens": ("protection.disconnect",),
    },
    {
        "role": "fuse",
        "iec_prefix": "F",
        "iec_aliases": ("F", "FU"),
        "nema_prefix": "FU",
        "nema_aliases": ("FU",),
        "categories": ("protection.fuse",),
        "role_tokens": ("fuse", "fu"),
        "library_tokens": ("protection.fuse",),
    },
    {
        "role": "overload",
        "iec_prefix": "F",
        "iec_aliases": ("F", "FR"),
        "nema_prefix": "OL",
        "nema_aliases": ("OL",),
        "categories": ("protection.overload",),
        "role_tokens": ("overload", "ol", "ol_contact"),
        "library_tokens": ("protection.overload", "protection.ol_contact"),
    },
    {
        "role": "pushbutton",
        "iec_prefix": "S",
        "iec_aliases": ("S", "SB"),
        "nema_prefix": "PB",
        "nema_aliases": ("PB",),
        "categories": ("operator.pushbutton",),
        "role_tokens": ("pushbutton", "pb", "start", "stop", "fwd_pb", "rev_pb", "reset"),
        "library_tokens": ("operators.pb_",),
    },
    {
        "role": "selector",
        "iec_prefix": "S",
        "iec_aliases": ("S", "SA"),
        "nema_prefix": "SEL",
        "nema_aliases": ("SEL", "SS"),
        "categories": ("operator.selector",),
        "role_tokens": ("selector", "sel", "hoa", "hand_off_auto", "fwd_rev"),
        "library_tokens": ("operators.selector_",),
    },
    {
        "role": "limit_switch",
        "iec_prefix": "S",
        "iec_aliases": ("S", "SQ", "B"),
        "nema_prefix": "LS",
        "nema_aliases": ("LS",),
        "categories": ("operator.limit",),
        "role_tokens": ("limit", "limit_switch", "ls"),
        "library_tokens": ("operators.limit_",),
    },
    {
        "role": "estop",
        "iec_prefix": "S",
        "iec_aliases": ("S", "SF"),
        "nema_prefix": "ES",
        "nema_aliases": ("ES", "ESTOP"),
        "categories": ("operator.estop",),
        "role_tokens": ("estop", "emergency_stop", "mushroom"),
        "library_tokens": ("operators.estop",),
    },
    {
        "role": "float_switch",
        "iec_prefix": "SL",
        "iec_aliases": ("SL", "S", "B"),
        "nema_prefix": "FS",
        "nema_aliases": ("FS",),
        "categories": ("operator.float",),
        "role_tokens": ("float", "float_switch", "fs"),
        "library_tokens": ("operators.float_",),
    },
    {
        "role": "level_switch",
        "iec_prefix": "SL",
        "iec_aliases": ("SL", "S", "B"),
        "nema_prefix": "LEV",
        "nema_aliases": ("LEV", "LS"),
        "categories": ("operator.level",),
        "role_tokens": ("level", "level_switch", "lev"),
        "library_tokens": ("operators.level_",),
    },
    {
        "role": "pressure_switch",
        "iec_prefix": "SP",
        "iec_aliases": ("SP", "S", "B"),
        "nema_prefix": "PRS",
        "nema_aliases": ("PRS", "PS"),
        "categories": ("operator.pressure",),
        "role_tokens": ("pressure", "pressure_switch", "prs"),
        "library_tokens": ("operators.pressure_",),
    },
    {
        "role": "temperature_switch",
        "iec_prefix": "ST",
        "iec_aliases": ("ST", "S", "B"),
        "nema_prefix": "TSW",
        "nema_aliases": ("TSW", "TS"),
        "categories": ("operator.temperature",),
        "role_tokens": ("temperature", "temperature_switch", "thermostat", "tsw"),
        "library_tokens": ("operators.temperature_",),
    },
    {
        "role": "flow_switch",
        "iec_prefix": "SF",
        "iec_aliases": ("SF", "S", "B"),
        "nema_prefix": "FSW",
        "nema_aliases": ("FSW", "FS"),
        "categories": ("operator.flow",),
        "role_tokens": ("flow", "flow_switch", "fsw"),
        "library_tokens": ("operators.flow_",),
    },
    {
        "role": "foot_switch",
        "iec_prefix": "SF",
        "iec_aliases": ("SF", "S"),
        "nema_prefix": "FTS",
        "nema_aliases": ("FTS",),
        "categories": ("operator.foot",),
        "role_tokens": ("foot", "foot_switch", "fts"),
        "library_tokens": ("operators.foot_",),
    },
    {
        "role": "pilot_light",
        "iec_prefix": "H",
        "iec_aliases": ("H", "P", "HL"),
        "nema_prefix": "PL",
        "nema_aliases": ("PL", "LT"),
        "categories": ("operator.light",),
        "role_tokens": (
            "pilot",
            "pilot_light",
            "indicator",
            "light",
            "pl",
            "lt",
            "pilot_run",
            "pilot_fault",
            "pilot_fwd",
            "pilot_rev",
        ),
        "library_tokens": ("operators.pilot_light", "operators.stack_light"),
    },
    {
        "role": "horn",
        "iec_prefix": "HA",
        "iec_aliases": ("HA", "H", "P"),
        "nema_prefix": "HN",
        "nema_aliases": ("HN", "AH"),
        "categories": ("operator.horn",),
        "role_tokens": ("horn", "buzzer", "siren", "alarm_horn", "hn"),
        "library_tokens": ("operators.horn",),
    },
    {
        "role": "transformer",
        "iec_prefix": "T",
        "iec_aliases": ("T", "TC"),
        "nema_prefix": "T",
        "nema_aliases": ("T", "XFMR", "CPT"),
        "categories": ("power.transformer",),
        "role_tokens": ("transformer", "xfmr", "cpt", "control_transformer"),
        "library_tokens": ("power.control_transformer",),
    },
    {
        "role": "psu",
        "iec_prefix": "G",
        "iec_aliases": ("G", "T", "U"),
        "nema_prefix": "PS",
        "nema_aliases": ("PS", "PSU"),
        "categories": ("power.supply",),
        "role_tokens": ("psu", "power_supply", "ps"),
        "library_tokens": ("power.psu_",),
    },
    {
        "role": "receptacle",
        "iec_prefix": "X",
        "iec_aliases": ("X", "XS"),
        "nema_prefix": "REC",
        "nema_aliases": ("REC", "RCPT"),
        "categories": ("power.receptacle",),
        "role_tokens": ("receptacle", "outlet", "rec"),
        "library_tokens": ("power.receptacle",),
    },
    {
        "role": "motor",
        "iec_prefix": "M",
        "iec_aliases": ("M",),
        "nema_prefix": "MTR",
        "nema_aliases": ("MTR", "MOT"),
        "categories": ("load.motor",),
        "role_tokens": ("motor", "mtr"),
        "library_tokens": ("loads.motor_",),
    },
    {
        "role": "fan",
        "iec_prefix": "M",
        "iec_aliases": ("M", "FAN"),
        "nema_prefix": "FAN",
        "nema_aliases": ("FAN",),
        "categories": ("load.fan",),
        "role_tokens": ("fan",),
        "library_tokens": ("loads.fan_",),
    },
    {
        "role": "solenoid",
        "iec_prefix": "Y",
        "iec_aliases": ("Y", "YV"),
        "nema_prefix": "SOL",
        "nema_aliases": ("SOL", "SV"),
        "categories": ("load.solenoid",),
        "role_tokens": ("solenoid", "sol", "valve"),
        "library_tokens": ("solenoid",),
    },
    {
        "id": "vfd",
        "role": "vfd",
        "iec_prefix": "U",
        "iec_aliases": ("U", "G", "T"),
        "nema_prefix": "VFD",
        "nema_aliases": ("VFD", "DRV"),
        "categories": ("drive.vfd",),
        "role_tokens": ("drive", "vfd"),
        "library_tokens": ("drives.vfd", "durapulse"),
    },
    {
        "role": "softstarter",
        "iec_prefix": "U",
        "iec_aliases": ("U", "Q"),
        "nema_prefix": "SS",
        "nema_aliases": ("SS",),
        "categories": ("drive.softstarter", "switching.softstarter"),
        "role_tokens": ("softstarter", "ss", "ss_coil"),
        "library_tokens": ("drives.softstarter",),
    },
    {
        "role": "plc",
        "iec_prefix": "A",
        "iec_aliases": ("A", "K"),
        "nema_prefix": "PLC",
        "nema_aliases": ("PLC",),
        "categories": ("control.plc",),
        "role_tokens": ("plc",),
        "library_tokens": ("plc.", "click."),
    },
    {
        "role": "hmi",
        "iec_prefix": "P",
        "iec_aliases": ("P", "A"),
        "nema_prefix": "HMI",
        "nema_aliases": ("HMI",),
        "categories": ("control.hmi",),
        "role_tokens": ("hmi",),
        "library_tokens": ("hmi.",),
    },
    {
        "role": "prox_sensor",
        "iec_prefix": "B",
        "iec_aliases": ("B", "S", "PX"),
        "nema_prefix": "PX",
        "nema_aliases": ("PX", "PRS"),
        "categories": ("sensor.proximity",),
        "role_tokens": ("prox", "proximity", "prox_sensor", "px"),
        "library_tokens": ("sensors.prox",),
    },
    {
        "role": "ground_terminal",
        "iec_prefix": "X",
        "iec_aliases": ("X", "PE"),
        "nema_prefix": "GND",
        "nema_aliases": ("GND", "PE"),
        "categories": (
            "connection.terminal.pe",
            "connection.terminal.earth",
            "connection.terminal.ground",
            "connection.ground",
        ),
        "role_tokens": ("pe", "ground", "gnd", "pe_bar"),
        "library_tokens": ("terminals.pe", "connection.ground"),
    },
    {
        "role": "terminal",
        "iec_prefix": "X",
        "iec_aliases": ("X", "XT"),
        "nema_prefix": "TB",
        "nema_aliases": ("TB",),
        "categories": ("connection.terminal", "connection.bus"),
        "role_tokens": ("terminal", "tb"),
        "library_tokens": ("terminals.x_", "terminals.feed_through"),
    },
]


# Terminal strip/pin designation rules: IEC ``-X1:3`` / ``X1:3`` <-> NEMA ``TB1-3``.
TERMINAL_TAG_TABLE: dict[str, dict[str, Any]] = {
    "IEC": {
        "strip_prefix": "X",
        "strip_aliases": ("X", "XT"),
        "pin_separator": ":",
        "leading_dash_allowed": True,
        "example": "-X1:3",
    },
    "NEMA/ANSI": {
        "strip_prefix": "TB",
        "strip_aliases": ("TB",),
        "pin_separator": "-",
        "leading_dash_allowed": False,
        "example": "TB1-3",
    },
}


# Wire-number format rules: IEC potential/address style vs. NEMA ladder
# line-reference style (rung-number based) or kept as-is (user option).
WIRE_NUMBER_FORMAT_TABLE: dict[str, dict[str, Any]] = {
    "IEC": {
        "style": "potential_address",
        "rung_digits": 0,
        "seq_format": "{sheet}{col}.{seq}",
        "rail_aliases": {
            "24V": "L+",
            "+24V": "L+",
            "0V": "0V",
            "L1": "L1",
            "L2": "L2",
            "L3": "L3",
            "N": "N",
            "PE": "PE",
            "GND": "PE",
            "X1": "X1",
            "X2": "X2",
        },
    },
    "NEMA/ANSI": {
        "style": "line_ref",
        "rung_digits": 3,
        "rung_base_format": "{rung:03d}",
        "rung_branch_format": "{rung:03d}{branch}",
        "rail_aliases": {
            "L+": "24V",
            "24V": "24V",
            "+24V": "+24V",
            "M": "0V",
            "0V": "0V",
            "L1": "L1",
            "L2": "L2",
            "L3": "L3",
            "N": "N",
            "PE": "GND",
            "GND": "GND",
            "X1": "X1",
            "X2": "X2",
        },
    },
    "KEEP": {
        "style": "keep",
        "rung_digits": 0,
        "rail_aliases": {},
    },
}


# Circuit Builder rail pairs. Aliases are wording already used on sheets
# (title-block L1/L2, +24V/0V) so a conversion can round-trip that wording.
RAILS: dict[str, dict[str, tuple[str, str]]] = {
    "IEC": {
        "dc": ("L+", "0V"),
        "ac": ("X1", "X2"),
        "io": ("L+", "0V"),
    },
    "NEMA/ANSI": {
        "dc": ("24V", "0V"),
        "ac": ("X1", "X2"),
        "io": ("24V", "0V"),
    },
}

# NEMA wording to restore when the sheet used it before an IEC conversion.
NEMA_RAIL_ALIASES: dict[str, tuple[str, str]] = {
    "L1/L2": ("L1", "L2"),
    "+24V/0V": ("+24V", "0V"),
    "24V/0V": ("24V", "0V"),
    "X1/X2": ("X1", "X2"),
}
