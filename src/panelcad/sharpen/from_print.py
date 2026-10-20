"""Recreate any controls print (one or more PDF pages) in a new PanelCAD drawing.

Job-specific names, refs, and title-block values come from the attached
print, never from this harness. The harness only: rasterize the pages,
list catalog families, nudge the process, and score process completeness.
"""

from __future__ import annotations

import base64
import json
import re
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from panelcad import Session
from panelcad.cli.agent import OPS
from panelcad.core.models import normalize_drawing_style
from panelcad.library.catalog import normalize_standard
from panelcad.sharpen.tasks import Task

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_PDF = PACKAGE_ROOT / "examples" / "dol_starter.pdf"

PHASES = ("TITLE", "PLACE", "WIRE", "CLOSE")
PHASE_OPS: dict[str, tuple[str, ...]] = {
    "TITLE": ("create_project", "save", "set_title_block", "set_sheet_title"),
    "PLACE": (
        "library_search",
        "place",
        "add_sheet",
        "set_active_sheet",
        "set_sheet_title",
    ),
    "WIRE": ("wire",),
    "CLOSE": ("cleanup", "validate", "save", "done"),
}

INSTRUCTION_TITLE = """PHASE TITLE. Recreate THIS printed page's heading in PanelCAD.
The image is the only source of truth. Ops: create_project, save, set_title_block, set_sheet_title.

- create_project name = the heading printed on this sheet only (copy the words you can read).
- If PRINTED PAGE text is listed, the name must share a word with that page's project, title, or description. Do not invent one.
- Title-block: copy printed boxes only. Blank if unread. FIG. n is drawing_number, not a company.
- set_sheet_title from this page's heading.

Order: create_project → save → set_title_block.
One JSON op per turn. Never click a GUI.
"""

INSTRUCTION_PLACE = """PHASE PLACE. Place only symbols drawn on THIS printed page.
The image is the only source of truth.
Do NOT dump a motor-starter kit (START + STOP + fuse + coil) unless that kit is lettered on the print.
Ops: library_search, place, add_sheet, set_active_sheet, set_sheet_title.

- library_search the largest unique device first (PLC, VFD, breaker, motor, E-stop, HMI). Then place it.
- Place only symbols that are drawn. L1, L2, L3, N, T1–T3 are NETS, not devices.
- Device letters follow the print: IEC Q/K/F/M or US CB/M/OL/MTR. When the printed refs are those power letters and the page has no START/STOP, do not place a pushbutton or a control relay.
- Role words (any print):
  START / RESET → iec.operators.pb_no
  STOP → iec.operators.pb_nc
  E-STOP / mushroom / EMO → iec.operators.estop  (START is not E-STOP)
  FU / fuse → iec.protection.fuse_10a
  OL → iec.protection.overload_9a
  PE / GND / earth → iec.terminals.pe_4
  motor circle → iec.loads.motor_3ph or iec.loads.motor_1ph
  coil / MCR / CR → iec.contactors.coil or iec.relays.k_4co_24vdc
  T / CCT / transformer → iec.power.control_transformer
  LT / PL / lamp → iec.operators.pilot_light
  HOA / SS → iec.operators.selector_3pos
  LS / limit / float → iec.operators.limit_no
  TR / TD / timer → iec.timers.on_delay
  PLC / CPU → iec.plc.compact_16dio
  HMI → iec.hmi.panel_7in
  VFD / drive / inverter / GS → iec.drives.vfd_1k5
  STACK / beacon / audible / horn → iec.operators.stack_light
  illuminated / touch button → iec.operators.pb_illum
- A coil on a ladder is a circle (contactors.coil), not a 4-pole relay box.
- Do not place notes.block or power.load_24v. Those rectangles are not the device.
- Do not invent PLC, HMI, or VFD unless that device is drawn.
- Do not invent parts, wire numbers, or gauges.

Catalog: library_search query + standard=IEC or standard=NEMA/ANSI
iec.operators.pb_no, iec.operators.pb_nc, iec.operators.estop
iec.operators.pilot_light, iec.operators.selector_3pos, iec.operators.limit_no
iec.protection.fuse_10a, iec.protection.overload_9a, iec.breakers.mcb_3p_32a
iec.contactors.coil, iec.contactors.ac3_9a, iec.contactors.aux_no
iec.relays.k_4co_24vdc, iec.timers.on_delay
iec.loads.motor_3ph, iec.loads.motor_1ph, iec.loads.solenoid
iec.power.control_transformer, iec.power.psu_24v_5a
iec.terminals.x_2p5, iec.terminals.pe_4
iec.drives.vfd_1k5, iec.plc.compact_16dio, iec.hmi.panel_7in
nema.contactors.coil, nema.power.control_transformer, nema.loads.motor_3ph
nema.operators.pb_no, nema.protection.fuse_10a, nema.protection.overload

Order: library_search → place.
One JSON op per turn. Never click a GUI.
"""

INSTRUCTION_WIRE = """PHASE WIRE. Wire the circuit drawn on THIS printed page.
The image is the only source of truth. Ops: wire.

- Wire a circuit that could work: a path A→B→C along the drawing.
  from/to = Ref.Pin. Never a pin to itself. Never A1–A2 on the same coil.
  Never the same two pins twice in reverse. Never star every device onto one pin.

Order: wire the next net on the print.
One JSON op per turn. Never click a GUI.
"""

INSTRUCTION_CLOSE = """PHASE CLOSE. Finish THIS printed page. Do not place or wire.
Ops: cleanup, validate, save, done.

- cleanup overlapping symbols.
- validate. Empty errors = ok.
- save the yaml.
- Send done only after cleanup, validate, and save.

Order: cleanup → validate → save → done.
One JSON op per turn. Never click a GUI.
"""

INSTRUCTION_TITLE_NEMA = """PHASE TITLE. Recreate THIS printed page's heading in PanelCAD.
The image is the only source of truth. Use NEMA/ANSI. Ops: create_project, save, set_title_block, set_sheet_title.

- create_project name = the heading printed on this sheet only (copy the words you can read).
- If PRINTED PAGE text is listed, the name must share a word with that page's project, title, or description. Do not invent one.
- Title-block: copy printed boxes only. Blank if unread. FIG. n is drawing_number, not a company.
  set_title_block standard=NEMA/ANSI. coil_style=square. drawing_style=ladder when the print is a ladder.
- set_sheet_title from this page's heading.

Order: create_project → save → set_title_block.
One JSON op per turn. Never click a GUI.
"""

INSTRUCTION_PLACE_NEMA = """PHASE PLACE. Place only symbols drawn on THIS printed page.
The image is the only source of truth.
Use NEMA/ANSI symbols only (nema.*). Do not place iec.* or oem.*.
Do NOT dump a motor-starter kit (START + STOP + fuse + coil) unless that kit is lettered on the print.
Ops: library_search, place, add_sheet, set_active_sheet, set_sheet_title.

- library_search the largest unique device first. Always pass standard=NEMA/ANSI. Then place it.
- Place only symbols that are drawn. L1, L2, L3, N, T1–T3 are NETS, not devices.
- Device letters: Q disconnect, K contactor, F protection, M motor. When the printed refs are those power letters and the page has no START/STOP, do not place a pushbutton or a control relay.
- Role words (any print):
  START / RESET → nema.operators.pb_no
  STOP → nema.operators.pb_nc
  E-STOP / mushroom / EMO → nema.operators.estop  (START is not E-STOP)
  FU / fuse → nema.protection.fuse_10a
  OL → nema.protection.overload
  PE / GND / earth → nema.terminals.pe
  motor circle → nema.loads.motor_3ph or nema.loads.motor_1ph
  coil / M / MCR / CR / K → nema.contactors.coil
  aux NO / 3-4 → nema.contactors.aux_no
  aux NC / 1-2 → nema.contactors.aux_nc
  T / CCT / transformer → nema.power.control_transformer
  LT / PL / lamp → nema.operators.pilot_light
  HOA / SS → nema.operators.selector_3pos
  LS / limit / float → nema.operators.limit_no
  TR / TD / timer → nema.timers.on_delay
  Q / disconnect / isolator → nema.protection.disconnect_3p
  STACK / beacon / audible / horn → nema.operators.stack_light
  illuminated / touch button → nema.operators.pb_illum
- A coil is a circle. Do not place a 4-pole relay box or a generic load rectangle.
- Do not invent PLC, HMI, or VFD unless that device is drawn.
- Do not invent parts, wire numbers, or gauges.

Catalog: library_search query + standard=NEMA/ANSI
nema.operators.pb_no, nema.operators.pb_nc, nema.operators.estop
nema.operators.pb_illum, nema.operators.stack_light
nema.operators.pilot_light, nema.operators.selector_3pos
nema.operators.limit_no, nema.operators.limit_nc
nema.protection.fuse_10a, nema.protection.overload, nema.protection.ol_contact
nema.protection.disconnect_3p
nema.contactors.coil, nema.contactors.aux_no, nema.contactors.aux_nc
nema.timers.on_delay
nema.loads.motor_3ph, nema.loads.motor_1ph
nema.power.control_transformer
nema.terminals.pe

Order: library_search → place.
One JSON op per turn. Never click a GUI.
"""

INSTRUCTION_WIRE_NEMA = """PHASE WIRE. Wire the circuit drawn on THIS printed page.
The image is the only source of truth. Use NEMA/ANSI. Ops: wire.

- Wire a circuit that could work: a path A→B→C along the drawing.
  from/to = Ref.Pin. Never a pin to itself. Never A1–A2 on the same coil.
  Never the same two pins twice in reverse. Never star every device onto one pin.

Order: wire the next net on the print.
One JSON op per turn. Never click a GUI.
"""

INSTRUCTION_CLOSE_NEMA = INSTRUCTION_CLOSE

INSTRUCTION = "\n".join(
    (INSTRUCTION_TITLE, INSTRUCTION_PLACE, INSTRUCTION_WIRE, INSTRUCTION_CLOSE)
)
INSTRUCTION_NEMA = "\n".join(
    (
        INSTRUCTION_TITLE_NEMA,
        INSTRUCTION_PLACE_NEMA,
        INSTRUCTION_WIRE_NEMA,
        INSTRUCTION_CLOSE_NEMA,
    )
)

LADDER_ADDENDUM = """
drawing_style=ladder. Horizontal rungs between L1 (hot) and L2 (return / reference).
A rung is Boolean: contacts in series = AND, parallel = OR, coil or lamp = the output (power on/off).
Each device has two ends. Every input feeds a load. Every circuit starts at L1 and ends at L2 (or N / 0V).
Motor / power distribution is not a ladder rung — keep it off L1/L2.
Never wire coil A1/A2, START/STOP, or L1/L2 to motor T1/T2/T3.
set_title_block drawing_style=ladder. cleanup after wiring.
"""

MULTIPAGE_ADDENDUM = """
There are {n} attached page images. One PanelCAD sheet per image.
Page 1 = first image (sheet main after create_project).
Pages 2..{n}: add_sheet then set_active_sheet, then place only that page's symbols.
Never dump page-1 devices onto later pages. set_sheet_title from the heading on that image.
"""

_FROM_PRINT_STANDARD: ContextVar[str] = ContextVar("from_print_standard", default="IEC")
_FROM_PRINT_STYLE: ContextVar[str] = ContextVar("from_print_style", default="schematic")
_FROM_PRINT_PAGE_COUNT: ContextVar[int] = ContextVar("from_print_page_count", default=1)
_FROM_PRINT_PAGE_TEXT: ContextVar[str] = ContextVar("from_print_page_text", default="")

# IEC role ids → NEMA/ANSI twins. Suffix swap covers the rest of iec.*.
_IEC_TO_NEMA = {
    "iec.operators.pb_no": "nema.operators.pb_no",
    "iec.operators.pb_nc": "nema.operators.pb_nc",
    "iec.operators.estop": "nema.operators.estop",
    "iec.operators.pilot_light": "nema.operators.pilot_light",
    "iec.operators.selector_3pos": "nema.operators.selector_3pos",
    "iec.operators.limit_no": "nema.operators.limit_no",
    "iec.protection.fuse_10a": "nema.protection.fuse_10a",
    "iec.protection.overload_9a": "nema.protection.overload",
    "iec.protection.ol_contact": "nema.protection.ol_contact",
    "iec.protection.disconnect_3p": "nema.protection.disconnect_3p",
    "iec.terminals.pe_4": "nema.terminals.pe",
    "iec.contactors.coil": "nema.contactors.coil",
    "iec.contactors.ac3_9a": "nema.contactors.coil",
    "iec.contactors.aux_no": "nema.contactors.aux_no",
    "iec.contactors.aux_nc": "nema.contactors.aux_nc",
    "iec.relays.k_4co_24vdc": "nema.contactors.coil",
    "iec.timers.on_delay": "nema.timers.on_delay",
    "iec.power.control_transformer": "nema.power.control_transformer",
    "iec.loads.motor_3ph": "nema.loads.motor_3ph",
    "iec.loads.motor_1ph": "nema.loads.motor_1ph",
    "iec.breakers.mcb_3p_32a": "nema.protection.disconnect_3p",
}


def from_print_standard() -> str:
    return _FROM_PRINT_STANDARD.get()


def from_print_style() -> str:
    return _FROM_PRINT_STYLE.get()


def from_print_page_count() -> int:
    return _FROM_PRINT_PAGE_COUNT.get()


