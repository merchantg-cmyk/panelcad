"""Verified manufacturer catalog parts. Overlay YAML; never invent numbers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from panelcad.library import yaml_load as _lib_yaml
from panelcad.library.paths import builtin_library_dir, parts_overlay_dirs

Verified = Literal["full", "partial"]
Mounting = Literal["din", "panel", "door", "plug_on", "none", ""]
ADJUSTABLE_CATEGORIES = frozenset({"protection.mpcb", "protection.overload"})

SOURCE_RE = re.compile(r"^https?://", re.I)
_ARTICLE_COMMENT = re.compile(
    r'article:\s*["\']([^"\']+)["\']\s*#\s*(https?://\S+)',
    re.I,
)
_URL_IN_COMMENT = re.compile(r"(https?://\S+)")


class TripCurveLimit(BaseModel):
    """One published breaker-trip result for a single curve."""

    model_config = ConfigDict(extra="forbid")

    curve: str
    max_amps: float | None = None
    loop_mohm: float | None = None
    trips: bool = True
    note: str = ""

    @field_validator("curve")
    @classmethod
    def _curve(cls, value: str) -> str:
        text = str(value or "").strip().upper()
        if not text:
            raise ValueError("trip curve is empty")
        return text


class OutputTrip(BaseModel):
    """Maker's published output-breaker tests for one supply."""

    model_config = ConfigDict(extra="forbid")

    source: str
    publication: str
    pages: str
    section: str = ""
    supply_end: list[TripCurveLimit] = []
    load_end: list[TripCurveLimit] = []

    @field_validator("source")
    @classmethod
    def _http_source(cls, value: str) -> str:
        text = str(value or "").strip()
        if not SOURCE_RE.match(text):
            raise ValueError("output_trip source must be an http(s) URL")
        return text

    @field_validator("publication", "pages")
    @classmethod
    def _required(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("output_trip publication and pages are required")
        return text


class CatalogPart(BaseModel):
    """One manufacturer SKU mapped onto a generic library symbol."""

    model_config = ConfigDict(extra="forbid")

    manufacturer: str
    catalog: str
    library_id: str
    category: str
    description: str = ""
    verified: Verified
    source: str
    source_date: str = ""
    amps: float | None = None
    amps_min: float | None = None
    amps_max: float | None = None
    poles: int | None = None
    coil_voltage: str = ""
    contact_config: str = ""
    ka: float | None = None
    sccr: str = ""
    sccr_ka: float | None = None
    heat_w: float | None = None
    coil_va_sealed: float | None = None
    coil_va_inrush: float | None = None
    power_w: float | None = None
    current_a: float | None = None
    va: float | None = None
    inrush_method: str = ""
    power_source: str = ""
    watt_loss: float | None = None
    watt_loss_source: str = ""
    watt_loss_note: str = ""
    max_ambient_c: float | None = None
    mounting: Mounting = ""
    width_mm: float | None = None
    height_mm: float | None = None
    depth_mm: float | None = None
    notes: str = ""
    series: str = ""
    suffix_rule: str = ""
    output_trip: OutputTrip | None = None
    # Shop-entered price. Leave blank in shipped libraries. Never invent a cost.
    unit_cost: float | None = None
    vendor: str = ""
    lead_time: str = ""
    price_date: str = ""
    # Catalog numbers of other parts already in this library. Unknown numbers are ignored.
    accessories: list[str] = Field(default_factory=list)

    @field_validator("accessories")
    @classmethod
    def _accessory_numbers(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in seen:
                seen.append(text)
        return seen

    @field_validator("manufacturer", "catalog", "library_id", "category", "source")
    @classmethod
    def _required_text(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("required field is empty")
        return text

    @field_validator("source")
    @classmethod
    def _http_source(cls, value: str) -> str:
        text = str(value or "").strip()
        if not SOURCE_RE.match(text):
            raise ValueError("source must be an http(s) URL")
        parsed = urlparse(text)
        if not parsed.netloc:
            raise ValueError("source URL is missing a host")
        return text

    @field_validator(
        "amps",
        "amps_min",
        "amps_max",
        "ka",
        "heat_w",
        "watt_loss",
        "coil_va_sealed",
        "coil_va_inrush",
        "power_w",
        "current_a",
        "va",
        "width_mm",
        "height_mm",
        "depth_mm",
        "unit_cost",
    )
    @classmethod
    def _positive(cls, value: float | None) -> float | None:
        if value is None:
            return None
        number = float(value)
        if number < 0:
            raise ValueError("numeric rating / dimension cannot be negative")
        return number

    @field_validator("power_source")
    @classmethod
    def _power_source(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if not SOURCE_RE.match(text):
            raise ValueError("power_source must be an http(s) URL")
        return text

    @field_validator("sccr_ka")
    @classmethod
    def _sccr_ka(cls, value: float | None) -> float | None:
        return cls._positive(value)
    @field_validator("max_ambient_c")
    @classmethod
    def _max_ambient(cls, value: float | None) -> float | None:
        if value is None:
            return None
        number = float(value)
        if number < -40 or number > 200:
            raise ValueError("max_ambient_c must be between -40 and 200 °C")
        return number

    @model_validator(mode="after")
    def _adjustable_range(self) -> CatalogPart:
        if self.amps_min is not None and self.amps_max is not None:
            if self.amps_min > self.amps_max:
                raise ValueError("amps_min cannot exceed amps_max")
            if self.amps is None:
                self.amps = self.amps_max
        if self.verified == "full" and self.category in ADJUSTABLE_CATEGORIES:
            if self.amps_min is None or self.amps_max is None:
                raise ValueError(
                    "verified:full entries in an adjustable category must have amps_min and amps_max"
                )
        if self.watt_loss is not None:
            if not str(self.watt_loss_source or "").strip():
                self.watt_loss_source = self.source
            if not SOURCE_RE.match(self.watt_loss_source):
                raise ValueError("watt_loss_source must be an http(s) URL citing the maker")
        return self

    def key(self) -> str:
        return part_key(self.manufacturer, self.catalog)

    def has_footprint(self) -> bool:
        return (
            self.width_mm is not None
            and self.height_mm is not None
            and self.width_mm > 0
            and self.height_mm > 0
        )

    def ratings_text(self) -> str:
        bits: list[str] = []
        if self.amps_min is not None and self.amps_max is not None:
            bits.append(f"{self.amps_min:g}–{self.amps_max:g} A")
        elif self.amps is not None:
            bits.append(f"{self.amps:g} A")
        if self.poles is not None:
            bits.append(f"{self.poles}P")
        if self.coil_voltage:
            bits.append(self.coil_voltage)
        if self.contact_config:
            bits.append(self.contact_config)
        if self.ka is not None:
            bits.append(f"{self.ka:g} kA")
        if self.sccr:
            bits.append(f"SCCR {self.sccr}")
        if self.sccr_ka is not None:
            bits.append(f"SCCR {self.sccr_ka:g} kA")
        if self.heat_w is not None:
            bits.append(f"{self.heat_w:g} W")
        if self.power_w is not None:
            bits.append(f"{self.power_w:g} W out")
        if self.current_a is not None and self.amps is None:
            bits.append(f"{self.current_a:g} A")
        if self.coil_va_sealed is not None:
            bits.append(f"{self.coil_va_sealed:g} VA sealed")
        if self.coil_va_inrush is not None:
            bits.append(f"{self.coil_va_inrush:g} VA inrush")
        if self.va is not None:
            bits.append(f"{self.va:g} VA")

        if self.watt_loss is not None:
            bits.append(f"{self.watt_loss:g} W loss")
        if self.max_ambient_c is not None:
            bits.append(f"{self.max_ambient_c:g} °C max")
        return ", ".join(bits)

    def to_row(self) -> dict[str, str]:
        return {
            "manufacturer": self.manufacturer,
            "catalog": self.catalog,
            "library_id": self.library_id,
            "category": self.category,
            "description": self.description,
            "verified": self.verified,
            "source": self.source,
            "source_date": self.source_date,
            "amps": "" if self.amps is None else f"{self.amps:g}",
            "amps_min": "" if self.amps_min is None else f"{self.amps_min:g}",
            "amps_max": "" if self.amps_max is None else f"{self.amps_max:g}",
            "poles": "" if self.poles is None else str(self.poles),
            "coil_voltage": self.coil_voltage,
            "contact_config": self.contact_config,
            "ka": "" if self.ka is None else f"{self.ka:g}",
            "sccr": self.sccr,
            "sccr_ka": "" if self.sccr_ka is None else f"{self.sccr_ka:g}",
            "heat_w": "" if self.heat_w is None else f"{self.heat_w:g}",
            "coil_va_sealed": "" if self.coil_va_sealed is None else f"{self.coil_va_sealed:g}",
            "coil_va_inrush": "" if self.coil_va_inrush is None else f"{self.coil_va_inrush:g}",
            "power_w": "" if self.power_w is None else f"{self.power_w:g}",
            "current_a": "" if self.current_a is None else f"{self.current_a:g}",
            "va": "" if self.va is None else f"{self.va:g}",
            "inrush_method": self.inrush_method,
            "power_source": self.power_source,
            "watt_loss": "" if self.watt_loss is None else f"{self.watt_loss:g}",
            "watt_loss_source": self.watt_loss_source,
            "max_ambient_c": "" if self.max_ambient_c is None else f"{self.max_ambient_c:g}",
            "mounting": self.mounting,
            "width_mm": "" if self.width_mm is None else f"{self.width_mm:g}",
            "height_mm": "" if self.height_mm is None else f"{self.height_mm:g}",
            "depth_mm": "" if self.depth_mm is None else f"{self.depth_mm:g}",
            "notes": self.notes,
            "series": self.series,
        }

    def assignment_properties(self) -> dict[str, Any]:
        """Instance properties written by ``Session.assign_part``."""

        props: dict[str, Any] = {
            "manufacturer": self.manufacturer,
            "part_number": self.catalog,
            "catalog": self.catalog,
            "description": self.description,
            "catalog_verified": self.verified,
            "catalog_source": self.source,
            "catalog_key": self.key(),
        }
        if self.amps_min is not None:
            props["amps_min"] = self.amps_min
        if self.amps_max is not None:
            props["amps_max"] = self.amps_max
        if self.amps_min is not None and self.amps_max is not None:
            props["range"] = f"{self.amps_min:g}-{self.amps_max:g}A"
        if self.amps is not None:
            props["amps"] = self.amps
            if self.category.startswith("protection.breaker") or self.category.startswith(
                "protection.fuse"
            ) or self.category.startswith("protection.mpcb"):
                props["In"] = f"{self.amps:g}A"
            elif self.category.startswith("switching.contactor"):
                props["Ie"] = f"{self.amps:g}A"
            elif self.category.startswith("protection.overload") and "range" not in props:
                props["range"] = f"{self.amps:g}A"
        if self.poles is not None:
            props["poles"] = self.poles
        if self.coil_voltage:
            props["coil_voltage"] = self.coil_voltage
            props["coil"] = self.coil_voltage
        if self.contact_config:
            props["contact_config"] = self.contact_config
        if self.ka is not None:
            props["ka"] = self.ka
            props["Icu"] = f"{self.ka:g}kA"
        if self.sccr:
            props["sccr"] = self.sccr
        if self.sccr_ka is not None:
            props["sccr_ka"] = float(self.sccr_ka)
        if self.heat_w is not None:
            props["heat_w"] = self.heat_w
        consumption = False
        if self.coil_va_sealed is not None:
            props["coil_va_sealed"] = self.coil_va_sealed
            consumption = True
        if self.coil_va_inrush is not None:
            props["coil_va_inrush"] = self.coil_va_inrush
            consumption = True
        if self.power_w is not None:
            props["power_w"] = self.power_w
            consumption = True
        if self.current_a is not None:
            props["current_a"] = self.current_a
            consumption = True
        if consumption:
            props["power_origin"] = "catalog"
            props["power_source"] = self.power_source or self.source
        if self.va is not None:
            props["va"] = self.va
            props["VA"] = self.va
        if self.inrush_method:
            props["inrush_method"] = self.inrush_method
        if self.watt_loss is not None:
            props["watt_loss"] = self.watt_loss
            props["watt_loss_source"] = self.watt_loss_source
            if self.watt_loss_note:
                props["watt_loss_note"] = self.watt_loss_note
        if self.max_ambient_c is not None:
            props["max_ambient_c"] = self.max_ambient_c
        if self.has_footprint():
            props["width_mm"] = float(self.width_mm or 0)
            props["height_mm"] = float(self.height_mm or 0)
            if self.depth_mm is not None:
                props["depth_mm"] = float(self.depth_mm)
            else:
                props["catalog_depth_missing"] = True
            props["dim_source"] = "catalog"
        if self.mounting:
            # Keep the library word (plug_on, din, …). `mount` is the panel
            # placement and collapses plug_on to "none".
            props["mounting"] = self.mounting
        mount = mounting_to_panel(self.mounting)
        if mount:
            props["mount"] = mount
            props["din_rail"] = mount == "din_rail"
        return props


ASSIGNED_KEYS = frozenset(
    {
        "manufacturer",
        "part_number",
        "catalog",
        "description",
        "catalog_verified",
        "catalog_source",
        "catalog_key",
        "catalog_fallback",
        "catalog_primary",
        "amps",
        "amps_min",
        "amps_max",
        "In",
        "Ie",
        "range",
        "poles",
        "coil_voltage",
        "coil",
        "contact_config",
        "ka",
        "Icu",
        "sccr",
        "sccr_ka",
        "heat_w",
        "coil_va_sealed",
        "coil_va_inrush",
        "power_w",
        "current_a",
        "power_origin",
        "power_source",
        "va",
        "VA",
        "inrush_method",
        "watt_loss",
        "watt_loss_source",
        "watt_loss_note",
        "max_ambient_c",
        "width_mm",
        "height_mm",
        "depth_mm",
        "dim_source",
        "catalog_depth_missing",
        "mount",
        "mounting",
        "din_rail",
        "cutout",
    }
)


def part_key(manufacturer: str, catalog: str) -> str:
    return f"{normalize_manufacturer(manufacturer)}|{str(catalog).strip().casefold()}"


def normalize_manufacturer(name: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "", str(name or "").casefold())
    aliases = {
        "allenbradley": "Allen-Bradley",
        "rockwell": "Allen-Bradley",
        "rockwellautomation": "Allen-Bradley",
        "ab": "Allen-Bradley",
        "siemens": "Siemens",
        "eaton": "Eaton",
        "moeller": "Eaton",
        "bussmann": "Eaton",
        "eatonbussmann": "Eaton",
        "schneider": "Schneider Electric",
        "schneiderelectric": "Schneider Electric",
        "squared": "Schneider Electric",
        "tesys": "Schneider Electric",
        "phoenix": "Phoenix Contact",
        "phoenixcontact": "Phoenix Contact",
        "wago": "WAGO",
        "weidmueller": "Weidmüller",
        "weidmuller": "Weidmüller",
        "automationdirect": "AutomationDirect",
        "adc": "AutomationDirect",
        "lselectric": "LS Electric",
        "lselectricamerica": "LS Electric",
        "lsis": "LS Electric",
        "ls": "LS Electric",
        "yaskawa": "Yaskawa",
    }
    return aliases.get(token, str(name or "").strip())


def mounting_to_panel(mounting: str) -> str:
    table = {
        "din": "din_rail",
        "din_rail": "din_rail",
        "panel": "backplate",
        "backplate": "backplate",
        "door": "door",
        "plug_on": "none",
        "none": "none",
    }
    return table.get(str(mounting or "").strip().lower(), "")


def is_parts_library_file(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    kind = str(data.get("kind") or "").strip().lower()
    if kind in {"manufacturer_parts", "parts"}:
        return True
    if data.get("parts") and not data.get("devices"):
        return True
    if data.get("suffix_rules") and not data.get("devices"):
        return True
    return False


class PartsCatalog:
    def __init__(self) -> None:
        self.parts: dict[str, CatalogPart] = {}
        self._loaded_files: list[Path] = []
        self.suffix_rules: dict[str, dict[str, Any]] = {}

    def load_defaults(self, project_dir: Path | None = None) -> None:
        builtin = builtin_library_dir()
        vendor = builtin / "terminal_vendor_articles.yaml"
        if vendor.is_file():
            self.load_vendor_articles(vendor)
        for directory in parts_overlay_dirs(project_dir):
            self.load_dir(directory)

    def load_dir(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            self.load_file(path)

    def load_file(self, path: Path) -> None:
        data = _lib_yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: manufacturer part file must be a mapping")
        if data.get("vendor_articles"):
            self.load_vendor_articles(path)
            return
        if not is_parts_library_file(data) and "parts" not in data:
            return
        manufacturer = str(data.get("manufacturer") or "")
        for raw in data.get("suffix_rules") or []:
            if isinstance(raw, dict) and raw.get("id"):
                self.suffix_rules[str(raw["id"])] = dict(raw)
        for raw in data.get("parts") or []:
            if not isinstance(raw, dict):
                continue
            for part in expand_part_row(raw, manufacturer, self.suffix_rules, path):
                self.parts[part.key()] = part
        self._loaded_files.append(path)

    def load_vendor_articles(self, path: Path) -> None:
        """Reuse ``terminal_vendor_articles.yaml`` — do not copy those SKUs."""

        text = path.read_text(encoding="utf-8")
        sources = _article_sources(text)
        data = _lib_yaml.safe_load(text) or {}
        articles = data.get("vendor_articles") or {}
        if not isinstance(articles, dict):
            return
        seen: set[str] = set()
        for library_id, vendors in articles.items():
            if not isinstance(vendors, dict):
                continue
            lib = str(library_id)
            if not lib.startswith("iec."):
                continue
            category = "connection.terminal"
            if ".accessories." in lib:
                category = "connection.accessory"
            for vendor, rec in vendors.items():
                if not isinstance(rec, dict):
                    continue
                article = str(rec.get("article") or "").strip()
                if not article or article in seen:
                    continue
                source = sources.get(article, "")
                if not source:
                    continue
                seen.add(article)
                manufacturer = normalize_manufacturer(str(vendor))
                designation = str(rec.get("designation") or article)
                part = CatalogPart(
                    manufacturer=manufacturer,
                    catalog=article,
                    library_id=lib,
                    category=category,
                    description=designation,
                    verified="full",
                    source=source,
                    source_date="2026-10-07",
                    mounting="din",
                    notes="Loaded from libraries/builtin/terminal_vendor_articles.yaml",
                    series="terminals",
                )
                self.parts[part.key()] = part
        self._loaded_files.append(path)

    def resolved_accessories(self, part: CatalogPart) -> list[CatalogPart]:
        """Accessory SKUs this part lists, skipping any number that is not already loaded.

        Never invents a catalog number. A listed SKU that is missing or ambiguous is omitted.
        """

        found: list[CatalogPart] = []
        seen: set[str] = set()
        for token in part.accessories:
            accessory = self._existing(token, part.manufacturer)
            if accessory is None:
                continue
            if accessory.key() in seen or accessory.key() == part.key():
                continue
            seen.add(accessory.key())
            found.append(accessory)
        return found

    def _existing(self, catalog: str, manufacturer: str | None = None) -> CatalogPart | None:
        try:
            return self.get(catalog, manufacturer)
        except KeyError:
            if not manufacturer:
                return None
        try:
            return self.get(catalog)
        except KeyError:
            return None

    def get(self, catalog: str, manufacturer: str | None = None) -> CatalogPart:
        cat = str(catalog or "").strip()
        if manufacturer:
            key = part_key(manufacturer, cat)
            try:
                return self.parts[key]
            except KeyError as exc:
                raise KeyError(f"unknown catalog part '{manufacturer} {cat}'") from exc
        hits = [p for p in self.parts.values() if p.catalog.casefold() == cat.casefold()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise KeyError(f"unknown catalog part '{cat}'")
        raise KeyError(f"catalog '{cat}' is used by more than one manufacturer; pass manufacturer=")

    def search(
        self,
        query: str = "",
        *,
        manufacturer: str | None = None,
        category: str | None = None,
        library_id: str | None = None,
        amps_min: float | None = None,
        amps_max: float | None = None,
    ) -> list[CatalogPart]:
        items = list(self.parts.values())
        if manufacturer:
            want = normalize_manufacturer(manufacturer).casefold()
            items = [p for p in items if p.manufacturer.casefold() == want]
        if category:
            cat = category.strip().lower()
            items = [p for p in items if p.category.lower() == cat or p.category.lower().startswith(cat)]
        if library_id:
            lid = library_id.strip().lower()
            items = [p for p in items if p.library_id.lower() == lid]
        if amps_min is not None or amps_max is not None:
            items = [p for p in items if _amp_window_overlaps(p, amps_min, amps_max)]
        q = query.lower().strip()
        if q:
            compact = q.replace(" ", "").replace("-", "").replace("/", "")
            items = [
                p
                for p in items
                if q in p.catalog.lower()
                or q in p.manufacturer.lower()
                or q in p.description.lower()
                or q in p.category.lower()
                or q in p.library_id.lower()
                or q in p.ratings_text().lower()
                or compact in p.catalog.lower().replace("-", "").replace(" ", "")
            ]
        return sorted(items, key=lambda p: (p.manufacturer, p.category, p.catalog))

    def manufacturers(self) -> list[str]:
        return sorted({p.manufacturer for p in self.parts.values()})

    def categories(self) -> list[str]:
        return sorted({p.category for p in self.parts.values()})

    def validate_against_symbols(self, library_ids: Iterable[str]) -> list[str]:
        known = set(library_ids)
        errors: list[str] = []
        for part in self.parts.values():
            if part.library_id not in known:
                errors.append(f"{part.manufacturer} {part.catalog}: unknown library_id '{part.library_id}'")
        return errors


def expand_part_row(
    raw: dict[str, Any],
    file_manufacturer: str,
    suffix_rules: dict[str, dict[str, Any]],
    path: Path,
) -> list[CatalogPart]:
    manufacturer = str(raw.get("manufacturer") or file_manufacturer or "").strip()
    template = str(raw.get("catalog_template") or "").strip()
    rule_id = str(raw.get("suffix_rule") or "").strip()
    if template and rule_id:
        rule = suffix_rules.get(rule_id)
        if not isinstance(rule, dict):
            raise ValueError(f"{path}: unknown suffix_rule '{rule_id}'")
        codes = {str(c.get("code")): c for c in (rule.get("codes") or []) if isinstance(c, dict)}
        wanted = raw.get("expand") or list(codes)
        placeholder = str(rule.get("placeholder") or "coil")
        out: list[CatalogPart] = []
        for code in wanted:
            token = str(code)
            spec = codes.get(token)
            if spec is None:
                raise ValueError(f"{path}: suffix '{token}' is not in rule '{rule_id}'")
            catalog = template.replace("{" + placeholder + "}", token)
            merged = dict(raw)
            merged.pop("catalog_template", None)
            merged.pop("expand", None)
            merged["catalog"] = catalog
            merged["manufacturer"] = manufacturer
            if spec.get("coil_voltage") and not merged.get("coil_voltage"):
                merged["coil_voltage"] = spec["coil_voltage"]
            if spec.get("heat_w") is not None and merged.get("heat_w") is None:
                merged["heat_w"] = spec["heat_w"]
            for key in ("coil_va_sealed", "coil_va_inrush", "power_w", "current_a"):
                if spec.get(key) is not None and merged.get(key) is None:
                    merged[key] = spec[key]
            merged["suffix_rule"] = rule_id
            if not merged.get("source"):
                merged["source"] = str(rule.get("source") or "")
            out.append(CatalogPart.model_validate(_clean_raw(merged, manufacturer)))
        return out
    return [CatalogPart.model_validate(_clean_raw(raw, manufacturer))]


def _clean_raw(raw: dict[str, Any], manufacturer: str) -> dict[str, Any]:
    data = {k: v for k, v in raw.items() if k not in {"catalog_template", "expand"}}
    data["manufacturer"] = str(data.get("manufacturer") or manufacturer)
    dims = data.get("dimensions_mm")
    if isinstance(dims, dict):
        data.setdefault("width_mm", dims.get("w", dims.get("width_mm")))
        data.setdefault("height_mm", dims.get("h", dims.get("height_mm")))
        data.setdefault("depth_mm", dims.get("d", dims.get("depth_mm")))
        data.pop("dimensions_mm", None)
    ratings = data.get("ratings")
    if isinstance(ratings, dict):
        for key in (
            "amps",
            "amps_min",
            "amps_max",
            "poles",
            "coil_voltage",
            "contact_config",
            "ka",
            "sccr",
            "sccr_ka",
            "heat_w",
            "coil_va_sealed",
            "coil_va_inrush",
            "power_w",
            "current_a",
            "va",
            "watt_loss",
            "watt_loss_source",
            "max_ambient_c",
        ):
            if key in ratings and data.get(key) in (None, ""):
                data[key] = ratings[key]
        data.pop("ratings", None)
    for key in list(data):
        if data[key] == "":
            if key in {
                "amps",
                "amps_min",
                "amps_max",
                "poles",
                "ka",
                "sccr_ka",
                "heat_w",
                "coil_va_sealed",
                "coil_va_inrush",
                "power_w",
                "current_a",
                "va",
                "watt_loss",
                "max_ambient_c",
                "width_mm",
                "height_mm",
                "depth_mm",
                "unit_cost",
            }:
                data[key] = None
    return data


def _amp_window_overlaps(
    part: CatalogPart,
    amps_min: float | None,
    amps_max: float | None,
) -> bool:
    """True when the part's published amp (or range) overlaps the filter window."""

    if part.amps_min is not None and part.amps_max is not None:
        low, high = float(part.amps_min), float(part.amps_max)
    elif part.amps is not None:
        low = high = float(part.amps)
    else:
        return False
    if amps_min is not None and high < float(amps_min):
        return False
    if amps_max is not None and low > float(amps_max):
        return False
    return True


def _article_sources(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _ARTICLE_COMMENT.finditer(text):
        found[match.group(1)] = match.group(2).rstrip(").,;")
    return found


_PARTS_CACHE: dict[tuple, PartsCatalog] = {}
_PARTS_CACHE_MAX = 4


def _parts_source_key(project_dir: Path | None) -> tuple:
    """Search path plus file mtimes. An overlay, a user library, or a saved edit misses."""

    env = os.environ.get("PANELCAD_LIBRARY_PATH", "")
    root = "" if project_dir is None else str(Path(project_dir).resolve())
    files: list[tuple[str, int, int]] = []
    for directory in parts_overlay_dirs(project_dir):
        try:
            folder = directory.stat()
        except OSError:
            files.append((str(directory), 0, 0))
            continue
        resolved_dir = directory.resolve()
        files.append((str(resolved_dir), int(folder.st_mtime_ns), 0))
        names = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
        for path in names:
            try:
                stat = path.stat()
            except OSError:
                files.append((str(path), 0, 0))
            else:
                files.append((str(resolved_dir / path.name), int(stat.st_mtime_ns), int(stat.st_size)))
    return (env, root, tuple(files))


def _detach_parts(source: PartsCatalog) -> PartsCatalog:
    """A new catalog dict. Replacing a row in one session does not change the next."""

    copy = PartsCatalog()
    copy.parts = dict(source.parts)
    copy.suffix_rules = dict(source.suffix_rules)
    copy._loaded_files = list(source._loaded_files)
    return copy


def default_parts_catalog(project_dir: Path | None = None) -> PartsCatalog:
    """Builtin parts library, plus user, project, and PANELCAD_LIBRARY_PATH overlays.

    The parsed files are kept while that search path and those mtimes stay put.
    Each call gets its own parts dict.
    """

    key = _parts_source_key(project_dir)
    found = _PARTS_CACHE.get(key)
    if found is None:
        found = PartsCatalog()
        found.load_defaults(project_dir)
        _PARTS_CACHE[key] = found
        while len(_PARTS_CACHE) > _PARTS_CACHE_MAX:
            _PARTS_CACHE.pop(next(iter(_PARTS_CACHE)))
    return _detach_parts(found)


def write_parts_csv(path: Path | str, parts: Iterable[CatalogPart]) -> Path:
    import csv

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = [p.to_row() for p in parts]
    fields = list(rows[0].keys()) if rows else [
        "manufacturer",
        "catalog",
        "library_id",
        "category",
        "description",
        "verified",
        "source",
    ]
    with dest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)
    return dest
