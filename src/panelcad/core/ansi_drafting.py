"""Check a drawing against the ANSI / JIC sheet rules this repo already draws.

LETTER is ANSI A and TABLOID is ANSI B. Larger sheets are ANSI_C, ANSI_D, and
ANSI_E. Line weights, rung numbers, wire numbers, and cross-references are the
same values the renderer uses. A value the reader could not see stays ``?``.
"""

from __future__ import annotations

import re
from typing import Any

from panelcad.core.geometry import match_page_size
from panelcad.core.ink import TAG_SIZE
from panelcad.core.models import Project
from panelcad.core.wire_numbering import _CONVENTIONAL
from panelcad.core.zones import DEFAULT_RUNG_PITCH_MM
from panelcad.render.painter import COIL_TAG_MM, Painter
from panelcad.render.sheet_labels import CONTACT_TAG_MM
from panelcad.render.titleblock import (
    BORDER_STROKE,
    RAIL_LABEL_MM,
    RAIL_STROKE,
    RUNG_NUMBER_MM,
    ZONE_LABEL_MM,
)
from panelcad.render.wire_labels import MARK_SIZE
from panelcad.render.xref import XREF_SIZE

# JIC / NFPA 79 prefixes from libraries/builtin/ref_prefixes.yaml.
# Longest first so MTR is not read as M and TSW is not read as T.
JIC_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        {
            "CB",
            "FU",
            "DISC",
            "OL",
            "MCP",
            "MTR",
            "FAN",
            "SOL",
            "PX",
            "VFD",
            "PB",
            "ES",
            "SEL",
            "PL",
            "LS",
            "PRS",
            "FS",
            "LEV",
            "TSW",
            "FSW",
            "FTS",
            "HN",
            "CR",
            "SS",
            "TR",
            "AR",
            "PS",
            "REC",
            "LD",
            "TB",
            "GND",
            "NET",
            "PLC",
            "HMI",
            "M",
            "T",
        },
        key=len,
        reverse=True,
    )
)

_SHEET_LINE = re.compile(r"^[1-9]\d{2,3}$")
_UNREADABLE = "?"

# ANSI name the sheet model uses. A and B keep the names already in PAGE_SIZES_MM.
ANSI_PAGE_NAMES = {
    "ANSI A": "LETTER",
    "ANSI B": "TABLOID",
    "ANSI C": "ANSI_C",
    "ANSI D": "ANSI_D",
    "ANSI E": "ANSI_E",
}


def jic_prefix(tag: str) -> str:
    """JIC prefix on a tag, or empty when the tag is not a JIC device."""

    upper = re.sub(r"[^A-Z0-9]", "", str(tag or "").upper())
    for prefix in JIC_PREFIXES:
        if not upper.startswith(prefix):
            continue
        rest = upper[len(prefix) :]
        if rest and (rest[0].isdigit() or rest[0] == "-"):
            return prefix
    return ""


class _Probe(Painter):
    """Records stroke widths and lettering. Does not draw pixels."""

    def __init__(self) -> None:
        self.strokes: list[float] = []
        self.texts: list[tuple[str, float]] = []
        self._width = 0.35

    def set_stroke(self, color: str, width: float) -> None:
        self._width = float(width)

    def set_fill(self, color: str | None) -> None:
        return

    def rect(self, x: float, y: float, w: float, h: float) -> None:
        self.strokes.append(self._width)

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.strokes.append(self._width)

    def circle(self, x: float, y: float, r: float) -> None:
        self.strokes.append(self._width)

    def polyline(self, points: list[tuple[float, float]]) -> None:
        self.strokes.append(self._width)

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float,
        align: str = "middle",
        rotate: float = 0,
    ) -> None:
        self.texts.append((str(text or ""), float(size)))


