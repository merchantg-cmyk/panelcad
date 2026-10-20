"""Minimal xlsx read/write. Stdlib only, so a quote does not need another package."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col_letter(index: int) -> str:
    """1-based column index to A, B, … Z, AA."""

    letters = ""
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _xml_text(value: object) -> str:
    text = str(value if value is not None else "")
    cleaned = "".join(ch for ch in text if ch in "\t\n\r" or ord(ch) >= 32)
    return (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class SheetNumber:
    """A numeric spreadsheet cell. Style 2 is hours (0.000). Style 0 is general."""

    def __init__(self, amount: object, style: int = 0) -> None:
        self.amount = amount
        self.style = style

    def text(self) -> str:
        amount = self.amount
        if hasattr(amount, "is_finite"):
            return format(amount, "f")
        return str(amount)


def _is_money(value: object) -> bool:
    return value.__class__.__name__ == "Money"


def _is_sheet_number(value: object) -> bool:
    return value.__class__.__name__ == "SheetNumber"


def _sheet_xml(rows: list[list[object]]) -> str:
    body = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    body.append(f'<worksheet xmlns="{_NS}">')
    if rows:
        widest = max(len(row) for row in rows)
        body.append("<cols>")
        for col in range(1, widest + 1):
            body.append(f'<col min="{col}" max="{col}" width="22" customWidth="1"/>')
        body.append("</cols>")
    body.append("<sheetData>")
    for r_index, row in enumerate(rows, start=1):
        body.append(f'<row r="{r_index}">')
        for c_index, value in enumerate(row, start=1):
            ref = f"{_col_letter(c_index)}{r_index}"
            if isinstance(value, bool) or value is None or value == "":
                body.append(f'<c r="{ref}"/>')
                continue
            if _is_money(value):
                body.append(f'<c r="{ref}" s="1"><v>{value.text()}</v></c>')  # type: ignore[attr-defined]
                continue
            if _is_sheet_number(value):
                style = int(getattr(value, "style", 0) or 0)
                attr = f' s="{style}"' if style else ""
                body.append(f'<c r="{ref}"{attr}><v>{value.text()}</v></c>')  # type: ignore[attr-defined]
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                body.append(f'<c r="{ref}"><v>{value}</v></c>')
                continue
            body.append(f'<c r="{ref}" t="inlineStr"><is><t>{_xml_text(value)}</t></is></c>')
        body.append("</row>")
    body.append("</sheetData></worksheet>")
    return "".join(body)


def write_xlsx(path: Path | str, sheets: dict[str, list[list[object]]]) -> Path:
    """Write a workbook. ``sheets`` keeps insertion order as the sheet order."""

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    names = list(sheets)
    if not names:
        raise ValueError("workbook needs at least one sheet")
    overrides = []
    rels = []
    for index, name in enumerate(names, start=1):
        overrides.append(
            "<Override "
            f'PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        rels.append(
            "<Relationship "
            f'Id="rId{index}" '
            f'Type="{_REL_OFFICE}/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + "".join(overrides)
        + "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL}">'
        f'<Relationship Id="rId1" Type="{_REL_OFFICE}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL}">' + "".join(rels) + "</Relationships>"
    )
    sheet_nodes = []
    for index, name in enumerate(names, start=1):
        sheet_nodes.append(
            f'<sheet name="{_xml_text(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS}" xmlns:r="{_REL_OFFICE}">'
        "<sheets>"
        + "".join(sheet_nodes)
        + "</sheets></workbook>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_NS}">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.000"/></numFmts>'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        "</cellXfs>"
        "</styleSheet>"
    )
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as book:
        book.writestr("[Content_Types].xml", content_types)
        book.writestr("_rels/.rels", root_rels)
        book.writestr("xl/workbook.xml", workbook)
        book.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        book.writestr("xl/styles.xml", styles)
        for index, name in enumerate(names, start=1):
            book.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(sheets[name]))
    return dest


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t", "")
    if kind == "inlineStr":
        texts = [node.text or "" for node in cell.iter() if _local(node.tag) == "t"]
        return "".join(texts)
    value_node = next((node for node in cell if _local(node.tag) == "v"), None)
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    return raw


def _shared_strings(blob: bytes) -> list[str]:
    root = ET.fromstring(blob)
    strings: list[str] = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        strings.append("".join(node.text or "" for node in si.iter() if _local(node.tag) == "t"))
    return strings


def _column_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - 64)
    return index


def _sheet_rows(blob: bytes, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(blob)
    rows: list[list[str]] = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        cells: dict[int, str] = {}
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            ref = cell.attrib.get("r", "")
            index = _column_index(ref) if ref else len(cells) + 1
            cells[index] = _cell_value(cell, shared)
        if cells:
            width = max(cells)
            parsed = [cells.get(i, "") for i in range(1, width + 1)]
        else:
            parsed = []
        raw_r = row.attrib.get("r", "")
        if raw_r.isdigit():
            index = int(raw_r)
            if index > 0:
                while len(rows) < index:
                    rows.append([])
                rows[index - 1] = parsed
                continue
        rows.append(parsed)
    return rows


def read_xlsx(path: Path | str) -> dict[str, list[list[str]]]:
    """First-row headers are the caller's job. Returns sheet name to a grid of strings."""

    src = Path(path)
    with zipfile.ZipFile(src) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            shared = _shared_strings(book.read("xl/sharedStrings.xml"))
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        targets = {}
        for rel in rels:
            if _local(rel.tag) != "Relationship":
                continue
            targets[rel.attrib.get("Id", "")] = rel.attrib.get("Target", "")
        sheets: dict[str, list[list[str]]] = {}
        for node in workbook.iter():
            if _local(node.tag) != "sheet":
                continue
            name = node.attrib.get("name") or "Sheet"
            rel_id = node.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            target = targets.get(rel_id, "")
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = "xl/" + target
            sheets[name] = _sheet_rows(book.read(target), shared)
        return sheets
