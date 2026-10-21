"""Prep a print for an outside agent, then apply the JSON it returns.

PanelCAD does not call a model and does not ship an API key. ``prepare_print``
writes page images, overlapping tiles, and the vector or OCR text already in
the file. ``apply_response`` checks that JSON against the versioned schema and
against the extracted text, then builds a project through Session.

A symbol type is not a catalog part. A catalog number is used only when that
exact number is in the parts library and has published dimensions. Everything
else is a needs-info mark: no millimetres, off the BOM and the quote.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path
from typing import Any

from panelcad.importer.models import LOW_CONFIDENCE

SCHEMA_VERSION = "panelcad.agent-print/v1"
# Page tiles for an outside agent. 200–300 dpi keeps a symbol readable in a crop.
DEFAULT_DPI = 200
_MIN_DPI = 72
_MAX_DPI = 600
_PAGE_MARGIN_MM = 5.0
# OCR confuses these glyphs. A near-match is a candidate, not a rejection.
_CONFUSABLE = str.maketrans({"1": "I", "0": "O", "5": "S", "8": "B", "2": "Z"})

_TITLE_FIELDS = (
    "company",
    "project",
    "title",
    "drawing_number",
    "rev",
    "sheet_number",
    "sheet_count",
    "customer",
    "location",
    "drawn_by",
    "date",
    "scale",
    "standard",
)


def schema_file() -> Path:
    return Path(__file__).with_name("agent_response.v1.json")


def load_schema() -> dict[str, Any]:
    return json.loads(schema_file().read_text(encoding="utf-8"))


def validate_response(data: Any, schema: dict[str, Any] | None = None) -> list[str]:
    """Return schema errors. An empty list means the document matches v1."""

    errors: list[str] = []
    _validate(data, schema if schema is not None else load_schema(), "$", errors)
    return errors


def prepare_print(sources: list[Path | str], dest: Path | str, *, dpi: float = DEFAULT_DPI) -> dict[str, Any]:
    """Write the agent folder: page PNGs, tiles, text, lines, manifest, README, schema."""

    resolution = _require_dpi(dpi)
    paths = [Path(item) for item in sources]
    if not paths:
        raise ValueError("pass a print (PDF, image, or DXF)")
    out = Path(dest)
    if out.exists() and not out.is_dir():
        raise ValueError(f"{out} is not a folder")
    out.mkdir(parents=True, exist_ok=True)
    work = out / "_ingest"
    if work.exists():
        shutil.rmtree(work)
    try:
        pages = _ingest(paths, work, resolution)
        published = [_publish_page(out, page, work) for page in pages]
    finally:
        if work.exists():
            shutil.rmtree(work)
    if not published:
        raise ValueError("the print has no pages")
    schema_dir = out / "schema"
    schema_dir.mkdir(parents=True, exist_ok=True)
    schema_rel = "schema/agent_response.v1.json"
    shutil.copyfile(schema_file(), out / schema_rel)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "schema": schema_rel,
        "readme": "README.md",
        "dpi": resolution,
        "tile_mm": _tile_mm(),
        "overlap_mm": _overlap_mm(),
        "origin": "top-left",
        "units": "mm",
        "sources": [str(path) for path in paths],
        "pages": published,
    }
    _write_json(out / "manifest.json", manifest)
    readme = _readme(manifest)
    (out / "README.md").write_text(readme, encoding="utf-8")
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "out": str(out),
        "readme": str(out / "README.md"),
        "schema": str(out / schema_rel),
        "manifest": str(out / "manifest.json"),
        "dpi": resolution,
        "pages": [
            {"id": page["id"], "image": page["image"], "tiles": len(page.get("tiles") or [])}
            for page in published
        ],
    }


def apply_response(
    path: Path | str,
    *,
    out: Path | str | None = None,
    prep: Path | str | None = None,
) -> dict[str, Any]:
    """Validate agent JSON, check it against extracted text, and build the project."""

    source = Path(path)
    report: dict[str, Any] = {
        "ok": False,
        "schema_version": SCHEMA_VERSION,
        "project": "",
        "prep_dir": "",
        "accepted": [],
        "rejected": [],
        "conflicts": [],
        "needs_info": [],
        "problems": [],
        "errors": [],
    }
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report["errors"].append(str(exc).splitlines()[0])
        report["rejected"].append({"kind": "document", "id": source.name, "reason": report["errors"][-1]})
        return report
    if not isinstance(data, dict):
        report["errors"].append("response must be a JSON object")
        report["rejected"].append({"kind": "document", "id": source.name, "reason": report["errors"][-1]})
        return report
    if prep is not None:
        data = dict(data)
        data["prep_dir"] = str(prep)
    errors = validate_response(data)
    if errors:
        report["errors"] = errors
        report["rejected"].append({"kind": "document", "id": source.name, "reason": errors[0]})
        return report
    prep_dir = _resolve_prep(source, str(data["prep_dir"]))
    report["prep_dir"] = str(prep_dir)
    try:
        evidence = _load_evidence(prep_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report["errors"].append(str(exc).splitlines()[0])
        report["rejected"].append({"kind": "document", "id": "manifest", "reason": report["errors"][-1]})
        return report
    decisions = _decide(data, evidence)
    decisions["prep_dir"] = str(prep_dir)
    dest = _project_path(source, data, out, prep_dir)
    try:
        session = _build(evidence, decisions, dest)
    except (OSError, ValueError, KeyError) as exc:
        report["errors"].append(str(exc).splitlines()[0])
        report["rejected"] = list(decisions["rejected"])
        report["rejected"].append({"kind": "project", "id": dest.name, "reason": report["errors"][-1]})
        report["accepted"] = list(decisions["accepted"])
        report["conflicts"] = list(decisions["conflicts"])
        report["needs_review"] = list(decisions.get("needs_review") or [])
        report["needs_info"] = list(decisions["needs_info"])
        report["counts"] = _counts(decisions)
        return report
    report["accepted"] = list(decisions["accepted"])
    report["rejected"] = list(decisions["rejected"])
    report["conflicts"] = list(decisions["conflicts"])
    report["needs_review"] = list(decisions.get("needs_review") or [])
    report["needs_info"] = list(decisions["needs_info"])
    report["counts"] = _counts(decisions)
    report["project"] = str(dest)
    report["problems"] = session.design_checks()
    from panelcad.importer.agent_questions import questions_for

    report["questions"] = questions_for(session)
    report["ok"] = True
    return report


def _require_dpi(dpi: float) -> int:
    try:
        value = int(round(float(dpi)))
    except (TypeError, ValueError) as exc:
        raise ValueError("dpi must be a number") from exc
    if value < _MIN_DPI or value > _MAX_DPI:
        raise ValueError(f"dpi must be between {_MIN_DPI} and {_MAX_DPI}")
    return value


def _tile_mm() -> float:
    from panelcad.importer.tile import TILE_MM

    return float(TILE_MM)


def _overlap_mm() -> float:
    from panelcad.importer.tile import OVERLAP_MM

    return float(OVERLAP_MM)


def _ingest(paths: list[Path], work: Path, dpi: int) -> list[dict[str, Any]]:
    from panelcad.importer.ingest import ingest_paths, is_print_path
    from panelcad.importer.workspace import Workspace

    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if not is_print_path(path):
            raise ValueError(f"not a print: {path.name}")
    # `--dpi` resamples PDF and vector pages. A scan keeps its own DPI for millimetres.
    return ingest_paths(paths, Workspace(work), preview_dpi=dpi)


def _publish_page(dest: Path, page: dict[str, Any], work: Path) -> dict[str, Any]:
    from panelcad.importer.tile import _grid, _trim_title_report, crop_tile, tokens_in_tile

    page_id = str(page["id"])
    pages = dest / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    image_src = Path(str(page.get("image") or ""))
    image_rel = f"pages/{page_id}.png"
    if image_src.is_file():
        shutil.copyfile(image_src, dest / image_rel)
    tokens = _read_work_json(work, page_id, "words.json")
    strokes = _read_work_json(work, page_id, "strokes.json")
    if not tokens and not page.get("vector"):
        ocr_tokens, ocr = _ocr_tokens(dest / image_rel, float(page["width_mm"]), float(page["height_mm"]))
        tokens = ocr_tokens
    else:
        ocr = "vector" if tokens else "none"
    if not strokes:
        source = Path(str(page.get("source") or ""))
        if source.suffix.lower() == ".dxf":
            strokes = []
            lines = _lines_from_dxf(source, float(page.get("height_mm") or 0))
        else:
            lines = []
    else:
        lines = _lines_from_strokes(strokes if isinstance(strokes, list) else [])
    text_rows = tokens if isinstance(tokens, list) else []
    record = {
        "id": page_id,
        "width_mm": page.get("width_mm"),
        "height_mm": page.get("height_mm"),
        "vector": bool(page.get("vector")),
        "ocr": ocr,
        "page_text": str(page.get("text") or ""),
        "text": [_public_token(item) for item in text_rows if isinstance(item, dict)],
        "lines": lines,
    }
    text_rel = f"pages/{page_id}.json"
    _write_json(dest / text_rel, record)
    tile_page = {
        "id": page_id,
        "width_mm": float(page.get("width_mm") or 0),
        "height_mm": float(page.get("height_mm") or 0),
        "image": str(dest / image_rel),
    }
    # The whole drawing grid, including light tiles. Ink skipping is for a paid
    # vision call. An outside agent reads the images locally.
    grid = _grid(tile_page, tile_page["width_mm"], tile_page["height_mm"])
    kept, skipped = _trim_title_report(grid, tile_page["height_mm"])
    if not kept:
        kept, skipped = grid, []
    tiles: list[dict[str, Any]] = []
    tile_dir = pages / page_id / "tiles"
    for tile in kept:
        tile_rel = f"pages/{page_id}/tiles/{tile['id']}.png"
        if image_src.is_file() or (dest / image_rel).is_file():
            crop_tile(tile_page, tile, dest / tile_rel)
        text_name = f"pages/{page_id}/tiles/{tile['id']}.json"
        _write_json(
            dest / text_name,
            {
                "id": tile["id"],
                "x": tile["x"],
                "y": tile["y"],
                "w": tile["w"],
                "h": tile["h"],
                "text": tokens_in_tile(record["text"], tile),
            },
        )
        tiles.append(
            {
                "id": tile["id"],
                "image": tile_rel,
                "text": text_name,
                "x": tile["x"],
                "y": tile["y"],
                "w": tile["w"],
                "h": tile["h"],
            }
        )
    return {
        "id": page_id,
        "image": image_rel,
        "width_mm": page.get("width_mm"),
        "height_mm": page.get("height_mm"),
        "vector": bool(page.get("vector")),
        "ocr": ocr,
        "text": text_rel,
        "tiles": tiles,
        "tiles_skipped": [
            {"id": item.get("id"), "reason": item.get("skip_reason")}
            for item in skipped
            if isinstance(item, dict)
        ],
    }


def _read_work_json(work: Path, page_id: str, name: str) -> Any:
    path = work / "pages" / page_id / name
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _public_token(item: dict[str, Any]) -> dict[str, Any]:
    row = {
        "text": str(item.get("text") or ""),
        "x": item.get("x"),
        "y": item.get("y"),
        "w": item.get("w"),
        "h": item.get("h"),
    }
    if item.get("confidence") is not None:
        row["confidence"] = item.get("confidence")
    return row


def _lines_from_strokes(strokes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for stroke in strokes:
        if stroke.get("kind") != "line":
            continue
        a = stroke.get("a") or [None, None]
        b = stroke.get("b") or [None, None]
        if len(a) < 2 or len(b) < 2:
            continue
        lines.append(
            {
                "x0": a[0],
                "y0": a[1],
                "x1": b[0],
                "y1": b[1],
                "role": str(stroke.get("role") or ""),
            }
        )
    return lines


def _lines_from_dxf(path: Path, height_mm: float) -> list[dict[str, Any]]:
    try:
        import ezdxf
    except ImportError:
        return []
    try:
        doc = ezdxf.readfile(str(path))
    except Exception:  # noqa: BLE001 — an unreadable DXF still preps; lines stay empty
        return []
    lines: list[dict[str, Any]] = []

    def add(x0: float, y0: float, x1: float, y1: float) -> None:
        lines.append(
            {
                "x0": round(float(x0), 3),
                "y0": round(height_mm - float(y0), 3),
                "x1": round(float(x1), 3),
                "y1": round(height_mm - float(y1), 3),
                "role": "dxf",
            }
        )

    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind == "LINE":
            a, b = entity.dxf.start, entity.dxf.end
            add(a[0], a[1], b[0], b[1])
        elif kind == "LWPOLYLINE":
            pts = list(entity.get_points("xy"))
            for left, right in zip(pts, pts[1:], strict=False):
                add(left[0], left[1], right[0], right[1])
    return lines


def _ocr_tokens(image_path: Path, width_mm: float, height_mm: float) -> tuple[list[dict[str, Any]], str]:
    if not image_path.is_file() or width_mm <= 0 or height_mm <= 0:
        return [], "unavailable"
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return [], "unavailable"
    try:
        image = Image.open(image_path)
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except Exception:  # noqa: BLE001 — a missing tesseract binary is "no OCR", not a failed prep
        return [], "unavailable"
    px_w = max(1, int(image.width))
    px_h = max(1, int(image.height))
    tokens: list[dict[str, Any]] = []
    texts = data.get("text") or []
    for index, raw in enumerate(texts):
        label = str(raw or "").strip()
        if not label:
            continue
        try:
            conf = float((data.get("conf") or [])[index])
        except (TypeError, ValueError, IndexError):
            conf = -1
        if conf < 0:
            continue
        try:
            left = float(data["left"][index])
            top = float(data["top"][index])
            width = float(data["width"][index])
            height = float(data["height"][index])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        tokens.append(
            {
                "text": label,
                "x": round((left + width / 2.0) / px_w * width_mm, 2),
                "y": round((top + height / 2.0) / px_h * height_mm, 2),
                "w": round(width / px_w * width_mm, 2),
                "h": round(height / px_h * height_mm, 2),
                "confidence": round(conf / 100.0, 3),
            }
        )
    return tokens, "tesseract"


def _readme(manifest: dict[str, Any]) -> str:
    example = json.dumps(load_schema()["examples"][0], indent=2)
    pages = "\n".join(
        f"- `{page['id']}` {page.get('width_mm')} x {page.get('height_mm')} mm  image `{page['image']}`  text `{page['text']}`  tiles {len(page.get('tiles') or [])}"
        for page in manifest.get("pages") or []
    )
    return f"""# PanelCAD print for an outside agent

