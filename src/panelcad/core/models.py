"""Project document models. This is the YAML schema."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel as PydanticBaseModel
from pydantic import ConfigDict, Field, field_validator

from panelcad.core.geometry import Page, Point
from panelcad.core.revisions import RevisionControl
from panelcad.version import FORMAT_VERSION


class BaseModel(PydanticBaseModel):
    """Project-document model. The core schema is built on first use.

    Importing the document used to generate every schema up front, inside the
    same step that loaded Pydantic plugins. Callers that only need a name or a
    helper do not pay for that.
    """

    model_config = ConfigDict(defer_build=True)

SheetKind = Literal["schematic", "panel_layout", "door_layout", "layout", "terminal", "network", "io", "wire_by_wire", "wire_schedule"]
PortKind = Literal["ethernet", "serial", "usb", "can", "fieldbus", "other"]
NetworkMedium = Literal["ethernet", "serial", "usb", "mixed", "other"]


class PinDef(BaseModel):
    id: str
    x: float
    y: float
    side: Literal["left", "right", "top", "bottom"] = "left"
    electrical: Literal["unspecified", "power", "input", "output", "passive"] = "passive"
    label: str = ""  # OEM terminal text; empty → show id
    jumper: str = ""  # same non-empty id on two pins: those pins are internally jumpered


class PinOverride(BaseModel):
    """Instance pin location. Library defaults apply when a pin id is omitted."""

    x: float
    y: float
    side: Literal["left", "right", "top", "bottom"] | None = None
    label: str | None = None


def infer_pin_side(x: float, y: float, width: float, height: float) -> str:
    """Nearest symbol edge — drives the auto-wire stub direction."""
    w = max(float(width), 1.0)
    h = max(float(height), 1.0)
    dists = {"left": x, "right": w - x, "top": y, "bottom": h - y}
    return min(dists, key=dists.get)


class CommPort(BaseModel):
    """Communications port on a PLC, HMI, drive, or similar."""

    id: str
    kind: PortKind = "serial"
    medium: str = ""
    protocol: str = ""
    connector: str = ""
    side: Literal["left", "right", "top", "bottom"] = "right"
    label: str = ""  # OEM silkscreen; empty → show id


class NetworkInfo(BaseModel):
    """Per-sheet comms page (Ethernet, RS-485, …)."""

    name: str = ""
    medium: NetworkMedium = "mixed"
    protocol: str = ""


MountKind = Literal["din_rail", "backplate", "door", "field", "none"]
DimSource = Literal["generic", "catalog", "measured"]


class PanelFootprint(BaseModel):
    """Physical size and how (or whether) the part sits in the enclosure."""

    width_mm: float = 18.0
    height_mm: float = 80.0
    depth_mm: float = 70.0
    din_rail: bool = True
    in_panel: bool = True
    mount: MountKind = "din_rail"
    dim_source: DimSource = "generic"
    # True when depth_mm was filled from GENERIC_BY_CATEGORY, not a published D.
    depth_from_generic: bool = False


class Rail(BaseModel):
    """TS35 DIN rail. (x, y) is the left end of the clip centerline, millimetres."""

    id: str
    x: float
    y: float
    length_mm: float = 500.0
    height_mm: float = 35.0  # drawn hat height (scaled with the sheet)
    type: Literal["ts35"] = "ts35"


class WireTray(BaseModel):
    """Wiring duct / wire tray. (x, y) is top-left, millimetres."""

    id: str
    x: float
    y: float
    length_mm: float = 400.0
    width_mm: float = 40.0
    orientation: Literal["h", "v"] = "h"


class Enclosure(BaseModel):
    """Backplate / box outline on a panel_layout sheet."""

    x: float = 20.0
    y: float = 20.0
    width_mm: float = 400.0
    height_mm: float = 500.0
    depth_mm: float = 200.0
    name: str = ""
    draw_scale: float = 1.0
    nema_type: str = ""
    material: str = ""
    catalog: str = ""
    manufacturer: str = ""
    mount: str = ""
    panel_width_mm: float = 0.0
    panel_height_mm: float = 0.0
    # True after set_enclosure(width and height), apply_enclosure, or Size Enclosure.
    # The page-outline box from a new panel sheet stays false.
    sized: bool = False


class BlockAttribute(BaseModel):
    """A field on a symbol block. The tag is the name; the value is filled in on insert."""

    tag: str
    x: float = 0.0
    y: float = 0.0
    h: float = 2.4


class Device(BaseModel):
    """A library object (symbol block + panel footprint + defaults)."""

    library_id: str
    id: str
    name: str
    category: str
    ref_prefix: str = "A"
    tags: list[str] = Field(default_factory=list)
    schematic_width: float = 20.0
    schematic_height: float = 40.0
    pins: list[PinDef] = Field(default_factory=list)
    primitives: list[Any] = Field(default_factory=list)  # Primitive, stored as dicts in YAML devices
    attributes: list[BlockAttribute] = Field(default_factory=list)
    ports: list[CommPort] = Field(default_factory=list)
    panel: PanelFootprint = Field(default_factory=PanelFootprint)
    footprint_primitives: list[Any] = Field(default_factory=list)
    footprint_attributes: list[BlockAttribute] = Field(default_factory=list)
    block_kind: str = ""  # parent, child, terminal, footprint — empty for older symbols
    properties: dict[str, Any] = Field(default_factory=dict)
    standard: str = "IEC"

    def pin(self, pin_id: str) -> PinDef:
        for p in self.pins:
            if p.id == pin_id:
                return p
        raise KeyError(f"{self.library_id} has no pin '{pin_id}'")


class Component(BaseModel):
    id: str
    library_id: str
    x: float
    y: float
    rotation: Literal[0, 90, 180, 270] = 0
    mirror: bool = False
    scale: float = 1.0
    ports: list[CommPort] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    pins_locked: bool = True
    pin_overrides: dict[str, PinOverride] = Field(default_factory=dict)
    picture: str = ""
    # Control panel this device belongs to (Panel.id). Empty on a single-panel file.
    panel: str = ""

    def visual_scale(self) -> float:
        try:
            s = float(self.scale)
        except (TypeError, ValueError):
            s = 1.0
        if s <= 0:
            s = 1.0
        return max(0.15, min(8.0, s))

    def pin_xy_id(self, pin_id: str, default: tuple[float, float]) -> tuple[float, float]:
        ov = self.pin_overrides.get(pin_id)
        if ov is None:
            return default
        return (ov.x, ov.y)

    def pin_xy(self, pin: PinDef) -> tuple[float, float]:
        return self.pin_xy_id(pin.id, (pin.x, pin.y))

    def pin_side(self, pin: PinDef) -> str:
        ov = self.pin_overrides.get(pin.id)
        if ov is not None and ov.side:
            return ov.side
        return pin.side


class WireNumbering(BaseModel):
    """Per-project automatic wire numbers. Omitted from YAML when it is the default."""

    scheme: Literal["sheet_line", "sequential", "iec"] = "sheet_line"
    enabled: bool = True
    start: int = 1
    # A feed-through terminal is one conductor. Set this to give each side its own number.
    split_feedthrough: bool = False


class NetWireSpec(BaseModel):
    """Optional size, color, and type for conductors on one net.

    A blank field does not override the same field on a wire. The project map
    key ``*`` is the fallback when that net has no entry of its own.
    """

    size: str = ""
    color: str = ""
    wire_type: str = Field(default="", alias="type")

    model_config = {"populate_by_name": True}


class CrossReference(BaseModel):
    """Per-project coil/contact cross-reference display. Omitted from YAML when default."""

    enabled: bool = True
    nc_mark: Literal["underline", "bar", "parentheses"] = "underline"
    # JIC/NEMA line = sheet*100 + rung index. 12.7 mm is 0.5 in.
    rung_pitch_mm: float = 12.7
    # Print a line number at every rung, or only rungs that have a symbol.
    rung_numbers: Literal["used", "all"] = "used"


class PhasePalette(BaseModel):
    """Stroke colors for L1, L2, and L3. Stored as #rrggbb."""

    l1: str = "#111111"
    l2: str = "#c42020"
    l3: str = "#1d4e9e"


