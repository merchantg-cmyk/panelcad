"""Derived coil / contact / off-page cross-references.

Locations use the printed sheet zone grid. NEMA sheet/line numbers follow
rung pitch (``sheet*100 + rung``); IEC stays ``/sheet.column``. Display text
is computed on the fly. Opening a file never writes ``properties.xref``;
``update_cross_references`` is the explicit persist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from panelcad.core.electrical import (
    SCHEMATIC_KINDS,
    contact_state,
    element_kind,
    family_key,
    is_coil_only_device,
    is_power_pole_device,
    symbol_role,
)
from panelcad.core.models import Component, Device, Project, Sheet
from panelcad.core.refs import sheet_drawing_standard
from panelcad.core.zones import (
    DEFAULT_RUNG_PITCH_MM,
    nema_line_at,
    rung_index_at,
    sheet_number,
    zone_at,
)
from panelcad.library.catalog import Catalog, normalize_standard

NC_MARKS = ("underline", "bar", "parentheses")
RUNG_NUMBER_MODES = ("used", "all")
NcMark = Literal["underline", "bar", "parentheses"]
XrefStyle = Literal["nema", "iec"]
RungNumbers = Literal["used", "all"]

_CHILD_RE = re.compile(r"^([A-Za-z]+\d+)[-.](\d+)$")
_COUNT_RE = re.compile(r"(\d+)\s*(?:NO|NC|CO|C/?O|CHANGEOVER)?", re.I)
_OFFPAGE_HINTS = ("offpage", "connection.offpage")


@dataclass
class Location:
    """One symbol's printed coordinate."""

    sheet_id: str
    sheet_no: int
    row: str
    col: int
    row_index: int
    line: int
    x: float
    y: float

    def nema(self) -> str:
        return str(self.line)

    def iec(self) -> str:
        return f"/{self.sheet_no}.{self.col}"

    def zone(self) -> str:
        return f"{self.row}{self.col}"

    def display(self, style: str) -> str:
        return self.iec() if style == "iec" else self.nema()


@dataclass
class XrefMember:
    sheet: Sheet
    comp: Component
    device: Device
    role: str
    state: str
    contact_no: str
    family: str
    location: Location
    element: str = ""


@dataclass
class XrefFamily:
    key: str
    coils: list[XrefMember] = field(default_factory=list)
    contacts: list[XrefMember] = field(default_factory=list)
    anchors: list[XrefMember] = field(default_factory=list)

    @property
    def members(self) -> list[XrefMember]:
        return [*self.coils, *self.anchors, *self.contacts]

    @property
    def parents(self) -> list[XrefMember]:
        return [*self.coils, *self.anchors]


@dataclass
class XrefAnnotation:
    """One clickable / drawable reference label (content only; render places it)."""

    sheet_id: str
    owner: str
    kind: str  # coil, contact, offpage, mirror
    style: str
    lines: list[str]
    marks: list[str]
    jump_sheet: str
    jump_ref: str
    jump_x: float
    jump_y: float
    prefer: str
    anchor_x: float
    anchor_y: float
    size: float = 2.0
    columns: tuple[list[str], list[str]] | None = None
    # IEC contact-mirror rows: {state, terminals, location}
    mirror_rows: list[dict[str, str]] | None = None


def normalize_nc_mark(value: str | None, *, default: str = "underline") -> str:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
    if token in {"underline", "under", "us", "nema"}:
        return "underline"
    if token in {"bar", "overbar", "over", "macron"}:
        return "bar"
    if token in {"parentheses", "parens", "paren", "brackets"}:
        return "parentheses"
    return default if default in NC_MARKS else "underline"


def normalize_rung_numbers(value: str | None, *, default: str = "used") -> str:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
    if token in {"used", "occupied", "content", "busy"}:
        return "used"
    if token in {"all", "every", "each"}:
        return "all"
    return default if default in RUNG_NUMBER_MODES else "used"


def _rung_pitch(raw: Any) -> float:
    try:
        pitch = float(getattr(raw, "rung_pitch_mm", DEFAULT_RUNG_PITCH_MM) or DEFAULT_RUNG_PITCH_MM)
    except (TypeError, ValueError):
        return DEFAULT_RUNG_PITCH_MM
    return pitch if pitch > 0.5 else DEFAULT_RUNG_PITCH_MM


def resolve_settings(project: Project) -> Any:
    from panelcad.core.models import CrossReference

    raw = getattr(project, "cross_reference", None)
    if raw is None:
        return CrossReference()
    return CrossReference(
        enabled=bool(getattr(raw, "enabled", True)),
        nc_mark=normalize_nc_mark(getattr(raw, "nc_mark", None)),  # type: ignore[arg-type]
        rung_pitch_mm=_rung_pitch(raw),
        rung_numbers=normalize_rung_numbers(getattr(raw, "rung_numbers", None)),  # type: ignore[arg-type]
    )


def set_settings(
    project: Project,
    *,
    nc_mark: str | None = None,
    enabled: bool | None = None,
    rung_pitch_mm: float | None = None,
    rung_numbers: str | None = None,
) -> Any:
    from panelcad.core.models import CrossReference

    current = resolve_settings(project)
    if nc_mark is not None:
        current.nc_mark = normalize_nc_mark(nc_mark)  # type: ignore[assignment]
    if enabled is not None:
        current.enabled = bool(enabled)
    if rung_pitch_mm is not None:
        current.rung_pitch_mm = _rung_pitch(type("P", (), {"rung_pitch_mm": rung_pitch_mm})())
    if rung_numbers is not None:
        current.rung_numbers = normalize_rung_numbers(rung_numbers)  # type: ignore[assignment]
    project.cross_reference = current
    return current


