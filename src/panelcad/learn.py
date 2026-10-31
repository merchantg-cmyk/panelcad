"""Offline copy for the welcome, the tour, task checklists, and what's-this.

No settings and no Qt. ``panelcad.guidance`` stores progress and searches help.
Nothing in this module is written into a project file.
"""

from __future__ import annotations

# Appended to the help catalog. Ids stay stable so a saved topic still opens.
EXTRA_TOPIC_ORDER = (
    "welcome",
    "tour",
    "new-project",
    "place",
    "quote",
    "csv",
    "print-import",
    "reconstruct",
    "updates",
    "report-bug",
    "undo",
    "dark-mode",
    "ai-account",
    "whats-this",
    "tasks",
)

EXTRA_TOPICS: dict[str, dict[str, str]] = {
    "welcome": {
        "title": "Welcome",
        "keywords": "first run onboarding start here tour",
        "body": (
            "The first time PanelCAD opens, a welcome offers a short tour, the "
            "how-tos, or a straight start. Start drawing hides that welcome. "
            "Help → Welcome opens it again.\n\n"
            "That choice is stored in your settings, not in the drawing. "
            "An experienced user who dismisses it does not see it on the next launch."
        ),
    },
    "tour": {
        "title": "Guided tour",
        "keywords": "coach marks walkthrough main areas library inspector bom",
        "body": (
            "Help → Take the tour walks the main areas: Library, the sheet, "
            "Wire, Inspector, BOM, Problems and print, then Troubleshoot.\n\n"
            "Next and Back move through the stops. Hide keeps your place. "
            "Skip tour puts it away. Help → Resume tour continues at the same stop. "
            "The place is stored in your settings."
        ),
    },
    "new-project": {
        "title": "How to start a project",
        "keywords": "new blank save yaml sample",
        "body": (
            "1. File → New, or New blank on the start page. Pick a print style.\n"
            "2. File → Save As. The project is one YAML file (.pcad.yaml). "
            "There is no separate binary project.\n"
            "3. Or open the sample. It is a copy of a catalog starter, so the "
            "original sample stays unchanged.\n\n"
            "The start page also lists recent drawings."
        ),
    },
    "place": {
        "title": "How to place parts",
        "keywords": "library drag tag catalog symbol",
        "body": (
            "1. Open the Library (View → Library, or Ctrl+1).\n"
            "2. Search, then drag a symbol onto the sheet. You can also click it "
            "and then click empty paper.\n"
            "3. The tag (Q1, K1, and the same kind of name) is the device. "
            "The same tag on another sheet is the same device.\n"
            "4. Catalog parts… assigns a manufacturer number. A catalog part "
            "brings the published width, height, and depth.\n\n"
            "A part with no catalog outline stays off the layout until you match one "
            "or confirm an envelope."
        ),
    },
    "quote": {
        "title": "How to build a quote",
        "keywords": "price labor markup bom cost",
        "body": (
            "Reports → Quote, or View → Quote, opens the quote for this drawing.\n\n"
            "Parts come from the devices on the sheets. Needs-info rows stay off "
            "the quote until you choose a library part. Prices come only from a "
            "price list, a library part, or the grid on this panel. "
            "Nothing is sent anywhere.\n\n"
            "Import price list… reads a file you already have. Export writes "
            "XLSX, PDF, or CSV on this computer."
        ),
    },
    "csv": {
        "title": "How to import a vendor CSV",
        "keywords": "parts list part number manufacturer qty header preamble needs-info",
        "body": (
            "File → Import BOM… opens a short guide and "
            "then the file.\n\n"
            "The header row needs a part number, a description, a quantity, and "
            "a manufacturer. These aliases are the same columns: Part #, part, "
            "item number, model, catalog number, cat no, brand, make, mfr, "
            "count, quantity required.\n\n"
            "A title line above that header is fine. PanelCAD finds the header. "
            "You do not have to delete the title in the spreadsheet.\n\n"
            "A tag (Tag, Ref, Device) is optional. A row with a part number and "
            "no tag is kept as needs-info: no millimetres. Parts with no size yet "
            "stay off the BOM and quote until you pick a catalog part. They also "
            "stay off cutouts. The print is marked NOT FOR BUILD until you choose "
            "a catalog part.\n\n"
            "A file whose header is From and To is a wire list. Use "
            "File → Import wires… for that file. A Module and Address file is a "
            "PLC I/O list.\n\n"
            "Example, not a real catalog number:\n"
            "Part Number,Description,Qty,Manufacturer\n"
            "ABC-123,Example device,1,Example Maker"
        ),
    },
    "print-import": {
        "title": "How to import a print",
        "keywords": "pdf scan dxf svg vision review from-print",
        "body": (
            "File → Import print… takes a PDF, a picture, a DXF, or an SVG of an existing drawing.\n\n"
            "Text and vector lines already in the file are read from the file. "
            "An SVG is read as vectors, not as a photo. A scan uses "
            "the vision backend you pick on that form. Test connection checks "
            "that backend before you start.\n\n"
            "What usually comes back: device tags, symbols that match the library, "
            "and wire numbers that landed on two terminals. The summary says how many "
            "devices were placed, how many wire numbers were read, how many wires "
            "connected, and how many numbers were left unconnected.\n\n"
            "What usually needs hand work: a symbol that does not match the library "
            "(it stays needs-info, off the BOM and the quote), wire numbers that were "
            "read but not connected, and catalog part numbers. Those import gaps are "
            "at the top of Problems.\n\n"
            "Open review before those devices become the drawing. Accept, reject, "
            "or edit each one. This path does not use the datasheet AI account "
            "in Edit → Settings… (Ctrl+,), on the AI tab."
        ),
    },
    "reconstruct": {
        "title": "Reconstruct from photos",
        "keywords": "reconstruct photos phone panel camera scale nameplate drawings flagged",
        "body": (
            "File → Reconstruct from photos… builds a drawing from photos of a "
            "real panel: the backplate, the door, nameplates, and terminal strips.\n\n"
            "File → Photos → drawings… is the same job in one run. It reads the "
            "imported photos and writes the drawings. It stops only for flagged "
            "items: missing info, low confidence, or a possible missed device. "
            "Everything else passes through. The step-by-step command stays available.\n\n"
            "Type the 4 corners of a part whose size you know (for example the PLC), "
            "and its width and height. Then type the part number from each label. "
            "Parts with no size yet stay off the BOM and quote until you pick a "
            "catalog part or confirm the size.\n\n"
            "An old PDF or scan of a drawing is File → Import print…, not this command."
        ),
    },
    "updates": {
        "title": "Updates",
        "keywords": "update updates check for updates feed roll back rollback",
        "body": (
            "Help → Check for updates… looks for a newer PanelCAD. "
            "Help → Updates… sets the release feed and whether PanelCAD checks "
            "at startup. Help → Roll back to previous version… restores the "
            "last installed copy.\n\n"
            "Nothing is installed until you confirm. With no feed, the checker stays off."
        ),
    },
    "report-bug": {
        "title": "Report a bug",
        "keywords": "bug report crash send slack email",
        "body": (
            "Help → Report a bug… saves a report on this PC. Include what you "
            "were doing. If sending is not set up, click Save and email the file "
            "to the office.\n\n"
            "The report can include a screenshot, the recent actions, and the log. "
            "Edit → Settings… (Ctrl+,), on the Bug reports tab, is where sending is configured."
        ),
    },
    "undo": {
        "title": "Undo",
        "keywords": "undo redo ctrl z",
        "body": (
            "Edit → Undo takes back the last change. Edit → Redo puts it back. "
            "The shortcuts are Ctrl+Z and Ctrl+Shift+Z.\n\n"
            "Undo is off until you change the drawing. The tooltip says why."
        ),
    },
    "dark-mode": {
        "title": "Dark mode",
        "keywords": "dark mode theme light appearance night",
        "body": (
            "View → Theme → Light uses the light window colors. Dark uses the dark "
            "window colors. Match Windows follows the PC.\n\n"
            "View → Sheet → Paper draws on the cream paper. White is a white sheet. "
            "Dark draws the sheet on a dark background. Auto matches the window theme. "
            "The printed PDF stays white paper.\n\n"
            "Edit → Settings… (Ctrl+,), on the Appearance tab, turns one-time tips on or off."
        ),
    },
    "ai-account": {
        "title": "AI key",
        "keywords": "ai key api key settings account ollama",
        "body": (
            "Edit → Settings… (Ctrl+,), on the AI tab, is where the AI key goes. "
            "PanelCAD does not include a key. A local Ollama install does not need a cloud key."
        ),
    },
    "whats-this": {
        "title": "What's this",
        "keywords": "tooltip shift f1 contextual help",
        "body": (
            "Hover a command for a short tooltip. The status bar repeats it, and "
            "Learn more opens the matching how-to.\n\n"
            "Help → What's This? (Shift+F1) waits for one click. The next control "
            "you click shows a longer note. Esc cancels. The note is dismissable."
        ),
    },
    "tasks": {
        "title": "Task checklists",
        "keywords": "getting started workflows tick dismiss resume",
        "body": (
            "Help → Task checklists opens eight short lists: new project, place "
            "parts, wire, BOM and quote, print, CSV import, print import, and "
            "troubleshoot.\n\n"
            "Tick a step when you have done it. Do this runs that command and "
            "ticks the step. Read more opens the how-to when the step is only "
            "an explanation. Hide checklist puts that list away. Show again brings "
            "it back. The ticks are stored in your settings, not in the drawing.\n\n"
            "Next steps, the other checklist, watches the open project itself "
            "(enclosure, devices, wires, parts, layout, print). Task checklists "
            "are the learning steps. Next steps is the drawing."
        ),
    },
}

