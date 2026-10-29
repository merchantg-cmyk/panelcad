"""As-built reconstruction from real panel photos.

Primary input is phone photos of a physical panel (backplate, door, labels,
terminal strips, wire numbers). Drawings and print scans are a secondary
source used only to cross-check. Dimensions are never invented.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PHOTO_ROLES = (
    "backpanel",
    "door_inside",
    "door_outside",
    "nameplate",
    "part_label",
    "terminal_strip",
    "wire_numbers",
    "other",
)
PhotoRole = Literal[
    "backpanel",
    "door_inside",
    "door_outside",
    "nameplate",
    "part_label",
    "terminal_strip",
    "wire_numbers",
    "other",
]
PRINT_KINDS = ("project", "pdf", "scan", "index", "other")
PrintKind = Literal["project", "pdf", "scan", "index", "other"]
SCALE_KINDS = ("din_rail", "catalog_part", "wire_duct", "measured")
ScaleKind = Literal["din_rail", "catalog_part", "wire_duct", "measured"]
ITEM_ROLES = ("part", "terminal", "wire_number", "device_tag", "rail", "duct")
ItemRole = Literal["part", "terminal", "wire_number", "device_tag", "rail", "duct"]
ITEM_STATUS = ("unconfirmed", "catalog", "envelope", "needs_info")
ItemStatus = Literal["unconfirmed", "catalog", "envelope", "needs_info"]
STEP_KINDS = ("identify_part", "trace_wire", "confirm_connection", "confirm_scale", "compare", "needs_info")
StepKind = Literal["identify_part", "trace_wire", "confirm_connection", "confirm_scale", "compare", "needs_info"]
STEP_STATUS = ("open", "answered", "confirmed", "skipped")
StepStatus = Literal["open", "answered", "confirmed", "skipped"]

# TS35 / EN 60715 hat height. Used only when the tech says the mark is a DIN rail.
DIN_RAIL_HEIGHT_MM = 35.0
# A few millimetres on a 600×800 mm panel. Two marks of the same axis are
# refused above this. Independent axes may still differ by perspective.
SCALE_ERROR_TOLERANCE_MM = 8.0
# 12% is how much one axis shortens at about 28 degrees off square-on
# (cos 28° is about 0.88). A normal phone angle stays inside that. A length
# typed further off than this is refused. The fraction is not tuned to any one panel.
PERSPECTIVE_SCALE_FRACTION = 0.12
# Part envelopes live on a panel face; 2 m is already past any real device.
ENVELOPE_MIN_MM = 0.5
ENVELOPE_MAX_MM = 2000.0
ENVELOPE_MOUNTS = ("din_rail", "backplate", "door", "field")
PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PHONE_EXPORT_EXTS = {".heic", ".heif"}
PRINT_EXTS = {".pdf", ".dxf", ".pcad.yaml", ".yaml", ".yml"} | PHOTO_EXTS


class PhotoCorner(BaseModel):
    """One corner of the panel face in photo pixels (tech-adjustable)."""

    x: float
    y: float


class PhotoRegion(BaseModel):
    """Axis-aligned crop in photo pixels (top-left origin)."""

    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0


class ScaleReference(BaseModel):
    """A known real length the tech marked on the photo. Never guessed."""

    kind: ScaleKind
    known_mm: float
    p1_x: float
    p1_y: float
    p2_x: float
    p2_y: float
    note: str = ""
    catalog: str = ""
    manufacturer: str = ""
    dimension: Literal["width", "height", "depth", ""] = ""


class Photo(BaseModel):
    """One phone photo of the physical panel, stored with the project."""

    id: str
    path: str
    role: PhotoRole = "other"
    note: str = ""
    width_px: int = 0
    height_px: int = 0
    corners: list[PhotoCorner] = Field(default_factory=list)
    scale: ScaleReference | None = None
    scale_b: ScaleReference | None = None
    face_width_mm: float | None = None
    face_height_mm: float | None = None
    # True only when the tech said the marked corners are the backpanel itself.
    face_is_backpanel: bool = False
    scale_error_mm: float | None = None
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0
    unreadable: bool = False
    vision_note: str = ""
    source_path: str = ""
    original_name: str = ""
    resolved_path: str = Field(default="", exclude=True)


class CatalogMatch(BaseModel):
    manufacturer: str
    catalog: str
    library_id: str
    confidence: float
    verified: str = ""
    width_mm: float | None = None
    height_mm: float | None = None
    depth_mm: float | None = None
    source: str = ""


class ObservedItem(BaseModel):
    """Something read from a panel photo. Not a layout part until confirmed."""

    id: str
    photo_id: str
    region: PhotoRegion = Field(default_factory=PhotoRegion)
    role: ItemRole = "part"
    label_text: str = ""
    brand: str = ""
    part_number: str = ""
    # What the photo label said. A later catalog pick must not replace this.
    label_brand: str = ""
    label_part_number: str = ""
    tag: str = ""
    terminal: str = ""
    wire_number: str = ""
    matches: list[CatalogMatch] = Field(default_factory=list)
    status: ItemStatus = "unconfirmed"
    envelope_w_mm: float | None = None
    envelope_h_mm: float | None = None
    envelope_d_mm: float | None = None
    envelope_confirmed: bool = False
    mount: str = ""
    library_id: str = ""
    placed_ref: str = ""
    sheet: str = ""
    x_mm: float | None = None
    y_mm: float | None = None
    rail_id: str = ""
    confidence: float = 0.0
    unreadable: bool = False
    closeup_photo_id: str = ""


class ChecklistItem(BaseModel):
    id: str
    kind: StepKind
    prompt: str
    status: StepStatus = "open"
    answer: str = ""
    answer_photo_id: str = ""
    item_id: str = ""
    from_ref: str = ""
    to_ref: str = ""
    wire_number: str = ""
    wire_id: str = ""
    inferred: bool = True
    confirmed: bool = False
    photo_id: str = ""


class PrintSource(BaseModel):
    """Secondary drawing / previous print. Never the primary layout source."""

    id: str
    path: str
    kind: PrintKind = "other"
    note: str = ""
    extracted: bool = False
    extractor: str = ""


class PrintMismatch(BaseModel):
    check: str
    message: str
    photo_tag: str = ""
    print_tag: str = ""
    photo_value: str = ""
    print_value: str = ""
    photo_id: str = ""
    item_id: str = ""


class Reconstruction(BaseModel):
    """As-built photo reconstruction stored on the project. Omitted when unused."""

    photos: list[Photo] = Field(default_factory=list)
    items: list[ObservedItem] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    prints: list[PrintSource] = Field(default_factory=list)
    mismatches: list[PrintMismatch] = Field(default_factory=list)
    panel_sheet: str = ""
    door_sheet: str = ""
    schematic_sheet: str = ""

    def photo(self, photo_id: str) -> Photo:
        for item in self.photos:
            if item.id == photo_id:
                return item
        raise KeyError(f"no reconstruct photo '{photo_id}'")

    def item(self, item_id: str) -> ObservedItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(f"no reconstruct item '{item_id}'")

    def step(self, step_id: str) -> ChecklistItem:
        for item in self.checklist:
            if item.id == step_id:
                return item
        raise KeyError(f"no reconstruct step '{step_id}'")

    def print_source(self, print_id: str) -> PrintSource:
        for item in self.prints:
            if item.id == print_id:
                return item
        raise KeyError(f"no reconstruct print '{print_id}'")

    def is_empty(self) -> bool:
        return not (self.photos or self.items or self.checklist or self.prints or self.mismatches)


def ensure_reconstruction(project: Any) -> Reconstruction:
    store = getattr(project, "reconstruct", None)
    if store is None:
        store = Reconstruction()
        project.reconstruct = store
    return store


def public_photo(photo: Photo) -> dict[str, Any]:
    return photo.model_dump(mode="json")


def public_item(item: ObservedItem) -> dict[str, Any]:
    data = item.model_dump(mode="json")
    data["layout_ready"] = item_is_layout_ready(item)
    return data


def _positive_mm(value: object) -> bool:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return False
    return ENVELOPE_MIN_MM <= number <= ENVELOPE_MAX_MM


def catalog_match_has_footprint(match: CatalogMatch) -> bool:
    return _positive_mm(match.width_mm) and _positive_mm(match.height_mm) and _positive_mm(match.depth_mm)


def item_is_layout_ready(item: ObservedItem) -> bool:
    """Catalog match with a real W×H×D, or a tech-confirmed envelope with W, H, D, and mount."""

    if item.status == "catalog" and item.matches:
        return catalog_match_has_footprint(item.matches[0])
    if item.status == "envelope" and item.envelope_confirmed:
        from panelcad.library.panel import LayoutNotReadyError, require_explicit_envelope

        try:
            require_explicit_envelope(
                item.envelope_w_mm, item.envelope_h_mm, item.envelope_d_mm, item.mount
            )
        except LayoutNotReadyError:
            return False
        return True
    return False
