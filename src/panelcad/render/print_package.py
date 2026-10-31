"""One-click print package: cover, index, revisions, drawings, and paginated reports.

Drawings reuse ``paint_sheet`` (same title block and symbols as File → Print).
Front matter and reports are US Letter by default; drawings keep their own
sheet size unless the caller picks one paper size for everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from panelcad.core.models import Project, Revision, Sheet, TitleBlock
from panelcad.io.bom import grouped_rows
from panelcad.render.page import PRINT_PAPER, configure_full_page, paint_sheet
from panelcad.render.qt_painter import QtPainter
from panelcad.render.reports import wire_rows
from panelcad.render.titleblock import CELL, HEADER, INK, RULE, resolve_logo
from panelcad.version import __version__

log = logging.getLogger("panelcad.print_package")

SECTIONS: tuple[str, ...] = (
    "cover",
    "index",
    "revisions",
    "nameplate",
    "drawings",
    "cutouts",
    "bom",
    "wires",
    "from_to",
    "xref",
    "plc_io",
    "terminals",
    "interconnect",
    "checks",
    "power",
    "thermal",
)

# Cutout sheets and the control-power budget are optional. Thermal stays in the default package.
DEFAULT_SECTIONS = tuple(name for name in SECTIONS if name not in {"power", "cutouts"})

PAPER_SIZES_MM: dict[str, tuple[float, float]] = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
    "a3": (297.0, 420.0),
    "tabloid": (279.4, 431.8),
}

_SECTION_ALIASES = {
    "toc": "index",
    "contents": "index",
    "drawing_index": "index",
    "rev": "revisions",
    "revision": "revisions",
    "revision_history": "revisions",
    "nameplate": "nameplate",
    "plate": "nameplate",
    "sheets": "drawings",
    "drawing": "drawings",
    "cutout": "cutouts",
    "drill": "cutouts",
    "cutout_drawings": "cutouts",
    "schematic": "drawings",
    "bom_grouped": "bom",
    "wire": "wires",
    "wire_list": "wires",
    "from_to": "from_to",
    "from-to": "from_to",
    "fromto": "from_to",
    "wire_from_to": "from_to",
    "xref": "xref",
    "cross_reference": "xref",
    "cross-reference": "xref",
    "device_index": "xref",
    "device-index": "xref",
    "xref_list": "xref",
    "io": "plc_io",
    "io_list": "plc_io",
    "plc": "plc_io",
    "plc_io_list": "plc_io",
    "terminal": "terminals",
    "terminal_strips": "terminals",
    "interconnect": "interconnect",
    "interpanel": "interconnect",
    "inter_panel": "interconnect",
    "cable_schedule": "interconnect",
    "check": "checks",
    "design_checks": "checks",
    "findings": "checks",
    "power_budget": "power",
    "control_power": "power",
    "heat": "thermal",
    "cooling": "thermal",
    "thermal_report": "thermal",
}

_PAPER_ALIASES = {
    "mixed": "mixed",
    "auto": "mixed",
    "default": "mixed",
    "letter": "letter",
    "usletter": "letter",
    "a4": "a4",
    "a3": "a3",
    "tabloid": "tabloid",
    "ansib": "tabloid",
    "b": "tabloid",
    "11x17": "tabloid",
}

MARGIN_X = 14.0
MARGIN_Y = 16.0
TITLE_H = 16.0
SUB_H = 7.5
ROW_H = 6.4
LINE_H = 3.6
ROW_PAD = 2.2
FOOTER_H = 10.0
INDEX_ROW_H = 7.2
CHAR_W = 0.52


@dataclass(frozen=True)
class PackagePage:
    """One PDF page in deterministic package order."""

    section: str
    title: str
    width_mm: float
    height_mm: float
    sheet_id: str = ""
    sheet_number: int = 0
    sheet_count: int = 0
    report: str = ""
    row_start: int = 0
    row_count: int = 0
    note: str = ""


@dataclass
class IndexRow:
    number: str
    title: str
    kind: str
    rev: str
    pdf_page: int
    target: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "kind": self.kind,
            "rev": self.rev,
            "page": self.pdf_page,
        }


@dataclass
class Bookmark:
    title: str
    page: int  # 0-based
    children: list[Bookmark] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": self.title, "page": self.page + 1}
        if self.children:
            payload["children"] = [child.as_dict() for child in self.children]
        return payload


@dataclass
class LinkHotspot:
    page: int
    x: float
    y: float
    w: float
    h: float
    target: int


@dataclass
class PackagePlan:
    pages: list[PackagePage]
    bookmarks: list[Bookmark]
    paper: str
    sections: list[str]
    drawing_count: int
    index_rows: list[IndexRow] = field(default_factory=list)
    _report_rows: dict[tuple[str, str], list[dict[str, str]]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _thermal: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    _power_budgets: list[Any] | None = field(default=None, repr=False, compare=False)
    _cutout_drawings: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def first_page(self, section: str) -> int | None:
        for i, page in enumerate(self.pages):
            if page.section == section:
                return i
        return None

    def sheet_page(self, sheet_id: str) -> int | None:
        for i, page in enumerate(self.pages):
            if page.section == "drawings" and page.sheet_id == sheet_id:
                return i
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": len(self.pages),
            "paper": self.paper,
            "sections": list(self.sections),
            "drawing_count": self.drawing_count,
            "bookmarks": [item.as_dict() for item in self.bookmarks],
            "order": [page.section for page in self.pages],
            "sheets": [
                {
                    "id": page.sheet_id,
                    "title": page.title,
                    "number": page.sheet_number,
                    "count": page.sheet_count,
                    "page": index + 1,
                }
                for index, page in enumerate(self.pages)
                if page.section == "drawings"
            ],
            "index": [row.as_dict() for row in self.index_rows],
        }


def normalize_paper(value: str | None) -> str:
    token = "".join(ch.lower() for ch in str(value or "mixed") if ch.isalnum())
    if token not in _PAPER_ALIASES:
        raise ValueError(f"unknown paper '{value}' (mixed, letter, a4, a3, tabloid)")
    return _PAPER_ALIASES[token]


def normalize_sections(sections: str | Sequence[str] | None) -> list[str]:
    if sections is None:
        wanted = set(DEFAULT_SECTIONS)
    elif isinstance(sections, str):
        wanted = set()
        for part in sections.split(","):
            token = part.strip().lower()
            if not token:
                continue
            key = _SECTION_ALIASES.get(token, token)
            if key not in SECTIONS:
                raise ValueError(f"unknown package section '{part.strip()}'")
            wanted.add(key)
    else:
        wanted = set()
        for part in sections:
            token = str(part).strip().lower()
            if not token:
                continue
            key = _SECTION_ALIASES.get(token, token)
            if key not in SECTIONS:
                raise ValueError(f"unknown package section '{part}'")
            wanted.add(key)
    return [name for name in SECTIONS if name in wanted]


def report_page_size(paper: str) -> tuple[float, float]:
    key = "letter" if paper == "mixed" else paper
    return PAPER_SIZES_MM[key]


def drawing_page_size(sheet: Sheet, paper: str) -> tuple[float, float]:
    sw, sh = sheet.page.dimensions()
    if paper == "mixed":
        return sw, sh
    bw, bh = PAPER_SIZES_MM[paper]
    landscape = sw >= sh
    if landscape and bw < bh:
        return bh, bw
    if (not landscape) and bw > bh:
        return bh, bw
    return bw, bh


def rows_per_report_page(height_mm: float, *, extra_lines: int = 0) -> int:
    usable = height_mm - 2 * MARGIN_Y - TITLE_H - SUB_H - ROW_H - FOOTER_H
    usable -= extra_lines * ROW_H
    return max(1, int(usable / ROW_H))


def current_revision(tb: TitleBlock) -> Revision | None:
    for rev in tb.revisions:
        if any(str(part or "").strip() for part in (rev.rev, rev.date, rev.description)):
            return rev
    return None


def revision_rows(tb: TitleBlock) -> list[Revision]:
    return [
        rev
        for rev in tb.revisions
        if any(str(part or "").strip() for part in (rev.rev, rev.date, rev.description, rev.drawn))
    ]


def project_description(project: Project) -> str:
    """Cover subtitle. An I/O continuation line is not the project description."""

    raw = str(project.properties.get("description") or "").strip()
    if raw:
        return raw
    for sheet in project.sheets:
        if sheet.kind != "schematic":
            continue
        if str(getattr(sheet, "io_cont", "") or "").strip():
            continue
        text = (sheet.description or "").strip()
        if text and "(cont.)" not in text:
            return text
    n = len(project.sheets)
    return f"{n}-sheet control-panel drawing set"


def _index_sheet_number(sheet: Sheet, index: int) -> str:
    """Drawing-index SHEET cell. A terminal strip is listed under its strip name."""

    if sheet.kind == "terminal":
        strip = str(sheet.strip or "").strip()
        if strip:
            part = int(sheet.strip_part or 1)
            return strip if part <= 1 else f"{strip} ({part})"
    return str(index + 1)


def _index_sheet_title(sheet: Sheet) -> str:
    title = str(sheet.title or "").strip()
    if sheet.kind == "terminal":
        strip = str(sheet.strip or "").strip()
        if strip and strip not in title:
            title = f"Terminal strip {strip}" if not title else f"{title} ({strip})"
        elif not title:
            title = f"Terminal strip {sheet.id}"
    return title or sheet.id


def _chunk(rows: Sequence[Any], size: int) -> list[tuple[int, int]]:
    if not rows:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    while start < len(rows):
        count = min(size, len(rows) - start)
        out.append((start, count))
        start += count
    return out


def wrap_lines(text: str, width_mm: float, size_mm: float, *, bold: bool = False) -> list[str]:
    """Word-wrap for table cells. Long tokens are split; nothing is ellipsized.

    ``bold`` measures the real bold face. A character-count wrap is too wide
    for that face and runs through the right margin.
    """

    raw = (text or "").replace("\n", " ").strip()
    if not raw:
        return [""]
    if bold:
        return _wrap_measured(raw, width_mm, size_mm)
    cpl = max(4, int(max(width_mm - 2.6, 4.0) / max(size_mm, 1.0) / CHAR_W))
    lines: list[str] = []
    current = ""
    for word in raw.split():
        trial = f"{current} {word}".strip() if current else word
        if len(trial) <= cpl:
            current = trial
            continue
        if current:
            lines.append(current)
        while len(word) > cpl:
            lines.append(word[:cpl])
            word = word[cpl:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def _wrap_measured(raw: str, width_mm: float, size_mm: float) -> list[str]:
    """Wrap on the bold font's advance, staying inside ``width_mm``."""

    from panelcad.render.qt_painter import text_advance_mm

    budget = max(8.0, float(width_mm) - 1.5)

    def width_of(token: str) -> float:
        return text_advance_mm(token, size_mm, bold=True)

    def split_token(word: str) -> list[str]:
        parts: list[str] = []
        while word and width_of(word) > budget:
            lo, hi, best = 1, len(word), 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if width_of(word[:mid]) <= budget:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            parts.append(word[:best])
            word = word[best:]
        if word:
            parts.append(word)
        return parts or [""]

    lines: list[str] = []
    current = ""
    for word in raw.split():
        for piece in split_token(word):
            trial = f"{current} {piece}".strip() if current else piece
            if width_of(trial) <= budget:
                current = trial
                continue
            if current:
                lines.append(current)
            current = piece
    if current:
        lines.append(current)
    return lines or [""]


