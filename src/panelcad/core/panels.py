"""Several control panels in one project.

A file with no ``panels`` list is one unnamed panel. Saving that file does
not add the list. Enclosure millimetres come only from
``libraries/enclosures.yaml``. This module does not size a box.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.models import Component, Panel, Project, Rail, Sheet
from panelcad.library.enclosures import StandardEnclosure, load_enclosures

LAYOUT_KINDS = frozenset({"panel_layout", "door_layout", "terminal"})
SCHEMATIC_TAG_KINDS = frozenset({"schematic", "io", "network"})


def panel_tag(panel: Panel) -> str:
    return str(panel.tag or panel.id).strip()


def find_panel(project: Project, token: str) -> Panel | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    key = raw.casefold()
    for panel in project.panels:
        if panel.id.casefold() == key or str(panel.tag or "").casefold() == key:
            return panel
    return None


def require_panel(project: Project, token: str) -> Panel:
    found = find_panel(project, token)
    if found is None:
        raise KeyError(f"no panel '{token}'")
    return found


def lookup_box(boxes: list[StandardEnclosure], token: str) -> StandardEnclosure | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    key = raw.casefold()
    for box in boxes:
        if box.id.casefold() == key or str(box.catalog or "").casefold() == key:
            return box
    return None


def require_catalog_box(token: str, project_dir: Any = None) -> StandardEnclosure:
    """A row from the enclosure table. Free millimetres are refused."""

    raw = str(token or "").strip()
    if not raw:
        raise ValueError(
            "Pick an enclosure id or catalog number from libraries/enclosures.yaml. "
            "PanelCAD does not invent enclosure millimetres."
        )
    box = lookup_box(load_enclosures(project_dir).boxes, raw)
    if box is None:
        raise ValueError(
            f"'{raw}' is not in libraries/enclosures.yaml. "
            "PanelCAD does not invent enclosure millimetres."
        )
    return box


def resolve_component_panel(project: Project, sheet: Sheet | None, comp: Component | None) -> Panel | None:
    """The panel a device belongs to. Empty on a single-panel file."""

    if not project.panels or comp is None:
        return None
    token = str(comp.panel or "").strip()
    if not token and sheet is not None and sheet.kind in LAYOUT_KINDS:
        token = str(sheet.panel or "").strip()
    if not token:
        return None
    return find_panel(project, token)


def component_panel_tag(project: Project, sheet: Sheet | None, comp: Component | None) -> str:
    panel = resolve_component_panel(project, sheet, comp)
    return panel_tag(panel) if panel is not None else ""


def panel_backpanel_chosen(project: Project, sheet: Sheet | None) -> bool:
    """True when this panel backpanel has a real enclosure, not the page outline.

    A catalog id from Size Enclosure or ``set_panel_enclosure`` counts.
    The generic page outline does not, so Circuit Builder may still size it.
    """

    if sheet is None or sheet.kind != "panel_layout":
        return False
    if sheet_panel(project, sheet) is None:
        return False
    from panelcad.core.layout import enclosure_is_sized

    return enclosure_is_sized(sheet.enclosure, sheet.page)


def sheet_panel(project: Project, sheet: Sheet | None) -> Panel | None:
    if sheet is None or not project.panels:
        return None
    return find_panel(project, sheet.panel)


def iec_system(project: Project, panel: Panel) -> str:
    return str(panel.system or project.location_system or "").strip().lstrip("=")


def iec_designation(project: Project, panel: Panel, product: str) -> str:
    """IEC 81346 ``=system+location-product``. The product id itself is unchanged."""

    system = iec_system(project, panel)
    loc = panel_tag(panel)
    body = product if str(product).startswith("-") else f"-{product}"
    location = f"+{loc}" if loc else ""
    function = f"={system}" if system else ""
    return f"{function}{location}{body}"


def _drawing_is_iec(project: Project, sheet: Sheet | None) -> bool:
    from panelcad.core.refs import sheet_drawing_standard

    return sheet_drawing_standard(project, sheet) == "IEC"


def schematic_tag_text(project: Project, sheet: Sheet, comp: Component) -> str:
    """Tag drawn on a schematic. NEMA keeps the device id. IEC adds =system+location."""

    panel = resolve_component_panel(project, sheet, comp)
    if panel is None or sheet.kind not in SCHEMATIC_TAG_KINDS:
        return comp.id
    if not _drawing_is_iec(project, sheet):
        return comp.id
    return iec_designation(project, panel, comp.id)


def schematic_location_caption(project: Project, sheet: Sheet, comp: Component) -> str:
    """NEMA location line. IEC already carries the location in the tag."""

    if sheet.kind not in SCHEMATIC_TAG_KINDS:
        return ""
    panel = resolve_component_panel(project, sheet, comp)
    if panel is None or _drawing_is_iec(project, sheet):
        return ""
    return panel_tag(panel)


def cross_panel_tag(
    project: Project,
    src_sheet: Sheet | None,
    src_comp: Component | None,
    dest_sheet: Sheet | None,
    dest_comp: Component | None,
) -> str:
    """Destination panel tag when the two ends are on different panels."""

    if len(project.panels) < 2:
        return ""
    src = resolve_component_panel(project, src_sheet, src_comp)
    dst = resolve_component_panel(project, dest_sheet, dest_comp)
    if src is None:
        src = sheet_panel(project, src_sheet)
    if dst is None:
        dst = sheet_panel(project, dest_sheet)
    if src is None or dst is None or src.id == dst.id:
        return ""
    return panel_tag(dst)


def device_wired_to(sheet: Sheet, arrow_id: str) -> Component | None:
    """The device on the same sheet wired to an off-page arrow."""

    token = str(arrow_id or "").strip()
    if not token:
        return None
    prefix = token + "."
    for wire in sheet.wires:
        ends = (str(wire.frm or ""), str(wire.to or ""))
        if not any(end == token or end.startswith(prefix) for end in ends):
            continue
        for end in ends:
            ref = end.split(".", 1)[0]
            if not ref or ref == token:
                continue
            try:
                return sheet.component(ref)
            except KeyError:
                continue
    return None


def offpage_other_panel(project: Project, sheet: Sheet, comp: Component) -> str:
    """Panel tag to print on an off-page arrow when the other end is another panel."""

    dest_id = str(comp.properties.get("dest_sheet") or "").strip()
    dest_ref = str(comp.properties.get("dest_ref") or "").strip()
    dest_sheet: Sheet | None = None
    dest_comp: Component | None = None
    if dest_id:
        try:
            dest_sheet = project.sheet(dest_id)
        except KeyError:
            dest_sheet = None
    if dest_sheet is not None and dest_ref:
        try:
            dest_comp = dest_sheet.component(dest_ref)
        except KeyError:
            dest_comp = None
    src_comp = device_wired_to(sheet, comp.id)
    return cross_panel_tag(project, sheet, src_comp, dest_sheet, dest_comp)


def package_sheet_order(project: Project) -> list[Sheet]:
    """Print order. Two or more panels group each panel's sheets together.

    Sheets with no panel stay first, in their saved order. A single-panel
    file keeps the saved order.
    """

    sheets = list(project.sheets)
    if len(project.panels) < 2:
        return sheets
    index = {panel.id: i for i, panel in enumerate(project.panels)}

    def sort_key(sheet: Sheet) -> tuple[int, int]:
        pid = str(sheet.panel or "")
        group = -1 if not pid else index.get(pid, 1000)
        return (group, sheets.index(sheet))

    return sorted(sheets, key=sort_key)


def package_sheet_title(project: Project, sheet: Sheet, title: str) -> str:
    if len(project.panels) < 2:
        return title
    panel = sheet_panel(project, sheet)
    if panel is None:
        return title
    tag = panel_tag(panel)
    if not tag or title.startswith(tag):
        return title
    return f"{tag} — {title}"


def fit_backpanel_rails(sheet: Sheet) -> None:
    """Three DIN rails inside a catalog backpanel. Skips a sheet that already has devices."""

    if sheet.components or sheet.enclosure is None:
        return
    enc = sheet.enclosure
    pw = float(enc.panel_width_mm or 0.0) or float(enc.width_mm)
    ph = float(enc.panel_height_mm or 0.0) or float(enc.height_mm)
    if pw < 40.0 or ph < 40.0:
        return
    from panelcad.core.layout import _draw_scale

    scale = _draw_scale(enc)
    ox = float(enc.x) + max(0.0, (float(enc.width_mm) - pw) / 2.0) * scale
    oy = float(enc.y) + max(0.0, (float(enc.height_mm) - ph) / 2.0) * scale
    inset = 16.0 * scale
    length = max(40.0 * scale, pw * scale - 2.0 * inset)
    rx = ox + inset
    ys = (
        oy + 40.0 * scale,
        oy + ph * scale * 0.45,
        oy + max(50.0 * scale, ph * scale - 36.0 * scale),
    )
    sheet.rails = [Rail(id=f"r{i}", x=rx, y=y, length_mm=length) for i, y in enumerate(ys, start=1)]


def interconnect_rows(project: Project) -> list[dict[str, str]]:
    """Cable schedule for cables that run from one panel to another."""

    rows: list[dict[str, str]] = []
    for cable in project.cables:
        if cable.kind != "interpanel":
            continue
        src = find_panel(project, cable.from_panel)
        dst = find_panel(project, cable.to_panel)
        length = "" if cable.length is None else f"{cable.length:g}"
        unit = cable.length_unit if cable.length is not None else ""
        rows.append(
            {
                "tag": cable.tag,
                "from_panel": panel_tag(src) if src is not None else cable.from_panel,
                "from_strip": cable.from_strip,
                "to_panel": panel_tag(dst) if dst is not None else cable.to_panel,
                "to_strip": cable.to_strip,
                "cable_type": cable.cable_type,
                "conductors": str(cable.conductors or ""),
                "gauge": cable.gauge,
                "length": f"{length} {unit}".strip(),
                "route": cable.route,
            }
        )
    return rows


INTERCONNECT_COLUMNS: tuple[str, ...] = (
    "tag",
    "from_panel",
    "from_strip",
    "to_panel",
    "to_strip",
    "cable_type",
    "conductors",
    "gauge",
    "length",
    "route",
)
