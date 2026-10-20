"""Wire-marker lists. One printed marker per wire end.

Each from-to conductor prints its wire number twice, once per end. Two
conductors that land on the same pin print two markers there. A drawing
junction is not an end (the from-to row already skipped it). Numbers are the
ones already on the wires.

Vendor files
------------
Phoenix Contact PROJECT complete / CLIP PROJECT (and the MARKING system that
replaced them) document a data-exchange import. The marking guide says the
marker text goes in a single column. No header name is published.

* https://assets.phoenixcontact.com/file/3706e4b1-1fb7-47fd-aedc-d04ae95cf8cb/media/original?Project_Complete_Marking_guide_Final_2020.pdf
* https://assets.phoenixcontact.com/file/3a3ca93e-cae1-45d4-8444-7e49ecedddd0/media/original?US_Project-Complete-Marking-Guide-updates_20241017_U007204A__1_.pdf
* https://assets.phoenixcontact.com/file/f45d95ab-b61d-43c1-a2ee-bce42edd215e/media/original
* https://www.phoenixcontact.com/en-gb/products/software-marking-system-software-1669297

Weidmüller M-Print PRO's user manual describes an import wizard: the user
picks the delimiter and maps columns. No wire-marker header is published, so
``weidmueller`` is the PanelCAD generic CSV.

* https://assets.dam.weidmueller.com/assets/api/e0a21ebb-a3e5-4a2c-8114-c954d403b63f/Original/User_Manual_EN.pdf
* https://www.weidmueller.com/int/service/engineering_data.jsp

Brady Workstation / LabelMark import a CSV whose first row is user-named
fields and whose later rows are labels. No wire-marker column list is
published, so ``brady`` is the PanelCAD generic CSV.

* https://support.bradyid.com/articles/en_US/Knowledge/How-to-format-Excel-files-for-importing-into-Brady-software
* https://support.bradyid.com/articles/en_US/Knowledge/How-Do-I-Import-Data-From-a-CSV-File-into-Brady-Workstation
* https://support.bradyid.com/s/article/Data-Import-in-LabelMark-6
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from panelcad.core.from_to import conductor_records, normalize_sort, write_table
from panelcad.core.models import Project
from panelcad.library.catalog import Catalog

MARKER_COLUMNS: tuple[str, ...] = (
    "text",
    "end",
    "device",
    "pin",
    "sheet",
    "location",
    "size",
    "color",
    "type",
    "style",
)

# Official pages cited above. Kept next to the writer so a format change has
# to name the document it follows.
PHOENIX_DOC_URLS: tuple[str, ...] = (
    "https://assets.phoenixcontact.com/file/3706e4b1-1fb7-47fd-aedc-d04ae95cf8cb/media/original?Project_Complete_Marking_guide_Final_2020.pdf",
    "https://assets.phoenixcontact.com/file/3a3ca93e-cae1-45d4-8444-7e49ecedddd0/media/original?US_Project-Complete-Marking-Guide-updates_20241017_U007204A__1_.pdf",
    "https://assets.phoenixcontact.com/file/f45d95ab-b61d-43c1-a2ee-bce42edd215e/media/original",
    "https://www.phoenixcontact.com/en-gb/products/software-marking-system-software-1669297",
)
WEIDMUELLER_DOC_URLS: tuple[str, ...] = (
    "https://assets.dam.weidmueller.com/assets/api/e0a21ebb-a3e5-4a2c-8114-c954d403b63f/Original/User_Manual_EN.pdf",
    "https://www.weidmueller.com/int/service/engineering_data.jsp",
)
BRADY_DOC_URLS: tuple[str, ...] = (
    "https://support.bradyid.com/articles/en_US/Knowledge/How-to-format-Excel-files-for-importing-into-Brady-software",
    "https://support.bradyid.com/articles/en_US/Knowledge/How-Do-I-Import-Data-From-a-CSV-File-into-Brady-Workstation",
    "https://support.bradyid.com/s/article/Data-Import-in-LabelMark-6",
)

PHOENIX_NOTE = (
    "Phoenix Contact PROJECT complete marking guides (2020 and the 2024 US update), "
    "File → Data Exchange: \"It is important to input the data into a single column "
    "of Excel.\" No header name is published; a header row would be another marker "
    "unless the import skips it. A later brochure says more than one column is "
    "possible, still without naming the columns. This file is one column, no header, "
    "one wire number per row (PROJECT complete / CLIP PROJECT / MARKING system data "
    "exchange). Heat-shrink versus clip-on is the material selected after Material "
    "mapping → Enforce default material, not a column in this file."
)
WEIDMUELLER_NOTE = (
    "Weidmüller M-Print PRO user manual, Importing Files (steps 4/5 and 7): the "
    "wizard asks for a delimiter, then field mappings send source columns onto element fields such "
    "as MARKER or TEXTBOX1 that belong to the marker type already chosen. No fixed "
    "wire-marker CSV header is published (the WMC article;designation;marker file is "
    "a terminal-rail import, not an M-Print PRO wire-marker file). This target is the "
    "PanelCAD generic marker CSV. Map the text column in the wizard, and pick the "
    "heat-shrink or clip-on marker type there."
)
BRADY_NOTE = (
    "Brady Workstation and LabelMark: the first row of a CSV is user-named "
    "field names, each later row is one label, and a .csv file uses a comma "
    "delimiter (Advanced Import / Data Import in LabelMark 6). Brady does not "
    "publish a wire-marker column list. This target is the PanelCAD generic "
    "marker CSV. Map text onto the label, and choose heat-shrink or clip-on "
    "material in the template."
)
GENERIC_NOTE = (
    "PanelCAD generic marker CSV. One row per wire end. text is the wire "
    "number printed on the marker. style is heat-shrink or clip-on."
)

WIRE_MARKER_SCHEMA: dict[str, object] = {
    "op": "export_wire_markers",
    "description": (
        "Print-ready wire markers, one row per conductor end. The count is twice "
        "the from-to rows: two conductors on the same pin (OL1:T1 from M1 and from M2) "
        "print two markers. A junction dot is not an end. style is heat-shrink or clip-on. "
        "target=generic writes the PanelCAD CSV (header: text,end,device,pin,sheet,"
        "location,size,color,type,style). "
        "target=phoenix writes the documented single-column file with no header "
        "(PROJECT complete / CLIP PROJECT / MARKING system data exchange). "
        "target=weidmueller and target=brady write that same generic CSV: "
        "M-Print PRO and Brady Workstation do not publish a fixed wire-marker header. "
        "result.confidence is documented or unverified. result.doc_urls cites the pages."
    ),
    "targets": ["generic", "phoenix", "weidmueller", "brady"],
    "styles": ["heat-shrink", "clip-on"],
    "columns": list(MARKER_COLUMNS),
    "args": {
        "path": "Output path. Required.",
        "target": "generic (default), phoenix, weidmueller, or brady.",
        "style": "heat-shrink (default) or clip-on.",
        "sort": "number (default) or device. Same order as the from-to report.",
    },
    "doc_urls": {
        "phoenix": list(PHOENIX_DOC_URLS),
        "weidmueller": list(WEIDMUELLER_DOC_URLS),
        "brady": list(BRADY_DOC_URLS),
    },
}


def normalize_style(value: str | None) -> str:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
    if token in {"", "heatshrink", "shrink", "sleeve", "hs"}:
        return "heat-shrink"
    if token in {"clipon", "clip", "carrier", "snapon"}:
        return "clip-on"
    raise ValueError(f"unknown marker style '{value}' (heat-shrink or clip-on)")


def normalize_target(value: str | None) -> str:
    token = "".join(ch.lower() for ch in str(value or "generic") if ch.isalnum())
    if token in {"generic", "csv", "panelcad"}:
        return "generic"
    if token in {"phoenix", "clipproject", "marking", "markingsystem", "projectcomplete"}:
        return "phoenix"
    if token in {"weidmueller", "weidmuller", "mprint", "mprintpro"}:
        return "weidmueller"
    if token in {"brady", "workstation", "bradyworkstation", "specialist", "labelmark"}:
        return "brady"
    raise ValueError(f"unknown marker target '{value}' (generic, phoenix, weidmueller, brady)")


def marker_rows(
    project: Project,
    catalog: Catalog | None = None,
    *,
    style: str = "heat-shrink",
    sort: str = "number",
) -> tuple[list[dict[str, str]], int]:
    """Marker dicts and a count of ends skipped because the wire has no number."""

    use_style = normalize_style(style)
    blank = 0
    rows: list[dict[str, str]] = []
    for cond in conductor_records(project, catalog, sort=normalize_sort(sort)):
        number = cond.number.strip()
        for side, end in (("from", cond.frm), ("to", cond.to)):
            if end.passthrough or end.kind == "rail":
                continue
            if not end.device or not end.pin:
                continue
            if not number:
                blank += 1
                continue
            rows.append(
                {
                    "text": number,
                    "end": side,
                    "device": end.device,
                    "pin": end.pin,
                    "sheet": end.sheet_id,
                    "location": end.loc,
                    "size": cond.size,
                    "color": cond.color,
                    "type": cond.wire_type,
                    "style": use_style,
                }
            )
    return rows, blank


def _write_phoenix(path: Path, texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        for text in texts:
            writer.writerow([text])


def _meta(target: str) -> dict[str, Any]:
    if target == "phoenix":
        return {
            "format": "phoenix-single-column",
            "header": False,
            "columns": ["text"],
            "confidence": "documented",
            "note": PHOENIX_NOTE,
            "doc_urls": list(PHOENIX_DOC_URLS),
            "product": "Phoenix Contact PROJECT complete / CLIP PROJECT / MARKING system",
        }
    if target == "weidmueller":
        return {
            "format": "generic-csv",
            "header": True,
            "columns": list(MARKER_COLUMNS),
            "confidence": "unverified",
            "note": WEIDMUELLER_NOTE,
            "doc_urls": list(WEIDMUELLER_DOC_URLS),
            "product": "Weidmüller M-Print PRO",
        }
    if target == "brady":
        return {
            "format": "generic-csv",
            "header": True,
            "columns": list(MARKER_COLUMNS),
            "confidence": "unverified",
            "note": BRADY_NOTE,
            "doc_urls": list(BRADY_DOC_URLS),
            "product": "Brady Workstation / LabelMark",
        }
    return {
        "format": "generic-csv",
        "header": True,
        "columns": list(MARKER_COLUMNS),
        "confidence": "panelcad",
        "note": GENERIC_NOTE,
        "doc_urls": [],
        "product": "PanelCAD generic wire markers",
    }


def export_wire_markers(
    project: Project,
    catalog: Catalog | None,
    path: Path | str,
    *,
    target: str = "generic",
    style: str = "heat-shrink",
    sort: str = "number",
) -> dict[str, Any]:
    """Write one marker file. Weidmüller and Brady receive the generic CSV."""

    use_target = normalize_target(target)
    use_style = normalize_style(style)
    rows, blank = marker_rows(project, catalog, style=use_style, sort=sort)
    dest = Path(path)
    info = _meta(use_target)
    if use_target == "phoenix":
        _write_phoenix(dest, [row["text"] for row in rows])
    else:
        write_table(dest, rows, MARKER_COLUMNS)
    return {
        "path": str(dest),
        "target": use_target,
        "style": use_style,
        "sort": normalize_sort(sort),
        "rows": len(rows),
        "blank_ends": blank,
        "markers": rows,
        **info,
    }