TOUR_STEPS: tuple[dict[str, str], ...] = (
    {
        "id": "library",
        "title": "Library",
        "body": (
            "Symbols live here. Drag one onto the sheet, or click it and then "
            "click empty paper. The tag, such as Q1 or K1, is the device. "
            "Catalog parts… assigns a manufacturer number and the published size."
        ),
        "target": "libraryDock",
        "topic": "place",
    },
    {
        "id": "sheet",
        "title": "Sheet",
        "body": (
            "This is the drawing. An empty sheet says what to do next. "
            "Right-click a part, a wire, a sheet tab, or empty paper for commands."
        ),
        "target": "sheetCanvas",
        "topic": "start",
    },
    {
        "id": "wire",
        "title": "Wire",
        "body": (
            "Wire, or the W key, connects two terminals. Click the first, then "
            "the next. Drag a segment to route it. Select, or Esc, leaves wire mode."
        ),
        "target": "mainToolbar",
        "topic": "wires",
    },
    {
        "id": "inspector",
        "title": "Inspector",
        "body": (
            "Select a device and the Inspector shows its tag, catalog number, "
            "and the fields for that symbol. The same tag on another sheet is "
            "the same device."
        ),
        "target": "inspectorDock",
        "topic": "devices",
    },
    {
        "id": "bom",
        "title": "BOM",
        "body": (
            "The BOM lists parts on the drawing. Import CSV… reads a vendor parts "
            "list. Parts with no size yet stay off the BOM and the quote until "
            "you choose a catalog part."
        ),
        "target": "bomDock",
        "topic": "csv",
    },
    {
        "id": "print",
        "title": "Problems and print",
        "body": (
            "Problems lists the live checks. File → Print Package… builds the "
            "customer PDF. While any part still needs info, the package is "
            "stamped NOT FOR BUILD."
        ),
        "target": "problemsDock",
        "topic": "print",
    },
    {
        "id": "troubleshoot",
        "title": "Troubleshoot and help",
        "body": (
            "Troubleshoot walks a fault from the wires and cross-references "
            "already on the sheets. Press F1 any time for the how-tos. "
            "Hide this tour and resume it from the Help menu."
        ),
        "target": "troubleshootDock",
        "topic": "troubleshoot",
    },
)

