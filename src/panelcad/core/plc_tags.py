"""PLC tag export from the I/O list.

Each exporter cites the vendor document it was checked against. A format that
could not be read column-for-column is a labeled generic CSV, not a guessed
vendor file.

Studio 5000 Logix Designer tag CSV
  CSV record (Type, Scope, Name, Description, Data Type, Specifier):
  https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/contents-ditamap/studio-5000-logix-designer/import-and-export/csv-record-format.html
  CSV file shape (first line is a version number, a line whose first column is
  Type is a schema line, Remark lines are ignored, IEC-61131 names, 40 characters):
  https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/contents-ditamap/studio-5000-logix-designer/import-and-export/csv-file-format.html
  TAG / ALIAS records add Attributes as the seventh field. Alias specifier is the
  base tag. Alias and comment data types are blank. Empty scope is controller
  scope. 1756-RM014, "Structure Tags and Comments in an Import/Export File":
  https://literature.rockwellautomation.com/idc/groups/literature/documents/rm/1756-rm014_-en-p.pdf
  The file-format page says, quoted:
  "The first line in the file contains a version number, followed by a set of
  component blocks."
  "Any line whose first column is "Type" is interpreted as a schema line."
  "Any line whose first column is Remark is ignored by the import process."
  So line 1 is the tag-CSV version 0.3 (not the L5K IE_VER 2.xx table in
  1756-RM014), then a Remark line, then the TYPE schema. The keyword is written
  Remark, the spelling on that page. 1756-RM014 uses REMARK for the same idea
  ("start the line with REMARK and a comma"). Field exports often put several
  lowercase remark lines before 0.3; this file follows the file-format page.
  Compact 5000 alias targets are per point, not a packed Data word.
  5069-UM004H-EN-P (March 2026) pp. 121, 126, and 129: digital members are
  Local:slot:I.Ptxx.Data and Local:slot:O.Ptxx.Data. 5069-UM005F-EN-P
  (December 2024) p. 139: analog members are Local:slot:I.Chxx.Data and
  Local:slot:O.Chxx.Data. xx is two digits. Base tags are BOOL (discrete)
  or REAL (analog) with an empty specifier.

TIA Portal PLC tag table (.xlsx)
  "Format of the export file (*.xlsx)" (the topic the v21 help page loads):
  https://docs.tia.siemens.cloud/r/en-us/v21/declaring-plc-tags/editing-plc-tag-tables/exporting-and-importing-plc-tags/format-of-the-export-file-.xlsx
  Quoted from that topic: "The export file contains two sheets:" "The name of
  the first sheet is "PLCTags"." "The name of the second sheet is
  "TagTableProperties"." "The column titles are predefined and are always
  expected to be in English." PLCTags columns: Name, Path, Data Type, Logical
  Address, Comment, Hmi Visible, Hmi Accessible, Hmi Writeable, Typeobject ID,
  Version ID. TagTableProperties columns: Path, BelongsToUnit, Accessibility.
  "The sorting order of columns can vary." A file "must contain all or a subset
  of the displayed columns" and "must not contain any other self-defined
  columns." Hmi Visible, Hmi Accessible, and Hmi Writeable expect TRUE or FALSE.
  Importing PLC tags says to set custom property TIA_Version to "1.0":
  https://docs.tia.siemens.cloud/r/en-us/v21/declaring-plc-tags/editing-plc-tag-tables/exporting-and-importing-plc-tags/importing-plc-tags
  Logical Address uses international mnemonics (%I0.0, %Q0.0, %IW, %QW).
  Data Type uses the PLC tag table notation (Bool for DI/DO, Int for AI/AO).
  %IW0 is IB0+IB1 and %QW0 is QB0+QB1, so an analog word never shares a byte
  with a digital input or output. The number in %IWn / %QWn is the byte
  address and stays even. A module property start_address (or the component)
  is that byte. Otherwise the project/dialog analog start is used. Otherwise
  the next free even word begins after the highest digital byte in that image.
  Target tia_csv remains a labeled generic CSV for callers who want text.

Do-more Designer element documentation
  File → Import → Element Documentation. Each line is Element, Nickname,
  Extra Info, Description. No header row. Nickname is 1–16 letters, digits,
  and underscores, starts with a letter or underscore, is unique, and must not
  match an element id. Extra Info is 1–16 characters. Description is 1–144:
  https://hosteng.com/dmdhelp/content/Import_Export/Import_Element_Documentation.htm
  X is discrete input, Y discrete output, WX analog input, WY analog output,
  numbered in decimal from 0:
  https://hosteng.com/DmDHelp/Content/Data_Types/Builtin_Data_Blocks.htm
  Discrete allocation is on byte boundaries, starting at X0 / Y0:
  https://hosteng.com/dmdhelp/content/Module_Mapping/Discrete_I_O_Modules.htm

Keyence KV STUDIO device comments
  KV STUDIO Ver.11 User's Manual, File → Read device comments in CSV/TXT
  format, says the file is "a CSV format file (MBCS) (*.csv) or TXT format
  file (Unicode) (*.txt)". That section names the dialog (Look in, File name,
  Files of type, Select a program to load) and does not list columns, a
  header row, or a delimiter. The public manuals index does not host the PDF
  without a Keyence account:
  https://www.keyence.com/support/user/controls/plc/manual/
  The column layout is therefore not confirmed. Target keyence is a labeled
  generic CSV. It is not a KV STUDIO import.
  Device numbers in that generic file follow the Unit Editor default the same
  manual states under Automatically Assigning Relays and DM Entries:
  "In the default settings, relays are assigned in 10-channel units from the
  leading number of 30000 and DM entries are assigned in 100-entry units from
  the leading number of 10000."
  "For example, R10000 is channel 100." A channel is the relay number without
  the last two digits, so one channel is 100 relays and 10 channels are 1000
  relays (R30000–R30999). The next unit starts at R31000. DM blocks are 100
  words from DM10000. "You can only enter even numbers for the leading DM
  numbers."
  KV-8000 capacities (not a number map) are on the specs page: input, output,
  and internal auxiliary relays are device R, "Total 32000 points 1 bit";
  internal auxiliary relay MR is "64000 points 1 bit"; data memory DM is
  "65535 points 16 bits":
  https://www.keyence.com/products/controls/plc-building-block-type/kv-8000/specs/
  The same assignment section gives a different KV Nano rule ("unit number ×
  1000" for input and I/O units, "unit number × 1000 + 500" for output units)
  and does not state the first unit number, so the generic file does not use
  it. No X/Y addressing mode is stated there. Buffer-memory addresses for an
  analog unit are not listed; analog points use the DM auto-assign.
  Modules are taken in drawing order as the units next to the CPU. The manual
  says assignments "start from the position neighboring the CPU unit."

Omron CX-Programmer symbol table
  SYSMAC CX-Programmer Ver. 9 Operation Manual, Cat. No. W446, Chapter 5,
  Importing and Exporting Symbols:
  https://files.omron.eu/downloads/latest/manual/en/w446_cx-programmer_operation_manual_en.pdf
  "The information is in a tabbed format of text."
  "For supplying symbols complete with data-type, use format of 4 columns,
  which consists of <Symbol Name> tab <Symbol Data-type> tab <Symbol Address>
  tab <Comment>."
  The example is: "Name BOOL 1.1 this is a comment" is valid (without the ").
  "Each item should be on a new line." There is no header row. W446 does not
  state an encoding; this file is UTF-8.
  "Each symbol name must be unique within its table."
  Address form: "Entering the address '10' is interpreted by CX-Programmer as
  '0.10'. If it had been intended as bit zero at address 50, it would have
  been necessary to enter it as '5000' or (more easily) '50.00'."
  "Contact inputs will be incremented from CIO 0.00 (word CIO 0) and contact
  outputs will be incremented from CIO 100.00 (word CIO 100)."
  BOOL is a bit. UINT is "Address of an unsigned, single binary word."
  An I: or Q: prefix is applied by the IO table after assignment ("An 'I: '
  prefix indicates that the address is mapped to an input unit"). The symbol
  address itself is the CIO form above, not I: or Q:.

Omron CJ analog words (CX-Programmer addresses)
  CS/CJ Series Analog I/O Units Operation Manual, Cat. No. W345:
  https://edata.omron.com.au/eData/PLCs/CJ1/W345-E1-12.pdf
  A module that has both analog inputs and analog outputs is one CJ1W-MAD42
  (4 inputs, 2 outputs), one Special I/O unit. Section 9-5-6, page 398,
  Allocations for Normal Mode:
  "n+1 Output 1 set value", "n+2 Output 2 set value",
  "n+5 Input 1 conversion value", then n+6, n+7, and n+8 for inputs 2 to 4.
  "For the CIO word addresses, n = CIO 2000 + unit number × 10."
  Section 9-6-1, page 401: "Analog input conversion values are stored for
  each input number, in CIO words n+5 to n+8." The unit-0 example reads
  "CIO word 2005 (input number 1)".
  Section 9-7, page 410: "Analog output set values are written to CIO words
  (n+1) and (n+2)." "n+1 Output 1 set value" is "16-bit binary data." The
  unit-0 example writes "CIO word 2001 (output number 1)."
  n+3 and n+4 are "Not used" on the MAD42. A channel past that table is
  the address "unverified".
  A module with only inputs uses the AD map: n+1 is input 1 (AD041-V1 and
  AD081-V1). Channels 0–7 are n+1 through n+8 (AD081-V1). A module with
  only outputs uses the DA map: n+1 is output 1, through n+8 on the
  CJ1W-DA08V/08C (page 243). Each such module is still one unit. The DM
  area (m = D20000 + unit number × 100) holds initial settings, not the
  conversion value.

Omron Sysmac Studio global variables
  Sysmac Studio Version 1 Operation Manual, Cat. No. W504:
  https://edata.omron.com.au/eData/NJ/W504-E1-35.pdf
  Global variables are created in the global variable table. Device variables
  are created in the I/O Map, and "The contents of the AT Fields of the device
  variables are displayed automatically." The AT setting for a device variable
  is "Not possible" to change. The documented AT texts are
  "IOBus://rack#[rack_number]/slot#[slot_number]/[I/O_port_name]" for a
  CJ-series Unit and "IOBus://unit#[NX_Unit_number]/I/O_port_name" for an NX
  Unit on the CPU rack. CJ-series memory AT is words:
  "CIO Area ... %0 to %6143" and "DM Area ... %D0 to %D32767."
  That table does not list a bit form. I/O Map paste is "Variable, variable
  comment, and variable type" into an I/O Map that already exists; the manual
  does not list a standalone file's column titles. IEC 61131-10 XML import
  says "refer to the XML schema in the same folder" and does not quote the
  elements. The comment workbook ("Sysmac Studio Element Comments File",
  columns Name and Comment) updates comments on variables that already exist.
  No global-variable import file with AT columns is specified. Target sysmac
  is a labeled generic CSV. Names follow the NJ/NX CPU manual, Cat. No. W501,
  section 6-3-12, page 6-84. Usable characters include "0 to 9, A to Z, and
  a to z" and "_ (underlines)". Under the heading "Characters that cannot
  be used together": "Strings that start with "P_"", "A text string that
  starts in an underline (_) character", "A text string that contains more
  than one underline (_) character" (underscores used together, so "__"),
  and "A text string that ends in an underline (_) character". Separate
  underscores are kept, so M1_RUN_FB stays M1_RUN_FB. "Not case sensitive".
  "127 bytes". "Global variable names in the same CPU Unit" must be unique:
  https://assets.omron.eu/downloads/latest/manual/en/w501_nj_nx-series_cpu_unit_software_users_manual_en.pdf
"""

