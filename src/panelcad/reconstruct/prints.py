"""Secondary print / previous-drawing input.

Panel photos are the source of truth for the as-built layout. An old drawing
(PDF, scan, or an already-redrawn PanelCAD project) is attached only to
cross-check tags, catalogs, and wire numbers.

When PR #28's importer is on the tree, PDF/scan extraction is delegated to
it. On main, a ``.pcad.yaml`` or a small print-index YAML is enough.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from panelcad.reconstruct.models import ObservedItem, PrintMismatch, Reconstruction, item_is_layout_ready

PRINT_INDEX_KIND = "print_index"


def classify_print_path(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name.endswith(".pcad.yaml"):
        return "project"
    if suffix in {".yaml", ".yml"}:
        return "index"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        return "scan"
    if suffix == ".dxf":
        return "other"
    return "other"


def load_print_index(path: Path | str) -> dict[str, Any]:
    """Tags and wires from a secondary print. Never invents missing fields."""

    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    kind = classify_print_path(src)
    if kind == "project" or src.name.endswith(".pcad.yaml"):
        return _from_project(src)
    if src.suffix.lower() in {".yaml", ".yml"}:
        return _from_index_yaml(src)
    imported = _from_importer(src)
    if imported is not None:
        imported["extracted"] = True
        imported["extractor"] = imported.get("extractor") or "importer"
        return imported
    return {
        "kind": kind,
        "path": str(src),
        "extracted": False,
        "extractor": "",
        "tags": [],
        "wires": [],
        "note": "print attached; extraction waits for the print-import pipeline",
    }


def compare_to_photos(
    store: Reconstruction,
    index: dict[str, Any],
) -> list[PrintMismatch]:
    """Flag photo vs print disagreements. Photos stay authoritative."""

    if not index.get("extracted"):
        return [
            PrintMismatch(
                check="reconstruct_print_unextracted",
                message="secondary print is attached but not extracted; load a PanelCAD project or wait for print-import",
            )
        ]
    photo_tags = _photo_tags(store)
    print_tags = {
        str(row.get("tag") or "").strip(): row
        for row in index.get("tags") or []
        if str(row.get("tag") or "").strip()
    }
    mismatches: list[PrintMismatch] = []
    for tag, row in print_tags.items():
        if tag not in photo_tags:
            mismatches.append(
                PrintMismatch(
                    check="reconstruct_print_missing_on_photos",
                    message=f"print has {tag} but no panel photo has confirmed that tag",
                    print_tag=tag,
                    print_value=str(row.get("catalog") or row.get("library_id") or ""),
                )
            )
            continue
        photo = photo_tags[tag]
        print_cat = _compact(row.get("catalog") or row.get("part_number") or "")
        photo_cat = _compact(photo.part_number or (photo.matches[0].catalog if photo.matches else ""))
        if print_cat and photo_cat and print_cat != photo_cat:
            mismatches.append(
                PrintMismatch(
                    check="reconstruct_print_catalog_mismatch",
                    message=f"{tag} catalog differs: photo {photo.part_number or photo_cat} vs print {row.get('catalog')}",
                    photo_tag=tag,
                    print_tag=tag,
                    photo_value=photo.part_number or photo_cat,
                    print_value=str(row.get("catalog") or ""),
                    photo_id=photo.photo_id,
                    item_id=photo.id,
                )
            )
    for tag, photo in photo_tags.items():
        if tag and tag not in print_tags:
            mismatches.append(
                PrintMismatch(
                    check="reconstruct_print_missing_on_print",
                    message=f"panel photo has {tag} but the attached print does not",
                    photo_tag=tag,
                    photo_value=photo.part_number,
                    photo_id=photo.photo_id,
                    item_id=photo.id,
                )
            )
    photo_wires = {str(item.wire_number or "").strip() for item in store.items if item.wire_number}
    for wire in index.get("wires") or []:
        number = str(wire.get("number") or wire.get("wire_number") or "").strip()
        if number and number not in photo_wires:
            mismatches.append(
                PrintMismatch(
                    check="reconstruct_print_wire_missing",
                    message=f"print wire {number} was not read on a panel photo",
                    print_value=number,
                    print_tag=str(wire.get("from") or ""),
                )
            )
    return mismatches


def _photo_tags(store: Reconstruction) -> dict[str, ObservedItem]:
    out: dict[str, ObservedItem] = {}
    for item in store.items:
        tag = str(item.tag or item.placed_ref or "").strip()
        if not tag:
            continue
        if item.unreadable and not item_is_layout_ready(item):
            continue
        out.setdefault(tag, item)
    return out


def _from_project(path: Path) -> dict[str, Any]:
    from panelcad.io.yaml_format import load_project

    project = load_project(path)
    tags: list[dict[str, Any]] = []
    seen: set[str] = set()
    wires: list[dict[str, Any]] = []
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id in seen:
                continue
            seen.add(comp.id)
            props = comp.properties or {}
            tags.append(
                {
                    "tag": comp.id,
                    "library_id": comp.library_id,
                    "catalog": str(props.get("catalog") or props.get("part_number") or ""),
                    "manufacturer": str(props.get("manufacturer") or ""),
                    "sheet": sheet.id,
                    "kind": sheet.kind,
                }
            )
        for wire in sheet.wires:
            wires.append(
                {
                    "number": str(wire.number or ""),
                    "from": wire.frm,
                    "to": wire.to,
                    "sheet": sheet.id,
                }
            )
    return {
        "kind": "project",
        "path": str(path),
        "extracted": True,
        "extractor": "panelcad_project",
        "name": project.name,
        "tags": tags,
        "wires": wires,
    }


def _from_index_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    tags = [dict(row) for row in data.get("tags") or [] if isinstance(row, dict)]
    wires = [dict(row) for row in data.get("wires") or [] if isinstance(row, dict)]
    return {
        "kind": "index",
        "path": str(path),
        "extracted": True,
        "extractor": "print_index",
        "tags": tags,
        "wires": wires,
        "note": str(data.get("note") or ""),
    }


def _from_importer(path: Path) -> dict[str, Any] | None:
    """Read an old print with the print-import pipeline. No vision account."""

    try:
        from panelcad.importer.pipeline import extract_print
    except ImportError:
        extract_print = None
    if extract_print is not None:
        try:
            payload = extract_print(path)
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict) and (payload.get("tags") or payload.get("wires") or payload.get("devices")):
            payload.setdefault("extractor", "importer.pipeline")
            payload.setdefault("extracted", True)
            return payload
    return _index_with_pipeline(path)


def _index_with_pipeline(path: Path) -> dict[str, Any] | None:
    """Vector text and strokes, or a raster page, without calling a model."""

    import tempfile

    if path.suffix.lower() not in {".pdf", ".dxf"}:
        return None
    try:
        from panelcad.importer.ingest import ingest_paths
        from panelcad.importer.workspace import Workspace
        from panelcad.library.catalog import default_catalog
    except ImportError:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="panelcad-print-") as tmp:
            workspace = Workspace(Path(tmp))
            pages = ingest_paths([path], workspace)
            catalog = default_catalog()
            tags: list[dict[str, Any]] = []
            wires: list[dict[str, Any]] = []
            for page in pages or []:
                facts = _page_facts(page, workspace, catalog)
                for device in facts.get("devices") or []:
                    if not isinstance(device, dict):
                        continue
                    tag = str(device.get("tag") or "").strip()
                    if not tag or device.get("unreadable"):
                        continue
                    tags.append(
                        {
                            "tag": tag,
                            "library_id": device.get("library_id") or "",
                            "catalog": device.get("catalog") or "",
                            "sheet": page.get("id") or "",
                        }
                    )
                for wire in facts.get("wires") or []:
                    if not isinstance(wire, dict):
                        continue
                    number = str(wire.get("number") or "").strip()
                    if not number:
                        continue
                    wires.append(
                        {
                            "number": number,
                            "from": wire.get("from") or wire.get("from_tag") or "",
                            "to": wire.get("to") or wire.get("to_tag") or "",
                        }
                    )
    except Exception:  # noqa: BLE001
        return None
    if not tags and not wires:
        return None
    return {
        "kind": classify_print_path(path),
        "path": str(path),
        "extracted": True,
        "extractor": "importer.pipeline",
        "tags": tags,
        "wires": wires,
    }


def _page_facts(page: dict[str, Any], workspace: Any, catalog: Any) -> dict[str, Any]:
    page_id = str(page.get("id") or "")
    tokens = workspace.read_json("pages", page_id, "words.json", default=[]) or []
    strokes = workspace.read_json("pages", page_id, "strokes.json", default=[]) or []
    width = float(page.get("width_mm") or 0)
    height = float(page.get("height_mm") or 0)
    if tokens and width > 0 and height > 0:
        from panelcad.importer.vectors import read_drawing

        return read_drawing(list(tokens), list(strokes or []), width, height, catalog)
    image_path = str(page.get("image") or "")
    if image_path and Path(image_path).is_file() and width > 0 and height > 0:
        try:
            from PIL import Image

            from panelcad.importer.raster import read_pixels

            with Image.open(image_path) as image:
                return read_pixels(image, width, height, catalog, allow_missing_ocr=True)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _compact(value: object) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())