class PhaseColors(BaseModel):
    """Project phase-color setting. Omitted from YAML when it is the default.

    NEMA picks ``nema_low`` below ``nema_high_voltage`` (208/240 V) and
    ``nema_high`` at or above it (480 V). Those two palettes are a US shop
    convention. IEC always uses ``iec``. Brown, black, and grey are a
    permitted default, not a required L1/L2/L3 order. ``neutral`` is the
    NEMA white/grey. ``iec_neutral`` is blue (default light blue). IEC 60445
    §6.2.2 is unverified from an official source. ``system_voltage`` empty
    means the line voltage on the drawing.
    """

    enabled: bool = True
    system_voltage: float | None = None
    nema_high_voltage: float = 300.0
    nema_low: PhasePalette = Field(default_factory=PhasePalette)
    nema_high: PhasePalette = Field(
        default_factory=lambda: PhasePalette(l1="#6b3e23", l2="#e06a12", l3="#c9a000")
    )
    iec: PhasePalette = Field(
        default_factory=lambda: PhasePalette(l1="#6b3e23", l2="#111111", l3="#7f7f7f")
    )
    # NEMA shop neutral. IEC sheets use iec_neutral.
    neutral: str = "#f2f2f2"
    iec_neutral: str = "#5eb0e8"
    ground: str = "#1e8a34"