def ansi_drafting_issues(project: Project, catalog: Any) -> list[str]:
    """Problems that keep this project off the repo's ANSI / JIC sheet rules.

    An empty list means the drawing follows those rules. IEC projects are
    reported as one issue so a scan of an IEC print is not rewritten.
    """

    standard = str(getattr(project.title_block, "standard", "") or "")
    if standard != "NEMA/ANSI":
        return [f"title block standard is {standard or 'blank'}, not NEMA/ANSI"]

    issues: list[str] = []
    style = str(getattr(project.title_block, "drawing_style", "") or "")
    coil = str(getattr(project.title_block, "coil_style", "") or "")
    if coil not in {"circle", "square"}:
        issues.append(f"coil style {coil or 'blank'} is not circle or square")
    numbering = getattr(project, "wire_numbering", None)
    scheme = str(getattr(numbering, "scheme", "") or "sheet_line")
    if scheme != "sheet_line":
        issues.append(f"wire numbering scheme is {scheme}, not sheet_line")

    from panelcad.core.ladder import sheet_drawing_style
    from panelcad.core.xref import occupied_rung_indices, resolve_settings, xref_annotations, xref_style
    from panelcad.core.zones import nema_line_from_index
    from panelcad.render.contents import draw_sheet_body
    from panelcad.render.titleblock import draw_sheet_frame

    pitch = float(resolve_settings(project).rung_pitch_mm)
    if abs(pitch - DEFAULT_RUNG_PITCH_MM) > 0.05:
        issues.append(f"rung pitch is {pitch} mm, not {DEFAULT_RUNG_PITCH_MM} mm (0.5 in)")

    for index, sheet in enumerate(project.sheets):
        name, orient = match_page_size(*sheet.page.dimensions())
        if name != "custom" and (sheet.page.size.upper() != name or sheet.page.orientation != orient):
            issues.append(
                f"{sheet.id}: page is {sheet.page.size} {sheet.page.orientation}, "
                f"measured sheet is {name} {orient}"
            )
        if name == "custom":
            width, height = sheet.page.dimensions()
            issues.append(f"{sheet.id}: {width:.1f}×{height:.1f} mm is not an ANSI or ISO sheet size")
        if xref_style(project, sheet) != "nema":
            issues.append(f"{sheet.id}: cross-reference style is not NEMA")

        for comp in sheet.components:
            lib = str(comp.library_id or "")
            if any(bit in lib for bit in ("offpage", "connection.node", "needs_info", "notes")):
                continue
            if jic_prefix(comp.id) and not lib.startswith("nema."):
                issues.append(f"{sheet.id}: {comp.id} is a JIC tag on {lib or 'no library part'}")

        if sheet.kind == "schematic":
            drawn_style = sheet_drawing_style(sheet, project)
            for wire in sheet.wires:
                mark = wire.drawn_mark(ladder=drawn_style == "ladder")
                if wire.inferred and not str(mark).startswith(_UNREADABLE):
                    issues.append(f"{sheet.id}: unreadable wire {wire.id} is drawn as {mark!r}")
                elif str(wire.number or "").strip() and not _wire_number_ok(str(wire.number)):
                    issues.append(f"{sheet.id}: wire number {wire.number!r} is not a sheet/line, rail, or ?")

        if sheet.kind not in {"schematic", "panel_layout", "door_layout", "terminal"}:
            continue
        probe = _Probe()
        try:
            draw_sheet_frame(
                probe,
                project,
                sheet,
                sheet_index=index,
                sheet_count=len(project.sheets),
                catalog=catalog,
            )
            draw_sheet_body(
                probe,
                sheet,
                catalog,
                coil_style=coil or "circle",
                drawing_style=sheet_drawing_style(sheet, project),
                project=project,
                sheet_no=index + 1,
            )
        except KeyError as exc:
            issues.append(f"{sheet.id}: sheet cannot be drawn ({exc})")
            continue
        issues.extend(_painted(project, sheet, probe, index, pitch, catalog, occupied_rung_indices, nema_line_from_index))
        if sheet.kind == "schematic":
            for ann in xref_annotations(project, catalog, sheet):
                if ann.style != "nema":
                    issues.append(f"{sheet.id}: cross-reference on {ann.owner} uses {ann.style}")
                if abs(float(ann.size) - XREF_SIZE) > 0.05:
                    issues.append(f"{sheet.id}: cross-reference lettering is {ann.size} mm, not {XREF_SIZE} mm")

    if style not in {"ladder", "schematic", "freeform"}:
        issues.append(f"drawing style {style or 'blank'} is not ladder, schematic, or freeform")
    return issues