def settings_payload(project: Project) -> dict[str, Any]:
    settings = resolve_settings(project)
    return {
        "enabled": bool(settings.enabled),
        "nc_mark": settings.nc_mark,
        "nc_marks": list(NC_MARKS),
        "default_nc_mark": "underline",
        "rung_pitch_mm": float(settings.rung_pitch_mm),
        "default_rung_pitch_mm": DEFAULT_RUNG_PITCH_MM,
        "rung_numbers": settings.rung_numbers,
        "rung_number_modes": list(RUNG_NUMBER_MODES),
        "styles": ["nema", "iec"],
    }


def xref_style(project: Project, sheet: Sheet | None = None) -> str:
    std = sheet_drawing_standard(project, sheet)
    token = normalize_standard(std, default="NEMA/ANSI")
    return "iec" if token == "IEC" else "nema"


def _device(catalog: Catalog, comp: Component) -> Device | None:
    return catalog.devices.get(comp.library_id)


def is_offpage_component(comp: Component, device: Device | None) -> bool:
    lid = (comp.library_id or "").lower()
    if any(hint in lid for hint in _OFFPAGE_HINTS):
        return True
    if device is None:
        return False
    cat = (device.category or "").lower()
    return cat.startswith("connection.offpage") or "offpage" in lid


def component_rung_y(comp: Component, device: Device | None) -> float:
    """World Y of the left-to-right conductor through the symbol (the rung)."""

    if device is None:
        return float(comp.y)
    pins = [pin for pin in device.pins if (pin.side or "") in {"left", "right"}]
    if not pins:
        return float(comp.y) + float(device.schematic_height) * comp.visual_scale() * 0.5
    prefer = {"A1", "A", "3", "1", "13", "21", "95"}
    pin = next((item for item in pins if str(item.id) in prefer), pins[0])
    from panelcad.render.sheet import pin_world

    return float(pin_world(comp, pin)[1])


def location_of(project: Project, sheet: Sheet, x: float, y: float) -> Location:
    sheet_no = sheet_number(project.sheets, sheet.id) or 1
    row, col, row_i = zone_at(sheet, x, y)
    return Location(
        sheet_id=sheet.id,
        sheet_no=sheet_no,
        row=row,
        col=col,
        row_index=row_i,
        line=nema_line_at(sheet, y, sheet_no, project=project),
        x=float(x),
        y=float(y),
    )


def component_location(
    project: Project,
    sheet: Sheet,
    comp: Component,
    device: Device | None = None,
) -> Location:
    y = component_rung_y(comp, device) if device is not None else float(comp.y)
    x = float(comp.x)
    # A 90° column symbol stores its right edge in comp.x, so the zone column
    # is the rung pin, which is where the contact is drawn.
    if device is not None and int(comp.rotation or 0) in {90, 270}:
        pins = [pin for pin in device.pins if (pin.side or "") in {"left", "right"}]
        if pins:
            prefer = {"A1", "A", "3", "1", "13", "21", "95", "11"}
            pin = next((item for item in pins if str(item.id) in prefer), pins[0])
            from panelcad.render.sheet import pin_world

            x, y = pin_world(comp, pin)
    return location_of(project, sheet, x, y)


def _symbol_pin_ids(comp: Component, device: Device | None) -> list[str]:
    """Pin ids as drawn on the symbol (overrides, then library id)."""

    if device is None:
        return []
    from panelcad.core.pins import pin_caption

    hidden = {str(item) for item in (comp.properties.get("hidden_pins") or [])}
    drawn: list[str] = []
    for pin in device.pins:
        if str(pin.id) in hidden:
            continue
        text = str(pin_caption(pin, comp) or pin.id).strip()
        if text:
            drawn.append(text)
    return drawn


def contact_terminals(comp: Component, device: Device | None) -> str:
    """IEC-style terminal pair (13-14, 21-22) for a contact mirror row.

    The numbers on the symbol win. A child tagged K4-18 that is still the
    13/14 aux block must print 13-14, not 17-18 from the id suffix.
    """

    explicit = str(comp.properties.get("terminals") or "").strip()
    if explicit:
        return explicit
    drawn = _symbol_pin_ids(comp, device)
    if len(drawn) >= 2:
        return "-".join(drawn[:2]) if len(drawn) == 2 else "-".join(drawn)
    if len(drawn) == 1:
        return drawn[0]
    raw = contact_number(comp, device)
    if raw.isdigit():
        number = int(raw)
        even = number if number % 2 == 0 else number + 1
        return f"{even - 1}-{even}"
    if device is None:
        return raw
    nums = sorted(int(pin.id) for pin in device.pins if str(pin.id).isdigit())
    if len(nums) >= 2:
        return f"{nums[0]}-{nums[1]}"
    return raw


def contact_number(comp: Component, device: Device | None) -> str:
    """Aux contact designation (13, 21, 95) from the child tag, a property, or pins."""

    raw = str(comp.properties.get("contact_no") or comp.properties.get("contact_number") or "").strip()
    if raw:
        return raw
    match = _CHILD_RE.match(comp.id)
    if match:
        return match.group(2)
    if device is None:
        return ""
    nums: list[int] = []
    for pin in device.pins:
        token = str(pin.id).strip()
        if token.isdigit():
            nums.append(int(token))
    if not nums:
        return ""
    nums.sort()
    first = nums[0]
    if first % 2 == 0:
        return str(first - 1)
    return str(first)