PanelCAD did not call a model. There is no API key in this folder.
Read the page images and the extracted text, then return one JSON object.

## Folder

- `manifest.json` lists pages, tiles, and millimetre sizes.
- `schema/agent_response.v1.json` is the contract (`{SCHEMA_VERSION}`).
- Page coordinates are millimetres. Origin is the top-left of the page.
- Tiles overlap by {manifest.get('overlap_mm')} mm. `--dpi` is the page PNG resolution (72–600, default 200). Tiles are crops of that PNG, so they use the same resolution. 200–300 keeps a symbol readable. Any value in range still writes tiles.
- Each tile PNG has a JSON file of the vector or OCR text whose centre falls in that tile, including the overlap.
- `pages/<id>.json` is extracted text and lines for the whole page. On a vector page, copy tags and wire numbers from that text. On a scan, the image is the source and the OCR text is a hint. A tag the OCR missed is confirmed by a person. Do not invent a character you cannot see.
- A wire row has two ends (`device.terminal`). A net with three or more terminals is several rows that share one number.

## Pages

{pages}

## What to return

Devices: tag, symbol type, NO or NC, position (x, y millimetres), sheet, rung, and a bbox.
Terminals: device, terminal name, and a bbox.
Wire numbers, endpoints as `device.terminal` (the first dot separates them, for example `Q1.2`), and a bbox around the wire number.
A net with three or more terminals is several rows that share one number, each row naming two ends.
Title block fields, copied from the print.
`confidence` from 0 to 1, and `needs_info` when you are not sure.
Every bbox is millimetres on the full page, origin top-left, so a person can confirm that crop.

Do not guess a catalog number or a manufacturer. Leave `catalog` empty unless it is printed.
Do not send millimetres for a part. PanelCAD uses published catalog dimensions only.
A symbol type such as `relay` is not a part. Set `needs_info` true when the part is unknown.
Set `prep_dir` to this folder.

Save the JSON and tell the person to run:

```
panelcad import apply RESPONSE.json --out project.pcad.yaml
```

## Example

```json
{example}
```
"""


def _resolve_prep(source: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = (source.parent / path).resolve()
    return path


def _load_evidence(prep_dir: Path) -> dict[str, dict[str, Any]]:
    manifest_path = prep_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"no manifest.json in {prep_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if str(manifest.get("schema_version") or "") != SCHEMA_VERSION:
        raise ValueError(
            f"prep folder is {manifest.get('schema_version') or 'unversioned'}; this apply expects {SCHEMA_VERSION}"
        )
    pages: dict[str, dict[str, Any]] = {}
    for page in manifest.get("pages") or []:
        if not isinstance(page, dict) or not page.get("id"):
            continue
        text_path = prep_dir / str(page.get("text") or "")
        body: dict[str, Any] = {}
        if text_path.is_file():
            loaded = json.loads(text_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                body = loaded
        tokens = [item for item in body.get("text") or [] if isinstance(item, dict)]
        page_text = str(body.get("page_text") or "")
        joined = " ".join(str(item.get("text") or "") for item in tokens)
        blob = f"{page_text}\n{joined}"
        pages[str(page["id"])] = {
            "id": str(page["id"]),
            "width_mm": float(page.get("width_mm") or body.get("width_mm") or 0),
            "height_mm": float(page.get("height_mm") or body.get("height_mm") or 0),
            "tokens": {_norm(str(item.get("text") or "")) for item in tokens if str(item.get("text") or "").strip()},
            "token_rows": tokens,
            "lines": [item for item in body.get("lines") or [] if isinstance(item, dict)],
            "title_block": body.get("title_block"),
            "border": body.get("border"),
            "image": str(page.get("image") or ""),
            "vector": bool(body.get("vector")) or str(body.get("ocr") or "") == "vector",
            "blob": blob,
            "compact": _compact(blob),
            "has_text": bool(tokens) or bool(page_text.strip()),
        }
    if not pages:
        raise ValueError("manifest has no pages")
    return pages


def _decide(data: dict[str, Any], pages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    needs_info: list[str] = []
    devices = _winning_devices(data.get("devices") or [], pages, accepted, rejected, conflicts, needs_info)
    from panelcad.importer.agent_review import review_devices

    needs_review = review_devices(devices, pages)
    _decide_title(data.get("title_block") or {}, pages, accepted, conflicts)
    terminals = _decide_terminals(data.get("terminals") or [], devices, pages, accepted, rejected)
    held_wires: list[dict[str, Any]] = []
    wires = _decide_wires(
        data.get("wires") or [], devices, pages, accepted, rejected, conflicts, needs_review, held_wires
    )
    return {
        "accepted": accepted,
        "rejected": rejected,
        "conflicts": conflicts,
        "needs_review": needs_review,
        "needs_info": needs_info,
        "held_wires": held_wires,
        "devices": devices,
        "terminals": terminals,
        "wires": wires,
        "title_block": data.get("title_block") or {},
        "project_name": str(data.get("project_name") or ""),
    }


def _winning_devices(
    rows: list[Any],
    pages: dict[str, dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    needs_info: list[str],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    pending: list[tuple[int, dict[str, Any], str]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        page_id = _page_id(str(raw.get("sheet") or ""), pages)
        tag = str(raw.get("tag") or "").strip()
        if page_id is None:
            rejected.append({"kind": "device", "id": tag or f"#{index}", "sheet": str(raw.get("sheet") or ""), "reason": "sheet is not in the prep manifest"})
            continue
        page = pages[page_id]
        if not tag:
            rejected.append({"kind": "device", "id": f"#{index}", "sheet": page_id, "reason": "tag is empty"})
            continue
        if not _inside(raw, page):
            rejected.append(
                {
                    "kind": "device",
                    "id": tag,
                    "sheet": page_id,
                    "reason": "position is outside the page",
                    "bbox": raw.get("bbox") if isinstance(raw.get("bbox"), dict) else None,
                    "image": str(page.get("image") or ""),
                    "x": raw.get("x"),
                    "y": raw.get("y"),
                }
            )
            continue
        pending.append((index, raw, page_id))
    for index, raw, page_id in pending:
        page = pages[page_id]
        tag = str(raw.get("tag") or "").strip()
        missing = bool(page["has_text"] and not _exact(tag, page))
        # Vector text is the page. OCR is a hint: a miss is confirmed, not dropped.
        if missing and page.get("vector"):
            from panelcad.importer.agent_review import covering_token

            longer = covering_token(tag, page.get("token_rows") or [])
            reason = "tag is not in the extracted text"
            if longer:
                reason += f"; extracted text has {longer}"
            rejected.append(
                {
                    "kind": "device",
                    "id": tag,
                    "sheet": page_id,
                    "reason": reason,
                    "bbox": raw.get("bbox") if isinstance(raw.get("bbox"), dict) else None,
                    "candidates": [longer] if longer else [],
                    "image": str(page.get("image") or ""),
                    "x": raw.get("x"),
                    "y": raw.get("y"),
                }
            )
            continue
        item = dict(raw)
        if missing:
            item["_confirm_tag"] = True
            item["_near"] = _near_readings(tag, page)
        item["_page"] = page_id
        item["_index"] = index
        key = (page_id, tag.upper())
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(item)
    winners: list[dict[str, Any]] = []
    for key in order:
        group = grouped[key]
        group.sort(key=lambda item: (-_confidence(item), int(item["_index"])))
        winner = group[0]
        contact_clash = False
        for other in group[1:]:
            from panelcad.importer.agent_review import coil_and_contact

            if coil_and_contact(winner, other):
                _keep_coil(winner, other)
                continue
            if _contact_clash(winner, other):
                contact_clash = True
                conflicts.append(
                    {
                        "kind": "device",
                        "id": str(other.get("tag") or ""),
                        "sheet": key[0],
                        "reason": "NO and NC were both read; neither is used",
                    }
                )
                continue
            if _device_signature(other) != _device_signature(winner):
                conflicts.append(
                    {
                        "kind": "device",
                        "id": str(other.get("tag") or ""),
                        "sheet": key[0],
                        "reason": f"another reading of {winner.get('tag')} was kept (confidence {_confidence(winner):.2f})",
                    }
                )
            else:
                _merge_terminals(winner, other)
        if contact_clash:
            winner["contact"] = ""
            winner["_contact_review"] = "NO and NC were both read; neither is used"
        placed_as, reason = _part_decision(winner)
        if placed_as == "needs-info" and str(winner.get("tag") or "") not in needs_info:
            needs_info.append(str(winner.get("tag") or ""))
        winner["_as"] = placed_as
        winner["_reason"] = reason
        winner["_image"] = str(pages[key[0]].get("image") or "")
        accepted.append(
            {
                "kind": "device",
                "id": str(winner.get("tag") or ""),
                "page": key[0],
                "sheet": key[0],
                "as": placed_as,
                "reason": reason,
            }
        )
        winners.append(winner)
    return winners


def _keep_coil(winner: dict[str, Any], other: dict[str, Any]) -> None:
    """Keep the coil as the device. A contact symbol that is not the coil's family is needs-review."""

    from panelcad.importer.agent_review import _is_coil_symbol, _is_contact_symbol

    coil = winner if _is_coil_symbol(str(winner.get("symbol") or "")) else other
    contact = other if coil is winner else winner
    if not _is_contact_symbol(str(contact.get("symbol") or "")):
        return
    winner["symbol"] = coil.get("symbol")
    if "timer" in str(coil.get("symbol") or "").casefold() and "timer" not in str(contact.get("symbol") or "").casefold():
        winner["_symbol_review"] = "contact symbol does not match its timer coil"
    _merge_terminals(winner, contact)