TASK_ORDER = (
    "new-project",
    "place-parts",
    "wire",
    "bom-quote",
    "print",
    "csv-import",
    "print-import",
    "troubleshoot",
)

TASKS: dict[str, dict[str, object]] = {
    "new-project": {
        "title": "New project",
        "summary": "Start a drawing and save it as YAML.",
        "steps": (
            {
                "id": "blank",
                "title": "Start a blank drawing",
                "detail": "File → New, or New blank on the start page. Pick a print style.",
                "action": "new-project",
            },
            {
                "id": "save",
                "title": "Save the YAML file",
                "detail": "File → Save As writes the project. The file is the drawing.",
                "action": "save",
            },
            {
                "id": "sample",
                "title": "Or open the sample",
                "detail": "The sample is a copy of a catalog starter. The original stays unchanged.",
                "action": "sample",
            },
        ),
    },
    "place-parts": {
        "title": "Place parts",
        "summary": "Put symbols on the sheet and give them catalog numbers.",
        "steps": (
            {
                "id": "library",
                "title": "Open the Library",
                "detail": "View → Library, or Ctrl+1. Search for the symbol.",
                "action": "library",
            },
            {
                "id": "drag",
                "title": "Drag a symbol onto the sheet",
                "detail": "The tag is the device. The same tag on another sheet is the same device.",
                "action": "library",
            },
            {
                "id": "catalog",
                "title": "Assign a catalog part",
                "detail": "Catalog parts… sets a manufacturer number and the published size.",
                "action": "catalog",
            },
        ),
    },
    "wire": {
        "title": "Wire",
        "summary": "Connect terminals, then route the line.",
        "steps": (
            {
                "id": "mode",
                "title": "Choose Wire",
                "detail": "Press W, or click Wire on the toolbar.",
                "action": "wire",
            },
            {
                "id": "click",
                "title": "Click two terminals",
                "detail": "Click the first terminal, then the next one. A ladder rail counts.",
                "action": "wire",
            },
            {
                "id": "route",
                "title": "Route the segment",
                "detail": "Drag the line to route it. Esc, or Select, leaves wire mode.",
                "action": "select",
            },
        ),
    },
    "bom-quote": {
        "title": "BOM and quote",
        "summary": "See the parts, then price the job on this computer.",
        "steps": (
            {
                "id": "bom",
                "title": "Open the BOM",
                "detail": "View → BOM, or Ctrl+3. Parts on the drawing are listed there.",
                "action": "bom",
            },
            {
                "id": "needs",
                "title": "Read needs-info parts",
                "detail": "Parts with no size yet stay off the BOM and the quote until you match a library part.",
                "action": "bom",
            },
            {
                "id": "quote",
                "title": "Open the quote",
                "detail": "Reports → Quote. Prices stay in this file. Nothing is sent.",
                "action": "quote",
            },
        ),
    },
    "print": {
        "title": "Print",
        "summary": "Clear the checks, then build the customer PDF.",
        "steps": (
            {
                "id": "checks",
                "title": "Read Problems",
                "detail": "View → Problems lists what is still open.",
                "action": "problems",
            },
            {
                "id": "package",
                "title": "Build the print package",
                "detail": "File → Print Package writes the customer PDF.",
                "action": "print",
            },
            {
                "id": "stamp",
                "title": "Watch the NOT FOR BUILD stamp",
                "detail": "While any part still needs info, the package is stamped NOT FOR BUILD.",
                "action": "print-help",
            },
        ),
    },
    "csv-import": {
        "title": "CSV import",
        "summary": "Bring in a vendor parts list without losing the header.",
        "steps": (
            {
                "id": "open",
                "title": "Open Import CSV",
                "detail": "BOM → Import CSV…, or File → Import BOM…, explains the columns first.",
                "action": "csv",
            },
            {
                "id": "header",
                "title": "Use a header row",
                "detail": "Part Number, Description, Qty, and Manufacturer. Aliases such as Part # and brand work.",
                "action": "csv-help",
            },
            {
                "id": "title",
                "title": "Leave a title line in place",
                "detail": "A line above the header, such as an order-sheet name, is fine.",
                "action": "csv-help",
            },
            {
                "id": "tag",
                "title": "Expect needs-info when there is no tag",
                "detail": "No tag means no millimetres. Parts with no size yet stay off the BOM and the quote until you pick a catalog part.",
                "action": "csv-help",
            },
            {
                "id": "wires",
                "title": "Send wire lists to Import wires",
                "detail": "From and To columns are a wire list, not a parts list.",
                "action": "wires-import",
            },
        ),
    },
    "print-import": {
        "title": "Print import",
        "summary": "Read an existing PDF, picture, DXF, or SVG, then review it.",
        "steps": (
            {
                "id": "file",
                "title": "Choose the print",
                "detail": "File → Import print. PDF, picture, DXF, or SVG.",
                "action": "print-import",
            },
            {
                "id": "vector",
                "title": "Know which reader runs",
                "detail": (
                    "Vector lines and text, including SVG, are taken from the file. "
                    "A scan uses the vision backend on the form."
                ),
                "action": "print-import-help",
            },
            {
                "id": "review",
                "title": "Review before you keep devices",
                "detail": "Open review. Accept, reject, or edit each device.",
                "action": "print-import-help",
            },
        ),
    },
    "troubleshoot": {
        "title": "Troubleshoot",
        "summary": "Walk a fault from the drawing that is already open.",
        "steps": (
            {
                "id": "open",
                "title": "Open Troubleshoot",
                "detail": "Tools → Troubleshoot, or View → Troubleshoot.",
                "action": "troubleshoot",
            },
            {
                "id": "lockout",
                "title": "Read the lockout notice",
                "detail": "The guide starts with lockout and NFPA 70E text. Each check is LIVE or DE-ENERGIZED.",
                "action": "troubleshoot-help",
            },
            {
                "id": "pick",
                "title": "Pick the symptom and the tag",
                "detail": "The checks follow that device through the wires and cross-references on the sheets.",
                "action": "troubleshoot",
            },
            {
                "id": "marks",
                "title": "Mark OK, Wrong, or Skipped",
                "detail": "OK keeps going. Wrong limits the fault to the path since the last OK.",
                "action": "troubleshoot-help",
            },
            {
                "id": "save",
                "title": "Save the log beside the project",
                "detail": "Save log writes a note next to the project file. It is not stored inside the drawing.",
                "action": "troubleshoot-help",
            },
        ),
    },
}