def from_print_page_text(job: Path | None = None) -> str:
    """Printed words for this run. Empty when the page has no text layer."""

    text = _FROM_PRINT_PAGE_TEXT.get()
    if text.strip():
        return text
    if job is None:
        return ""
    parent = job.parent
    # Beside a from-print job only. A missing path's cwd is not a run dir.
    if not parent.is_dir():
        return ""
    if not job.is_file() and not (parent / "print-source.txt").is_file():
        return ""
    parts: list[str] = []
    for path in sorted(parent.glob("print-p*-text.txt")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if raw.strip():
            parts.append(raw.strip())
    return "\n".join(parts)


def set_from_print_page_text(text: str) -> Token:
    return _FROM_PRINT_PAGE_TEXT.set(text or "")


def set_from_print_standard(standard: str) -> Token:
    return _FROM_PRINT_STANDARD.set(normalize_standard(standard))


def set_from_print_style(style: str) -> Token:
    return _FROM_PRINT_STYLE.set(normalize_drawing_style(style))


def phase_instruction(phase: str, standard: str | None = None) -> str:
    """Current-phase card only. IEC/NEMA twins; addenda stay on the matching phase."""

    std = normalize_standard(standard) if standard else from_print_standard()
    nema = std == "NEMA/ANSI"
    cards = {
        "TITLE": INSTRUCTION_TITLE_NEMA if nema else INSTRUCTION_TITLE,
        "PLACE": INSTRUCTION_PLACE_NEMA if nema else INSTRUCTION_PLACE,
        "WIRE": INSTRUCTION_WIRE_NEMA if nema else INSTRUCTION_WIRE,
        "CLOSE": INSTRUCTION_CLOSE_NEMA if nema else INSTRUCTION_CLOSE,
    }
    key = str(phase or "TITLE").upper()
    text = cards.get(key) or cards["TITLE"]
    if key == "WIRE" and from_print_style() == "ladder":
        text = text.rstrip() + "\n" + LADDER_ADDENDUM
    n = from_print_page_count()
    if n > 1 and key == "TITLE":
        text = text.rstrip() + (
            f"\nThere are {n} page images. create_project / title-block from page 1 only.\n"
        )
    if n > 1 and key == "PLACE":
        text = text.rstrip() + "\n" + MULTIPAGE_ADDENDUM.format(n=n)
    return text


def build_instruction(standard: str | None = None) -> str:
    std = normalize_standard(standard) if standard else from_print_standard()
    parts = [phase_instruction(p, std) for p in PHASES]
    n = from_print_page_count()
    if n > 1:
        parts.append(
            "Order: create_project → save → set_title_block → "
            "library_search → place → wire → add_sheet → set_active_sheet → "
            "place → wire → cleanup → validate → save → done.\n"
        )
    return "\n".join(parts)


def _lib(iec_id: str) -> str:
    """Catalog id for the active drawing standard (IEC default, nema.* when ANSI)."""

    lib = str(iec_id or "")
    if from_print_standard() != "NEMA/ANSI":
        return lib
    low = lib.lower()
    if low in _IEC_TO_NEMA:
        return _IEC_TO_NEMA[low]
    if low.startswith("iec."):
        return "nema." + lib.split(".", 1)[1]
    return lib

# Alphanumeric tokens only. Print line names, not a job.
NET_REFS = frozenset(
    {
        "L1",
        "L2",
        "L3",
        "N",
        "L",
        "T1",
        "T2",
        "T3",
        "0V",
        "24V",
        "24VDC",
        "24VAC",
        "120V",
        "120VAC",
        "480V",
        "480VAC",
        "COM",
        "NEUTRAL",
    }
)

_PIN_CACHE: dict[str, list[str]] = {}


def _alnum_upper(ref: str) -> str:
    return "".join(ch for ch in (ref or "").upper() if ch.isalnum())


def _role_library(ref: str) -> str | None:
    """Map print labels to catalog families. Role words/prefixes, not a job name."""

    token = _alnum_upper(ref)
    if not token:
        return None
    if "ESTOP" in token or token in {"ES", "EMO"} or (
        token.startswith("ES") and token[2:].isdigit()
    ):
        return _lib("iec.operators.estop")
    if token in {"START", "STARTPB", "PBSTART", "PBNO"} or token.startswith("START"):
        return _lib("iec.operators.pb_no")
    if token.startswith("PB") and (len(token) == 2 or token[2:].isdigit()):
        return _lib("iec.operators.pb_no")
    if token in {"STOP", "STOPPB", "PBSTOP", "PBNC"} or token.startswith("STOP"):
        return _lib("iec.operators.pb_nc")
    if token in {"RESET", "RESETPB", "PBRESET"} or token.startswith("RESET"):
        return _lib("iec.operators.pb_no")
    if token in {"OL", "OLR", "OVERLOAD"} or token.startswith("OL"):
        if from_print_style() == "ladder":
            return _lib("iec.protection.ol_contact")
        return _lib("iec.protection.overload_9a")
    if (
        token in {"GND", "PE", "XPE", "EARTH", "GROUND"}
        or token.startswith("GND")
        or (token.startswith("PE") and (len(token) == 2 or token[2:].isdigit()))
    ):
        return _lib("iec.terminals.pe_4")
    if token.startswith("FU") or token.startswith("FUSE"):
        return _lib("iec.protection.fuse_10a")
    if token == "F" or (token.startswith("F") and token[1:].isdigit()):
        return _lib("iec.protection.overload_9a")
    if token in {"T", "CCT", "XFMR", "XFRM", "XFR", "PRI", "SEC"} or token.startswith("XFMR"):
        return _lib("iec.power.control_transformer")
    if token in {"LT", "PL", "PILOT", "LAMP"} or token.startswith("LT") or (
        token.startswith("PL") and (len(token) == 2 or token[2:].isdigit())
    ):
        return _lib("iec.operators.pilot_light")
    if token in {"HOA", "SS", "SS1", "SS2"} or token.startswith("HOA"):
        return _lib("iec.operators.selector_3pos")
    if token in {"LS", "LIM", "LIMIT", "FLT", "FS"} or token.startswith("LS"):
        return _lib("iec.operators.limit_no")
    if token in {"TR", "TD", "TMR", "TON", "TIMER", "KT"} or token.startswith("TR") or token.startswith("TD"):
        return _lib("iec.timers.on_delay")
    if token in {"Y", "SOL", "SOLENOID"} or token.startswith("SOL"):
        return _lib("iec.loads.solenoid")
    if token == "K" or (token.startswith("K") and token[1:].isdigit()):
        return _lib("iec.contactors.ac3_9a")
    if token == "Q" or (token.startswith("Q") and token[1:].isdigit()):
        return _lib("iec.breakers.mcb_3p_32a")
    if token.startswith("CR") and any(tag in token for tag in ("34", "13", "14", "12", "AUX", "NO", "NC")):
        if "NC" in token or "12" in token:
            return _lib("iec.contactors.aux_nc")
        return _lib("iec.contactors.aux_no")
    if token in {"MCR", "SCR"} or (
        token.startswith("CR") and (len(token) == 2 or token[2:].isdigit())
    ):
        if from_print_style() == "ladder":
            return _lib("iec.contactors.coil")
        return _lib("iec.relays.k_4co_24vdc")
    if "STACK" in token or token in {"BEACON", "HORN", "AUDIBLE"}:
        return _lib("iec.operators.stack_light")
    if token.startswith("TOUCH") or "ILLUM" in token:
        return _lib("iec.operators.pb_illum")
    if "PLC" in token or token in {"CPU"} or token.startswith("CPU"):
        return _lib("iec.plc.compact_16dio")
    if "HMI" in token:
        return _lib("iec.hmi.panel_7in")
    if (
        token in {"VFD", "INVERTER", "DRIVE"}
        or "VFD" in token
        or "INVERTER" in token
        or (token.startswith("GS") and token[2:].isdigit())
    ):
        return _lib("iec.drives.vfd_1k5")
    return None


def _is_net_ref(ref: str) -> bool:
    return _alnum_upper(ref) in NET_REFS


def _family_key(library_id: str) -> str:
    parts = [p for p in str(library_id or "").split(".") if p]
    if len(parts) >= 2:
        return parts[1].lower()
    return str(library_id or "").lower()


def _kind_key(library_id: str) -> str:
    parts = [p for p in str(library_id or "").split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:]).lower()
    return str(library_id or "").lower()


def _placed_libs(turns: list[dict[str, Any]] | None, job: Path) -> set[str]:
    libs = {lib.lower() for _ref, lib in _placed_map(turns).values() if lib}
    if job.is_file():
        session = Session.open(job)
        for sheet in session.project.sheets:
            for comp in sheet.components:
                if comp.library_id and not _is_net_ref(comp.id):
                    libs.add(comp.library_id.lower())
    return libs


def _lib_match(libs: set[str], needle: str) -> bool:
    token = needle.lower()
    aliases = {"overload": ("overload", "ol_contact")}
    tokens = aliases.get(token, (token,))
    return any(any(t in lib for t in tokens) for lib in libs)


def _wired_refs(turns: list[dict[str, Any]] | None, job: Path) -> set[str]:
    refs: set[str] = set()
    for turn in turns or []:
        if turn.get("op") != "wire":
            continue
        reply = turn.get("reply") or {}
        if not reply.get("ok"):
            continue
        result = reply.get("result") or {}
        msg = turn.get("msg") or {}
        for key in ("from", "to"):
            raw = str(result.get(key) or msg.get(key) or "")
            if raw:
                refs.add(raw.split(".", 1)[0].upper())
    if job.is_file():
        session = Session.open(job)
        for sheet in session.project.sheets:
            for wire in sheet.wires:
                for raw in (wire.frm, wire.to):
                    if raw:
                        refs.add(str(raw).split(".", 1)[0].upper())
    return refs


def _refs_of_kind(turns: list[dict[str, Any]] | None, job: Path, needle: str) -> set[str]:
    hits: set[str] = set()
    token = needle.lower()
    for ref, lib in _all_placed(turns, job).items():
        if token in lib.lower():
            hits.add(ref.upper())
    return hits


def _coil_refs(turns: list[dict[str, Any]] | None, job: Path) -> set[str]:
    """Coil/contactor bodies only — aux contacts are a different device."""

    hits: set[str] = set()
    for ref, lib in _all_placed(turns, job).items():
        low = lib.lower()
        if "aux" in low:
            continue
        if "coil" in low or "relays." in low or "contactors" in low:
            hits.add(ref.upper())
    return hits


def _is_contact_lib(lib: str) -> bool:
    """Input devices: operators and aux that make the IF side of a rung."""

    low = str(lib or "").lower()
    if "aux" in low or "ol_contact" in low:
        return True
    return any(t in low for t in ("pb_no", "pb_nc", "estop", "limit", "selector"))


def _is_load_lib(lib: str) -> bool:
    """THEN side: coil, lamp, solenoid. Aux is an input, not a load."""

    low = str(lib or "").lower()
    if "aux" in low:
        return False
    return any(t in low for t in ("coil", "pilot", "solenoid", "motor", "relays.")) or low.endswith(
        "light"
    )


def _is_earth_lib(lib: str) -> bool:
    low = str(lib or "").lower()
    return "terminals.pe" in low or ".pe_" in low or low.endswith(".pe")


def _is_power_side_lib(lib: str) -> bool:
    """Not on the L1–L2 control assignment (motor T-leads, earth, transformer)."""

    low = str(lib or "").lower()
    if _is_earth_lib(low):
        return True
    return any(t in low for t in ("motor", "transformer", "psu", "plc.", "drives.", "hmi."))