def _contact_clash(winner: dict[str, Any], other: dict[str, Any]) -> bool:
    """Same symbol, both contact states filled, and they disagree. Do not pick one."""

    if str(winner.get("symbol") or "").casefold() != str(other.get("symbol") or "").casefold():
        return False
    left = str(winner.get("contact") or "").upper()
    right = str(other.get("contact") or "").upper()
    return left in {"NO", "NC"} and right in {"NO", "NC"} and left != right


def _part_decision(device: dict[str, Any]) -> tuple[str, str]:
    """Catalog part only when the number is published with dimensions. Never from the symbol type."""

    if bool(device.get("needs_info")):
        return "needs-info", "agent marked needs-info"
    if _confidence(device) < LOW_CONFIDENCE:
        return "needs-info", f"confidence {_confidence(device):.2f} is below {LOW_CONFIDENCE:.2f}"
    catalog = str(device.get("catalog") or "").strip()
    if not catalog:
        return "needs-info", "no catalog part; a symbol type is not a part"
    return "catalog-candidate", catalog


def _decide_title(
    block: dict[str, Any],
    pages: dict[str, dict[str, Any]],
    accepted: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> None:
    unread = {str(item).strip() for item in block.get("unreadable_fields") or [] if str(item).strip()}
    any_text = any(page["has_text"] for page in pages.values())
    for field in _TITLE_FIELDS:
        if field in unread:
            value = str(block.get(field) or "").strip()
            if value:
                conflicts.append({"kind": "title_block", "id": field, "reason": "marked unreadable and also given a value; left blank"})
            continue
        value = str(block.get(field) or "").strip()
        if not value:
            continue
        if any_text and not any(_phrase(value, page) for page in pages.values()):
            conflicts.append({"kind": "title_block", "id": field, "reason": "not in the extracted text; left blank"})
            continue
        reason = "matches extracted text" if any_text else "extracted text was not available"
        accepted.append({"kind": "title_block", "id": field, "as": "title", "reason": reason})


def _decide_terminals(
    rows: list[Any],
    devices: list[dict[str, Any]],
    pages: dict[str, dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    known = {(str(item["_page"]), str(item.get("tag") or "").upper()) for item in devices}
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        device = str(raw.get("device") or "").strip()
        terminal = str(raw.get("terminal") or "").strip()
        page_id = _page_id(str(raw.get("sheet") or ""), pages)
        if page_id is None or (page_id, device.upper()) not in known:
            rejected.append(
                {
                    "kind": "terminal",
                    "id": f"{device}.{terminal}".strip("."),
                    "sheet": str(raw.get("sheet") or ""),
                    "reason": "device is not an accepted device on that sheet",
                }
            )
            continue
        key = (page_id, device.upper(), terminal.upper())
        if key in seen:
            continue
        seen.add(key)
        kept.append({"page": page_id, "device": device, "terminal": terminal})
        accepted.append(
            {
                "kind": "terminal",
                "id": f"{device}.{terminal}",
                "page": page_id,
                "sheet": page_id,
                "as": "pin",
                "reason": "named on an accepted device",
            }
        )
    return kept


def _decide_wires(
    rows: list[Any],
    devices: list[dict[str, Any]],
    pages: dict[str, dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    needs_review: list[dict[str, Any]],
    held_wires: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    known = {(str(item["_page"]), str(item.get("tag") or "").upper()): item for item in devices}
    kept: list[dict[str, Any]] = []
    by_ends: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        page_id = _page_id(str(raw.get("sheet") or ""), pages)
        number = str(raw.get("number") or "").strip()
        label = number or f"#{index}"
        if page_id is None:
            rejected.append({"kind": "wire", "id": label, "sheet": str(raw.get("sheet") or ""), "reason": "sheet is not in the prep manifest"})
            continue
        page = pages[page_id]
        frm = str(raw.get("from") or "").strip()
        to = str(raw.get("to") or "").strip()
        ends = []
        missing = False
        for endpoint in (frm, to):
            device, _pin = _split_endpoint(endpoint)
            if (page_id, device.upper()) not in known:
                row = _wire_reject(
                    label,
                    page,
                    raw,
                    f"endpoint {endpoint} is not an accepted device",
                    frm=frm,
                    to=to,
                )
                rejected.append(row)
                held_wires.append(_held_wire(label, page_id, raw, frm, to, page))
                missing = True
                break
            ends.append(endpoint)
        if missing:
            continue
        text_miss = bool(number and page["has_text"] and not _exact(number, page))
        if text_miss and page.get("vector"):
            rejected.append(
                _wire_reject(label, page, raw, "wire number is not in the extracted text", frm=frm, to=to)
            )
            continue
        number_unconfirmed = text_miss
        if not number and not bool(raw.get("needs_info")):
            rejected.append({"kind": "wire", "id": label, "sheet": page_id, "reason": "wire number is empty"})
            continue
        signature = (page_id, _end_key(ends[0], ends[1]))
        item = {
            "page": page_id,
            "number": number,
            "from": frm,
            "to": to,
            "confidence": _confidence(raw),
            "needs_info": bool(raw.get("needs_info")),
            "bbox": raw.get("bbox") if isinstance(raw.get("bbox"), dict) else None,
            "image": str(page.get("image") or ""),
            "_number_unconfirmed": number_unconfirmed,
        }
        previous = by_ends.get(signature)
        if previous is None:
            by_ends[signature] = item
            continue
        if previous["number"] == number:
            continue
        if item["confidence"] > previous["confidence"]:
            conflicts.append(_wire_conflict(previous, page, f"same endpoints already use wire number {item['number'] or '(blank)'}"))
            by_ends[signature] = item
        else:
            conflicts.append(_wire_conflict(item, page, f"same endpoints already use wire number {previous['number'] or '(blank)'}"))
    from panelcad.importer.agent_review import review_wire

    for item in by_ends.values():
        page = pages[item["page"]]
        if item.get("_number_unconfirmed"):
            issue = "PanelCAD's text did not read this wire number"
        else:
            issue = review_wire(item, known, page) if item["number"] else ""
        if not item["number"] and not page["has_text"]:
            reason = "endpoints accepted; extracted text was not available"
        elif issue:
            reason = issue
            item["needs_review"] = True
            review_id = f"wire:{item['page']}:{item['number']}:{item['from']}:{item['to']}"
            item["review_id"] = review_id
            needs_review.append(
                {
                    "review_id": review_id,
                    "kind": "wire",
                    "id": item["number"] or f"{item['from']}->{item['to']}",
                    "page": item["page"],
                    "sheet": item["page"],
                    "reason": issue,
                    "check": "needs_review",
                    "from": item["from"],
                    "to": item["to"],
                    "bbox": item.get("bbox"),
                    "image": str(page.get("image") or ""),
                    "candidates": [item["from"], item["to"]],
                }
            )
        elif not page["has_text"]:
            reason = "endpoints accepted; extracted text was not available"
        else:
            reason = "wire number is on a traced conductor and both ends touch it"
        accepted.append(
            {
                "kind": "wire",
                "id": item["number"] or f"{item['from']}->{item['to']}",
                "page": item["page"],
                "sheet": item["page"],
                "as": "needs-review" if item.get("needs_review") else "wire",
                "number": item["number"],
                "from": item["from"],
                "to": item["to"],
                "reason": reason,
            }
        )
        kept.append(item)
    return kept


def _build(pages: dict[str, dict[str, Any]], decisions: dict[str, Any], dest: Path) -> Any:
    from panelcad.api.session import Session
    from panelcad.importer.build import _apply_page_size, _remember_needs_info_pin
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    name = decisions["project_name"] or "Imported print"
    session = Session.create(name, sheet_id="import-hold", title="hold")
    sheet_of: dict[str, str] = {}
    for page_id, page in pages.items():
        created = session.add_sheet(page_id, title=page_id, kind="schematic")
        _apply_page_size(created, {"width_mm": page["width_mm"], "height_mm": page["height_mm"]})
        sheet_of[page_id] = created.id
    if len(session.project.sheets) > 1:
        session.delete_sheet("import-hold")
    _write_title(session, decisions, pages)
    placed: dict[tuple[str, str], Any] = {}
    for page_id, page in pages.items():
        page["device_points"] = [
            {
                "tag": str(item.get("tag") or ""),
                "x": item.get("x"),
                "y": item.get("y"),
                "bbox": item.get("bbox") if isinstance(item.get("bbox"), dict) else None,
            }
            for item in decisions["devices"]
            if str(item.get("_page") or "") == page_id
        ]
    for device in decisions["devices"]:
        page_id = str(device["_page"])
        sheet_id = sheet_of[page_id]
        tag = str(device.get("tag") or "").strip()
        comp = _place_device(session, device, sheet_id, NEEDS_INFO_LIBRARY_ID, pages.get(page_id))
        if comp is None:
            decisions["accepted"][:] = [
                row
                for row in decisions["accepted"]
                if not (row.get("kind") == "device" and row.get("id") == tag and row.get("page") == page_id)
            ]
            decisions["rejected"].append({"kind": "device", "id": tag, "sheet": page_id, "reason": "could not be placed"})
            continue
        placed[(page_id, tag.upper())] = comp
        _stamp_sheet(decisions, tag, page_id, sheet_id)
    _attach_pins(session, decisions, placed, sheet_of, _remember_needs_info_pin)
    _place_wires(session, decisions, placed, sheet_of)
    _sync_needs_info(decisions)
    session.project.properties["print_import"] = {
        "source": "agent-print",
        "schema_version": SCHEMA_VERSION,
        "prep_dir": str(decisions.get("prep_dir") or ""),
        "items": _problem_items(decisions),
        "held_wires": list(decisions.get("held_wires") or []),
    }
    if sheet_of:
        session.set_active_sheet(next(iter(sheet_of.values())))
    dest.parent.mkdir(parents=True, exist_ok=True)
    session.save(dest)
    return session


def _place_device(session: Any, device: dict[str, Any], sheet_id: str, needs_id: str, page: dict[str, Any] | None = None) -> Any | None:
    tag = str(device.get("tag") or "").strip()
    x = float(device.get("x") or 0)
    y = float(device.get("y") or 0)
    props = _reading_properties(device)
    catalog = str(device.get("catalog") or "").strip()
    manufacturer = str(device.get("manufacturer") or "").strip()
    if device.get("_as") == "catalog-candidate" and catalog:
        part = _published_part(session, catalog, manufacturer)
        cap = 0.0 if part is None else _symbol_reach_cap(session.catalog, str(part.library_id or ""))
        blocked = "" if part is None else _catalog_block(device, page, part, cap)
        if part is None:
            device["_as"] = "needs-info"
            device["_reason"] = _catalog_miss_reason(session, catalog, manufacturer)
        elif blocked:
            device["_as"] = "needs-info"
            device["_reason"] = blocked
            device["_catalog_review"] = blocked
        else:
            comp = None
            try:
                session.catalog.get(part.library_id)
                comp = session.place(
                    part.library_id,
                    ref=tag,
                    x=x,
                    y=y,
                    sheet_id=sheet_id,
                    snap=False,
                    properties=props,
                )
                session.assign_part(tag, part.catalog, part.manufacturer, sheet_id=sheet_id)
            except (KeyError, ValueError, LookupError):
                if comp is not None:
                    session.delete_component(tag, sheet_id=sheet_id)
                device["_as"] = "needs-info"
                device["_reason"] = "catalog part has no matching library symbol"
            else:
                device["_as"] = "catalog"
                device["_reason"] = f"catalog part {part.catalog} has published dimensions"
                _update_device_row(device, "catalog", device["_reason"])
                return comp
    if device.get("_as") != "catalog":
        _update_device_row(device, "needs-info", str(device.get("_reason") or "needs-info"))
    note = str(device.get("_reason") or "needs a catalog part")
    props.update(
        {
            "reconstruct_needs_info": "yes",
            "description": "INFO NEEDED",
            "label_text": tag,
            "needs_missing": note,
            "bom": "no",
            "in_panel": "no",
            "incabinet": "no",
            "print_import_anchor": "yes",
            "notes": "print import — needs a catalog part with published dimensions",
        }
    )
    if isinstance(device.get("bbox"), dict):
        props["agent_bbox"] = device["bbox"]
    review = str(device.get("_catalog_review") or "").strip()
    if review:
        props["print_catalog_review"] = review
    if catalog:
        props["part_number"] = catalog
    if manufacturer:
        props["manufacturer"] = manufacturer
    try:
        return session.place(
            needs_id,
            ref=tag,
            x=x,
            y=y,
            sheet_id=sheet_id,
            snap=False,
            allow_needs_info=True,
            properties=props,
        )
    except (KeyError, ValueError, LookupError):
        return None


def _wire_conflict(item: dict[str, Any], page: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "kind": "wire",
        "id": str(item.get("number") or ""),
        "sheet": str(item.get("page") or page.get("id") or ""),
        "reason": reason,
        "from": str(item.get("from") or ""),
        "to": str(item.get("to") or ""),
        "bbox": item.get("bbox") if isinstance(item.get("bbox"), dict) else None,
        "image": str(page.get("image") or ""),
    }


def _catalog_block(device: dict[str, Any], page: dict[str, Any] | None, part: Any, symbol_cap: float = 0.0) -> str:
    """Empty when this printed catalog may be assigned. A guess stays needs-info."""

    catalog = str(getattr(part, "catalog", "") or device.get("catalog") or "")
    symbol = str(device.get("symbol") or "")
    library_id = str(getattr(part, "library_id", "") or "")
    contact = str(device.get("contact") or "")
    if _read_as_contact(symbol, contact):
        return f"catalog {catalog} was read as a contact, so it takes no part."
    if not _family_allows(symbol, library_id, contact):
        return f"catalog {catalog} is a {library_id or 'library'} part and the symbol is {symbol or 'unknown'}."
    if page and page.get("vector") and not _catalog_belongs_to_device(catalog, device, page, symbol_cap):
        return f"catalog {catalog} is not printed on this device."
    if page and not page.get("vector"):
        return f"confirm catalog {catalog}. A scan does not assign a part by itself."
    return ""


# The whole symbol, not a word inside it. A longer phrase such as "solenoid coil"
# or "fused disconnect" is not one of these and gets no part.
_SYMBOL_KINDS = {
    "coil": "coil",
    "contactor": "contactor",
    "relay": "relay",
    "control relay": "relay",
    "contact": "contact",
    "breaker": "breaker",
    "circuit breaker": "breaker",
    "mcb": "mcb",
    "mpcb": "mpcb",
    "mccb": "mccb",
    "fuse": "fuse",
    "overload": "overload",
    "power supply": "psu",
    "psu": "psu",
    "power": "psu",
    "transformer": "transformer",
    "pilot": "pilot",
    "pilot light": "pilot",
    "indicator lamp": "pilot",
    "pushbutton": "pushbutton",
    "push button": "pushbutton",
    "selector": "selector",
    "selector switch": "selector",
    "estop": "estop",
    "e-stop": "estop",
    "emergency stop": "estop",
    "horn": "horn",
    "overload relay": "overload",
    "contactor coil": "coil",
    "relay coil": "relay",
    "motor protector": "mpcb",
    "drive": "drive",
    "vfd": "drive",
    "plc": "plc",
    "terminal": "terminal",
}
_OPERATOR_KINDS = frozenset({"pushbutton", "selector", "estop"})


def _read_as_contact(symbol: str, contact: str) -> bool:
    """NO or NC is a contact, except on a pushbutton, selector, or estop."""

    state = str(contact or "").casefold().strip()
    if state not in {"no", "nc"}:
        return False
    kind = _SYMBOL_KINDS.get(str(symbol or "").casefold().strip())
    return kind not in _OPERATOR_KINDS


def _symbol_reach_cap(catalog: Any, library_id: str) -> float:
    """The library symbol's own size. An agent bbox cannot outgrow it."""

    if catalog is None or not library_id:
        return 0.0
    try:
        symbol = catalog.get(library_id)
    except KeyError:
        return 0.0
    sides = [
        float(value)
        for value in (
            getattr(symbol, "schematic_width", 0) or 0,
            getattr(symbol, "schematic_height", 0) or 0,
        )
        if float(value or 0) > 0
    ]
    if not sides:
        return 0.0
    return min(sides)


def _family_allows(symbol: str, library_id: str, contact: str = "") -> bool:
    """True when this exact symbol may take this library part.

    ``contact`` NO or NC makes the row a contact, except on a pushbutton,
    selector, or estop. A disconnect, a limit switch, and an unknown symbol
    stay needs-info.
    """

    kind = _SYMBOL_KINDS.get(str(symbol or "").casefold().strip())
    lib = str(library_id or "").casefold().strip()
    if not kind or kind == "contact" or not lib:
        return False
    state = str(contact or "").casefold().strip()
    if state in {"no", "nc"} and kind not in _OPERATOR_KINDS:
        return False
    return _library_kind_ok(kind, lib, state)


def _library_kind_ok(kind: str, library_id: str, contact: str) -> bool:
    tail = library_id.rsplit(".", 1)[-1]
    group = library_id.split(".")
    family = group[-2] if len(group) >= 2 else ""
    if kind == "coil":
        return _is_contactor_main(tail, library_id) or _is_relay_body(tail, library_id)
    if kind == "contactor":
        return _is_contactor_main(tail, library_id)
    if kind == "relay":
        return _is_relay_body(tail, library_id)
    if kind == "breaker":
        return family == "breakers"
    if kind == "mcb":
        return tail.startswith("mcb") and not tail.startswith("mpcb")
    if kind == "mpcb":
        return tail.startswith("mpcb")
    if kind == "mccb":
        return tail.startswith("mccb")
    if kind == "fuse":
        return tail.startswith("fuse")
    if kind == "overload":
        return tail.startswith("overload") or tail.startswith("ol_") or "ol_contact" in tail
    if kind == "psu":
        return tail.startswith("psu")
    if kind == "transformer":
        return "transformer" in tail
    if kind == "pilot":
        return tail.startswith("pilot")
    if kind == "pushbutton":
        return _pushbutton_kind(contact, tail)
    if kind == "selector":
        return tail.startswith("selector")
    if kind == "estop":
        return tail.startswith("estop")
    if kind == "horn":
        return tail.startswith("horn")
    if kind == "drive":
        return family == "drives"
    if kind == "plc":
        return family == "plc"
    if kind == "terminal":
        return family == "terminals"
    return False


def _pushbutton_kind(contact: str, kind: str) -> bool:
    """NO and NC are different parts. An unmarked pushbutton is not guessed."""

    state = str(contact or "").casefold().strip()
    if state == "nc":
        return kind.startswith("pb_nc")
    if state == "no":
        return kind.startswith("pb_no")
    return False


def _is_contactor_main(kind: str, library_id: str) -> bool:
    """A contactor body, not an auxiliary block or an interlock."""

    if "aux" in kind or "interlock" in kind:
        return False
    return ".contactors." in f".{library_id}."


def _is_relay_body(kind: str, library_id: str) -> bool:
    if "aux" in kind or "interlock" in kind:
        return False
    return ".relays." in f".{library_id}."


def _catalog_belongs_to_device(
    catalog: str, device: dict[str, Any], page: dict[str, Any], symbol_cap: float = 0.0
) -> bool:
    """True when the catalog token is on this symbol, or close to it on this print.

    Close means inside the symbol, or nearer than every other tag and within
    the smaller of one symbol bbox and half the distance to the nearest
    neighbour. A token in the print's own title block or drawing border does
    not count.
    """

    want = _compact(catalog)
    if len(want) < 4:
        return False
    points = [item for item in page.get("device_points") or [] if isinstance(item, dict)]
    mine = str(device.get("tag") or "")
    own = device.get("bbox") if isinstance(device.get("bbox"), dict) else None
    excluded = _excluded_boxes(page)
    for token in page.get("token_rows") or []:
        if _compact(str(token.get("text") or "")) != want:
            continue
        try:
            tx = float(token["x"])
            ty = float(token["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if _point_in_any_box(tx, ty, excluded):
            continue
        inside = _point_in_box(tx, ty, own) and not any(
            str(other.get("tag") or "") != mine and _point_in_box(tx, ty, other.get("bbox"))
            for other in points
        )
        nearest, tie = _nearest_tag(tx, ty, points)
        close = _within_catalog_reach(tx, ty, device, points, symbol_cap)
        if inside and close:
            return True
        if nearest == mine and not tie and close:
            return True
    return False


def _within_catalog_reach(
    x: float, y: float, device: dict[str, Any], points: list[dict[str, Any]], symbol_cap: float = 0.0
) -> bool:
    """Distance from the symbol box edge, capped so an agent box cannot grow the reach."""

    reach = _catalog_reach(device, points, symbol_cap)
    if reach <= 0:
        return False
    box = _clamped_symbol_box(device, symbol_cap)
    if box is None:
        return False
    # Past the symbol edge, only about half the capped size still counts.
    return _distance_outside_box(x, y, box) <= reach * 0.5


def _clamped_symbol_box(device: dict[str, Any], symbol_cap: float) -> dict[str, float] | None:
    try:
        x = float(device["x"])
        y = float(device["y"])
    except (KeyError, TypeError, ValueError):
        return None
    agent = device.get("bbox") if isinstance(device.get("bbox"), dict) else None
    try:
        width = float(agent["w"]) if agent else 0.0
        height = float(agent["h"]) if agent else 0.0
    except (KeyError, TypeError, ValueError):
        width = height = 0.0
    if symbol_cap > 0:
        width = symbol_cap if width <= 0 else min(width, symbol_cap)
        height = symbol_cap if height <= 0 else min(height, symbol_cap)
    if width <= 0 or height <= 0:
        return None
    return {"x": x - width / 2.0, "y": y - height / 2.0, "w": width, "h": height}


def _distance_outside_box(x: float, y: float, box: dict[str, float]) -> float:
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["w"], y0 + box["h"]
    dx = 0.0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1))
    dy = 0.0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
    return math.hypot(dx, dy)


def _catalog_reach(device: dict[str, Any], points: list[dict[str, Any]], symbol_cap: float = 0.0) -> float:
    """The tighter of the symbol, the agent box, and half the gap to the next device.

    Distance is measured from the device, so a large agent box cannot claim a
    number that sits well outside the symbol.
    """

    box = device.get("bbox") if isinstance(device.get("bbox"), dict) else None
    size = 0.0
    if box is not None:
        try:
            size = max(float(box.get("w") or 0), float(box.get("h") or 0))
        except (TypeError, ValueError):
            size = 0.0
    if symbol_cap > 0:
        size = symbol_cap if size <= 0 else min(size, symbol_cap)
    if size <= 0:
        return 0.0
    try:
        x = float(device["x"])
        y = float(device["y"])
    except (KeyError, TypeError, ValueError):
        return size
    mine = str(device.get("tag") or "")
    nearest: float | None = None
    for other in points:
        if str(other.get("tag") or "") == mine:
            continue
        try:
            dist = math.hypot(float(other["x"]) - x, float(other["y"]) - y)
        except (KeyError, TypeError, ValueError):
            continue
        if dist > 0 and (nearest is None or dist < nearest):
            nearest = dist
    if nearest is None:
        return size
    return min(size, nearest / 2.0)


_TITLE_LABELS = frozenset(
    {
        "SHEET",
        "SHT",
        "REV",
        "REVISION",
        "DWG",
        "DRAWING",
        "TITLE",
        "COMPANY",
        "LOCATION",
        "STANDARD",
        "APPROVED",
        "SCALE",
        "DESCRIPTION",
        "LOGO",
    }
)
# One title-block row. Farther than this is the drawing, not the block.
_TITLE_ROW_HEIGHTS = 2.0
# Nearest horizontals and verticals considered for one title rectangle.
_TITLE_LINE_CAP = 24
# Title boxes for one prep page, shared by every fresh record of that file.
# Bounded so a long import does not keep every page's geometry.
_TITLE_FILE_CACHE: dict[tuple[str, int], list[dict[str, float]]] = {}


def _excluded_boxes(page: dict[str, Any]) -> list[dict[str, float]]:
    """Title block and drawing border taken from this page, not from a template."""

    boxes: list[dict[str, float]] = []
    for key in ("title_block", "border"):
        box = _as_box(page.get(key))
        if box is not None:
            boxes.append(box)
    tokens = [item for item in page.get("token_rows") or [] if isinstance(item, dict)]
    boxes.extend(_title_label_boxes(tokens, page))
    try:
        width = float(page.get("width_mm") or 0)
        height = float(page.get("height_mm") or 0)
    except (TypeError, ValueError):
        width = height = 0.0
    boxes.extend(_border_boxes(page.get("lines") or [], width, height))
    return boxes


def _as_box(raw: Any) -> dict[str, float] | None:
    if isinstance(raw, dict) and isinstance(raw.get("bbox"), dict):
        raw = raw["bbox"]
    if not isinstance(raw, dict):
        return None
    try:
        box = {"x": float(raw["x"]), "y": float(raw["y"]), "w": float(raw["w"]), "h": float(raw["h"])}
    except (KeyError, TypeError, ValueError):
        return None
    if box["w"] <= 0 or box["h"] <= 0:
        return None
    return box


def _is_title_label(text: str) -> bool:
    """SHEET, REV:, DWG NO., and SHT. Punctuation is not part of the word."""

    words = re.sub(r"[^A-Za-z0-9]+", " ", str(text or "")).upper().split()
    return any(word in _TITLE_LABELS for word in words)


def _title_file_key(page: dict[str, Any]) -> tuple[str, int] | None:
    """Prep path plus mtime. A new record of the same file reuses the boxes."""

    path = page.get("_source_path")
    if not isinstance(path, str) or not path:
        return None
    try:
        return path, int(page["_source_mtime"])
    except (KeyError, TypeError, ValueError):
        return None


def _title_label_boxes(tokens: list[dict[str, Any]], page: dict[str, Any] | None = None) -> list[dict[str, float]]:
    """The title block drawn around the labels, or one short row of text when it is not drawn."""

    page = page or {}
    cached = page.get("_title_label_cache")
    if isinstance(cached, list):
        return cached
    key = _title_file_key(page)
    if key is not None and key in _TITLE_FILE_CACHE:
        stored = _TITLE_FILE_CACHE.pop(key)
        _TITLE_FILE_CACHE[key] = stored
        page["_title_label_cache"] = stored
        return stored
    boxes = _compute_title_label_boxes(tokens, page)
    page["_title_label_cache"] = boxes
    if key is not None:
        _TITLE_FILE_CACHE[key] = boxes
        from panelcad.memory import import_cache_limit

        while len(_TITLE_FILE_CACHE) > import_cache_limit():
            _TITLE_FILE_CACHE.pop(next(iter(_TITLE_FILE_CACHE)))
    return boxes


def _compute_title_label_boxes(tokens: list[dict[str, Any]], page: dict[str, Any]) -> list[dict[str, float]]:
    """Search this page once. Callers cache the result."""

    labels = [token for token in tokens if _is_title_label(str(token.get("text") or "")) and _as_box(token) is not None]
    if not labels:
        return []
    try:
        width = float(page.get("width_mm") or 0)
        height = float(page.get("height_mm") or 0)
    except (TypeError, ValueError):
        width = height = 0.0
    lines = [item for item in page.get("lines") or [] if isinstance(item, dict)]
    text_h = max(_text_height(token) for token in labels)
    horiz, vert = _merged_axis_lines(lines)
    boxes: list[dict[str, float]] = []
    claimed: set[int] = set()
    for rect in _rectangles_around_labels(horiz, vert, labels, width, height):
        boxes.append(rect)
        for label in labels:
            center = _token_center(label)
            if center is not None and _point_in_box(center[0], center[1], rect):
                claimed.add(id(label))
    loose = [label for label in labels if id(label) not in claimed]
    for group in _cluster_label_blocks(loose):
        bounds = _undrawn_title_bounds(group, tokens, lines, page)
        if bounds is None:
            continue
        boxes.append(_extend_title_box(bounds, text_h, width, height))
    return boxes


def _text_height(token: dict[str, Any]) -> float:
    box = _as_box(token)
    if box is None:
        return 1.0
    return max(box["h"], 1.0)


def _cluster_seeds(seeds: list[dict[str, Any]], limit: float) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for token in seeds:
        box = _as_box(token)
        if box is None:
            continue
        joined = [
            group
            for group in groups
            if any(_box_gap(box, other) <= limit for other in (_as_box(item) for item in group) if other is not None)
        ]
        if not joined:
            groups.append([token])
            continue
        host = joined[0]
        host.append(token)
        for extra in joined[1:]:
            host.extend(extra)
            groups.remove(extra)
    return groups


def _cluster_label_blocks(labels: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Labels in one column, or on one row, are one block even when no box is drawn."""

    groups: list[list[dict[str, Any]]] = []
    for token in labels:
        box = _as_box(token)
        if box is None:
            continue
        joined = [group for group in groups if any(_labels_share_block(box, _as_box(item)) for item in group)]
        if not joined:
            groups.append([token])
            continue
        host = joined[0]
        host.append(token)
        for extra in joined[1:]:
            host.extend(extra)
            groups.remove(extra)
    return groups


def _labels_share_block(left: dict[str, float] | None, right: dict[str, float] | None) -> bool:
    if left is None or right is None:
        return False
    row_h = max(left["h"], right["h"], 1.0)
    gap = _box_gap(left, right)
    x_overlap = left["x"] + left["w"] >= right["x"] - 1 and right["x"] + right["w"] >= left["x"] - 1
    y_overlap = left["y"] + left["h"] >= right["y"] - 1 and right["y"] + right["h"] >= left["y"] - 1
    if x_overlap and gap <= row_h * 6:
        return True
    if y_overlap and gap <= row_h * 4:
        return True
    same_row = abs((left["y"] + left["h"] / 2.0) - (right["y"] + right["h"] / 2.0)) <= row_h * 2
    return same_row and gap <= row_h * 4


def _undrawn_title_bounds(
    group: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    page: dict[str, Any],
) -> dict[str, float] | None:
    """Rows of an undrawn block. A column also keeps one label-pitch past its last label."""

    bounds = _token_bounds(group)
    if bounds is None:
        return None
    column = bounds["h"] >= bounds["w"]
    pitch = _label_pitch(group, vertical=column) if column else 0.0
    text_h = max(_text_height(token) for token in group)

    def claimed_box() -> dict[str, float]:
        box = dict(_token_bounds(cluster) or bounds)
        box["h"] += pitch
        return box

    cluster = list(group)
    pending = [token for token in tokens if token not in cluster and _as_box(token) is not None]
    changed = True
    while changed and pending:
        changed = False
        still: list[dict[str, Any]] = []
        claimed = claimed_box()
        for token in pending:
            if not _undrawn_row_fits(token, claimed, column, text_h, lines, page):
                still.append(token)
                continue
            cluster.append(token)
            changed = True
        pending = still
    return claimed_box()


def _undrawn_row_fits(
    token: dict[str, Any],
    claimed: dict[str, float],
    column: bool,
    text_h: float,
    lines: list[dict[str, Any]],
    page: dict[str, Any],
) -> bool:
    box = _as_box(token)
    center = _token_center(token)
    if box is None or center is None:
        return False
    if not _is_title_label(str(token.get("text") or "")) and _belongs_to_a_device(token, page):
        return False
    row_h = max(_text_height(token), text_h, 1.0)
    x_overlap = box["x"] + box["w"] >= claimed["x"] - 1 and box["x"] <= claimed["x"] + claimed["w"] + 1
    y_overlap = box["y"] + box["h"] >= claimed["y"] - 1 and box["y"] <= claimed["y"] + claimed["h"] + 1
    if _point_in_box(center[0], center[1], claimed):
        near = True
    elif column and x_overlap:
        near = _box_gap(box, claimed) <= row_h * 2.6
    elif not column and (y_overlap or x_overlap):
        near = _box_gap(box, claimed) <= row_h * 2.6
    else:
        near = False
    if not near:
        return False
    anchor = (claimed["x"] + claimed["w"] / 2.0, claimed["y"] + claimed["h"] / 2.0)
    return not _segment_crosses_long_line(center, anchor, lines)


def _label_pitch(group: list[dict[str, Any]], vertical: bool) -> float:
    centers = sorted(
        center[1 if vertical else 0]
        for center in (_token_center(token) for token in group)
        if center is not None
    )
    gaps = [centers[index + 1] - centers[index] for index in range(len(centers) - 1)]
    gaps = [gap for gap in gaps if gap > 0.5]
    if not gaps:
        return 0.0
    gaps.sort()
    return gaps[len(gaps) // 2]


def _chain_tokens(
    seeds: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    limit: float | None = None,
    lines: list[dict[str, Any]] | None = None,
    page: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Tokens within one title row of the labels. Stops at a rung and at a device."""

    cluster = [token for token in seeds if _as_box(token) is not None]
    if not cluster:
        return []
    if limit is None:
        limit = max(_text_height(token) for token in cluster) * _TITLE_ROW_HEIGHTS
    pending = [token for token in tokens if token not in cluster and _as_box(token) is not None]
    changed = True
    while changed and pending:
        changed = False
        still: list[dict[str, Any]] = []
        for token in pending:
            if _title_token_blocked(token, cluster, lines or [], page or {}, limit):
                still.append(token)
                continue
            cluster.append(token)
            changed = True
        pending = still
    return cluster


def _title_token_blocked(
    token: dict[str, Any],
    cluster: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    page: dict[str, Any],
    limit: float,
) -> bool:
    if _is_title_label(str(token.get("text") or "")):
        blocked_kind = False
    else:
        blocked_kind = _belongs_to_a_device(token, page)
    box = _as_box(token)
    if box is None:
        return True
    near = [item for item in cluster if (_as_box(item) is not None and _box_gap(box, _as_box(item) or box) <= limit)]
    if not near:
        return True
    if blocked_kind:
        return True
    center = _token_center(token)
    for item in near:
        other = _token_center(item)
        if center is None or other is None:
            continue
        if _segment_crosses_long_line(center, other, lines):
            return True
    return False


def _belongs_to_a_device(token: dict[str, Any], page: dict[str, Any]) -> bool:
    """An agent device tag, or a catalog printed beside that device."""

    text = _compact(str(token.get("text") or ""))
    center = _token_center(token)
    if not text or center is None:
        return False
    for device in page.get("device_points") or []:
        if not isinstance(device, dict):
            continue
        if _compact(str(device.get("tag") or "")) == text:
            return True
        box = device.get("bbox") if isinstance(device.get("bbox"), dict) else None
        token_box = _as_box(token)
        if isinstance(box, dict) and token_box is not None and _box_gap(token_box, _as_box(box) or box) <= 12.0 and len(text) >= 4:
            return True
        if _point_in_box(center[0], center[1], box):
            return True
    return False


def _token_center(token: dict[str, Any]) -> tuple[float, float] | None:
    box = _as_box(token)
    if box is None:
        return None
    return (box["x"] + box["w"] / 2.0, box["y"] + box["h"] / 2.0)


def _segment_crosses_long_line(
    start: tuple[float, float], end: tuple[float, float], lines: list[dict[str, Any]]
) -> bool:
    ax, ay = start
    bx, by = end
    for line in lines:
        try:
            x0, y0 = float(line["x0"]), float(line["y0"])
            x1, y1 = float(line["x1"]), float(line["y1"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.hypot(x1 - x0, y1 - y0) < 25.0:
            continue
        if _segments_cross(ax, ay, bx, by, x0, y0, x1, y1):
            return True
    return False


def _segments_cross(ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float) -> bool:
    def side(px: float, py: float, qx: float, qy: float, rx: float, ry: float) -> float:
        return (qy - py) * (rx - px) - (qx - px) * (ry - py)

    return side(ax, ay, bx, by, cx, cy) * side(ax, ay, bx, by, dx, dy) < 0 and side(cx, cy, dx, dy, ax, ay) * side(
        cx, cy, dx, dy, bx, by
    ) < 0


def _merged_axis_lines(lines: list[dict[str, Any]]) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Horizontal (y, x0, x1) and vertical (x, y0, y1) spans, with collinear pieces joined."""

    horiz: list[tuple[float, float, float]] = []
    vert: list[tuple[float, float, float]] = []
    for line in lines:
        try:
            x0, y0 = float(line["x0"]), float(line["y0"])
            x1, y1 = float(line["x1"]), float(line["y1"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(y1 - y0) <= 1.2 and abs(x1 - x0) >= 4.0:
            horiz.append(((y0 + y1) / 2.0, min(x0, x1), max(x0, x1)))
        elif abs(x1 - x0) <= 1.2 and abs(y1 - y0) >= 4.0:
            vert.append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
    return _merge_spans(horiz), _merge_spans(vert)


def _merge_spans(spans: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    groups: list[list[Any]] = []
    for pos, start, stop in sorted(spans, key=lambda item: (item[0], item[1])):
        host = None
        for group in groups:
            if abs(float(group[0]) - pos) <= 1.5:
                host = group
                break
        if host is None:
            groups.append([pos, [(start, stop)]])
            continue
        host[1].append((start, stop))
    merged: list[tuple[float, float, float]] = []
    for pos, intervals in groups:
        intervals.sort()
        cur_a, cur_b = intervals[0]
        for start, stop in intervals[1:]:
            if start <= cur_b + 2.5:
                cur_b = max(cur_b, stop)
            else:
                merged.append((float(pos), cur_a, cur_b))
                cur_a, cur_b = start, stop
        merged.append((float(pos), cur_a, cur_b))
    return merged


def _rectangles_around_labels(
    horiz: list[tuple[float, float, float]],
    vert: list[tuple[float, float, float]],
    labels: list[dict[str, Any]],
    width: float,
    height: float,
) -> list[dict[str, float]]:
    """Closed boxes around the labels. Cell grids count, and a side may be the page edge."""

    points = [center for center in (_token_center(label) for label in labels) if center is not None]
    if not points or width <= 0 or height <= 0:
        return []
    label_box = _token_bounds(labels)
    if label_box is None:
        return []
    x0, x1 = label_box["x"], label_box["x"] + label_box["w"]
    y0, y1 = label_box["y"], label_box["y"] + label_box["h"]
    near_h = [item for item in horiz if item[2] >= x0 - 2 and item[1] <= x1 + 2]
    near_v = [item for item in vert if item[2] >= y0 - 2 and item[1] <= y1 + 2]
    # A block may close on the page edge when the labels already sit against it.
    if label_box["y"] + label_box["h"] >= height - 40:
        near_h.append((height, 0.0, width))
    if label_box["y"] <= 40:
        near_h.append((0.0, 0.0, width))
    if label_box["x"] <= 40:
        near_v.append((0.0, 0.0, height))
    if label_box["x"] + label_box["w"] >= width - 40:
        near_v.append((width, 0.0, height))
    found: list[dict[str, float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for point in points:
        # Rank by this label, not the whole strip. Inner cell walls would otherwise
        # fill the cap and drop the outer edges. The band and the cell expansion
        # still see every line.
        edges_h = [
            item
            for item in near_h
            if item[0] in (0.0, height) and item[1] == 0.0 and item[2] == width
        ]
        edges_v = [
            item
            for item in near_v
            if item[0] in (0.0, width) and item[1] == 0.0 and item[2] == height
        ]
        ph = sorted(
            (item for item in near_h if item not in edges_h),
            key=lambda item: abs(item[0] - point[1]),
        )[:_TITLE_LINE_CAP] + edges_h
        pv = sorted(
            (item for item in near_v if item not in edges_v),
            key=lambda item: abs(item[0] - point[0]),
        )[:_TITLE_LINE_CAP] + edges_v
        band = _vertical_band_box(near_v, [point], height, width)
        rect = _smallest_meeting_rectangle(ph, pv, [point], width, height)
        chosen = rect or band
        if chosen is None:
            continue
        if rect is not None:
            chosen = _expand_sibling_cells(rect, near_h, near_v)
        key = (round(chosen["x"], 2), round(chosen["y"], 2), round(chosen["w"], 2), round(chosen["h"], 2))
        if key in seen:
            continue
        seen.add(key)
        found.append(chosen)
    return found


def _vertical_band_box(
    vert: list[tuple[float, float, float]],
    points: list[tuple[float, float]],
    height: float,
    width: float = 0.0,
) -> dict[str, float] | None:
    """Outer verticals around the labels. A shorter divider only splits cells."""

    if len(vert) < 2 or height <= 0:
        return None
    ys = [point[1] for point in points]
    xs = [point[0] for point in points]
    y0, y1 = min(ys), max(ys)
    spanning = [
        item
        for item in vert
        if item[1] <= y0 + 2 and item[2] >= y1 - 2 and item[2] - item[1] < height * 0.45
    ]
    lefts = [item for item in spanning if item[0] <= min(xs) + 1]
    rights = [item for item in spanning if item[0] >= max(xs) - 1]
    if not lefts or not rights:
        return None
    left = _outer_vertical(lefts, nearest=max)
    right = _outer_vertical(rights, nearest=min)
    if right[0] - left[0] < 4:
        return None
    top = min(left[1], right[1])
    bottom = max(left[2], right[2])
    if bottom - top < 4 or bottom - top > height * 0.45:
        return None
    if width > 0 and height - bottom <= 40:
        bottom = height
    if not (left[0] - 1 <= min(xs) and max(xs) <= right[0] + 1 and top - 1 <= y0 and y1 <= bottom + 1):
        return None
    return {"x": left[0], "y": top, "w": right[0] - left[0], "h": bottom - top}


def _outer_vertical(
    sides: list[tuple[float, float, float]], nearest: Any
) -> tuple[float, float, float]:
    """The nearest side that starts as high as the tallest one. A lower divider is a cell wall."""

    highest = min(item[1] for item in sides)
    tall = [item for item in sides if item[1] <= highest + 2]
    return nearest(tall, key=lambda item: item[0])


def _drawn_side_meets(start: float, stop: float, top: float, bottom: float) -> bool:
    """A drawn side ends on the top and the bottom. A frame line that only crosses them does not."""

    return (abs(start - top) <= 3.0 or abs(stop - top) <= 3.0) and (abs(start - bottom) <= 3.0 or abs(stop - bottom) <= 3.0)


def _side_meets(pos: float, start: float, stop: float, top: float, bottom: float, width: float, height: float) -> bool:
    if abs(pos) <= 0.5 or abs(pos - width) <= 0.5:
        return True
    return _drawn_side_meets(start, stop, top, bottom) and start <= top + 3 and stop >= bottom - 3


def _smallest_meeting_rectangle(
    horiz: list[tuple[float, float, float]],
    vert: list[tuple[float, float, float]],
    points: list[tuple[float, float]],
    width: float,
    height: float,
) -> dict[str, float] | None:
    """The smallest rectangle whose sides meet, not a rung the frame merely crosses."""

    best: dict[str, float] | None = None
    best_area = 0.0
    page_area = width * height
    px = points[0][0] if points else None
    py = points[0][1] if points else None
    for top, tx0, tx1 in horiz:
        if py is not None and top > py + 1:
            continue
        for bottom, bx0, bx1 in horiz:
            if bottom <= top + 4:
                continue
            if py is not None and bottom < py - 1:
                continue
            for left, ly0, ly1 in vert:
                if px is not None and left > px + 1:
                    continue
                for right, ry0, ry1 in vert:
                    if right <= left + 4:
                        continue
                    if px is not None and right < px - 1:
                        continue
                    if tx0 > left + 2 or tx1 < right - 2 or bx0 > left + 2 or bx1 < right - 2:
                        continue
                    if not _side_meets(left, ly0, ly1, top, bottom, width, height) or not _side_meets(
                        right, ry0, ry1, top, bottom, width, height
                    ):
                        continue
                    if not _drawn_side_meets(ly0, ly1, top, bottom) and not _drawn_side_meets(ry0, ry1, top, bottom):
                        continue
                    if not all(left - 1 <= x <= right + 1 and top - 1 <= y <= bottom + 1 for x, y in points):
                        continue
                    area = (right - left) * (bottom - top)
                    if area <= 0 or area > page_area * 0.35:
                        continue
                    if best is None or area < best_area:
                        best = {"x": left, "y": top, "w": right - left, "h": bottom - top}
                        best_area = area
    return best


def _expand_sibling_cells(
    box: dict[str, float],
    horiz: list[tuple[float, float, float]],
    vert: list[tuple[float, float, float]],
) -> dict[str, float]:
    """Include the next title cell when its divider meets the same top and bottom."""

    grown = dict(box)
    for _ in range(6):
        right = grown["x"] + grown["w"]
        bottom = grown["y"] + grown["h"]
        moved = False
        for x, y0, y1 in vert:
            if grown["x"] - 80 <= x < grown["x"] - 2 and y0 <= grown["y"] + 3 and y1 >= bottom - 3:
                if _span_covers(horiz, grown["y"], x, right) and _span_covers(horiz, bottom, x, right):
                    grown["w"] += grown["x"] - x
                    grown["x"] = x
                    moved = True
            if right + 2 < x <= right + 80 and y0 <= grown["y"] + 3 and y1 >= bottom - 3:
                if _span_covers(horiz, grown["y"], grown["x"], x) and _span_covers(horiz, bottom, grown["x"], x):
                    grown["w"] = x - grown["x"]
                    moved = True
        if not moved:
            break
    return grown


def _span_covers(spans: list[tuple[float, float, float]], pos: float, start: float, stop: float) -> bool:
    for at, a0, a1 in spans:
        if abs(at - pos) <= 2.0 and a0 <= start + 2 and a1 >= stop - 2:
            return True
    return False


def _union_box(left: dict[str, float], right: dict[str, float]) -> dict[str, float]:
    x0 = min(left["x"], right["x"])
    y0 = min(left["y"], right["y"])
    x1 = max(left["x"] + left["w"], right["x"] + right["w"])
    y1 = max(left["y"] + left["h"], right["y"] + right["h"])
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _extend_title_box(bounds: dict[str, float], text_h: float, width: float, height: float) -> dict[str, float]:
    """Snap a label row out to a page edge it already touches. Do not climb into the drawing."""

    box = dict(bounds)
    limit = max(text_h, 1.0) * _TITLE_ROW_HEIGHTS
    if width > 0:
        if box["x"] <= limit:
            box["w"] += box["x"]
            box["x"] = 0.0
        right_gap = width - (box["x"] + box["w"])
        if 0 <= right_gap <= limit:
            box["w"] += right_gap
    if height > 0:
        if box["y"] <= limit:
            box["h"] += box["y"]
            box["y"] = 0.0
        bottom_gap = height - (box["y"] + box["h"])
        if 0 <= bottom_gap <= limit:
            box["h"] += bottom_gap
    return box


def _title_block_identifiers(page: dict[str, Any]) -> set[str]:
    """Drawing and sheet numbers printed in the title block, even with no project field."""

    from panelcad.importer.read import DWG_RE

    tokens = [item for item in page.get("token_rows") or [] if isinstance(item, dict)]
    boxes = _title_label_boxes(tokens, page)
    found: set[str] = set()
    for token in tokens:
        try:
            x = float(token["x"]) + float(token.get("w") or 0) / 2.0
            y = float(token["y"]) + float(token.get("h") or 0) / 2.0
        except (KeyError, TypeError, ValueError):
            continue
        if not _point_in_any_box(x, y, boxes):
            continue
        text = str(token.get("text") or "").strip()
        upper = text.upper()
        if DWG_RE.fullmatch(re.sub(r"[^A-Z0-9-]+", "", upper).strip("-")) or DWG_RE.search(upper):
            compact = re.sub(r"[^A-Z0-9-]+", "", upper).strip("-")
            if compact:
                found.add(compact)
        words = re.sub(r"[^A-Za-z0-9]+", " ", upper).split()
        if any(word in {"SHEET", "SHT"} for word in words):
            for word in words:
                if word.isdigit():
                    found.add(word)
    return found


def _token_size(token: dict[str, Any]) -> float:
    box = _as_box(token)
    if box is None:
        return 0.0
    return max(box["w"], box["h"], 1.0)


def _token_bounds(tokens: list[dict[str, Any]]) -> dict[str, float] | None:
    boxes = [box for box in (_as_box(token) for token in tokens) if box is not None]
    if not boxes:
        return None
    x0 = min(box["x"] for box in boxes)
    y0 = min(box["y"] for box in boxes)
    x1 = max(box["x"] + box["w"] for box in boxes)
    y1 = max(box["y"] + box["h"] for box in boxes)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _box_gap(left: dict[str, float], right: dict[str, float]) -> float:
    dx = 0.0
    if left["x"] + left["w"] < right["x"]:
        dx = right["x"] - (left["x"] + left["w"])
    elif right["x"] + right["w"] < left["x"]:
        dx = left["x"] - (right["x"] + right["w"])
    dy = 0.0
    if left["y"] + left["h"] < right["y"]:
        dy = right["y"] - (left["y"] + left["h"])
    elif right["y"] + right["h"] < left["y"]:
        dy = left["y"] - (right["y"] + right["h"])
    return math.hypot(dx, dy)


def _border_boxes(lines: list[Any], width: float, height: float) -> list[dict[str, float]]:
    """Bands outside the page's own frame lines. No frame means no border."""

    if width <= 0 or height <= 0:
        return []
    horiz: list[float] = []
    vert: list[float] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        try:
            x0 = float(line["x0"])
            y0 = float(line["y0"])
            x1 = float(line["x1"])
            y1 = float(line["y1"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(y1 - y0) <= 1.0 and abs(x1 - x0) >= width * 0.5:
            horiz.append((y0 + y1) / 2.0)
        if abs(x1 - x0) <= 1.0 and abs(y1 - y0) >= height * 0.5:
            vert.append((x0 + x1) / 2.0)
    if len(horiz) < 2 or len(vert) < 2:
        return []
    top, bottom = min(horiz), max(horiz)
    left, right = min(vert), max(vert)
    if bottom - top < height * 0.4 or right - left < width * 0.4:
        return []
    boxes: list[dict[str, float]] = []
    if top > 0:
        boxes.append({"x": 0.0, "y": 0.0, "w": width, "h": top})
    if bottom < height:
        boxes.append({"x": 0.0, "y": bottom, "w": width, "h": height - bottom})
    if left > 0:
        boxes.append({"x": 0.0, "y": 0.0, "w": left, "h": height})
    if right < width:
        boxes.append({"x": right, "y": 0.0, "w": width - right, "h": height})
    return boxes


def _point_in_any_box(x: float, y: float, boxes: list[dict[str, float]]) -> bool:
    return any(_point_in_box(x, y, box) for box in boxes)


def _point_in_box(x: float, y: float, box: Any) -> bool:
    if not isinstance(box, dict):
        return False
    try:
        x0 = float(box["x"])
        y0 = float(box["y"])
        width = float(box["w"])
        height = float(box["h"])
    except (KeyError, TypeError, ValueError):
        return False
    return x0 <= x <= x0 + width and y0 <= y <= y0 + height


def _nearest_tag(x: float, y: float, points: list[dict[str, Any]]) -> tuple[str, bool]:
    best = ""
    best_d = -1.0
    tie = False
    for item in points:
        try:
            dist = (float(item["x"]) - x) ** 2 + (float(item["y"]) - y) ** 2
        except (KeyError, TypeError, ValueError):
            continue
        tag = str(item.get("tag") or "")
        if best_d < 0 or dist < best_d - 1.0:
            best = tag
            best_d = dist
            tie = False
        elif abs(dist - best_d) <= 1.0 and tag != best:
            tie = True
    return best, tie


def _published_part(session: Any, catalog: str, manufacturer: str) -> Any | None:
    try:
        part = session.parts.get(catalog, manufacturer or None)
    except KeyError:
        return None
    if not part.has_footprint():
        return None
    if not str(part.library_id or "").strip():
        return None
    return part


def _catalog_miss_reason(session: Any, catalog: str, manufacturer: str) -> str:
    try:
        part = session.parts.get(catalog, manufacturer or None)
    except KeyError as exc:
        return str(exc).splitlines()[0]
    if not part.has_footprint():
        return "catalog part has no published dimensions"
    return "catalog part has no library symbol"


def _reading_properties(device: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {
        "print_tag": str(device.get("tag") or ""),
        "print_symbol": str(device.get("symbol") or ""),
        "print_confidence": round(_confidence(device), 3),
    }
    contact = str(device.get("contact") or "").upper()
    if device.get("_contact_review"):
        props["contact_needs_info"] = "yes"
        props["contact_review"] = str(device["_contact_review"])
    elif contact in {"NO", "NC"}:
        props["contact"] = contact
    rung = device.get("rung")
    if isinstance(rung, int) and rung > 0:
        props["rung"] = str(rung)
    return props


def _attach_pins(session: Any, decisions: dict[str, Any], placed: dict[tuple[str, str], Any], sheet_of: dict[str, str], remember: Any) -> None:
    pins: dict[tuple[str, str], list[str]] = {}

    def add(page_id: str, device: str, terminal: str) -> None:
        key = (page_id, device.upper())
        if key not in placed or not terminal:
            return
        names = pins.setdefault(key, [])
        if terminal not in names:
            names.append(terminal)

    for device in decisions["devices"]:
        for terminal in device.get("terminals") or []:
            add(str(device["_page"]), str(device.get("tag") or ""), str(terminal).strip())
    for terminal in decisions["terminals"]:
        add(str(terminal["page"]), str(terminal["device"]), str(terminal["terminal"]))
    for wire in decisions["wires"]:
        for endpoint in (wire["from"], wire["to"]):
            device, pin = _split_endpoint(endpoint)
            add(str(wire["page"]), device, pin)
    for key, names in pins.items():
        comp = placed[key]
        device = session.catalog.get(comp.library_id)
        for name in names:
            try:
                device.pin(name)
            except KeyError:
                remember(comp, name)


def _place_wires(session: Any, decisions: dict[str, Any], placed: dict[tuple[str, str], Any], sheet_of: dict[str, str]) -> None:
    kept: list[dict[str, Any]] = []
    for wire in decisions["wires"]:
        page_id = str(wire["page"])
        sheet_id = sheet_of[page_id]
        frm = _endpoint_on_sheet(wire["from"], page_id, placed)
        to = _endpoint_on_sheet(wire["to"], page_id, placed)
        if not frm or not to:
            _reject_wire(decisions, wire, "endpoint is not an accepted device")
            continue
        try:
            created = session.wire(
                frm,
                to,
                sheet_id=sheet_id,
                number=wire["number"] or None,
            )
        except (KeyError, ValueError) as exc:
            _reject_wire(decisions, wire, str(exc).splitlines()[0])
            continue
        if wire.get("needs_review"):
            created.inferred = True
            for note in decisions.get("needs_review") or []:
                if note.get("review_id") == wire.get("review_id"):
                    note["sheet"] = sheet_id
        wire["id"] = getattr(created, "id", "")
        wire["sheet_id"] = sheet_id
        for row in decisions["accepted"]:
            if row.get("kind") == "wire" and row.get("page") == page_id and row.get("from") == wire["from"] and row.get("to") == wire["to"]:
                row["sheet"] = sheet_id
                row["id"] = wire["number"] or wire["id"]
        kept.append(wire)
    decisions["wires"] = kept


def _write_title(session: Any, decisions: dict[str, Any], pages: dict[str, dict[str, Any]]) -> None:
    block = decisions["title_block"]
    unread = {str(item).strip() for item in block.get("unreadable_fields") or [] if str(item).strip()}
    any_text = any(page["has_text"] for page in pages.values())
    fields: dict[str, str] = {"drawing_style": "freeform"}
    for field in _TITLE_FIELDS:
        if field in {"project", "title", "sheet_number", "sheet_count"}:
            continue
        if field in unread:
            continue
        value = str(block.get(field) or "").strip()
        if not value:
            continue
        if any_text and not any(_phrase(value, page) for page in pages.values()):
            continue
        fields[field] = value
    session.set_title_block(**fields)
    title = str(block.get("title") or "").strip()
    if title and (not any_text or any(_phrase(title, page) for page in pages.values())) and "title" not in unread:
        for sheet in session.project.sheets:
            sheet.title = title


def _counts(decisions: dict[str, Any]) -> dict[str, int]:
    connected = sum(1 for wire in decisions.get("wires") or [] if not wire.get("needs_review"))
    return {
        "connected": connected,
        "needs_review": len(decisions.get("needs_review") or []),
        "rejected": len(decisions.get("rejected") or []),
    }


def confirm_review(project: Path | str, review_id: str) -> dict[str, Any]:
    """One-click confirm. Clears that review flag and the dashed wire mark."""

    from panelcad.api.session import Session

    session = Session.open(project)
    payload = dict(session.project.properties.get("print_import") or {})
    items = list(payload.get("items") or [])
    found = None
    for item in items:
        if str(item.get("review_id") or "") == review_id:
            item["confirmed"] = True
            found = item
    if found is None:
        raise ValueError(f"no review item {review_id}")
    payload["items"] = items
    session.project.properties["print_import"] = payload
    _clear_inferred(session, found)
    session.save(project)
    return {"ok": True, "review_id": review_id, "project": str(session.path or project)}


def _clear_inferred(session: Any, item: dict[str, Any]) -> None:
    if str(item.get("kind") or "") != "wire":
        return
    number = str(item.get("text") or "")
    frm = str(item.get("wire_from") or "")
    to = str(item.get("wire_to") or "")
    sheet_id = str(item.get("sheet") or "")
    try:
        sheet = session.project.sheet(sheet_id)
    except KeyError:
        sheet = None
        for page in session.project.sheets:
            if page.id == sheet_id or page.id.replace("-", "_") == sheet_id:
                sheet = page
                break
        if sheet is None:
            return
    for wire in sheet.wires:
        if number and wire.number != number:
            continue
        if frm and to and {wire.frm, wire.to} != {frm, to}:
            continue
        wire.inferred = False
        return


def _problem_items(decisions: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for device in decisions["devices"]:
        if device.get("_as") == "needs-info":
            catalog = str(device.get("catalog") or "").strip()
            reason = str(device.get("_reason") or "").lower()
            if catalog and ("unknown catalog" in reason or "more than one manufacturer" in reason):
                items.append(
                    {
                        "kind": "device",
                        "reason": "unknown",
                        "text": catalog,
                        "tag": str(device.get("tag") or ""),
                        "sheet": str(device.get("_page") or ""),
                        "confidence": 1,
                        "bbox": device.get("bbox") if isinstance(device.get("bbox"), dict) else None,
                        "x": device.get("x"),
                        "y": device.get("y"),
                    }
                )
            else:
                catalog_hint = str(device.get("catalog") or "").strip()
                items.append(
                    {
                        "kind": "device",
                        "reason": "needs_info",
                        "text": str(device.get("tag") or ""),
                        "tag": str(device.get("tag") or ""),
                        "sheet": str(device.get("_page") or ""),
                        "bbox": device.get("bbox") if isinstance(device.get("bbox"), dict) else None,
                        "image": str(device.get("_image") or ""),
                        "detail": str(device.get("_catalog_review") or ""),
                        "candidates": [catalog_hint] if device.get("_catalog_review") and catalog_hint else [],
                        "x": device.get("x"),
                        "y": device.get("y"),
                    }
                )
    for row in decisions["rejected"]:
        reason = "not_in_evidence" if "extracted text" in str(row.get("reason") or "") else "outside_page" if "outside" in str(row.get("reason") or "") else "unresolved" if row.get("kind") == "wire" else "not_in_evidence"
        items.append(
            {
                "kind": str(row.get("kind") or "item"),
                "reason": reason,
                "text": str(row.get("id") or ""),
                "sheet": str(row.get("sheet") or ""),
                "detail": str(row.get("reason") or ""),
                "bbox": row.get("bbox") if isinstance(row.get("bbox"), dict) else None,
                "candidates": _reject_candidates(row),
                "image": str(row.get("image") or ""),
                "wire_from": str(row.get("from") or ""),
                "wire_to": str(row.get("to") or ""),
                "x": row.get("x"),
                "y": row.get("y"),
            }
        )
    for row in decisions["conflicts"]:
        items.append(
            {
                "kind": str(row.get("kind") or "item"),
                "reason": "conflict",
                "text": str(row.get("id") or ""),
                "sheet": str(row.get("sheet") or ""),
                "detail": str(row.get("reason") or ""),
                "bbox": row.get("bbox") if isinstance(row.get("bbox"), dict) else None,
                "image": str(row.get("image") or ""),
                "wire_from": str(row.get("from") or ""),
                "wire_to": str(row.get("to") or ""),
                "candidates": [end for end in (row.get("from"), row.get("to")) if end],
            }
        )
    for row in decisions.get("needs_review") or []:
        items.append(
            {
                "kind": str(row.get("kind") or "item"),
                "reason": str(row.get("check") or "needs_review"),
                "text": str(row.get("id") or ""),
                "sheet": str(row.get("sheet") or ""),
                "detail": str(row.get("reason") or ""),
                "review_id": str(row.get("review_id") or ""),
                "bbox": row.get("bbox"),
                "image": str(row.get("image") or ""),
                "wire_from": str(row.get("from") or ""),
                "wire_to": str(row.get("to") or ""),
                "candidates": list(row.get("candidates") or []),
                "confidence": 1,
            }
        )
    return items


def _sync_needs_info(decisions: dict[str, Any]) -> None:
    names: list[str] = []
    for device in decisions["devices"]:
        tag = str(device.get("tag") or "")
        if device.get("_as") == "needs-info" and tag and tag not in names:
            names.append(tag)
    decisions["needs_info"] = names


def _update_device_row(device: dict[str, Any], placed_as: str, reason: str) -> None:
    device["_as"] = placed_as
    device["_reason"] = reason


def _stamp_sheet(decisions: dict[str, Any], tag: str, page_id: str, sheet_id: str) -> None:
    for row in decisions["accepted"]:
        if row.get("kind") == "device" and row.get("id") == tag and row.get("page") == page_id:
            row["sheet"] = sheet_id
            row["as"] = str(device_as(decisions, page_id, tag))
            row["reason"] = str(device_reason(decisions, page_id, tag))


def device_as(decisions: dict[str, Any], page_id: str, tag: str) -> str:
    for device in decisions["devices"]:
        if str(device.get("_page")) == page_id and str(device.get("tag")) == tag:
            return str(device.get("_as") or "")
    return ""


def device_reason(decisions: dict[str, Any], page_id: str, tag: str) -> str:
    for device in decisions["devices"]:
        if str(device.get("_page")) == page_id and str(device.get("tag")) == tag:
            return str(device.get("_reason") or "")
    return ""


def _reject_wire(decisions: dict[str, Any], wire: dict[str, Any], reason: str) -> None:
    decisions["accepted"][:] = [
        row
        for row in decisions["accepted"]
        if not (
            row.get("kind") == "wire"
            and row.get("page") == wire["page"]
            and row.get("from") == wire["from"]
            and row.get("to") == wire["to"]
        )
    ]
    decisions["rejected"].append(
        {
            "kind": "wire",
            "id": wire["number"] or f"{wire['from']}->{wire['to']}",
            "sheet": wire["page"],
            "reason": reason,
            "from": wire.get("from"),
            "to": wire.get("to"),
            "bbox": wire.get("bbox") if isinstance(wire.get("bbox"), dict) else None,
            "image": str(wire.get("image") or ""),
        }
    )
    review_id = str(wire.get("review_id") or "")
    if review_id:
        decisions["needs_review"] = [
            item for item in decisions.get("needs_review") or [] if item.get("review_id") != review_id
        ]


def _endpoint_on_sheet(endpoint: str, page_id: str, placed: dict[tuple[str, str], Any]) -> str:
    device, pin = _split_endpoint(endpoint)
    comp = placed.get((page_id, device.upper()))
    if comp is None or not pin:
        return ""
    return f"{comp.id}.{pin}"


def _project_path(source: Path, data: dict[str, Any], out: Path | str | None, prep_dir: Path) -> Path:
    if out is not None:
        dest = Path(out)
    elif str(data.get("out") or "").strip():
        dest = Path(str(data["out"]))
        if not dest.is_absolute():
            dest = (source.parent / dest).resolve()
    else:
        dest = prep_dir / "project.pcad.yaml"
    return dest


def _page_id(sheet: str, pages: dict[str, dict[str, Any]]) -> str | None:
    key = str(sheet or "").strip()
    if key in pages:
        return key
    from panelcad.api.session import _slug

    slug = _slug(key)
    for page_id in pages:
        if _slug(page_id) == slug:
            return page_id
    return None


def _inside(device: dict[str, Any], page: dict[str, Any]) -> bool:
    try:
        x = float(device.get("x"))
        y = float(device.get("y"))
    except (TypeError, ValueError):
        return False
    width = float(page.get("width_mm") or 0)
    height = float(page.get("height_mm") or 0)
    if width <= 0 or height <= 0:
        return False
    return -_PAGE_MARGIN_MM <= x <= width + _PAGE_MARGIN_MM and -_PAGE_MARGIN_MM <= y <= height + _PAGE_MARGIN_MM


def _confidence(item: dict[str, Any]) -> float:
    try:
        return float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        return 0.0


def _device_signature(device: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(device.get("symbol") or "").casefold(),
        str(device.get("contact") or "").upper(),
        str(device.get("manufacturer") or "").casefold(),
        str(device.get("catalog") or "").casefold(),
    )


def _merge_terminals(winner: dict[str, Any], other: dict[str, Any]) -> None:
    names = [str(item).strip() for item in winner.get("terminals") or [] if str(item).strip()]
    for item in other.get("terminals") or []:
        text = str(item).strip()
        if text and text not in names:
            names.append(text)
    winner["terminals"] = names


def _wire_reject(
    label: str,
    page: dict[str, Any],
    raw: dict[str, Any],
    reason: str,
    *,
    frm: str,
    to: str,
) -> dict[str, Any]:
    return {
        "kind": "wire",
        "id": label,
        "sheet": str(page.get("id") or ""),
        "reason": reason,
        "from": frm,
        "to": to,
        "bbox": raw.get("bbox") if isinstance(raw.get("bbox"), dict) else None,
        "image": str(page.get("image") or ""),
        "candidates": [end for end in (frm, to) if end],
    }


def _held_wire(
    label: str,
    page_id: str,
    raw: dict[str, Any],
    frm: str,
    to: str,
    page: dict[str, Any],
) -> dict[str, Any]:
    return {
        "number": label if not str(label).startswith("#") else str(raw.get("number") or ""),
        "from": frm,
        "to": to,
        "sheet": page_id,
        "bbox": raw.get("bbox") if isinstance(raw.get("bbox"), dict) else None,
        "image": str(page.get("image") or ""),
    }


def _reject_candidates(row: dict[str, Any]) -> list[str]:
    found = [str(value) for value in row.get("candidates") or [] if str(value).strip()]
    for end in (row.get("from"), row.get("to")):
        text = str(end or "").strip()
        if text and text not in found:
            found.append(text)
    return found


def _near_readings(tag: str, page: dict[str, Any]) -> list[str]:
    """OCR tokens that match ``tag`` after I/1, O/0, S/5, B/8, and Z/2."""

    want = _confusable(tag)
    if len(want) < 2:
        return []
    found: list[str] = []
    for token in page.get("token_rows") or []:
        text = str(token.get("text") or "").strip()
        if not text or _norm(text) == _norm(tag):
            continue
        if _confusable(text) == want and text not in found:
            found.append(text)
    return found


def _confusable(text: str) -> str:
    return _norm(text).translate(_CONFUSABLE)


def _exact(text: str, page: dict[str, Any]) -> bool:
    return _norm(text) in page["tokens"]


def _phrase(text: str, page: dict[str, Any]) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    if not page["has_text"]:
        return True
    norm = _norm(raw)
    if len(norm) <= 2:
        return norm in page["tokens"]
    if norm in page["tokens"] or norm in page["compact"]:
        return True
    words = [word for word in re.split(r"\s+", raw) if word]
    return len(words) >= 2 and all(_norm(word) in page["tokens"] or _norm(word) in page["compact"] for word in words)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").upper())


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(text or "").upper())


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    text = str(endpoint or "").strip()
    if "." not in text:
        return text, ""
    device, pin = text.split(".", 1)
    return device.strip(), pin.strip()


def _end_key(frm: str, to: str) -> str:
    left = _norm(frm)
    right = _norm(to)
    return "|".join(sorted((left, right)))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _validate(instance: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path} must be {schema['const']!r}")
        return
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
        return
    expected = schema.get("type")
    if expected and not _type_ok(instance, str(expected)):
        errors.append(f"{path} must be {expected}")
        return
    if expected == "object":
        if not isinstance(instance, dict):
            return
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in instance:
                errors.append(f"{path}.{key} is required")
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child = f"{path}.{key}"
            if key in props:
                _validate(value, props[key], child, errors)
            elif extra is False:
                errors.append(f"{child} is not allowed")
        return
    if expected == "array":
        if not isinstance(instance, list):
            return
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                _validate(item, item_schema, f"{path}[{index}]", errors)
        return
    if expected == "string" and isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            errors.append(f"{path} is too short")
        pattern = schema.get("pattern")
        if pattern and re.search(str(pattern), instance) is None:
            errors.append(f"{path} must match {pattern}")
        return
    if expected in {"number", "integer"} and isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path} is below {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path} is above {schema['maximum']}")


def _type_ok(instance: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(instance, dict)
    if expected == "array":
        return isinstance(instance, list)
    if expected == "string":
        return isinstance(instance, str)
    if expected == "boolean":
        return isinstance(instance, bool)
    if expected == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if expected == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    return True
