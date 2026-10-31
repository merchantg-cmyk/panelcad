"""In-app guidance: import advice, checklist state, hints, and help topics.

Widgets live in ``panelcad.gui.guidance``. This module stays free of Qt so a
test can prove the decisions without opening a window.

Welcome, tour, task-checklist, and what's-this progress is stored in the user
settings file under ``guidance``. Nothing here is written into a project.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from panelcad.io.bom import csv_header_is_bom, unrecognized_csv_message
from panelcad.learn import (
    EXTRA_TOPIC_ORDER,
    EXTRA_TOPICS,
    TASKS,
    TOUR_STEPS,
    task_step_ids,
)

HELP_TOPIC_ORDER = (
    "start",
    "import",
    "sample",
    "enclosure",
    "devices",
    "wires",
    "parts",
    "layout",
    "print",
    "problems",
    "troubleshoot",
    "hints",
    "next-steps",
)

HELP_TOPICS: dict[str, dict[str, str]] = {
    "start": {
        "title": "Getting started",
        "body": (
            "Open a recent drawing, start a blank sheet, or open the sample project. "
            "The sample is a copy made only of catalog parts with published sizes.\n\n"
            "Next steps, on the bottom dock, tracks the enclosure, devices, wires, "
            "parts, layout, and print. Each row has a Do it button.\n\n"
            "A greyed-out command shows why it is off, in the tooltip and on the status bar."
        ),
    },
    "import": {
        "title": "What files can I import?",
        "body": (
            "File → Import reads:\n"
            "• A PanelCAD project (.pcad.yaml) or a package folder\n"
            "• A vendor parts list (CSV). Part Number, Description, Qty, and "
            "Manufacturer are enough. Parts with no size yet stay off the BOM and "
            "quote until you pick a catalog part. They also stay off cutouts and "
            "exports. The print is marked NOT FOR BUILD until you pick a catalog "
            "part. Saving does not wait on them. AI lookup can propose a datasheet "
            "size with its source, and nothing changes until you accept it.\n"
            "• A wire list (CSV with From and To)\n"
            "• A PLC I/O list (CSV with Module and Address)\n"
            "• A symbol library (YAML)\n\n"
            "A print (PDF, photo or scan as JPG/PNG/TIFF, DXF, or SVG) uses File → Import print. "
            "SVG lines and text are read as vectors, not as a photo. "
            "Photos of a real panel use File → Reconstruct from photos."
        ),
    },
    "sample": {
        "title": "Sample project",
        "body": (
            "Open sample on the start page copies a direct-on-line starter into "
            "your samples folder. Every device is a catalog part with the maker's "
            "published width, height, and depth. Nothing uses a generic placeholder size.\n\n"
            "Edit the copy. The original sample stays unchanged."
        ),
    },
    "enclosure": {
        "title": "Enclosure",
        "body": (
            "Tools → Panel → Size Enclosure… picks a listed box that fits the parts "
            "already on the drawing. Apply writes that box onto the layout sheet.\n\n"
            "A part without a catalog outline or a confirmed envelope is left off the layout."
        ),
    },
    "devices": {
        "title": "Devices",
        "body": (
            "Drag a symbol from the Library onto the sheet, or use Circuit Builder "
            "for a starter. The tag is the reference (Q1, K1). The same tag on "
            "another sheet is the same device."
        ),
    },
    "wires": {
        "title": "Wires",
        "keywords": "wire number wire numbers",
        "body": (
            "Choose Wire, click a terminal, then click the next one. "
            "On a ladder sheet the rails are terminals too: click anywhere on the "
            "left or right rail (L1 and L2, or the names in the title block) to start "
            "or end the wire. The junction is drawn where you clicked. "
            "An IEC column sheet is the same on the top and bottom rails (L+ and 0V, "
            "or the names you set). "
            "Drag a segment to route it.\n\n"
            "A wire number is the number printed on that wire. "
            "Wire numbers fill in on their own. The scheme is sheet/line (101, 102 on "
            "sheet 1), sequential (1, 2, 3), or IEC (1-1). The Inspector sets the scheme.\n\n"
            "Reports → Number wires assigns them. "
            "Set wire number locks a number. A full renumber keeps every locked number. "
            "To match a print, lock each number (Set wire number, or panelcad wire --number). "
            "After an import, Problems lists wire numbers that were read and not connected. "
            "Draw those wires, then set the number from the print.\n\n"
            "Reports → Wire List (From-To)… lists where each one lands. "
            "File → Import wires… reads a CSV with From and To columns."
        ),
    },
    "parts": {
        "title": "Parts and BOM",
        "body": (
            "File → Import BOM… reads a vendor parts list. Parts with no size yet "
            "stay off the BOM and quote until you pick a catalog part. They also "
            "stay off cutouts. The print is marked NOT FOR BUILD until each of "
            "those rows has a catalog part.\n\n"
            "Assign a catalog number from the Inspector or the Library to give a "
            "device its published size."
        ),
    },
    "layout": {
        "title": "Layout",
        "body": (
            "Insert → Build layout from schematic copies in-cabinet devices onto the "
            "panel layout and packs the ones that have catalog sizes. Generic placeholder "
            "sizes are not placed. Door devices stay on the door."
        ),
    },
    "print": {
        "title": "Print",
        "body": (
            "File → Print Package… builds the customer PDF: drawings, BOM, wires, "
            "and the open checks. While any part still needs info, the package is "
            "stamped NOT FOR BUILD."
        ),
    },
    "problems": {
        "title": "Problems",
        "body": (
            "The Problems dock lists live design checks. Fix assigns a catalog part "
            "or confirms an envelope. An empty list means nothing is blocking the print."
        ),
    },
    "troubleshoot": {
        "title": "Troubleshoot",
        "body": (
            "Tools → Troubleshoot… walks a fault on the open drawing. The guide "
            "starts with lockout and NFPA 70E text. Pick what is "
            "wrong and the device tag. The checks follow the wires and the "
            "cross-references already on the sheets. Each check is LIVE or "
            "DE-ENERGIZED. OK, Wrong, or Skipped moves the guide. A Wrong reading "
            "limits the fault to the path between the last OK and that check.\n\n"
            "Expected voltage comes from the drawing. When the drawing does not "
            "state it, the check says needs info.\n\n"
            "Save log writes a note beside the project. Export CSV or PDF is the "
            "copy for the job file. Explain this step uses the AI account in "
            "Edit → Settings… (Ctrl+,), on the AI tab. PanelCAD does not include a key.\n\n"
            "This walk does not import a PLC program."
        ),
    },
    "hints": {
        "title": "Tips",
        "body": (
            "The first time you open a tool, PanelCAD shows one short tip. "
            "Don't show again hides that tip. "
            "Edit → Settings… (Ctrl+,), on the Appearance tab, turns one-time tips off."
        ),
    },
    "next-steps": {
        "title": "Next steps",
        "body": (
            "Next steps watches the open project: enclosure, devices, wires, parts, "
            "layout, then print. Do it opens the matching command. Hide the dock from "
            "its Dismiss button or View → Next steps. That choice is remembered."
        ),
    },
}

HELP_TOPICS.update(EXTRA_TOPICS)
HELP_TOPIC_ORDER = HELP_TOPIC_ORDER + EXTRA_TOPIC_ORDER

# Menu label (ampersands removed) → (status tip, help topic id).
# A tip that only repeats the label is not a tip. Each entry says what the command does.
ACTION_TIPS: dict[str, tuple[str, str]] = {
    "File": ("Projects, import, export, and print.", "start"),
    "Edit": ("Undo, selection, wires, and settings.", "start"),
    "View": ("Zoom, docks, theme, and the sheet background.", "start"),
    "Insert": ("Add a sheet, a circuit, a strip, or a layout.", "devices"),
    "Tools": ("Troubleshoot, the enclosure, and photo reconstruct.", "troubleshoot"),
    "Strip": ("Build and renumber the terminal strip that is open.", "devices"),
    "Sheets": ("Add, rename, or close a sheet.", "start"),
    "Reports": ("Wire lists, the BOM reports, the nameplate, and the quote.", "print"),
    "Revisions": ("Issue a revision and mark the changes.", "print"),
    "Help": ("Open the searchable how-tos. F1.", "start"),
    "Export": ("Shop files: wire markers and PLC tag tables.", "print"),
    "Drawing prefs": ("Standard, ladder style, wire numbers, and cross-reference marks.", "wires"),
    "Theme": ("Light, dark, or the same colors as Windows.", "dark-mode"),
    "Light": ("Use the light window colors.", "dark-mode"),
    "Dark": ("Use the dark colors.", "dark-mode"),
    "Match Windows": ("Follow the Windows light or dark setting.", "dark-mode"),
    "Sheet": ("Paper, white, or dark background for the drawing.", "dark-mode"),
    "Paper": ("Draw on the cream paper background.", "dark-mode"),
    "White": ("Draw on a white background.", "dark-mode"),
    "Auto": ("Match the sheet background to the window theme.", "dark-mode"),
    "Panel": ("Enclosure size, heat, and door cutouts.", "enclosure"),
    "Closed pages": ("Reopen a sheet you closed.", "start"),
    "New": ("Start a blank project.", "new-project"),
    "Open…": ("Open a PanelCAD project.", "start"),
    "Save": ("Save the project as YAML.", "new-project"),
    "Save As…": ("Save the project to a new YAML file.", "new-project"),
    "Print…": ("Print the sheet that is open.", "print"),
    "Print Package…": ("Build the customer PDF: drawings, BOM, wires, and open checks.", "print"),
    "Export package…": ("Write the print package and the shop files into one folder.", "print"),
    "Export JSON…": ("Save a JSON copy of this project.", "start"),
    "Export SVG…": ("Save the open sheet as an SVG picture.", "print"),
    "Export PDF…": ("Save the open sheet as a PDF.", "print"),
    "Export DXF…": ("Save the drawing as DXF for AutoCAD or a laser/CNC shop.", "print"),
    "Export BOM…": ("Save the bill of materials as a spreadsheet.", "parts"),
    "Export BOM grouped…": ("Save the bill of materials grouped by part number.", "parts"),
    "Export wire list…": ("Save the wire list for this drawing.", "wires"),
    "Wire Markers…": ("Print one wire marker for each end of a conductor.", "wires"),
    "PLC Tags…": ("Export PLC tags for the programming software.", "devices"),
    "Import…": (
        "Import a project, parts list, wire list, or I/O list.",
        "import",
    ),
    "Import print…": ("Read a PDF, DXF, or SVG print into a new drawing. A scan uses the vision backend.", "print-import"),
    "Reconstruct from photos…": (
        "Build a drawing from photos of a physical panel. A manual path stays available.",
        "reconstruct",
    ),
    "Photos → drawings…": (
        "Read imported photos and write the drawings. The run stops only for flagged items.",
        "reconstruct",
    ),
    "Import BOM…": ("Import a vendor parts list (CSV).", "parts"),
    "Import wires…": ("Import a From/To wire list (CSV).", "wires"),
    "Import library…": ("Load a symbol library YAML file.", "import"),
    "Symbol Builder…": ("Draw or edit a symbol in the library.", "devices"),
    "Exit": ("Close PanelCAD.", "start"),
    "Undo": ("Undo the last edit.", "undo"),
    "Redo": ("Redo the edit you just undid.", "undo"),
    "Select": ("Select parts and wires. Esc leaves wire mode.", "devices"),
    "Wire": ("Click a terminal or a ladder rail, then the next one, to draw a wire.", "wires"),
    "Refresh from disk": ("Reload the project file to pick up a save made on another PC.", "start"),
    "Project activity…": ("See who saved this project, from which PC, and when.", "start"),
    "Object editor…": ("Edit the selected part, wire, or sheet object.", "devices"),
    "Set address…": ("Set the PLC address on the selected I/O point.", "devices"),
    "Duplicate": ("Copy the selection onto this sheet.", "devices"),
    "Rotate 90°": ("Turn the selected part a quarter turn.", "devices"),
    "Bigger": ("Make the selected part larger.", "devices"),
    "Smaller": ("Make the selected part smaller.", "devices"),
    "Delete selected": ("Remove the selected parts and wires.", "devices"),
    "Unlock connection points": ("Unlock the terminals so you can drag them onto the real connection.", "devices"),
    "Lock connection points": ("Lock the terminals so they stay put.", "devices"),
    "Reset connection points": ("Put the terminals back on the library positions.", "devices"),
    "Standard: IEC": ("Draw new circuits to IEC.", "devices"),
    "Standard: NEMA/ANSI": ("Draw new circuits to NEMA/ANSI.", "devices"),
    "Style: ladder": ("Use a ladder for the schematic.", "wires"),
    "Style: schematic": ("Use a schematic layout instead of a ladder.", "wires"),
    "Style: freeform": ("Place symbols freely, without a ladder grid.", "wires"),
    "Choose print style…": ("Pick the title block and drawing style for this job.", "print"),
    "Coil style: circle": ("Draw coils as circles.", "devices"),
    "Coil style: square": ("Draw coils as squares.", "devices"),
    "Wire numbers: sheet/line (101…)": ("Number wires by sheet and line, such as 101.", "wires"),
    "Wire numbers: sequential": ("Number wires 1, 2, 3 in the order they are drawn.", "wires"),
    "Wire numbers: IEC (1-1)": ("Number wires in the IEC sheet-column style.", "wires"),
    "Auto wire numbers on": ("Assign a wire number as you draw.", "wires"),
    "Auto wire numbers off": ("Leave wire numbers blank until you number them.", "wires"),
    "NC mark: underline": ("Mark a normally closed contact with an underline.", "devices"),
    "NC mark: bar": ("Mark a normally closed contact with a bar.", "devices"),
    "NC mark: parentheses": ("Mark a normally closed contact with parentheses.", "devices"),
    "NEMA line pitch: 0.25 in (6.35 mm)": ("Space NEMA line numbers a quarter inch apart.", "wires"),
    "NEMA line pitch: 0.5 in (12.7 mm)": ("Space NEMA line numbers a half inch apart.", "wires"),
    "NEMA line pitch: 1 in (25.4 mm)": ("Space NEMA line numbers one inch apart.", "wires"),
    "NEMA line pitch…": ("Type the spacing between NEMA line numbers.", "wires"),
    "Settings…": ("Theme, one-time tips, autosave, and bug reports.", "hints"),
    "Scan vision…": ("Choose how a scanned print is read.", "print-import"),
    "Zoom in": ("Zoom in on the sheet.", "start"),
    "Zoom out": ("Zoom out on the sheet.", "start"),
    "Fit page": ("Fit the whole sheet in the window.", "start"),
    "Zoom to drawing": ("Zoom so the drawing fills the window.", "start"),
    "Next sheet": ("Open the next sheet.", "start"),
    "Previous sheet": ("Open the previous sheet.", "start"),
    "Phase colors on screen": ("Color each phase on the screen. The print stays black.", "devices"),
    "Reset layout": ("Put the docks back in their usual places.", "start"),
    "Sheet…": ("Add a sheet to this project.", "start"),
    "Circuit Builder…": ("Insert a motor starter circuit.", "devices"),
    "PLC I/O sheets…": ("Add PLC I/O sheets for the modules you pick.", "devices"),
    "Terminal strip…": ("Open the terminal strip editor.", "devices"),
    "Company logo…": ("Place the company logo on the title block.", "print"),
    "Build layout from schematic": ("Place catalog-sized devices onto the panel layout.", "layout"),
    "Pack I/O ladder": ("Lay the PLC I/O points out as a ladder.", "devices"),
    "Cleanup sweep": ("Tidy spacing and overlapping symbols on this sheet.", "layout"),
    "Example: DOL starter": ("Insert the built-in DOL template.", "sample"),
    "Troubleshoot…": (
        "Walk a fault from the wires and cross-references on this drawing.",
        "troubleshoot",
    ),
    "Size Enclosure…": ("Pick a listed enclosure that fits the parts.", "enclosure"),
    "Thermal…": ("Estimate heat rise and whether the box needs cooling.", "enclosure"),
    "Motor sizing": ("Size the breaker, contactor, overload, and wire for a motor.", "devices"),
    "Cutout drawings…": ("Draw the door holes for the operators.", "enclosure"),
    "Convert drawing standard...": ("Convert this drawing between IEC and NEMA/ANSI.", "devices"),
    "Open strip…": ("Open a terminal strip to edit.", "devices"),
    "Add terminal (2.5 mm²)": ("Add a 2.5 mm² terminal to this strip.", "devices"),
    "Add PE terminal": ("Add a ground terminal to this strip.", "devices"),
    "Add partition": ("Add a partition plate on this strip.", "devices"),
    "Jumper selected": ("Jumper the terminals you selected.", "devices"),
    "Remove jumper": ("Remove the jumper from the selected terminals.", "devices"),
    "Move earlier": ("Move the selected terminal toward the start of the strip.", "devices"),
    "Move later": ("Move the selected terminal toward the end of the strip.", "devices"),
    "Renumber tags": ("Renumber the terminals on this strip 1, 2, 3… in order.", "devices"),
    "New sheet…": ("Add a sheet.", "start"),
    "Rename sheet…": ("Rename the sheet that is open.", "start"),
    "Close page": ("Close this sheet without deleting it.", "start"),
    "Delete page…": ("Delete this sheet from the project.", "start"),
    "Number wires": ("Assign wire numbers on this drawing.", "wires"),
    "Update cross-references": ("Refresh the coil and contact cross-references.", "devices"),
    "Assign item numbers": ("Number the BOM lines.", "parts"),
    "Wire List (From-To)…": ("Save one row per wire, with both ends.", "wires"),
    "Net wire defaults…": ("Set the default size, color, and type for a net.", "wires"),
    "Export cross-reference…": ("Save the coil and contact cross-reference table.", "devices"),
    "Export terminal strips…": ("Save the terminal strip tables.", "devices"),
    "Export Phoenix strip (PROJECT complete)…": ("Save this strip for Phoenix PROJECT complete.", "devices"),
    "Export Phoenix CLIPX XML…": ("Save this strip as Phoenix CLIPX XML.", "devices"),
    "Export WAGO strip (smartDESIGNER)…": ("Save this strip for WAGO smartDESIGNER.", "devices"),
    "Export WAGO smartDESIGNER XML…": ("Save this strip as WAGO smartDESIGNER XML.", "devices"),
    "Export Weidmüller strip (WMC)…": ("Save this strip for Weidmüller WMC.", "devices"),
    "Schematic / panel audit…": ("List devices that are on the schematic but not the panel, and the reverse.", "layout"),
    "Design checks…": ("Save the open design checks.", "problems"),
    "Nameplate…": ("Build the panel nameplate from this drawing.", "print"),
    "Control power budget…": ("Total the control-power loads and the margin.", "devices"),
    "PLC I/O List…": ("List each PLC point, address, and device.", "devices"),
    "Short-circuit rating (SCCR)…": ("Calculate the panel short-circuit rating.", "print"),
    "Import PLC I/O…": ("Import a PLC I/O list from CSV.", "devices"),
    "Regenerate I/O sheets": ("Redraw the PLC I/O sheets from the modules.", "devices"),
    "Field cables…": ("Edit cables that leave the panel.", "devices"),
    "Export cable schedule…": ("Save the field cable schedule.", "devices"),
    "Export cable pull list…": ("Save the cable pull list.", "devices"),
    "Export cable terminations…": ("Save where each cable conductor lands.", "devices"),
    "Panel wire annotation…": ("Save the wire notes drawn on the panel.", "wires"),
    "Quote": ("Price this job on this computer. Nothing is sent.", "quote"),
    "Issue revision…": ("Snapshot this drawing and bump the revision.", "print"),
    "Revision history…": ("Show the revisions already issued.", "print"),
    "Compare revisions…": ("List what changed between two revisions.", "print"),
    "Revision cloud": ("Draw a cloud around a change. Press C.", "print"),
    "Clear clouds": ("Remove the revision clouds.", "print"),
    "Updates…": ("Choose the update feed and whether startup checks are on.", "updates"),
    "Help topics…": ("Open the in-app help topics.", "start"),
    "Report a bug…": ("Save a bug report, and send it when sending is set up.", "report-bug"),
    "Keyboard shortcuts": ("List the keyboard shortcuts.", "start"),
    "Check for updates…": ("Look for a newer PanelCAD on the update feed.", "updates"),
    "Roll back to previous version…": ("Restore the previous installed version.", "updates"),
    "Welcome": ("Open the first-run welcome again.", "welcome"),
    "Take the tour": ("Walk the main areas. Hide keeps your place.", "tour"),
    "Resume tour": ("Continue the tour at the stop you left.", "tour"),
    "Task checklists": ("Learning steps for the core jobs. Ticks are saved in settings.", "tasks"),
    "What's This?": ("Click one control to read what it does. Esc cancels.", "whats-this"),
    "Help panel": ("Show the searchable how-tos.", "start"),
    "About": ("PanelCAD version and what this window is for.", "start"),
}

HINTS: dict[str, dict[str, str]] = {
    "wire": {
        "title": "Wire",
        "text": "Tip: click a terminal or a ladder rail, then click the next one. Drag the line to route it.",
        "topic": "wires",
    },
    "library": {
        "title": "Library",
        "text": "Tip: drag a part from the library onto the sheet.",
        "topic": "devices",
    },
    "bom": {
        "title": "BOM",
        "text": "Tip: import a vendor parts list, or drag a BOM row onto the sheet.",
        "topic": "parts",
    },
    "layout": {
        "title": "Layout",
        "text": "Tip: Build layout from schematic places catalog parts on the back panel.",
        "topic": "layout",
    },
    "import": {
        "title": "Import",
        "text": "Tip: a vendor CSV imports as a parts list. Rows without a tag stay needs-info.",
        "topic": "import",
    },
    "troubleshoot": {
        "title": "Troubleshoot",
        "text": (
            "Tip: pick the symptom and the device tag. "
            "OK keeps going. Wrong limits the fault to that part of the circuit."
        ),
        "topic": "troubleshoot",
    },
}

STEP_ORDER = ("enclosure", "devices", "wires", "parts", "layout", "print")

STEP_TITLES = {
    "enclosure": "Enclosure",
    "devices": "Devices",
    "wires": "Wires",
    "parts": "Parts and BOM",
    "layout": "Layout",
    "print": "Print",
}

# Do-it targets are MainWindow method names. The panel does not call them itself.
STEP_ACTIONS = {
    "enclosure": "size_enclosure",
    "devices": "show_library",
    "wires": "set_wire_mode",
    "parts": "show_bom",
    "layout": "pack_from_schematic",
    "print": "print_package",
}

NEXT_STEPS_DEBOUNCE_MS = 200


@dataclass(frozen=True)
class ImportAdvice:
    """What an unrecognized file most likely is, and the button that fixes it."""

    route: str
    title: str
    message: str
    primary_label: str
    secondary_label: str
    help_label: str = "What files can I import?"
    help_topic: str = "import"
    prepared_text: str | None = None


@dataclass(frozen=True)
class ChecklistStep:
    id: str
    title: str
    done: bool
    detail: str
    action: str


def help_topic(topic_id: str) -> dict[str, str]:
    found = HELP_TOPICS.get(topic_id) or HELP_TOPICS["start"]
    return {"id": topic_id if topic_id in HELP_TOPICS else "start", **found}


_HELP_TOKEN = re.compile(r"[a-z0-9]+")


def _help_tokens(text: str) -> set[str]:
    return set(_HELP_TOKEN.findall(text.lower()))


def _help_word_matches(word: str, tokens: set[str]) -> bool:
    """Whole words, plus a simple plural. ``lock`` does not match ``blocking`` or ``lockout``."""

    if word in tokens:
        return True
    return f"{word}s" in tokens or f"{word}es" in tokens


def search_help(query: str) -> list[str]:
    """Topic ids whose title, keywords, or body contain every word. Blank returns all."""

    needle = " ".join(str(query or "").lower().split())
    if not needle:
        return list(HELP_TOPIC_ORDER)
    words = needle.split()
    ranked: list[tuple[int, int, str]] = []
    for order, topic_id in enumerate(HELP_TOPIC_ORDER):
        topic = HELP_TOPICS[topic_id]
        title = topic["title"].lower()
        body = topic["body"].lower()
        keywords = str(topic.get("keywords") or "").lower()
        tokens = _help_tokens(f"{title}\n{keywords}\n{body}")
        if not all(_help_word_matches(word, tokens) for word in words):
            continue
        score = 0
        if needle in title:
            score += 50
        if needle in keywords:
            score += 20
        if needle in body:
            score += 10
        score += sum(3 if word in title else 1 for word in words)
        ranked.append((score, -order, topic_id))
    ranked.sort(reverse=True)
    return [topic_id for _score, _order, topic_id in ranked]


def search_tasks(query: str) -> list[str]:
    """Task ids matching the query. Blank returns every checklist."""

    from panelcad.learn import TASK_ORDER

    needle = " ".join(str(query or "").lower().split())
    if not needle:
        return list(TASK_ORDER)
    words = needle.split()
    found: list[str] = []
    for task_id in TASK_ORDER:
        task = TASKS[task_id]
        hay = f"{task['title']}\n{task['summary']}".lower()
        for step in task["steps"]:  # type: ignore[union-attr]
            hay = f"{hay}\n{step['title']}\n{step['detail']}".lower()
        if all(word in hay for word in words):
            found.append(task_id)
    return found


def _guidance_document() -> dict[str, Any]:
    from panelcad.settings import get

    raw = get("guidance", {})
    data = dict(raw) if isinstance(raw, dict) else {}
    data.setdefault("show_hints", True)
    data.setdefault("next_steps_open", True)
    seen = data.get("hints_seen")
    data["hints_seen"] = [str(item) for item in seen] if isinstance(seen, list) else []
    return data


def _write_guidance(data: dict[str, Any]) -> None:
    from panelcad.settings import update

    update({"guidance": data})


def hints_enabled() -> bool:
    return bool(_guidance_document().get("show_hints", True))


def set_hints_enabled(enabled: bool) -> None:
    data = _guidance_document()
    data["show_hints"] = bool(enabled)
    _write_guidance(data)


def hints_seen() -> set[str]:
    return set(_guidance_document().get("hints_seen") or [])


def hint_available(hint_id: str) -> bool:
    """True when tips are on and this tip has not been dismissed."""

    if hint_id not in HINTS:
        return False
    if not hints_enabled():
        return False
    return hint_id not in hints_seen()


def dismiss_hint(hint_id: str) -> None:
    data = _guidance_document()
    seen = list(data.get("hints_seen") or [])
    if hint_id not in seen:
        seen.append(hint_id)
    data["hints_seen"] = seen
    _write_guidance(data)


def next_steps_open() -> bool:
    return bool(_guidance_document().get("next_steps_open", True))


def set_next_steps_open(open_: bool) -> None:
    data = _guidance_document()
    data["next_steps_open"] = bool(open_)
    _write_guidance(data)


_WELCOME_STATES = {"pending", "dismissed", "done"}
_TOUR_STATES = {"new", "active", "paused", "done", "dismissed"}


def welcome_status() -> str:
    value = str(_guidance_document().get("welcome") or "pending")
    if value not in _WELCOME_STATES:
        return "pending"
    return value


def welcome_pending() -> bool:
    """True until the user finishes or dismisses the first-run welcome."""

    return welcome_status() == "pending"


def set_welcome_status(status: str) -> None:
    if status not in _WELCOME_STATES:
        return
    data = _guidance_document()
    data["welcome"] = status
    _write_guidance(data)


def tour_state() -> dict[str, object]:
    raw = _guidance_document().get("tour")
    data = dict(raw) if isinstance(raw, dict) else {}
    status = str(data.get("status") or "new")
    if status not in _TOUR_STATES:
        status = "new"
    try:
        index = int(data.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    last = max(0, len(TOUR_STEPS) - 1)
    index = min(max(0, index), last)
    return {"status": status, "index": index}


def set_tour_state(*, status: str | None = None, index: int | None = None) -> dict[str, object]:
    current = tour_state()
    if status is not None and status in _TOUR_STATES:
        current["status"] = status
    if index is not None:
        try:
            current["index"] = int(index)
        except (TypeError, ValueError):
            pass
        last = max(0, len(TOUR_STEPS) - 1)
        current["index"] = min(max(0, int(current["index"])), last)
    data = _guidance_document()
    data["tour"] = {"status": current["status"], "index": current["index"]}
    _write_guidance(data)
    return tour_state()


def task_record(task_id: str) -> dict[str, object]:
    """Checked step ids and whether this checklist is hidden. Unknown ids are empty."""

    raw_tasks = _guidance_document().get("tasks")
    tasks = raw_tasks if isinstance(raw_tasks, dict) else {}
    item = tasks.get(task_id)
    item = dict(item) if isinstance(item, dict) else {}
    checked_raw = item.get("checked")
    allowed = task_step_ids(task_id)
    checked = [str(step) for step in checked_raw if str(step) in allowed] if isinstance(checked_raw, list) else []
    return {"checked": checked, "dismissed": bool(item.get("dismissed", False))}


def set_task_step(task_id: str, step_id: str, checked: bool) -> None:
    if step_id not in task_step_ids(task_id):
        return
    data = _guidance_document()
    tasks = dict(data.get("tasks") or {}) if isinstance(data.get("tasks"), dict) else {}
    item = dict(tasks.get(task_id) or {}) if isinstance(tasks.get(task_id), dict) else {}
    current = [str(step) for step in item.get("checked") or [] if str(step) in task_step_ids(task_id)]
    if checked and step_id not in current:
        current.append(step_id)
    if not checked:
        current = [step for step in current if step != step_id]
    item["checked"] = current
    item["dismissed"] = bool(item.get("dismissed", False))
    tasks[task_id] = item
    data["tasks"] = tasks
    _write_guidance(data)


def set_task_dismissed(task_id: str, dismissed: bool) -> None:
    if task_id not in TASKS:
        return
    data = _guidance_document()
    tasks = dict(data.get("tasks") or {}) if isinstance(data.get("tasks"), dict) else {}
    item = dict(tasks.get(task_id) or {}) if isinstance(tasks.get(task_id), dict) else {}
    allowed = task_step_ids(task_id)
    checked = [str(step) for step in item.get("checked") or [] if str(step) in allowed]
    tasks[task_id] = {"checked": checked, "dismissed": bool(dismissed)}
    data["tasks"] = tasks
    _write_guidance(data)


def note_dismissed(note_id: str) -> bool:
    raw = _guidance_document().get("notes_dismissed")
    if not isinstance(raw, list):
        return False
    return str(note_id) in {str(item) for item in raw}


def dismiss_note(note_id: str) -> None:
    data = _guidance_document()
    raw = data.get("notes_dismissed")
    seen = [str(item) for item in raw] if isinstance(raw, list) else []
    if note_id not in seen:
        seen.append(str(note_id))
    data["notes_dismissed"] = seen
    _write_guidance(data)


def restore_note(note_id: str) -> None:
    data = _guidance_document()
    raw = data.get("notes_dismissed")
    seen = [str(item) for item in raw] if isinstance(raw, list) else []
    data["notes_dismissed"] = [item for item in seen if item != str(note_id)]
    _write_guidance(data)


def _csv_scan(text: str) -> tuple[str, str | None]:
    """Find a parts, wire, or I/O header in the first lines. Return the tail."""

    lines = text.splitlines()
    for index, line in enumerate(lines[:40]):
        if not line.strip():
            continue
        try:
            cells = next(csv.reader(StringIO(line)))
        except csv.Error:
            continue
        lowered = [cell.strip().lower() for cell in cells if cell and cell.strip()]
        if "from" in lowered and "to" in lowered:
            return "wires", "\n".join(lines[index:]) + "\n"
        if "address" in lowered and ("module" in lowered or "plc" in lowered):
            return "io", "\n".join(lines[index:]) + "\n"
        if csv_header_is_bom(cells):
            return "bom", "\n".join(lines[index:]) + "\n"
    return "", None


def _advice_for(
    route: str,
    *,
    detail: str = "",
    prepared_text: str | None = None,
    filename: str = "",
) -> ImportAdvice:
    extra = f"\n\n{detail}" if detail else ""
    if route == "bom":
        return ImportAdvice(
            route="bom",
            title="Import failed",
            message=(
                "This looks like a parts list. Import it as a parts list, "
                "or open the BOM importer to pick another file. A row with a "
                "part number and no tag is kept as needs-info: no millimetres, "
                "off the BOM, quote, and cutouts. The print is marked NOT FOR BUILD "
                "until you choose a library part."
                + extra
            ),
            primary_label="Import as parts list",
            secondary_label="Open BOM import",
            prepared_text=prepared_text,
        )
    if route == "wires":
        return ImportAdvice(
            route="wires",
            title="Import failed",
            message="This looks like a wire list (From and To)." + extra,
            primary_label="Import as wires",
            secondary_label="Open wire import",
            prepared_text=prepared_text,
        )
    if route == "io":
        return ImportAdvice(
            route="io",
            title="Import failed",
            message="This looks like a PLC I/O list." + extra,
            primary_label="Import as I/O",
            secondary_label="Open I/O import",
            prepared_text=prepared_text,
        )
    if route in {"project", "project_json"}:
        return ImportAdvice(
            route="project",
            title="Import failed",
            message="This looks like a PanelCAD project." + extra,
            primary_label="Open project",
            secondary_label="Choose another file",
        )
    if route == "package":
        return ImportAdvice(
            route="package",
            title="Import failed",
            message="This looks like a PanelCAD package folder." + extra,
            primary_label="Import package",
            secondary_label="Choose another file",
        )
    if route == "library":
        return ImportAdvice(
            route="library",
            title="Import failed",
            message="This looks like a symbol library." + extra,
            primary_label="Import library",
            secondary_label="Choose another file",
        )
    if route == "print":
        return ImportAdvice(
            route="print",
            title="Import failed",
            message="This looks like a print. Use Import print to read it.",
            primary_label="Import print",
            secondary_label="Choose another file",
        )
    if route == "sheet":
        return ImportAdvice(
            route="sheet",
            title="Import failed",
            message="This looks like one PanelCAD sheet." + extra,
            primary_label="Import sheet",
            secondary_label="Choose another file",
        )
    name = filename or "this file"
    message = (
        f"PanelCAD can't open {name}. It opens .pcad.yaml projects, "
        "parts lists (CSV/Excel), wire lists, and prints (PDF, picture, DXF)."
    )
    if detail and "Expected columns" in detail:
        message = f"{message}\n\n{detail}"
    return ImportAdvice(
        route="unknown",
        title="Import failed",
        message=message,
        primary_label="What files can I import?",
        secondary_label="",
    )


def classify_import(path: Path | str) -> ImportAdvice:
    """Detect the likely file type. Does not mutate a project."""

    from panelcad.io.exchange import sniff

    src = Path(path)
    if src.is_dir():
        return _advice_for("package")
    kind = ""
    sniff_error = ""
    try:
        kind = sniff(src)
    except ValueError as exc:
        sniff_error = str(exc)
    except OSError as exc:
        sniff_error = str(exc)
    if kind == "pdf":
        return _advice_for("print")
    if kind == "svg":
        detail = f"\n\n{sniff_error}" if sniff_error else ""
        return ImportAdvice(
            route="print",
            title="Import failed",
            message="This looks like an SVG drawing. Use Import print to read the vectors." + detail,
            primary_label="Import print",
            secondary_label="What files can I import?",
        )
    if kind in {"bom", "wires", "io", "project", "project_json", "library", "package", "sheet", "title_block"}:
        route = "project" if kind == "project_json" else kind
        if kind == "title_block":
            route = "unknown"
            return _advice_for("unknown", filename=src.name)
        return _advice_for(route)
    prepared_route = ""
    prepared: str | None = None
    text = ""
    if src.suffix.lower() == ".csv" or sniff_error:
        try:
            text = src.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            text = ""
        if text:
            prepared_route, prepared = _csv_scan(text)
    if prepared_route:
        # A title row above the real header still routes to that importer.
        # The sniff error names the title line and would contradict this route.
        same = prepared is not None and prepared.strip() == text.strip()
        return _advice_for(prepared_route, prepared_text=None if same else prepared)
    if kind in {"yaml", "json"}:
        return _advice_for("unknown", filename=src.name)
    if sniff_error:
        detail = sniff_error if "Expected columns" in sniff_error else ""
        return _advice_for("unknown", detail=detail, filename=src.name)
    if src.suffix.lower() == ".csv":
        try:
            header_line = src.read_text(encoding="utf-8-sig").splitlines()[:1]
            cells = next(csv.reader(header_line), [])
        except (OSError, UnicodeError, csv.Error):
            cells = []
        return _advice_for("unknown", detail=unrecognized_csv_message(cells), filename=src.name)
    return _advice_for("unknown", filename=src.name)


def apply_import_fix(session: Any, path: Path | str, advice: ImportAdvice) -> dict[str, Any]:
    """Run the fix-it route. A parts list uses the BOM importer, including a sliced header."""

    import tempfile

    src = Path(path)
    route = advice.route
    if route == "unknown":
        raise ValueError(advice.message)
    if route == "print":
        return {"kind": "print", "path": str(src)}
    temporary: Path | None = None
    try:
        target = src
        if advice.prepared_text:
            handle = tempfile.NamedTemporaryFile(
                "w",
                suffix=".csv",
                prefix="panelcad-import-",
                delete=False,
                encoding="utf-8",
                newline="",
            )
            handle.write(advice.prepared_text)
            handle.close()
            temporary = Path(handle.name)
            target = temporary
        if route == "bom":
            return {"kind": "bom", **session.import_bom(target)}
        if route == "wires":
            return {"kind": "wires", **session.import_wires(target)}
        if route == "io":
            return {"kind": "io", **session.import_io_list(target)}
        if route == "project":
            return session.import_any(target, replace=True)
        if route == "package":
            return session.import_any(src, replace=False)
        if route == "library":
            ids = session.import_library(target)
            return {"kind": "library", "devices": len(ids)}
        if route == "sheet":
            sheet_id = session.import_sheet(target)
            return {"kind": "sheet", "id": sheet_id}
        raise ValueError(advice.message)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def bom_empty_copy(row_count: int, needs_info_count: int) -> str:
    if row_count:
        return ""
    if needs_info_count:
        noun = "item" if needs_info_count == 1 else "items"
        return (
            f"No parts on the BOM. NOT FOR BUILD: {needs_info_count} {noun} need info. "
            "Import a vendor parts list, or add a part from the catalog. "
            "Next: match each needs-info row to a library part."
        )
    return (
        "No parts yet. Import a vendor parts list, or add one from the catalog. "
        "Next: use Import CSV if the file came from a vendor."
    )


def problems_empty_copy(row_count: int) -> str:
    if row_count:
        return ""
    return "No problems: ready to print."


def troubleshoot_empty_copy(*, devices: int) -> str:
    """What the Troubleshoot dock says when the drawing has no device tags."""

    if devices:
        return ""
    return (
        "No device tags are on this drawing yet. Next: add devices and wires, then pick "
        "a symptom here. The guide follows the tags, wire numbers, and "
        "cross-references already on the sheets."
    )


def empty_sheet_copy(kind: str, *, components: int, wires: int) -> tuple[str, tuple[tuple[str, str], ...]]:
    """One sentence and up to two buttons for an empty sheet or layout."""

    if components or wires:
        return "", ()
    if kind in {"panel_layout", "door_layout"}:
        return (
            "This layout is empty. Next: build it from the schematic so devices with "
            "catalog sizes land on the panel.",
            (("Build layout from schematic", "layout"),),
        )
    return (
        "This sheet is empty. Next: add a part from the catalog, or import a vendor parts list.",
        (("Add from catalog", "catalog"), ("Import a vendor parts list (CSV)", "import")),
    )


def _needs_info_count(session: Any) -> int:
    from panelcad.reconstruct.placeholder import is_needs_info_component

    return sum(
        1
        for sheet in session.project.sheets
        for comp in sheet.components
        if is_needs_info_component(comp)
    )


def _component_count(session: Any) -> int:
    return sum(len(sheet.components) for sheet in session.project.sheets)


def _wire_count(session: Any) -> int:
    return sum(len(sheet.wires) for sheet in session.project.sheets)


def project_checklist(session: Any, *, problem_count: int | None = None) -> list[ChecklistStep]:
    """Project-state checklist. Does not run design checks."""

    from panelcad.library.panel import effective_footprint, is_incabinet
    from panelcad.reconstruct.placeholder import is_needs_info_component

    sheets = list(session.project.sheets)
    enclosure_done = any(
        sheet.enclosure is not None and sheet.enclosure.width_mm > 0 and sheet.enclosure.height_mm > 0
        for sheet in sheets
        if sheet.kind in {"panel_layout", "door_layout"}
    )
    devices = _component_count(session)
    wires = _wire_count(session)
    needs = _needs_info_count(session)
    missing: list[str] = []
    for sheet in sheets:
        for comp in sheet.components:
            if is_needs_info_component(comp):
                continue
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                missing.append(comp.id)
                continue
            if not is_incabinet(device, comp):
                continue
            footprint = effective_footprint(device, comp)
            if str(footprint.dim_source or "") != "catalog":
                missing.append(comp.id)
    layout_devices = sum(
        len(sheet.components)
        for sheet in sheets
        if sheet.kind in {"panel_layout", "door_layout"}
    )
    if needs:
        noun = "item" if needs == 1 else "items"
        parts_detail = f"NOT FOR BUILD: {needs} {noun} need info"
        parts_done = False
    elif missing:
        shown = ", ".join(missing[:6])
        parts_detail = f"Assign a catalog part: {shown}"
        parts_done = False
    elif devices:
        parts_detail = "Catalog parts are on the drawing"
        parts_done = True
    else:
        parts_detail = "Import a vendor parts list or assign catalog parts"
        parts_done = False
    if problem_count:
        parts_detail = f"{parts_detail}. Problems: {problem_count}"
    layout_done = layout_devices > 0
    print_done = bool(enclosure_done and devices and wires and parts_done and layout_done and not needs)
    if needs:
        print_detail = f"NOT FOR BUILD: {needs} {'item' if needs == 1 else 'items'} need info"
    elif print_done:
        print_detail = "Ready to print"
    else:
        print_detail = "Finish the earlier steps, then print the package"
    steps = [
        ChecklistStep(
            "enclosure",
            STEP_TITLES["enclosure"],
            enclosure_done,
            "Enclosure is set" if enclosure_done else "Set the enclosure that will hold these parts",
            STEP_ACTIONS["enclosure"],
        ),
        ChecklistStep(
            "devices",
            STEP_TITLES["devices"],
            devices > 0,
            f"{devices} device(s) placed" if devices else "Place devices on a sheet",
            STEP_ACTIONS["devices"],
        ),
        ChecklistStep(
            "wires",
            STEP_TITLES["wires"],
            wires > 0,
            f"{wires} wire(s)" if wires else "Connect the devices",
            STEP_ACTIONS["wires"],
        ),
        ChecklistStep(
            "parts",
            STEP_TITLES["parts"],
            parts_done,
            parts_detail,
            STEP_ACTIONS["parts"],
        ),
        ChecklistStep(
            "layout",
            STEP_TITLES["layout"],
            layout_done,
            f"{layout_devices} device(s) on the layout" if layout_done else "Build the panel layout",
            STEP_ACTIONS["layout"],
        ),
        ChecklistStep(
            "print",
            STEP_TITLES["print"],
            print_done,
            print_detail,
            STEP_ACTIONS["print"],
        ),
    ]
    return steps


def placeholder_size_issues(session: Any) -> list[str]:
    """Catalog-only sample rule: every device uses a published outline, not a generic fill."""

    from panelcad.library.panel import effective_footprint
    from panelcad.reconstruct.placeholder import is_needs_info_component

    issues: list[str] = []
    seen: set[str] = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            seen.add(comp.id)
            if is_needs_info_component(comp):
                issues.append(f"{comp.id} is needs-info")
                continue
            catalog = str(comp.properties.get("catalog") or "").strip()
            manufacturer = str(comp.properties.get("manufacturer") or "").strip()
            if not catalog:
                issues.append(f"{comp.id} has no catalog number")
                continue
            try:
                part = session.parts.get(catalog, manufacturer or None)
            except KeyError:
                issues.append(f"{comp.id} catalog {catalog} is not in the parts library")
                continue
            if not part.has_footprint():
                issues.append(f"{comp.id} catalog {catalog} has no published footprint")
                continue
            try:
                device = session.catalog.get(comp.library_id)
            except KeyError:
                issues.append(f"{comp.id} library {comp.library_id} is missing")
                continue
            footprint = effective_footprint(device, comp)
            if str(footprint.dim_source or "") != "catalog":
                issues.append(f"{comp.id} size source is {footprint.dim_source or 'generic'}, not catalog")
            if footprint.depth_from_generic:
                issues.append(f"{comp.id} depth is a generic fill")
            if abs(float(footprint.width_mm) - float(part.width_mm or 0)) > 0.2:
                issues.append(f"{comp.id} width is not the published catalog width")
            if abs(float(footprint.height_mm) - float(part.height_mm or 0)) > 0.2:
                issues.append(f"{comp.id} height is not the published catalog height")
            if part.depth_mm is not None and abs(float(footprint.depth_mm) - float(part.depth_mm)) > 0.2:
                issues.append(f"{comp.id} depth is not the published catalog depth")
            if str(comp.properties.get("placeholder_footprint") or "").lower() in {"1", "true", "yes"}:
                issues.append(f"{comp.id} is marked placeholder")
    if not seen:
        issues.append("sample has no devices")
    return issues
