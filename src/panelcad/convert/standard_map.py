"""One symbol table for IEC <-> NEMA conversion.

Each row names two symbols that already exist in the PanelCAD libraries.
Pin ids are the terminals drawn on those symbols. Where the ids differ, the
pair is the same physical terminal (same side and the same local coordinates).

Tag letters are not copied here. IEC letter codes stay on each device as
``ref_prefix`` (IEC 81346-2, as the IEC library already stores them). NEMA
prefixes stay in ``libraries/builtin/ref_prefixes.yaml`` (the NEMA tag-prefix
feature). The converter calls ``resolve_ref_prefix`` for both.

Motor leads: the IEC symbol is marked U1/V1/W1 and, on the 6-lead symbol,
W2/U2/V2 (IEC 60034-8 designations already printed in
``libraries/builtin/power_terminals.yaml``). The NEMA symbol prints T1/T2/T3
and T6/T4/T5 at those same coordinates (``libraries/builtin/nema.yaml``).
That pairing is also ``PinScheme.motor`` / ``motor_end`` in
``circuit_builder``. T4 sits on U2, T5 on V2, T6 on W2 — the winding ends,
not T4→U2, T5→V2, T6→W2 in numeric order.

Control contact numbers (13-14, 11-12, 21-22, 15-18 versus 3-4, 1-2, 1-3)
are the terminals drawn on each symbol and the pairs in
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
        "id": "protection.disconnect_3p",
        "nema": "nema.protection.disconnect_3p",
        "iec": "iec.protection.disconnect_3p",
        "pins_nema_to_iec": {"L1": "1", "L2": "3", "L3": "5", "T1": "2", "T2": "4", "T3": "6"},
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
        "id": "relays.k_4co_24vdc",
        "nema": "nema.relays.k_4co_24vdc",
        "iec": "iec.relays.k_4co_24vdc",
    },
    {
        "id": "terminals.pe",
        "nema": "nema.terminals.pe",
        "iec": "iec.terminals.pe_4",
        "note": "Protective-earth terminal in each library. Pins A and B match.",
    },
    {
        "id": "timers.on_delay",
        "nema": "nema.timers.on_delay",
        "iec": "iec.timers.on_delay",
        "pins_nema_to_iec": {"1": "15", "3": "18"},
    },
]


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