WHATS_THIS: dict[str, dict[str, str]] = {
    "libraryDock": {
        "title": "Library",
        "text": (
            "Drag a symbol onto the sheet. Search narrows the list. "
            "Catalog parts… assigns a manufacturer number and the published size."
        ),
        "topic": "place",
    },
    "inspectorDock": {
        "title": "Inspector",
        "text": "The selected device: tag, catalog number, and the fields for that symbol.",
        "topic": "devices",
    },
    "bomDock": {
        "title": "BOM",
        "text": (
            "Parts on the drawing. Import CSV… reads a vendor parts list. "
            "Needs-info rows are listed and not counted."
        ),
        "topic": "parts",
    },
    "problemsDock": {
        "title": "Problems",
        "text": "Live design checks. An empty list means nothing here is blocking the print.",
        "topic": "problems",
    },
    "quoteDock": {
        "title": "Quote",
        "text": "Prices for this drawing. They stay in the file. Nothing is sent.",
        "topic": "quote",
    },
    "troubleshootDock": {
        "title": "Troubleshoot",
        "text": (
            "Walk a fault from the wires and cross-references on this drawing. "
            "OK keeps going. Wrong limits the fault to that part of the circuit."
        ),
        "topic": "troubleshoot",
    },
    "nextStepsDock": {
        "title": "Next steps",
        "text": "The open project: enclosure, devices, wires, parts, layout, then print.",
        "topic": "next-steps",
    },
    "aiDock": {
        "title": "AI",
        "text": (
            "Shows which AI is driving the drawing, the page, the live operations, "
            "and a prompt box. It uses your own connected AI. No key is built in."
        ),
        "topic": "hints",
    },
    "sheetCanvas": {
        "title": "Sheet",
        "text": "The drawing. Right-click a part, a wire, or empty paper. An empty sheet says the next step.",
        "topic": "start",
    },
    "mainToolbar": {
        "title": "Toolbar",
        "text": "New, Open, Save, Undo, Select, Wire, zoom, print, and Circuit Builder. Hover for the name.",
        "topic": "start",
    },
    "helpDock": {
        "title": "Help",
        "text": "Searchable how-tos and the task checklists. F1 opens this panel. It works offline.",
        "topic": "start",
    },
    "Wire": {
        "title": "Wire",
        "text": "Click a terminal, then the next one. Drag the line to route it. Esc leaves wire mode.",
        "topic": "wires",
    },
    "Select": {
        "title": "Select",
        "text": "Select parts and wires. Esc also leaves wire mode.",
        "topic": "devices",
    },
    "Import CSV…": {
        "title": "Import CSV",
        "text": (
            "Open the parts-list guide, then pick a vendor CSV. "
            "Part Number, Description, Qty, and Manufacturer are enough. "
            "A title line above the header is fine."
        ),
        "topic": "csv",
    },
    "Import BOM…": {
        "title": "Import BOM",
        "text": (
            "Same parts-list guide as Import CSV. A row with no tag stays needs-info "
            "and the print is marked NOT FOR BUILD until you choose a library part."
        ),
        "topic": "csv",
    },
    "Import…": {
        "title": "Import",
        "text": "A project, a parts list, a wire list, or an I/O list. An unknown file offers the likely fix.",
        "topic": "import",
    },
    "Import print…": {
        "title": "Import print",
        "text": "Read a PDF, a picture, a DXF, or an SVG. Review each device before it is kept.",
        "topic": "print-import",
    },
    "Print Package…": {
        "title": "Print package",
        "text": "The customer PDF: drawings, BOM, wires, and the open checks.",
        "topic": "print",
    },
    "New": {
        "title": "New",
        "text": "Start a blank project. You pick the print style, then the sheet opens.",
        "topic": "new-project",
    },
    "Open…": {
        "title": "Open",
        "text": "Open a PanelCAD YAML project.",
        "topic": "start",
    },
    "Save": {
        "title": "Save",
        "text": "Write the project YAML. Save As picks a new file.",
        "topic": "new-project",
    },
    "csvImportPath": {
        "title": "CSV file",
        "text": (
            "The vendor file. The first row of data can be a title. "
            "The header itself names Part Number, Description, Qty, and Manufacturer, "
            "or the usual aliases."
        ),
        "topic": "csv",
    },
    "openSample": {
        "title": "Sample project",
        "text": "A copy of a catalog starter. Edit the copy. The original sample stays unchanged.",
        "topic": "sample",
    },
}