from __future__ import annotations

import csv
import re
from io import StringIO
from pathlib import Path
from typing import Any

from panelcad.core.models import Project
from panelcad.library.catalog import Catalog

TARGETS = ("studio5000", "tia", "tia_csv", "domore", "keyence", "sysmac", "cxprogrammer")
_UNVERIFIED = frozenset({"tia_csv", "keyence", "sysmac"})

TIA_PLC_COLUMNS = (
    "Name",
    "Path",
    "Data Type",
    "Logical Address",
    "Comment",
    "Hmi Visible",
    "Hmi Accessible",
    "Hmi Writeable",
    "Typeobject ID",
    "Version ID",
)
TIA_PROP_COLUMNS = ("Path", "BelongsToUnit", "Accessibility")
TIA_SHEET_TAGS = "PLCTags"
TIA_SHEET_PROPS = "TagTableProperties"
TIA_TABLE_PATH = "PLC tags"

STUDIO5000_DOC = (
    "https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/"
    "contents-ditamap/studio-5000-logix-designer/import-and-export/csv-record-format.html"
)
STUDIO5000_FILE_DOC = (
    "https://www.rockwellautomation.com/en-us/docs/studio-5000-logix-designer/37-00/"
    "contents-ditamap/studio-5000-logix-designer/import-and-export/csv-file-format.html"
)
STUDIO5000_RM = (
    "https://literature.rockwellautomation.com/idc/groups/literature/documents/rm/1756-rm014_-en-p.pdf"
)
TIA_DOC = (
    "https://docs.tia.siemens.cloud/r/en-us/v21/declaring-plc-tags/"
    "editing-plc-tag-tables/exporting-and-importing-plc-tags/"
    "basics-for-exporting-and-importing-plc-tags"
)
TIA_XLSX_DOC = (
    "https://docs.tia.siemens.cloud/r/en-us/v21/declaring-plc-tags/"
    "editing-plc-tag-tables/exporting-and-importing-plc-tags/format-of-the-export-file-.xlsx"
)
TIA_CAX_DOC = (
    "https://docs.tia.siemens.cloud/r/en-us/v21/tia-portal-openness-api-for-automation-of-engineering-workflows/"
    "export/import/importing/exporting-hardware-data/export/import-of-plc-tags"
)
DOMORE_DOC = "https://hosteng.com/dmdhelp/content/Import_Export/Import_Element_Documentation.htm"
DOMORE_BLOCKS_DOC = "https://hosteng.com/DmDHelp/Content/Data_Types/Builtin_Data_Blocks.htm"
DOMORE_MAP_DOC = "https://hosteng.com/dmdhelp/content/Module_Mapping/Discrete_I_O_Modules.htm"
KEYENCE_MANUALS = "https://www.keyence.com/support/user/controls/plc/manual/"
KEYENCE_SPECS = "https://www.keyence.com/products/controls/plc-building-block-type/kv-8000/specs/"
CXPROGRAMMER_DOC = (
    "https://files.omron.eu/downloads/latest/manual/en/w446_cx-programmer_operation_manual_en.pdf"
)
OMRON_ANALOG_DOC = "https://edata.omron.com.au/eData/PLCs/CJ1/W345-E1-12.pdf"
SYSMAC_DOC = "https://edata.omron.com.au/eData/NJ/W504-E1-35.pdf"
SYSMAC_NAMES_DOC = (
    "https://assets.omron.eu/downloads/latest/manual/en/"
    "w501_nj_nx-series_cpu_unit_software_users_manual_en.pdf"
)

