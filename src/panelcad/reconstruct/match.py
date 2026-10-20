"""Match photo-read part numbers to the verified manufacturer catalog."""

from __future__ import annotations

import re
from typing import Any

from panelcad.library.parts import CatalogPart, PartsCatalog
from panelcad.reconstruct.models import CatalogMatch

_COMPACT = re.compile(r"[^a-z0-9]+")
# Exact catalog numbers sit here. A shorter number inside a longer label does not.
LIKELY_CONFIDENCE = 0.8


def compact(value: str) -> str:
    return _COMPACT.sub("", (value or "").casefold())


def match_catalog(
    parts: PartsCatalog,
    *,
    part_number: str = "",
    brand: str = "",
    query: str = "",
    limit: int = 8,
    devices: Any = None,
) -> list[CatalogMatch]:
    """Propose catalog rows and library devices. Does not place anything.

    A library device counts only with the size that device published.
    Generic symbol sizes are not copied onto the match.
    """

    number = str(part_number or "").strip()
    brand_text = str(brand or "").strip()
    extra = str(query or "").strip()
    if not number and not extra and not brand_text:
        return []
    want = compact(number or extra)
    brand_c = compact(brand_text)
    hits: list[tuple[float, str, str, CatalogMatch]] = []
    pool = list(parts.parts.values()) if hasattr(parts, "parts") else []
    if not pool and hasattr(parts, "search"):
        pool = list(parts.search(number or extra or brand_text))
    for part in pool:
        score = _score(part, want, brand_c, number, extra)
        if score <= 0:
            continue
        hits.append((score, part.manufacturer, part.catalog, _match_from_part(part, score)))
    for device in _device_pool(devices):
        score = _score_device(device, want, brand_c, number, extra)
        if score <= 0:
            continue
        match = _match_from_device(device, score)
        if match is None:
            continue
        hits.append((score, match.manufacturer, match.catalog, match))
    hits.sort(key=lambda item: (-item[0], item[1], item[2]))
    out: list[CatalogMatch] = []
    seen: set[str] = set()
    for _score_value, _maker, _catalog, match in hits:
        key = f"{match.manufacturer}|{match.catalog}|{match.library_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(match)
        if len(out) >= limit:
            break
    return out


def _match_from_part(part: CatalogPart, score: float) -> CatalogMatch:
    return CatalogMatch(
        manufacturer=part.manufacturer,
        catalog=part.catalog,
        library_id=part.library_id,
        confidence=round(score, 3),
        verified=str(part.verified or ""),
        width_mm=part.width_mm,
        height_mm=part.height_mm,
        depth_mm=part.depth_mm,
        source=str(part.source or ""),
    )


def _device_pool(devices: Any) -> list[Any]:
    if devices is None:
        return []
    table = getattr(devices, "devices", None)
    if isinstance(table, dict):
        return list(table.values())
    if isinstance(devices, dict):
        return list(devices.values())
    return []


def _match_from_device(device: Any, score: float) -> CatalogMatch | None:
    library_id = str(getattr(device, "library_id", "") or "")
    if library_id.startswith("reconstruct."):
        return None
    props = getattr(device, "properties", None) or {}
    catalog = str(props.get("catalog") or props.get("part_number") or "").strip()
    if not catalog:
        catalog = str(getattr(device, "name", "") or "").strip()
    if not catalog:
        return None
    width, height, depth = _published_device_mm(device)
    return CatalogMatch(
        manufacturer=str(props.get("manufacturer") or "").strip(),
        catalog=catalog,
        library_id=library_id,
        confidence=round(score, 3),
        verified="library" if width and height and depth else "",
        width_mm=width,
        height_mm=height,
        depth_mm=depth,
        source=library_id,
    )


def _published_device_mm(device: Any) -> tuple[float | None, float | None, float | None]:
    """Library W×H×D only when that device published them. Never a generic size."""

    panel = getattr(device, "panel", None)
    if panel is None or str(getattr(panel, "dim_source", "") or "") != "catalog":
        return (None, None, None)

    def _positive(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    return (
        _positive(getattr(panel, "width_mm", None)),
        _positive(getattr(panel, "height_mm", None)),
        _positive(getattr(panel, "depth_mm", None)),
    )


def _score_device(device: Any, want: str, brand_c: str, number: str, extra: str) -> float:
    props = getattr(device, "properties", None) or {}
    cat = compact(str(props.get("catalog") or props.get("part_number") or ""))
    name = compact(str(getattr(device, "name", "") or ""))
    ident = compact(str(getattr(device, "id", "") or ""))
    mfr = compact(str(props.get("manufacturer") or ""))
    if brand_c and brand_c not in mfr and mfr not in brand_c and brand_c not in name:
        return 0.0
    score = 0.0
    if want and cat and cat == want:
        score = 0.95
    elif want and cat:
        score = _partial_score(want, cat)
    elif want and len(want) >= 4 and (want in name or want in ident):
        score = _partial_score(want, name or ident)
    elif number and cat and number.casefold() in str(props.get("catalog") or props.get("part_number") or "").casefold():
        score = _partial_score(compact(number), cat)
    elif extra and extra.casefold() in str(getattr(device, "name", "") or "").casefold():
        score = 0.45
    elif brand_c and brand_c in mfr and not want:
        score = 0.2
    return _with_brand(score, brand_c, mfr)


def _score(part: CatalogPart, want: str, brand_c: str, number: str, extra: str) -> float:
    cat = compact(part.catalog)
    mfr = compact(part.manufacturer)
    desc = compact(part.description)
    if brand_c and brand_c not in mfr and mfr not in brand_c:
        if brand_c not in desc:
            return 0.0
    score = 0.0
    if want and cat == want:
        score = 0.95
    elif want and (want in cat or cat in want):
        score = _partial_score(want, cat)
    elif number and number.casefold() in part.catalog.casefold():
        score = _partial_score(compact(number), cat)
    elif extra and extra.casefold() in part.description.casefold():
        score = 0.45
    elif brand_c and brand_c in mfr and not want:
        score = 0.2
    return _with_brand(score, brand_c, mfr)


def _partial_score(want: str, cat: str) -> float:
    """Containment is below an exact match, and below likely when the label is longer."""

    if not want or not cat or want == cat:
        return 0.0
    if want not in cat and cat not in want:
        return 0.0
    if min(len(want), len(cat)) < 4:
        return 0.55
    if len(want) > len(cat) and cat in want:
        return 0.5
    return 0.7


def _with_brand(score: float, brand_c: str, mfr: str) -> float:
    if not score or not brand_c or not (brand_c in mfr or mfr in brand_c):
        return score
    if score >= 0.9:
        return min(0.99, score + 0.04)
    return min(LIKELY_CONFIDENCE - 0.01, score + 0.04)


def catalog_dimension_mm(match: CatalogMatch, dimension: str = "width") -> float | None:
    token = str(dimension or "width").strip().lower()
    if token == "height":
        value = match.height_mm
    elif token == "depth":
        value = match.depth_mm
    else:
        value = match.width_mm
    if value is None:
        return None
    number = float(value)
    return number if number > 0 else None
