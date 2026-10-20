"""Load the editable standard-enclosure table (not a device catalog)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from panelcad.library.paths import builtin_library_dir, repo_root, user_library_dir

INCH_MM = 25.4

# Catalog numbers confirmed on an official manufacturer page in libraries/enclosures.yaml.
CONFIRMED_CATALOGS = frozenset(
    {
        "A201208LP",
        "A161208LP",
        "A161606LP",
        "A16H1206SSLP",
        "N412121206C",
        "N412161206C",
        "N412161208C",
        "N412201608C",
        "N412242008C",
        "N412161206SSC",
        "BB121275W",
        "BB161255W",
        "BB201275W",
    }
)


@dataclass(frozen=True)
class StandardEnclosure:
    """One row from libraries/enclosures.yaml. Dimensions stored in millimetres."""

    id: str
    mount: str
    nema: str
    material: str
    height_mm: float
    width_mm: float
    depth_mm: float
    panel_height_mm: float
    panel_width_mm: float
    manufacturer: str = ""
    catalog: str = ""
    url: str = ""

    @property
    def height_in(self) -> float:
        return self.height_mm / INCH_MM

    @property
    def width_in(self) -> float:
        return self.width_mm / INCH_MM

    @property
    def depth_in(self) -> float:
        return self.depth_mm / INCH_MM

    @property
    def panel_height_in(self) -> float:
        return self.panel_height_mm / INCH_MM

    @property
    def panel_width_in(self) -> float:
        return self.panel_width_mm / INCH_MM

    @property
    def volume_in3(self) -> float:
        return self.height_in * self.width_in * self.depth_in

    def display_name(self, *, units: str = "in") -> str:
        nema = f"NEMA {self.nema}" if self.nema else "enclosure"
        mat = self.material or "steel"
        if units == "in":
            box = f"{_in(self.height_in)} × {_in(self.width_in)} × {_in(self.depth_in)} in"
        else:
            box = f"{self.height_mm:.0f} × {self.width_mm:.0f} × {self.depth_mm:.0f} mm"
        extra = ""
        if self.catalog:
            who = f"{self.manufacturer} " if self.manufacturer else ""
            extra = f" ({who}{self.catalog})"
        return f"{box} {nema} {mat}{extra}"

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mount": self.mount,
            "nema": self.nema,
            "material": self.material,
            "height_in": round(self.height_in, 3),
            "width_in": round(self.width_in, 3),
            "depth_in": round(self.depth_in, 3),
            "height_mm": round(self.height_mm, 2),
            "width_mm": round(self.width_mm, 2),
            "depth_mm": round(self.depth_mm, 2),
            "panel_height_in": round(self.panel_height_in, 3),
            "panel_width_in": round(self.panel_width_in, 3),
            "panel_height_mm": round(self.panel_height_mm, 2),
            "panel_width_mm": round(self.panel_width_mm, 2),
            "manufacturer": self.manufacturer,
            "catalog": self.catalog,
            "url": self.url,
            "name": self.display_name(),
        }


@dataclass
class EnclosureLibrary:
    boxes: list[StandardEnclosure] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)

    def by_id(self, box_id: str) -> StandardEnclosure | None:
        for box in self.boxes:
            if box.id == box_id:
                return box
        return None


def _in(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _inch_or_mm(raw: dict[str, Any], inch_key: str, mm_key: str) -> float:
    inches = _num(raw.get(inch_key))
    if inches is not None:
        return inches * INCH_MM
    mm = _num(raw.get(mm_key))
    if mm is not None:
        return mm
    raise ValueError(f"enclosure row {raw.get('id')!r} needs {inch_key} or {mm_key}")


def enclosure_search_paths(project_dir: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    extra = os.environ.get("PANELCAD_ENCLOSURE_PATH", "")
    if extra:
        paths.extend(Path(p) for p in extra.split(os.pathsep) if p)
    builtin = repo_root() / "libraries" / "enclosures.yaml"
    if not builtin.is_file():
        alt = builtin_library_dir().parent / "enclosures.yaml"
        if alt.is_file():
            builtin = alt
    if builtin.is_file():
        paths.append(builtin)
    user = user_library_dir().parent / "enclosures.yaml"
    if user.is_file():
        paths.append(user)
    if project_dir is not None:
        local = Path(project_dir) / "library" / "enclosures.yaml"
        if local.is_file():
            paths.append(local)
    return paths


def _row_from_raw(raw: dict[str, Any]) -> StandardEnclosure:
    height = _inch_or_mm(raw, "height_in", "height_mm")
    width = _inch_or_mm(raw, "width_in", "width_mm")
    depth = _inch_or_mm(raw, "depth_in", "depth_mm")
    panel_h = _num(raw.get("panel_height_in"))
    panel_w = _num(raw.get("panel_width_in"))
    if panel_h is None:
        panel_h_mm = _num(raw.get("panel_height_mm"))
        panel_h = (panel_h_mm / INCH_MM) if panel_h_mm is not None else (height / INCH_MM - 3.0)
    if panel_w is None:
        panel_w_mm = _num(raw.get("panel_width_mm"))
        panel_w = (panel_w_mm / INCH_MM) if panel_w_mm is not None else (width / INCH_MM - 3.0)
    catalog = str(raw.get("catalog") or "").strip()
    url = str(raw.get("url") or "").strip()
    if catalog and catalog not in CONFIRMED_CATALOGS:
        # Table may grow; still refuse a catalog with no official URL.
        if not url:
            catalog = ""
    return StandardEnclosure(
        id=str(raw.get("id") or f"{width/INCH_MM:.0f}x{height/INCH_MM:.0f}"),
        mount=str(raw.get("mount") or "wall"),
        nema=str(raw.get("nema") or raw.get("nema_type") or "12"),
        material=str(raw.get("material") or "steel"),
        height_mm=height,
        width_mm=width,
        depth_mm=depth,
        panel_height_mm=panel_h * INCH_MM,
        panel_width_mm=panel_w * INCH_MM,
        manufacturer=str(raw.get("manufacturer") or ""),
        catalog=catalog,
        url=url,
    )


def load_enclosure_file(path: Path, library: EnclosureLibrary) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return
    defaults = data.get("defaults") or {}
    if isinstance(defaults, dict):
        library.defaults.update(defaults)
    rows = data.get("enclosures") or data.get("boxes") or []
    by_id = {box.id: i for i, box in enumerate(library.boxes)}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        box = _row_from_raw(raw)
        if box.id in by_id:
            library.boxes[by_id[box.id]] = box
        else:
            by_id[box.id] = len(library.boxes)
            library.boxes.append(box)


def load_enclosures(project_dir: Path | None = None) -> EnclosureLibrary:
    library = EnclosureLibrary()
    for path in enclosure_search_paths(project_dir):
        if path.is_file():
            load_enclosure_file(path, library)
    return library


def largest_box_mm(library: EnclosureLibrary | None = None) -> float:
    """Largest single catalog box dimension (width, height, or depth), millimetres.

    A confirmed part envelope larger than this cannot fit any listed enclosure.
    Shared with PR #40 ``require_explicit_envelope``.
    """

    boxes = (library or load_enclosures()).boxes
    biggest = 0.0
    for box in boxes:
        biggest = max(biggest, box.width_mm, box.height_mm, box.depth_mm)
    return biggest if biggest > 0 else 90.0 * INCH_MM
