"""Map strip blocks to vendor article numbers from library data."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from panelcad.core.electrical import _device
from panelcad.core.models import Component
from panelcad.library.catalog import Catalog
from panelcad.library.paths import builtin_library_dir

_VENDOR_MFR = {
    "phoenix": frozenset({"phoenix", "phoenixcontact", "phx", "clipx"}),
    "wago": frozenset({"wago"}),
    "weidmueller": frozenset({"weidmueller", "weidmuller", "weidmüller", "wmc"}),
}

_MAP_NAMES = ("terminal_vendor_articles.yaml", "vendor_articles.yaml")


@dataclass
class MappedArticle:
    article: str
    designation: str = ""
    mapped: bool = False
    source: str = ""
    warning: str = ""
    manufacturer: str = ""


@dataclass
class ArticleTable:
    by_library: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    def lookup(self, library_id: str, vendor: str) -> dict[str, str] | None:
        keys = [library_id]
        if "." in library_id:
            keys.append(library_id.split(".", 1)[1])
        for key in keys:
            found = self.by_library.get(key, {}).get(vendor)
            if found and found.get("article"):
                return found
        return None


def _mfr_token(value: Any) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def manufacturer_matches(vendor: str, manufacturer: Any) -> bool:
    token = _mfr_token(manufacturer)
    return bool(token) and token in _VENDOR_MFR.get(vendor, ())


def _as_entry(raw: Any) -> dict[str, str] | None:
    if raw is None or raw is False:
        return None
    if isinstance(raw, dict):
        article = str(raw.get("article") or raw.get("part_number") or raw.get("catalog") or "").strip()
        if not article:
            return None
        designation = str(raw.get("designation") or raw.get("name") or "").strip()
        return {"article": article, "designation": designation}
    article = str(raw).strip()
    if not article:
        return None
    return {"article": article, "designation": ""}


def _props_entry(props: dict[str, Any], vendor: str) -> dict[str, str] | None:
    block = props.get("vendor_articles")
    if isinstance(block, dict):
        return _as_entry(block.get(vendor))
    return None


def load_article_table(catalog: Catalog) -> ArticleTable:
    table = ArticleTable()
    directories: list[Path] = []
    for path in getattr(catalog, "_loaded_files", []):
        parent = Path(path).parent
        if parent not in directories:
            directories.append(parent)
    builtin = builtin_library_dir()
    if builtin.is_dir() and builtin not in directories:
        directories.append(builtin)
    for directory in directories:
        for name in _MAP_NAMES:
            path = directory / name
            if not path.is_file():
                continue
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            raw = data.get("vendor_articles") or {}
            if not isinstance(raw, dict):
                continue
            for library_id, vendors in raw.items():
                if not isinstance(vendors, dict):
                    continue
                slot = table.by_library.setdefault(str(library_id), {})
                for vendor, entry in vendors.items():
                    parsed = _as_entry(entry)
                    if parsed:
                        slot[str(vendor)] = parsed
    return table


def map_article(
    catalog: Catalog,
    comp: Component | None,
    vendor: str,
    *,
    library_id: str = "",
    table: ArticleTable | None = None,
) -> MappedArticle:
    """Resolve one block to a vendor article. Component overrides library map."""

    table = table or load_article_table(catalog)
    device = _device(catalog, comp) if comp is not None else None
    lid = str((comp.library_id if comp is not None else "") or library_id or "")
    if not lid and device is not None:
        lid = device.library_id
    device_props = dict(device.properties) if device is not None else {}
    comp_props = dict(comp.properties) if comp is not None else {}
    merged = {**device_props, **comp_props}
    designation_fallback = str(device.name if device is not None else "") 

    entry = _props_entry(comp_props, vendor)
    if entry:
        return MappedArticle(
            article=entry["article"],
            designation=entry["designation"] or designation_fallback,
            mapped=True,
            source="component.vendor_articles",
            manufacturer=str(comp_props.get("manufacturer") or device_props.get("manufacturer") or ""),
        )

    part = str(comp_props.get("part_number") or comp_props.get("catalog") or "").strip()
    if part and manufacturer_matches(vendor, comp_props.get("manufacturer")):
        return MappedArticle(
            article=part,
            designation=str(comp_props.get("designation") or designation_fallback),
            mapped=True,
            source="component.part_number",
            manufacturer=str(comp_props.get("manufacturer") or ""),
        )

    entry = _props_entry(device_props, vendor)
    if entry:
        return MappedArticle(
            article=entry["article"],
            designation=entry["designation"] or designation_fallback,
            mapped=True,
            source="device.vendor_articles",
            manufacturer=str(device_props.get("manufacturer") or ""),
        )

    part = str(device_props.get("part_number") or device_props.get("catalog") or "").strip()
    if part and manufacturer_matches(vendor, device_props.get("manufacturer")):
        return MappedArticle(
            article=part,
            designation=designation_fallback,
            mapped=True,
            source="device.part_number",
            manufacturer=str(device_props.get("manufacturer") or ""),
        )

    found = table.lookup(lid, vendor)
    if found:
        return MappedArticle(
            article=found["article"],
            designation=found.get("designation") or designation_fallback,
            mapped=True,
            source="library.vendor_articles",
            manufacturer="",
        )

    fallback = str(
        merged.get("part_number") or merged.get("catalog") or lid or ""
    ).strip()
    warning = ""
    if fallback:
        warning = (
            f"{comp.id if comp is not None else lid or 'item'}: no {vendor} article "
            f"in library data; using '{fallback}'"
        )
    else:
        warning = (
            f"{comp.id if comp is not None else 'item'}: no {vendor} article "
            "and no part number"
        )
        fallback = lid or "UNKNOWN"
    return MappedArticle(
        article=fallback,
        designation=designation_fallback,
        mapped=False,
        source="fallback",
        warning=warning,
        manufacturer=str(merged.get("manufacturer") or ""),
    )