def _motor_on_ladder_rails(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """Motor T-leads on L1/L2 — power sitting on the control rails."""

    from panelcad.core.circuit import is_ladder_rail, is_motor_library

    placed = _all_placed(turns, job)
    for a, _ap, b, _bp in _wire_ends(turns, job):
        a_lib = placed.get(a, "")
        b_lib = placed.get(b, "")
        if is_motor_library(a_lib) and is_ladder_rail(b):
            return (
                f"{a} is on the ladder rails; motor is power, not a rung. "
                "Keep it off L1/L2."
            )
        if is_motor_library(b_lib) and is_ladder_rail(a):
            return (
                f"{b} is on the ladder rails; motor is power, not a rung. "
                "Keep it off L1/L2."
            )
    return None


def _device_degree(turns: list[dict[str, Any]] | None, job: Path) -> dict[str, int]:
    deg: dict[str, int] = {}
    for a, _ap, b, _bp in _wire_ends(turns, job):
        for ref in (a, b):
            if not ref or _is_net_ref(ref):
                continue
            token = ref.upper()
            deg[token] = deg.get(token, 0) + 1
    return deg


def _device_adj(turns: list[dict[str, Any]] | None, job: Path) -> dict[str, set[str]]:
    """Device-to-device edges only (rails/nets omitted)."""

    adj: dict[str, set[str]] = {}
    for a, _ap, b, _bp in _wire_ends(turns, job):
        if not a or not b or _is_net_ref(a) or _is_net_ref(b):
            continue
        adj.setdefault(a.upper(), set()).add(b.upper())
        adj.setdefault(b.upper(), set()).add(a.upper())
    return adj


def _full_adj(turns: list[dict[str, Any]] | None, job: Path) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {}
    for a, _ap, b, _bp in _wire_ends(turns, job):
        if not a or not b:
            continue
        adj.setdefault(a.upper(), set()).add(b.upper())
        adj.setdefault(b.upper(), set()).add(a.upper())
    return adj


def _reaches(adj: dict[str, set[str]], start: str, goals: set[str]) -> bool:
    return _reaches_avoid(adj, start, goals, set())


def _reaches_avoid(
    adj: dict[str, set[str]],
    start: str,
    goals: set[str],
    forbidden: set[str],
) -> bool:
    """Walk adj from start to any goal without visiting forbidden nodes (e.g. the other rail)."""

    want = {g.upper() for g in goals}
    ban = {f.upper() for f in forbidden}
    src = start.upper()
    if src in ban:
        return False
    if src in want:
        return True
    seen = {src}
    stack = [src]
    while stack:
        node = stack.pop()
        for nxt in adj.get(node, ()):
            up = nxt.upper()
            if up in ban or up in seen:
                continue
            if up in want:
                return True
            seen.add(up)
            stack.append(up)
    return False


def _hot_rails(turns: list[dict[str, Any]] | None, job: Path) -> set[str]:
    names = {"L1", "L", "24V", "24VDC", "24VAC", "120V", "120VAC", "480V", "480VAC"}
    if job.is_file():
        session = Session.open(job)
        hot = str((session.get_title_block() or {}).get("rail_hot") or "").strip()
        if hot:
            names.add(hot.upper())
    return names


def _return_rails(turns: list[dict[str, Any]] | None, job: Path) -> set[str]:
    names = {"L2", "N", "0V", "0VDC", "COM", "NEUTRAL"}
    if job.is_file():
        session = Session.open(job)
        ret = str((session.get_title_block() or {}).get("rail_return") or "").strip()
        if ret:
            names.add(ret.upper())
    return names


def _circuit_laws_gap(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """Ladder as a Boolean assignment: inputs (if) → load (then), L1 to L2.

    Avoid walking through the opposite rail so a finished parallel rung cannot
    launder a dead-end coil back to L2 (sneak path).
    """

    if from_print_style() != "ladder":
        return None
    placed = _all_placed(turns, job)
    if len(placed) < 2:
        return None
    contacts = [r for r, lb in placed.items() if _is_contact_lib(lb)]
    loads = [r for r, lb in placed.items() if _is_load_lib(lb) and not _is_power_side_lib(lb)]
    hot = _hot_rails(turns, job)
    ret = _return_rails(turns, job)
    full = _full_adj(turns, job)
    hot_name = "L1" if "L1" in hot else sorted(hot)[0]
    ret_name = "L2" if "L2" in ret else sorted(ret)[0]
    deg = _device_degree(turns, job)
    short = [
        ref
        for ref in sorted(placed)
        if deg.get(ref, 0) < 2 and not _is_earth_lib(placed.get(ref) or "")
    ]
    if short:
        ref = short[0]
        lib = placed.get(ref) or ""
        neighbors = full.get(ref, set())
        touches_hot = bool(neighbors & hot)
        touches_ret = bool(neighbors & ret)
        touches_load = bool(neighbors & set(loads))
        if _is_load_lib(lib) and not _is_power_side_lib(lib):
            if not touches_ret:
                pin = "X2" if "pilot" in lib.lower() or lib.lower().endswith("light") else "A2"
                example = (
                    f'{{"op":"wire","from":"{ref}.{pin}","to":"{ret_name}",'
                    f'"net":"{ret_name}","sheet":"main"}}'
                )
            else:
                src = next((c for c in contacts if c != ref), contacts[0] if contacts else hot_name)
                src_end = _ex_pin(turns, job, src, "14", "4") if src in placed else src
                pin = "X1" if "pilot" in lib.lower() or lib.lower().endswith("light") else "A1"
                example = (
                    f'{{"op":"wire","from":"{src_end}","to":"{ref}.{pin}",'
                    f'"net":"{hot_name}","sheet":"main"}}'
                )
        elif not touches_hot:
            example = (
                f'{{"op":"wire","from":"{hot_name}",'
                f'"to":"{_ex_pin(turns, job, ref, "13", "3", "1")}",'
                f'"net":"{hot_name}","sheet":"main"}}'
            )
        elif loads and not touches_load:
            dest = loads[0]
            pin = (
                "X1"
                if "pilot" in (placed.get(dest) or "").lower()
                else "A1"
            )
            example = (
                f'{{"op":"wire","from":"{_ex_pin(turns, job, ref, "14", "4", "2")}",'
                f'"to":"{dest}.{pin}","net":"{hot_name}","sheet":"main"}}'
            )
        else:
            example = (
                f'{{"op":"wire","from":"{_ex_pin(turns, job, ref, "14", "4", "2")}",'
                f'"to":"{ret_name}","net":"{ret_name}","sheet":"main"}}'
            )
        return (
            f"{ref} has {deg.get(ref, 0)} wire(s); each device needs two ends in the circuit. "
            f"Example: {example}"
        )
    if contacts and not loads:
        return (
            "Input devices (if) need a load (coil or lamp) so the rung has a then. "
            f'Example: {{"op":"place","library_id":"{_lib("iec.contactors.coil")}","ref":"CR1","x":80,"y":80,"sheet":"main"}}'
        )
    adj = _device_adj(turns, job)
    for contact in sorted(contacts):
        if loads and not _reaches(adj, contact, set(loads)):
            dest = loads[0]
            return (
                f"{contact} is an input that does not feed a load (if → then). "
                f'Example: {{"op":"wire","from":"{_ex_pin(turns, job, contact, "14", "4")}","to":"{dest}.A1","net":"L1","sheet":"main"}}'
            )
    for load in sorted(loads):
        lib = placed.get(load) or ""
        if not _reaches_avoid(full, load, hot, ret):
            pin = "X1" if "pilot" in lib.lower() or lib.lower().endswith("light") else "A1"
            src = contacts[0] if contacts else hot_name
            src_end = (
                _ex_pin(turns, job, src, "14", "4") if src in (placed or {}) else src
            )
            return (
                f"{load} is not assigned from {hot_name} (no if). "
                f'Example: {{"op":"wire","from":"{src_end}","to":"{load}.{pin}","net":"{hot_name}","sheet":"main"}}'
            )
        if not _reaches_avoid(full, load, ret, hot):
            pin = "X2" if "pilot" in lib.lower() or lib.lower().endswith("light") else "A2"
            return (
                f"{load} does not return to {ret_name}. "
                f'Example: {{"op":"wire","from":"{load}.{pin}","to":"{ret_name}","net":"{ret_name}","sheet":"main"}}'
            )
    for contact in sorted(contacts):
        if not _reaches_avoid(full, contact, hot, ret):
            return (
                f"{contact} is not on a circuit from {hot_name}. "
                f'Example: {{"op":"wire","from":"{hot_name}","to":"{_ex_pin(turns, job, contact, "13", "3")}","net":"{hot_name}","sheet":"main"}}'
            )
        if not _reaches_avoid(full, contact, ret, hot):
            dest = loads[0] if loads else ret_name
            dest_end = f"{dest}.A1" if dest in placed else dest
            return (
                f"{contact} does not complete to {ret_name}. "
                f'Example: {{"op":"wire","from":"{_ex_pin(turns, job, contact, "14", "4")}","to":"{dest_end}","net":"L1","sheet":"main"}}'
            )
    return None


def _all_placed(turns: list[dict[str, Any]] | None, job: Path) -> dict[str, str]:
    """REF.upper() -> library_id from the turn trace and, if present, the job file."""

    out: dict[str, str] = {}
    for ref, lib in _placed_map(turns).values():
        if ref and lib:
            out[ref.upper()] = lib
    if job.is_file():
        session = Session.open(job)
        for sheet in session.project.sheets:
            for comp in sheet.components:
                if comp.library_id and not _is_net_ref(comp.id):
                    out[comp.id.upper()] = comp.library_id
    return out


def _parse_end(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if "." in text:
        ref, pin = text.split(".", 1)
        return ref.strip().upper(), pin.strip().upper()
    return text.upper(), ""


def _wire_ends(
    turns: list[dict[str, Any]] | None, job: Path
) -> list[tuple[str, str, str, str]]:
    ends: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(a_ref: str, a_pin: str, b_ref: str, b_pin: str) -> None:
        if not a_ref or not b_ref:
            return
        key = tuple(sorted((f"{a_ref}.{a_pin}", f"{b_ref}.{b_pin}")))
        if key in seen:
            return
        seen.add(key)
        ends.append((a_ref, a_pin, b_ref, b_pin))

    for turn in turns or []:
        if turn.get("op") != "wire":
            continue
        reply = turn.get("reply") or {}
        if not reply.get("ok"):
            continue
        result = reply.get("result") or {}
        msg = turn.get("msg") or {}
        a_ref, a_pin = _parse_end(str(result.get("from") or msg.get("from") or ""))
        b_ref, b_pin = _parse_end(str(result.get("to") or msg.get("to") or ""))
        _add(a_ref, a_pin, b_ref, b_pin)
    if job.is_file():
        session = Session.open(job)
        for sheet in session.project.sheets:
            for wire in sheet.wires:
                a_ref, a_pin = _parse_end(wire.frm)
                b_ref, b_pin = _parse_end(wire.to)
                _add(a_ref, a_pin, b_ref, b_pin)
    return ends


def _refs_connected(ends: list[tuple[str, str, str, str]], left: str, right: str) -> bool:
    a, b = left.upper(), right.upper()
    return any((x == a and y == b) or (x == b and y == a) for x, _ap, y, _bp in ends)


def _ref_pins_used(ends: list[tuple[str, str, str, str]], ref: str) -> set[str]:
    token = ref.upper()
    pins: set[str] = set()
    for a, ap, b, bp in ends:
        if a == token and ap:
            pins.add(ap)
        if b == token and bp:
            pins.add(bp)
    return pins


def _sheet_text(turns: list[dict[str, Any]] | None, job: Path) -> str:
    """Text Gemma already copied from the print (name, titles). Not a job list."""

    bits: list[str] = []
    for turn in turns or []:
        msg = turn.get("msg") or {}
        for key in ("name", "title", "drawing_number", "company"):
            val = msg.get(key)
            if val:
                bits.append(str(val))
    if job.is_file():
        session = Session.open(job)
        bits.append(session.project.name or "")
        tb = session.get_title_block()
        for key in ("company", "customer", "drawing_number"):
            bits.append(str(tb.get(key) or ""))
        for sheet in session.project.sheets:
            bits.append(sheet.title or "")
    return " ".join(bits).upper()


def _print_hints(text: str) -> set[str]:
    blob = (text or "").upper().replace("-", "")
    compact = blob.replace(" ", "")
    hints: set[str] = set()
    if any(k in blob for k in ("PLC", "CLICK", "CPU", "HMI")):
        hints.add("plc")
    if any(k in blob for k in ("VFD", "DRIVE", "INVERTER", "DURAPULSE", "GS20", "GS21")):
        hints.add("vfd")
    if any(k in blob for k in ("ESTOP", "EMERGENCY", "EMO", "MCR")):
        hints.add("estop")
    if "3WIRE" in compact or "THREEWIRE" in compact:
        hints.add("3wire")
    if "MOTOR" in blob:
        hints.add("motor")
    if "STARTER" in blob and "MOTOR" in blob:
        hints.add("3wire")
    if "RELAY" in blob or "PILOT" in blob:
        hints.add("relay")
    if "TRANSFORMER" in blob or "POWER SUPPLY" in blob:
        hints.add("transformer")
    return hints


_SMALL_WORDS = frozenset(
    {
        "THE",
        "AND",
        "FOR",
        "WITH",
        "FROM",
        "THIS",
        "THAT",
        "ARE",
        "WAS",
        "NOT",
        "BUT",
        "PER",
        "VIA",
        "INTO",
        "ONLY",
    }
)
# Fallback when a text layer has no PROJECT/TITLE/DESCRIPTION labels.
_PAGE_BOILERPLATE = _SMALL_WORDS | frozenset(
    {
        "SHEET",
        "PAGE",
        "DRAWING",
        "TITLE",
        "PROJECT",
        "DESCRIPTION",
        "COMPANY",
        "CUSTOMER",
        "LOCATION",
        "STANDARD",
        "SCALE",
        "SIZE",
        "DRAWN",
        "CHECKED",
        "APPROVED",
        "INITIAL",
        "ISSUE",
        "LOGO",
        "DATE",
        "REV",
        "NTS",
        "LAND",
        "PANELCAD",
        "DWG",
        "NUMBER",
        "IEC",
        "ANSI",
        "NEMA",
    }
)
_DESIGNATOR_RE = re.compile(r"\b([A-Z]{1,4})(\d{1,3})\b")
_TERMINAL_GROUPS = (
    frozenset({"L1", "L2", "L3"}),
    frozenset({"T1", "T2", "T3"}),
    frozenset({"U1", "V1", "W1"}),
)
_CLASS_ALIAS = {"FU": "F", "FUSE": "F", "OL": "F", "OLR": "F"}
_POWER_LETTERS = frozenset({"Q", "K", "F", "M"})
_CONTROL_CLASSES = frozenset(
    {"S", "H", "B", "PB", "CR", "SS", "LT", "PL", "ES", "LS", "HOA", "MCR"}
)
# Letter → catalog family. Order is source-to-load, not a specific drawing.
_POWER_PLACE = (
    ("Q", "iec.breakers.mcb_3p_32a", ("breaker", "mcb", "disconnect")),
    ("K", "iec.contactors.ac3_9a", ("contactor",)),
    ("F", "iec.protection.overload_9a", ("overload", "ol_contact", "fuse")),
    ("M", "iec.loads.motor_3ph", ("motor",)),
)


def _real_words(text: str) -> set[str]:
    return {
        word.upper()
        for word in re.findall(r"[A-Za-z]{3,}", text or "")
        if word.upper() not in _SMALL_WORDS
    }


def _flat_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _between(flat: str, start: str, end: str) -> str:
    match = re.search(
        rf"\b{start}\b\s+(.*?)(?:\s+\b{end}\b|$)",
        flat,
        flags=re.I,
    )
    if not match:
        return ""
    return match.group(1).strip(" -:")


def _company_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        token = line.upper()
        if token != "COMPANY" and not token.startswith("COMPANY "):
            continue
        rest = line[7:].strip(" :.-")
        if rest and rest.upper() not in {"LOGO", "NAME"}:
            return rest
        for nxt in lines[index + 1 : index + 4]:
            if not nxt:
                continue
            if nxt.upper() in {"LOGO", "PROJECT", "CUSTOMER", "LOCATION"}:
                break
            return nxt
    flat = _flat_text(text)
    match = re.search(
        r"\bCOMPANY\b\s+([A-Za-z][A-Za-z0-9&.,' -]{1,80}?)(?=\s+\d|\s+PROJECT\b|\s+CUSTOMER\b|\s+LOCATION\b|$)",
        flat,
        flags=re.I,
    )
    return match.group(1).strip() if match else ""


def _drawing_number(flat: str) -> str:
    match = re.search(
        r"\bDWG(?:\s+NO\.?|\s+NUMBER)?\s*\.?\s*([A-Z0-9][A-Z0-9./_-]{0,30})",
        flat,
        flags=re.I,
    )
    if match:
        token = match.group(1).strip(" .")
        if token.upper() not in {"NO", "NUMBER", "STANDARD"}:
            return token
    match = re.search(r"\bFIG\.?\s*([A-Z]?\d+[A-Z0-9-]*)", flat, flags=re.I)
    if match:
        return match.group(1)
    return ""


def _printed_fields(text: str) -> dict[str, Any]:
    """Project / title / description / company / drawing number from a text layer."""

    flat = _flat_text(text)
    project = _between(flat, "PROJECT", "TITLE")
    title = _between(flat, "TITLE", "DESCRIPTION")
    description = _between(flat, "DESCRIPTION", "DWG") or _between(
        flat, "DESCRIPTION", "STANDARD"
    )
    heading = " ".join(part for part in (project, title, description) if part)
    if heading.strip():
        words = _real_words(heading)
    else:
        words = _real_words(text) - _PAGE_BOILERPLATE
    return {
        "project": project,
        "title": title,
        "description": description,
        "company": _company_line(text),
        "drawing_number": _drawing_number(flat),
        "heading_words": words,
    }


def _device_designators(text: str) -> list[tuple[str, str]]:
    """IEC/NEMA device refs in the text layer. Supply and motor terminals are nets."""

    flat = _flat_text(text).upper()
    sizes = set(re.findall(r"\bSIZE\s+(A[0-5])\b", flat))
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for prefix, number in _DESIGNATOR_RE.findall(flat):
        ref = f"{prefix}{number}"
        if ref in sizes or ref in NET_REFS or ref in seen:
            continue
        seen.add(ref)
        kind = _CLASS_ALIAS.get(prefix, prefix)
        found.append((ref, kind))
    refs = {ref for ref, _kind in found}
    drop: set[str] = set()
    for group in _TERMINAL_GROUPS:
        if group <= refs:
            drop |= set(group)
    if not drop:
        return found
    return [(ref, kind) for ref, kind in found if ref not in drop]


def _has_operator_words(text: str) -> bool:
    return bool(re.search(r"\b(?:START|STOP|ESTOP|E-STOP)\b", text or "", flags=re.I))


def _is_power_subject(title: str, description: str) -> bool:
    blob = f"{title} {description}".upper()
    if re.search(r"\b(?:START|STOP|CONTROL|RELAY|LADDER|PLC|ESTOP|E-STOP)\b", blob):
        return False
    return bool(re.search(r"\bPOWER\b", blob))


def _is_control_designator(kind: str) -> bool:
    return kind in _CONTROL_CLASSES or kind.startswith("PB") or kind.startswith("CR")


def _power_device_page(text: str) -> bool:
    """Printed refs are power letters (Q/K/F/M), not a control sheet."""

    if not (text or "").strip() or _has_operator_words(text):
        return False
    fields = _printed_fields(text)
    subject = f"{fields['title']} {fields['description']}"
    if re.search(r"\b(?:CONTROL|RELAY|LADDER|PLC|ESTOP|E-STOP)\b", subject, flags=re.I):
        return False
    refs = _device_designators(text)
    if any(_is_control_designator(kind) for _ref, kind in refs):
        return False
    letters = {kind for _ref, kind in refs if kind in _POWER_LETTERS}
    if not letters:
        return False
    if _is_power_subject(str(fields["title"]), str(fields["description"])):
        return True
    return bool(letters & {"Q", "M"})


def _explicit_3wire(text: str) -> bool:
    compact = re.sub(r"[\s-]+", "", (text or "").upper())
    return "3WIRE" in compact or "THREEWIRE" in compact


def _suppress_starter_kit(text: str) -> bool:
    """Job name STARTER+MOTOR must not demand START/STOP on a power page."""

    if not (text or "").strip():
        return False
    if _explicit_3wire(text):
        return False
    if re.search(r"\bSTART\b", text, flags=re.I) and re.search(r"\bSTOP\b", text, flags=re.I):
        return False
    fields = _printed_fields(text)
    if _is_power_subject(str(fields["title"]), str(fields["description"])):
        return True
    return _power_device_page(text)


def _hint_blob(page: str) -> str:
    """Sheet subject for hints. Drop the job name stamped on every page."""

    flat = _flat_text(page)
    return re.sub(
        r"\bPROJECT\b\s+.*?\s+(?=\bTITLE\b)",
        "",
        flat,
        count=1,
        flags=re.I,
    )


def _active_hints(turns: list[dict[str, Any]] | None, job: Path) -> set[str]:
    page = from_print_page_text(job).strip()
    if not page:
        return _print_hints(_sheet_text(turns, job))
    hints = _print_hints(_hint_blob(page))
    if _suppress_starter_kit(page):
        hints.discard("3wire")
    return hints


def _score_text(turns: list[dict[str, Any]] | None, job: Path) -> str:
    page = from_print_page_text(job).strip()
    if page:
        return page
    return _sheet_text(turns, job)


def _project_name(turns: list[dict[str, Any]] | None, job: Path) -> str:
    name = ""
    if job.is_file():
        name = Session.open(job).project.name or ""
    for turn in turns or []:
        reply = turn.get("reply")
        if reply is not None and not reply.get("ok"):
            continue
        msg = turn.get("msg") or {}
        op = turn.get("op")
        if op == "create_project" and msg.get("name"):
            name = str(msg["name"])
        elif op == "set_title_block" and msg.get("name"):
            name = str(msg["name"])
    return name


def _three_phase_page(text: str) -> bool:
    flat = (text or "").upper()
    if re.search(r"\b(?:3\s*PH|THREE[-\s]?PHASE|3-PHASE)\b", flat):
        return True
    return bool(re.search(r"\b(?:L3|T3|W1)\b", flat))


def _power_class_placed(libs: set[str], needles: tuple[str, ...], letter: str) -> bool:
    for lib in libs:
        low = lib.lower()
        if letter == "K" and "relay" in low and "contactor" not in low:
            continue
        if letter == "K" and "aux" in low:
            continue
        if any(needle in low for needle in needles):
            return True
    return False


def _ref_for_class(text: str, letter: str) -> str:
    for ref, kind in _device_designators(text):
        if kind == letter:
            return ref
    return f"{letter}1"


def _next_power_place(text: str, libs: set[str]) -> tuple[str, str] | None:
    present = {kind for _ref, kind in _device_designators(text)}
    for letter, iec_id, needles in _POWER_PLACE:
        if letter not in present:
            continue
        if _power_class_placed(libs, needles, letter):
            continue
        lib_id = iec_id
        if letter == "M" and not _three_phase_page(text):
            lib_id = "iec.loads.motor_1ph"
        if (
            letter == "F"
            and re.search(r"\bFUSE\b", text, flags=re.I)
            and not re.search(r"\b(?:OVERLOAD|OL)\b", text, flags=re.I)
        ):
            lib_id = "iec.protection.fuse_10a"
        return _lib(lib_id), _ref_for_class(text, letter)
    return None


def _is_pushbutton_lib(lib: str) -> bool:
    low = lib.lower()
    return "pb_no" in low or "pb_nc" in low


def _is_control_relay_lib(lib: str) -> bool:
    low = lib.lower()
    if "contactor" in low:
        return False
    return ".relays." in low or low.startswith("relays.")


def _power_contactor(placed: dict[str, str]) -> str | None:
    for ref, lib in placed.items():
        low = lib.lower()
        if "contactor" in low and "aux" not in low:
            return ref
    return None


def _power_wire_example(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """Next source-to-load power pole. Catalog pins, refs already on the sheet."""

    placed = _all_placed(turns, job)
    ends = _wire_ends(turns, job)
    breaker = _first_of(placed, "breaker", "mcb", "disconnect")
    contactor = _power_contactor(placed)
    overload = _first_of(placed, "overload", "ol_contact")
    motor = _first_of(placed, "motor")
    if breaker and contactor and not _refs_connected(ends, breaker, contactor):
        return (
            f'{{"op":"wire","from":"{breaker}.2","to":"{contactor}.L1",'
            f'"net":"L1","sheet":"main"}}'
        )
    if contactor and overload and not _refs_connected(ends, contactor, overload):
        return (
            f'{{"op":"wire","from":"{contactor}.T1","to":"{overload}.1",'
            f'"net":"T1","sheet":"main"}}'
        )
    if overload and motor and not _refs_connected(ends, overload, motor):
        return (
            f'{{"op":"wire","from":"{overload}.2","to":"{motor}.U1",'
            f'"net":"U1","sheet":"main"}}'
        )
    if contactor and motor and not _refs_connected(ends, contactor, motor):
        return (
            f'{{"op":"wire","from":"{contactor}.T1","to":"{motor}.U1",'
            f'"net":"T1","sheet":"main"}}'
        )
    return None


# Phase sets named on a power print. Required only when that whole set is in the text.
_PHASE_GROUPS = (
    ("L1", "L2", "L3"),
    ("T1", "T2", "T3"),
    ("U1", "V1", "W1"),
)


def _text_has_token(text: str, token: str) -> bool:
    return bool(re.search(rf"\b{re.escape(token)}\b", text or "", flags=re.I))


def _printed_phase_groups(text: str) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for group in _PHASE_GROUPS:
        if all(_text_has_token(text, name) for name in group):
            found.append(group)
    return found


def _phase_tokens_present(turns: list[dict[str, Any]] | None, job: Path) -> set[str]:
    """Net labels and pin ids already used on a wire. Not device refs."""

    present: set[str] = set()

    def _take(net: str, frm: str, to: str) -> None:
        if net:
            present.add(net.upper())
        for raw in (frm, to):
            if "." in str(raw):
                present.add(str(raw).split(".", 1)[1].upper())

    for turn in turns or []:
        if turn.get("op") != "wire":
            continue
        reply = turn.get("reply")
        if reply is not None and not reply.get("ok"):
            continue
        result = (reply or {}).get("result") or {}
        msg = turn.get("msg") or {}
        _take(
            str(result.get("net") or msg.get("net") or ""),
            str(result.get("from") or msg.get("from") or ""),
            str(result.get("to") or msg.get("to") or ""),
        )
    if job.is_file():
        session = Session.open(job)
        for sheet in session.project.sheets:
            for wire in sheet.wires:
                _take(str(wire.net or ""), wire.frm, wire.to)
    return present


def _missing_phase_nets(text: str, turns: list[dict[str, Any]] | None, job: Path) -> list[str]:
    present = _phase_tokens_present(turns, job)
    missing: list[str] = []
    for group in _printed_phase_groups(text):
        for name in group:
            if name not in present:
                missing.append(name)
    return missing


def _catalog_pin_map(lib: str) -> dict[str, str]:
    return {pin.upper(): pin for pin in _pins_for_library(lib)}


def _phase_pin(lib: str, role: str, index: int) -> str | None:
    """Catalog pin for phase index 0..2. Absent on a 1-pole symbol."""

    pins = _catalog_pin_map(lib)
    table = {
        "breaker-load": ("2", "4", "6"),
        "breaker-line": ("1", "3", "5"),
        "contactor-line": ("L1", "L2", "L3"),
        "contactor-load": ("T1", "T2", "T3"),
        "overload-line": ("1", "3", "5"),
        "overload-load": ("2", "4", "6"),
        "motor": ("U1", "V1", "W1"),
    }
    want = table.get(role, ())
    if index >= len(want) or want[index] not in pins:
        return None
    if role.startswith("breaker") and index and "4" not in pins:
        return None
    return pins[want[index]]


def _three_pole_sets(lib: str) -> list[frozenset[str]] | None:
    """Power poles of a 3-pole breaker, contactor, or overload. None if it is not one."""

    low = lib.lower()
    pins = set(_catalog_pin_map(lib))
    if any(tag in low for tag in ("mcb_3p", "mccb_3p", "disconnect_3p")) and {
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
    } <= pins:
        return [frozenset({"1", "2"}), frozenset({"3", "4"}), frozenset({"5", "6"})]
    if (
        "contactor" in low
        and "aux" not in low
        and "coil" not in low
        and {"L1", "L2", "L3", "T1", "T2", "T3"} <= pins
    ):
        return [
            frozenset({"L1", "T1"}),
            frozenset({"L2", "T2"}),
            frozenset({"L3", "T3"}),
        ]
    if "overload" in low and "ol_contact" not in low and {"1", "2", "3", "4", "5", "6"} <= pins:
        return [frozenset({"1", "2"}), frozenset({"3", "4"}), frozenset({"5", "6"})]
    return None


def _single_landed_pole(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """A 3-pole device with exactly one power pole landed. One-pole symbols are ignored."""

    placed = _all_placed(turns, job)
    ends = _wire_ends(turns, job)
    for ref, lib in placed.items():
        poles = _three_pole_sets(lib)
        if not poles:
            continue
        used = {pin.upper() for pin in _ref_pins_used(ends, ref)}
        landed = sum(1 for pole in poles if used & pole)
        if landed == 1:
            return ref
    return None


def _phase_wire_gap(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """Missing printed phase net, or a 3-pole device still on a single pole."""

    page = from_print_page_text(job).strip()
    if not page or not _printed_phase_groups(page):
        return None
    missing = _missing_phase_nets(page, turns, job)
    if missing:
        return f"printed net {missing[0]} is not a wire"
    lone = _single_landed_pole(turns, job)
    if lone:
        return f"{lone} still has only one pole landed"
    return None


def _overload_ref(placed: dict[str, str]) -> str | None:
    for ref, lib in placed.items():
        low = lib.lower()
        if "overload" in low and "ol_contact" not in low:
            return ref
    return None


def _phase_wire_json(net: str, turns: list[dict[str, Any]] | None, job: Path) -> str:
    placed = _all_placed(turns, job)
    index = 0
    for group in _PHASE_GROUPS:
        if net in group:
            index = group.index(net)
            break
    breaker = _first_of(placed, "breaker", "mcb", "disconnect")
    contactor = _power_contactor(placed)
    overload = _overload_ref(placed)
    motor = _first_of(placed, "motor")

    def _end(ref: str | None, role: str) -> str | None:
        if not ref:
            return None
        pin = _phase_pin(placed.get(ref, ""), role, index)
        if not pin:
            return None
        return f"{ref}.{pin}"

    if net.startswith("L"):
        pair = (_end(breaker, "breaker-load"), _end(contactor, "contactor-line"))
    elif net.startswith("T"):
        pair = (_end(contactor, "contactor-load"), _end(overload, "overload-line"))
    else:
        pair = (_end(overload, "overload-load"), _end(motor, "motor"))
    if pair[0] and pair[1]:
        body: dict[str, str] = {
            "op": "wire",
            "from": pair[0],
            "to": pair[1],
            "net": net,
            "sheet": "main",
        }
    else:
        body = {"op": "wire", "net": net, "sheet": "main"}
    return json.dumps(body, separators=(",", ":"))


def _phase_wire_nudge(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    gap = _phase_wire_gap(turns, job)
    if not gap:
        return None
    page = from_print_page_text(job).strip()
    missing = _missing_phase_nets(page, turns, job)
    net = missing[0] if missing else ""
    if not net:
        placed = _all_placed(turns, job)
        lone = _single_landed_pole(turns, job)
        lib = placed.get(lone or "", "")
        poles = _three_pole_sets(lib) or []
        used = {pin.upper() for pin in _ref_pins_used(_wire_ends(turns, job), lone or "")}
        groups = _printed_phase_groups(page)
        group = groups[0] if groups else ()
        for index, pole in enumerate(poles):
            if not (used & pole) and index < len(group):
                net = group[index]
                break
    if not net:
        return f"{gap}. Land the other poles before cleanup."
    return (
        f"{gap}. Wire that printed pole next. Do not repeat a net already on the sheet. "
        f"Example: {_phase_wire_json(net, turns, job)}"
    )


def _short_page_text(text: str, limit: int = 900) -> str:
    flat = re.sub(r"[ \t]+", " ", text or "").strip()
    flat = re.sub(r"\n{2,}", "\n", flat)
    if len(flat) <= limit:
        return flat
    head = flat[:500].rstrip()
    tail = flat[-300:].lstrip()
    return head + "\n…\n" + tail


def _search_query(hints: set[str]) -> str:
    """First library_search query from the heading — not a motor unless the print says motor."""

    page = from_print_page_text().strip()
    if page and _power_device_page(page):
        nxt = _next_power_place(page, set())
        if nxt:
            lib_id = nxt[0].lower()
            if "breaker" in lib_id or "disconnect" in lib_id:
                return "breaker"
            if "contactor" in lib_id:
                return "contactor"
            if "motor" in lib_id:
                return "motor"
            if "fuse" in lib_id:
                return "fuse"
            return "overload"
    if "plc" in hints:
        return "plc"
    if "vfd" in hints:
        return "vfd"
    if "estop" in hints:
        return "estop"
    if "3wire" in hints:
        return "coil"
    if "transformer" in hints:
        return "transformer"
    if "motor" in hints:
        return "motor"
    return "coil"


def _crowded_pin(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """A pin that too many wires share — a star dump, not a series path."""

    counts: dict[str, int] = {}
    for a, ap, b, bp in _wire_ends(turns, job):
        for ref, pin in ((a, ap), (b, bp)):
            if not ref or not pin:
                continue
            key = f"{ref}.{pin}"
            counts[key] = counts.get(key, 0) + 1
    for key, n in counts.items():
        if n >= 3:
            return key
    return None


def _place_example(
    hints: set[str],
    turns: list[dict[str, Any]] | None = None,
    job: Path | None = None,
) -> tuple[str, str]:
    libs = _placed_libs(turns, job) if turns is not None and job is not None else set()
    page = from_print_page_text(job).strip()
    if page and _power_device_page(page):
        nxt = _next_power_place(page, libs)
        if nxt:
            return nxt
    if "estop" in hints:
        return _lib("iec.operators.estop"), "ESTOP"
    if "plc" in hints:
        return _lib("iec.plc.compact_16dio"), "PLC"
    if "vfd" in hints:
        return _lib("iec.drives.vfd_1k5"), "VFD"
    if "3wire" in hints:
        return _lib("iec.contactors.ac3_9a"), "K1"
    if "motor" in hints:
        return _lib("iec.loads.motor_3ph"), "M1"
    if "transformer" in hints:
        if not any("transformer" in lib for lib in libs):
            return _lib("iec.power.control_transformer"), "T1"
        if not any("fuse" in lib for lib in libs):
            return _lib("iec.protection.fuse_10a"), "FU1"
        return _lib("iec.power.control_transformer"), "T1"
    if "relay" in hints:
        have_coil = any("coil" in lib or "relays." in lib for lib in libs)
        have_pb = any("pb_" in lib for lib in libs)
        have_aux = any("aux" in lib for lib in libs)
        have_lamp = any("pilot" in lib or "light" in lib for lib in libs)
        if not have_coil:
            return _lib("iec.contactors.coil"), "CR1"
        if not have_pb:
            return _lib("iec.operators.pb_no"), "PB1"
        if not have_aux:
            return _lib("iec.contactors.aux_no"), "CR1-3-4"
        if not have_lamp:
            return _lib("iec.operators.pilot_light"), "PL1"
        return _lib("iec.operators.pilot_light"), "PL1"
    return _lib("iec.contactors.coil"), "CR1"


def _is_starter_kit(turns: list[dict[str, Any]] | None, job: Path) -> bool:
    """True when the sheet is only a START/STOP/fuse/coil dump."""

    libs = _placed_libs(turns, job)
    families = {_family_key(lib) for lib in libs}
    extra = families - {"operators", "protection", "contactors"}
    return bool(libs) and not extra


def _first_of(placed: dict[str, str], *needles: str) -> str | None:
    for ref, lib in placed.items():
        blob = f"{ref} {lib}".lower()
        if any(n.lower() in blob for n in needles):
            return ref
    return None


def _ex_pin(turns: list[dict[str, Any]], job: Path, ref: str, *prefer: str) -> str:
    lib = _lib_for_ref(turns, ref) or _all_placed(turns, job).get(ref) or ""
    pins = _pins_for_library(lib)
    want = prefer[0] if prefer else None
    if pins:
        return f"{ref}.{_pick_pin(want, pins)}"
    return f"{ref}.{want or '1'}"


def _three_wire_gap(turns: list[dict[str, Any]], job: Path) -> str | None:
    """Next missing series/hold-in wire, or None if the 3-wire rung is complete."""

    libs = _placed_libs(turns, job)
    if not (
        _lib_match(libs, "contactors")
        and _lib_match(libs, "operators")
        and _lib_match(libs, "overload")
    ):
        return None
    placed = _all_placed(turns, job)
    ends = _wire_ends(turns, job)
    stop = _first_of(placed, "pb_nc")
    start = _first_of(placed, "pb_no")
    fuse = _first_of(placed, "fuse")
    coil = _first_of(placed, "contactors")
    ol = _first_of(placed, "overload", "ol_contact")
    pe = _first_of(placed, "terminals.pe", ".pe_")
    hint = "If the print is a 3-wire starter, "
    if fuse and stop and not _refs_connected(ends, fuse, stop):
        return (
            f"{hint}wire fuse then STOP. "
            f'Example: {{"op":"wire","from":"{_ex_pin(turns, job, fuse, "2")}","to":"{_ex_pin(turns, job, stop, "11", "1")}","net":"L1","sheet":"main"}}'
        )
    if stop and start and not _refs_connected(ends, stop, start):
        return (
            f"{hint}wire STOP then START. "
            f'Example: {{"op":"wire","from":"{_ex_pin(turns, job, stop, "12", "2")}","to":"{_ex_pin(turns, job, start, "13", "3")}","net":"L1","sheet":"main"}}'
        )
    if start and coil and not _refs_connected(ends, start, coil):
        return (
            f"{hint}wire START to coil A1. "
            f'Example: {{"op":"wire","from":"{_ex_pin(turns, job, start, "14", "4")}","to":"{coil}.A1","net":"L1","sheet":"main"}}'
        )
    if coil and ol and not _refs_connected(ends, coil, ol):
        return (
            f"{hint}wire coil A2 to OL (not A1–A2). "
            f'Example: {{"op":"wire","from":"{coil}.A2","to":"{ol}.95","net":"L1","sheet":"main"}}'
        )
    if coil and start:
        aux = _first_of(placed, "aux_no")
        if not aux:
            return (
                f"{hint}hold-in is a separate aux NO around START. "
                f'Example: {{"op":"place","library_id":"{_lib("iec.contactors.aux_no")}","ref":"M3","x":80,"y":160,"sheet":"main"}}'
            )
        if not _refs_connected(ends, aux, start):
            aux_lib = placed.get(aux) or ""
            pins = _pins_for_library(aux_lib)
            ap = pins[0] if pins else "3"
            start_lib = placed.get(start) or ""
            start_pins = _pins_for_library(start_lib)
            sp = next((p for p in start_pins if p.upper() in {"3", "13"}), None)
            if sp is None:
                sp = start_pins[0] if start_pins else "3"
            return (
                f"{hint}wire the hold-in aux in parallel with START. "
                f'Example: {{"op":"wire","from":"{aux}.{ap}","to":"{start}.{sp}","net":"L1","sheet":"main"}}'
            )
    if ol and pe and not _refs_connected(ends, ol, pe):
        return (
            f"{hint}wire OL to PE/GND. "
            f'Example: {{"op":"wire","from":"{ol}.96","to":"{pe}.A","net":"0V","sheet":"main"}}'
        )
    return None

FROM_PRINT_OPS = tuple(
    op
    for op in (
        "create_project",
        "set_title_block",
        "library_search",
        "place",
        "wire",
        "add_sheet",
        "set_active_sheet",
        "set_sheet_title",
        "cleanup",
        "validate",
        "save",
    )
    if op in OPS
)

MIN_DEVICES = 3
MIN_WIRES = 2
MIN_FAMILIES = 2
MIN_WIRES_3WIRE = 6


def _phase_op_done(turns: list[dict[str, Any]] | None, op: str) -> bool:
    """True when the op landed (ok reply) or a unit-test turn listed it with no reply."""

    for turn in turns or []:
        if turn.get("op") != op:
            continue
        reply = turn.get("reply")
        if reply is None or reply.get("ok"):
            return True
    return False


def _title_name_matches(turns: list[dict[str, Any]] | None, job: Path) -> bool:
    """Blank scans stay image-only. A text layer must share a heading word."""

    page = from_print_page_text(job).strip()
    if not page:
        return True
    words = _printed_fields(page).get("heading_words") or set()
    if not words:
        return True
    return bool(_real_words(_project_name(turns, job)) & set(words))


def _title_phase_done(turns: list[dict[str, Any]] | None, job: Path) -> bool:
    created = _phase_op_done(turns, "create_project") or _phase_op_done(turns, "open")
    if not (created and _phase_op_done(turns, "save") and _phase_op_done(turns, "set_title_block")):
        return False
    return _title_name_matches(turns, job)


def _heading_place_gap(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """Missing heading-required devices. Stay in PLACE so CLOSE cannot deadlock."""

    hints = _active_hints(turns, job)
    libs = _placed_libs(turns, job)
    page = from_print_page_text(job).strip()
    if page and _power_device_page(page):
        missing = _next_power_place(page, libs)
        if missing:
            return f"printed power device {missing[1]} is not placed"
    if "3wire" in hints and not any("pb_no" in lib for lib in libs):
        return "3-wire heading but no START placed"
    if "3wire" in hints and not any("pb_nc" in lib for lib in libs):
        return "3-wire heading but no STOP placed"
    if "3wire" in hints and not _lib_match(libs, "contactors"):
        return "3-wire heading but no coil/contactor placed"
    if "plc" in hints and not _lib_match(libs, "plc"):
        return "print heading calls for a PLC"
    if "vfd" in hints and not _lib_match(libs, "drive"):
        return "print heading calls for a VFD/drive"
    if "estop" in hints and not any("estop" in lib for lib in libs):
        return "print heading is E-stop/emergency"
    if hints & {"plc", "vfd", "estop"} and _is_starter_kit(turns, job):
        return "nonsense starter kit does not match this print"
    if "relay" in hints and not any("pilot" in lib or "light" in lib for lib in libs):
        return "relay heading but no lamp/pilot placed"
    if (
        "relay" in hints
        and _lib_match(libs, "contactors")
        and not any("aux" in lib for lib in libs)
    ):
        return "relay heading but no aux contact placed"
    if not _suppress_starter_kit(from_print_page_text(job)):
        gap = _three_wire_gap(turns or [], job)
        if gap and '"op":"place"' in gap.replace(" ", ""):
            return gap
    return None


def _place_phase_done(turns: list[dict[str, Any]] | None, job: Path) -> bool:
    if not _phase_op_done(turns, "library_search"):
        return False
    disk_refs, _disk_wires = _placed_on_disk(job)
    refs = {r for r in (disk_refs | _refs_from_turns(turns or [])) if not _is_net_ref(r)}
    families = _families_from_turns(turns or []) | _families_on_disk(job)
    if len(refs) < MIN_DEVICES or len(families) < MIN_FAMILIES:
        return False
    if _heading_place_gap(turns, job):
        return False
    n_pages = from_print_page_count()
    if n_pages > 1:
        if _sheet_count(turns, job) < n_pages:
            return False
        if _empty_sheets(turns, job):
            return False
    return True


def _wire_related_gap(turns: list[dict[str, Any]] | None, job: Path) -> str | None:
    """Circuit-law / wire gaps the existing check would fail on."""

    disk_refs, disk_wires = _placed_on_disk(job)
    wires = max(disk_wires, _wires_from_turns(turns or []))
    if wires < MIN_WIRES:
        return f"only {wires} wires from the print"
    crowded = _crowded_pin(turns, job)
    if crowded:
        return f"star wiring on {crowded}; use a series path"
    if _fuse_jumped_to_coil(turns, job):
        return "fuse jumped to coil; wire the series path on the print"
    wired = _wired_refs(turns, job)
    coils = _coil_refs(turns, job)
    if coils - wired:
        return "coil placed but not wired"
    motors = _refs_of_kind(turns, job, "motor")
    if from_print_style() == "ladder":
        rail_hit = _motor_on_ladder_rails(turns, job)
        if rail_hit:
            return rail_hit
    elif motors and not (motors & wired):
        return "motor placed but not wired"
    lamps = _refs_of_kind(turns, job, "pilot")
    if lamps and not (lamps & wired):
        return "lamp/pilot placed but not wired"
    pe_refs = _refs_of_kind(turns, job, "terminals.pe") | _refs_of_kind(turns, job, ".pe_")
    if pe_refs and not (pe_refs & wired):
        return "PE/GND placed but not wired"
    for kind, label in (("plc", "PLC"), ("drive", "VFD"), ("hmi", "HMI")):
        refs_k = _refs_of_kind(turns, job, kind)
        if refs_k and not (refs_k & wired):
            return f"{label} placed but not wired"
    libs = _placed_libs(turns, job)
    if any("estop" in lib for lib in libs):
        estop_refs = {
            ref
            for ref, lib in _all_placed(turns, job).items()
            if "estop" in lib.lower()
        }
        if estop_refs and not (estop_refs & wired):
            return "E-STOP placed but not wired"
    law = _circuit_laws_gap(turns, job)
    if law:
        return law
    hints = _active_hints(turns, job)
    if not (hints & {"plc", "vfd", "estop"}) and not _suppress_starter_kit(
        from_print_page_text(job)
    ):
        gap = _three_wire_gap(turns or [], job)
        if gap and '"op":"place"' not in gap.replace(" ", ""):
            return gap
    phase_gap = _phase_wire_gap(turns, job)
    if phase_gap:
        return phase_gap
    return None


def _wire_phase_done(turns: list[dict[str, Any]] | None, job: Path) -> bool:
    if not _place_phase_done(turns, job):
        return False
    return _wire_related_gap(turns, job) is None


def current_phase(turns: list[dict[str, Any]] | None, job: Path) -> str:
    """TITLE → PLACE → WIRE → CLOSE from Session.open / turns, not model English."""

    if not _title_phase_done(turns, job):
        return "TITLE"
    if not _place_phase_done(turns, job):
        return "PLACE"
    if not _wire_phase_done(turns, job):
        return "WIRE"
    return "CLOSE"


def from_print_phase_allowed(
    turns: list[dict[str, Any]] | None, job: Path
) -> tuple[str, ...]:
    phase = current_phase(turns, job)
    return tuple(op for op in PHASE_OPS[phase] if op in FROM_PRINT_OPS)


def from_print_window_user(turns: list[dict[str, Any]] | None, job: Path) -> str:
    phase = current_phase(turns, job)
    card = phase_instruction(phase)
    page = from_print_page_text(job).strip()
    printed = f"PRINTED PAGE:\n{_short_page_text(page)}\n" if page else ""
    return (
        f"Task from-print. PHASE {phase}. Job: {job}\n"
        f"{printed}"
        f"{card.rstrip()}\n"
        "One JSON object per turn. Work only on that file."
    )


def render_pdf_page(pdf: Path, page: int, dest: Path, *, dpi: int = 110) -> Path:
    """Rasterize one 1-indexed PDF page with QtPdf (already a PanelCAD dep)."""

    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    doc = QPdfDocument()
    doc.load(str(pdf))
    n = doc.pageCount()
    if n < 1:
        raise FileNotFoundError(f"PDF has no pages: {pdf}")
    index = max(0, min(page - 1, n - 1))
    pt = doc.pagePointSize(index)
    width = max(int(pt.width() * dpi / 72), 800)
    height = max(int(pt.height() * dpi / 72), 600)
    rendered = doc.render(index, QSize(width, height))
    image = QImage(rendered.size(), QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    overlay = QPainter(image)
    overlay.drawImage(0, 0, rendered)
    overlay.end()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(dest), "PNG"):
        raise RuntimeError(f"failed to write {dest}")
    return dest


def pdf_page_count(pdf: Path) -> int:
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    doc = QPdfDocument()
    doc.load(str(pdf))
    return int(doc.pageCount())


def read_pdf_pages_text(pdf: Path, pages: list[int]) -> dict[int, str]:
    """Text layer of each 1-indexed page. Empty string when the page is a scan."""

    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    doc = QPdfDocument()
    doc.load(str(pdf))
    count = int(doc.pageCount())
    out: dict[int, str] = {}
    for page in pages:
        if count < 1 or page < 1 or page > count:
            out[page] = ""
            continue
        try:
            sel = doc.getAllText(page - 1)
        except Exception:  # noqa: BLE001
            out[page] = ""
            continue
        if sel is None or not sel.isValid():
            out[page] = ""
            continue
        out[page] = str(sel.text() or "")
    return out


def read_pdf_page_text(pdf: Path, page: int) -> str:
    return read_pdf_pages_text(pdf, [page]).get(page, "")


def parse_pages(spec: str | None, page: int, page_count: int) -> list[int]:
    """1, 1-3, 1,2,3, or all. Clamped to the PDF."""

    if page_count < 1:
        return [1]
    if spec is None or str(spec).strip() == "":
        return [max(1, min(page, page_count))]
    token = str(spec).strip().lower()
    if token in {"all", "*"}:
        return list(range(1, page_count + 1))
    out: list[int] = []
    for part in token.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        else:
            out.append(int(part))
    seen: set[int] = set()
    pages: list[int] = []
    for n in out:
        if 1 <= n <= page_count and n not in seen:
            seen.add(n)
            pages.append(n)
    return pages or [1]


def png_to_b64(path: Path, *, max_width: int = 900) -> str:
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])
    image = QImage(str(path))
    if image.isNull():
        raise FileNotFoundError(f"not an image: {path}")
    if image.width() > max_width:
        image = image.scaledToWidth(max_width)
    tmp = path.with_name(path.stem + "-feed.jpg")
    image.save(str(tmp), "JPG", 72)
    return base64.b64encode(tmp.read_bytes()).decode("ascii")


def _seen_ops(turns: list[dict[str, Any]]) -> list[str]:
    return [str(t.get("op")) for t in turns if t.get("op")]


def _refs_from_turns(turns: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for turn in turns:
        if turn.get("op") != "place":
            continue
        reply = turn.get("reply") or {}
        if reply.get("ok"):
            rid = (reply.get("result") or {}).get("id")
            if rid:
                refs.add(str(rid))
        if "already exists" in str(reply.get("error") or ""):
            ref = (turn.get("msg") or {}).get("ref")
            if ref:
                refs.add(str(ref))
    return refs


def _wires_from_turns(turns: list[dict[str, Any]]) -> int:
    return sum(
        1
        for turn in turns
        if turn.get("op") == "wire" and (turn.get("reply") or {}).get("ok")
    )


def _placed_on_disk(job: Path) -> tuple[set[str], int]:
    if not job.is_file():
        return set(), 0
    session = Session.open(job)
    refs = {c.id for sheet in session.project.sheets for c in sheet.components}
    wires = sum(len(sheet.wires) for sheet in session.project.sheets)
    return refs, wires


def _components_by_sheet(
    turns: list[dict[str, Any]] | None, job: Path
) -> dict[str, set[str]]:
    """Refs per sheet from the live trace, with disk as a floor after save."""

    by: dict[str, set[str]] = {}
    if job.is_file():
        session = Session.open(job)
        for sh in session.project.sheets:
            by[sh.id] = {
                c.id for c in sh.components if c.library_id and not _is_net_ref(c.id)
            }
    active = "main"
    for turn in turns or []:
        op = str(turn.get("op") or "")
        msg = turn.get("msg") or {}
        reply = turn.get("reply") or {}
        ok = bool(reply.get("ok"))
        result = reply.get("result") or {}
        if op == "create_project" and ok:
            by.setdefault("main", set())
            active = "main"
        elif op == "add_sheet" and ok:
            sid = str(result.get("id") or msg.get("sheet_id") or "")
            if sid:
                by.setdefault(sid, set())
                active = sid
        elif op == "set_active_sheet" and ok:
            active = str(result.get("id") or msg.get("sheet_id") or msg.get("sheet") or active)
        elif op == "place" and ok:
            sid = str(msg.get("sheet") or active)
            ref = str(result.get("id") or msg.get("ref") or "")
            if ref:
                by.setdefault(sid, set()).add(ref)
    return by


def _empty_sheets(turns: list[dict[str, Any]] | None, job: Path) -> list[str]:
    return [
        sid
        for sid, refs in _components_by_sheet(turns, job).items()
        if len(refs) < 2
    ]


def _sheet_count(turns: list[dict[str, Any]] | None, job: Path) -> int:
    n = 0
    if job.is_file():
        n = len(Session.open(job).project.sheets)
    added = sum(
        1
        for turn in turns or []
        if turn.get("op") == "add_sheet" and (turn.get("reply") or {}).get("ok")
    )
    created = any(
        turn.get("op") == "create_project" and (turn.get("reply") or {}).get("ok")
        for turn in turns or []
    )
    return max(n, (1 if created else 0) + added)


def _title_filled(job: Path) -> bool:
    if not job.is_file():
        return False
    tb = Session.open(job).get_title_block()
    for key in ("company", "customer", "drawing_number", "location"):
        if str(tb.get(key) or "").strip():
            return True
    revs = tb.get("revisions") or []
    if revs and str((revs[0] or {}).get("rev") or "").strip():
        return True
    return bool(str(Session.open(job).project.name or "").strip())


def _placed_map(turns: list[dict[str, Any]] | None) -> dict[str, tuple[str, str]]:
    """Successful place ops in the turn trace: REF.upper() -> (ref, library_id)."""

    placed: dict[str, tuple[str, str]] = {}
    for turn in turns or []:
        if turn.get("op") != "place":
            continue
        reply = turn.get("reply") or {}
        msg = turn.get("msg") or {}
        ok = bool(reply.get("ok")) or "already exists" in str(reply.get("error") or "")
        if not ok:
            continue
        rid = str((reply.get("result") or {}).get("id") or msg.get("ref") or "").strip()
        lib = str(
            (reply.get("result") or {}).get("library_id") or msg.get("library_id") or ""
        ).strip()
        if rid and lib:
            placed[rid.upper()] = (rid, lib)
    return placed


def _lib_for_ref(turns: list[dict[str, Any]], ref: str) -> str | None:
    hit = _placed_map(turns).get(str(ref or "").upper())
    return hit[1] if hit else None


def _pins_for_library(library_id: str) -> list[str]:
    if not library_id:
        return []
    cached = _PIN_CACHE.get(library_id)
    if cached is not None:
        return list(cached)
    try:
        info = Session.create("tmp").inspect_device(library_id)
    except Exception:  # noqa: BLE001
        _PIN_CACHE[library_id] = []
        return []
    pins = info.get("pins") or []
    ids = [str(p["id"]) for p in pins if p.get("id")]
    _PIN_CACHE[library_id] = ids
    return list(ids)


_CONTACT_ALIAS = {"11": "1", "12": "2", "13": "3", "14": "4"}


def _pick_pin(pin: str | None, pins: list[str], used: set[str] | None = None) -> str:
    if pin:
        for catalog_pin in pins:
            if catalog_pin.lower() == pin.lower():
                return catalog_pin
        alias = _CONTACT_ALIAS.get(str(pin))
        if alias:
            for catalog_pin in pins:
                if catalog_pin.lower() == alias.lower():
                    return catalog_pin
    taken = {p.upper() for p in (used or set())}
    for catalog_pin in pins:
        if catalog_pin.upper() not in taken:
            return catalog_pin
    return pins[0]


def _coil_aux_pins(lib: str) -> list[str]:
    return [p for p in _pins_for_library(lib) if p.upper() in {"3", "4", "13", "14"}]


def _operators_on_sheet(turns: list[dict[str, Any]] | None, job: Path) -> bool:
    libs = _placed_libs(turns, job)
    return any("pb_no" in lib for lib in libs) and any("pb_nc" in lib for lib in libs)


def _fuse_jumped_to_coil(turns: list[dict[str, Any]] | None, job: Path) -> bool:
    """True when a fuse lands on coil A1 while STOP/START are already on the sheet."""

    if not _operators_on_sheet(turns, job):
        return False
    placed = _all_placed(turns, job)
    for a, ap, b, bp in _wire_ends(turns, job):
        a_lib = (placed.get(a) or "").lower()
        b_lib = (placed.get(b) or "").lower()
        if "fuse" in a_lib and "contactors" in b_lib and bp.upper() == "A1":
            return True
        if "fuse" in b_lib and "contactors" in a_lib and ap.upper() == "A1":
            return True
    return False


def _prefer_control_pin(
    lib: str,
    pin: str | None,
    other_lib: str | None,
    *,
    used: set[str] | None = None,
) -> str | None:
    """On a control rung, land on coil/OL aux pins — not power poles."""

    if _suppress_starter_kit(from_print_page_text()):
        return pin
    if not other_lib:
        return pin
    kind = _kind_key(lib)
    other = _kind_key(other_lib)
    taken = {p.upper() for p in (used or set())}
    if "contactors" in kind and ("loads" in other or "motor" in other):
        have = {p.upper() for p in _pins_for_library(lib)}
        if "T1" in have and (pin is None or pin.upper() in {"A1", "A2", "13", "14"}):
            return "T1"
        return pin
    control_other = any(
        token in other for token in ("operators", "protection", "terminals", "contactors")
    )
    if not control_other:
        return pin
    if "contactors" in kind:
        if "overload" in other or "ol_contact" in other:
            return "A2"
        if pin is None or pin.upper() in {"L1", "L2", "L3", "T1", "T2", "T3"}:
            if "operators" in other and "A1" in taken:
                return "13"
            if "A1" in taken:
                return "A2"
            return "A1"
        return pin
    if "overload" in kind:
        if pin is None or pin.upper() in {"L1", "L2", "L3", "T1", "T2", "T3"} or pin in {
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
        }:
            return "96" if "95" in taken else "95"
        return pin
    return pin


def coerce_place(msg: dict[str, Any], job: Path, turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Map print labels to catalog families. Do not place net names as devices."""

    if msg.get("op") != "place":
        return msg
    msg = dict(msg)
    ref = str(msg.get("ref") or "").strip()
    if _is_net_ref(ref):
        msg["_harness_error"] = (
            f"'{ref}' is a net/line name on the print, not a device. "
            f"Place operators ({_lib('iec.operators.pb_no')} / pb_nc / estop), fuses, "
            "contactors, motors, OL, PE instead."
        )
        return msg
    role = _role_library(ref)
    incoming = str(msg.get("library_id") or "").lower()
    if role:
        if "aux" in incoming and "aux" not in role:
            pass
        else:
            msg["library_id"] = role
    elif msg.get("library_id"):
        msg["library_id"] = _lib(str(msg.get("library_id") or ""))
    lib = str(msg.get("library_id") or "").lower()
    token = _alnum_upper(ref)
    coil_ref = token in {"M", "K"} or (
        len(token) <= 3 and token[:1] in {"M", "K"} and token[1:].isdigit()
    )
    # A coil tagged M/K is not an MCB. Leave motors (3-lead circles) alone.
    if coil_ref and ("breaker" in lib or "mcb" in lib or "disconnect" in lib):
        msg["library_id"] = _lib("iec.contactors.ac3_9a")
        lib = str(msg.get("library_id") or "").lower()
    if from_print_style() == "ladder" and (
        lib.endswith("protection.overload") or lib.endswith("protection.overload_9a")
    ):
        msg["library_id"] = _lib("iec.protection.ol_contact")
        lib = str(msg.get("library_id") or "").lower()
    pe_ref = token in {"GND", "PE", "XPE", "EARTH", "GROUND"} or token.startswith("GND") or (
        token.startswith("PE") and (len(token) == 2 or token[2:].isdigit())
    )
    page = from_print_page_text(job).strip()
    if page and _power_device_page(page) and (
        _is_pushbutton_lib(lib) or _is_control_relay_lib(lib)
    ):
        nxt = _next_power_place(page, _placed_libs(turns, job))
        example = ""
        if nxt:
            example = (
                f' Example: {{"op":"place","library_id":"{nxt[0]}","ref":"{nxt[1]}",'
                f'"x":80,"y":80,"sheet":"main"}}'
            )
        msg["_harness_error"] = (
            "Printed refs on this page are power devices (IEC letters Q/K/F/M). "
            "Do not place a pushbutton or a control relay."
            + example
        )
        return msg
    text = _score_text(turns, job)
    hints = _active_hints(turns, job)
    off_kind = hints & {"relay", "transformer", "3wire"}
    if pe_ref or "terminals.pe" in lib:
        if off_kind and not _mentions_earth(text):
            msg["_harness_error"] = (
                "Do not invent a PE/GND device unless that symbol is drawn on the print. "
                "On a ladder, the load returns to L2."
            )
            return msg
    if "relay" in hints:
        for word in ("START", "STOP", "RESET"):
            if token == word or (token.startswith(word) and token[len(word) :].isdigit()):
                if not _mentions_word(text, word):
                    msg["_harness_error"] = (
                        f"Do not invent {word} unless that label is drawn on the print."
                    )
                    return msg
    if "motor" in lib and "motor" not in hints and (hints & {"relay", "transformer"}):
        msg["_harness_error"] = (
            "Do not invent a motor unless a motor is drawn on the print."
        )
        return msg
    if "motor" in lib and from_print_style() == "ladder":
        already = {
            ref
            for ref, lb in _all_placed(turns, job).items()
            if "motor" in (lb or "").lower()
        }
        if already and token not in already:
            msg["_harness_error"] = (
                "Do not invent a second motor. One power load is enough; "
                "it stays off the L1/L2 rails."
            )
            return msg
    if "estop" in lib and "estop" not in hints and off_kind:
        msg["_harness_error"] = (
            "Do not invent E-STOP unless that symbol is drawn on the print."
        )
        return msg
    if "fuse" in lib and "relay" in hints:
        msg["_harness_error"] = (
            "Do not invent a fuse unless a fuse is drawn on the print."
        )
        return msg
    if "notes.block" in lib:
        msg["_harness_error"] = (
            "A note block is text, not a device. Place the symbol drawn on the print."
        )
        return msg
    if "power.load" in lib:
        msg["_harness_error"] = (
            "A generic load rectangle is not a part. "
            "Place the symbol that is drawn (coil, light, button, stack light, PLC, HMI, drive)."
        )
        return msg
    return msg


def _mentions_earth(text: str) -> bool:
    return any(_mentions_word(text, k) for k in ("PE", "GND", "EARTH", "GROUND", "XPE"))


def _mentions_word(text: str, word: str) -> bool:
    blob = f" {(text or '').upper().replace('-', ' ')} "
    return f" {word.upper()} " in blob


def coerce_wire_pins(msg: dict[str, Any], job: Path, turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Normalize from/to to Ref.Pin using catalog pins of placed refs in the trace."""

    if msg.get("op") != "wire":
        return msg
    msg = dict(msg)
    placed = _placed_map(turns)
    parsed: dict[str, tuple[str, str | None, str]] = {}
    for key in ("from", "to"):
        raw = str(msg.get(key) or "").strip()
        if not raw:
            continue
        if "." in raw:
            ref, pin = raw.split(".", 1)
        else:
            ref, pin = raw, None
        ref = ref.strip()
        pin = pin.strip() if pin else None
        hit = placed.get(ref.upper())
        if hit is None:
            if from_print_style() == "ladder" and pin is None:
                parsed[key] = (ref, None, "__rail__")
                continue
            if _is_net_ref(ref):
                msg["_harness_error"] = (
                    f"'{raw}' is a net/line name, not a device pin. "
                    "Wire Ref.Pin to Ref.Pin of devices you placed."
                )
                return msg
            continue
        canon, lib = hit
        parsed[key] = (canon, pin, lib)
    for key, (canon, pin, lib) in parsed.items():
        if not pin or lib == "__rail__":
            continue
        pins = _pins_for_library(lib)
        have = {p.upper() for p in pins}
        if (
            pin.upper() in {"3", "4", "13", "14"}
            and pin.upper() not in have
            and "contactors" in lib.lower()
            and "aux" not in lib.lower()
        ):
            msg["_harness_error"] = (
                f"{canon} has pins {', '.join(pins) or 'A1/A2'} — not {pin}. "
                "Hold-in is a separate aux NO around START. "
                f'Place {_lib("iec.contactors.aux_no")} then wire it in parallel with START.'
            )
            return msg
    other_lib = {
        "from": parsed.get("to", ("", None, ""))[2] or None,
        "to": parsed.get("from", ("", None, ""))[2] or None,
    }
    ends = _wire_ends(turns, job)
    for key, (canon, pin, lib) in parsed.items():
        if lib == "__rail__":
            msg[key] = canon
            continue
        pins = _pins_for_library(lib)
        if not pins:
            continue
        used = _ref_pins_used(ends, canon)
        pin = _prefer_control_pin(
            lib, pin, other_lib[key], used=used
        )
        msg[key] = f"{canon}.{_pick_pin(pin, pins, used)}"
    left, right = str(msg.get("from") or ""), str(msg.get("to") or "")
    if left and right and left.upper() == right.upper():
        msg["_harness_error"] = f"cannot wire a pin to itself ({left})"
        return msg
    if left and right:
        for endpoint in (left, right):
            n = 0
            token = endpoint.upper()
            for x, xp, y, yp in ends:
                if f"{x}.{xp}" == token or f"{y}.{yp}" == token:
                    n += 1
            if n >= 2:
                msg["_harness_error"] = (
                    f"Too many wires on {endpoint}. That is a star, not a series path. "
                    "Do not add another wire to that pin. Connect two other devices."
                )
                return msg
    if left and right:
        new_a = left.upper()
        new_b = right.upper()
        for x, xp, y, yp in ends:
            old_a, old_b = f"{x}.{xp}", f"{y}.{yp}"
            if {old_a, old_b} == {new_a, new_b}:
                msg["_harness_error"] = (
                    f"{left} to {right} is already on the sheet (or reversed). "
                    "Wire the next net drawn on the print."
                )
                return msg
    if from_print_style() == "ladder" and left and right:
        from panelcad.core.circuit import ladder_motor_on_rails

        def _lib_of(raw: str) -> str:
            ref = str(raw).split(".", 1)[0].upper()
            return (placed.get(ref) or ("", ""))[1]

        mix = ladder_motor_on_rails(left, right, _lib_of(left), _lib_of(right))
        if mix:
            msg["_harness_error"] = mix
            return msg
    if "." in left and "." in right:
        a_ref, a_pin = left.split(".", 1)
        b_ref, b_pin = right.split(".", 1)
        a_lib = (placed.get(a_ref.upper()) or ("", ""))[1].lower()
        b_lib = (placed.get(b_ref.upper()) or ("", ""))[1].lower()
        jumped = (
            ("fuse" in a_lib and "contactors" in b_lib and b_pin.upper() == "A1")
            or ("fuse" in b_lib and "contactors" in a_lib and a_pin.upper() == "A1")
        )
        if jumped and _operators_on_sheet(turns, job):
            msg["_harness_error"] = (
                "Do not jump fuse to coil A1. Wire the series path on the print "
                "(fuse then the operators, then the coil)."
            )
            return msg
        coil_lamp = (
            (("coil" in a_lib or ("contactors" in a_lib and "aux" not in a_lib)) and ("pilot" in b_lib or b_lib.endswith("light")))
            or (("coil" in b_lib or ("contactors" in b_lib and "aux" not in b_lib)) and ("pilot" in a_lib or a_lib.endswith("light")))
        )
        if coil_lamp:
            msg["_harness_error"] = (
                "Do not series a lamp with a coil (two loads). "
                "Wire the lamp on a rung with the aux contact."
            )
            return msg
        a_coil = "coil" in a_lib or ("contactors" in a_lib and "aux" not in a_lib)
        b_coil = "coil" in b_lib or ("contactors" in b_lib and "aux" not in b_lib)
        if a_coil and "aux" in b_lib and a_pin.upper() in {"A1", "A2"}:
            msg["_harness_error"] = (
                "Do not hang an aux contact off the coil A1/A2 (two loads / wrong rung). "
                "Wire the aux to the lamp, or in parallel with START for hold-in."
            )
            return msg
        if b_coil and "aux" in a_lib and b_pin.upper() in {"A1", "A2"}:
            msg["_harness_error"] = (
                "Do not hang an aux contact off the coil A1/A2 (two loads / wrong rung). "
                "Wire the aux to the lamp, or in parallel with START for hold-in."
            )
            return msg
        if a_ref.upper() == b_ref.upper():
            pair = {a_pin.upper(), b_pin.upper()}
            lib = (placed.get(a_ref.upper()) or ("", ""))[1]
            if "contactors" in lib.lower() and pair == {"A1", "A2"}:
                msg["_harness_error"] = (
                    "Do not short the coil (A1–A2). "
                    "Wire each coil pin to the other devices shown on the print."
                )
                return msg
            msg["_harness_error"] = (
                f"Do not jumper {a_ref} to itself. Wire it to another device on the print."
            )
            return msg
        from panelcad.core.circuit import control_power_conflict

        a_lib = (placed.get(a_ref.upper()) or ("", ""))[1]
        b_lib = (placed.get(b_ref.upper()) or ("", ""))[1]
        mix = control_power_conflict(a_lib, a_pin, b_lib, b_pin)
        if mix:
            msg["_harness_error"] = mix
            return msg
    return msg


def coerce_title(msg: dict[str, Any], job: Path, turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Title-block values come from the print. Drawing prefs follow the run."""

    if msg.get("op") != "set_title_block":
        return msg
    msg = dict(msg)
    if from_print_standard() == "NEMA/ANSI":
        msg["standard"] = "NEMA/ANSI"
        if not msg.get("coil_style"):
            msg["coil_style"] = "square"
    if from_print_style() == "ladder":
        msg["drawing_style"] = "ladder"
    page = from_print_page_text(job).strip()
    if page:
        fields = _printed_fields(page)
        if fields.get("drawing_number"):
            msg["drawing_number"] = fields["drawing_number"]
        if fields.get("company"):
            msg["company"] = fields["company"]
        if fields.get("title"):
            msg["title"] = fields["title"]
        if fields.get("description"):
            msg["description"] = fields["description"]
        words = fields.get("heading_words") or set()
        given = str(msg.get("name") or _project_name(turns, job) or "")
        if words and not (_real_words(given) & set(words)):
            copied = fields.get("project") or fields.get("title") or fields.get("description")
            if copied:
                msg["name"] = copied
    return msg


def rewrite_from_print(msg: dict[str, Any], job: Path, turns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    op = str(msg.get("op") or "")
    if op in {"help", "inspect", "inspect_device"}:
        idle = 0
        for turn in reversed(turns or []):
            top = str(turn.get("op") or "")
            ok = bool((turn.get("reply") or {}).get("ok"))
            if top in {"help", "inspect", "inspect_device"} or not ok:
                idle += 1
            else:
                break
        if idle >= 3:
            stuck = dict(msg)
            stuck["_harness_error"] = (
                "LOOP: help/inspect is not progress. Do not send help. Follow NEXT."
            )
            return stuck
    if op == "create_project":
        msg = dict(msg)
        if from_print_standard() == "NEMA/ANSI":
            msg["standard"] = "NEMA/ANSI"
            msg.setdefault("coil_style", "square")
        if from_print_style() == "ladder":
            msg["drawing_style"] = "ladder"
        page = from_print_page_text(job).strip()
        if page:
            fields = _printed_fields(page)
            words = fields.get("heading_words") or set()
            given = str(msg.get("name") or "")
            if words and not (_real_words(given) & set(words)):
                copied = fields.get("project") or fields.get("title") or fields.get("description")
                if copied:
                    msg["name"] = copied
    if op == "library_search" and from_print_standard() == "NEMA/ANSI":
        msg = dict(msg)
        msg["standard"] = "NEMA/ANSI"
    if op == "cleanup" and _phase_wire_gap(turns, job):
        blocked = dict(msg)
        blocked["_harness_error"] = (
            f"Do not cleanup yet. {_phase_wire_gap(turns, job)}. "
            "Wire the missing pole first."
        )
        return blocked
    if op == "add_sheet" and from_print_page_count() <= 1:
        blocked = dict(msg)
        blocked["_harness_error"] = (
            "This print is one page. Do not add_sheet. "
            "Place or wire the next symbol on the current sheet."
        )
        return blocked
    msg = coerce_place(msg, job, turns)
    if msg.get("_harness_error"):
        return msg
    msg = coerce_title(msg, job, turns)
    return coerce_wire_pins(msg, job, turns)


def _nudge_from_print(turns: list[dict[str, Any]], job: Path) -> str:
    phase = current_phase(turns, job)
    seen = _seen_ops(turns)
    if phase == "TITLE":
        page = from_print_page_text(job).strip()
        fields = _printed_fields(page) if page else {}
        if not _phase_op_done(turns, "create_project") and not _phase_op_done(turns, "open"):
            name = str(fields.get("project") or fields.get("title") or "Power")
            return (
                "Next op MUST be create_project. Set name to words printed on THIS sheet. "
                "Example: "
                + json.dumps({"op": "create_project", "name": name}, separators=(",", ":"))
            )
        if not _phase_op_done(turns, "save"):
            return 'Next op MUST be: {"op":"save"}'
        example: dict[str, Any] = {
            "op": "set_title_block",
            "drawing_number": str(fields.get("drawing_number") or "..."),
            "rev": "",
            "company": str(fields.get("company") or ""),
        }
        if fields.get("project") and not _title_name_matches(turns, job):
            example["name"] = fields["project"]
        elif fields.get("title") and not _title_name_matches(turns, job):
            example["name"] = fields["title"]
        if fields.get("title"):
            example["title"] = fields["title"]
        if from_print_standard() == "NEMA/ANSI":
            example["standard"] = "NEMA/ANSI"
            example["coil_style"] = "square"
        return (
            "Copy title-block boxes from the printed page. Leave unread boxes empty. "
            "Do not invent a company. Example: "
            + json.dumps(example, separators=(",", ":"))
        )
    hints = _active_hints(turns, job)
    if phase == "PLACE" and not _phase_op_done(turns, "library_search"):
        query = _search_query(hints)
        std = (
            ',"standard":"NEMA/ANSI"'
            if from_print_standard() == "NEMA/ANSI"
            else ""
        )
        return (
            "library_search the largest unique device drawn on the print. "
            f'Example: {{"op":"library_search","query":"{query}"{std}}}'
        )
    last_err = str(((turns[-1] if turns else {}).get("reply") or {}).get("error") or "")
    refs = _refs_from_turns(turns)
    families = _families_from_turns(turns)
    if phase == "PLACE":
        if "already exists" in last_err.lower():
            n_pages = from_print_page_count()
            if n_pages > 1:
                empty = _empty_sheets(turns, job)
                if empty:
                    sid = empty[0]
                    return (
                        "That ref is already on this sheet. Do not retry it. "
                        f"set_active_sheet {sid} and place the next printed page. "
                        f'Example: {{"op":"set_active_sheet","sheet_id":"{sid}"}}'
                    )
                n_sheets = _sheet_count(turns, job)
                if n_sheets < n_pages:
                    nxt = n_sheets + 1
                    return (
                        "That ref is already on this sheet. Do not retry it. "
                        "add_sheet for the next printed page. "
                        f'Example: {{"op":"add_sheet","sheet_id":"p{nxt}","title":"Page {nxt}","kind":"schematic"}}'
                    )
            return (
                "That ref is already on the sheet. Do not retry it. "
                "Place a different device with a new ref."
            )
        page = from_print_page_text(job).strip()
        if page and _power_device_page(page):
            nxt = _next_power_place(page, _placed_libs(turns, job))
            if nxt:
                lib, ref = nxt
                return (
                    "Printed device refs on this page are power letters (Q/K/F/M). "
                    f"Place {ref} next. Do not place a pushbutton or a control relay. "
                    f'Example: {{"op":"place","library_id":"{lib}","ref":"{ref}","x":80,"y":80,"sheet":"main"}}'
                )
        if len(refs) < MIN_DEVICES or len(families) < MIN_FAMILIES:
            lib, ref = _place_example(hints, turns, job)
            return (
                f"Only {len(refs)} devices / {len(families)} catalog families "
                f"(need {MIN_DEVICES} devices and {MIN_FAMILIES} kinds). "
                "Place the next symbol drawn on the print. "
                "Do not add PLC/HMI/VFD/motor unless drawn. "
                f'Example: {{"op":"place","library_id":"{lib}","ref":"{ref}","x":80,"y":80,"sheet":"main"}}'
            )
        libs_now = _placed_libs(turns, job)
        if "3wire" in hints:
            have_start = any("pb_no" in lib for lib in libs_now) or any(
                r.upper().startswith("START") for r in _refs_from_turns(turns)
            )
            have_stop = any("pb_nc" in lib for lib in libs_now) or any(
                r.upper().startswith("STOP") for r in _refs_from_turns(turns)
            )
            if not have_start:
                return (
                    "Heading is 3-wire control. Place START (NO) from the print "
                    "before adding more wires onto one fuse pin. "
                    f'Example: {{"op":"place","library_id":"{_lib("iec.operators.pb_no")}","ref":"START","x":80,"y":160,"sheet":"main"}}'
                )
            if not have_stop:
                return (
                    "Heading is 3-wire control. Place STOP (NC) from the print. "
                    f'Example: {{"op":"place","library_id":"{_lib("iec.operators.pb_nc")}","ref":"STOP","x":80,"y":200,"sheet":"main"}}'
                )
            if not _lib_match(libs_now, "contactors"):
                return (
                    "Heading is 3-wire control. Place the coil/contactor shown on the print. "
                    f'Example: {{"op":"place","library_id":"{_lib("iec.contactors.ac3_9a")}","ref":"K1","x":200,"y":80,"sheet":"main"}}'
                )
            if not _lib_match(libs_now, "overload"):
                return (
                    "Heading is 3-wire control. Place the overload shown on the print. "
                    f'Example: {{"op":"place","library_id":"{_lib("iec.protection.overload_9a")}","ref":"OL","x":200,"y":160,"sheet":"main"}}'
                )
            tw = _three_wire_gap(turns, job)
            if tw and '"op":"place"' in tw.replace(" ", ""):
                return tw
        if "relay" in hints:
            if not any("aux" in lib for lib in libs_now):
                return (
                    "Heading is relay logic. Place the aux NO drawn on the print. "
                    f'Example: {{"op":"place","library_id":"{_lib("iec.contactors.aux_no")}","ref":"CR1-3-4","x":80,"y":160,"sheet":"main"}}'
                )
            if not any("pilot" in lib or "light" in lib for lib in libs_now):
                return (
                    "Heading is relay logic. Place the lamp drawn on the print. "
                    f'Example: {{"op":"place","library_id":"{_lib("iec.operators.pilot_light")}","ref":"PL1","x":200,"y":160,"sheet":"main"}}'
                )
        if hints & {"plc", "vfd", "estop"} and _is_starter_kit(turns, job):
            want = (
                f"E-STOP ({_lib('iec.operators.estop')}) and/or PLC"
                if "estop" in hints
                else "a PLC brick"
                if "plc" in hints
                else "a VFD/drive"
            )
            return (
                f"START/STOP/fuse/coil is a starter template. This print needs {want}. "
                "Place what is drawn."
            )
        n_pages = from_print_page_count()
        if n_pages > 1:
            empty = _empty_sheets(turns, job)
            if empty:
                sid = empty[0]
                return (
                    f"Sheet {sid} is empty. set_active_sheet to {sid}, then place the symbols "
                    f"from that printed page. "
                    f'Example: {{"op":"set_active_sheet","sheet_id":"{sid}"}}'
                )
            n_sheets = _sheet_count(turns, job)
            if n_sheets < n_pages:
                nxt = n_sheets + 1
                return (
                    f"Print has {n_pages} pages; drawing has {n_sheets} sheet(s). "
                    "add_sheet for the next printed page, then set_active_sheet, "
                    "then place only that page's symbols. "
                    f'Example: {{"op":"add_sheet","sheet_id":"p{nxt}","title":"Page {nxt}","kind":"schematic"}}'
                )
        lib, ref = _place_example(hints, turns, job)
        return (
            "Place the next symbol drawn on the print. "
            "Do not add PLC/HMI/VFD/motor unless drawn. "
            f'Example: {{"op":"place","library_id":"{lib}","ref":"{ref}","x":80,"y":80,"sheet":"main"}}'
        )
    if phase == "WIRE":
        phase_hint = _phase_wire_nudge(turns, job)
        if phase_hint:
            return phase_hint
        libs = _placed_libs(turns, job)
        wired = _wired_refs(turns, job)
        coils = _coil_refs(turns, job)
        if "relay" in hints:
            lamps = {
                ref
                for ref, lib in _all_placed(turns, job).items()
                if "pilot" in lib.lower()
            }
            if lamps and not (lamps & wired):
                aux = _first_of(_all_placed(turns, job), "aux_no", "aux")
                lamp = sorted(lamps)[0]
                src = f"{aux}.4" if aux else "L1"
                return (
                    f"Lamp {lamp} is not in a circuit. Wire it on a rung with the aux contact. "
                    f'Example: {{"op":"wire","from":"{src}","to":"{lamp}.X1","net":"L1","sheet":"main"}}'
                )
        if coils - wired:
            page = from_print_page_text(job).strip()
            if page and _suppress_starter_kit(page):
                example = _power_wire_example(turns, job)
                if example:
                    return (
                        "Power circuit: wire the next power pole on the print. "
                        "Do not wire a pushbutton or short coil A1–A2. "
                        f"Example: {example}"
                    )
            coil = sorted(coils - wired)[0]
            placed_now = _all_placed(turns, job)
            pb = _first_of(placed_now, "pb_no")
            src = _ex_pin(turns, job, pb, "14", "4") if pb else "START.4"
            return (
                f"Coil {coil} is on the sheet but not wired. "
                f"Wire the operator to {coil}.A1 — not the aux or lamp. "
                f'Example: {{"op":"wire","from":"{src}","to":"{coil}.A1","net":"L1","sheet":"main"}}'
            )
        motors = _refs_of_kind(turns, job, "motor")
        if motors and from_print_style() != "ladder" and not (motors & wired):
            page = from_print_page_text(job).strip()
            if page and _suppress_starter_kit(page):
                example = _power_wire_example(turns, job)
                if example:
                    return (
                        "Power circuit: wire the next power pole on the print. "
                        "Do not wire a pushbutton or short coil A1–A2. "
                        f"Example: {example}"
                    )
            motor = sorted(motors)[0]
            coil = sorted(coils)[0] if coils else "K1"
            return (
                f"Motor {motor} is on the sheet but not wired. "
                f"Land the load from the contactor power poles. "
                f'Example: {{"op":"wire","from":"{coil}.T1","to":"{motor}.U1","net":"T1","sheet":"main"}}'
            )
        rail_hit = _motor_on_ladder_rails(turns, job)
        if rail_hit:
            return rail_hit
        law = _circuit_laws_gap(turns, job)
        if law:
            return law
        pe_refs = _refs_of_kind(turns, job, "terminals.pe") | _refs_of_kind(turns, job, ".pe_")
        if pe_refs and not (pe_refs & wired) and not (
            (hints & {"relay", "transformer", "3wire"}) and not _mentions_earth(_score_text(turns, job))
        ):
            pe = sorted(pe_refs)[0]
            ol = _first_of(_all_placed(turns, job), "overload")
            src = ol or (sorted(coils)[0] if coils else None)
            pin = "96" if ol else "A2"
            example = (
                f'{{"op":"wire","from":"{src}.{pin}","to":"{pe}.A","net":"0V","sheet":"main"}}'
                if src
                else f'{{"op":"wire","from":"STOP.2","to":"{pe}.A","sheet":"main"}}'
            )
            return (
                f"{pe} is on the sheet but not wired. Connect it only if the print shows that net. "
                f"Example: {example}"
            )
        last = turns[-1] if turns else {}
        err = str((last.get("reply") or {}).get("error") or "")
        if (
            "same pin" in err.lower()
            or "short the coil" in err.lower()
            or "jump fuse" in err.lower()
        ):
            return (
                "That wire is illegal. Do not retry it. "
                "Wire two different devices that are connected on the print."
            )
        if "too many wires" in err.lower() or "already on the sheet" in err.lower():
            return (
                "Do not retry that wire. Connect two devices that are not already joined, "
                "using a pin with fewer than two wires."
            )
        crowded = _crowded_pin(turns, job)
        if crowded:
            return (
                f"Too many wires on {crowded}. That is a star, not a circuit. "
                "Wire a series path along the print (A to B to C), each pin used once or twice."
            )
        if not (hints & {"plc", "vfd", "estop"}) and not _suppress_starter_kit(
            from_print_page_text(job)
        ):
            gap = _three_wire_gap(turns, job)
            if gap and '"op":"place"' not in gap.replace(" ", ""):
                return gap
        need_wires = (
            MIN_WIRES_3WIRE
            if (
                _lib_match(libs, "overload")
                and _lib_match(libs, "contactors")
                and _lib_match(libs, "operators")
            )
            else MIN_WIRES
        )
        placed = sorted(_refs_from_turns(turns))
        extra = (
            " Wire each net drawn on the print. from/to are Ref.Pin. "
            "Coil pins are A1/A2, not L1/T1."
        )
        if err:
            extra = f" Last error: {err}." + extra
        if len(placed) >= 2:
            a, b = _next_wire_pair(turns, placed)
            example = (
                f'{{"op":"wire","from":"{_endpoint_example(turns, a)}",'
                f'"to":"{_endpoint_example(turns, b)}","net":"L1","sheet":"main"}}'
            )
            return (
                f"Wire devices already on the sheet ({', '.join(placed)}). "
                f"from/to must be Ref.Pin. Example: {example}.{extra}"
            )
        return (
            "Wire nets drawn on the print using Ref.Pin for devices you placed."
            f"{extra}"
        )
    if "cleanup" not in seen:
        return 'Next op MUST be: {"op":"cleanup"}'
    if "validate" not in seen:
        return 'Next op MUST be: {"op":"validate"}'
    ok_now, why = _check_from_print(turns, job)
    if not ok_now:
        return f"{why} Then save."
    if not seen or seen[-1] != "save":
        return 'Next op MUST be: {"op":"save"}'
    return 'Checks pass. Send {"op":"done","summary":"recreated sheet from print"}'


def _last_wire_ends(turns: list[dict[str, Any]]) -> tuple[str, str]:
    for turn in reversed(turns):
        if turn.get("op") != "wire":
            continue
        msg = turn.get("msg") or {}
        return (
            str(msg.get("from") or "").upper(),
            str(msg.get("to") or "").upper(),
        )
    return "", ""


def _next_wire_pair(turns: list[dict[str, Any]], placed: list[str]) -> tuple[str, str]:
    """Skip the last failed from/to pair so NEXT is a different step, not a retry."""

    a, b = placed[0], placed[1]
    last_from, last_to = _last_wire_ends(turns)
    last_ok = False
    for turn in reversed(turns):
        if turn.get("op") != "wire":
            continue
        last_ok = bool((turn.get("reply") or {}).get("ok"))
        break
    if last_ok or not last_from:
        return a, b
    last_refs = {
        last_from.split(".", 1)[0],
        last_to.split(".", 1)[0] if last_to else "",
    }
    if a.upper() in last_refs and b.upper() in last_refs and len(placed) >= 3:
        return placed[1], placed[2]
    return a, b


def _endpoint_example(turns: list[dict[str, Any]], ref: str) -> str:
    lib = _lib_for_ref(turns, ref)
    pins = _pins_for_library(lib) if lib else []
    if not pins:
        return f"{ref}.1"
    preferred = _prefer_control_pin(lib or "", None, "iec.operators.pb_no")
    return f"{ref}.{_pick_pin(preferred, pins)}"


def _families_from_turns(turns: list[dict[str, Any]]) -> set[str]:
    families: set[str] = set()
    for _ref, lib in _placed_map(turns).values():
        if lib:
            families.add(_family_key(lib))
    return families


def _families_on_disk(job: Path) -> set[str]:
    if not job.is_file():
        return set()
    session = Session.open(job)
    families: set[str] = set()
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.library_id and not _is_net_ref(comp.id):
                families.add(_family_key(comp.library_id))
    return families


def _check_from_print(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
    disk_refs, disk_wires = _placed_on_disk(job)
    refs = {r for r in (disk_refs | _refs_from_turns(turns)) if not _is_net_ref(r)}
    wires = max(disk_wires, _wires_from_turns(turns))
    ops = _seen_ops(turns)
    families = _families_from_turns(turns) | _families_on_disk(job)
    libs = _placed_libs(turns, job)
    if "create_project" not in ops and "open" not in ops:
        return False, "did not create or open a drawing"
    if len(refs) < MIN_DEVICES:
        return False, f"only {len(refs)} devices placed from the print (need {MIN_DEVICES})"
    if wires < MIN_WIRES:
        return False, f"{len(refs)} devices but only {wires} wires from the print"
    if "save" not in ops:
        return False, "never saved"
    if "place" in ops:
        last_place = max(
            (
                i
                for i, turn in enumerate(turns)
                if turn.get("op") == "place" and (turn.get("reply") or {}).get("ok")
            ),
            default=-1,
        )
        last_save = max((i for i, op in enumerate(ops) if op == "save"), default=-1)
        last_ref = ""
        if last_place >= 0:
            last_msg = turns[last_place].get("msg") or {}
            last_ref = str(
                ((turns[last_place].get("reply") or {}).get("result") or {}).get("id")
                or last_msg.get("ref")
                or ""
            ).upper()
        disk_has = last_ref in disk_refs if last_ref else bool(disk_refs)
        if last_place >= 0 and last_save < last_place and not disk_has:
            return False, "placed devices but did not save them to the job YAML"
    if len(families) < MIN_FAMILIES:
        return False, (
            f"only {len(families)} catalog families among successful places "
            f"(need {MIN_FAMILIES} kinds, not four of the same)"
        )
    if not _title_filled(job) and "set_title_block" not in ops:
        return False, "title block not copied from the print"
    if "library_search" not in ops:
        return False, "did not library_search before placing"
    n_pages = from_print_page_count()
    if n_pages > 1:
        n_sheets = _sheet_count(turns, job)
        if n_sheets < n_pages:
            return False, f"print has {n_pages} pages but drawing has {n_sheets} sheets"
        empty = _empty_sheets(turns, job)
        if empty:
            return False, f"sheet {empty[0]} has no devices from that printed page"
        if job.is_file():
            session = Session.open(job)
            for sh in session.project.sheets:
                n = sum(1 for c in sh.components if c.library_id and not _is_net_ref(c.id))
                if n < 2:
                    return False, (
                        f"sheet {sh.id} only has {n} devices from that printed page (need 2)"
                    )
    if from_print_standard() == "NEMA/ANSI":
        iec_hit = next((lib for lib in libs if str(lib).startswith("iec.")), None)
        if iec_hit:
            return False, f"standard is NEMA/ANSI but placed IEC symbol {iec_hit}"
    hints = _active_hints(turns, job)
    page = from_print_page_text(job).strip()
    if page and _power_device_page(page):
        missing = _next_power_place(page, libs)
        if missing:
            return False, f"printed power device {missing[1]} is not placed"
        if any(_is_pushbutton_lib(lib) or _is_control_relay_lib(lib) for lib in libs):
            return False, "power page cannot be a pushbutton or a control relay"
    if "3wire" in hints and not any("pb_no" in lib for lib in libs):
        return False, "3-wire heading but no START placed"
    if "3wire" in hints and not any("pb_nc" in lib for lib in libs):
        return False, "3-wire heading but no STOP placed"
    if "3wire" in hints and not _lib_match(libs, "contactors"):
        return False, "3-wire heading but no coil/contactor placed"
    if "plc" in hints and not _lib_match(libs, "plc"):
        return False, "print heading calls for a PLC; starter kit is not a PLC drawing"
    if "vfd" in hints and not _lib_match(libs, "drive"):
        return False, "print heading calls for a VFD/drive; starter kit is not a drive drawing"
    if "estop" in hints and not any("estop" in lib for lib in libs):
        return False, "print heading is E-stop/emergency; START is not E-STOP"
    if hints & {"plc", "vfd", "estop"} and _is_starter_kit(turns, job):
        return False, "nonsense starter kit (START/STOP/fuse/coil) does not match this print"
    if _lib_match(libs, "drive") and "vfd" not in hints:
        return False, "do not invent a VFD unless a drive is drawn on the print"
    if _lib_match(libs, "hmi") and "hmi" not in hints and "HMI" not in _score_text(turns, job).upper():
        return False, "do not invent an HMI unless an HMI is drawn on the print"
    if "relay" in hints and any("fuse" in lib for lib in libs):
        return False, "do not invent a fuse unless a fuse is drawn on the print"
    if _lib_match(libs, "motor") and "motor" not in hints and (hints & {"relay", "transformer"}):
        return False, "do not invent a motor unless a motor is drawn on the print"
    if "relay" in hints and not any("pilot" in lib or "light" in lib for lib in libs):
        return False, "relay heading but no lamp/pilot placed"
    if (
        "relay" in hints
        and _lib_match(libs, "contactors")
        and not any("aux" in lib for lib in libs)
    ):
        return False, "relay heading but no aux contact placed"
    if any("estop" in lib for lib in libs) and "estop" not in hints and (
        hints & {"relay", "transformer", "3wire"}
    ):
        return False, "do not invent E-STOP unless an E-stop is drawn on the print"
    if (
        (hints & {"relay", "transformer", "3wire"})
        and not _mentions_earth(_score_text(turns, job))
        and any("terminals.pe" in lib or ".pe_" in lib for lib in libs)
    ):
        return False, "do not invent PE/GND unless that symbol is drawn on the print"
    crowded = _crowded_pin(turns, job)
    if crowded:
        return False, f"star wiring on {crowded}; use a series path"
    if _fuse_jumped_to_coil(turns, job):
        return False, "fuse jumped to coil; wire the series path on the print"
    if from_print_style() == "ladder" and "cleanup" not in ops:
        return False, "ladder sheet needs cleanup"
    wired = _wired_refs(turns, job)
    coils = _coil_refs(turns, job)
    if coils - wired:
        return False, "coil placed but not wired"
    motors = _refs_of_kind(turns, job, "motor")
    if from_print_style() == "ladder":
        rail_hit = _motor_on_ladder_rails(turns, job)
        if rail_hit:
            return False, rail_hit
    elif motors and not (motors & wired):
        return False, "motor placed but not wired"
    lamps = _refs_of_kind(turns, job, "pilot")
    if lamps and not (lamps & wired):
        return False, "lamp/pilot placed but not wired"
    pe_refs = _refs_of_kind(turns, job, "terminals.pe") | _refs_of_kind(turns, job, ".pe_")
    if pe_refs and not (pe_refs & wired):
        return False, "PE/GND placed but not wired"
    for kind, label in (("plc", "PLC"), ("drive", "VFD"), ("hmi", "HMI")):
        refs_k = _refs_of_kind(turns, job, kind)
        if refs_k and not (refs_k & wired):
            return False, f"{label} placed but not wired"
    if any("estop" in lib for lib in libs):
        estop_refs = {
            ref
            for ref, lib in _all_placed(turns, job).items()
            if "estop" in lib.lower()
        }
        if estop_refs and not (estop_refs & wired):
            return False, "E-STOP placed but not wired"
    law = _circuit_laws_gap(turns, job)
    if law:
        return False, law
    phase_gap = _phase_wire_gap(turns, job)
    if phase_gap:
        return False, phase_gap
    return True, f"{len(refs)} devices, {len(families)} families, {wires} wires, saved"


def from_print_task() -> Task:
    n = from_print_page_count()
    return Task(
        id="from-print",
        instruction=build_instruction(),
        allowed=FROM_PRINT_OPS,
        max_turns=48 if n <= 1 else min(96, 36 + 24 * n),
        check=_check_from_print,
        nudge=_nudge_from_print,
        needs_open=False,
        rewrite=rewrite_from_print,
        windowed=True,
        window_user=from_print_window_user,
        phase_allowed=from_print_phase_allowed,
    )


FROM_PRINT_TASKS = {from_print_task().id: from_print_task()}


def run_from_print(
    *,
    pdf: Path | None = None,
    page: int = 1,
    pages: str | None = None,
    model: str = "gemma3:4b",
    host: str = "http://127.0.0.1:11434",
    via_cli: bool = True,
    client: Any | None = None,
    scratch: Path | None = None,
    standard: str = "IEC",
    style: str = "schematic",
) -> Any:
    from panelcad.sharpen.loop import OllamaClient, _run_dir, run_sharpen

    pdf = Path(pdf) if pdf is not None else EXAMPLE_PDF
    if not pdf.is_file():
        raise FileNotFoundError(
            f"controls print PDF not found: {pdf}. Pass --pdf PATH to any print."
        )
    std = normalize_standard(standard)
    sty = normalize_drawing_style(style)
    want = parse_pages(pages, page, pdf_page_count(pdf))
    tokens = [
        set_from_print_standard(std),
        set_from_print_style(sty),
        _FROM_PRINT_PAGE_COUNT.set(len(want)),
    ]
    text_token: Token | None = None
    try:
        run_dir = scratch or _run_dir(None)
        dpi = 80 if len(want) > 1 else 110
        pngs: list[Path] = []
        images: list[str] = []
        page_texts = read_pdf_pages_text(pdf, want)
        chunks: list[str] = []
        for n in want:
            png = render_pdf_page(pdf, n, run_dir / f"print-p{n:02d}.png", dpi=dpi)
            raw = page_texts.get(n, "")
            (run_dir / f"print-p{n:02d}-text.txt").write_text(raw, encoding="utf-8")
            if raw.strip():
                chunks.append(f"PAGE {n}\n{raw.strip()}")
            pngs.append(png)
            images.append(png_to_b64(png, max_width=720 if len(want) > 1 else 900))
        text_token = set_from_print_page_text("\n".join(chunks))
        llm = client or OllamaClient(
            model=model,
            host=host,
            num_ctx=16384 if len(images) > 1 else 8192,
            timeout=300.0 if len(images) > 1 else 180.0,
            images=images,
        )
        if client is not None and hasattr(client, "images"):
            client.images = images
        (run_dir / "print-source.txt").write_text(
            f"pdf={pdf}\npages={','.join(str(n) for n in want)}\n"
            f"images={';'.join(str(p) for p in pngs)}\n"
            f"standard={std}\nstyle={sty}\n",
            encoding="utf-8",
        )
        return run_sharpen(
            "from-print",
            model=model,
            host=host,
            via_cli=via_cli,
            client=llm,
            scratch=run_dir,
            copy=False,
            fresh=True,
        )
    finally:
        if text_token is not None:
            _FROM_PRINT_PAGE_TEXT.reset(text_token)
        _FROM_PRINT_PAGE_COUNT.reset(tokens[2])
        _FROM_PRINT_STYLE.reset(tokens[1])
        _FROM_PRINT_STANDARD.reset(tokens[0])