def wrapped_row_height(values: Sequence[str], widths: Sequence[float], size_mm: float) -> float:
    n = max(len(wrap_lines(str(val or ""), w, size_mm)) for val, w in zip(values, widths, strict=True))
    return max(ROW_H, n * LINE_H + ROW_PAD)


def _paginate_wrapped(
    rows: Sequence[dict[str, str]],
    keys: Sequence[str],
    widths: Sequence[float],
    usable_h: float,
) -> list[tuple[int, int]]:
    if not rows:
        return []
    chunks: list[tuple[int, int]] = []
    i = 0
    header = ROW_H
    while i < len(rows):
        used = header
        start = i
        while i < len(rows):
            values = [str(rows[i].get(key, "") or "") for key in keys]
            height = wrapped_row_height(values, widths, 2.15)
            if used + height > usable_h and i > start:
                break
            used += height
            i += 1
        chunks.append((start, i - start))
    return chunks


def _need_catalog_column(rows: Sequence[dict[str, str]]) -> bool:
    for row in rows:
        part = str(row.get("part_number") or "").strip()
        catalog = str(row.get("catalog") or "").strip()
        if part and catalog and part != catalog:
            return True
    return False


def _bom_display_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    extra = _need_catalog_column(rows)
    out: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        part = str(item.get("part_number") or "").strip()
        catalog = str(item.get("catalog") or "").strip()
        item["part_number"] = part or catalog
        if extra:
            item["catalog"] = "" if not catalog or catalog == part else catalog
        out.append(item)
    return out


def _resolved_widths(raw: list[float], page_w: float) -> list[float]:
    usable = page_w - 2 * MARGIN_X
    flex = usable - sum(raw)
    return [flex if w == 0.0 else w for w in raw]