class Wire(BaseModel):
    id: str
    frm: str = Field(alias="from")
    to: str
    net: str | None = None
    label: str = ""
    number: str = ""  # project wire number; same net shares one number
    number_locked: bool = False  # user-set; auto-number and overwrite never touch it
    color: str = ""
    gauge: str = ""
    wire_type: str = Field(default="", alias="type")  # MTW, THHN, control, …
    cable: str = ""  # cable marker; wires with the same id form a cable
    points: list[Point] = Field(default_factory=list)
    # Where a named-rail end lands, in sheet millimetres. None follows the other
    # pin (a straight rung, or a vertical IEC drop). Omitted from YAML when unset.
    from_at: Point | None = None
    to_at: Point | None = None
    inferred: bool = False  # as-built guess; dashed until a tech confirms it

    model_config = {"populate_by_name": True}

    def caption(self) -> str:
        """Text drawn on the wire. A wire number wins; else the label; else the net."""
        number = str(self.number or "").strip()
        if number:
            mark = number
        else:
            mark = str(self.label or self.net or "").strip()
        if self.inferred and mark and not mark.startswith("?"):
            return f"?{mark}"
        if self.inferred and not mark:
            return "?"
        return mark

    def spec_text(self) -> str:
        """Color, gauge, and cable marker drawn under the wire number."""
        color = str(self.color or "").strip()
        gauge = str(self.gauge or "").strip()
        cable = str(self.cable or "").strip()
        body = " ".join(part for part in (color, gauge) if part)
        if cable and body:
            return f"{cable} {body}"
        return cable or body

    def drawn_mark(self, *, ladder: bool = False) -> str:
        """Ladder rungs show a wire number only. Other sheets show caption()."""
        if ladder:
            mark = str(self.number or "").strip()
            if self.inferred and mark and not mark.startswith("?"):
                return f"?{mark}"
            if self.inferred and not mark:
                return "?"
            return mark
        return self.caption()


class Revision(BaseModel):
    """One row in the title-block revision table."""

    rev: str = ""
    date: str = ""
    description: str = ""
    drawn: str = ""
    checked: str = ""
    approved: str = ""


class TitleBlock(BaseModel):
    """Project drawing info that fills the boxes along the bottom of each sheet."""

    company: str = ""
    address: str = ""
    logo: str = ""  # path to png/jpg/svg; relative to the project file when possible
    customer: str = ""
    location: str = ""
    drawing_number: str = ""
    drawn_by: str = ""
    checked_by: str = ""
    approved_by: str = ""
    scale: str = "NTS"
    standard: str = "NEMA/ANSI"
    drawing_style: Literal["ladder", "schematic", "freeform", ""] = ""
    coil_style: Literal["circle", "square"] = "circle"
    rail_hot: str = "L1"
    rail_return: str = "L2"
    revisions: list[Revision] = Field(default_factory=list)


PRINT_STYLES: tuple[tuple[str, str, str], ...] = (
    (
        "schematic",
        "Schematic",
        "Left-to-right signal flow. IEC-style logical print.",
    ),
    (
        "ladder",
        "Ladder",
        "Elementary diagram like PLC ladder: rails both sides (L1/L2, 120/N, 24VDC/0VDC), ANSI/NEMA symbols on rungs.",
    ),
    (
        "freeform",
        "Freeform",
        "Place symbols anywhere. Cleanup will not force a rung.",
    ),
)


