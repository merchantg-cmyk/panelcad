"""JSON-lines protocol for AI subprocesses. One request per stdin line."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from panelcad.api.session import Session
from panelcad.core.circuit_builder import (
    INSERT_CIRCUIT_SCHEMA,
    _phase_count,
    insert_circuit_help,
    validate_insert_circuit_tool,
)
from panelcad.core.io_sheets import IO_ADD_MODULE_SCHEMA, IO_ASSIGN_POINT_SCHEMA, IO_GENERATE_SHEETS_SCHEMA
from panelcad.core.phase_color import PHASE_COLOR_SCHEMA


def _json_bool(value: Any, default: bool = False) -> bool:
    """JSONL flags. The string ``false`` is false; a missing value keeps the default."""

    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)
from panelcad.core.plc_tags import EXPORT_PLC_TAGS_SCHEMA, PLC_IO_LIST_SCHEMA, validate_export_plc_tags

SETTINGS_SCHEMA = {
    "op": "settings",
    "autosave_seconds": (
        "Optional. Seconds between recovery copies of unsaved work. 0 turns autosave off."
    ),
    "returns": ["autosave_seconds"],
    "notes": (
        "Omit autosave_seconds to read the interval in effect. "
        "PANELCAD_AUTOSAVE_SECONDS overrides the saved value. "
        "Does not require an open project."
    ),
}

REPORT_BUG_SCHEMA = {
    "op": "report_bug",
    "title": "Short summary. The first line of the description is used when title is empty.",
    "description": "What happened.",
    "steps": "Optional steps to reproduce.",
    "contact": "Optional contact name.",
    "include_project": False,
    "include_screenshot": False,
    "send": False,
    "returns": ["path", "sent", "send"],
    "notes": (
        "Always writes a zip under the reports folder. send posts a short summary "
        "when a webhook is set (environment, settings, or the release default) "
        "or uploads the zip with a Slack bot token. "
        "PANELCAD_BUG_REPORTS=off or bug_reports.disabled saves the zip only. "
        "A failed op includes crash.id and crash.path so this op can be filed next."
    ),
}

UPDATE_SCHEMA: dict[str, Any] = {
    "update_check": {
        "op": "update_check",
        "description": (
            "Read the configured release feed and report whether a newer version is published. "
            "Does not download the installer."
        ),
        "args": {"feed": "Optional https URL or file/UNC/folder path for this call only."},
    },
    "update_install": {
        "op": "update_install",
        "description": (
            "Download or copy the feed's setup zip, verify sha256, and start the Windows installer "
            "in quiet upgrade mode. Refuses a hash mismatch. Requires confirm=true. "
            "Does not install unless the operator asked."
        ),
        "args": {
            "confirm": "Must be JSON true. In the open GUI this still waits for Install now.",
            "dry_run": "When true, verify the file and return the handoff. The installer is not started.",
            "feed": "Optional feed override.",
            "expected_version": "Optional version the operator already confirmed.",
            "expected_sha256": "Optional sha256 the operator already confirmed.",
        },
    },
    "update_rollback": {
        "op": "update_rollback",
        "description": (
            "Restore src, scripts, and top-level packaging files (pyproject.toml) from the "
            "saved backup and reinstall the venv. Drawings and other files in the program "
            "folder stay. Requires confirm=true. "
            "In the open GUI this still waits for the Roll back button."
        ),
        "args": {"confirm": "Must be JSON true.", "dry_run": "When true, return the plan only."},
    },
}

OPS = {
    "help",
    "create_project",
    "open",
    "inspect",
    "list_sheets",
    "add_sheet",
    "set_active_sheet",
    "set_sheet_title",
    "delete_sheet",
    "close_sheet",
    "open_sheet",
    "move_sheet",
    "reorder_sheets",
    "list_components",
    "list_wires",
    "inspect_device",
    "place",
    "place_from_bom",
    "insert_circuit",
    "undo",
    "redo",
    "move",
    "set_properties",
    "wire",
    "delete_component",
    "rename_component",
    "duplicate_component",
    "rotate_component",
    "delete_wire",
    "set_wire_label",
    "set_wire_spec",
    "set_wire_route",
    "number_wires",
    "set_wire_numbering",
    "wire_numbering",
    "wire_list",
    "set_net_wire_spec",
    "export_wire_markers",
    "retag",
    "convert_standard_plan",
    "convert_standard_next",
    "convert_standard_apply",
    "convert_standard",
    "convert_standard_review",
    "convert_standard_accept",
    "convert_standard_rerun",
    "convert_standard_revert",
    "cross_reference",
    "update_cross_references",
    "export_cross_reference",
    "set_cross_reference",
    "cross_reference_settings",
    "device_index",
    "lookup_zone",
    "trace_wire",
    "circuit_for",
    "checkpoints",
    "upstream",
    "downstream",
    "plc_point",
    "likely_causes",
    "troubleshoot_guide",
    "export_troubleshoot_csv",
    "export_troubleshoot_pdf",
    "export_troubleshoot",
    "append_troubleshoot_log",
    "phase_colors",
    "set_phase_colors",
    "terminal_strips",
    "sequence_terminals",
    "jumper_terminals",
    "clear_terminal_jumpers",
    "open_terminal_strip",
    "list_terminal_strips",
    "add_terminal",
    "reorder_terminals",
    "move_terminal",
    "retag_terminal_strip",
    "add_strip_accessory",
    "delete_strip_component",
    "export_terminal_strip",
    "audit_panel",
    "export_audit",
    "design_checks",
    "check_control_power",
    "size_motor_circuit",
    "check_sizing",
    "export_checks",
    "compute_sccr",
    "set_available_fault",
    "set_part_sccr",
    "nameplate_build",
    "nameplate_set",
    "nameplate_export",
    "suppress_check",
    "unsuppress_check",
    "io_list",
    "io_add_module",
    "io_assign_point",
    "io_generate_sheets",
    "plc_io_list",
    "import_io",
    "export_io",
    "export_plc_tags",
    "assign_item_numbers",
    "cable_schedule",
    "propose_cables",
    "add_cable",
    "update_cable",
    "assign_cable_conductors",
    "export_cable_schedule",
    "panel_wire_annotation",
    "get_title_block",
    "set_title_block",
    "issue_revision",
    "list_revisions",
    "compare_revisions",
    "add_revision_cloud",
    "library_search",
    "parts_search",
    "assign_part",
    "save_library_device",
    "validate",
    "save",
    "export_svg",
    "export_pdf",
    "export_dxf",
    "cutouts_generate",
    "cutouts_export_dxf",
    "cutouts_export_pdf",
    "part_set_cutout",
    "export_bom",
    "import_bom",
    "quote_build",
    "quote_set_rates",
    "quote_import_prices",
    "quote_export",
    "export_wire_list",
    "import_wires",
    "export_json",
    "export_package",
    "print_package",
    "import_any",
    "import_library",
    "import_sheet",
    "import_start",
    "import_next",
    "import_submit",
    "import_status",
    "import_build",
    "import_agent_prep",
    "import_agent_apply",
    "import_agent_answer",
    "import_diagnostics",
    "import_add_photos",
    "add_rail",
    "delete_rail",
    "list_rails",
    "set_enclosure",
    "layout_from_schematic",
    "recommend_cabinet",
    "size_enclosure",
    "recommend_enclosure",
    "apply_enclosure",
    "compute_heat_load",
    "set_ambient",
    "set_scale",
    "set_display_size",
    "set_picture",
    "clear_picture",
    "apply_cut_sheet_pictures",
    "request_oem_update",
    "update_oem",
    "set_pins_locked",
    "set_pin",
    "reset_pins",
    "set_panel",
    "confirm_envelope",
    "list_ports",
    "add_port",
    "set_port",
    "delete_port",
    "set_network",
    "layout_network",
    "layout_io",
    "cleanup",
    "page_density",
    "split_sheet",
    "set_pin_labels",
    "set_address",
    "update_check",
    "update_install",
    "update_rollback",
    "hello",
    "ask_style",
    "say",
    "idle",
    "report_bug",
    "settings",
    "list_prompts",
    "read_prompts",
    "prompt",
    "ui_set_theme",
    "ui_set_canvas",
    "ui_set_phase_colors",
    "reconstruct_add_photos",
    "reconstruct_tag_photo",
    "reconstruct_list_photos",
    "reconstruct_set_corners",
    "reconstruct_set_scale",
    "reconstruct_clear_scale",
    "reconstruct_identify",
    "reconstruct_confirm_item",
    "reconstruct_place_confirmed",
    "reconstruct_place_needs_info",
    "reconstruct_checklist",
    "reconstruct_next_question",
    "reconstruct_answer",
    "reconstruct_confirm_connection",
    "reconstruct_attach_print",
    "reconstruct_compare_print",
    "reconstruct_photo_region",
    "reconstruct_unconfirmed",
    "reconstruct_status",
}

# Short contracts so an AI can discover the API without reading source.
OP_HELP: dict[str, dict[str, str]] = {
    "help": "List every op and its arguments.",
    "inspect": "Snapshot of the project (or one sheet). Args: sheet?",
    "list_sheets": "Pages in the print set: id, title, kind, n/N, counts.",
    "list_components": "Devices on a sheet with pins and properties. Args: sheet?",
    "list_wires": "Wires on a sheet. Args: sheet?",
    "inspect_device": "Library symbol + pins. Args: library_id",
    "place": "Args: library_id, ref?, x, y, sheet?, rotation?, properties?. On panel_layout or door_layout the part must be layout-ready (catalog dims or a confirmed envelope). Do not invent millimetres.",
    "place_from_bom": "Same library part onto this sheet. Args: library_id, ref?, x?, y?, sheet?. Schematic/ladder/network/panel/io each draw it their own way. Does not clone the library.",
    "insert_circuit": insert_circuit_help() + " secondary_manufacturer fills categories the preferred brand does not make (omit = project vendor, else AutomationDirect; none disables). Fallback rows set catalog_fallback=yes.",
    "undo": "Undo the last drawing change. No args.",
    "redo": "Redo. No args.",
    "move": "Args: ref, x, y, sheet?",
    "set_properties": "Args: ref, properties{}, sheet?",
    "wire": "Args: from, to, net?, sheet?, to_sheet?, number?. number locks that wire number, the same as Set wire number. to_sheet = other page, places TO/FROM arrows.",
    "delete_component": "Args: ref, sheet?",
    "duplicate_component": "Copy a device offset 10 mm. Args: ref, sheet?",
    "rotate_component": "Rotate 90° clockwise (or degrees=). Args: ref, degrees?=90, sheet?",
    "delete_wire": "Args: wire_id, sheet?",
    "set_wire_label": "Text drawn on the wire. Args: wire_id? or from+to, label?, net?, sheet?",
    "set_wire_spec": "Wire number, color, gauge (size), type, cable marker. Setting number locks it. Args: wire_id? or from+to, number?, color?, gauge?, type?, cable?, locked?, sheet?",
    "set_net_wire_spec": "Default size, color, and type for one net. Blank wire fields inherit it. net='*' or all is the fallback (stored as *). Empty string clears a field. Args: net, size?, color?, type?",
    "number_wires": "Full renumber with undo. Locked and conventional names (L1/L2/L3, 24V/0V, power/common) stay. Args: start?=1, overwrite?=false, scheme?=sheet_line|sequential|iec",
    "set_wire_numbering": "Project auto wire-number settings. Args: scheme?=sheet_line|sequential|iec, enabled?=true, start?=1, split_feedthrough?=false. split_feedthrough gives each side of a feed-through terminal its own number and renumbers unlocked wires.",
    "wire_numbering": "Current auto wire-number settings. No args.",
    "wire_list": "From-to report. Pass from_to=true for one row per conductor (wire_number, from tag:pin or strip:terminal, to, sheet/line/column at each end, size, color, type, crossing). An unknown gauge is TBD; no default size is substituted. sort=number|device. Without from_to, rows stay the legacy device/terminal list and from_to is still returned. Args: path?, sort?=number|device, from_to?=false",
    "export_wire_markers": "One marker per wire end (a number twice, or once per distinct end). style=heat-shrink|clip-on. target=generic|phoenix|weidmueller|brady. Phoenix is the documented single-column file. Weidmüller and Brady have no published wire-marker header, so they get the generic CSV. Args: path, target?=generic, style?=heat-shrink, sort?=number",
    "retag": "Renumber duplicate schematic tags. Args: mode?=duplicates|family|standard, apply_prefixes?=false. standard (or apply_prefixes) remaps tags to the project's drawing-standard prefixes (JIC/NFPA on NEMA/ANSI). One undo.",
    "convert_standard_plan": (
        "Plan an IEC <-> NEMA conversion (sheet list, tag map, chunks) and write a workspace JSON. "
        "Does not edit the drawing. Args: to=iec|nema, sheets? (comma list or array; omit for the "
        "whole project), out?, workspace?, resume?, keep_numbers?=false "
        "(leave PB2/PB3/PB4; otherwise each letter family is 1, 2, 3)."
    ),
    "convert_standard_next": "Convert the next unfinished chunk and save the workspace. Resumes after a stop. One undo. Does not overwrite the source file. Args: workspace?",
    "convert_standard_apply": "Run every pending chunk and save-as. One undo. Args: workspace?, out?",
    "convert_standard": "Plan and convert the whole project or selected sheets in one undo. Save-as. Args: to=iec|nema, sheets?, out?, workspace?, keep_numbers?=false.",
    "convert_standard_review": "List chunks, flags, and the tag map. Args: workspace?",
    "convert_standard_accept": "Mark a finished chunk accepted. Args: chunk",
    "convert_standard_rerun": "Re-run one chunk from the saved plan. One undo. Args: chunk, workspace?",
    "convert_standard_revert": "Revert one chunk (and a layout that depends on a symbol chunk). One undo. Args: chunk, workspace?",
    "cross_reference": "Coil/contact/device cross-reference report (derived, does not write YAML). Args: write?=false (persist properties.xref), path? (CSV).",
    "update_cross_references": "Write coil/contact refs into the project (properties.xref and off-page callouts). Display is already live; this persists them. No args. Same as cross_reference write=true.",
    "export_cross_reference": "CSV of each coil and its contacts with sheet/line/zone. Args: path",
    "set_cross_reference": "Cross-reference display. Args: rung_pitch_mm?=12.7 (NEMA/JIC line pitch in mm; 12.7 is 0.5 in; line = sheet*100 + rung), rung_numbers?=used|all, nc_mark?=underline|bar|parentheses, enabled?=true",
    "device_index": "Where every element of a device lives (page, zone, element type, terminals, wire numbers). Args: ref? (omit for the full index).",
    "lookup_zone": "What device/element is at a printed page-zone. Args: zone (5-A2, A2, /1.3, 105, sheet:A2), sheet?",
    "trace_wire": "Follow a wire number across sheets (from/to, pages, devices). Args: number.",
    "circuit_for": "Read-only control and power path for a device. Args: ref. Expected voltages come from the drawing; missing ones are needs-info.",
    "checkpoints": "Read-only ordered checks for a symptom. Each check is LIVE or DE-ENERGIZED. Args: ref, symptom.",
    "upstream": "Read-only walk from a terminal toward the supply. Args: endpoint (ref.pin).",
    "downstream": "Read-only walk from a terminal toward the load or return. Args: endpoint (ref.pin).",
    "plc_point": "Read-only PLC I/O point by address, point label, or tag on the drawing. Args: address.",
    "likely_causes": "Read-only ranked causes. readings? is a list of {id or device+terminal or wire, result: ok|wrong|skipped}. Args: ref, symptom, readings?.",
    "troubleshoot_guide": "Read-only guided next check, fault window, and ranked causes. Same walk as the Troubleshoot dock. Args: ref, symptom, readings?.",
    "export_troubleshoot_csv": "CSV of that guided session. Does not change the project file. Args: path, ref, symptom, readings?, who?, note?.",
    "export_troubleshoot_pdf": "PDF of that guided session, opening with the lockout text. Args: path, ref, symptom, readings?, who?, note?.",
    "export_troubleshoot": "Write the open troubleshoot.json document. Does not change the project file. Args: path.",
    "append_troubleshoot_log": "Append readings beside the project (* .troubleshoot-log.json). Does not write the project file. Args: ref, symptom, readings?, who?, note?, when?.",
    "phase_colors": "Read the project phase-color setting (US shop 208/240 and 480 conventions, IEC permitted default).",
    "set_phase_colors": "Set phase colors. Args: enabled?, system_voltage? (blank = follow the drawing), nema_high_voltage?=300, nema_low? (L1,L2,L3; US shop convention black,red,blue), nema_high? (US shop convention brown,orange,yellow), iec? (permitted default brown,black,grey, not a required order), neutral? (NEMA white/grey), iec_neutral? (blue, default light blue; IEC 60445 §6.2.2 is unverified from an official source), ground?",
    "cross_reference_settings": "Current cross-reference display settings (nc_mark, rung_pitch_mm, rung_numbers, enabled). No args.",
    "terminal_strips": "Terminal strip table (internal A, external B, jumper, spare). Args: path?",
    "sequence_terminals": "Write levels from schematic position (sheet, then top to bottom). rename=true retags STRIP-n the same way reorder does. Args: strip, rename?=false",
    "jumper_terminals": "Jumper a consecutive span. Pole count is the span. Picks a catalog part (family + pitch). Args: refs[], group?. One undo.",
    "clear_terminal_jumpers": "Remove jumper marks. Args: refs[]? or group?. One undo.",
    "open_terminal_strip": "Open or create the DIN-rail drawing for a strip and add end hardware if it is missing. Args: strip. One undo.",
    "list_terminal_strips": "Strips in the project: name, terminal count, sheet id. No args.",
    "add_terminal": "Add a terminal on the strip's schematic sheet and shift later levels. Args: strip, library_id?=iec.terminals.x_2p5, after?=ref. One undo.",
    "reorder_terminals": "Schematic order wins. Moves symbols into refs[] order, writes levels, and retags STRIP-n. Args: strip, refs[] every terminal. One undo.",
    "move_terminal": "Move one terminal earlier or later. Same rule as reorder: position, level, and tag stay together. Args: strip, ref, direction?=earlier|later. One undo.",
    "retag_terminal_strip": "Rename tags to STRIP-n in schematic order (sheet, then top to bottom). Wires and panel footprints follow. Args: strip. One undo.",
    "add_strip_accessory": "End plate, end stop, or partition on the panel DIN rail with the strip. Args: strip, kind=end_plate|end_stop|partition, after?=ref (partition). One undo.",
    "delete_strip_component": "Delete a terminal or accessory from the strip, the schematic, and the panel. Args: ref. One undo.",
    "export_terminal_strip": "Vendor strip file. Args: path, strip, vendor?=phoenix|clipx|wago|weidmueller, format?=csv|xml. Phoenix PROJECT complete and WAGO smartDESIGNER default to comma CSV; vendor clipx or a .xml path writes interchange XML (schema unpublished — import unconfirmed). Weidmüller WMC is the documented semicolon CSV (article;designation;marker). Unmapped articles are listed in result.warnings.",
    "audit_panel": "Schematic vs panel/door exceptions. Args: path?",
    "export_audit": "Args: path",
    "design_checks": "Live design findings (errors, warnings, info). Same engine as validate and the GUI problems panel. Args: include_suppressed?=false, path?",
    "check_control_power": "Sum loads on each control supply (24 VDC PSU or control transformer) and compare with the rating. Does not invent ratings; unpublished loads are estimates. An output breaker is suggested only when it covers the continuous load, stays within a known conductor ampacity, and the supply's published trip test shows that rating will trip. If that test is missing, the report says breaker tripping capability is not published and does not suggest a size. When no single breaker fits, it groups the loads into the fewest branches that stay inside the published trip limit and names a sourced library breaker, or the rating range when the library has none. Args: supply?, margin?, path? (CSV), pdf?.",
    "compute_sccr": "UL 508A Supplement SB short-circuit current rating. Does not write the drawing. No args.",
    "set_available_fault": "Available fault current at the panel, in kA. Args: ka (or available_fault_ka), voltage? (fault voltage), sccr_voltage? (panel SCCR voltage).",
    "set_part_sccr": "Set one device's SCCR. Args: ref, sccr_ka?, ir_ka?, fuse_class?, fuse_amps?, let_through_ka?, let_through_at_ka?, circuit?.",
    "size_motor_circuit": (
        "Size one motor branch. NEC Tables 430.250 / 430.248, 430.52, 430.22. "
        "Wire is the larger of NEC 310.16 and UL 508A Table 28.1 "
        "(UL 508A value, not verified against the standard). "
        "Termination temperature defaults to UL 508A 29.6, not verified against the standard "
        "(60°C at 100 A or less, 75°C above). "
        "IEC needs fla and does not invent a full-load table. "
        "Args: hp?, kw?, volts?, phases?=3, fla?, standard?=NEMA/ANSI, device?=inverse_time_breaker, "
        "wire_table?=auto, temp_c? (60 or 75 mark), termination_c?, motor_type?, service_factor?, temp_rise_c?, "
        "allow_nearest_hp?=false, overload_factor?=1, apply?=false, ref? (apply writes a placed motor)."
    ),
    "check_sizing": (
        "Motor sizing findings already in the project: undersized wire, branch device above NEC 430.52, "
        "overload range that misses the current, overload still using the table current with no nameplate. "
        "Also returns a 430.24 feeder when two or more motors have a table current. "
        "Needs an open project."
    ),
    "nameplate_build": "Industrial control panel nameplate from the drawing: fields, full-load working, checks. Does not write. No args.",
    "nameplate_set": "Override a nameplate field or plate option. Args: manufacturer?, trademark?, catalog_model?, job_number?, voltage?, phases?, frequency?, fla?, sccr?, enclosure_type?, drawing_number?, date_code?, largest_load?, max_ocpd?, service_equipment?=yes|no, control_circuit_only?=yes|no, disconnect_locations?, caution_text?, caution_confirmed?=yes|no, preset?=3x5|custom, width_in?, height_in?, hole_diameter_in?, hole_inset_in?, clear?=field list. The first supply uses fla, voltage, phases, frequency. Later supplies use fla:2.",
    "nameplate_export": "Real-size nameplate PDF and engraving DXF (TEXT, CUT, HOLE). Args: pdf?, dxf?, png?.",
    "export_checks": "Write design findings as CSV. Args: path, include_suppressed?=false",
    "suppress_check": "Ignore one finding; stored on the project. Args: finding_id (or check+sheet+ref+target)",
    "unsuppress_check": "Clear a suppressed finding. Args: finding_id",
    "io_list": "PLC I/O list. Args: path?",
    "io_add_module": IO_ADD_MODULE_SCHEMA["description"]
    + " Args: module, ref?, slot?=1, position?=1, polarity?=sink|source, address_base?",
    "io_assign_point": IO_ASSIGN_POINT_SCHEMA["description"]
    + " Args: module, pin, tag?, description?, field?",
    "io_generate_sheets": IO_GENERATE_SHEETS_SCHEMA["description"] + " Args: modules[]?, fuses?",
    "plc_io_list": PLC_IO_LIST_SCHEMA["description"] + " Args: path?",
    "import_io": "PLC I/O spreadsheet. Args: path. Columns: module, address, description, type, wire, field. field is the schematic symbol (pushbutton, prox_3wire, solenoid, …).",
    "rename_component": "Rename a device tag. Wires on its sheet follow. A field device is copied back onto the I/O list. Args: ref (or old), new, sheet?. One undo.",
    "export_io": "Args: path",
    "export_plc_tags": EXPORT_PLC_TAGS_SCHEMA["description"]
    + " Args: path, target=studio5000|tia|tia_csv|domore|keyence|sysmac|cxprogrammer, prefix?, name_from?=device|description, logix_style?=alias|base, logix_slot?=1, analog_start?=byte or auto",
    "assign_item_numbers": "Store BOM item numbers for panel balloons. No args.",
    "cable_schedule": "Field cable schedule when the project has cables, else wires grouped by wire.cable. Args: path?",
    "propose_cables": "Group field wires from panel terminals to each field device into one cable. Spare conductors are configurable. Does not invent manufacturer part numbers. Args: spares?, replace?=false, show_tags?=true, route?",
    "add_cable": "Add one field cable. Args: tag?, to_device?, from_strip?, from_device? (drive when the panel end is VFD1.U), from_terminals?, cable_type?, conductors?, gauge?, shield?, drain?, shield_bond?=both|panel|field|none, shield_source?, length?, length_unit?=ft|m, slack_pct?, gland? (blank unless entered), route?, part_number?, part_source? (http URL), show_tag?, assignments?",
    "update_cable": "Edit one cable. Args: tag, plus any add_cable field. Empty length clears it.",
    "assign_cable_conductors": "Replace the conductor map. Args: tag, conductors[] {id, color, panel_terminal, field_terminal, wire_id?, wire_number?, spare?, shield?, role?}",
    "export_cable_schedule": "Schedule CSV, or PDF when path ends in .pdf. Args: path, pull? (pull-list CSV), terminations? (conductor-to-terminal CSV)",
    "panel_wire_annotation": "Wire number/color/gauge at each panel footprint. Args: path?",
    "set_wire_route": "Keep a wire on an elbow instead of wandering. Args: from, to, points:[{x,y}], sheet?",
    "add_sheet": "Args: sheet_id, title?, kind?=schematic|panel_layout|network|io|door_layout|terminal",
    "set_active_sheet": "Args: sheet_id",
    "set_sheet_title": "Args: title, sheet?",
    "delete_sheet": "Args: sheet_id? Permanently removes the page.",
    "close_sheet": "Hide a page from the tab bar (does not delete). Args: sheet_id?",
    "open_sheet": "Reopen a closed page. Args: sheet_id",
    "move_sheet": "Move a sheet earlier or later in the drawing index. Args: sheet_id, direction?=earlier|later, index?. One undo.",
    "reorder_sheets": "Put open sheets in this order. Args: sheets[] or ids[]. Closed sheets not listed keep their slots. One undo.",
    "get_title_block": "Current title-block fields.",
    "set_title_block": "Any title-block field: company, drawing_number, rev, date, …",
    "issue_revision": "Snapshot the project, bump the rev (A, B, C or 0, 1, 2), and fill the title-block revision table. Args: description?, rev?, scheme?=letter|number, date?, drawn?, checked?, approved?, clear_clouds?, clouds?=true",
    "list_revisions": "Issued revisions (oldest first) and revision clouds. current is the rev on every sheet. No args.",
    "compare_revisions": "Diff two revisions or 'working'. Devices, wires, terminals, BOM, layout moves. Args: left, right, path? (CSV), pdf?",
    "add_revision_cloud": "Manual revision cloud. Args: x, y, w, h, sheet?, rev?",
    "library_search": "Args: query?",
    "parts_search": "Search verified manufacturer catalog parts. Args: query?, manufacturer?, category?, library_id?, amps_min?, amps_max?. Returns manufacturer, catalog, ratings, verified, source, footprint, amps_min/amps_max.",
    "assign_part": "Assign a verified catalog SKU to a placed device. Keeps library_id, tag, and wiring. One undo. Args: ref, catalog, manufacturer?, sheet?. Writes ratings including amps_min/amps_max. Copies a published cutout when that SKU has one.",
    "cutouts_generate": "Cutout sheets for the door, backplate, and walls. Args: datum?, style?=ordinate|baseline, view?=front|back, units?=mm|inch, edge_clearance_mm?, flange_mm?, panel_thickness_mm?, surfaces?, persist?=false. persist stores settings (one undo).",
    "cutouts_export_dxf": "1:1 shop DXF per surface that has openings. Layers CUT, DRILL, DIM, TEXT, OUTLINE. Args: path, surface? or surfaces?, datum?, style?, view?, units?=mm|inch. Skipped faces are listed. Unknown openings are text only.",
    "cutouts_export_pdf": "1:1 cutout PDF (the sheet, not the print package). Args: path, surface? or surfaces?, datum?, style?, view?, units?=mm|inch.",
    "part_set_cutout": "Set or clear one device cutout. Args: ref, cutout? (round, rect, or d, plus holes, corner_radius_mm, clearance_mm, depth_behind_mm, and min_center_x_mm/min_center_y_mm), unknown?=true (false restores the opening, key, and centre minimums; the strings false and true are booleans), clear?=true, surface?, include_key?, min_center_x_mm?, min_center_y_mm?, centers_interchangeable?, pitch_swap? (same flag as centers_interchangeable; both together must agree), sheet?. One undo.",
    "save_library_device": "Save a symbol block into the project library. Never overwrites builtin YAML. Args: id, name?, category?, ref_prefix?, block?=parent|child|terminal|footprint, pins?, width?, height?, draw?, attributes?[{tag,x,y}], footprint_draw?, footprint_attributes?, panel?, library_id?=source to duplicate.",
    "validate": "List drawing errors (empty = ok).",
    "export_pdf": "Drawing sheets only unless sheet= is set. Args: path, sheet?",
    "export_svg": "Args: path, sheet?",
    "export_dxf": "One AutoCAD DXF (R2010, mm) per call. Active sheet unless sheet= is set. Args: path, sheet?",
    "export_bom": "Args: path, grouped?",
    "import_bom": "Parts-list CSV. Columns: Tag/Ref, Part Number, Manufacturer, Description, Qty, library_id (optional). A row with a part number or description and no tag or library id becomes needs-info (no millimetres; off the BOM, quote, and cutouts; print is NOT FOR BUILD). Args: path, place_missing?",
    "quote_build": "Quote from the drawing. Prices only from an imported list, library fields, or manual lines. Labor minutes are example defaults until edited. Nothing is sent. Args: today?",
    "quote_set_rates": "Edit labor minutes, shop rates, markup, tax, freight, contingency, quote number, customer, project, or manual prices. Args: scope?=project|user|both, labor?, shop_rates?, parts_markup_percent?, labor_markup_percent?, category_markups?, tax_percent?, freight?, contingency_percent?, stale_after_days?, price_mode?=cheapest|preferred, preferred_vendor?, number?, customer?, project?, currency?, manual?{line_key:{unit_cost,vendor,lead_time,price_date}}. Nothing is sent.",
    "quote_import_prices": "Import a CSV or XLSX price list (catalog number, vendor, cost, date, lead time). Keeps every vendor. Replaces the price list saved on this project. Args: path, price_mode?=cheapest|preferred, preferred_vendor?. Nothing is downloaded.",
    "quote_export": "Write the quote to a file. Args: path, format?=xlsx|pdf|csv|json, view?=internal|customer, today?. Customer view omits cost, markup, vendor, and shop rate in every format, including json. Result includes incomplete, unpriced, and total_label. Nothing is sent.",
    "export_wire_list": "Args: path",
    "import_wires": "Args: path",
    "export_json": "Full project JSON. Args: path",
    "export_package": "Folder package (yaml/json/bom/wires/library/svg/dxf + print-package PDF). Args: path, pdf?=true, paper?, sections?",
    "print_package": "One customer/shop PDF: cover, index, revisions, drawings, BOM, wires, device index, PLC I/O list, terminals, checks. Blocked (NOT FOR BUILD) while any layout occupant lacks catalog dims or a confirmed envelope. Args: path, paper?=mixed, sections?",
    "import_any": "Auto-detect package, yaml, json, csv, library, sheet. Args: path, replace?",
    "import_library": "Args: path",
    "import_sheet": "Args: path",
    "import_start": "Start a print import. Returns the next crop plus instructions and a JSON schema, or a ready status when the file is vector. The status summary includes tiles_done, tiles_total, spent_usd, input_tokens, estimated_usd, fallbacks, fallback_reasons, and ocr_skipped. Args: path or paths[], photos?, standard?, workspace?, resume?, backend?=external|auto|fake, vision_backend?=none|grok|grok-build|ollama|custom, vision_model?, vision_base_url?, vision_api_key_env?, vision_workers? or workers? (default 3, max 8), vision_timeout_s? (one request; Ollama default 300), vision_page_timeout_s? (one page, not the whole job; Ollama default 3600), num_ctx? (Ollama context, default 16384), num_predict?, think? (Ollama thinking, default false), reasoning_effort? (none|low|medium|high for a /v1 server), clean_grok_sessions? (default true; deletes only that tile's folder under ~/.grok/sessions/<encoded cwd>/<id>), grok_tools_none? (default false; passes --tools none), truth? (json or yaml ground truth). The API key is read from that environment variable and is not stored. Local Grok Build does not take a key.",
    "import_next": "The current crop work item. No args.",
    "import_submit": "Submit extracted JSON for the current crop. Args: work_id, data{}.",
    "import_status": "Compact import status. No images. No args.",
    "import_build": "Build the PanelCAD project from accepted extractions through the Session API. Args: path?",
    "import_agent_prep": "Write page images, overlapping tiles, extracted text, and the agent JSON schema. Does not call a model and does not use an API key. Args: path or paths[], out (folder), dpi? (PDF and vector page PNG resolution, 72–600, default 200; a scan keeps its own DPI; tiles are crops of the page PNG).",
    "import_agent_apply": "Validate an outside agent's JSON, check it against the extracted text, and build the project. Unknown parts become needs-info (no millimetres; off the BOM and the quote). The reply includes questions for a human. Args: path (the JSON), out?, prep? (folder when the JSON omits prep_dir).",
    "import_agent_answer": "Apply one human answer, or a list, and return the questions still open. Does not guess. Args: path (project), id, value, or answers:[{id, value}].",
    "import_diagnostics": "Funnel for one print import. Counts per stage (reported, parsed, accepted, merged, built) and per loss (model_miss, parse_fail, code_reject, wrong_field, merge_loss, build_fail). wrong_field means a wire number was returned in the device field, or a device tag in the wire field. Args: run? (directory, project, or run id; default is the open import), truth? (json or yaml). Ground truth items include lost_at.",
    "import_add_photos": "Add panel photos. They propose layout only. Args: path or paths[].",
    "save": "Args: path?",
    "open": "Args: path",
    "create_project": "Args: name",
    "add_rail": "DIN rail on a panel_layout sheet. Args: rail_id, x, y, length_mm?, sheet?",
    "delete_rail": "Args: rail_id, sheet?",
    "list_rails": "Args: sheet?",
    "set_enclosure": "Backplate size. Args: width_mm?, height_mm?, depth_mm?, x?, y?, name?, sheet?",
    "layout_from_schematic": "Pack unique schematic devices onto a panel_layout sheet. Keeps a sized enclosure and does not invent a box size. The page outline is not a size: result.needs_info is set and nothing is placed until the enclosure is sized. Generic/placeholder parts are left unplaced (result.unplaced). Args: sheet?",
    "recommend_cabinet": "Cabinet W×H×D from BOM incabinet=yes. Args: sheet?, apply?=false",
    "size_enclosure": "Shop enclosure auto-size from footprints, wire duct, spare space, and a 60-70% fill target. Picks the next real catalog size up when the tightest library box is too full. Never invents a width or height. Never writes unless apply=true. Args: apply?=false, place_parts?=false, sheet?, spare_pct?, fill_target?, fill_min?, fill_max?, nema?, material?, ambient_c?, max_internal_c?, duct_face_in?, duct_depth_in?, rail_to_duct_in?, wall_gutter_in?, separate_control_rail?=false",
    "recommend_enclosure": "Same as size_enclosure with apply=false. Reports fill.ratio, fill.stepped_up, and the catalog number.",
    "apply_enclosure": "Write the recommended enclosure (one undo). Args: place_parts?=false, sheet?, same sizing args as size_enclosure.",
    "compute_heat_load": "Watt loss, temperature rise, and cooling for the enclosure already on the panel. Does not resize the box. Args: path? (PDF), ambient_c?, max_internal_c?, min_internal_c?, min_ambient_c?, component_max_c?, altitude_m?, outdoor?, mount?, material?, nema?. Estimates stay labeled ESTIMATE.",
    "set_ambient": "Store ambient and temperature limits used by the thermal report. Does not change enclosure size. One undo. Args: ambient_c?, max_internal_c?, min_internal_c?, min_ambient_c?, component_max_c?, altitude_m?, outdoor?, mount?, material?, nema?, estimates? {ref: watts}.",
    "set_scale": "Resize a device. Args: ref, scale (1.0 = library size), sheet?",
    "set_display_size": "Drawn W×H on panel/door (photo box). Catalog millimetres stay on BOM. Args: ref, width_mm, height_mm, sheet?",
    "set_picture": "Photo on panel/door layout footprints only. Args: ref, path, sheet?",
    "clear_picture": "Args: ref, sheet?",
    "apply_cut_sheet_pictures": "Match OEM cut-sheet images in a folder to layout footprints. Skips print scans. Args: folder?",
    "request_oem_update": "Ask the AI to pull OEM dims/pins/photo. Args: refs? (omit = all)",
    "update_oem": "Apply OEM data. Args: ref, width_mm, height_mm, depth_mm (all required), pins?, picture?, manufacturer?, part_number?, catalog?. Does not mark catalog-ready; use assign_part or confirm_envelope.",
    "set_pins_locked": "Lock (default) or unlock connection points. Args: ref, locked?=true, sheet?",
    "set_pin": "Slide a connection point on this instance. Args: ref, pin_id, x, y, side?, sheet?",
    "reset_pins": "Restore library pin positions. Args: ref, pin_id?, sheet?",
    "set_panel": "OEM / measured footprint. Args: ref, width_mm?, height_mm?, depth_mm?, din_rail?, in_panel?, mount?, dim_source?, confirm?, confirmed_by?, confirmed_at?, sheet?. Without confirm, dim_source=catalog is not written (unconfirmed measured). confirm=true requires explicit width_mm, height_mm, depth_mm ≥ 1 mm, finite, and mount — never confirm a table-fill. Never invent dimensions.",
    "confirm_envelope": "Confirm a measured envelope. Args: ref, width_mm, height_mm, depth_mm, mount (all required), din_rail?, in_panel?, confirmed_by?, confirmed_at?, sheet?. Rejects NaN, sizes under 1 mm, and sizes larger than the largest catalog enclosure. Never invent millimetres.",
    "list_ports": "Comms ports on a device. Args: ref, sheet?",
    "add_port": "Args: ref, port_id, kind?=serial|ethernet|usb, medium?, protocol?, connector?, side?, sheet?",
    "set_port": "Args: ref, port_id, kind?, medium?, protocol?, connector?, side?, sheet?",
    "delete_port": "Args: ref, port_id, sheet?",
    "set_network": "Comms page header. Args: name?, medium?, protocol?, sheet?",
    "layout_network": "Place devices with matching ports on a network sheet. Does not invent IP addresses; an unknown IP stays blank or TBD. Args: sheet?, medium?",
    "layout_io": "Arrange PLC/I/O cards as ladder rungs. Args: sheet?",
    "cleanup": "Unstack overlaps, spread devices on the spacing grid (minimum horizontal and vertical clearance, across the usable width), scale field symbols to rungs, keep devices on the page. Returns density (page fullness). Args: sheet? (omit = all sheets). Run this after place/layout_io.",
    "page_density": "Devices and rungs against the usable-area limit for each page. Not a fixed count. over_dense recommends split_sheet. Args: sheet?. Does not write.",
    "split_sheet": "When a schematic or I/O page is over the usable-area limit, recommend a continuation. apply=true moves the overflow onto the next page and ties the cut with off-page references. Args: sheet?, apply?=false.",
    "set_pin_labels": "OEM terminal captions. Args: ref, labels{id:text}, sheet?",
    "set_address": "IP and/or node/station. Args: ref, ip?, node?, sheet?",
    "update_check": (
        "Read the release feed (Help > Updates, PANELCAD_UPDATE_FEED, or the built-in feed). "
        "Does not download. Args: feed?"
    ),
    "update_install": (
        "Download the installer, verify sha256, and start the Windows upgrade. "
        "Requires confirm=true. dry_run=true returns the handoff and does not run it. "
        "Args: confirm, dry_run?, feed?"
    ),
    "update_rollback": (
        "Restore src, scripts, and packaging files such as pyproject.toml from the saved "
        "backup. Requires confirm=true. Optional dry_run=true. Drawings in the program folder stay."
    ),
    "hello": "Announce the AI to the GUI. Args: model?=Grok 4.6, name?=Grok",
    "ask_style": "Ask the operator schematic vs ladder vs freeform before drawing a print. No args.",
    "report_bug": (
        "Write a bug-report zip (report.json, last log, optional screenshot and project). "
        "Args: title, description, steps?, contact?, include_project?=false, "
        "include_screenshot?=false, send?=false. Returns path and send. "
        "send posts when a webhook is configured (environment, settings, or the "
        "release default) or a Slack bot token and channel are set. "
        "PANELCAD_BUG_REPORTS=off saves the zip only. Secrets are never written into the zip."
    ),
    "settings": (
        "Autosave interval in seconds (0 off). Args: autosave_seconds?. "
        "Returns autosave_seconds. PANELCAD_AUTOSAVE_SECONDS overrides the saved value. "
        "No project required."
    ),
    "say": "Status line for the AI window. Args: text",
    "idle": "Mark the AI idle in the GUI. Args: text?",
    "list_prompts": "Human prompts typed in the AI dock (recent).",
    "read_prompts": "Unread prompt-line messages. Consume them, then act via Session ops.",
    "prompt": "Run a human prompt line now. Args: text",
    "ui_set_theme": "Window theme for the GUI. Args: theme=light|dark|system. Saved outside the project. Print, PDF, DXF, and SVG stay unchanged.",
    "ui_set_phase_colors": "Phase colours on the sheet. Args: enabled=true|false. Print, PDF, DXF, and SVG stay unchanged.",
    "ui_set_canvas": "On-screen sheet. Args: canvas=paper|white|dark|auto. auto follows the theme. Exports stay white paper with the drawing colors.",
    "reconstruct_add_photos": "Add phone photos of a physical panel. Args: paths[] or path, role?=backpanel|door_inside|door_outside|nameplate|part_label|terminal_strip|wire_numbers|other, note?",
    "reconstruct_tag_photo": "Tag a stored photo. Args: photo_id, role, note?",
    "reconstruct_list_photos": "Photos stored with the project.",
    "reconstruct_set_corners": "Four tech-adjustable perspective corners in photo pixels. Args: photo_id, corners=[{x,y}×4]",
    "reconstruct_set_scale": (
        "Real scale from two known lengths or the enclosure face width and height. "
        "A single length does not publish millimetres. Residual above 8 mm returns scaled=false "
        "(does not raise). Args: photo_id, kind=din_rail|catalog_part|wire_duct|measured|face, "
        "p1=[x,y], p2=[x,y], known_mm?, known_height_mm?, catalog?, manufacturer?, "
        "dimension?=width|height, note?, slot?=1|2, replace?=scale|scale_b. "
        "known_height_mm applies only to kind=face. A length mark does not drop a face "
        "scale; call reconstruct_clear_scale first to replace the face."
    ),
    "reconstruct_clear_scale": "Clear face and length scale marks on a photo. Args: photo_id",
    "reconstruct_identify": "Read labels on panel photos via the local vision backend (stub/fake/ollama; print-import plugs in later). Args: photo_id?",
    "reconstruct_confirm_item": "Catalog match or tech-confirmed envelope (W, H, D, mount). Converts a needs-info placeholder in place. Args: item_id, catalog?, manufacturer?, envelope_w_mm?, envelope_h_mm?, envelope_d_mm?, mount?, tag?, library_id?",
    "reconstruct_place_needs_info": "Place reconstruct-only INFO NEEDED markers at measured photo positions. Not generic parts. Args: photo_id?",
    "reconstruct_place_confirmed": "Place layout-ready parts at measured millimetres. Unconfirmed items stay in Problems. Args: sheet?",
    "reconstruct_checklist": "Floor checklist (trace wires, confirm parts, compare an old print).",
    "reconstruct_next_question": "Next open checklist question for the tech on the floor.",
    "reconstruct_answer": "Record an answer (typed landing like K1:A1, or a new photo). Args: step_id, text?, photo?, confirm?",
    "reconstruct_confirm_connection": "Mark an inferred wire as confirmed. Args: step_id? or wire_id?",
    "reconstruct_attach_print": "Attach a secondary old print (PanelCAD project, print-index YAML, PDF/scan). Photos stay primary. Args: path, note?",
    "reconstruct_compare_print": "Flag mismatches between panel photos and the attached print. Reuses print-import when that pipeline is present. Args: path?",
    "reconstruct_photo_region": "Photo crop linked to an observed item or placed ref. Args: item_id? or ref?",
    "reconstruct_unconfirmed": "Items that must not go on the layout yet.",
    "reconstruct_status": "Photos, items, checklist, prints, mismatches.",
}


def _pin_ids(device: Any) -> list[str]:
    return [p.id for p in getattr(device, "pins", []) or []]


def _catalog_hit(device: Any) -> dict[str, Any]:
    return {
        "library_id": device.library_id,
        "name": device.name,
        "standard": device.standard,
        "pins": _pin_ids(device),
    }


def _placed(session: Session, comp: Any) -> dict[str, Any]:
    device = session.catalog.get(comp.library_id)
    return {
        "id": comp.id,
        "library_id": comp.library_id,
        "x": comp.x,
        "y": comp.y,
        "pins": _pin_ids(device),
        "standard": device.standard,
    }


def _cutout_options(msg: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key in ("datum", "style", "view", "units"):
        if msg.get(key):
            options[key] = msg[key]
    for key in ("edge_clearance_mm", "flange_mm", "panel_thickness_mm"):
        if key in msg:
            options[key] = _opt_float(msg.get(key))
    surfaces = msg.get("surfaces")
    if msg.get("surface"):
        surfaces = msg.get("surface")
    if surfaces:
        if isinstance(surfaces, str):
            options["surfaces"] = [part.strip() for part in surfaces.split(",") if part.strip()]
        else:
            options["surfaces"] = [str(part).strip() for part in surfaces if str(part).strip()]
    return options


def _section_list(value: Any) -> list[str] | None:
    if value is None or value is False:
        return None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",") if part.strip()]
        return parts or None
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
        return parts or None
    return None


def _domain(msg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """A resource name. Never the JSONL request ``id`` (that is correlation only)."""

    for key in keys:
        if key == "id":
            continue
        val = msg.get(key)
        if val is not None and str(val).strip() != "":
            return val
    return default


def _json_bool(value: Any, default: bool | None = None) -> bool | None:
    """A JSON boolean. The string ``false`` is false. Any other token is an error."""

    if value is None or value == "":
        return default
    parsed = _flag(value, None)
    if parsed is None:
        raise ValueError(f"expected true or false, got {value!r}")
    return parsed


def _flag(value: Any, default: bool | None = None) -> bool | None:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return default
_CABLE_FIELDS = {
    "tag",
    "from_strip",
    "from_device",
    "from_terminals",
    "to_device",
    "to_kind",
    "cable_type",
    "conductors",
    "gauge",
    "shield",
    "drain",
    "shield_bond",
    "shield_source",
    "shield_note",
    "length",
    "length_unit",
    "slack_pct",
    "gland",
    "od",
    "od_source",
    "route",
    "part_number",
    "part_source",
    "show_tag",
    "pairs",
    "assignments",
}


def _cable_fields(msg: dict[str, Any]) -> dict[str, Any]:
    return {key: msg[key] for key in _CABLE_FIELDS if key in msg}


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _from_to_columns() -> tuple[str, ...]:
    from panelcad.core.from_to import FROM_TO_COLUMNS

    return FROM_TO_COLUMNS


def _wire_list_schema() -> dict[str, object]:
    from panelcad.core.from_to import WIRE_LIST_SCHEMA

    return WIRE_LIST_SCHEMA


def _wire_marker_schema() -> dict[str, object]:
    from panelcad.io.wire_markers import WIRE_MARKER_SCHEMA

    return WIRE_MARKER_SCHEMA


def _device_index_schema() -> dict[str, object]:
    from panelcad.core.xref_index import DEVICE_INDEX_SCHEMA

    return DEVICE_INDEX_SCHEMA


def _lookup_zone_schema() -> dict[str, object]:
    from panelcad.core.xref_index import LOOKUP_ZONE_SCHEMA

    return LOOKUP_ZONE_SCHEMA


def _trace_wire_schema() -> dict[str, object]:
    from panelcad.core.xref_index import TRACE_WIRE_SCHEMA

    return TRACE_WIRE_SCHEMA


def _troubleshoot_schemas() -> dict[str, dict[str, object]]:
    from panelcad.core.troubleshoot import TROUBLESHOOT_SCHEMAS

    return TROUBLESHOOT_SCHEMAS


def _readings_arg(msg: dict[str, Any]) -> list[dict[str, Any]] | None:
    """JSONL readings may be a list or a JSON string. Unknown shapes are a query error."""

    from panelcad.core.xref_index import QueryError

    raw = msg.get("readings")
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QueryError("readings must be a JSON list") from exc
    if not isinstance(raw, list):
        raise QueryError("readings must be a list")
    return raw


def _is_query_error(exc: BaseException) -> bool:
    """True for a technician query the operator rejected (not a crash)."""

    from panelcad.core.xref_index import QueryError

    return isinstance(exc, QueryError)


def _crash_for_agent(exc: BaseException) -> dict[str, str] | None:
    """Crash record for a failed op. The drawing agent can pass the id to report_bug."""

    from panelcad.library.panel import LayoutNotReadyError

    if isinstance(exc, LayoutNotReadyError):
        return None
    from panelcad.io.share import ShareError

    if isinstance(exc, ShareError):
        return None
    try:
        from panelcad.diag.crash import record_exception

        record = record_exception(exc, source="agent", fatal=False)
    except Exception:  # noqa: BLE001
        return None
    crash_id = str(record.get("id") or "")
    if not crash_id:
        return None
    return {"id": crash_id, "path": str(record.get("path") or "")}


class Agent:
    def __init__(self) -> None:
        self.session: Session | None = None
        self.print_import = None

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        req_id = msg.get("id")
        op = msg.get("op")
        try:
            if self.session is not None:
                from panelcad.diag.crash import set_recovery_session

                set_recovery_session(self.session)
            if op not in OPS:
                raise ValueError(f"unknown op '{op}'")
            result = getattr(self, f"op_{op}")(msg)
            if self.session is not None:
                from panelcad.diag.crash import set_recovery_session

                set_recovery_session(self.session)
            try:
                from panelcad.diag.log import log_agent

                log_agent(str(op or ""), True)
            except Exception:  # noqa: BLE001
                pass
            return {"id": req_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 — protocol must never crash the loop
            if self.session is not None:
                try:
                    self.session.emit(str(op or "op"), str(exc), status="error", error=str(exc))
                except Exception:  # noqa: BLE001
                    pass
            crash = None
            if not _is_query_error(exc):
                crash = _crash_for_agent(exc)
            try:
                from panelcad.diag.log import log_agent

                log_agent(str(op or ""), False)
            except Exception:  # noqa: BLE001
                pass
            reply: dict[str, Any] = {"id": req_id, "ok": False, "error": str(exc)}
            if crash:
                reply["crash"] = crash
            return reply

    def _need(self) -> Session:
        if self.session is None:
            raise RuntimeError("no project; call create_project or open first")
        return self.session

    def op_help(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {
            "ops": sorted(OPS),
            "help": OP_HELP,
            "schemas": {
                "insert_circuit": INSERT_CIRCUIT_SCHEMA,
                "wire_list": _wire_list_schema(),
                "export_wire_markers": _wire_marker_schema(),
                "plc_io_list": PLC_IO_LIST_SCHEMA,
                "export_plc_tags": EXPORT_PLC_TAGS_SCHEMA,
                "report_bug": REPORT_BUG_SCHEMA,
                "settings": SETTINGS_SCHEMA,
                "size_motor_circuit": {
                    "op": "size_motor_circuit",
                    "args": {
                        "hp": "Horsepower. Exact table row unless allow_nearest_hp is true.",
                        "volts": "System voltage. 480 uses the 460 V column. 400 is not an NEC column.",
                        "phases": "1 or 3.",
                        "fla": "Nameplate current. Used for the overload, not the conductor.",
                        "device": "inverse_time_breaker, dual_element_fuse, nontime_delay_fuse, instantaneous_trip.",
                        "wire_table": "auto, marked-75, or marked-60. Both 310.16 and Table 28.1 are checked. Table 28.1 is not verified against the standard.",
                        "temp_c": "60 or 75 when terminations are marked. Omit for UL 508A 29.6 (not verified against the standard).",
                        "termination_c": "Same as temp_c. A marked temperature overrides 29.6.",
                        "apply": "When true, write the result onto ref.",
                        "ref": "Placed motor tag. Required when apply is true.",
                    },
                },
                "check_sizing": {"op": "check_sizing", "args": {}},
                "update": UPDATE_SCHEMA,
                "io_add_module": IO_ADD_MODULE_SCHEMA,
                "io_assign_point": IO_ASSIGN_POINT_SCHEMA,
                "io_generate_sheets": IO_GENERATE_SHEETS_SCHEMA,
                "set_phase_colors": PHASE_COLOR_SCHEMA,
                "device_index": _device_index_schema(),
                "lookup_zone": _lookup_zone_schema(),
                "trace_wire": _trace_wire_schema(),
                **_troubleshoot_schemas(),
            },
            "note": "GUI is for humans. AIs should use this JSONL protocol or Python Session.describe().",
        }

    def op_ask_style(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().ask_print_style()

    def op_settings(self, msg: dict[str, Any]) -> dict[str, Any]:
        raw = msg.get("autosave_seconds", None)
        if self.session is not None:
            if raw is not None and str(raw).strip() != "":
                return self.session.set_autosave_seconds(int(raw))
            return self.session.app_settings()
        from panelcad.diag.settings import autosave_seconds, set_autosave_seconds

        if raw is not None and str(raw).strip() != "":
            set_autosave_seconds(int(raw))
        return {"autosave_seconds": autosave_seconds()}

    def op_report_bug(self, msg: dict[str, Any]) -> dict[str, Any]:
        include_project = msg.get("include_project", False)
        include_screenshot = msg.get("include_screenshot", False)
        send = msg.get("send", False)
        return self._need().report_bug(
            str(msg.get("title") or ""),
            str(msg.get("description") or msg.get("what") or ""),
            include_project=bool(include_project),
            include_screenshot=bool(include_screenshot),
            send=bool(send),
            steps=str(msg.get("steps") or ""),
            contact=str(msg.get("contact") or ""),
        )

    def op_hello(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.api.activity import DEFAULT_MODEL

        model = str(msg.get("model") or msg.get("version") or DEFAULT_MODEL)
        name = str(msg.get("name") or "Grok")
        if self.session is None:
            self.session = Session.create("Untitled Panel")
            from panelcad.diag.crash import set_recovery_session

            set_recovery_session(self.session)
        return self.session.announce(model=model, name=name)

    def op_say(self, msg: dict[str, Any]) -> dict[str, Any]:
        text = str(msg.get("text") or msg.get("message") or "")
        return self._need().emit("say", text, status="working")

    def op_idle(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().set_idle(str(msg.get("text") or "idle"))

    def op_ui_set_theme(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.theme import set_theme

        return set_theme(str(msg.get("theme") or ""))

    def op_ui_set_canvas(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.theme import set_canvas

        return set_canvas(str(msg.get("canvas") or ""))

    def op_ui_set_phase_colors(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.theme import parse_phase_enabled, set_phase_colors

        raw = msg.get("enabled", msg.get("phase_colors", True))
        return set_phase_colors(parse_phase_enabled(raw))

    def op_list_prompts(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.api.activity import prompt_path, read_jsonl

        rows = read_jsonl(prompt_path(self._need().path), limit=int(msg.get("limit") or 50))
        return {"prompts": rows}

    def op_read_prompts(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.api.activity import drain_prompts

        rows = drain_prompts(self._need().path)
        return {"prompts": rows, "n": len(rows)}

    def op_prompt(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().run_prompt(str(msg.get("text") or msg.get("prompt") or ""))

    def op_inspect(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().describe(msg.get("sheet") or msg.get("sheet_id"))

    def op_list_sheets(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"sheets": self._need().list_sheets(), "active": self._need().active_sheet_id}

    def op_set_sheet_title(self, msg: dict[str, Any]) -> dict[str, Any]:
        sheet = self._need().set_sheet_title(str(msg["title"]), sheet_id=msg.get("sheet"))
        return {"id": sheet.id, "title": sheet.title}

    def op_list_wires(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"wires": self._need().list_wires(msg.get("sheet"))}

    def op_inspect_device(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().inspect_device(msg["library_id"])

    def op_move(self, msg: dict[str, Any]) -> dict[str, Any]:
        comp = self._need().move(msg["ref"], float(msg["x"]), float(msg["y"]), sheet_id=msg.get("sheet"))
        return {"id": comp.id, "x": comp.x, "y": comp.y}

    def op_set_scale(self, msg: dict[str, Any]) -> dict[str, Any]:
        comp = self._need().set_scale(msg["ref"], float(msg["scale"]), sheet_id=msg.get("sheet"))
        return {"id": comp.id, "scale": comp.scale}

    def op_set_display_size(self, msg: dict[str, Any]) -> dict[str, Any]:
        w = msg.get("width_mm") if msg.get("width_mm") is not None else msg.get("w")
        h = msg.get("height_mm") if msg.get("height_mm") is not None else msg.get("h")
        comp = self._need().set_display_size(
            msg["ref"], float(w), float(h), sheet_id=msg.get("sheet")
        )
        return {
            "id": comp.id,
            "display_width_mm": comp.properties.get("display_width_mm"),
            "display_height_mm": comp.properties.get("display_height_mm"),
        }

    def op_set_picture(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().set_picture(msg["ref"], msg["path"], sheet_id=msg.get("sheet"))
        return {"id": msg["ref"], "picture": str(path)}

    def op_clear_picture(self, msg: dict[str, Any]) -> dict[str, Any]:
        comp = self._need().clear_picture(msg["ref"], sheet_id=msg.get("sheet"))
        return {"id": comp.id, "picture": comp.picture}

    def op_apply_cut_sheet_pictures(self, msg: dict[str, Any]) -> dict[str, Any]:
        folder = msg.get("folder") or msg.get("path")
        return self._need().apply_cut_sheet_pictures(folder)

    def op_request_oem_update(self, msg: dict[str, Any]) -> dict[str, Any]:
        refs = msg.get("refs") or msg.get("ref")
        if isinstance(refs, str):
            refs = [refs]
        return self._need().request_oem_update(refs)

    def op_update_oem(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().apply_oem(
            msg["ref"],
            width_mm=msg.get("width_mm"),
            height_mm=msg.get("height_mm"),
            depth_mm=msg.get("depth_mm"),
            manufacturer=msg.get("manufacturer"),
            part_number=msg.get("part_number"),
            catalog=msg.get("catalog"),
            pins=msg.get("pins"),
            picture=msg.get("picture") or msg.get("path"),
            sheet_id=msg.get("sheet"),
        )

    def op_set_pins_locked(self, msg: dict[str, Any]) -> dict[str, Any]:
        locked = msg.get("locked")
        if locked is None:
            locked = True
        comp = self._need().set_pins_locked(msg["ref"], bool(locked), sheet_id=msg.get("sheet"))
        return {"id": comp.id, "pins_locked": comp.pins_locked}

    def op_set_pin(self, msg: dict[str, Any]) -> dict[str, Any]:
        pin_id = str(msg.get("pin_id") or msg.get("pin"))
        ov = self._need().set_pin(
            msg["ref"],
            pin_id,
            float(msg["x"]),
            float(msg["y"]),
            side=msg.get("side"),
            sheet_id=msg.get("sheet"),
        )
        return {"ref": msg["ref"], "pin_id": pin_id, **ov.model_dump()}

    def op_reset_pins(self, msg: dict[str, Any]) -> dict[str, Any]:
        comp = self._need().reset_pins(msg["ref"], pin_id=msg.get("pin_id") or msg.get("pin"), sheet_id=msg.get("sheet"))
        return {"id": comp.id, "pin_overrides": {k: v.model_dump() for k, v in comp.pin_overrides.items()}}

    def op_list_ports(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"ports": self._need().list_ports(msg["ref"], sheet_id=msg.get("sheet"))}

    def op_add_port(self, msg: dict[str, Any]) -> dict[str, Any]:
        port = self._need().add_port(
            msg["ref"],
            str(_domain(msg, "port_id", default="COM")),
            kind=str(msg.get("kind") or "serial"),
            medium=str(msg.get("medium") or ""),
            protocol=str(msg.get("protocol") or ""),
            connector=str(msg.get("connector") or ""),
            side=msg.get("side"),
            sheet_id=msg.get("sheet"),
        )
        return port.model_dump()

    def op_set_port(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in msg.items() if k not in ("id", "op", "ref", "port_id", "sheet")}
        port = self._need().set_port(msg["ref"], str(msg.get("port_id") or msg.get("pid")), sheet_id=msg.get("sheet"), **fields)
        return port.model_dump()

    def op_delete_port(self, msg: dict[str, Any]) -> dict[str, Any]:
        pid = str(msg.get("port_id") or msg.get("pid"))
        self._need().delete_port(msg["ref"], pid, sheet_id=msg.get("sheet"))
        return {"deleted": pid}

    def op_set_network(self, msg: dict[str, Any]) -> dict[str, Any]:
        info = self._need().set_network(
            name=msg.get("name"),
            medium=msg.get("medium"),
            protocol=msg.get("protocol"),
            sheet_id=msg.get("sheet"),
        )
        return info.model_dump()

    def op_layout_network(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().layout_network(msg.get("sheet") or msg.get("sheet_id"), medium=msg.get("medium"))

    def op_layout_io(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().layout_io(msg.get("sheet") or msg.get("sheet_id"))

    def op_set_address(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().set_address(
            msg["ref"],
            ip=msg.get("ip") or msg.get("address"),
            node=msg.get("node") or msg.get("station"),
            sheet_id=msg.get("sheet"),
        )

    def op_set_panel(self, msg: dict[str, Any]) -> dict[str, Any]:
        din = msg.get("din_rail")
        if isinstance(din, str):
            din = din.lower() in ("1", "true", "yes")
        inp = msg.get("incabinet") if msg.get("incabinet") is not None else msg.get("in_panel")
        if isinstance(inp, str):
            inp = inp.lower() in ("1", "true", "yes")
        confirm = msg.get("confirm")
        if isinstance(confirm, str):
            confirm = confirm.lower() in ("1", "true", "yes")
        return self._need().set_panel(
            msg["ref"],
            width_mm=msg.get("width_mm"),
            height_mm=msg.get("height_mm"),
            depth_mm=msg.get("depth_mm"),
            din_rail=din,
            in_panel=inp,
            incabinet=inp,
            mount=msg.get("mount"),
            dim_source=str(msg.get("dim_source") or ""),
            confirm=bool(confirm),
            confirmed_by=str(msg.get("confirmed_by") or ""),
            confirmed_at=str(msg.get("confirmed_at") or ""),
            sheet_id=msg.get("sheet"),
        )

    def op_confirm_envelope(self, msg: dict[str, Any]) -> dict[str, Any]:
        din = msg.get("din_rail")
        if isinstance(din, str):
            din = din.lower() in ("1", "true", "yes")
        inp = msg.get("incabinet") if msg.get("incabinet") is not None else msg.get("in_panel")
        if isinstance(inp, str):
            inp = inp.lower() in ("1", "true", "yes")
        from panelcad.library.panel import require_explicit_envelope

        width_mm, height_mm, depth_mm, mount = require_explicit_envelope(
            msg.get("width_mm"),
            msg.get("height_mm"),
            msg.get("depth_mm"),
            msg.get("mount"),
        )
        return self._need().confirm_envelope(
            msg["ref"],
            width_mm=width_mm,
            height_mm=height_mm,
            depth_mm=depth_mm,
            mount=mount,
            din_rail=din,
            in_panel=inp,
            confirmed_by=str(msg.get("confirmed_by") or ""),
            confirmed_at=str(msg.get("confirmed_at") or ""),
            sheet_id=msg.get("sheet"),
        )

    def op_set_properties(self, msg: dict[str, Any]) -> dict[str, Any]:
        props = msg.get("properties") or {k: v for k, v in msg.items() if k not in ("id", "op", "ref", "sheet")}
        comp = self._need().set_component_properties(msg["ref"], props, sheet_id=msg.get("sheet"))
        return {"id": comp.id, "properties": comp.properties}

    def op_get_title_block(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().get_title_block()

    def op_create_project(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.session = Session.create(str(msg.get("name", "Untitled")))
        from panelcad.diag.crash import set_recovery_session

        set_recovery_session(self.session)
        fields = {
            key: msg[key]
            for key in ("standard", "drawing_style", "coil_style")
            if msg.get(key)
        }
        if fields:
            self.session.set_title_block(**fields)
        tb = self.session.get_title_block()
        style = str(tb.get("drawing_style") or "")
        return {
            "name": self.session.project.name,
            "sheet": self.session.active_sheet_id,
            "standard": tb.get("standard"),
            "drawing_style": style,
            "coil_style": tb.get("coil_style"),
            "style_needed": style not in {"ladder", "schematic", "freeform"},
            "next": None if style in {"ladder", "schematic", "freeform"} else "ask_style",
            "wire_numbering": self.session.wire_numbering(),
            "cross_reference": self.session.cross_reference_settings(),
        }

    def op_open(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.session = Session.open(msg["path"])
        return {"name": self.session.project.name, "path": str(self.session.path)}

    def op_add_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        sheet = self._need().add_sheet(
            str(_domain(msg, "sheet_id", "sheet", default="sheet")),
            title=str(msg.get("title", "")),
            kind=msg.get("kind", "schematic"),
        )
        if msg.get("kind") == "network" and (msg.get("medium") or msg.get("protocol") or msg.get("name")):
            self._need().set_network(
                name=msg.get("name") or sheet.title,
                medium=msg.get("medium"),
                protocol=msg.get("protocol"),
            )
        return {"id": sheet.id}

    def op_set_active_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        sheet = self._need().set_active_sheet(str(msg.get("sheet_id") or msg.get("sheet")))
        return {"id": sheet.id, "title": sheet.title}

    def op_delete_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        sid = str(msg.get("sheet_id") or msg.get("sheet") or "")
        self._need().delete_sheet(sid or None)
        return {"active": self._need().active_sheet_id}

    def op_close_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        sid = str(msg.get("sheet_id") or msg.get("sheet") or "")
        return self._need().close_sheet(sid or None)

    def op_open_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        sheet = self._need().open_sheet(str(msg.get("sheet_id") or msg.get("sheet")))
        return {"id": sheet.id, "title": sheet.title, "closed": sheet.closed}

    def op_move_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        index = msg.get("index")
        return self._need().move_sheet(
            str(msg.get("sheet_id") or msg.get("sheet") or ""),
            index=None if index is None or index == "" else int(index),
            direction=None if msg.get("direction") is None else str(msg.get("direction")),
        )

    def op_reorder_sheets(self, msg: dict[str, Any]) -> dict[str, Any]:
        raw = msg.get("sheets") or msg.get("ids") or msg.get("order") or []
        return self._need().reorder_sheets([str(item) for item in raw])

    def op_undo(self, msg: dict[str, Any]) -> dict[str, Any]:
        ok = self._need().undo()
        return {"undone": ok, "can_undo": self._need().can_undo(), "can_redo": self._need().can_redo()}

    def op_redo(self, msg: dict[str, Any]) -> dict[str, Any]:
        ok = self._need().redo()
        return {"redone": ok, "can_undo": self._need().can_undo(), "can_redo": self._need().can_redo()}

    def op_place_from_bom(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().place_from_bom(
            library_id=msg["library_id"],
            ref=msg.get("ref"),
            x=float(msg.get("x", 40)),
            y=float(msg.get("y", 40)),
            sheet_id=msg.get("sheet"),
        )

    def op_place(self, msg: dict[str, Any]) -> dict[str, Any]:
        comp = self._need().place(
            library_id=msg["library_id"],
            ref=msg.get("ref"),
            x=float(msg.get("x", 40)),
            y=float(msg.get("y", 40)),
            rotation=int(msg.get("rotation", 0)),
            sheet_id=msg.get("sheet"),
            properties=msg.get("properties"),
            rail_id=msg.get("rail_id"),
            snap=msg.get("snap", True),
        )
        return _placed(self._need(), comp)

    def op_insert_circuit(self, msg: dict[str, Any]) -> dict[str, Any]:
        def _num(*keys: str) -> float | None:
            for key in keys:
                if key not in msg:
                    continue
                val = msg[key]
                if val is None or val == "":
                    return None
                return float(val)
            return None

        sheet = msg.get("sheet", msg.get("sheet_id"))
        if sheet is not None and not str(sheet).strip():
            sheet = None
        validate_insert_circuit_tool(msg)
        phases = msg.get("phases")
        if phases in ("", None):
            phases = None
        else:
            phases = _phase_count(phases)
        return self._need().insert_circuit(
            kind=str(msg.get("kind") or "dol"),
            drive=msg.get("drive"),
            branch=str(msg.get("branch") or "breaker"),
            line_contactor=_json_bool(msg.get("line_contactor"), False) if "line_contactor" in msg else False,
            control=str(msg.get("control") or "none"),
            kw=_num("kw", "kW"),
            hp=_num("hp"),
            sheet_id=None if sheet is None else str(sheet),
            new_sheet=_json_bool(msg.get("new_sheet"), False) if "new_sheet" in msg else False,
            x=float(msg.get("x", 40)),
            y=float(msg.get("y", 22)),
            voltage=_num("voltage"),
            control_voltage=_num("control_voltage", "controlVoltage"),
            fla=_num("fla", "FLA"),
            pilot_lights=_json_bool(msg.get("pilot_lights"), False) if "pilot_lights" in msg else False,
            bypass=_json_bool(msg.get("bypass"), False) if "bypass" in msg else False,
            control_wiring=str(msg.get("control_wiring") or "3wire"),
            starter=str(msg.get("starter") or "dol"),
            timer_seconds=_num("timer_seconds", "timer"),
            overload_placement=str(msg.get("overload_placement") or "windings"),
            hoa=_json_bool(msg.get("hoa"), True) if "hoa" in msg else True,
            plc_model=msg.get("plc_model"),
            io_run=msg.get("io_run") or msg.get("ioRun"),
            io_ol=msg.get("io_ol") or msg.get("ioOl"),
            io_auto=msg.get("io_auto") or msg.get("ioAuto"),
            io_out=msg.get("io_out") or msg.get("ioOut"),
            manufacturer=(str(msg.get("manufacturer")).strip() or None) if msg.get("manufacturer") else None,
            secondary_manufacturer=(
                str(msg.get("secondary_manufacturer")).strip()
                if "secondary_manufacturer" in msg and msg.get("secondary_manufacturer") is not None
                else None
            ),
            phases=phases,
            place_layout=_json_bool(msg.get("place_layout"), True) if "place_layout" in msg else True,
            shared_supply=_json_bool(msg.get("shared_supply"), True) if "shared_supply" in msg else True,
            shared_estop=_json_bool(msg.get("shared_estop"), True) if "shared_estop" in msg else True,
            separate_control_rail=(
                _json_bool(msg.get("separate_control_rail"), False) if "separate_control_rail" in msg else False
            ),
            standard=msg.get("standard"),
        )

    def op_add_rail(self, msg: dict[str, Any]) -> dict[str, Any]:
        rail = self._need().add_rail(
            str(_domain(msg, "rail_id", default="r1")),
            x=float(msg.get("x", 40)),
            y=float(msg.get("y", 80)),
            length_mm=float(msg.get("length_mm", 400)),
            sheet_id=msg.get("sheet"),
        )
        return rail.model_dump()

    def op_delete_rail(self, msg: dict[str, Any]) -> dict[str, Any]:
        rid = str(_domain(msg, "rail_id", default=""))
        self._need().delete_rail(rid, sheet_id=msg.get("sheet"))
        return {"deleted": rid}

    def op_list_rails(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"rails": self._need().list_rails(msg.get("sheet"))}

    def op_set_enclosure(self, msg: dict[str, Any]) -> dict[str, Any]:
        enc = self._need().set_enclosure(
            width_mm=msg.get("width_mm"),
            height_mm=msg.get("height_mm"),
            depth_mm=msg.get("depth_mm"),
            x=msg.get("x"),
            y=msg.get("y"),
            name=msg.get("name"),
            sheet_id=msg.get("sheet"),
        )
        return enc.model_dump()

    def op_layout_from_schematic(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().layout_from_schematic(msg.get("sheet") or msg.get("sheet_id"))

    def op_recommend_cabinet(self, msg: dict[str, Any]) -> dict[str, Any]:
        apply = msg.get("apply")
        if isinstance(apply, str):
            apply = apply.lower() in ("1", "true", "yes")
        return self._need().recommend_cabinet(
            sheet_id=msg.get("sheet") or msg.get("sheet_id"),
            apply=bool(apply),
        )

    def _enclosure_options(self, msg: dict[str, Any]) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        for key in (
            "spare_pct",
            "fill_target",
            "fill_min",
            "fill_max",
            "duct_face_in",
            "duct_depth_in",
            "rail_to_duct_in",
            "wall_gutter_in",
            "airflow_gap_in",
            "ambient_c",
            "max_internal_c",
            "vfd_efficiency",
            "psu_efficiency",
            "transformer_efficiency",
        ):
            if msg.get(key) is not None:
                opts[key] = float(msg[key])
        for key in ("nema", "material", "mount"):
            if msg.get(key) is not None:
                opts[key] = str(msg[key])
        if msg.get("side_ducts") is not None:
            flag = msg.get("side_ducts")
            if isinstance(flag, str):
                flag = flag.lower() in ("1", "true", "yes", "y", "on")
            opts["side_ducts"] = bool(flag)
        if "separate_control_rail" in msg and msg.get("separate_control_rail") is not None:
            flag = msg.get("separate_control_rail")
            if isinstance(flag, str):
                flag = flag.strip().lower() in ("1", "true", "yes", "y", "on")
            opts["separate_control_rail"] = bool(flag)
        return opts

    def op_size_enclosure(self, msg: dict[str, Any]) -> dict[str, Any]:
        apply = msg.get("apply")
        if isinstance(apply, str):
            apply = apply.lower() in ("1", "true", "yes")
        place = msg.get("place_parts") or msg.get("place")
        if isinstance(place, str):
            place = place.lower() in ("1", "true", "yes")
        opts = self._enclosure_options(msg)
        sheet = msg.get("sheet") or msg.get("sheet_id")
        if apply:
            return self._need().apply_enclosure(
                sheet_id=None if sheet is None else str(sheet),
                place_parts=bool(place),
                **opts,
            )
        return self._need().recommend_enclosure(
            sheet_id=None if sheet is None else str(sheet),
            **opts,
        )

    def op_recommend_enclosure(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg = dict(msg)
        msg["apply"] = False
        return self.op_size_enclosure(msg)

    def op_apply_enclosure(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg = dict(msg)
        msg["apply"] = True
        return self.op_size_enclosure(msg)

    def _thermal_fields(self, msg: dict[str, Any]) -> dict[str, Any]:
        opts: dict[str, Any] = {}
        for key in (
            "ambient_c",
            "max_internal_c",
            "min_internal_c",
            "min_ambient_c",
            "component_max_c",
            "altitude_m",
            "vfd_efficiency",
            "psu_efficiency",
            "transformer_efficiency",
        ):
            if key in msg and msg.get(key) is not None:
                opts[key] = float(msg[key])
            elif key in msg and msg.get(key) is None and key in {"min_internal_c", "min_ambient_c"}:
                opts[key] = None
        for key in ("mount", "material", "nema"):
            if key in msg and msg.get(key) is not None:
                opts[key] = str(msg[key])
        if "outdoor" in msg and msg.get("outdoor") is not None:
            flag = msg.get("outdoor")
            if isinstance(flag, str):
                flag = flag.lower() in ("1", "true", "yes")
            opts["outdoor"] = bool(flag)
        if isinstance(msg.get("estimates"), dict):
            opts["estimates"] = msg["estimates"]
        return opts

    def op_compute_heat_load(self, msg: dict[str, Any]) -> dict[str, Any]:
        opts = self._thermal_fields(msg)
        if msg.get("path"):
            opts["path"] = str(msg["path"])
        return self._need().compute_heat_load(**opts)

    def op_set_ambient(self, msg: dict[str, Any]) -> dict[str, Any]:
        opts = self._thermal_fields(msg)
        if not opts:
            raise ValueError("set_ambient needs at least one temperature or mount setting")
        return self._need().set_ambient(**opts)

    def op_wire(self, msg: dict[str, Any]) -> dict[str, Any]:
        w = self._need().wire(
            frm=msg.get("from") or msg.get("frm"),
            to=msg["to"],
            net=msg.get("net"),
            sheet_id=msg.get("sheet"),
            wire_id=msg.get("wire_id"),
            label=msg.get("label"),
            to_sheet=msg.get("to_sheet") or msg.get("dest_sheet"),
            number=msg.get("number"),
            color=msg.get("color"),
            gauge=msg.get("gauge"),
            cable=msg.get("cable"),
            wire_type=msg.get("type") if msg.get("type") is not None else msg.get("wire_type"),
        )
        if isinstance(w, dict):
            return w
        return {
            "id": w.id,
            "from": w.frm,
            "to": w.to,
            "net": w.net,
            "number": w.number,
            "number_locked": bool(w.number_locked),
            "color": w.color,
            "gauge": w.gauge,
            "type": w.wire_type,
            "cable": w.cable,
        }

    def op_cleanup(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().cleanup(msg.get("sheet") or msg.get("sheet_id"))

    def op_page_density(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().page_density(msg.get("sheet") or msg.get("sheet_id"))

    def op_split_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        apply = msg.get("apply")
        if isinstance(apply, str):
            apply = apply.lower() in ("1", "true", "yes", "y", "on")
        return self._need().split_sheet(msg.get("sheet") or msg.get("sheet_id"), apply=bool(apply))

    def op_set_pin_labels(self, msg: dict[str, Any]) -> dict[str, Any]:
        labels = msg.get("labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        comp = self._need().set_pin_labels(msg["ref"], labels, sheet_id=msg.get("sheet"))
        return {"id": comp.id, "pin_labels": comp.properties.get("pin_labels")}

    def op_delete_component(self, msg: dict[str, Any]) -> dict[str, Any]:
        self._need().delete_component(msg["ref"], sheet_id=msg.get("sheet"))
        return {"deleted": msg["ref"]}

    def op_rename_component(self, msg: dict[str, Any]) -> dict[str, Any]:
        old = str(msg.get("ref") or msg.get("old") or "")
        new = str(msg.get("new") or msg.get("name") or "")
        if not old or not new:
            raise ValueError("rename_component needs ref and new")
        return self._need().rename_component(old, new, sheet_id=msg.get("sheet"))

    def op_duplicate_component(self, msg: dict[str, Any]) -> dict[str, Any]:
        comp = self._need().duplicate_component(msg["ref"], sheet_id=msg.get("sheet"))
        return {"id": comp.id, "library_id": comp.library_id, "x": comp.x, "y": comp.y}

    def op_rotate_component(self, msg: dict[str, Any]) -> dict[str, Any]:
        comp = self._need().rotate_component(
            msg["ref"],
            int(msg.get("degrees", 90)),
            sheet_id=msg.get("sheet"),
        )
        return {"id": comp.id, "rotation": comp.rotation}

    def op_delete_wire(self, msg: dict[str, Any]) -> dict[str, Any]:
        wid = str(_domain(msg, "wire_id", default=""))
        self._need().delete_wire(wid, sheet_id=msg.get("sheet"))
        return {"deleted": wid}

    def op_set_wire_label(self, msg: dict[str, Any]) -> dict[str, Any]:
        wire = self._need().set_wire_label(
            wire_id=_domain(msg, "wire_id"),
            label=msg.get("label") or msg.get("text"),
            net=msg.get("net"),
            frm=msg.get("from") or msg.get("frm"),
            to=msg.get("to"),
            sheet_id=msg.get("sheet"),
        )
        return {"id": wire.id, "label": wire.label, "net": wire.net, "caption": wire.caption()}

    def op_set_wire_spec(self, msg: dict[str, Any]) -> dict[str, Any]:
        locked = msg.get("locked")
        if isinstance(locked, str):
            locked = locked.lower() in {"1", "true", "yes"}
        wire = self._need().set_wire_spec(
            wire_id=_domain(msg, "wire_id"),
            frm=msg.get("from") or msg.get("frm"),
            to=msg.get("to"),
            sheet_id=msg.get("sheet"),
            number=msg.get("number"),
            color=msg.get("color"),
            gauge=msg.get("gauge"),
            cable=msg.get("cable"),
            locked=locked,
            wire_type=msg.get("type") if msg.get("type") is not None else msg.get("wire_type"),
        )
        return {
            "id": wire.id,
            "number": wire.number,
            "number_locked": bool(wire.number_locked),
            "color": wire.color,
            "gauge": wire.gauge,
            "type": wire.wire_type,
            "cable": wire.cable,
        }

    def op_number_wires(self, msg: dict[str, Any]) -> dict[str, Any]:
        overwrite = msg.get("overwrite")
        if isinstance(overwrite, str):
            overwrite = overwrite.lower() in {"1", "true", "yes"}
        start = msg.get("start")
        return self._need().number_wires(
            start=int(start if start is not None else 1),
            overwrite=bool(overwrite),
            scheme=msg.get("scheme"),
        )

    def op_set_wire_numbering(self, msg: dict[str, Any]) -> dict[str, Any]:
        enabled = msg.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.lower() in {"1", "true", "yes", "on"}
        start = msg.get("start")
        split = msg.get("split_feedthrough")
        if isinstance(split, str):
            split = split.lower() in {"1", "true", "yes", "on"}
        return self._need().set_wire_numbering(
            scheme=msg.get("scheme"),
            enabled=None if enabled is None else bool(enabled),
            start=None if start is None else int(start),
            split_feedthrough=None if split is None else bool(split),
        )

    def op_wire_numbering(self, msg: dict[str, Any]) -> dict[str, Any]:
        del msg
        return self._need().wire_numbering()

    def op_set_net_wire_spec(self, msg: dict[str, Any]) -> dict[str, Any]:
        wire_type = msg.get("type") if msg.get("type") is not None else msg.get("wire_type")
        return self._need().set_net_wire_spec(
            str(msg.get("net") or ""),
            size=None if msg.get("size") is None else str(msg.get("size")),
            color=None if msg.get("color") is None else str(msg.get("color")),
            wire_type=None if wire_type is None else str(wire_type),
        )

    def op_wire_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        session = self._need()
        sort = str(msg.get("sort") or "number")
        want = _flag(msg.get("from_to")) or str(msg.get("report") or "").lower() in {"from_to", "from-to"}
        legacy = session.wire_list_rows()
        report = session.from_to_rows(sort=sort)
        if msg.get("path"):
            if want:
                written = session.export_from_to(msg["path"], sort=sort)
            else:
                written = session.export_wire_list(msg["path"])
            path = str(written)
        else:
            path = ""
        result: dict[str, Any] = {
            "rows": report if want else legacy,
            "from_to": report,
            "sort": sort,
            "columns": list(_from_to_columns()),
        }
        if path:
            result["path"] = path
        return result

    def op_export_wire_markers(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = msg.get("path")
        if not path:
            raise ValueError("path is required")
        return self._need().export_wire_markers(
            path,
            target=str(msg.get("target") or "generic"),
            style=str(msg.get("style") or "heat-shrink"),
            sort=str(msg.get("sort") or "number"),
        )

    def op_retag(self, msg: dict[str, Any]) -> dict[str, Any]:
        apply = msg.get("apply_prefixes")
        if isinstance(apply, str):
            apply = apply.lower() in {"1", "true", "yes"}
        renamed = self._need().retag(str(msg.get("mode") or "duplicates"), apply_prefixes=bool(apply))
        return {"renamed": renamed}

    def _convert_target(self, msg: dict[str, Any]) -> str:
        # ``id`` is the JSONL correlation token. The drawing standard is ``to`` / ``target``.
        token = _domain(msg, "to", "target", "standard")
        if not token:
            raise ValueError("to is required (iec or nema)")
        return str(token)

    def _convert_sheets(self, msg: dict[str, Any]) -> list[str] | None:
        return _section_list(msg.get("sheets"))

    def _keep_numbers(self, msg: dict[str, Any]) -> bool:
        flag = msg.get("keep_numbers")
        if isinstance(flag, str):
            return flag.lower() in {"1", "true", "yes"}
        return bool(flag)

    def op_convert_standard_plan(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("resume"):
            return self._need().convert_standard_plan("", resume=msg.get("resume"))
        return self._need().convert_standard_plan(
            self._convert_target(msg),
            sheets=self._convert_sheets(msg),
            out=msg.get("out"),
            workspace=msg.get("workspace"),
            keep_numbers=self._keep_numbers(msg),
        )

    def op_convert_standard_next(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().convert_standard_next(msg.get("workspace"))

    def op_convert_standard_apply(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().convert_standard_apply(msg.get("workspace"), out=msg.get("out"))

    def op_convert_standard(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().convert_standard(
            self._convert_target(msg),
            sheets=self._convert_sheets(msg),
            out=msg.get("out"),
            workspace=msg.get("workspace"),
            keep_numbers=self._keep_numbers(msg),
        )

    def op_convert_standard_review(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().convert_standard_review(msg.get("workspace"))

    def op_convert_standard_accept(self, msg: dict[str, Any]) -> dict[str, Any]:
        chunk = _domain(msg, "chunk", "chunk_id")
        if not chunk:
            raise ValueError("chunk is required")
        return self._need().convert_standard_accept(str(chunk), msg.get("workspace"))

    def op_convert_standard_rerun(self, msg: dict[str, Any]) -> dict[str, Any]:
        chunk = _domain(msg, "chunk", "chunk_id")
        if not chunk:
            raise ValueError("chunk is required")
        return self._need().convert_standard_rerun(str(chunk), msg.get("workspace"))

    def op_convert_standard_revert(self, msg: dict[str, Any]) -> dict[str, Any]:
        chunk = _domain(msg, "chunk", "chunk_id")
        if not chunk:
            raise ValueError("chunk is required")
        return self._need().convert_standard_revert(str(chunk), msg.get("workspace"))

    def op_cross_reference(self, msg: dict[str, Any]) -> dict[str, Any]:
        write = msg.get("write")
        if isinstance(write, str):
            write = write.lower() in {"1", "true", "yes"}
        session = self._need()
        if write:
            rows = session.update_cross_references()
        else:
            rows = session.cross_reference_rows()
        report = session.cross_reference_report()
        result: dict[str, Any] = {"rows": rows, "report": report}
        if msg.get("path"):
            result["path"] = str(session.export_cross_reference(msg["path"]))
        return result

    def op_update_cross_references(self, msg: dict[str, Any]) -> dict[str, Any]:
        del msg
        rows = self._need().update_cross_references()
        return {"rows": rows, "report": self._need().cross_reference_report()}

    def op_export_cross_reference(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_cross_reference(msg["path"])
        return {"path": str(path)}

    def op_set_cross_reference(self, msg: dict[str, Any]) -> dict[str, Any]:
        enabled = msg.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.lower() in {"1", "true", "yes", "on"}
        pitch = msg.get("rung_pitch_mm")
        if pitch is not None:
            try:
                pitch = float(pitch)
            except (TypeError, ValueError):
                pitch = None
        return self._need().set_cross_reference(
            nc_mark=msg.get("nc_mark"),
            enabled=None if enabled is None else bool(enabled),
            rung_pitch_mm=pitch,
            rung_numbers=msg.get("rung_numbers"),
        )

    def op_cross_reference_settings(self, msg: dict[str, Any]) -> dict[str, Any]:
        del msg
        return self._need().cross_reference_settings()

    def op_device_index(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = msg.get("ref") or msg.get("device") or msg.get("tag")
        return self._need().device_index(None if ref in (None, "") else str(ref))

    def op_lookup_zone(self, msg: dict[str, Any]) -> dict[str, Any]:
        zone = msg.get("zone")
        if zone in (None, ""):
            zone = msg.get("location") or msg.get("at") or ""
        sheet = msg.get("sheet") or msg.get("sheet_id")
        return self._need().lookup_zone(str(zone), sheet=None if sheet in (None, "") else str(sheet))

    def op_trace_wire(self, msg: dict[str, Any]) -> dict[str, Any]:
        number = msg.get("number")
        if number in (None, ""):
            number = msg.get("wire") or msg.get("wire_number") or ""
        return self._need().trace_wire("" if number is None else str(number))

    def op_circuit_for(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = msg.get("ref") or msg.get("device") or msg.get("tag") or ""
        return self._need().circuit_for(str(ref))

    def op_checkpoints(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = msg.get("ref") or msg.get("device") or msg.get("tag") or ""
        symptom = msg.get("symptom") or ""
        return self._need().checkpoints(str(ref), str(symptom))

    def op_upstream(self, msg: dict[str, Any]) -> dict[str, Any]:
        endpoint = msg.get("endpoint") or msg.get("pin") or msg.get("at") or ""
        return self._need().upstream(str(endpoint))

    def op_downstream(self, msg: dict[str, Any]) -> dict[str, Any]:
        endpoint = msg.get("endpoint") or msg.get("pin") or msg.get("at") or ""
        return self._need().downstream(str(endpoint))

    def op_plc_point(self, msg: dict[str, Any]) -> dict[str, Any]:
        address = msg.get("address") or msg.get("tag") or msg.get("point") or ""
        return self._need().plc_point(str(address))

    def op_likely_causes(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = msg.get("ref") or msg.get("device") or msg.get("tag") or ""
        symptom = msg.get("symptom") or ""
        return self._need().likely_causes(str(ref), str(symptom), _readings_arg(msg))

    def op_troubleshoot_guide(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = msg.get("ref") or msg.get("device") or msg.get("tag") or ""
        symptom = msg.get("symptom") or ""
        return self._need().troubleshoot_guide(str(ref), str(symptom), _readings_arg(msg))

    def op_export_troubleshoot_csv(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._export_troubleshoot_session(msg, "csv")

    def op_export_troubleshoot_pdf(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._export_troubleshoot_session(msg, "pdf")

    def _export_troubleshoot_session(self, msg: dict[str, Any], kind: str) -> dict[str, Any]:
        path = msg.get("path")
        if not path:
            raise ValueError("path is required")
        ref = msg.get("ref") or msg.get("device") or msg.get("tag") or ""
        symptom = msg.get("symptom") or ""
        kwargs = {
            "ref": str(ref),
            "symptom": str(symptom),
            "readings": _readings_arg(msg),
            "who": str(msg.get("who") or ""),
            "note": str(msg.get("note") or ""),
        }
        if kind == "pdf":
            dest = self._need().export_troubleshoot_pdf(path, **kwargs)
        else:
            dest = self._need().export_troubleshoot_csv(path, **kwargs)
        return {"path": str(dest), "format": kind}

    def op_export_troubleshoot(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = msg.get("path")
        if not path:
            raise ValueError("path is required")
        dest = self._need().export_troubleshoot(path)
        return {"path": str(dest), "format": "panelcad-troubleshoot"}

    def op_append_troubleshoot_log(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = msg.get("ref") or msg.get("device") or msg.get("tag") or ""
        symptom = msg.get("symptom") or ""
        project_path = msg.get("project_path") or msg.get("project")
        return self._need().append_troubleshoot_log(
            ref=str(ref),
            symptom=str(symptom),
            readings=_readings_arg(msg),
            who=str(msg.get("who") or ""),
            note=str(msg.get("note") or ""),
            when=str(msg.get("when") or ""),
            project_path=None if project_path in (None, "") else project_path,
        )

    def op_terminal_strips(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("path"):
            path = self._need().export_terminal_strips(msg["path"])
            return {"path": str(path), "rows": self._need().terminal_strip_rows()}
        return {"rows": self._need().terminal_strip_rows()}

    def op_sequence_terminals(self, msg: dict[str, Any]) -> dict[str, Any]:
        rename = msg.get("rename")
        if isinstance(rename, str):
            rename = rename.lower() in {"1", "true", "yes"}
        rows = self._need().sequence_terminal_strip(str(msg.get("strip") or ""), rename=bool(rename))
        return {"rows": rows}

    def op_jumper_terminals(self, msg: dict[str, Any]) -> dict[str, Any]:
        refs = msg.get("refs") or msg.get("terminals") or []
        if isinstance(refs, str):
            refs = [part.strip() for part in refs.split(",") if part.strip()]
        return self._need().jumper_terminals(list(refs), group=msg.get("group"))

    def op_clear_terminal_jumpers(self, msg: dict[str, Any]) -> dict[str, Any]:
        refs = msg.get("refs") or msg.get("terminals") or []
        if isinstance(refs, str):
            refs = [part.strip() for part in refs.split(",") if part.strip()]
        group = msg.get("group")
        return self._need().clear_terminal_jumpers(refs=list(refs) or None, group=group)

    def op_list_terminal_strips(self, msg: dict[str, Any]) -> dict[str, Any]:
        del msg
        return {"strips": self._need().list_terminal_strips()}

    def op_open_terminal_strip(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().open_terminal_strip(str(msg.get("strip") or ""))

    def op_add_terminal(self, msg: dict[str, Any]) -> dict[str, Any]:
        library_id = str(msg.get("library_id") or "iec.terminals.x_2p5")
        after = msg.get("after")
        if after is not None and not str(after).strip():
            after = None
        return self._need().add_terminal(str(msg.get("strip") or ""), library_id=library_id, after=after)

    def op_reorder_terminals(self, msg: dict[str, Any]) -> dict[str, Any]:
        refs = msg.get("refs") or msg.get("terminals") or []
        if isinstance(refs, str):
            refs = [part.strip() for part in refs.split(",") if part.strip()]
        return self._need().reorder_terminals(str(msg.get("strip") or ""), list(refs))

    def op_move_terminal(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().move_terminal(
            str(msg.get("strip") or ""),
            str(msg.get("ref") or ""),
            str(msg.get("direction") or "later"),
        )

    def op_retag_terminal_strip(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().retag_terminal_strip(str(msg.get("strip") or ""))

    def op_add_strip_accessory(self, msg: dict[str, Any]) -> dict[str, Any]:
        after = msg.get("after")
        if after is not None and not str(after).strip():
            after = None
        return self._need().add_strip_accessory(
            str(msg.get("strip") or ""),
            str(msg.get("kind") or ""),
            after=after,
        )

    def op_delete_strip_component(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().delete_strip_component(str(msg.get("ref") or ""))

    def op_export_terminal_strip(self, msg: dict[str, Any]) -> dict[str, Any]:
        raw_format = msg.get("format")
        file_format = str(raw_format) if raw_format not in (None, "") else None
        return self._need().export_terminal_strip(
            msg["path"],
            str(msg.get("strip") or ""),
            vendor=str(msg.get("vendor") or "phoenix"),
            file_format=file_format,
        )

    def op_audit_panel(self, msg: dict[str, Any]) -> dict[str, Any]:
        rows = self._need().audit_schematic_panel()
        if msg.get("path"):
            self._need().export_audit(msg["path"])
        return {"rows": rows, "count": len(rows)}

    def op_export_audit(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_audit(msg["path"])
        return {"path": str(path)}

    def op_check_control_power(self, msg: dict[str, Any]) -> dict[str, Any]:
        session = self._need()
        margin = msg.get("margin")
        if margin not in (None, ""):
            margin = float(margin)
        else:
            margin = None
        supply = msg.get("supply") or msg.get("ref")
        if supply is not None:
            supply = str(supply)
        report = session.power_budget(supply=supply or None, margin=margin)
        if msg.get("path"):
            session.export_power_budget(msg["path"], supply=supply or None, margin=margin)
            report["path"] = str(msg["path"])
        if msg.get("pdf"):
            session.export_power_budget_pdf(msg["pdf"], supply=supply or None, margin=margin)
            report["pdf"] = str(msg["pdf"])
        return report

    def op_size_motor_circuit(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.core.motor_apply import apply_sizing_to_motor
        from panelcad.core.motor_sizing import size_motor_circuit

        def _num(*keys: str) -> float | None:
            for key in keys:
                if key not in msg or msg[key] in (None, ""):
                    continue
                return float(msg[key])
            return None

        phases = msg.get("phases", 3)
        temp = msg.get("temp_c", msg.get("temp"))
        marked = msg.get("termination_c", temp)
        result = size_motor_circuit(
            hp=_num("hp"),
            kw=_num("kw", "kW"),
            volts=_num("volts", "voltage"),
            phases=int(phases or 3),
            fla=_num("fla", "FLA"),
            standard=str(msg.get("standard") or "NEMA/ANSI"),
            device=str(msg.get("device") or "inverse_time_breaker"),
            motor_type=msg.get("motor_type"),
            wire_table=str(msg.get("wire_table") or "auto"),
            temp_c=None if marked in (None, "") else int(marked),
            service_factor=_num("service_factor"),
            temp_rise_c=_num("temp_rise_c"),
            allow_nearest_hp=bool(msg.get("allow_nearest_hp")),
            overload_factor=float(msg.get("overload_factor") or 1.0),
        )
        if msg.get("apply"):
            ref = str(msg.get("ref") or msg.get("motor") or "").strip()
            if not ref:
                raise ValueError("apply requires ref")
            result = {**result, "applied": apply_sizing_to_motor(self._need(), ref, result)}
        return result

    def op_check_sizing(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().check_sizing()

    def op_design_checks(self, msg: dict[str, Any]) -> dict[str, Any]:
        include = bool(msg.get("include_suppressed"))
        rows = self._need().design_checks(include_suppressed=include)
        if msg.get("path"):
            self._need().export_checks(msg["path"], include_suppressed=include)
        counts = {"error": 0, "warning": 0, "info": 0}
        for row in rows:
            sev = str(row.get("severity") or "")
            if sev in counts:
                counts[sev] += 1
        return {"rows": rows, "count": len(rows), **counts}

    def op_export_checks(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_checks(msg["path"], include_suppressed=bool(msg.get("include_suppressed")))
        return {"path": str(path)}

    def op_suppress_check(self, msg: dict[str, Any]) -> dict[str, Any]:
        finding_id = str(msg.get("finding_id") or msg.get("key") or "").strip()
        if not finding_id:
            check = str(msg.get("check") or "").strip()
            sheet = str(msg.get("sheet") or "").strip()
            ref = str(msg.get("ref") or "").strip()
            target = str(msg.get("target") or "").strip()
            if check:
                finding_id = f"{check}:{sheet}:{ref}:{target}"
        return self._need().suppress_finding(finding_id)

    def op_unsuppress_check(self, msg: dict[str, Any]) -> dict[str, Any]:
        finding_id = str(msg.get("finding_id") or msg.get("key") or "").strip()
        if not finding_id:
            check = str(msg.get("check") or "").strip()
            sheet = str(msg.get("sheet") or "").strip()
            ref = str(msg.get("ref") or "").strip()
            target = str(msg.get("target") or "").strip()
            if check:
                finding_id = f"{check}:{sheet}:{ref}:{target}"
        return self._need().unsuppress_finding(finding_id)

    def op_io_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        rows = self._need().io_rows()
        if msg.get("path"):
            self._need().export_io_list(msg["path"])
        return {"rows": rows}

    def op_io_add_module(self, msg: dict[str, Any]) -> dict[str, Any]:
        module = str(msg.get("module") or msg.get("module_id") or "").strip()
        if not module:
            raise ValueError("module is required")
        base = msg.get("address_base")
        return self._need().io_add_module(
            module,
            ref=msg.get("ref") or None,
            slot=int(msg.get("slot") or 1),
            position=int(msg.get("position") or 1),
            polarity=str(msg.get("polarity") or ""),
            address_base=None if base in (None, "") else int(base),
        )

    def op_io_assign_point(self, msg: dict[str, Any]) -> dict[str, Any]:
        module = str(msg.get("module") or msg.get("ref") or "").strip()
        pin = str(msg.get("pin") or msg.get("address") or "").strip()
        if not module or not pin:
            raise ValueError("module and pin are required")
        return self._need().io_assign_point(
            module,
            pin,
            tag=msg.get("tag"),
            description=msg.get("description"),
            field=msg.get("field"),
        )

    def op_io_generate_sheets(self, msg: dict[str, Any]) -> dict[str, Any]:
        modules = msg.get("modules")
        if isinstance(modules, str):
            modules = [part.strip() for part in modules.split(",") if part.strip()]
        fuses = msg.get("fuses")
        return self._need().io_generate_sheets(
            list(modules) if modules else None,
            fuses=None if fuses is None else bool(fuses),
        )

    def op_plc_io_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        rows = self._need().io_rows()
        payload: dict[str, Any] = {"rows": rows, "count": len(rows)}
        if msg.get("path"):
            payload["path"] = str(self._need().export_io_list(msg["path"]))
        return payload

    def op_export_plc_tags(self, msg: dict[str, Any]) -> dict[str, Any]:
        opts = validate_export_plc_tags(msg)
        kwargs: dict[str, Any] = {
            "target": opts["target"],
            "prefix": opts["prefix"],
            "name_from": opts["name_from"],
            "logix_style": opts["logix_style"],
            "logix_slot": opts["logix_slot"],
        }
        if opts["analog_start"] != "project":
            kwargs["analog_start"] = opts["analog_start"]
        return self._need().export_plc_tags(opts["path"], **kwargs)

    def op_import_io(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().import_io_list(msg["path"])

    def op_export_io(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_io_list(msg["path"])
        return {"path": str(path)}

    def op_assign_item_numbers(self, msg: dict[str, Any]) -> dict[str, Any]:
        count = self._need().assign_item_numbers()
        return {"count": count}

    def op_cable_schedule(self, msg: dict[str, Any]) -> dict[str, Any]:
        rows = self._need().cable_rows()
        if msg.get("path"):
            self._need().export_cable_schedule(msg["path"])
        return {"rows": rows, "cables": self._need().list_cables()}

    def op_propose_cables(self, msg: dict[str, Any]) -> dict[str, Any]:
        spares = msg.get("spares", None)
        if spares is None or spares == "":
            spare_n = None
        else:
            spare_n = int(spares)
        show = msg.get("show_tags", True)
        if isinstance(show, str):
            show = show.strip().lower() not in {"0", "false", "no"}
        return self._need().propose_cables(
            spares=spare_n,
            replace=bool(msg.get("replace")) and str(msg.get("replace")).lower() not in {"0", "false", "no"},
            show_tags=bool(show),
            route=msg.get("route"),
        )

    def op_add_cable(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().add_cable(**_cable_fields(msg))

    def op_update_cable(self, msg: dict[str, Any]) -> dict[str, Any]:
        tag = str(msg.get("tag") or "").strip()
        if not tag:
            raise ValueError("tag is required")
        return self._need().update_cable(tag, **_cable_fields(msg))

    def op_assign_cable_conductors(self, msg: dict[str, Any]) -> dict[str, Any]:
        tag = str(msg.get("tag") or "").strip()
        rows = msg.get("conductors") or msg.get("assignments") or []
        if not isinstance(rows, list):
            raise ValueError("conductors must be a list")
        return self._need().assign_cable_conductors(tag, rows)

    def op_export_cable_schedule(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_cable_schedule(msg["path"])
        result: dict[str, Any] = {"path": str(path)}
        if msg.get("pull"):
            result["pull"] = str(self._need().export_cable_pull_list(msg["pull"]))
        if msg.get("terminations"):
            result["terminations"] = str(self._need().export_cable_terminations(msg["terminations"]))
        return result

    def op_panel_wire_annotation(self, msg: dict[str, Any]) -> dict[str, Any]:
        rows = self._need().panel_wire_annotations()
        if msg.get("path"):
            self._need().export_panel_wires(msg["path"])
        return {"rows": rows}

    def op_set_wire_route(self, msg: dict[str, Any]) -> dict[str, Any]:
        sheet = msg.get("sheet")
        wid = str(msg.get("wire_id") or "").strip()
        if not wid:
            found = self._need().find_wire(
                frm=msg.get("from") or msg.get("frm"),
                to=msg.get("to"),
                sheet_id=sheet,
            )
            wid = found.id
        wire = self._need().set_wire_route(wid, msg.get("points") or [], sheet_id=sheet)
        return {"id": wire.id, "points": len(wire.points)}

    def op_save(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().save(msg.get("path"))
        return {"path": str(path)}

    def op_export_svg(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_svg(msg["path"], sheet_id=msg.get("sheet"))
        return {"path": str(path)}

    def op_export_pdf(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_pdf(msg["path"], sheet_id=msg.get("sheet"))
        return {"path": str(path)}

    def op_export_dxf(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_dxf(msg["path"], sheet_id=msg.get("sheet"))
        return {"path": str(path)}

    def op_cutouts_generate(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().cutouts_generate(_cutout_options(msg), persist=bool(msg.get("persist")))

    def op_cutouts_export_dxf(self, msg: dict[str, Any]) -> dict[str, Any]:
        if not msg.get("path"):
            raise ValueError("cutouts_export_dxf needs path")
        return self._need().cutouts_export_dxf(msg["path"], _cutout_options(msg))

    def op_cutouts_export_pdf(self, msg: dict[str, Any]) -> dict[str, Any]:
        if not msg.get("path"):
            raise ValueError("cutouts_export_pdf needs path")
        path = self._need().cutouts_export_pdf(msg["path"], _cutout_options(msg))
        return {"path": str(path)}

    def op_part_set_cutout(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = str(_domain(msg, "ref") or "").strip()
        if not ref:
            raise ValueError("part_set_cutout needs ref")
        from panelcad.core.cutout import resolve_centers_interchangeable

        unknown = _json_bool(msg["unknown"]) if "unknown" in msg else None
        include_key = _json_bool(msg["include_key"]) if "include_key" in msg else None
        interchangeable = (
            _json_bool(msg["centers_interchangeable"]) if "centers_interchangeable" in msg else None
        )
        pitch_swap = _json_bool(msg["pitch_swap"]) if "pitch_swap" in msg else None
        interchangeable = resolve_centers_interchangeable(pitch_swap, interchangeable)
        return self._need().part_set_cutout(
            ref,
            msg.get("cutout") if isinstance(msg.get("cutout"), dict) else None,
            sheet_id=msg.get("sheet"),
            unknown=unknown,
            clear=bool(_json_bool(msg["clear"]) if "clear" in msg else False),
            surface=msg.get("surface"),
            include_key=include_key,
            min_center_x_mm=msg.get("min_center_x_mm"),
            min_center_y_mm=msg.get("min_center_y_mm"),
            centers_interchangeable=interchangeable,
        )

    def op_export_bom(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_bom(msg["path"], grouped=bool(msg.get("grouped")))
        return {"path": str(path)}

    def op_quote_build(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().quote_build(today=msg.get("today"))

    def op_quote_set_rates(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = {
            key: msg[key]
            for key in (
                "scope",
                "labor",
                "shop_rates",
                "manual",
                "clear_project_labor",
                "number",
                "customer",
                "project",
                "currency",
                "price_mode",
                "preferred_vendor",
                "parts_markup_percent",
                "labor_markup_percent",
                "category_markups",
                "tax_percent",
                "freight",
                "contingency_percent",
                "stale_after_days",
            )
            if key in msg
        }
        return self._need().quote_set_rates(**fields)

    def op_quote_import_prices(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().quote_import_prices(
            msg["path"],
            price_mode=msg.get("price_mode"),
            preferred_vendor=msg.get("preferred_vendor"),
        )

    def op_quote_export(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().quote_export(
            msg["path"],
            fmt=msg.get("format") or msg.get("fmt"),
            view=str(msg.get("view") or "internal"),
            today=msg.get("today"),
        )

    def op_import_bom(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().import_bom(msg["path"], place_missing=msg.get("place_missing", True))

    def op_export_wire_list(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_wire_list(msg["path"])
        return {"path": str(path)}

    def op_import_wires(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().import_wires(msg["path"])

    def op_export_json(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_json(msg["path"])
        return {"path": str(path)}

    def op_export_package(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = self._need().export_package(
            msg["path"],
            pdf=msg.get("pdf", True),
            paper=str(msg.get("paper") or "mixed"),
            sections=_section_list(msg.get("sections")),
        )
        return {"path": str(path)}

    def op_print_package(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().print_package(
            msg["path"],
            paper=str(msg.get("paper") or "mixed"),
            sections=_section_list(msg.get("sections")),
        )

    def op_import_any(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().import_any(msg["path"], replace=bool(msg.get("replace")))

    def op_import_library(self, msg: dict[str, Any]) -> dict[str, Any]:
        ids = self._need().import_library(msg["path"])
        return {"devices": ids}

    def op_import_sheet(self, msg: dict[str, Any]) -> dict[str, Any]:
        sid = self._need().import_sheet(msg["path"])
        return {"id": sid}

    def _import_job(self):
        if self.print_import is None:
            raise RuntimeError("no print import; call import_start first")
        return self.print_import

    def op_import_start(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.importer.pipeline import ImportJob

        resume = msg.get("resume") or msg.get("workspace")
        paths = msg.get("paths") or msg.get("path") or msg.get("sources")
        if isinstance(paths, str):
            paths = [paths]
        backend = str(msg.get("backend") or "external")
        vision = _vision_from_message(msg)
        if vision is not None and vision.preset != "none":
            backend = vision.preset
        elif msg.get("vision_backend"):
            backend = "raster"
        if resume and not paths:
            job = ImportJob.resume(
                resume,
                backend=backend,
                model=msg.get("vision_model"),
                vision=vision,
                truth=msg.get("truth"),
            )
        else:
            if not paths:
                raise ValueError("import_start needs path or resume")
            workspace = msg.get("workspace") or msg.get("resume") or "import-workspace"
            job = ImportJob.start(
                list(paths),
                workspace,
                photos=msg.get("photos"),
                standard=msg.get("standard"),
                backend=backend,
                model=msg.get("vision_model"),
                vision=vision,
                dpi=msg.get("dpi"),
                truth=msg.get("truth"),
            )
        self.print_import = job
        return job.status()

    def op_import_next(self, msg: dict[str, Any]) -> dict[str, Any]:
        job = self._import_job()
        job.pump()
        return job.status()

    def op_import_submit(self, msg: dict[str, Any]) -> dict[str, Any]:
        data = msg.get("data")
        if data is None:
            data = msg.get("extraction") or msg.get("result")
        if not isinstance(data, dict):
            raise ValueError("import_submit needs data{}")
        work_id = str(msg.get("work_id") or msg.get("work") or "")
        if not work_id:
            work_id = str(self._import_job().manifest.get("pending") or "")
        return self._import_job().submit(work_id, data)

    def op_import_status(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._import_job().status()

    def op_import_build(self, msg: dict[str, Any]) -> dict[str, Any]:
        job = self._import_job()
        session = job.build(out=msg.get("path"), name=str(msg.get("name") or ""))
        self.session = session
        return {
            "path": job.manifest.get("project"),
            "name": session.project.name,
            "sheets": len(session.project.sheets),
            "stage": job.manifest.get("stage"),
            "summary": job.summary(),
        }

    def op_import_agent_prep(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.importer.agent_exchange import DEFAULT_DPI, prepare_print

        paths = msg.get("paths") or msg.get("path") or msg.get("sources")
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            raise ValueError("import_agent_prep needs path")
        out = msg.get("out") or msg.get("dest")
        if not out:
            raise ValueError("import_agent_prep needs out")
        dpi = msg.get("dpi")
        return prepare_print(paths, out, dpi=DEFAULT_DPI if dpi is None else float(dpi))

    def op_import_agent_apply(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.importer.agent_exchange import apply_response

        path = msg.get("path")
        if not path:
            raise ValueError("import_agent_apply needs path")
        report = apply_response(path, out=msg.get("out"), prep=msg.get("prep"))
        if report.get("ok") and report.get("project"):
            self.session = Session.open(report["project"])
        return report

    def op_import_agent_answer(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.importer.agent_questions import apply_answer, apply_answers

        path = msg.get("path")
        if path:
            self.session = Session.open(path)
        if self.session is None:
            raise ValueError("import_agent_answer needs an open project or path")
        answers = msg.get("answers")
        if isinstance(answers, list):
            return apply_answers(self.session, answers)
        return apply_answer(self.session, str(msg.get("id") or ""), str(msg.get("value") or ""))

    def op_import_diagnostics(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.importer.diagnostics import load_run

        truth = msg.get("truth")
        run = msg.get("run") or msg.get("path")
        job = self.print_import
        if job is not None and not run:
            if truth and job.diagnostics is not None:
                return job.diagnostics.score_truth(truth)
            if job.diagnostics is not None:
                return job.diagnostics.public()
        if self.session is not None:
            return self.session.import_diagnostics(run, truth=truth)
        if not run:
            raise RuntimeError("import_diagnostics needs a run or an open import")
        return load_run(run, truth=truth)

    def op_import_add_photos(self, msg: dict[str, Any]) -> dict[str, Any]:
        paths = msg.get("paths") or msg.get("path") or msg.get("dir")
        if not paths:
            raise ValueError("import_add_photos needs path")
        return self._import_job().add_photos(paths)

    def op_library_search(self, msg: dict[str, Any]) -> dict[str, Any]:
        session = self.session or Session.create("tmp")
        items = session.catalog.search(str(msg.get("query", "")), standard=msg.get("standard"))
        return {"devices": [_catalog_hit(d) for d in items]}

    def op_parts_search(self, msg: dict[str, Any]) -> dict[str, Any]:
        session = self.session or Session.create("tmp")
        return {
            "parts": session.search_parts(
                str(msg.get("query", "")),
                manufacturer=msg.get("manufacturer"),
                category=msg.get("category"),
                library_id=msg.get("library_id"),
                amps_min=_opt_float(msg.get("amps_min")),
                amps_max=_opt_float(msg.get("amps_max")),
            )
        }

    def op_assign_part(self, msg: dict[str, Any]) -> dict[str, Any]:
        catalog = str(msg.get("catalog") or msg.get("part_number") or "").strip()
        if not catalog:
            raise ValueError("assign_part needs catalog")
        return self._need().assign_part(
            str(_domain(msg, "ref")),
            catalog,
            manufacturer=msg.get("manufacturer"),
            sheet_id=msg.get("sheet"),
        )

    def op_save_library_device(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().save_library_device(msg)

    def op_list_components(self, msg: dict[str, Any]) -> dict[str, Any]:
        return {"components": self._need().list_components(msg.get("sheet"))}

    def op_validate(self, msg: dict[str, Any]) -> dict[str, Any]:
        errors = self._need().validate()
        return {"ok": not errors, "errors": errors}

    def op_issue_revision(self, msg: dict[str, Any]) -> dict[str, Any]:
        clear = msg.get("clear_clouds") if "clear_clouds" in msg else None
        clouds = msg.get("clouds") if "clouds" in msg else msg.get("auto_clouds", True)
        return self._need().issue_revision(
            description=str(msg.get("description") or ""),
            rev=(str(msg.get("rev")).strip() or None) if msg.get("rev") not in (None, "") else None,
            scheme=msg.get("scheme"),
            date=msg.get("date"),
            drawn=str(msg.get("drawn") or ""),
            checked=str(msg.get("checked") or ""),
            approved=str(msg.get("approved") or ""),
            clear_clouds=_flag(clear, None),
            auto_clouds=bool(_flag(clouds, True)),
        )

    def op_list_revisions(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().list_revisions()

    def op_compare_revisions(self, msg: dict[str, Any]) -> dict[str, Any]:
        left = msg.get("left") if msg.get("left") not in (None, "") else msg.get("from_rev")
        right = msg.get("right") if msg.get("right") not in (None, "") else msg.get("to_rev")
        if left in (None, "") or right in (None, ""):
            raise ValueError("compare_revisions needs left and right (a revision or 'working')")
        return self._need().compare_revisions(
            str(left),
            str(right),
            csv_path=msg.get("path") or msg.get("csv"),
            pdf_path=msg.get("pdf"),
        )

    def op_add_revision_cloud(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().add_revision_cloud(
            float(msg["x"]),
            float(msg["y"]),
            float(msg.get("w") if msg.get("w") is not None else msg.get("width")),
            float(msg.get("h") if msg.get("h") is not None else msg.get("height")),
            sheet_id=msg.get("sheet"),
            rev=str(msg.get("rev") or ""),
        )

    def op_set_title_block(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in msg.items() if k not in ("id", "op")}
        return self._need().set_title_block(**fields)

    def op_compute_sccr(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().compute_sccr()

    def op_set_available_fault(self, msg: dict[str, Any]) -> dict[str, Any]:
        ka = msg.get("ka", msg.get("available_fault_ka", msg.get("available_ka")))
        return self._need().set_available_fault(
            None if ka is None or ka == "" else float(ka),
            voltage=_opt_float(msg.get("voltage")),
            sccr_voltage=_opt_float(msg.get("sccr_voltage")),
        )

    def op_set_part_sccr(self, msg: dict[str, Any]) -> dict[str, Any]:
        ref = str(_domain(msg, "ref") or "").strip()
        if not ref:
            raise ValueError("set_part_sccr needs ref")
        fields = {
            key: msg[key]
            for key in (
                "sccr",
                "ir_ka",
                "fuse_class",
                "fuse_amps",
                "circuit",
                "current_limiting",
                "let_through_ka",
                "let_through_at_ka",
                "hp",
                "kva",
                "secondary_voltage",
                "secondary_voltage_ceiling",
                "phases",
                "impedance_pct",
                "transformer_isc_ka",
                "sccr_with_class",
                "sccr_with_max_amps",
                "ul_type",
                "gfci",
                "volts",
                "voltage",
                "amps",
                "kva",
            )
            if key in msg and msg[key] is not None
        }
        sccr_ka = msg.get("sccr_ka")
        return self._need().set_part_sccr(
            ref,
            None if sccr_ka is None or sccr_ka == "" else float(sccr_ka),
            **fields,
        )

    def op_update_check(self, msg: dict[str, Any]) -> dict[str, Any]:
        from panelcad.update.service import check_for_update

        feed = msg.get("feed")
        return check_for_update(feed=str(feed) if feed else None)

    def op_update_install(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("confirm") is not True:
            raise ValueError("update_install requires confirm=true")
        expected = msg.get("expected_version")
        sha = msg.get("expected_sha256") or msg.get("sha256")
        return self._update_session().update_install(
            confirm=True,
            feed=str(msg["feed"]) if msg.get("feed") else None,
            dry_run=msg.get("dry_run") is True,
            expected_version=str(expected) if expected else None,
            expected_sha256=str(sha) if sha else None,
        )

    def op_update_rollback(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("confirm") is not True:
            raise ValueError("update_rollback requires confirm=true")
        return self._update_session().update_rollback(
            confirm=True, dry_run=msg.get("dry_run") is True
        )

    def _update_session(self) -> Session:
        if self.session is None:
            self.session = Session.create("Untitled Panel")
        return self.session

    def op_phase_colors(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().phase_colors()

    def op_set_phase_colors(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = {k: v for k, v in msg.items() if k not in ("id", "op")}
        return self._need().set_phase_colors(**fields)

    def op_reconstruct_add_photos(self, msg: dict[str, Any]) -> dict[str, Any]:
        paths = msg.get("paths") or msg.get("path")
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            raise ValueError("reconstruct_add_photos needs paths or path")
        return self._need().reconstruct_add_photos(list(paths), role=str(msg.get("role") or "other"), note=str(msg.get("note") or ""))

    def op_reconstruct_tag_photo(self, msg: dict[str, Any]) -> dict[str, Any]:
        photo_id = str(msg.get("photo_id") or "").strip()
        if not photo_id:
            raise ValueError("reconstruct_tag_photo needs photo_id")
        return self._need().reconstruct_tag_photo(photo_id, str(msg.get("role") or "other"), note=msg.get("note"))

    def op_reconstruct_list_photos(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_list_photos()

    def op_reconstruct_set_corners(self, msg: dict[str, Any]) -> dict[str, Any]:
        photo_id = str(msg.get("photo_id") or "").strip()
        corners = msg.get("corners")
        if not photo_id or not isinstance(corners, list):
            raise ValueError("reconstruct_set_corners needs photo_id and corners")
        return self._need().reconstruct_set_corners(photo_id, corners)

    def op_reconstruct_set_scale(self, msg: dict[str, Any]) -> dict[str, Any]:
        photo_id = str(msg.get("photo_id") or "").strip()
        if not photo_id:
            raise ValueError("reconstruct_set_scale needs photo_id")
        return self._need().reconstruct_set_scale(
            photo_id,
            str(msg.get("kind") or ""),
            msg.get("p1") or msg.get("from"),
            msg.get("p2") or msg.get("to"),
            known_mm=_opt_float(msg.get("known_mm")),
            known_height_mm=_opt_float(msg.get("known_height_mm") or msg.get("face_height_mm")),
            face_width_mm=_opt_float(msg.get("face_width_mm")),
            face_height_mm=_opt_float(msg.get("face_height_mm")),
            backpanel=bool(msg.get("backpanel")),
            catalog=str(msg.get("catalog") or ""),
            manufacturer=str(msg.get("manufacturer") or ""),
            dimension=str(msg.get("dimension") or "width"),
            note=str(msg.get("note") or ""),
            slot=msg.get("slot"),
            replace=msg.get("replace"),
        )

    def op_reconstruct_clear_scale(self, msg: dict[str, Any]) -> dict[str, Any]:
        photo_id = str(msg.get("photo_id") or "").strip()
        if not photo_id:
            raise ValueError("reconstruct_clear_scale needs photo_id")
        return self._need().reconstruct_clear_scale(photo_id)

    def op_reconstruct_identify(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_identify(msg.get("photo_id"))

    def op_reconstruct_confirm_item(self, msg: dict[str, Any]) -> dict[str, Any]:
        item_id = str(msg.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("reconstruct_confirm_item needs item_id")
        return self._need().reconstruct_confirm_item(
            item_id,
            catalog=str(msg.get("catalog") or ""),
            manufacturer=str(msg.get("manufacturer") or ""),
            envelope_w_mm=_opt_float(msg.get("envelope_w_mm") or msg.get("width_mm")),
            envelope_h_mm=_opt_float(msg.get("envelope_h_mm") or msg.get("height_mm")),
            envelope_d_mm=_opt_float(msg.get("envelope_d_mm") or msg.get("depth_mm")),
            mount=str(msg.get("mount") or ""),
            tag=str(msg.get("tag") or ""),
            library_id=str(msg.get("library_id") or ""),
        )

    def op_reconstruct_place_needs_info(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_place_needs_info(msg.get("photo_id"))

    def op_reconstruct_place_confirmed(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_place_confirmed(msg.get("sheet"))

    def op_reconstruct_checklist(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_checklist()

    def op_reconstruct_next_question(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_next_question()

    def op_reconstruct_answer(self, msg: dict[str, Any]) -> dict[str, Any]:
        step_id = str(msg.get("step_id") or "").strip()
        if not step_id:
            raise ValueError("reconstruct_answer needs step_id")
        confirm = _json_bool(msg["confirm"]) if "confirm" in msg else False
        return self._need().reconstruct_answer(
            step_id,
            str(msg.get("text") or msg.get("answer") or ""),
            photo=msg.get("photo") or msg.get("path"),
            confirm=bool(confirm),
        )

    def op_reconstruct_confirm_connection(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_confirm_connection(step_id=msg.get("step_id"), wire_id=msg.get("wire_id"))

    def op_reconstruct_attach_print(self, msg: dict[str, Any]) -> dict[str, Any]:
        path = msg.get("path")
        if not path:
            raise ValueError("reconstruct_attach_print needs path")
        return self._need().reconstruct_attach_print(path, note=str(msg.get("note") or ""))

    def op_reconstruct_compare_print(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_compare_print(msg.get("path"))

    def op_reconstruct_photo_region(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_photo_region(item_id=msg.get("item_id"), ref=_domain(msg, "ref"))

    def op_reconstruct_unconfirmed(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_unconfirmed()

    def op_reconstruct_status(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().reconstruct_status()

    def op_nameplate_build(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().nameplate_build()

    def op_nameplate_set(self, msg: dict[str, Any]) -> dict[str, Any]:
        fields = {key: value for key, value in msg.items() if key not in {"id", "op"}}
        return self._need().nameplate_set(**fields)

    def op_nameplate_export(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._need().nameplate_export(
            pdf=msg.get("pdf") or msg.get("path"),
            dxf=msg.get("dxf"),
            png=msg.get("png"),
        )


def _vision_from_message(msg: dict[str, Any]):
    keys = (
        "vision_backend",
        "vision_model",
        "vision_base_url",
        "vision_api_key_env",
        "vision_workers",
        "workers",
        "vision_timeout_s",
        "vision_page_timeout_s",
        "timeout_s",
        "page_timeout_s",
        "num_ctx",
        "num_predict",
        "think",
        "reasoning_effort",
        "clean_grok_sessions",
        "grok_tools_none",
    )
    if not any(msg.get(key) not in (None, "") for key in keys):
        return None
    from panelcad.importer.vision_config import vision_from_options

    workers = msg.get("vision_workers")
    if workers is None:
        workers = msg.get("workers")
    clean = msg.get("clean_grok_sessions")
    tools_none = msg.get("grok_tools_none")
    timeout = msg.get("vision_timeout_s")
    if timeout is None:
        timeout = msg.get("timeout_s")
    page_timeout = msg.get("vision_page_timeout_s")
    if page_timeout is None:
        page_timeout = msg.get("page_timeout_s")
    return vision_from_options(
        preset=str(msg.get("vision_backend") or "none"),
        base_url=str(msg.get("vision_base_url") or ""),
        model=str(msg.get("vision_model") or ""),
        api_key_env=str(msg.get("vision_api_key_env") or ""),
        workers=None if workers is None else workers,
        timeout_s=None if timeout is None else timeout,
        page_timeout_s=None if page_timeout is None else page_timeout,
        num_ctx=msg.get("num_ctx"),
        num_predict=msg.get("num_predict"),
        think=msg.get("think"),
        reasoning_effort=str(msg.get("reasoning_effort") or ""),
        clean_grok_sessions=True if clean is None else clean,
        grok_tools_none=False if tools_none is None else tools_none,
    )


def run_agent(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    from panelcad.diag.crash import mark_clean_exit
    from panelcad.diag.runtime import install_process_diagnostics

    install_process_diagnostics(gui=False)
    agent = Agent()
    inp = stdin or sys.stdin
    out = stdout or sys.stdout
    try:
        _run_agent_lines(agent, inp, out)
    except Exception:
        raise
    else:
        mark_clean_exit(force=True)


def _agent_json(value: object) -> object:
    """Decimal text stays exact. Money and paths become text. Hours are not cent-rounded."""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Path):
        return str(value)
    if value.__class__.__name__ == "Money" and callable(getattr(value, "text", None)):
        return value.text()
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _run_agent_lines(agent: Agent, inp: TextIO, out: TextIO) -> None:
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            out.write(json.dumps({"ok": False, "error": f"request was not JSON: {exc}"}) + "\n")
            out.flush()
            continue
        if not isinstance(msg, dict):
            out.write(json.dumps({"ok": False, "error": "request was not a JSON object"}) + "\n")
            out.flush()
            continue
        try:
            reply = agent.handle(msg)
        except Exception as exc:  # noqa: BLE001 — one bad op must not kill the stream
            reply = {"id": msg.get("id"), "ok": False, "error": str(exc)}
        try:
            text = json.dumps(reply, default=_agent_json)
        except (TypeError, ValueError) as exc:
            text = json.dumps(
                {
                    "id": msg.get("id"),
                    "ok": False,
                    "error": f"reply was not JSON: {exc}",
                }
            )
        out.write(text + "\n")
        out.flush()