def _report_table_usable(height_mm: float, *, extra_lines: int = 0) -> float:
    usable = height_mm - 2 * MARGIN_Y - TITLE_H - SUB_H - FOOTER_H
    usable -= extra_lines * ROW_H
    return max(ROW_H * 2, usable)


_REPORT_TITLES = {
    "bom": "Bill of Materials",
    "wires": "Wire List",
    "from_to": "Wire List (From-To)",
    "xref": "Device Index",
    "plc_io": "PLC I/O List",
    "terminals": "Terminal Strips",
    "interconnect": "Interconnect Schedule",
    "checks": "Design Checks",
}


def _collect_report_rows_and_note(session: Any, kind: str) -> tuple[list[dict[str, str]], str]:
    if kind == "bom":
        from panelcad.io.bom import with_needs_info_section

        counted = _bom_display_rows(grouped_rows(session.project, session.catalog))
        return with_needs_info_section(counted, session.project, session.catalog), ""
    if kind == "wires":
        return wire_rows(session.project), ""
    if kind == "from_to":
        return list(session.from_to_rows(sort="number")), ""
    if kind == "xref":
        return list(session.device_index_rows()), ""
    if kind == "plc_io":
        return list(session.io_rows()), ""
    if kind == "terminals":
        return list(session.terminal_strip_rows()), ""
    if kind == "interconnect":
        from panelcad.core.panels import interconnect_rows

        return interconnect_rows(session.project), ""
    all_rows = session.design_checks(include_suppressed=True)
    open_rows = [row for row in all_rows if row.get("suppressed") != "yes"]
    n_suppressed = len(all_rows) - len(open_rows)
    note = f"{n_suppressed} finding(s) suppressed (not listed)." if n_suppressed else ""
    return open_rows, note


def _panel_bom_pages(
    session: Any,
    report_w: float,
    report_h: float,
    *,
    cache: dict[tuple[str, str], list[dict[str, str]]] | None = None,
) -> list[PackagePage]:
    """One BOM block per panel, then the summed project bill."""

    from panelcad.core.panels import panel_tag
    from panelcad.io.bom import bom_for_panel, grouped_rows

    pages: list[PackagePage] = []
    project = session.project
    for panel in project.panels:
        rows = _bom_display_rows(bom_for_panel(project, session.catalog, panel.id))
        if not rows:
            continue
        if cache is not None:
            cache[("bom", panel.id)] = rows
        pages.extend(
            _paginated_report(
                "bom",
                f"Bill of Materials — {panel_tag(panel)}",
                rows,
                "",
                report_w,
                report_h,
                sheet_id=panel.id,
            )
        )
    rolled = _bom_display_rows(grouped_rows(project, session.catalog, by_panel=False))
    from panelcad.io.bom import with_needs_info_section

    rolled = with_needs_info_section(rolled, project, session.catalog)
    if rolled:
        if cache is not None:
            cache[("bom", "project")] = rolled
        pages.extend(
            _paginated_report(
                "bom",
                "Bill of Materials",
                rolled,
                "",
                report_w,
                report_h,
                sheet_id="project",
            )
        )
    return pages


def _paginated_report(
    kind: str,
    title: str,
    rows: list[dict[str, str]],
    note: str,
    report_w: float,
    report_h: float,
    *,
    sheet_id: str = "",
) -> list[PackagePage]:
    keys, _labels, raw_widths = _report_spec(kind, rows)
    widths = _resolved_widths(raw_widths, report_w)
    usable = _report_table_usable(report_h, extra_lines=1 if note else 0)
    pages: list[PackagePage] = []
    for start, count in _paginate_wrapped(rows, keys, widths, usable):
        pages.append(
            PackagePage(
                kind,
                title,
                report_w,
                report_h,
                sheet_id=sheet_id,
                report=kind,
                row_start=start,
                row_count=count,
                note=note,
            )
        )
    return pages


def _index_rows_per_page(height_mm: float) -> int:
    usable = height_mm - 2 * MARGIN_Y - TITLE_H - SUB_H - INDEX_ROW_H - FOOTER_H
    return max(1, int(usable / INDEX_ROW_H))


def plan_print_package(
    session: Any,
    *,
    sections: str | Sequence[str] | None = None,
    paper: str = "mixed",
    sheet_ids: Sequence[str] | None = None,
) -> PackagePlan:
    """Deterministic page list. Same project + options → same order and count.

    Empty report sections are omitted (no placeholder page, no index row).
    Sheet numbers inside reports follow the printed order.
    """

    from panelcad.core.panels import package_sheet_order
    from panelcad.core.zones import use_sheet_order

    with use_sheet_order(package_sheet_order(session.project)):
        return _plan_print_package(session, sections=sections, paper=paper, sheet_ids=sheet_ids)


def normalize_sheet_ids(project: Project, sheet_ids: Sequence[str] | None) -> list[str] | None:
    """``None`` keeps every sheet. Otherwise only known ids, in project order.

    Unknown ids raise so a stale selection can never silently print nothing.
    """

    if sheet_ids is None:
        return None
    known = [sheet.id for sheet in project.sheets]
    wanted = {str(item).strip() for item in sheet_ids if str(item).strip()}
    unknown = sorted(wanted - set(known))
    if unknown:
        raise ValueError(f"unknown sheet id(s) for print package: {', '.join(unknown)}")
    return [sid for sid in known if sid in wanted]