def normalize_drawing_style(value: str | None, *, default: str = "") -> str:
    """ladder, schematic, or freeform. Empty means the operator has not chosen yet."""

    if value is None or str(value).strip() == "":
        return default
    token = "".join(ch.lower() for ch in str(value) if ch.isalnum())
    if token in {"ladder", "jic", "rung", "rungs"}:
        return "ladder"
    if token in {"schematic", "schem", "wiring"}:
        return "schematic"
    if token in {"freeform", "free", "anywhere", "open"}:
        return "freeform"
    return default


def _drop_nonfinite_voltage(value: Any) -> Any:
    """A control voltage is a real number of volts. nan and inf load as empty."""

    if isinstance(value, bool) or value is None:
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number):
        return None
    return number


class Sheet(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    kind: SheetKind = "schematic"
    page: Page = Field(default_factory=Page)
    components: list[Component] = Field(default_factory=list)
    wires: list[Wire] = Field(default_factory=list)
    rails: list[Rail] = Field(default_factory=list)
    trays: list[WireTray] = Field(default_factory=list)
    enclosure: Enclosure | None = None
    network: NetworkInfo | None = None
    closed: bool = False
    rail_hot: str = ""
    rail_return: str = ""
    drawing_style: Literal["ladder", "schematic", "freeform", "iec", ""] = ""
    strip: str = ""  # terminal sheets: which strip this page draws
    strip_part: int = 1  # continuation sheet: 2, 3, … Omitted from YAML when 1
    io_cont: str = ""  # "IO3:2" marks a generated I/O continuation sheet. Not the title-block description.
    standard: str = ""  # empty = inherit title_block.standard (default NEMA/ANSI)
    # Volts a person or Circuit Builder stated for this sheet. Empty is not a guess.
    control_voltage: float | None = None
    # Panel.id this sheet belongs to. Empty on a single-panel file.
    panel: str = ""
    # Overview page (network, rack, block overview). Devices on it are
    # references to the primary device on its own sheet. Omitted when false.
    overview: bool = False
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("control_voltage", mode="before")
    @classmethod
    def _load_control_voltage(cls, value: Any) -> Any:
        return _drop_nonfinite_voltage(value)

    def component(self, ref: str) -> Component:
        for c in self.components:
            if c.id == ref:
                return c
        raise KeyError(f"no component '{ref}' on sheet '{self.id}'")

    def rail(self, rail_id: str) -> Rail:
        for r in self.rails:
            if r.id == rail_id:
                return r
        raise KeyError(f"no rail '{rail_id}' on sheet '{self.id}'")


class Panel(BaseModel):
    """One physical control panel in a project.

    A project with no ``panels`` list is a single unnamed panel. The file
    stays unchanged until a panel is added. This record does not store
    width, height, or depth. The enclosure is a catalog row applied to
    this panel's backpanel sheet.
    """

    id: str
    tag: str = ""
    name: str = ""
    enclosure_id: str = ""
    system: str = ""  # IEC =system aspect. Empty uses project.location_system.


class CableConductor(BaseModel):
    """One cable conductor landed on a panel terminal and a field terminal."""

    id: str
    color: str = ""
    panel_terminal: str = ""
    field_terminal: str = ""
    sheet: str = ""
    wire_id: str = ""
    wire_number: str = ""
    role: str = ""  # phase, ground, signal, spare, shield
    spare: bool = False
    shield: bool = False


class Cable(BaseModel):
    """A field cable leaving the panel for one field device.

    The panel end is a terminal strip or, when a drive feeds the motor with
    no strip between them, that drive's output terminals. ``part_number``
    stays blank unless a person typed it or it came from the part library
    with an http(s) source URL. Descriptions are standard cable names
    (``3C+G #14 TC-ER``), not manufacturer catalog numbers. ``gland`` stays
    blank unless someone enters a cord grip; a sourced library OD is stored
    on ``od`` and is not turned into a trade size.
    """

    tag: str
    from_strip: str = ""
    from_device: str = ""
    from_terminals: list[str] = Field(default_factory=list)
    to_device: str = ""
    to_kind: str = ""  # motor, sensor, valve, pushbutton, instrument
    cable_type: str = ""
    conductors: int = 0
    gauge: str = ""
    shield: bool = False
    drain: bool = False
    # both = drive and motor (VFD output). panel = control end only (analog).
    # field / none are user choices. Empty leaves the conductor map alone.
    shield_bond: Literal["", "both", "panel", "field", "none"] = ""
    shield_source: str = ""
    shield_note: str = ""
    length: float | None = None
    length_unit: Literal["ft", "m"] = "ft"
    slack_pct: float | None = None
    gland: str = ""
    od: str = ""
    od_source: str = ""
    route: str = ""
    part_number: str = ""
    part_source: str = ""
    show_tag: bool = True
    pairs: int = 0
    assignments: list[CableConductor] = Field(default_factory=list)
    # field = a cable leaving the panel. interpanel = a cable between two panels.
    kind: Literal["field", "interpanel"] = "field"
    from_panel: str = ""
    to_panel: str = ""
    to_strip: str = ""
    to_terminals: list[str] = Field(default_factory=list)


class ShareEvent(BaseModel):
    """One recorded save. Empty strings are omitted from the YAML."""

    user: str = ""
    host: str = ""
    time: str = ""
    summary: str = ""


class Project(BaseModel):
    format: Literal["panelcad"] = "panelcad"
    version: str = FORMAT_VERSION
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    # Volts stated for the control circuit. Empty is not a library default.
    control_voltage: float | None = None
    title_block: TitleBlock = Field(default_factory=TitleBlock)
    # Shop overrides for NEMA/ANSI tags. Keys are category or category.tag.
    # Empty = use libraries/builtin/ref_prefixes.yaml. Omitted from YAML when empty.
    ref_prefixes: dict[str, str] = Field(default_factory=dict)
    wire_numbering: WireNumbering | None = None
    # Keyed by net name. ``*`` applies when a net has no entry. Omitted when empty.
    net_wire_specs: dict[str, NetWireSpec] = Field(default_factory=dict)
    cross_reference: CrossReference | None = None
    # Issued snapshots and revision clouds. Omitted from YAML until a revision is issued.
    revision_control: RevisionControl | None = None
    cables: list[Cable] = Field(default_factory=list)
    # Omitted from YAML when empty. One implicit panel is the legacy file.
    panels: list[Panel] = Field(default_factory=list)
    active_panel: str = ""
    # Default IEC =system aspect for panels that do not set their own.
    location_system: str = ""
    phase_colors: PhaseColors | None = None
    reconstruct: Any = None
    sheets: list[Sheet] = Field(default_factory=list)
    # Who last wrote the file, and a short save history for a shared folder.
    # Omitted from YAML until a save records them. Revision snapshots leave them out.
    last_saved: ShareEvent | None = None
    activity: list[ShareEvent] = Field(default_factory=list)

    @field_validator("control_voltage", mode="before")
    @classmethod
    def _load_control_voltage(cls, value: Any) -> Any:
        return _drop_nonfinite_voltage(value)

    @field_validator("reconstruct", mode="before")
    @classmethod
    def _load_reconstruct(cls, value: Any) -> Any:
        if value in (None, {}, []):
            return None
        from panelcad.reconstruct.models import Reconstruction

        if isinstance(value, Reconstruction):
            return value
        return Reconstruction.model_validate(value)

    @field_validator("last_saved", mode="before")
    @classmethod
    def _load_last_saved(cls, value: Any) -> Any:
        if value in (None, {}, []):
            return None
        return value

    @field_validator("activity", mode="before")
    @classmethod
    def _load_activity(cls, value: Any) -> Any:
        if value in (None, {}, ""):
            return []
        return value

    @property
    def metadata(self) -> dict[str, Any]:
        return self.properties

    @metadata.setter
    def metadata(self, value: dict[str, Any]) -> None:
        self.properties = dict(value or {})

    def sheet(self, sheet_id: str) -> Sheet:
        for s in self.sheets:
            if s.id == sheet_id:
                return s
        raise KeyError(f"no sheet '{sheet_id}'")

    def default_sheet(self) -> Sheet:
        if not self.sheets:
            raise ValueError("project has no sheets")
        return self.sheets[0]
