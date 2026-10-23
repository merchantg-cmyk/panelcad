"""Reference copies: one physical device drawn again on another sheet.

A network or overview sheet shows devices that are drawn in full on their
own schematic sheet. Those copies are references to the primary device, not
a second device with the same tag. The duplicate-tag check skips them, and
the device index and printed sheet show a "see sheet" pointer for them.

A sheet is an overview when any of these hold:

* ``kind == "network"``. ``Session.layout_network`` writes this kind.
* ``Sheet.overview`` is true (set with ``Session.set_sheet_overview``).
* Its title has the word "overview" (the description is not used; it
  often only mentions an overview page elsewhere).

A rack view of an I/O module is also a reference. The module's I/O card on
an ``io`` sheet is the primary, and the same tag and part on any other
schematic sheet is the rack view. This is the pairing ``io_sheets`` already
draws with ``xref_rack`` / ``xref_io``.

Any copy can also be marked by hand with ``Session.mark_reference``. That
writes ``properties["reference_of"] = <primary sheet id>`` and keeps the
copy's wires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from panelcad.core.models import Component, Device, Project, Sheet
from panelcad.library.catalog import Catalog

REFERENCE_PROP = "reference_of"

# Sheets that never hold schematic device symbols. Their copies of a tag are
# footprints or terminals, and other checks own them.
_NON_SCHEMATIC = frozenset({"panel_layout", "door_layout", "terminal"})
_OVERVIEW_WORD = re.compile(r"\boverview\b", re.IGNORECASE)


@dataclass(frozen=True)
class ReferenceLink:
    """One reference copy and the primary it points at."""

    sheet: Sheet
    comp: Component
    primary_sheet: Sheet
    primary: Component
    reason: str  # "marked", "overview", or "rack"


def is_overview_sheet(sheet: Sheet) -> bool:
    if sheet.kind == "network":
        return True
    if bool(getattr(sheet, "overview", False)):
        return True
    return bool(_OVERVIEW_WORD.search(sheet.title or ""))


def reference_target(comp: Component) -> str:
    return str(comp.properties.get(REFERENCE_PROP) or "").strip()


def _device(catalog: Catalog, comp: Component) -> Device | None:
    return catalog.devices.get(comp.library_id)


def _io_module(device: Device | None) -> bool:
    if device is None:
        return False
    from panelcad.render.io_ladder import is_io_module

    try:
        return bool(is_io_module(device))
    except Exception:  # noqa: BLE001 - odd library rows are just not modules
        return False


def reference_links(project: Project, catalog: Catalog) -> list[ReferenceLink]:
    """Every reference copy in the project, in sheet order."""

    groups: dict[str, list[tuple[Sheet, Component]]] = {}
    for sheet in project.sheets:
        if sheet.kind in _NON_SCHEMATIC:
            continue
        for comp in sheet.components:
            groups.setdefault(comp.id, []).append((sheet, comp))
    links: list[ReferenceLink] = []
    for items in groups.values():
        if len(items) < 2:
            continue
        links.extend(_group_links(items, catalog))
    order = {sheet.id: i for i, sheet in enumerate(project.sheets)}
    links.sort(key=lambda link: (order.get(link.sheet.id, 0), link.comp.id))
    return links


def _group_links(items: list[tuple[Sheet, Component]], catalog: Catalog) -> list[ReferenceLink]:
    marked = [(s, c) for s, c in items if reference_target(c)]
    rest = [(s, c) for s, c in items if not reference_target(c)]
    overview = [(s, c) for s, c in rest if is_overview_sheet(s)]
    ordinary = [(s, c) for s, c in rest if not is_overview_sheet(s)]
    rack: list[tuple[Sheet, Component]] = []
    cards = [(s, c) for s, c in ordinary if s.kind == "io" and _io_module(_device(catalog, c))]
    if cards:
        libs = {c.library_id for _s, c in cards}
        rack = [(s, c) for s, c in ordinary if s.kind != "io" and c.library_id in libs]
        rack_ids = {id(c) for _s, c in rack}
        ordinary = [(s, c) for s, c in ordinary if id(c) not in rack_ids]
    if ordinary:
        primary = ordinary[0]
    elif cards:
        primary = cards[0]
    elif overview:
        primary = overview[0]
        overview = overview[1:]
    else:
        return []
    links: list[ReferenceLink] = []
    for sheet, comp in marked:
        target = reference_target(comp)
        found = next(
            ((s, c) for s, c in items if s.id == target and c is not comp and not reference_target(c)),
            None,
        )
        psheet, pcomp = found or primary
        if pcomp is comp:
            continue
        links.append(ReferenceLink(sheet, comp, psheet, pcomp, "marked"))
    for sheet, comp in overview:
        links.append(ReferenceLink(sheet, comp, primary[0], primary[1], "overview"))
    for sheet, comp in rack:
        links.append(ReferenceLink(sheet, comp, primary[0], primary[1], "rack"))
    return links


def reference_map(project: Project, catalog: Catalog) -> dict[int, ReferenceLink]:
    """``id(component)`` → link, for quick skips inside checks and painters."""

    return {id(link.comp): link for link in reference_links(project, catalog)}


def see_label(project: Project, link: ReferenceLink, catalog: Catalog) -> str:
    """Printed pointer for one reference copy, in the sheet's xref style."""

    from panelcad.core.xref import component_location, xref_style

    loc = component_location(project, link.primary_sheet, link.primary, _device(catalog, link.primary))
    if xref_style(project, link.sheet) == "iec":
        return loc.iec()
    return f"{loc.sheet_no}-{loc.zone()}"