def _plan_print_package(
    session: Any,
    *,
    sections: str | Sequence[str] | None = None,
    paper: str = "mixed",
    sheet_ids: Sequence[str] | None = None,
) -> PackagePlan:
    paper = normalize_paper(paper)
    enabled = normalize_sections(sections)
    project: Project = session.project
    from panelcad.render.terminal_strip import ensure_terminal_pages

    ensure_terminal_pages(project, session.catalog)
    from panelcad.core.panels import package_sheet_order, package_sheet_title
    tb = project.title_block
    rev = current_revision(tb)
    rev_s = (rev.rev if rev else "") or ""
    report_w, report_h = report_page_size(paper)
    all_sheets = package_sheet_order(project)
    chosen = normalize_sheet_ids(project, sheet_ids)
    if "drawings" not in enabled:
        drawn_sheets = []
    elif chosen is None:
        drawn_sheets = all_sheets
    else:
        keep = set(chosen)
        drawn_sheets = [sheet for sheet in all_sheets if sheet.id in keep]
    drawing_count = len(drawn_sheets)

    cover_pages: list[PackagePage] = []
    if "cover" in enabled:
        cover_pages.append(PackagePage("cover", "Cover", report_w, report_h))

    rev_pages: list[PackagePage] = []
    if "revisions" in enabled and revision_rows(tb):
        rev_pages.append(PackagePage("revisions", "Revision History", report_w, report_h))

    nameplate_pages: list[PackagePage] = []
    if "nameplate" in enabled:
        nameplate_pages.append(PackagePage("nameplate", "Nameplate", report_w, report_h))

    drawing_pages: list[PackagePage] = []
    for index, sheet in enumerate(drawn_sheets):
        dw, dh = drawing_page_size(sheet, paper)
        drawing_pages.append(
            PackagePage(
                "drawings",
                package_sheet_title(project, sheet, sheet.title or sheet.id),
                dw,
                dh,
                sheet_id=sheet.id,
                sheet_number=index + 1,
                sheet_count=drawing_count,
            )
        )

    report_rows_cache: dict[tuple[str, str], list[dict[str, str]]] = {}
    thermal_cache: dict[str, Any] | None = None
    power_budgets_cache: list[Any] | None = None
    cutout_drawings_cache: dict[str, Any] = {}

    cutout_pages: list[PackagePage] = []
    if "cutouts" in enabled:
        cutout_pages.extend(_cutout_package_pages(session, paper, cache=cutout_drawings_cache))

    report_pages: list[PackagePage] = []
    for kind, title in _REPORT_TITLES.items():
        if kind not in enabled:
            continue
        if kind == "bom" and len(project.panels) >= 2:
            report_pages.extend(
                _panel_bom_pages(session, report_w, report_h, cache=report_rows_cache)
            )
            continue
        rows, note = _collect_report_rows_and_note(session, kind)
        if not rows:
            continue
        report_rows_cache[(kind, "")] = rows
        keys, _labels, raw_widths = _report_spec(kind, rows)
        widths = _resolved_widths(raw_widths, report_w)
        usable = _report_table_usable(report_h, extra_lines=1 if note else 0)
        for start, count in _paginate_wrapped(rows, keys, widths, usable):
            report_pages.append(
                PackagePage(
                    kind,
                    title,
                    report_w,
                    report_h,
                    report=kind,
                    row_start=start,
                    row_count=count,
                    note=note,
            )
        )

    if "thermal" in enabled:
        from panelcad.core.thermal import compute_heat_load
        from panelcad.render.thermal_page import thermal_spans

        thermal = compute_heat_load(project, session.catalog, getattr(session, "parts", None))
        thermal_cache = thermal
        if thermal.get("enclosure") or thermal.get("devices"):
            for start, count in thermal_spans(thermal, report_w, report_h):
                report_pages.append(
                    PackagePage(
                        "thermal",
                        "Enclosure Thermal",
                        report_w,
                        report_h,
                        report="thermal",
                        row_start=start,
                        row_count=count,
                    )
                )

    if "power" in enabled:
        from panelcad.core.power_budget import analyze

        budgets = analyze(project, session.catalog, getattr(session, "parts", None))
        power_budgets_cache = budgets
        for budget in budgets:
            report_pages.append(
                PackagePage(
                    "power",
                    f"Control power {budget.ref}",
                    report_w,
                    report_h,
                    sheet_id=budget.ref,
                    report="power",
                )
            )

    pending: list[dict[str, str]] = []
    if nameplate_pages:
        pending.append(
            {
                "number": "—",
                "title": "Nameplate",
                "kind": "nameplate",
                "rev": rev_s,
                "section": "nameplate",
                "sheet_id": "",
            }
        )
    if drawing_pages:
        for index, sheet in enumerate(drawn_sheets):
            pending.append(
                {
                    "number": _index_sheet_number(sheet, index),
                    "title": package_sheet_title(project, sheet, _index_sheet_title(sheet)),
                    "kind": sheet.kind.replace("_", " "),
                    "rev": rev_s,
                    "section": "drawings",
                    "sheet_id": sheet.id,
                }
            )
    for index, page in enumerate(cutout_pages):
        pending.append(
            {
                "number": f"C{index + 1}",
                "title": page.title,
                "kind": "cutout",
                "rev": rev_s,
                "section": "cutouts",
                "sheet_id": page.sheet_id,
            }
        )
    seen_reports: set[tuple[str, str]] = set()
    for page in report_pages:
        sid = page.sheet_id if page.section == "bom" else ""
        key = (page.section, sid)
        if key in seen_reports:
            continue
        seen_reports.add(key)
        pending.append(
            {
                "number": "—",
                "title": page.title,
                "kind": "report",
                "rev": rev_s,
                "section": page.section,
                "sheet_id": sid,
            }
        )

    index_pages: list[PackagePage] = []
    if "index" in enabled and pending:
        per = _index_rows_per_page(report_h)
        for start, count in _chunk(pending, per):
            index_pages.append(
                PackagePage(
                    "index",
                    "Drawing Index",
                    report_w,
                    report_h,
                    row_start=start,
                    row_count=count,
                )
            )

    pages = cover_pages + index_pages + rev_pages + nameplate_pages + drawing_pages + cutout_pages + report_pages
    index_rows: list[IndexRow] = []
    for entry in pending:
        if entry["section"] == "bom" and entry.get("sheet_id"):
            target = next(
                i
                for i, page in enumerate(pages)
                if page.section == "bom" and page.sheet_id == entry["sheet_id"]
            )
        elif entry["section"] in {"drawings", "cutouts"} and entry.get("sheet_id"):
            target = next(
                i
                for i, page in enumerate(pages)
                if page.section == entry["section"] and page.sheet_id == entry["sheet_id"]
            )
        else:
            target = next(i for i, page in enumerate(pages) if page.section == entry["section"])
        index_rows.append(
            IndexRow(
                number=entry["number"],
                title=entry["title"],
                kind=entry["kind"],
                rev=entry["rev"],
                pdf_page=target + 1,
                target=target,
            )
        )

    used: list[str] = []
    for page in pages:
        if page.section not in used:
            used.append(page.section)
    return PackagePlan(
        pages=pages,
        bookmarks=_bookmarks(pages),
        paper=paper,
        sections=used,
        drawing_count=drawing_count,
        index_rows=index_rows,
        _report_rows=report_rows_cache,
        _thermal=thermal_cache,
        _power_budgets=power_budgets_cache,
        _cutout_drawings=cutout_drawings_cache,
    )


def _cutout_package_pages(
    session: Any,
    paper: str,
    *,
    cache: dict[str, Any] | None = None,
) -> list[PackagePage]:
    from panelcad.core.cutout import surface_title

    pages: list[PackagePage] = []
    for drawing in session.cutout_drawings(None):
        if not drawing.features:
            continue
        if cache is not None:
            cache[drawing.surface] = drawing
        width, height = _fitted_page(drawing.page_width_mm, drawing.page_height_mm, paper)
        pages.append(
            PackagePage(
                "cutouts",
                f"{surface_title(drawing.surface)} cutouts",
                width,
                height,
                sheet_id=drawing.surface,
            )
        )
    return pages