def task_button_label(action: str) -> str:
    """A command is Do this. A how-to step is Read more."""

    if str(action).endswith("-help"):
        return "Read more"
    return "Do this"


def task_ids() -> tuple[str, ...]:
    return TASK_ORDER


def task_def(task_id: str) -> dict[str, object] | None:
    found = TASKS.get(task_id)
    if found is None:
        return None
    return found


def task_step_ids(task_id: str) -> set[str]:
    found = TASKS.get(task_id)
    if found is None:
        return set()
    return {str(step["id"]) for step in found["steps"]}  # type: ignore[index]


def tour_step(index: int) -> dict[str, str] | None:
    if index < 0 or index >= len(TOUR_STEPS):
        return None
    return TOUR_STEPS[index]


def whats_this(key: str) -> dict[str, str] | None:
    found = WHATS_THIS.get(str(key or ""))
    if found is None:
        return None
    return {"id": key, **found}


def csv_import_summary() -> str:
    """Always visible on the CSV dialog. The longer note can be dismissed."""

    return (
        "Use a header row. Part Number, Description, Qty, and Manufacturer are enough. "
        "Aliases such as Part #, catalog number, brand, and quantity are the same columns. "
        "A title line above that header is fine — you do not have to delete it. "
        "A tag is optional. A row with a part number and no tag is kept as needs-info: "
        "no millimetres, off the quote and the cutouts. The print is marked NOT FOR BUILD "
        "until you choose a library part. A file with From and To is a wire list; use "
        "File → Import wires for that one."
    )