def library_contact_count(device: Device | None) -> int | None:
    """How many schematic contacts the catalog part allows. None = unknown."""

    if device is None:
        return None
    raw = device.properties.get("contacts")
    if raw is None:
        raw = device.properties.get("contact_count")
    parsed = _parse_contact_count(raw)
    if parsed is not None:
        return parsed
    pairs = _aux_pin_pairs(device)
    return pairs if pairs else None


def _parse_contact_count(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return max(0, int(raw))
    text = str(raw).strip()
    if not text:
        return None
    found = [int(n) for n in _COUNT_RE.findall(text)]
    if found:
        return sum(found)
    return None


def _aux_pin_pairs(device: Device) -> int:
    """Catalog aux contact pairs. Trip/OL blades 95–98 are not aux allowance."""

    nums = []
    for pin in device.pins:
        token = str(pin.id).strip()
        if token.isdigit() and 13 <= int(token) < 95:
            nums.append(int(token))
    if not nums:
        return 0
    odds = {n if n % 2 else n - 1 for n in nums}
    return len(odds)


def mark_nc(text: str, state: str, nc_mark: str) -> str:
    """Apply the configured NC glyph. Underline/bar stay as the plain number;
    the mark is drawn by the renderer. Parentheses wrap the text."""

    if str(state or "").upper() != "NC":
        return text
    if nc_mark == "parentheses":
        return f"({text})"
    return text


def nc_draw_mark(state: str, nc_mark: str) -> str:
    if str(state or "").upper() != "NC":
        return ""
    if nc_mark == "parentheses":
        return ""
    return nc_mark if nc_mark in {"underline", "bar"} else "underline"


def _member(project: Project, sheet: Sheet, comp: Component, device: Device, family: str) -> XrefMember:
    role = symbol_role(device)
    if role == "contact":
        stored = "contact"
    elif role in {"coil", "parent"}:
        stored = "coil"
    else:
        stored = role
    return XrefMember(
        sheet=sheet,
        comp=comp,
        device=device,
        role=stored,
        state=contact_state(device),
        contact_no=contact_number(comp, device) if stored == "contact" else "",
        family=family,
        location=component_location(project, sheet, comp, device),
        element=element_kind(comp, device),
    )


def xref_families(project: Project, catalog: Catalog) -> dict[str, XrefFamily]:
    groups: dict[str, XrefFamily] = {}
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if device is None or is_offpage_component(comp, device):
                continue
            key = family_key(comp, device)
            if key is None:
                continue
            family = groups.setdefault(key, XrefFamily(key=key))
            member = _member(project, sheet, comp, device, key)
            if member.role == "contact":
                family.contacts.append(member)
            elif member.role == "coil":
                family.coils.append(member)
            else:
                family.anchors.append(member)
    for family in groups.values():
        _separate_power_poles(family)
        family.coils.sort(key=lambda m: (m.location.sheet_no, m.location.line, m.comp.id))
        family.anchors.sort(key=lambda m: (m.location.sheet_no, m.location.line, m.comp.id))
        family.contacts.sort(key=lambda m: (m.location.sheet_no, m.location.line, m.comp.id))
    return groups


def _separate_power_poles(family: XrefFamily) -> None:
    """When a coil symbol exists, the power-pole symbol is not the coil line."""

    real = [member for member in family.coils if is_coil_only_device(member.device)]
    poles = [member for member in family.coils if is_power_pole_device(member.device)]
    if not real or not poles:
        return
    family.coils = [member for member in family.coils if member not in poles]
    for member in poles:
        member.role = "poles"
        family.anchors.append(member)


def coil_member(family: XrefFamily) -> XrefMember | None:
    """The coil cross-references point at. Power poles lose to a coil symbol."""

    real = [member for member in family.coils if is_coil_only_device(member.device)]
    if real:
        return real[0]
    if family.coils:
        return family.coils[0]
    for member in family.anchors:
        if is_power_pole_device(member.device) or member.role == "coil":
            return member
    return family.anchors[0] if family.anchors else None


def contact_list_label(contact: XrefMember, style: str, nc_mark: str) -> tuple[str, str]:
    """Forward-list text that names the contact, not only its line/column.

    Aux and trip on the same rung must not print the same number. Back-refs
    still use ``contact_coil_text`` (parent location only).
    """

    loc = contact.location.display(style)
    terminals = contact_terminals(contact.comp, contact.device)
    state = str(contact.state or "").upper()
    if terminals:
        body = f"{terminals} {loc}"
    elif state in {"NO", "NC"}:
        body = f"{state} {loc}"
    elif contact.element and contact.element not in {"contact", "other"}:
        body = f"{contact.element} {loc}"
    else:
        body = loc
    return mark_nc(body, contact.state, nc_mark), nc_draw_mark(contact.state, nc_mark)


def coil_contact_text(
    family: XrefFamily,
    style: str,
    nc_mark: str,
    *,
    skip: XrefMember | None = None,
) -> tuple[list[str], list[str]]:
    """Display lines and per-line NC draw marks for a coil's contact list."""

    lines: list[str] = []
    marks: list[str] = []
    for contact in family.contacts:
        if skip is not None and contact.comp.id == skip.comp.id and contact.sheet.id == skip.sheet.id:
            continue
        text, mark = contact_list_label(contact, style, nc_mark)
        lines.append(text)
        marks.append(mark)
    for member in family.anchors:
        if member.role != "poles":
            continue
        if skip is not None and member.comp.id == skip.comp.id and member.sheet.id == skip.sheet.id:
            continue
        loc = member.location.display(style)
        label = f"poles {loc}"
        if label not in lines:
            lines.append(label)
            marks.append("")
    return lines, marks


def iec_mirror_columns(family: XrefFamily, *, skip: XrefMember | None = None) -> tuple[list[str], list[str]]:
    no: list[str] = []
    nc: list[str] = []
    for row in iec_mirror_rows(family, skip=skip):
        if row["state"] == "NC":
            nc.append(row["location"])
        else:
            no.append(row["location"])
    return no, nc


def iec_mirror_rows(family: XrefFamily, *, skip: XrefMember | None = None) -> list[dict[str, str]]:
    """One row per contact: NO/NC glyph, terminals, /sheet.column."""

    rows: list[dict[str, str]] = []
    for contact in family.contacts:
        if skip is not None and contact.comp.id == skip.comp.id and contact.sheet.id == skip.sheet.id:
            continue
        state = "NC" if str(contact.state or "").upper() == "NC" else "NO"
        terminals = contact_terminals(contact.comp, contact.device)
        location = contact.location.iec()
        # Same terminals on the same sheet/column are one contact. Two aux_no
        # blocks that both draw 13/14 on different pages are two rows.
        if any(
            row["terminals"] == terminals and row["state"] == state and row["location"] == location
            for row in rows
        ):
            continue
        rows.append(
            {
                "state": state,
                "terminals": terminals,
                "location": location,
                "contact": contact.comp.id,
            }
        )
    return rows


def contact_coil_text(family: XrefFamily, style: str) -> str:
    parent = coil_member(family)
    if parent is None:
        return ""
    return parent.location.display(style)


def offpage_peer(project: Project, catalog: Catalog, sheet: Sheet, comp: Component) -> tuple[Sheet, Component] | None:
    dest_sheet_id = str(comp.properties.get("dest_sheet") or "").strip()
    dest_ref = str(comp.properties.get("dest_ref") or "").strip()
    if not dest_sheet_id:
        return None
    try:
        dest_sheet = project.sheet(dest_sheet_id)
    except KeyError:
        return None
    if dest_ref:
        try:
            return dest_sheet, dest_sheet.component(dest_ref)
        except KeyError:
            pass
    dest_pin = str(comp.properties.get("dest_pin") or "").strip()
    if dest_pin:
        try:
            return dest_sheet, dest_sheet.component(dest_pin.split(".")[0])
        except KeyError:
            pass
    link = str(comp.properties.get("link") or "")
    if "|" in link:
        # The link names both ends. Use the far end: the end on this sheet is
        # the arrow's own conductor, and resolving it made an arrow point at
        # its own page.
        for other in link.split("|", 1):
            if ":" not in other:
                continue
            sid, rest = other.split(":", 1)
            if sid == sheet.id or rest.startswith("rail:"):
                continue
            ref = rest.split(".", 1)[0]
            try:
                page = project.sheet(sid)
                return page, page.component(ref)
            except KeyError:
                continue
    del catalog
    return None


def offpage_partner(project: Project, catalog: Catalog, sheet: Sheet, comp: Component) -> tuple[Sheet, Component] | None:
    """The matching arrow on the far page: same link, on the sheet this one names.

    A continuation feed (``sheet:rail:0V|sheet-2:rail:0V``) ends on a rail, not
    a device. Its pair of arrows is still one valid reference.
    """

    link = str(comp.properties.get("link") or "").strip()
    dest_sheet_id = str(comp.properties.get("dest_sheet") or "").strip()
    if "|" not in link or not dest_sheet_id or dest_sheet_id == sheet.id:
        return None
    # Only a rail-to-rail feed pairs by arrows alone. An end that names a
    # device must resolve to that device, so deleting it is still broken.
    far_ends = [part.split(":", 1)[1] for part in link.split("|") if part.split(":", 1)[0] == dest_sheet_id and ":" in part]
    if not far_ends or not all(end.startswith("rail:") for end in far_ends):
        return None
    try:
        page = project.sheet(dest_sheet_id)
    except KeyError:
        return None
    for other in page.components:
        if other is comp or str(other.properties.get("link") or "").strip() != link:
            continue
        if not is_offpage_component(other, _device(catalog, other)):
            continue
        if str(other.properties.get("dest_sheet") or "").strip() != sheet.id:
            continue
        return page, other
    return None


def offpage_from_match(project: Project, catalog: Catalog, sheet: Sheet, comp: Component) -> tuple[Sheet, Component] | None:
    """The FROM arrow on the far page that answers this TO arrow (same link, names this page)."""

    link = str(comp.properties.get("link") or "").strip()
    dest_sheet_id = str(comp.properties.get("dest_sheet") or "").strip()
    if not link or not dest_sheet_id or dest_sheet_id == sheet.id:
        return None
    try:
        page = project.sheet(dest_sheet_id)
    except KeyError:
        return None
    for other in page.components:
        if other is comp or str(other.properties.get("link") or "").strip() != link:
            continue
        if not is_offpage_component(other, _device(catalog, other)):
            continue
        return page, other
    # A FROM arrow drawn by hand may carry its own link. It still answers this
    # TO arrow when it points back at this page under the same tag.
    tag = str(comp.properties.get("tag") or "").strip()
    if tag:
        for other in page.components:
            if other is comp or str(other.properties.get("tag") or "").strip() != tag:
                continue
            if str(other.properties.get("dest_sheet") or "").strip() != sheet.id:
                continue
            role = str(other.properties.get("role") or "").lower()
            if role != "from" and "offpage_from" not in (other.library_id or "").lower():
                continue
            if not is_offpage_component(other, _device(catalog, other)):
                continue
            return page, other
    return None


def offpage_needs_from(project: Project, catalog: Catalog, sheet: Sheet, comp: Component) -> bool:
    """A TO arrow that names a device pin on another page, with no FROM arrow there."""

    role = str(comp.properties.get("role") or "").lower()
    lid = (comp.library_id or "").lower()
    if role == "from" or "offpage_from" in lid:
        return False
    if role != "to":
        return False
    if not str(comp.properties.get("dest_pin") or "").strip():
        return False
    if not str(comp.properties.get("link") or "").strip():
        return False
    peer = offpage_peer(project, catalog, sheet, comp)
    if peer is None:
        return False
    return offpage_from_match(project, catalog, sheet, comp) is None


def offpage_callout(project: Project, catalog: Catalog, sheet: Sheet, comp: Component) -> str:
    peer = offpage_peer(project, catalog, sheet, comp)
    role = str(comp.properties.get("role") or "").lower()
    lid = (comp.library_id or "").lower()
    going = role == "from" or "offpage_from" in lid
    prefix = "FROM" if going else "TO"
    if peer is None:
        dest_sheet_id = str(comp.properties.get("dest_sheet") or "").strip()
        dest_ref = str(comp.properties.get("dest_ref") or "").strip()
        if dest_sheet_id:
            no = sheet_number(project.sheets, dest_sheet_id) or dest_sheet_id
            extra = f" / {dest_ref}" if dest_ref else ""
            from panelcad.core.panels import offpage_other_panel

            other = offpage_other_panel(project, sheet, comp)
            if other:
                return f"{prefix} {other} {no}{extra}"
            return f"{prefix} {no}{extra}"
        return str(comp.properties.get("callout") or "").strip()
    dest_sheet, dest = peer
    dest_device = _device(catalog, dest)
    loc = component_location(project, dest_sheet, dest, dest_device)
    style = xref_style(project, dest_sheet)
    # Location.display already returns /sheet.col for IEC. Do not add another slash.
    loc_text = loc.iec().lstrip("/") if style == "iec" else loc.display(style)
    from panelcad.core.panels import offpage_other_panel

    other = offpage_other_panel(project, sheet, comp)
    if other:
        return f"{prefix} {other} {loc.sheet_no} / {loc_text}"
    return f"{prefix} {loc.sheet_no} / {loc_text}"


def xref_annotations(project: Project, catalog: Catalog, sheet: Sheet | None = None) -> list[XrefAnnotation]:
    """Derived labels. Empty when the operator has turned cross-reference off."""

    settings = resolve_settings(project)
    if not settings.enabled:
        return []
    wanted = sheet.id if sheet is not None else None
    out: list[XrefAnnotation] = []
    families = xref_families(project, catalog)
    for family in families.values():
        for member in family.members:
            if wanted and member.sheet.id != wanted:
                continue
            style = xref_style(project, member.sheet)
            if member.role == "contact":
                out.append(_contact_annotation(family, member, style))
            elif member.role == "coil":
                ann = _coil_annotation(family, member, style, settings.nc_mark)
                if ann is not None:
                    out.append(ann)
            else:
                ann = _anchor_annotation(family, member, style, settings.nc_mark)
                if ann is not None:
                    out.append(ann)
    for page in project.sheets:
        if wanted and page.id != wanted:
            continue
        if page.kind not in SCHEMATIC_KINDS:
            continue
        for comp in page.components:
            device = _device(catalog, comp)
            if not is_offpage_component(comp, device):
                continue
            text = offpage_callout(project, catalog, page, comp)
            if not text:
                continue
            peer = offpage_peer(project, catalog, page, comp)
            if peer is not None:
                dest_sheet, dest = peer
                jx, jy = dest.x, dest.y
                jump_sheet, jump_ref = dest_sheet.id, dest.id
            else:
                jx, jy = comp.x, comp.y
                jump_sheet, jump_ref = str(comp.properties.get("dest_sheet") or page.id), str(
                    comp.properties.get("dest_ref") or ""
                )
            w = (device.schematic_width if device else 32.0) * comp.visual_scale()
            h = (device.schematic_height if device else 12.0) * comp.visual_scale()
            going = str(comp.properties.get("role") or "").lower() == "from" or "offpage_from" in (
                comp.library_id or ""
            ).lower()
            if going:
                prefer, ax, ay = "right", comp.x + w + 3.2, comp.y + h * 0.45
            else:
                prefer, ax, ay = "left", comp.x - 2.4, comp.y + h * 0.45
            out.append(
                XrefAnnotation(
                    sheet_id=page.id,
                    owner=comp.id,
                    kind="offpage",
                    style=xref_style(project, page),
                    lines=[text],
                    marks=[""],
                    jump_sheet=jump_sheet,
                    jump_ref=jump_ref,
                    jump_x=jx,
                    jump_y=jy,
                    prefer=prefer,
                    anchor_x=ax,
                    anchor_y=ay,
                    size=2.0,
                )
            )
    return out


def _coil_annotation(
    family: XrefFamily, member: XrefMember, style: str, nc_mark: str
) -> XrefAnnotation | None:
    jump = family.contacts[0] if family.contacts else (family.parents[0] if family.parents else member)
    w = member.device.schematic_width * member.comp.visual_scale()
    h = member.device.schematic_height * member.comp.visual_scale()
    from panelcad.core.route import component_aabb

    box = component_aabb(
        member.comp.x,
        member.comp.y,
        w,
        h,
        int(member.comp.rotation or 0),
        bool(member.comp.mirror),
    )
    if style == "iec":
        rows = iec_mirror_rows(family)
        if not rows:
            return None
        no, nc = iec_mirror_columns(family)
        lines = [f"{row['terminals']} {row['location']}" for row in rows]
        token = f"{getattr(member.device, 'id', '')} {member.comp.library_id}".lower()
        if "overload" in token:
            # The motor lands on the load pins, so a table under the relay
            # sits on that symbol. 95–98 hang past the right edge; centre the
            # table on that aux column, clear of the captions. The top of the
            # relay is K1's bay.
            from panelcad.render.sheet import pin_world

            aux = [pin for pin in member.device.pins if str(pin.id) in {"95", "96", "97", "98"}]
            if aux:
                ys = [pin_world(member.comp, pin)[1] for pin in aux]
                anchor_y = (min(ys) + max(ys)) * 0.5
            else:
                anchor_y = box[1] + 2.4
            # Half a mirror table (21) plus the 95–98 caption. Further right
            # lands on the next contactor; the search grid is 4 mm.
            anchor_x = box[2] + 6.0 + 21.0
        else:
            anchor_x = (box[0] + box[2]) * 0.5
            anchor_y = box[3] + 5.0
        return XrefAnnotation(
            sheet_id=member.sheet.id,
            owner=member.comp.id,
            kind="mirror",
            style=style,
            lines=lines,
            marks=[""] * len(lines),
            jump_sheet=jump.sheet.id,
            jump_ref=jump.comp.id,
            jump_x=jump.location.x,
            jump_y=jump.location.y,
            prefer="below",
            anchor_x=anchor_x,
            anchor_y=anchor_y,
            size=2.2,
            columns=(no, nc) if rows else None,
            mirror_rows=rows or None,
        )
    lines, marks = coil_contact_text(family, style, nc_mark)
    return XrefAnnotation(
        sheet_id=member.sheet.id,
        owner=member.comp.id,
        kind="coil",
        style=style,
        lines=lines,
        marks=marks,
        jump_sheet=jump.sheet.id,
        jump_ref=jump.comp.id,
        jump_x=jump.location.x,
        jump_y=jump.location.y,
        prefer="below",
        anchor_x=member.comp.x + w * 0.5,
        anchor_y=member.comp.y + h + 4.0,
        size=2.0,
    )


def _anchor_annotation(
    family: XrefFamily, member: XrefMember, style: str, nc_mark: str
) -> XrefAnnotation | None:
    """Location list (or IEC contact table) for a non-coil parent device."""

    others = [
        item
        for item in family.members
        if not (item.comp.id == member.comp.id and item.sheet.id == member.sheet.id)
    ]
    if not others:
        return None
    if all(item.sheet.id == member.sheet.id for item in others):
        # Same-sheet children already carry a back-ref. A table on a PB or
        # breaker is only needed when an element lives on another page.
        return None
    # IEC contact-mirror tables stay on coils / historic parents (K3/K4
    # header-column placement). Breakers, drives, and operators get the
    # same location list as a NEMA coil so a PB table cannot steal a column.
    jump = others[0]
    w = member.device.schematic_width * member.comp.visual_scale()
    h = member.device.schematic_height * member.comp.visual_scale()
    lines: list[str] = []
    marks: list[str] = []
    for item in others:
        if item.role == "contact":
            text, mark = contact_list_label(item, style, nc_mark)
            lines.append(text)
            marks.append(mark)
        else:
            loc = item.location.display(style)
            label = item.element if item.element and item.element not in {"other", item.role} else ""
            lines.append(f"{label} {loc}".strip() if label else loc)
            marks.append("")
    return XrefAnnotation(
        sheet_id=member.sheet.id,
        owner=member.comp.id,
        kind="coil",
        style=style,
        lines=lines,
        marks=marks,
        jump_sheet=jump.sheet.id,
        jump_ref=jump.comp.id,
        jump_x=jump.location.x,
        jump_y=jump.location.y,
        prefer="below",
        anchor_x=member.comp.x + w * 0.5,
        anchor_y=member.comp.y + h + 4.0,
        size=2.0,
    )


def _contact_annotation(family: XrefFamily, member: XrefMember, style: str) -> XrefAnnotation:
    text = contact_coil_text(family, style)
    coil = coil_member(family) or member
    w = member.device.schematic_width * member.comp.visual_scale()
    h = member.device.schematic_height * member.comp.visual_scale()
    from panelcad.core.route import component_aabb

    box = component_aabb(
        member.comp.x,
        member.comp.y,
        w,
        h,
        int(member.comp.rotation or 0),
        bool(member.comp.mirror),
    )
    # A 90° contact's tag is beside the symbol. The unrotated anchor landed
    # off the body, and the wide search then parked "/1.1" in the margin.
    rot = int(member.comp.rotation or 0) % 360
    if rot in {90, 270}:
        anchor_x = (box[0] + box[2]) * 0.5
        anchor_y = box[3] + 3.6
    else:
        anchor_x = member.comp.x + w * 0.5
        # Aux tags sit at y+h+3.2; the coil line goes on the next line under the tag.
        anchor_y = member.comp.y + h + 6.8
    return XrefAnnotation(
        sheet_id=member.sheet.id,
        owner=member.comp.id,
        kind="contact",
        style=style,
        lines=[text] if text else [],
        marks=[""],
        jump_sheet=coil.sheet.id,
        jump_ref=coil.comp.id,
        jump_x=coil.location.x,
        jump_y=coil.location.y,
        prefer="below",
        anchor_x=anchor_x,
        anchor_y=anchor_y,
        size=2.0,
    )


def cross_reference_rows(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """One row per coil or contact. Derived — does not write the project."""

    settings = resolve_settings(project)
    rows: list[dict[str, str]] = []
    for family in xref_families(project, catalog).values():
        for member in family.members:
            style = xref_style(project, member.sheet)
            if member.role == "contact":
                xref = contact_coil_text(family, style)
            elif member.role == "coil":
                lines, _marks = coil_contact_text(family, style, settings.nc_mark)
                xref = ", ".join(lines)
            else:
                peers = [
                    item
                    for item in family.members
                    if not (item.comp.id == member.comp.id and item.sheet.id == member.sheet.id)
                ]
                xref = ", ".join(item.location.display(style) for item in peers)
            loc = member.location
            rows.append(
                {
                    "parent": family.key,
                    "child": member.comp.id,
                    "role": member.role,
                    "element": member.element,
                    "state": member.state,
                    "contact_no": member.contact_no,
                    "sheet": member.sheet.id,
                    "sheet_no": str(loc.sheet_no),
                    "line": str(loc.line),
                    "zone": loc.zone(),
                    "location": loc.display(style),
                    "location_iec": loc.iec(),
                    "style": style,
                    "nc_mark": settings.nc_mark,
                    "xref": xref,
                    "x": f"{member.comp.x:g}",
                    "y": f"{member.comp.y:g}",
                }
            )
    return rows


def cross_reference_report(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """Coil-centric report: one row per contact, plus coils that have none."""

    settings = resolve_settings(project)
    rows: list[dict[str, str]] = []
    for family in xref_families(project, catalog).values():
        coil = coil_member(family)
        style = xref_style(project, coil.sheet if coil is not None else None)
        coil_loc = coil.location.display(style) if coil is not None else ""
        if family.contacts:
            for contact in family.contacts:
                cstyle = xref_style(project, contact.sheet)
                rows.append(
                    {
                        "coil": family.key,
                        "coil_sheet": coil.sheet.id if coil is not None else "",
                        "coil_location": coil_loc,
                        "coil_line": str(coil.location.line) if coil is not None else "",
                        "coil_zone": coil.location.zone() if coil is not None else "",
                        "contact": contact.comp.id,
                        "contact_sheet": contact.sheet.id,
                        "contact_location": contact.location.display(cstyle),
                        "contact_line": str(contact.location.line),
                        "contact_zone": contact.location.zone(),
                        "state": contact.state,
                        "contact_no": contact.contact_no,
                        "style": cstyle,
                        "nc_mark": settings.nc_mark,
                    }
                )
        elif coil is not None:
            rows.append(
                {
                    "coil": family.key,
                    "coil_sheet": coil.sheet.id,
                    "coil_location": coil_loc,
                    "coil_line": str(coil.location.line),
                    "coil_zone": coil.location.zone(),
                    "contact": "",
                    "contact_sheet": "",
                    "contact_location": "",
                    "contact_line": "",
                    "contact_zone": "",
                    "state": "",
                    "contact_no": "",
                    "style": style,
                    "nc_mark": settings.nc_mark,
                }
            )
    return rows


def update_cross_references(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """Write ``properties.xref`` / off-page callouts. Explicit persist only."""

    rows = cross_reference_rows(project, catalog)
    by_key = {(row["sheet"], row["child"]): row["xref"] for row in rows}
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if is_offpage_component(comp, device):
                text = offpage_callout(project, catalog, sheet, comp)
                if text:
                    comp.properties["callout"] = text
                continue
            xref = by_key.get((sheet.id, comp.id))
            if xref is None:
                continue
            if xref:
                comp.properties["xref"] = xref
            else:
                comp.properties.pop("xref", None)
    return rows


def _assigned_contact_limit(member: XrefMember) -> tuple[int, str, str] | None:
    """(count, part name, built-in phrase) from the assigned catalog part."""

    props = member.comp.properties or {}
    if not str(props.get("catalog_key") or props.get("part_number") or props.get("catalog") or "").strip():
        return None
    raw = props.get("contact_config")
    if raw in (None, ""):
        raw = props.get("contacts")
    parsed = _parse_contact_count(raw)
    if parsed is None:
        return None
    name = " ".join(
        bit
        for bit in (
            str(props.get("manufacturer") or "").strip(),
            str(props.get("catalog") or props.get("part_number") or "").strip(),
        )
        if bit
    )
    return parsed, name, _built_in_contacts(str(raw or ""), parsed)


def _built_in_contacts(raw: str, count: int) -> str:
    found = re.findall(r"(\d+)\s*(NO|NC|CO|C/?O)", str(raw or ""), re.I)
    if not found:
        return str(count)
    bits: list[str] = []
    for number, kind in found:
        token = kind.upper().replace("/", "")
        if token in {"CO", "C"}:
            token = "CO"
        bits.append(f"{int(number)} {token}")
    return " and ".join(bits)


def _contact_count_message(sheet_id: str, tag: str, used: int, limit: tuple[int, str, str]) -> str:
    count, part, built_in = limit
    if part:
        return (
            f"{tag} uses {used} contacts. {part} has {built_in} built in. "
            "Add an auxiliary contact block or pick a part with more contacts."
        )
    return f"{sheet_id}:{tag} uses {used} contacts; library allows {count}"


def _family_contact_allowance(family: XrefFamily) -> tuple[int, str, str] | None:
    """How many schematic contacts this device allows, and where that limit came from.

    A coil-only symbol's library ``contacts: 1NO`` is the generic coil, not a
    limit. An assigned catalog contact arrangement is a limit and names the
    part. Otherwise a split contactor uses the power-pole symbol. Operators
    are not limited.
    """

    if any(parent.role == "operator" for parent in family.parents):
        return None
    catalog_limits = [
        limit
        for parent in family.parents
        if (limit := _assigned_contact_limit(parent)) is not None
    ]
    if catalog_limits:
        return min(catalog_limits, key=lambda item: item[0])
    poles = [parent for parent in family.parents if is_power_pole_device(parent.device)]
    if poles:
        known = [
            count
            for parent in poles
            if (count := library_contact_count(parent.device)) is not None
        ]
        return (min(known), "", "") if known else None
    known = [
        count
        for parent in family.parents
        if not is_coil_only_device(parent.device)
        and (count := library_contact_count(parent.device)) is not None
    ]
    return (min(known), "", "") if known else None


def xref_problems(project: Project, catalog: Catalog) -> list[dict[str, Any]]:
    """Shop findings for the design-check engine (not structural validate)."""

    out: list[dict[str, Any]] = []
    for family in xref_families(project, catalog).values():
        if family.contacts and not family.parents:
            for contact in family.contacts:
                out.append(
                    {
                        "check": "contact_without_coil",
                        "severity": "warning",
                        "message": f"{contact.sheet.id}:{contact.comp.id} contact has no coil {family.key}",
                        "sheet": contact.sheet.id,
                        "ref": contact.comp.id,
                        "target": family.key,
                        "x": contact.comp.x,
                        "y": contact.comp.y,
                    }
                )
        allowance = _family_contact_allowance(family)
        used = [
            contact
            for contact in family.contacts
            if contact.element not in {"trip_alarm", "ol_contact"}
        ]
        if allowance is not None and len(used) > allowance[0]:
            coil = coil_member(family) or (family.parents[0] if family.parents else family.contacts[0])
            out.append(
                {
                    "check": "contact_count_exceeded",
                    "severity": "warning",
                    "message": _contact_count_message(coil.sheet.id, family.key, len(used), allowance),
                    "sheet": coil.sheet.id,
                    "ref": family.key,
                    "target": str(len(used)),
                    "x": coil.comp.x,
                    "y": coil.comp.y,
                }
            )
        seen: dict[str, XrefMember] = {}
        for contact in family.contacts:
            number = contact.contact_no
            if not number:
                continue
            prior = seen.get(number)
            if prior is None:
                seen[number] = contact
                continue
            out.append(
                {
                    "check": "duplicate_contact_number",
                    "severity": "warning",
                    "message": (
                        f"{contact.sheet.id}:{contact.comp.id} reuses contact {number} "
                        f"already on {prior.sheet.id}:{prior.comp.id}"
                    ),
                    "sheet": contact.sheet.id,
                    "ref": contact.comp.id,
                    "target": prior.comp.id,
                    "x": contact.comp.x,
                    "y": contact.comp.y,
                }
            )
    for sheet in project.sheets:
        if sheet.kind not in SCHEMATIC_KINDS:
            continue
        for comp in sheet.components:
            device = _device(catalog, comp)
            if not is_offpage_component(comp, device):
                continue
            dest_sheet = str(comp.properties.get("dest_sheet") or "").strip()
            dest_ref = str(comp.properties.get("dest_ref") or "").strip()
            dest_pin = str(comp.properties.get("dest_pin") or "").strip()
            link = str(comp.properties.get("link") or "").strip()
            if not (dest_sheet or dest_ref or dest_pin or link):
                continue
            if offpage_peer(project, catalog, sheet, comp) is not None:
                if offpage_needs_from(project, catalog, sheet, comp):
                    out.append(
                        {
                            "check": "offpage_missing_from",
                            "severity": "warning",
                            "message": (
                                f"{sheet.id}:{comp.id} TO arrow has no matching FROM arrow on "
                                f"{dest_sheet} at {dest_ref}.{dest_pin} (pair_offpage adds it)"
                            ),
                            "sheet": sheet.id,
                            "ref": comp.id,
                            "target": f"{dest_sheet}:{dest_ref}.{dest_pin}",
                            "x": comp.x,
                            "y": comp.y,
                        }
                    )
                continue
            if offpage_partner(project, catalog, sheet, comp) is not None:
                continue
            out.append(
                {
                    "check": "broken_offpage",
                    "severity": "warning",
                    "message": (
                        f"{sheet.id}:{comp.id} off-page reference "
                        f"{dest_sheet or '?'} / {dest_ref or dest_pin or link or '?'} is broken"
                    ),
                    "sheet": sheet.id,
                    "ref": comp.id,
                    "target": dest_sheet or dest_ref or dest_pin or link,
                    "x": comp.x,
                    "y": comp.y,
                }
            )
    return out


def occupied_rung_indices(project: Project, sheet: Sheet, catalog: Catalog | None) -> set[int]:
    """Rung indexes that have a symbol (or its conductor) on *sheet*."""

    found: set[int] = set()
    for comp in sheet.components:
        device = _device(catalog, comp) if catalog is not None else None
        y = component_rung_y(comp, device)
        found.add(rung_index_at(sheet, y, project=project))
    return found


def jump_row(annotation: XrefAnnotation) -> dict[str, str]:
    return {
        "sheet": annotation.jump_sheet,
        "ref": annotation.jump_ref,
        "x": f"{annotation.jump_x:g}",
        "y": f"{annotation.jump_y:g}",
        "message": " ".join(annotation.lines),
        "check": "xref",
    }