def _wire_number_ok(number: str) -> bool:
    text = number.strip().upper()
    if text.startswith(_UNREADABLE):
        return True
    if text in _CONVENTIONAL:
        return True
    return _SHEET_LINE.match(text) is not None


def _painted(
    project: Project,
    sheet: Any,
    probe: _Probe,
    index: int,
    pitch: float,
    catalog: Any,
    occupied_rung_indices: Any,
    nema_line_from_index: Any,
) -> list[str]:
    issues: list[str] = []
    sid = sheet.id
    widths = {round(value, 2) for value in probe.strokes}
    if BORDER_STROKE not in widths:
        issues.append(f"{sid}: border is not {BORDER_STROKE} mm")
    drawn_style = ""
    from panelcad.core.ladder import sheet_drawing_style

    drawn_style = sheet_drawing_style(sheet, project)
    if drawn_style == "ladder" and sheet.kind == "schematic":
        if RAIL_STROKE not in widths:
            issues.append(f"{sid}: ladder rails are not {RAIL_STROKE} mm")
        indexes = sorted(occupied_rung_indices(project, sheet, catalog))
        for rung in indexes:
            line = str(nema_line_from_index(index + 1, rung))
            if not any(text == line and abs(size - RUNG_NUMBER_MM) < 0.05 for text, size in probe.texts):
                issues.append(f"{sid}: rung {line} is missing or not {RUNG_NUMBER_MM} mm")
        if not any(text == "A" and abs(size - ZONE_LABEL_MM) < 0.05 for text, size in probe.texts):
            issues.append(f"{sid}: zone lettering is not {ZONE_LABEL_MM} mm")
        hot = str(getattr(sheet, "rail_hot", "") or project.title_block.rail_hot or "L1")
        if not any(text == hot and abs(size - RAIL_LABEL_MM) < 0.05 for text, size in probe.texts):
            issues.append(f"{sid}: rail caption {hot} is not {RAIL_LABEL_MM} mm")
        for wire in sheet.wires:
            mark = str(wire.drawn_mark(ladder=True) or "")
            if not mark:
                continue
            if not any(text == mark and abs(size - MARK_SIZE) < 0.05 for text, size in probe.texts):
                # A rail name drawn as the rail caption is not also a mid-wire mark.
                if mark in {hot, str(getattr(sheet, "rail_return", "") or project.title_block.rail_return or "L2")}:
                    continue
                issues.append(f"{sid}: wire number {mark} is not drawn at {MARK_SIZE} mm")
        for comp in sheet.components:
            if "offpage" in str(comp.library_id or ""):
                continue
            lib = str(comp.library_id or "")
            if "offpage" in lib or "needs_info" in lib or comp.id in {"?", ""}:
                continue
            from panelcad.render.terminal_strip import TAG_SIZE as STRIP_TAG_MM
            from panelcad.render.terminal_strip import WIRE_SIZE as STRIP_WIRE_MM

            allowed = (TAG_SIZE, COIL_TAG_MM, CONTACT_TAG_MM)
            if getattr(sheet, "kind", "") == "terminal":
                allowed = allowed + (STRIP_TAG_MM, STRIP_WIRE_MM)
            if not any(text == comp.id and any(abs(size - item) < 0.05 for item in allowed) for text, size in probe.texts):
                issues.append(
                    f"{sid}: tag {comp.id} is not drawn at {TAG_SIZE}, {COIL_TAG_MM}, or {CONTACT_TAG_MM} mm"
                )
    drawing_number = str(project.title_block.drawing_number or "")
    if drawing_number and not any(text == drawing_number for text, _size in probe.texts):
        issues.append(f"{sid}: title block does not show drawing number {drawing_number}")
    if not any(text == "NEMA/ANSI" for text, _size in probe.texts):
        issues.append(f"{sid}: title block does not show NEMA/ANSI")
    return issues
