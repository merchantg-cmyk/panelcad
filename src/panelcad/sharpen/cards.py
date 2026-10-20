"""Operator cards shown to the weak model.

Keep this file short. When Gemma 3 fails a task, change the cards or
the parser — not the CAD. That is the sharpening loop.
"""

from __future__ import annotations

from panelcad.cli.agent import OP_HELP

# Subset a 4B model can hold. Full OP_HELP is too large for the operator.
STARTER_OPS: tuple[str, ...] = (
    "help",
    "open",
    "ask_style",
    "list_sheets",
    "set_active_sheet",
    "inspect",
    "list_components",
    "list_wires",
    "inspect_device",
    "library_search",
    "place",
    "move",
    "set_properties",
    "wire",
    "cleanup",
    "validate",
    "get_title_block",
    "set_title_block",
    "save",
    "export_pdf",
)

CARDS: dict[str, str] = {
    name: (help if isinstance(help, str) else str(help))
    for name, help in OP_HELP.items()
}
CARDS.update({
    "help": "List allowed ops.",
    "open": "Open a drawing. Args: path.",
    "list_sheets": "Pages: id, title, kind, counts.",
    "set_active_sheet": "Args: sheet_id",
    "inspect": "Snapshot of one sheet. Args: sheet?",
    "list_components": "Devices and pin ids. Args: sheet?",
    "list_wires": "Wires on a sheet. Args: sheet?",
    "inspect_device": "Library pins. Args: library_id",
    "library_search": "Catalog lookup. Args: query, standard?. Returns library_id, name, standard, pins. standard is IEC or NEMA/ANSI.",
    "parts_search": "Manufacturer SKU lookup. Args: query?, manufacturer?, category?, library_id?, amps_min?, amps_max?. Returns catalog, verified, source, ratings, range. Do not invent catalog numbers.",
    "assign_part": "Assign a verified SKU to a placed tag. Args: ref, catalog, manufacturer?. Keeps library_id and wiring. Writes amps_min/amps_max. One undo.",
    "place": "Put a symbol on the sheet. Args: library_id, ref, x, y. Returns ref, pins, standard.",
    "place_from_bom": "Drop a BOM part onto the active sheet. Same library_id, same ref. Ladder/network/panel/io representations differ. Do not add a second library object.",
    "move": "Args: ref, x, y, sheet?",
    "set_properties": "Args: ref, manufacturer?, part_number?, sheet?",
    "wire": "Connect two pins. Args: from, to, net?. from/to = Ref.Pin.",
    "cleanup": "Unstack overlapping symbols.",
    "validate": "Drawing errors. Empty errors = ok.",
    "get_title_block": "Title-block fields.",
    "set_title_block": "Title-block fields. Args: drawing_number?, rev?, company?, standard?, drawing_style?, coil_style?",
    "update_cross_references": "Write coil/contact refs (properties.xref). Display is already live.",
    "set_cross_reference": "Args: rung_pitch_mm?=12.7 (0.5 in NEMA line pitch), nc_mark?, rung_numbers?, enabled?",
    "device_index": "Where every element of a device lives. Args: ref?",
    "lookup_zone": "What is at a page-zone. Args: zone, sheet?",
    "trace_wire": "Follow a wire number across sheets. Args: number.",
    "save": "Write the yaml. Args: path?",
    "export_pdf": "Args: path, sheet?",
    "create_project": "New drawing. Args: name, standard?, drawing_style?, coil_style?.",
    "ask_style": "Ask the operator schematic vs ladder vs freeform before drawing a print. No args. GUI shows example pictures. Skip panel_layout and network.",
    "add_sheet": "New page. Args: sheet_id, title?, kind?=schematic.",
    "set_active_sheet": "Draw on this page next. Args: sheet_id",
    "set_sheet_title": "Args: title, sheet?",
})

SKILL_RULES = """You operate PanelCAD. You do not write code. You do not click a GUI.
Reply with ONE JSON object and nothing else.
When the task is finished: {"op":"done","summary":"what you did"}
Follow the NEXT line exactly. One op per turn. Do not send ops that are not listed.
"""

RULES = """You operate PanelCAD. You do not write code. You do not click a GUI.
Reply with ONE JSON object and nothing else.
When the task is finished: {"op":"done","summary":"what you did"}
A done summary is a lie unless you actually sent those ops.
Follow the NEXT line exactly. One op per turn.
User drawing prefs live on the title block: standard, drawing_style, coil_style.
When you start a print (create_project or first schematic place), send ask_style.
Do not guess the print style. Wait for the operator. Panel layout and network pages ignore it.
Read prefs (create_project / get_title_block / ask_style). Honor them on schematic sheets.
standard=IEC → iec.* symbols. standard=NEMA/ANSI → nema.* symbols.
drawing_style=ladder → elementary diagram (your print type A): rails both sides, labels from rail_hot/rail_return (L1/L2, 120/N, 24VDC/0VDC). Rungs like PLC ladder. ANSI/NEMA symbols. from/to may be a rail name. 3-phase motor is type B, not on the control rung.
drawing_style=schematic → left-to-right signal flow.
drawing_style=freeform → place anywhere; do not force a rung.
Never wire coil A1/A2 or a pushbutton to motor T1/T2/T3. That mixes control with 3-phase power.
coil_style=square with NEMA/ANSI; circle with IEC.
IEC refs: Q breaker, K contactor/relay, A PLC, X terminal, S operator, H lamp, F fuse, M motor.
NEMA refs follow libraries/builtin/ref_prefixes.yaml (JIC/NFPA): CB breaker, FU fuse, DISC disconnect, M starter, OL overload, MTR motor, VFD drive, PB pushbutton, SEL selector, PL light, CR relay, TR timer, T transformer, PS supply, TB terminal, PLC, LS limit, PRS pressure, ES e-stop.
"""


def system_prompt(
    job_path: str,
    allowed: tuple[str, ...] | None = None,
    *,
    short: bool = False,
) -> str:
    ops = allowed or STARTER_OPS
    rules = SKILL_RULES if short or (allowed is not None and len(ops) <= 2) else RULES
    lines = [rules.strip(), "", f"Job path: {job_path}", "", "Allowed ops:"]
    for name in ops:
        lines.append(f"- {name}: {CARDS.get(name, name)}")
    lines.append('- done: {"op":"done","summary":"..."}  (harness only, ends the run)')
    return "\n".join(lines)