def _fitted_page(width_mm: float, height_mm: float, paper: str) -> tuple[float, float]:
    if paper == "mixed":
        return width_mm, height_mm
    box_w, box_h = PAPER_SIZES_MM[paper]
    landscape = width_mm >= height_mm
    if landscape and box_w < box_h:
        return box_h, box_w
    if (not landscape) and box_w > box_h:
        return box_h, box_w
    return box_w, box_h


def _bookmarks(pages: list[PackagePage]) -> list[Bookmark]:
    items: list[Bookmark] = []
    drawings: Bookmark | None = None
    cutouts: Bookmark | None = None
    seen: set[str] = set()
    for index, page in enumerate(pages):
        if page.section == "drawings":
            if drawings is None:
                drawings = Bookmark("Drawings", index)
                items.append(drawings)
                seen.add("drawings")
            drawings.children.append(Bookmark(page.title, index))
            continue
        if page.section == "cutouts":
            if cutouts is None:
                cutouts = Bookmark("Cutouts", index)
                items.append(cutouts)
                seen.add("cutouts")
            cutouts.children.append(Bookmark(page.title, index))
            continue
        if page.section in seen:
            continue
        seen.add(page.section)
        items.append(Bookmark(page.title, index))
    return items


def _import_not_for_build(project: Any) -> bool:
    """A print import that skipped a tag the sheet cannot build."""

    info = getattr(project, "properties", {}) or {}
    block = info.get("print_import") if isinstance(info, dict) else None
    if not isinstance(block, dict):
        return False
    return str(block.get("not_for_build") or "").strip().lower() in {"yes", "true", "1"}


def _paint_package_pages(painter, writer, session, plan, hotspots: list, base_dir: Path) -> None:
    from panelcad.reconstruct.placeholder import project_has_needs_info

    needs_banner = project_has_needs_info(session.project) or _import_not_for_build(session.project)
    for i, page in enumerate(plan.pages):
        if i > 0:
            configure_full_page(writer, page.width_mm, page.height_mm)
            writer.newPage()
        dpi_x = float(writer.logicalDpiX() or 72)
        dpi_y = float(writer.logicalDpiY() or 72)
        painter.resetTransform()
        painter.scale(dpi_x / 25.4, dpi_y / 25.4)
        found = _paint_page(
            painter,
            session,
            plan,
            page,
            i,
            base_dir=base_dir,
        )
        if needs_banner:
            qp = QtPainter(painter, print_paper=True)
            qp.set_stroke("#c45c00", 0.6)
            qp.text(page.width_mm / 2.0, 8.0, "NOT FOR BUILD — info needed", 3.2, align="middle")
        hotspots.extend(found)