_TARGET_ALIASES = {
    "studio5000": "studio5000",
    "studio_5000": "studio5000",
    "logix": "studio5000",
    "rockwell": "studio5000",
    "rslogix": "studio5000",
    "tia": "tia",
    "tiaportal": "tia",
    "tia_portal": "tia",
    "siemens": "tia",
    "tiaxlsx": "tia",
    "tia_csv": "tia_csv",
    "tiacsv": "tia_csv",
    "tia_generic": "tia_csv",
    "tiageneric": "tia_csv",
    "domore": "domore",
    "do_more": "domore",
    "brx": "domore",
    "automationdirect": "domore",
    "keyence": "keyence",
    "kvstudio": "keyence",
    "kv_studio": "keyence",
    "kv": "keyence",
    "sysmac": "sysmac",
    "sysmacstudio": "sysmac",
    "sysmac_studio": "sysmac",
    "nj": "sysmac",
    "nx": "sysmac",
    "cxprogrammer": "cxprogrammer",
    "cx_programmer": "cxprogrammer",
    "cxp": "cxprogrammer",
    "cj": "cxprogrammer",
    "cp": "cxprogrammer",
}

_NATIVE = re.compile(r"^(WX|WY|X|Y)(\d+)$", re.IGNORECASE)
_BYTE_BIT = re.compile(r"^([IQ])(\d+)[._](\d+)$", re.IGNORECASE)
_CHANNEL = re.compile(r"^(AI|AO|DI|DO|IW|QW|I|Q)(\d+)$", re.IGNORECASE)
_ELEMENT_ID = re.compile(
    r"^(WX|WY|RX|RY|DLX|DLY|DLC|DLV|MIR|MHR|SDT|UDT|X|Y|C|V|N|D|R|T|CT|MI|MC|SL|SS|PL|ST)(\d+)$",
    re.IGNORECASE,
)

PLC_IO_LIST_SCHEMA: dict[str, Any] = {
    "op": "plc_io_list",
    "description": (
        "PLC I/O list built from the drawing. One row per point on every PLC and "
        "I/O module: address, type (DI/DO/AI/AO), tag, description (connected device "
        "tag, function, wire number, sheet and line), connected device, and spares."
    ),
    "args": {
        "path": {
            "type": "string",
            "required": False,
            "description": "Optional CSV path. Omit to return the rows only.",
        },
    },
}

EXPORT_PLC_TAGS_SCHEMA: dict[str, Any] = {
    "op": "export_plc_tags",
    "description": (
        "Export PLC tags from the I/O list. studio5000 is a Logix Designer CSV tag "
        "import (ALIAS to Local:x:I.Pt00.Data, or a base BOOL/REAL tag). tia is the "
        "TIA Portal Excel tag table (sheets PLCTags and TagTableProperties). "
        "tia_csv is a labeled generic CSV. domore is a Do-more element "
        "documentation import (X0, Y0, WX0, WY0). keyence is a labeled generic "
        "CSV (KV STUDIO does not publish the device-comment columns) with the "
        "Unit Editor default devices R30000 and DM10000. sysmac is a labeled "
        "generic CSV (Sysmac Studio does not publish a global-variable import "
        "file). cxprogrammer is the CX-Programmer symbol-table paste: four "
        "tab-separated columns, Name, Data type, Address, Comment."
    ),
    "args": {
        "path": {"type": "string", "required": True, "description": "Output file path."},
        "target": {
            "type": "string",
            "required": True,
            "enum": list(TARGETS),
            "description": (
                "studio5000, tia (.xlsx), tia_csv (generic CSV), domore, "
                "keyence (generic CSV), sysmac (generic CSV), or cxprogrammer (.txt)."
            ),
        },
        "prefix": {
            "type": "string",
            "required": False,
            "description": "Text placed in front of each tag name. A missing underscore is added.",
        },
        "name_from": {
            "type": "string",
            "required": False,
            "enum": ["device", "description"],
            "description": (
                "device uses the I/O tag (parent plus contact function, such as "
                "M1_RUN_FB). description uses the I/O description."
            ),
        },
        "logix_style": {
            "type": "string",
            "required": False,
            "enum": ["alias", "base"],
            "description": "studio5000 only. alias targets Local:x:I.Pt00.Data. base writes a BOOL or REAL tag.",
        },
        "logix_slot": {
            "type": "integer",
            "required": False,
            "description": "studio5000 slot of the first module. Default 1. Later modules increment.",
        },
        "analog_start": {
            "type": "integer",
            "required": False,
            "description": (
                "TIA analog byte address for %IW/%QW when the module has no "
                "start_address. Omit to use the project property analog_start, "
                "then the next free even word after the highest digital byte. "
                "auto leaves the address automatic."
            ),
        },
    },
}


def normalize_target(value: str) -> str:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum() or ch == "_")
    key = _TARGET_ALIASES.get(token)
    if key is None:
        raise ValueError(
            f"unknown PLC tag target '{value}' "
            "(studio5000, tia, tia_csv, domore, keyence, sysmac, cxprogrammer)"
        )
    return key


def validate_export_plc_tags(msg: dict[str, Any]) -> dict[str, Any]:
    """Check export_plc_tags arguments. Raises ValueError with the field name."""

    if not str(msg.get("path") or "").strip():
        raise ValueError("path is required")
    target = normalize_target(str(msg.get("target") or ""))
    name_from = str(msg.get("name_from") or "device").strip().lower()
    if name_from not in {"device", "description"}:
        raise ValueError("name_from must be device or description")
    style = str(msg.get("logix_style") or "alias").strip().lower()
    if style not in {"alias", "base"}:
        raise ValueError("logix_style must be alias or base")
    slot_raw = msg.get("logix_slot", 1)
    if slot_raw is None or slot_raw == "":
        slot = 1
    else:
        slot = int(slot_raw)
        if slot < 0:
            raise ValueError("logix_slot must be >= 0")
    if "analog_start" not in msg:
        analog_start: int | None | str = "project"
    else:
        analog_start = _parse_analog_start(msg.get("analog_start"))
    return {
        "path": str(msg["path"]),
        "target": target,
        "prefix": str(msg.get("prefix") or ""),
        "name_from": name_from,
        "logix_style": style,
        "logix_slot": slot,
        "analog_start": analog_start,
    }


def _parse_analog_start(raw: Any) -> int | None:
    """Byte address, or None when the caller asked for the automatic word."""

    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("analog_start must be an integer byte address or auto")
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.lower() == "auto":
            return None
        if not re.fullmatch(r"\d+", text):
            raise ValueError("analog_start must be an integer byte address or auto")
        raw = int(text)
    value = int(raw)
    if value < 0:
        raise ValueError("analog_start must be >= 0 or auto")
    return value


def _project_analog_start(project: Project) -> int | None:
    if "analog_start" not in project.properties:
        return None
    return _parse_analog_start(project.properties.get("analog_start"))