def csv_import_note() -> dict[str, str]:
    return {
        "title": "If the last CSV did not import",
        "body": (
            "The usual miss is a title row, or columns named Part # and Qty instead of "
            "Part Number and Quantity. PanelCAD still finds that header. "
            "Example only, not a catalog number:\n"
            "Part Number,Description,Qty,Manufacturer\n"
            "ABC-123,Example device,1,Example Maker\n"
            "Leave this note hidden once the columns are familiar. The short guide above stays."
        ),
    }


def print_import_note() -> dict[str, str]:
    return {
        "title": "What this form does",
        "body": (
            "Pick a PDF, a picture, a DXF, or an SVG. Text and lines already in the file are read from the file. "
            "An SVG is not sent to the photo reader. "
            "A scan uses the vision backend below — Test connection checks it before you start. "
            "Open review, and accept or reject each device, before it becomes the drawing. "
            "This form does not use the datasheet AI account."
        ),
    }


def quote_empty_copy(part_count: int) -> str:
    if part_count:
        return ""
    return (
        "No parts on the quote yet. Next: place devices or import a vendor parts list, "
        "then enter prices here. The quote stays in this file and nothing is sent."
    )


def library_search_empty(query: str, count: int) -> str:
    text = str(query or "").strip()
    if count or not text:
        return ""
    return (
        f"No symbol matches “{text}”. "
        "Next: clear the search, or use Catalog parts… for a manufacturer number."
    )