def write_print_package(
    session: Any,
    path: Path | str,
    *,
    sections: str | Sequence[str] | None = None,
    paper: str = "mixed",
    sheet_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Write a vector PDF with bookmarks and index links. Returns a result dict.

    Drawings and reports number sheets in the printed order, including
    off-page arrows and cross-reference line numbers.
    """

    from panelcad.core.panels import package_sheet_order
    from panelcad.core.zones import use_sheet_order

    with use_sheet_order(package_sheet_order(session.project)):
        return _write_print_package(
            session, path, sections=sections, paper=paper, sheet_ids=sheet_ids
        )


def _requested_list(sections: str | Sequence[str] | None) -> list[str] | None:
    if sections is None:
        return None
    if isinstance(sections, str):
        return [part.strip() for part in sections.split(",") if part.strip()]
    return [str(part) for part in sections]


def _write_print_package(
    session: Any,
    path: Path | str,
    *,
    sections: str | Sequence[str] | None = None,
    paper: str = "mixed",
    sheet_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    from PySide6.QtGui import QPainter, QPdfWriter

    from panelcad.render.pdf_outline import apply_outlines_and_links

    plan = plan_print_package(session, sections=sections, paper=paper, sheet_ids=sheet_ids)
    requested = _requested_list(sections)
    log.info(
        "print package: requested sections=%s resolved=%s sheets=%s pages=%d",
        "default" if requested is None else requested,
        plan.sections,
        "all" if sheet_ids is None else list(sheet_ids),
        len(plan.pages),
    )
    if not plan.pages:
        raise ValueError("print package has no pages (enable at least one section)")

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(dest))
    writer.setResolution(300)
    writer.setTitle(session.project.name or "PanelCAD print package")
    if hasattr(writer, "setCreator"):
        writer.setCreator(f"PanelCAD {__version__}")
    first = plan.pages[0]
    configure_full_page(writer, first.width_mm, first.height_mm)
    painter = QPainter(writer)
    hotspots: list[LinkHotspot] = []
    base_dir = session.path.parent if getattr(session, "path", None) else Path.cwd()
    from panelcad.render.titleblock import share_title_nameplate

    try:
        with share_title_nameplate():
            _paint_package_pages(painter, writer, session, plan, hotspots, base_dir)
    finally:
        if painter.isActive():
            painter.end()

    outlined = apply_outlines_and_links(dest, plan.bookmarks, hotspots, plan.pages)
    result = plan.as_dict()
    result["path"] = str(dest)
    result["outlines"] = outlined
    result["requested_sections"] = requested
    result["sheet_ids"] = None if sheet_ids is None else [s["id"] for s in result["sheets"]]
    from panelcad.reconstruct.placeholder import project_has_needs_info

    result["not_for_build"] = project_has_needs_info(session.project) or _import_not_for_build(
        session.project
    )
    return result


def render_package_page_png(
    pdf_path: Path | str,
    page_index: int,
    dest: Path | str,
    *,
    width_px: int = 1400,
) -> Path:
    """Raster one package page for shop previews. The PDF itself stays vector."""

    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument

    src = Path(pdf_path)
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = QPdfDocument()
    err = doc.load(str(src))
    none = getattr(getattr(QPdfDocument, "Error", None), "None_", 0)
    if err != none:
        raise RuntimeError(f"could not load {src}")
    if page_index < 0 or page_index >= doc.pageCount():
        raise IndexError(f"page {page_index} out of range ({doc.pageCount()})")
    size = doc.pagePointSize(page_index)
    w = max(1, int(width_px))
    h = max(1, int(w * float(size.height()) / max(float(size.width()), 1.0)))
    image = doc.render(page_index, QSize(w, h))
    if image.isNull():
        raise RuntimeError(f"could not render page {page_index}")
    image.save(str(out), "PNG")
    return out


def extract_pdf_text(path: Path | str, page_index: int | None = None) -> str:
    """Selectable text from a QPdfWriter file (Qt, then pypdf)."""

    from PySide6.QtPdf import QPdfDocument

    src = Path(path)
    doc = QPdfDocument()
    err = doc.load(str(src))
    none = getattr(getattr(QPdfDocument, "Error", None), "None_", 0)
    if err != none:
        raise RuntimeError(f"could not load {src}")
    pages = range(doc.pageCount()) if page_index is None else [page_index]
    chunks: list[str] = []
    getter = getattr(doc, "getAllText", None)
    if callable(getter):
        for i in pages:
            selection = getter(i)
            text = getattr(selection, "text", lambda: "")()
            chunks.append(text or "")
        joined = "\n".join(chunks)
        if joined.strip():
            return joined
    try:
        from pypdf import PdfReader
    except ImportError:
        return "\n".join(chunks)

    reader = PdfReader(str(src))
    if page_index is None:
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    return reader.pages[page_index].extract_text() or ""


def _paint_page(
    painter,
    session: Any,
    plan: PackagePlan,
    page: PackagePage,
    page_index: int,
    *,
    base_dir: Path,
) -> list[LinkHotspot]:
    from PySide6.QtCore import QRectF

    painter.fillRect(QRectF(0, 0, page.width_mm, page.height_mm), PRINT_PAPER)
    qp = QtPainter(painter, print_paper=True)
    if page.section == "cover":
        _paint_cover(qp, session.project, page, page_index, len(plan.pages), base_dir)
        return []
    if page.section == "index":
        return _paint_index(qp, session.project, plan, page, page_index, len(plan.pages))
    if page.section == "revisions":
        _paint_revisions(qp, session.project, page, page_index, len(plan.pages))
        return []
    if page.section == "nameplate":
        _paint_nameplate(qp, session, page)
        return []
    if page.section == "drawings":
        _paint_drawing(painter, session, page, base_dir)
        return []
    if page.section == "cutouts":
        _paint_cutout(painter, session, page, plan=plan)
        return []
    if page.section == "power":
        _paint_power(qp, session, page, page_index, len(plan.pages), plan=plan)
        return []
    if page.section == "thermal":
        from panelcad.render.thermal_page import paint_thermal_page

        thermal = plan._thermal
        if thermal is None:
            from panelcad.core.thermal import compute_heat_load

            thermal = compute_heat_load(
                session.project, session.catalog, getattr(session, "parts", None)
            )
        paint_thermal_page(
            qp,
            session.project,
            thermal,
            page_w=page.width_mm,
            page_h=page.height_mm,
            page_index=page_index,
            page_count=len(plan.pages),
            row_start=page.row_start,
            row_count=page.row_count,
        )
        return []
    _paint_report(qp, session, plan, page, page_index, len(plan.pages))
    return []


def _paint_cutout(
    painter,
    session: Any,
    page: PackagePage,
    *,
    plan: PackagePlan | None = None,
) -> None:
    from panelcad.render.cutout_draw import (
        legend_reserves,
        needs_package_legend,
        paint_cutout,
        paint_cutout_legend,
    )
    from panelcad.render.qt_painter import QtPainter

    drawing = plan._cutout_drawings.get(page.sheet_id) if plan is not None else None
    if drawing is None:
        drawing = next(
            (item for item in session.cutout_drawings(None) if item.surface == page.sheet_id),
            None,
        )
    if drawing is None:
        return
    sw, sh = drawing.page_width_mm, drawing.page_height_mm
    pw, ph = page.width_mm, page.height_mm
    raw = 1.0
    if abs(pw - sw) > 0.5 or abs(ph - sh) > 0.5:
        raw = min(pw / max(sw, 1.0), ph / max(sh, 1.0))
    if needs_package_legend(raw):
        top_band, bottom_band = legend_reserves(drawing, pw)
        avail_w = max(40.0, pw - 8.0)
        avail_h = max(40.0, ph - top_band - bottom_band)
        fit = min(avail_w / max(sw, 1.0), avail_h / max(sh, 1.0))
        painter.save()
        painter.translate((pw - sw * fit) / 2.0, top_band + max(0.0, avail_h - sh * fit) / 2.0)
        painter.scale(fit, fit)
        paint_cutout(QtPainter(painter, print_paper=True), drawing, session.project, fit_scale=fit, chrome=False)
        painter.restore()
        paint_cutout_legend(
            QtPainter(painter, print_paper=True),
            drawing,
            session.project,
            page_width=pw,
            page_height=ph,
            fit_scale=fit,
        )
        return
    painter.save()
    if raw < 1.0:
        painter.translate((pw - sw * raw) / 2.0, (ph - sh * raw) / 2.0)
        painter.scale(raw, raw)
    paint_cutout(QtPainter(painter, print_paper=True), drawing, session.project, fit_scale=raw)
    painter.restore()


def _paint_nameplate(qp: QtPainter, session: Any, page: PackagePage) -> None:
    from panelcad.electrical.nameplate import build_nameplate
    from panelcad.electrical.nameplate_page import paint_package_page

    plate = build_nameplate(session.project, session.catalog)
    paint_package_page(qp, plate, page.width_mm, page.height_mm, session.project.name)


def _paint_drawing(painter, session: Any, page: PackagePage, base_dir: Path) -> None:
    # paint_sheet draws the title-block revision table and any revision clouds.
    sheet = session.project.sheet(page.sheet_id)
    sw, sh = sheet.page.dimensions()
    pw, ph = page.width_mm, page.height_mm
    painter.save()
    if abs(pw - sw) > 0.05 or abs(ph - sh) > 0.05:
        scale = min(pw / sw, ph / sh)
        painter.translate((pw - sw * scale) / 2.0, (ph - sh * scale) / 2.0)
        painter.scale(scale, scale)
    paint_sheet(
        painter,
        session.project,
        sheet,
        session.catalog,
        sheet_index=page.sheet_number - 1,
        sheet_count=page.sheet_count,
        base_dir=base_dir,
    )
    painter.restore()


def _outline(qp: QtPainter, x: float, y: float, w: float, h: float, fill: str) -> None:
    qp.set_stroke(RULE, 0.28)
    qp.set_fill(fill)
    qp.rect(x, y, w, h)
    qp.set_fill(None)


def _frame(qp: QtPainter, w: float, h: float) -> None:
    qp.set_stroke(INK, 0.45)
    qp.set_fill(None)
    qp.rect(MARGIN_X / 2, MARGIN_Y / 2, w - MARGIN_X, h - MARGIN_Y)


def _footer(qp: QtPainter, project: Project, page: PackagePage, page_index: int, page_count: int) -> None:
    tb = project.title_block
    y = page.height_mm - MARGIN_Y + 2.0
    left = f"{tb.company or 'PanelCAD'}  {tb.drawing_number or ''}".strip()
    right = f"{page_index + 1} / {page_count}"
    qp.set_stroke(INK, 0.2)
    qp.line(MARGIN_X, y - 4.2, page.width_mm - MARGIN_X, y - 4.2)
    qp.text(MARGIN_X, y, left, 2.2, align="start")
    qp.text(page.width_mm - MARGIN_X, y, right, 2.2, align="end")


def _header(qp: QtPainter, title: str, project: Project, page: PackagePage, page_index: int, page_count: int) -> float:
    tb = project.title_block
    rev = current_revision(tb)
    rev_s = (rev.rev if rev else "") or ""
    x = MARGIN_X
    y = MARGIN_Y
    qp.text(x, y + 5.5, title, 5.2, align="start")
    meta = "  ·  ".join(
        part
        for part in (
            project.name,
            tb.drawing_number,
            f"Rev {rev_s}" if rev_s else "",
            f"Page {page_index + 1} / {page_count}",
        )
        if part
    )
    qp.text(x, y + TITLE_H - 1.5, meta, 2.3, align="start")
    qp.set_stroke(RULE, 0.35)
    qp.line(x, y + TITLE_H + 1.0, page.width_mm - MARGIN_X, y + TITLE_H + 1.0)
    return y + TITLE_H + 4.0


def _paint_cover(
    qp: QtPainter,
    project: Project,
    page: PackagePage,
    page_index: int,
    page_count: int,
    base_dir: Path,
) -> None:
    tb = project.title_block
    rev = current_revision(tb)
    _frame(qp, page.width_mm, page.height_mm)
    x = MARGIN_X + 6.0
    y = MARGIN_Y + 10.0
    logo = resolve_logo(tb.logo, base_dir)
    if logo is not None:
        qp.image(str(logo), x, y, 42.0, 22.0)
    qp.text(x, y + 32.0, tb.company or "PanelCAD", 4.4, align="start")
    qp.text(x, y + 38.5, tb.address or "", 2.4, align="start")
    qp.text(x, y + 62.0, "PRINT PACKAGE", 3.2, align="start")
    qp.text(x, y + 78.0, project.name or "Untitled", 8.0, align="start")
    desc = project_description(project)
    qp.text(x, y + 90.0, desc, 3.2, align="start")

    fields = [
        ("CUSTOMER", tb.customer),
        ("LOCATION", tb.location),
        ("DRAWING NUMBER", tb.drawing_number),
        ("REVISION", (rev.rev if rev else "") or ""),
        ("DATE", (rev.date if rev else "") or ""),
        ("DRAWN", tb.drawn_by),
        ("CHECKED", tb.checked_by),
        ("APPROVED", tb.approved_by),
        ("STANDARD", tb.standard),
        ("SHEETS", str(len(project.sheets))),
    ]
    table_y = y + 108.0
    col_w = (page.width_mm - 2 * MARGIN_X - 12.0) / 2.0
    row_h = 12.0
    for i, (label, value) in enumerate(fields):
        cx = x + (i % 2) * col_w
        cy = table_y + (i // 2) * row_h
        _outline(qp, cx, cy, col_w - 2.0, row_h, CELL)
        qp.text(cx + 2.0, cy + 3.4, label, 1.8, align="start")
        qp.text(cx + 2.0, cy + 9.6, value or "—", 2.8, align="start")
    qp.text(x, page.height_mm - MARGIN_Y - 8.0, f"PanelCAD {__version__}", 2.2, align="start")
    _footer(qp, project, page, page_index, page_count)


def _paint_index(
    qp: QtPainter,
    project: Project,
    plan: PackagePlan,
    page: PackagePage,
    page_index: int,
    page_count: int,
) -> list[LinkHotspot]:
    _frame(qp, page.width_mm, page.height_mm)
    y0 = _header(qp, "DRAWING INDEX", project, page, page_index, page_count)
    headers = ["SHEET", "TITLE", "KIND", "REV", "PAGE"]
    widths = _resolved_widths([18.0, 0.0, 32.0, 16.0, 16.0], page.width_mm)
    usable = page.width_mm - 2 * MARGIN_X
    x = MARGIN_X
    y = y0
    _row_cells(qp, x, y, widths, INDEX_ROW_H, headers, header=True)
    y += INDEX_ROW_H
    hotspots: list[LinkHotspot] = []
    listed = plan.index_rows[page.row_start : page.row_start + page.row_count]
    for row in listed:
        values = [row.number, row.title, row.kind, row.rev, str(row.pdf_page)]
        _row_cells(qp, x, y, widths, INDEX_ROW_H, values, header=False)
        if 0 <= row.target < page_count:
            hotspots.append(LinkHotspot(page_index, x, y, usable, INDEX_ROW_H, row.target))
        y += INDEX_ROW_H
    _footer(qp, project, page, page_index, page_count)
    return hotspots


def _paint_revisions(
    qp: QtPainter,
    project: Project,
    page: PackagePage,
    page_index: int,
    page_count: int,
) -> None:
    _frame(qp, page.width_mm, page.height_mm)
    y0 = _header(qp, "REVISION HISTORY", project, page, page_index, page_count)
    headers = ["REV", "DATE", "DESCRIPTION", "DRAWN", "CHK", "APP"]
    widths = _resolved_widths([18.0, 28.0, 0.0, 28.0, 22.0, 22.0], page.width_mm)
    x = MARGIN_X
    y = y0
    _row_cells(qp, x, y, widths, ROW_H + 1.0, headers, header=True)
    y += ROW_H + 1.0
    for rev in revision_rows(project.title_block):
        values = [rev.rev, rev.date, rev.description, rev.drawn, rev.checked, rev.approved]
        height = wrapped_row_height(values, widths, 2.15)
        _row_cells(qp, x, y, widths, height, values, header=False)
        y += height
    _footer(qp, project, page, page_index, page_count)


def _bom_has_needs_info_flag(rows: Sequence[dict[str, str]] | None) -> bool:
    for row in rows or []:
        flag = str(row.get("flag") or "").strip().casefold().replace("_", "-")
        if flag in {"needs-info", "needs info"}:
            return True
    return False


def _report_spec(
    kind: str, rows: Sequence[dict[str, str]] | None = None
) -> tuple[list[str], list[str], list[float]]:
    if kind == "bom":
        extra = _need_catalog_column(rows or [])
        flagged = _bom_has_needs_info_flag(rows)
        if extra:
            keys = ["item", "qty", "ref", "description", "manufacturer", "part_number", "catalog"]
            labels = ["ITEM", "QTY", "REFS", "DESCRIPTION", "MFR", "PART NO.", "CATALOG"]
            widths = [12.0, 10.0, 28.0, 0.0, 24.0, 26.0, 26.0]
        else:
            keys = ["item", "qty", "ref", "description", "manufacturer", "part_number"]
            labels = ["ITEM", "QTY", "REFS", "DESCRIPTION", "MFR", "PART NO."]
            widths = [12.0, 10.0, 30.0, 0.0, 26.0, 32.0]
        if flagged:
            keys.append("flag")
            labels.append("FLAG")
            widths.append(22.0)
        if any(str(row.get("panel") or "").strip() for row in (rows or [])):
            keys.insert(2, "panel")
            labels.insert(2, "PANEL")
            widths.insert(2, 18.0)
        return keys, labels, widths
    if kind == "interconnect":
        return (
            ["tag", "from_panel", "from_strip", "to_panel", "to_strip", "cable_type", "conductors", "length"],
            ["TAG", "FROM", "STRIP", "TO", "STRIP", "CABLE", "CND", "LENGTH"],
            [18.0, 22.0, 28.0, 22.0, 28.0, 0.0, 14.0, 24.0],
        )
    if kind == "wires":
        return (
            ["number", "from_device", "from_terminal", "to_device", "to_terminal", "color", "gauge", "sheet"],
            ["NUMBER", "FROM", "TERM", "TO", "TERM", "COLOR", "GAUGE", "SHEET"],
            [22.0, 28.0, 18.0, 28.0, 18.0, 20.0, 18.0, 0.0],
        )
    if kind == "from_to":
        return (
            ["wire_number", "from", "from_loc", "to", "to_loc", "size", "color", "type", "crossing"],
            ["NUMBER", "FROM", "LOC", "TO", "LOC", "SIZE", "COLOR", "TYPE", "CROSSING"],
            [18.0, 26.0, 24.0, 26.0, 24.0, 12.0, 14.0, 14.0, 0.0],
        )
    if kind == "xref":
        return (
            ["device", "ref", "element", "page_zone", "location", "terminals", "wires"],
            ["DEVICE", "REF", "ELEMENT", "PAGE-ZONE", "LOC", "TERM", "WIRES"],
            [20.0, 22.0, 20.0, 22.0, 18.0, 28.0, 0.0],
        )
    if kind == "terminals":
        return (
            ["strip", "terminal", "ref", "internal", "external", "jumper", "spare"],
            ["STRIP", "TERM", "REF", "INTERNAL", "EXTERNAL", "JUMPER", "SPARE"],
            [22.0, 18.0, 28.0, 0.0, 40.0, 24.0, 16.0],
        )
    if kind == "plc_io":
        return (
            ["module", "point", "type", "tag", "device", "spare", "description"],
            ["PLC", "ADDRESS", "TYPE", "TAG", "DEVICE", "SPARE", "DESCRIPTION"],
            [18.0, 18.0, 12.0, 22.0, 20.0, 14.0, 0.0],
        )
    return (
        ["severity", "check", "message", "sheet", "ref"],
        ["SEV", "CHECK", "MESSAGE", "SHEET", "REF"],
        [16.0, 36.0, 0.0, 24.0, 22.0],
    )


def _collect_report_rows(session: Any, kind: str) -> list[dict[str, str]]:
    rows, _note = _collect_report_rows_and_note(session, kind)
    return rows


def _rows_for_package_page(session: Any, page: PackagePage) -> list[dict[str, str]]:
    """The same row list the planner paginated for this page."""

    if page.report == "bom" and page.sheet_id:
        from panelcad.io.bom import bom_for_panel, grouped_rows, with_needs_info_section

        project = session.project
        if page.sheet_id == "project":
            rows = _bom_display_rows(grouped_rows(project, session.catalog, by_panel=False))
            return with_needs_info_section(rows, project, session.catalog)
        return _bom_display_rows(bom_for_panel(project, session.catalog, page.sheet_id))
    return _collect_report_rows(session, page.report)


def _paint_power(
    qp,
    session: Any,
    page: PackagePage,
    page_index: int,
    page_count: int,
    *,
    plan: PackagePlan | None = None,
) -> None:
    from panelcad.render.power_report import paint_supply_page

    budgets = plan._power_budgets if plan is not None else None
    if budgets is None:
        from panelcad.core.power_budget import analyze

        budgets = analyze(session.project, session.catalog, getattr(session, "parts", None))
    match = next((item for item in budgets if item.ref == page.sheet_id), None)
    if match is None:
        return
    paint_supply_page(
        qp,
        match,
        title=session.project.name,
        page_index=page_index,
        page_count=page_count,
        width=page.width_mm,
        height=page.height_mm,
    )


def _paint_report(
    qp: QtPainter,
    session: Any,
    plan: PackagePlan,
    page: PackagePage,
    page_index: int,
    page_count: int,
) -> None:
    _frame(qp, page.width_mm, page.height_mm)
    y0 = _header(qp, page.title.upper(), session.project, page, page_index, page_count)
    rows = plan._report_rows.get((page.report, page.sheet_id)) if plan is not None else None
    if rows is None:
        rows = _rows_for_package_page(session, page)
    keys, labels, raw_widths = _report_spec(page.report, rows)
    widths = _resolved_widths(raw_widths, page.width_mm)
    usable = page.width_mm - 2 * MARGIN_X
    slice_rows = rows[page.row_start : page.row_start + page.row_count]
    x = MARGIN_X
    y = y0
    _row_cells(qp, x, y, widths, ROW_H, labels, header=True)
    y += ROW_H
    if not slice_rows:
        _outline(qp, x, y, usable, ROW_H * 2, CELL)
        qp.text(x + 3.0, y + 8.0, "None", 2.6, align="start")
        y += ROW_H * 2
    else:
        for row in slice_rows:
            values = [str(row.get(key, "") or "") for key in keys]
            height = wrapped_row_height(values, widths, 2.15)
            _row_cells(qp, x, y, widths, height, values, header=False)
            y += height
    if page.note:
        qp.text(x, y + 6.0, page.note, 2.4, align="start")
    _footer(qp, session.project, page, page_index, page_count)


def _row_cells(
    qp: QtPainter,
    x: float,
    y: float,
    widths: list[float],
    h: float,
    values: Iterable[str],
    *,
    header: bool,
    colors: Sequence[str | None] | None = None,
) -> None:
    cx = x
    size = 1.9 if header else 2.15
    for index, (w, val) in enumerate(zip(widths, values, strict=True)):
        _outline(qp, cx, y, w, h, HEADER if header else CELL)
        color = None if not colors or index >= len(colors) else colors[index]
        if header:
            qp.text(cx + 1.4, y + h / 2.0 + 0.85, str(val or ""), size, align="start", color=color)
        else:
            ty = y + LINE_H
            for line in wrap_lines(str(val or ""), w, size):
                qp.text(cx + 1.4, ty, line, size, align="start", color=color)
                ty += LINE_H
        cx += w