def export_plc_tags(
    project: Project,
    catalog: Catalog,
    path: Path | str,
    *,
    target: str,
    prefix: str = "",
    name_from: str = "device",
    logix_style: str = "alias",
    logix_slot: int = 1,
    analog_start: int | None | str = "project",
) -> dict[str, Any]:
    from panelcad.core.electrical import io_rows

    msg: dict[str, Any] = {
        "path": path,
        "target": target,
        "prefix": prefix,
        "name_from": name_from,
        "logix_style": logix_style,
        "logix_slot": logix_slot,
    }
    if analog_start != "project":
        msg["analog_start"] = analog_start
    opts = validate_export_plc_tags(msg)
    resolved = opts["analog_start"]
    if resolved == "project":
        resolved = _project_analog_start(project)
    rows = io_rows(project, catalog)
    assert_export_target(rows, opts["target"])
    prepared = _prepare(
        rows,
        prefix=opts["prefix"],
        name_from=opts["name_from"],
        logix_slot=opts["logix_slot"],
        analog_start=resolved,
    )
    dest = Path(opts["path"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    if opts["target"] == "tia" and dest.suffix.lower() != ".xlsx":
        dest = dest.with_suffix(".xlsx")
    if opts["target"] == "cxprogrammer" and dest.suffix.lower() not in {".txt", ".tsv"}:
        dest = dest.with_suffix(".txt")
    if opts["target"] == "tia":
        _write_tia_xlsx(prepared, dest)
    else:
        text = _render_prepared(prepared, target=opts["target"], logix_style=opts["logix_style"])
        dest.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")
    return {
        "path": str(dest),
        "target": opts["target"],
        "count": len(rows),
        "format": _format_name(opts["target"]),
        "verified": opts["target"] not in _UNVERIFIED,
        "doc": _doc_url(opts["target"]),
        "note": _format_note(opts["target"]),
    }


def render_tags(
    rows: list[dict[str, str]],
    *,
    target: str,
    prefix: str = "",
    name_from: str = "device",
    logix_style: str = "alias",
    logix_slot: int = 1,
    analog_start: int | None = None,
) -> str:
    key = normalize_target(target)
    if key == "tia":
        raise ValueError("target tia is an .xlsx workbook; use export_plc_tags")
    assert_export_target(rows, key)
    prepared = _prepare(
        rows,
        prefix=prefix,
        name_from=name_from,
        logix_slot=logix_slot,
        analog_start=None if analog_start is None else _parse_analog_start(analog_start),
    )
    return _render_prepared(prepared, target=key, logix_style=logix_style)


def _render_prepared(points: list[_Point], *, target: str, logix_style: str) -> str:
    if target == "studio5000":
        return _studio5000(points, logix_style=logix_style)
    if target == "tia_csv":
        return _tia_generic(points)
    if target == "domore":
        return _domore(points)
    if target == "keyence":
        return _keyence_generic(points)
    if target == "sysmac":
        return _sysmac_generic(points)
    if target == "cxprogrammer":
        return _cxprogrammer(points)
    raise ValueError(f"unknown PLC tag target '{target}'")


def _format_name(target: str) -> str:
    return {
        "studio5000": "studio5000-csv",
        "tia": "tia-xlsx",
        "tia_csv": "tia-generic-csv",
        "domore": "domore-element-documentation",
        "keyence": "keyence-generic-csv",
        "sysmac": "sysmac-generic-csv",
        "cxprogrammer": "cxprogrammer-symbol-txt",
    }[target]


def _doc_url(target: str) -> str:
    return {
        "studio5000": STUDIO5000_DOC,
        "tia": TIA_XLSX_DOC,
        "tia_csv": TIA_DOC,
        "domore": DOMORE_DOC,
        "keyence": KEYENCE_SPECS,
        "sysmac": SYSMAC_DOC,
        "cxprogrammer": CXPROGRAMMER_DOC,
    }[target]


def _format_note(target: str) -> str:
    if target == "tia":
        return (
            "TIA Portal Excel tag table. Sheets PLCTags and TagTableProperties. "
            "English columns from Format of the export file (*.xlsx). "
            "Custom property TIA_Version is 1.0."
        )
    if target == "tia_csv":
        return (
            "Generic CSV. The TIA import file is the .xlsx target. "
            "This text file is not a TIA import."
        )
    if target == "studio5000":
        return "Logix Designer CSV tag import. Alias rows target Local:slot:I.Pt00.Data."
    if target == "keyence":
        return (
            "Generic CSV. KV STUDIO reads a CSV (MBCS) or TXT (Unicode) device-comment "
            "file, and that manual section does not list the columns. "
            "This text file is not a KV STUDIO import. "
            "Device numbers use the Unit Editor default: relays from R30000 in "
            "10-channel blocks, DM from DM10000 in 100-word blocks."
        )
    if target == "sysmac":
        return (
            "Generic CSV. Sysmac Studio does not publish a global-variable import "
            "file with AT columns. This text file is not a Sysmac Studio import. "
            "cio_address is the CJ/CP symbol address (0.00, 100.00, CIO word)."
        )
    if target == "cxprogrammer":
        return (
            "CX-Programmer symbol-table paste. Four tab-separated columns and no "
            "header: symbol name, data type, address, comment. "
            "Inputs start at 0.00, outputs at 100.00. A combo analog module is "
            "one CJ1W-MAD42 unit: output 1 is n+1, input 1 is n+5."
        )
    return "Do-more Designer element documentation import (element, nickname, extra info, description)."


_TARGET_STYLES = {
    "domore": frozenset({"domore"}),
    "studio5000": frozenset({"logix"}),
    "tia": frozenset({"tia"}),
    "tia_csv": frozenset({"tia"}),
}


def assert_export_target(rows: list[dict[str, str]], target: str) -> None:
    """Refuse a tag file the project's I/O modules do not belong to.

    CLICK and Micro800 have no verified import file in this tree, so they are
    not written as Do-more or Studio 5000 tags. The sheet address stays on the
    I/O list.
    """

    styles = sorted({str(row.get("address_style") or "") for row in rows if str(row.get("address_style") or "")})
    if not styles:
        return
    allowed = _TARGET_STYLES.get(target, frozenset())
    bad = [style for style in styles if style not in allowed]
    if not bad:
        return
    names = ", ".join(bad)
    raise ValueError(
        f"target {target} does not match I/O address style {names}. "
        "Export only the platform those modules belong to. "
        "CLICK and Micro800 have no verified tag-import format here, so they are refused "
        "rather than rewritten as another PLC."
    )


def _local_index(address: str, io_type: str) -> tuple[int | None, tuple[str, int] | None]:
    """(bit or channel index, native Do-more element) parsed from a pin id."""

    text = str(address or "").strip()
    native = _NATIVE.match(text)
    if native:
        kind = native.group(1).upper()
        number = int(native.group(2))
        expect = {"DI": "X", "DO": "Y", "AI": "WX", "AO": "WY"}.get(io_type)
        if kind == expect:
            return number, (kind, number)
    byte_bit = _BYTE_BIT.match(text)
    if byte_bit:
        return int(byte_bit.group(2)) * 8 + int(byte_bit.group(3)), None
    channel = _CHANNEL.match(text)
    if channel:
        return int(channel.group(2)), None
    return None, None


def _align(value: int, step: int) -> int:
    if step <= 1:
        return value
    rem = value % step
    return value if rem == 0 else value + (step - rem)


class _Point:
    def __init__(self, row: dict[str, str], ordinal: int) -> None:
        self.row = row
        self.key = (row.get("module") or "", row.get("address") or "", str(ordinal))
        self.address_style = str(row.get("address_style") or "")
        self.sheet_address = str(row.get("sheet_address") or "").strip()
        probe = row.get("address") or ""
        if self.address_style == "domore" and self.sheet_address:
            probe = self.sheet_address
        index, native = _local_index(probe, row.get("type") or "")
        self.local = ordinal if index is None else index
        self.native = native
        self.global_index = self.local
        self.element = ""
        self.logix = ""
        self.tia = ""
        self.omron = ""
        self.omron_verified = True
        self.keyence = ""
        self.slot = 1
        self.tag = row.get("tag") or "Tag"


def _prepare(
    rows: list[dict[str, str]],
    *,
    prefix: str,
    name_from: str,
    logix_slot: int,
    analog_start: int | None = None,
) -> list[_Point]:
    points: list[_Point] = []
    ordinals: dict[tuple[str, str], int] = {}
    for row in rows:
        kind = str(row.get("type") or "")
        module = str(row.get("module") or "")
        n = ordinals.get((module, kind), 0)
        ordinals[(module, kind)] = n + 1
        points.append(_Point(row, n))
    _assign_slots(points, logix_slot)
    _assign_image(points, analog_start)
    _assign_names(points, prefix=prefix, name_from=name_from)
    return points


def _assign_slots(points: list[_Point], start: int) -> None:
    slot_of: dict[str, int] = {}
    nxt = start
    for point in points:
        module = point.row.get("module") or ""
        if module in slot_of:
            point.slot = slot_of[module]
            continue
        explicit = str(point.row.get("slot") or "").strip()
        if explicit:
            slot = int(explicit)
        else:
            slot = nxt
            nxt += 1
        slot_of[module] = slot
        nxt = max(nxt, slot + 1)
        point.slot = slot


def _assign_image(points: list[_Point], analog_start: int | None) -> None:
    """Do-more X/Y and WX/WY are separate blocks. TIA I and IW share one image."""

    used: dict[str, set[int]] = {"X": set(), "Y": set(), "WX": set(), "WY": set()}
    kind_of = {"DI": "X", "DO": "Y", "AI": "WX", "AO": "WY"}
    for point in points:
        if point.native is None:
            continue
        kind, number = point.native
        used[kind].add(number)
        point.global_index = number
    groups: dict[tuple[str, str], list[_Point]] = {}
    order: list[tuple[str, str]] = []
    for point in points:
        if point.native is not None:
            continue
        key = (point.row.get("module") or "", kind_of[point.row.get("type") or "DI"])
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(point)
    for module, kind in order:
        items = groups[(module, kind)]
        step = 1 if kind in {"WX", "WY"} else 8
        taken = used[kind]
        base = _align(max(taken) + 1 if taken else 0, step)
        while any(base + item.local in taken for item in items):
            base += step
        for item in items:
            number = base + item.local
            while number in taken:
                number += 1
            taken.add(number)
            item.global_index = number
    for point in points:
        kind = kind_of[point.row.get("type") or "DI"]
        io_type = point.row.get("type") or "DI"
        point.element = f"{kind}{point.global_index}"
        point.logix = _logix_address(io_type, point.slot, point.local)
    _assign_tia(points, analog_start)
    for point in points:
        # I/O-module sheets already stored the address. Do not re-slot or re-base it.
        if not point.sheet_address:
            continue
        if point.address_style == "domore":
            point.element = point.sheet_address
        elif point.address_style == "logix":
            point.logix = point.sheet_address
        elif point.address_style == "tia":
            point.tia = point.sheet_address
    _assign_omron(points)
    _assign_keyence(points)


def _even_byte(value: int) -> int:
    value = max(0, int(value))
    return value if value % 2 == 0 else value + 1


_START_ADDRESS = re.compile(r"^(?:%[IQ]W|IW|QW|%W|W)?(\d+)$", re.IGNORECASE)


def _row_start_address(row: dict[str, str]) -> int | None:
    text = str(row.get("start_address") or "").strip()
    if not text:
        return None
    match = _START_ADDRESS.fullmatch(text)
    if match is None:
        return _parse_analog_start(text)
    return int(match.group(1))


def _tia_channel(item: _Point, items: list[_Point]) -> int:
    """Channel within the module. A native WX/WY number stays a Do-more address."""

    if item.native is not None:
        return items.index(item)
    return item.local


def _analog_words(items: list[_Point], base: int) -> list[int]:
    return [_even_byte(base) + _tia_channel(item, items) * 2 for item in items]


def _words_overlap(words: list[int], taken: set[int]) -> bool:
    for word in words:
        if word in taken or (word + 1) in taken:
            return True
    return False


def _assign_tia(points: list[_Point], analog_start: int | None) -> None:
    """Map TIA addresses so %IWn never covers a byte that %Ib.x already uses."""

    occupied: dict[str, set[int]] = {"AI": set(), "AO": set()}
    for point in points:
        io_type = point.row.get("type") or "DI"
        if io_type == "DI":
            occupied["AI"].add(point.global_index // 8)
            point.tia = _tia_address("DI", point.global_index)
        elif io_type == "DO":
            occupied["AO"].add(point.global_index // 8)
            point.tia = _tia_address("DO", point.global_index)
    groups: dict[tuple[str, str], list[_Point]] = {}
    order: list[tuple[str, str]] = []
    for point in points:
        io_type = point.row.get("type") or ""
        if io_type not in {"AI", "AO"}:
            continue
        key = (point.row.get("module") or "", io_type)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(point)
    for _module, io_type in order:
        items = groups[(_module, io_type)]
        taken = occupied[io_type]
        explicit = _row_start_address(items[0].row)
        if explicit is not None:
            base = _even_byte(explicit)
        elif analog_start is not None:
            base = _even_byte(analog_start)
        elif taken:
            base = _even_byte(max(taken) + 1)
        else:
            base = 0
        while _words_overlap(_analog_words(items, base), taken):
            base += 2
        for item, word in zip(items, _analog_words(items, base), strict=True):
            while word in taken or (word + 1) in taken:
                word += 2
            taken.add(word)
            taken.add(word + 1)
            item.tia = _tia_address(io_type, word)


_TIA_BIT = re.compile(r"^%([IQ])(\d+)\.(\d+)$", re.IGNORECASE)
_TIA_BYTE = re.compile(r"^%([IQ])B(\d+)$", re.IGNORECASE)
_TIA_WORD = re.compile(r"^%([IQ])W(\d+)$", re.IGNORECASE)
_TIA_DWORD = re.compile(r"^%([IQ])D(\d+)$", re.IGNORECASE)


def tia_occupied_bytes(address: str) -> set[tuple[str, int]]:
    """Bytes one TIA logical address touches. I and Q are separate images.

    %I0.0 touches byte 0. %IW0 is IB0+IB1. %IW2 is bytes 2 and 3. The number
    in %IWn is the byte the word starts on. Bits in one byte touch that byte
    without taking it away from each other; use ``tia_byte_collisions`` for
    a word covering a bit, or two claims on the same bit.
    """

    text = str(address or "").strip()
    bit = _TIA_BIT.fullmatch(text)
    if bit:
        return {(bit.group(1).upper(), int(bit.group(2)))}
    byte = _TIA_BYTE.fullmatch(text)
    if byte:
        return {(byte.group(1).upper(), int(byte.group(2)))}
    word = _TIA_WORD.fullmatch(text)
    if word:
        start = int(word.group(2))
        image = word.group(1).upper()
        return {(image, start), (image, start + 1)}
    dword = _TIA_DWORD.fullmatch(text)
    if dword:
        start = int(dword.group(2))
        image = dword.group(1).upper()
        return {(image, start + offset) for offset in range(4)}
    return set()


def _tia_claim(address: str) -> tuple[str, set[int], set[tuple[int, int]]] | None:
    """(image, whole bytes, bits as (byte, bit)). None when the text is not a TIA address."""

    text = str(address or "").strip()
    bit = _TIA_BIT.fullmatch(text)
    if bit:
        return bit.group(1).upper(), set(), {(int(bit.group(2)), int(bit.group(3)))}
    byte = _TIA_BYTE.fullmatch(text)
    if byte:
        return byte.group(1).upper(), {int(byte.group(2))}, set()
    word = _TIA_WORD.fullmatch(text)
    if word:
        start = int(word.group(2))
        return word.group(1).upper(), {start, start + 1}, set()
    dword = _TIA_DWORD.fullmatch(text)
    if dword:
        start = int(dword.group(2))
        return dword.group(1).upper(), {start + offset for offset in range(4)}, set()
    return None


def tia_byte_collisions(addresses: list[str]) -> list[tuple[str, str, str, int]]:
    """Addresses that cover the same bit, or a word/byte that covers a bit.

    %I0.0 and %I0.1 share byte 0 and do not collide. %IW0 collides with both
    because that word is IB0+IB1.
    """

    whole: dict[tuple[str, int], str] = {}
    bits: dict[tuple[str, int, int], str] = {}
    hits: list[tuple[str, str, str, int]] = []
    for index, address in enumerate(addresses):
        claim = _tia_claim(address)
        if claim is None:
            continue
        token = f"{index}:{address}"
        image, byte_set, bit_set = claim
        for number in byte_set:
            key = (image, number)
            previous = whole.get(key)
            if previous is not None:
                hits.append((previous.split(":", 1)[1], address, image, number))
            else:
                whole[key] = token
            for (owner_image, owner_byte, _owner_bit), owner in bits.items():
                if owner_image == image and owner_byte == number:
                    hits.append((owner.split(":", 1)[1], address, image, number))
        for number, bit in bit_set:
            owner = whole.get((image, number))
            if owner is not None:
                hits.append((owner.split(":", 1)[1], address, image, number))
            key_b = (image, number, bit)
            previous = bits.get(key_b)
            if previous is not None:
                hits.append((previous.split(":", 1)[1], address, image, number))
            else:
                bits[key_b] = token
    return hits


def _cio_bit(origin_word: int, bit_index: int) -> str:
    """CIO bit as word.bit with two bit digits. 0.00 is bit 0 of word 0."""

    word = origin_word + bit_index // 16
    return f"{word}.{bit_index % 16:02d}"


# CJ1W-MAD42 normal mode, W345 section 9-5-6 page 398.
# Offsets from n: output 1, output 2, then input 1 through input 4.
_MAD42_OUT = (1, 2)
_MAD42_IN = (5, 6, 7, 8)
# Unit 95 occupies CIO 2950 to CIO 2959. W345 section 9-5-2.
_CIO_SPECIAL_LAST = 2950


def _assign_omron(points: list[_Point]) -> None:
    """CIO bits from W446. One Special I/O unit per analog module.

    A module with both AI and AO uses the CJ1W-MAD42 map, so input 1 and
    output 1 share that unit. A module with only one direction uses n+1 for
    its first channel (AD or DA). A channel the cited table does not list is
    left as the address "unverified".
    """

    occupied: set[int] = set()
    for point in points:
        io_type = point.row.get("type") or "DI"
        if io_type == "DI":
            point.omron = _cio_bit(0, point.global_index)
            occupied.add(int(point.omron.split(".", 1)[0]))
        elif io_type == "DO":
            point.omron = _cio_bit(100, point.global_index)
            occupied.add(int(point.omron.split(".", 1)[0]))

    modules: list[str] = []
    for point in points:
        if (point.row.get("type") or "") not in {"AI", "AO"}:
            continue
        module = point.row.get("module") or ""
        if module not in modules:
            modules.append(module)

    unit = 0
    for module in modules:
        items = [
            point
            for point in points
            if (point.row.get("module") or "") == module and (point.row.get("type") or "") in {"AI", "AO"}
        ]
        ais = [point for point in items if (point.row.get("type") or "") == "AI"]
        aos = [point for point in items if (point.row.get("type") or "") == "AO"]
        combo = bool(ais) and bool(aos)
        explicit = None
        for point in items:
            found = _row_start_address(point.row)
            if found is not None:
                explicit = found
                break
        base = 2000 + unit * 10 if explicit is None else explicit

        def offset(point: _Point) -> int | None:
            same = ais if (point.row.get("type") or "") == "AI" else aos
            channel = _tia_channel(point, same)
            if combo:
                table = _MAD42_IN if (point.row.get("type") or "") == "AI" else _MAD42_OUT
                if 0 <= channel < len(table):
                    return table[channel]
                return None
            # AD081-V1 inputs 1–8 and DA08V/08C outputs 1–8 are n+1 through n+8.
            if 0 <= channel <= 7:
                return 1 + channel
            return None

        placed = False
        while base <= _CIO_SPECIAL_LAST:
            planned = [base + off for point in items if (off := offset(point)) is not None]
            if not any(word in occupied for word in planned):
                placed = True
                break
            base += 10
        used: set[int] = set()
        for point in items:
            off = offset(point) if placed else None
            word = None if off is None else base + off
            if word is None or word in used or word in occupied:
                point.omron = "unverified"
                point.omron_verified = False
                continue
            used.add(word)
            occupied.add(word)
            point.omron = str(word)
            point.omron_verified = True
        unit += 1


_OMRON_BIT = re.compile(r"^(\d+)\.(\d{2})$")
_OMRON_WORD = re.compile(r"^(\d+)$")


def omron_address_collisions(addresses: list[str]) -> list[tuple[str, str, int]]:
    """CIO addresses that claim the same bit, or a word that covers a bit.

    0.00 and 0.01 share word 0 and do not collide. Word 2001 collides with
    2001.00. Inputs (words from 0) and outputs (words from 100) are one CIO area.
    """

    bits: dict[tuple[int, int], str] = {}
    words: dict[int, str] = {}
    hits: list[tuple[str, str, int]] = []
    for index, address in enumerate(addresses):
        token = f"{index}:{address}"
        bit = _OMRON_BIT.fullmatch(str(address or "").strip())
        if bit:
            word = int(bit.group(1))
            number = int(bit.group(2))
            owner = words.get(word)
            if owner is not None:
                hits.append((owner.split(":", 1)[1], address, word))
            key = (word, number)
            previous = bits.get(key)
            if previous is not None:
                hits.append((previous.split(":", 1)[1], address, word))
            else:
                bits[key] = token
            continue
        word_match = _OMRON_WORD.fullmatch(str(address or "").strip())
        if word_match is None:
            continue
        word = int(word_match.group(1))
        previous = words.get(word)
        if previous is not None:
            hits.append((previous.split(":", 1)[1], address, word))
        else:
            words[word] = token
        for (owner_word, _owner_bit), owner in bits.items():
            if owner_word == word:
                hits.append((owner.split(":", 1)[1], address, word))
    return hits


# KV STUDIO default: 10 channels from relay 30000, 100 DM words from DM10000.
_KV_RELAY0 = 30000
_KV_CHANNEL_RELAYS = 100
_KV_RELAY_BLOCK = 10 * _KV_CHANNEL_RELAYS
_KV_DM0 = 10000
_KV_DM_BLOCK = 100


def _assign_keyence(points: list[_Point]) -> None:
    """One Unit Editor block per module, in drawing order."""

    modules: list[str] = []
    for point in points:
        module = point.row.get("module") or ""
        if module not in modules:
            modules.append(module)
    relay_base = _KV_RELAY0
    dm_base = _KV_DM0
    for module in modules:
        items = [point for point in points if (point.row.get("module") or "") == module]
        discrete = sorted(
            (point for point in items if (point.row.get("type") or "") in {"DI", "DO"}),
            key=lambda point: (0 if point.row.get("type") == "DI" else 1, point.local, point.key),
        )
        analog = sorted(
            (point for point in items if (point.row.get("type") or "") in {"AI", "AO"}),
            key=lambda point: (0 if point.row.get("type") == "AI" else 1, point.local, point.key),
        )
        for offset, point in enumerate(discrete):
            point.keyence = f"R{relay_base + offset}"
        for offset, point in enumerate(analog):
            point.keyence = f"DM{dm_base + offset}"
        relay_span = max(_KV_RELAY_BLOCK, ((len(discrete) + _KV_RELAY_BLOCK - 1) // _KV_RELAY_BLOCK) * _KV_RELAY_BLOCK)
        dm_span = max(_KV_DM_BLOCK, ((max(len(analog), 1) + _KV_DM_BLOCK - 1) // _KV_DM_BLOCK) * _KV_DM_BLOCK)
        if len(analog) == 0:
            dm_span = _KV_DM_BLOCK
        relay_base += relay_span
        dm_base += dm_span


_KV_RELAY = re.compile(r"^R(\d+)$", re.IGNORECASE)
_KV_DM = re.compile(r"^DM(\d+)$", re.IGNORECASE)


def keyence_address_collisions(addresses: list[str]) -> list[tuple[str, str]]:
    """Duplicate R relays or duplicate DM words. R and DM are different devices."""

    seen: dict[str, str] = {}
    hits: list[tuple[str, str]] = []
    for index, address in enumerate(addresses):
        text = str(address or "").strip().upper()
        if _KV_RELAY.fullmatch(text) is None and _KV_DM.fullmatch(text) is None:
            continue
        token = f"{index}:{text}"
        previous = seen.get(text)
        if previous is not None:
            hits.append((previous.split(":", 1)[1], text))
        else:
            seen[text] = token
    return hits


def _tia_address(io_type: str, number: int) -> str:
    if io_type == "DI":
        return f"%I{number // 8}.{number % 8}"
    if io_type == "DO":
        return f"%Q{number // 8}.{number % 8}"
    if io_type == "AI":
        return f"%IW{number}"
    return f"%QW{number}"


def _logix_address(io_type: str, slot: int, local: int) -> str:
    """Compact 5000 tag member.

    Digital: 5069-UM004H-EN-P March 2026 pp. 121, 126, 129 (Ptxx.Data).
    Analog: 5069-UM005F-EN-P December 2024 p. 139 (Chxx.Data).
    """

    point = f"{int(local):02d}"
    if io_type == "DI":
        return f"Local:{slot}:I.Pt{point}.Data"
    if io_type == "DO":
        return f"Local:{slot}:O.Pt{point}.Data"
    if io_type == "AI":
        return f"Local:{slot}:I.Ch{point}.Data"
    return f"Local:{slot}:O.Ch{point}.Data"


def _assign_names(points: list[_Point], *, prefix: str, name_from: str) -> None:
    limits = {"studio": 40, "domore": 16, "tia": 80}
    # Names are chosen once, then clipped per target at write time. The stored
    # tag is the Logix-length name; Do-more clips again so a long description
    # still fits a nickname.
    used: set[str] = set()
    reserved = {point.element.casefold() for point in points}
    for point in points:
        raw = _raw_name(point, name_from=name_from)
        named = _iec_name(_join_prefix(prefix, raw), limit=limits["studio"])
        point.tag = _unique(named, used, limits["studio"], reserved)


def _raw_name(point: _Point, *, name_from: str) -> str:
    if name_from == "description":
        text = str(point.row.get("description") or "").strip()
        if text and text.lower() != "spare":
            return text
    tag = str(point.row.get("tag") or "").strip()
    if tag:
        return tag
    device = str(point.row.get("device") or "").strip()
    if device:
        return device
    return f"Spare_{point.row.get('address') or 'IO'}"


def _join_prefix(prefix: str, name: str) -> str:
    pre = prefix.strip()
    if not pre:
        return name
    if pre.endswith("_"):
        return pre + name
    return f"{pre}_{name}"


def _iec_name(raw: str, *, limit: int) -> str:
    text = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        text = "Tag"
    if text[0].isdigit():
        text = f"T_{text}"
    if len(text) > limit:
        text = text[:limit].strip("_")
    return text or "Tag"


def _unique(name: str, used: set[str], limit: int, reserved: set[str]) -> str:
    candidate = name
    n = 1
    while candidate.casefold() in used or candidate.casefold() in reserved or _ELEMENT_ID.match(candidate):
        n += 1
        suffix = f"_{n}"
        candidate = (name[: max(1, limit - len(suffix))].strip("_") or "Tag") + suffix
        if len(candidate) > limit:
            candidate = candidate[:limit].strip("_") or "Tag"
    used.add(candidate.casefold())
    reserved.add(candidate.casefold())
    return candidate


def _clip_nickname(name: str, element: str, used: set[str]) -> str:
    limit = 16
    candidate = _iec_name(name, limit=limit)
    n = 1
    while (
        candidate.casefold() in used
        or candidate.casefold() == element.casefold()
        or _ELEMENT_ID.match(candidate)
    ):
        n += 1
        suffix = f"_{n}"
        candidate = (_iec_name(name, limit=limit)[: max(1, limit - len(suffix))].strip("_") or "Tag") + suffix
        candidate = candidate[:limit].strip("_") or "Tag"
    used.add(candidate.casefold())
    return candidate


def _studio_description(text: str) -> str:
    cleaned = text.replace('"', "'").replace("$", " ").replace("=", " ")
    cleaned = cleaned.strip()
    if cleaned[:1] in {"+", "-"}:
        cleaned = f" {cleaned}"
    return cleaned


def _csv_line(fields: list[str]) -> str:
    buf = StringIO()
    csv.writer(buf, lineterminator="").writerow(fields)
    return buf.getvalue()


# Studio 5000 CSV tag import.
# File-format page: "The first line in the file contains a version number",
# then "Any line whose first column is Remark is ignored". Schema line starts
# with Type. Case is Remark, as that page spells it.
def _studio5000(points: list[_Point], *, logix_style: str) -> str:
    lines = [
        "0.3",
        'Remark,"CSV-Import-Export"',
        "TYPE,SCOPE,NAME,DESCRIPTION,DATATYPE,SPECIFIER,ATTRIBUTES",
    ]
    for point in points:
        description = _studio_description(str(point.row.get("description") or ""))
        if logix_style == "base":
            datatype = "BOOL" if point.row.get("type") in {"DI", "DO"} else "REAL"
            fields = ["TAG", "", point.tag, description, datatype, "", ""]
        else:
            fields = ["ALIAS", "", point.tag, description, "", point.logix, ""]
        lines.append(_csv_line(fields))
    return "\n".join(lines) + "\n"


def _write_tia_xlsx(points: list[_Point], dest: Path) -> None:
    """Workbook from "Format of the export file (*.xlsx)". See the module docstring."""

    from openpyxl import Workbook
    from openpyxl.packaging.custom import StringProperty

    book = Workbook()
    tags = book.active
    tags.title = TIA_SHEET_TAGS
    tags.append(list(TIA_PLC_COLUMNS))
    for point in points:
        io_type = point.row.get("type") or "DI"
        datatype = "Bool" if io_type in {"DI", "DO"} else "Int"
        # TRUE/FALSE are the values that page expects. Writeable is TRUE on outputs.
        writable = "TRUE" if io_type in {"DO", "AO"} else "FALSE"
        tags.append(
            [
                point.tag,
                TIA_TABLE_PATH,
                datatype,
                point.tia,
                str(point.row.get("description") or ""),
                "TRUE",
                "TRUE",
                writable,
                None,
                None,
            ]
        )
    props = book.create_sheet(TIA_SHEET_PROPS)
    props.append(list(TIA_PROP_COLUMNS))
    props.append([TIA_TABLE_PATH, None, None])
    book.custom_doc_props.append(StringProperty(name="TIA_Version", value="1.0"))
    book.save(dest)


# Generic CSV. Not the TIA .xlsx tag table. The verified workbook is target tia.
def _tia_generic(points: list[_Point]) -> str:
    lines = [
        "# PanelCAD generic PLC tag CSV (target: tia_csv). Not a TIA Portal import file.",
        "# The TIA import file is target tia: sheets PLCTags and TagTableProperties.",
        f"# {TIA_XLSX_DOC}",
        "# These columns are PanelCAD's text export, not the English xlsx titles.",
        f"# Logical addresses use international mnemonics (%I / %Q). {TIA_CAX_DOC}",
        "name,data_type,logical_address,comment",
    ]
    for point in points:
        datatype = "Bool" if point.row.get("type") in {"DI", "DO"} else "Int"
        lines.append(
            _csv_line([point.tag, datatype, point.tia, str(point.row.get("description") or "")])
        )
    return "\n".join(lines) + "\n"


# Do-more Designer element documentation import.
# No header. "Element","Nickname","Extra Info","Description" per the help URL
# at the top of this module. Nicknames are clipped to 16 characters.
def _domore(points: list[_Point]) -> str:
    used: set[str] = set()
    lines: list[str] = []
    for point in points:
        nickname = _clip_nickname(point.tag, point.element, used)
        extra = str(point.row.get("wire") or "").strip()[:16]
        description = re.sub(r"\s+", " ", str(point.row.get("description") or "").replace(",", " ")).strip()
        description = description[:144]
        buf = StringIO()
        csv.writer(buf, lineterminator="", quoting=csv.QUOTE_ALL).writerow(
            [point.element, nickname, extra, description]
        )
        lines.append(buf.getvalue())
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _keyence_generic(points: list[_Point]) -> str:
    lines = [
        "# PanelCAD generic PLC tag CSV (target: keyence). Not a KV STUDIO import file.",
        "# KV STUDIO Ver.11 User's Manual, File - Read device comments in CSV/TXT format:",
        '# "Read comments saved in a CSV format file (MBCS) (*.csv) or TXT format file (Unicode) (*.txt)."',
        "# That section does not list columns, a header, or a delimiter.",
        f"# Manuals index (the PDF is behind a Keyence account): {KEYENCE_MANUALS}",
        "# Device values follow Automatically Assigning Relays and DM Entries:",
        '# "relays are assigned in 10-channel units from the leading number of 30000"',
        '# "DM entries are assigned in 100-entry units from the leading number of 10000."',
        "# R10000 is channel 100. These columns are PanelCAD's, not KV STUDIO's.",
        f"# {KEYENCE_SPECS}",
        "name,device,type,comment",
    ]
    for point in points:
        lines.append(
            _csv_line(
                [
                    point.tag,
                    point.keyence,
                    str(point.row.get("type") or ""),
                    str(point.row.get("description") or ""),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _sysmac_clip(text: str, reserve: int = 0) -> str:
    room = 127 - reserve
    raw = text.encode("utf-8")
    if len(raw) <= max(room, 1):
        return text or "Tag"
    clipped = raw[: max(1, room)].decode("utf-8", errors="ignore").rstrip("_")
    return clipped or "Tag"


def _sysmac_ok(text: str) -> bool:
    if not text or text[0].isdigit() or text.startswith(("_", "P_")) or text.endswith("_"):
        return False
    if "__" in text:
        return False
    return len(text.encode("utf-8")) <= 127


def _sysmac_name(name: str, used: set[str]) -> str:
    """W501 6-3-12. Keep separate underscores. Drop __, a leading or trailing _, and a P_ prefix."""

    text = re.sub(r"[^A-Za-z0-9_]", "_", str(name or ""))
    text = re.sub(r"_+", "_", text).strip("_")
    if text.startswith("P_"):
        text = text[2:].strip("_")
    if not text or text[0].isdigit():
        text = f"T{text}" if text else "Tag"
    text = _sysmac_clip(text).rstrip("_") or "Tag"
    candidate = text
    n = 1
    while candidate.casefold() in used or not _sysmac_ok(candidate):
        n += 1
        suffix = str(n)
        stem = _sysmac_clip(text, reserve=len(suffix.encode("utf-8"))).rstrip("_") or "Tag"
        candidate = stem + suffix
    used.add(candidate.casefold())
    return candidate


def _omron_comment(point: _Point) -> str:
    text = str(point.row.get("description") or "")
    if (point.row.get("type") or "") in {"AI", "AO"} and not point.omron_verified:
        return f"unverified {text}".strip()
    return text


def _sysmac_generic(points: list[_Point]) -> str:
    lines = [
        "# PanelCAD generic PLC tag CSV (target: sysmac). Not a Sysmac Studio import file.",
        "# W504 creates global variables in the variable table and device variables in the I/O Map.",
        "# It does not list columns for a global-variable file with AT specifications.",
        "# CJ-series memory AT in that manual is words only (%0 to %6143, %D0 to %D32767), not bits.",
        f"# {SYSMAC_DOC}",
        "# cio_address is the CX-Programmer CIO address (W446 / W345), recorded by PanelCAD.",
        "# A combo AI/AO module is one CJ1W-MAD42 unit: n+1 is output 1, n+5 is input 1.",
        "# cio_address unverified means that channel is outside the cited W345 table.",
        "# Names follow W501 6-3-12, Characters that cannot be used together:",
        "# no leading underscore, no trailing underscore, no consecutive underscores (__), no P_ prefix.",
        "# Separate underscores stay, so M1_RUN_FB is unchanged. Not case sensitive. 127 bytes.",
        f"# {SYSMAC_NAMES_DOC}",
        "name,data_type,cio_address,comment",
    ]
    used: set[str] = set()
    for point in points:
        datatype = "BOOL" if point.row.get("type") in {"DI", "DO"} else "UINT"
        lines.append(
            _csv_line(
                [
                    _sysmac_name(point.tag, used),
                    datatype,
                    point.omron,
                    _omron_comment(point),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _cx_field(text: str) -> str:
    return str(text or "").replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _cxprogrammer(points: list[_Point]) -> str:
    """Headerless tabbed text. A comment line would be pasted as a symbol."""

    lines: list[str] = []
    for point in points:
        datatype = "BOOL" if point.row.get("type") in {"DI", "DO"} else "UINT"
        lines.append(
            "\t".join(
                [
                    _cx_field(point.tag),
                    datatype,
                    point.omron,
                    _cx_field(_omron_comment(point)),
                ]
            )
        )
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
