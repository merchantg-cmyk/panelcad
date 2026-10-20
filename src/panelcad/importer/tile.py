"""Overlap tiles on a page, then merge extractions that landed in two tiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from panelcad.importer.symbols import symbol_rank

TILE_MM = 130.0
OVERLAP_MM = 28.0
TITLE_BAND_MM = 56.0
_MIN_DRAWING_MM = 24.0
_BLANK_INK = 0.0035
# A frame or a title strip on a full tile stays under this. A crop of symbols does not.
_CONTENT_INK = 0.08


def tile_page(page: dict[str, Any]) -> list[dict[str, Any]]:
    kept, _skipped = partition_page_tiles(page)
    return kept


def partition_page_tiles(page: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Tiles the vision pool reads, and the grid cells it skips.

    A skipped cell keeps its grid id and a reason (``title_band`` or ``blank``).
    The importer writes that reason under ``pages/<page>/tiles/<id>.json``.
    A scan with no ink still returns every cell, so the page is not hidden.
    """

    width = float(page["width_mm"])
    height = float(page["height_mm"])
    visible, skipped = _trim_title_report(_grid(page, width, height), height)
    if not visible:
        return _grid(page, width, height), []
    image_path = page.get("image")
    if not image_path:
        return visible, skipped
    try:
        image = Image.open(image_path)
    except OSError:
        return visible, skipped
    kept: list[dict[str, Any]] = []
    for tile in visible:
        ink = _tile_ink(image, page, tile)
        if ink >= _BLANK_INK:
            kept.append(tile)
            continue
        blank = dict(tile)
        blank["skip_reason"] = "blank"
        blank["ink"] = round(ink, 4)
        skipped.append(blank)
    # A blank scan still has a tile for every cell. Dropping them all would
    # hide the page from the vision pool.
    if not kept:
        return visible, [item for item in skipped if item.get("skip_reason") == "title_band"]
    return kept, skipped


def _grid(page: dict[str, Any], width: float, height: float) -> list[dict[str, Any]]:
    step = max(40.0, TILE_MM - OVERLAP_MM)
    tiles: list[dict[str, Any]] = []
    y = 0.0
    row = 0
    index = 0
    while y < height - 1.0:
        x = 0.0
        col = 0
        y1 = min(height, y + TILE_MM)
        while x < width - 1.0:
            x1 = min(width, x + TILE_MM)
            tiles.append(
                {
                    "id": f"tile_{index:02d}",
                    "page_id": page["id"],
                    "row": row,
                    "col": col,
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "w": round(x1 - x, 2),
                    "h": round(y1 - y, 2),
                }
            )
            index += 1
            col += 1
            if x1 >= width - 0.5:
                break
            x += step
        row += 1
        if y1 >= height - 0.5:
            break
        y += step
    return tiles


def _trim_title(tiles: list[dict[str, Any]], height: float) -> list[dict[str, Any]]:
    """Drop the title strip. A tile that still has drawing above it is shortened."""

    kept, _skipped = _trim_title_report(tiles, height)
    return kept


def _trim_title_report(
    tiles: list[dict[str, Any]], height: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The drawing cells, and the title-band cells with ``skip_reason`` set."""

    title_top = height - TITLE_BAND_MM
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for tile in tiles:
        y = float(tile["y"])
        if y >= title_top - 0.5:
            item = dict(tile)
            item["skip_reason"] = "title_band"
            skipped.append(item)
            continue
        bottom = y + float(tile["h"])
        if bottom > title_top:
            above = title_top - y
            if above < _MIN_DRAWING_MM:
                item = dict(tile)
                item["skip_reason"] = "title_band"
                skipped.append(item)
                continue
            tile = dict(tile)
            tile["h"] = round(above, 2)
        kept.append(tile)
    return kept, skipped


def image_ink_fraction(image: Image.Image) -> float:
    """Fraction of a page or crop that holds a stroke. A blank scan is zero."""

    arr = np.asarray(image.convert("L"))
    if arr.size == 0:
        return 0.0
    cell = 12
    height, width = arr.shape[:2]
    if height < cell or width < cell:
        return float((arr < 230).mean())
    trimmed_h = (height // cell) * cell
    trimmed_w = (width // cell) * cell
    view = arr[:trimmed_h, :trimmed_w].reshape(trimmed_h // cell, cell, trimmed_w // cell, cell)
    pooled = view.min(axis=(1, 3))
    return float((pooled < 230).mean())


def _tile_ink(image: Image.Image, page: dict[str, Any], tile: dict[str, Any]) -> float:
    """Fraction of the tile that holds a stroke.

    A box downsample turns a hairline into white, so a power schematic looked
    blank and only the heavy symbols survived. Min-pooling keeps one dark
    pixel in each cell.
    """

    box = _pixel_box(image.size, page, tile)
    if box[2] <= box[0] or box[3] <= box[1]:
        return 0.0
    arr = np.asarray(image.crop(box).convert("L"))
    if arr.size == 0:
        return 0.0
    cell = 12
    height, width = arr.shape[:2]
    if height < cell or width < cell:
        return float((arr < 230).mean())
    trimmed_h = (height // cell) * cell
    trimmed_w = (width // cell) * cell
    view = arr[:trimmed_h, :trimmed_w].reshape(trimmed_h // cell, cell, trimmed_w // cell, cell)
    pooled = view.min(axis=(1, 3))
    return float((pooled < 230).mean())


def crop_tile(page: dict[str, Any], tile: dict[str, Any], dest: Path) -> Path:
    image = Image.open(page["image"]).convert("RGB")
    box = _pixel_box(image.size, page, tile)
    crop = image.crop(box)
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest)
    return dest


def draw_tile_overlay(page: dict[str, Any], tiles: list[dict[str, Any]], dest: Path) -> Path:
    image = Image.open(page["image"]).convert("RGB")
    draw = ImageDraw.Draw(image)
    for tile in tiles:
        x0, y0, x1, y1 = _pixel_box(image.size, page, tile)
        draw.rectangle((x0, y0, x1, y1), outline=(180, 40, 40), width=3)
        draw.text((x0 + 6, y0 + 6), tile["id"], fill=(180, 40, 40))
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest)
    return dest


def tokens_in_tile(tokens: list[dict[str, Any]], tile: dict[str, Any]) -> list[dict[str, Any]]:
    x0, y0 = float(tile["x"]), float(tile["y"])
    x1, y1 = x0 + float(tile["w"]), y0 + float(tile["h"])
    found = []
    for tok in tokens:
        if x0 <= float(tok["x"]) <= x1 and y0 <= float(tok["y"]) <= y1:
            found.append(tok)
    return found


def merge_extractions(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Drop a device or wire that the overlap wrote twice. Keep both wires when they sit apart."""

    devices: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    xrefs: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    spatial_drops: list[dict[str, Any]] = []
    for part in parts:
        for device in part.get("devices") or []:
            _merge_device(devices, device, spatial_drops)
        for wire in part.get("wires") or []:
            _merge_wire(wires, wire)
        for xref in part.get("xrefs") or []:
            xrefs.append(xref)
        for text in part.get("texts") or []:
            texts.append(text)
    _absorb_pinless_labels(wires)
    return {
        "devices": devices,
        "wires": wires,
        "xrefs": _dedupe_xrefs(xrefs),
        "texts": texts,
        "spatial_drops": spatial_drops,
    }


def device_tag_key(tag: str) -> str:
    """Identity of a printed tag. Case and a PLC dot do not make a second device."""

    from panelcad.importer.read import is_plc_address, normalize_device_tag

    text = normalize_device_tag(tag)
    if not text:
        return ""
    if is_plc_address(text):
        return text.replace(".", "_")
    return text


def _device_keep_score(device: dict[str, Any]) -> tuple[int, int, float]:
    """A power symbol outranks a higher-confidence coil or contact. Then confidence."""

    rank = symbol_rank(device)
    return (1 if rank >= 3 else 0, rank, float(device.get("confidence") or 0))


def _prefer_incoming(current: dict[str, Any], device: dict[str, Any]) -> bool:
    """True when ``device`` is the reading to keep for a tag already seen."""

    if device.get("recovered") and not current.get("recovered"):
        return False
    if current.get("recovered") and not device.get("recovered"):
        return True
    return _device_keep_score(device) > _device_keep_score(current)


def _fold_device(current: dict[str, Any], device: dict[str, Any]) -> None:
    """Keep one dict for the tag. The stronger reading replaces the fields."""

    if not _prefer_incoming(current, device):
        return
    tag = current.get("tag")
    current.clear()
    current.update(device)
    current["recovered"] = False
    if tag:
        current["tag"] = tag


def _merge_device(
    devices: list[dict[str, Any]],
    device: dict[str, Any],
    spatial_drops: list[dict[str, Any]] | None = None,
) -> None:
    tag = device_tag_key(str(device.get("tag") or ""))
    if device.get("unreadable") or not tag:
        devices.append(device)
        return
    for current in devices:
        if device_tag_key(str(current.get("tag") or "")) != tag:
            continue
        # Only the same symbol, seen again inside a tile overlap, is one device.
        # A coil and its contact, or the same tag printed again further along
        # the sheet, stay separate occurrences.
        if _same_overlapped_symbol(current, device):
            _fold_device(current, device)
            return
    if _reject_spatial_duplicate(devices, device, spatial_drops):
        return
    devices.append(device)


def _same_overlapped_symbol(current: dict[str, Any], device: dict[str, Any]) -> bool:
    """True when two reads are one symbol photographed in the tile overlap."""

    if symbol_rank(current) != symbol_rank(device):
        return False
    current_shape = _symbol_key(current)
    incoming_shape = _symbol_key(device)
    if current_shape != incoming_shape:
        return False
    if _tiles_known_apart(current, device):
        return False
    return _close(current, device, OVERLAP_MM)


def _tiles_known_apart(current: dict[str, Any], device: dict[str, Any]) -> bool:
    if not all(key in current for key in ("tile_x", "tile_y", "tile_w", "tile_h")):
        return False
    if not all(key in device for key in ("tile_x", "tile_y", "tile_w", "tile_h")):
        return False
    return not _tiles_share(current, device)


def collapse_devices_across_sheets(sheets: list[dict[str, Any]]) -> None:
    """Fold overlapping reads of one symbol. Other occurrences stay on the sheet.

    A second page does not delete a contact, and three switches that share a
    tag stay three boxes. Placement still keeps one component id per tag.
    """

    for sheet in sheets:
        devices: list[dict[str, Any]] = []
        for device in sheet.get("devices") or []:
            if isinstance(device, dict):
                _merge_device(devices, device)
        sheet["devices"] = devices


_SIDE_PINS = {"left", "right", "top", "bottom"}


def _traced_pins(wires: list[dict[str, Any]] | None, tag: str) -> set[str]:
    """Library pin ids a traced conductor already named on this tag."""

    found: set[str] = set()
    for wire in wires or []:
        if not isinstance(wire, dict):
            continue
        for tag_key, pin_key in (("from_tag", "from_pin"), ("to_tag", "to_pin")):
            if str(wire.get(tag_key) or "") != tag:
                continue
            pin = str(wire.get(pin_key) or "").strip()
            if pin and pin not in _SIDE_PINS:
                found.add(pin)
    return found


def _library_pin_ids(device: dict[str, Any], catalog: Any) -> set[str]:
    lib = str(device.get("library_id") or "")
    if not lib or device.get("guess") or catalog is None:
        return set()
    try:
        part = catalog.get(lib)
    except KeyError:
        return set()
    return {str(pin.id) for pin in part.pins}


def _prefer_occurrence(
    current: dict[str, Any],
    device: dict[str, Any],
    wanted: set[str] | None = None,
    catalog: Any = None,
) -> bool:
    """The taller printed tag stays the placed part. The other occurrence still lands wires.

    A coil and a contact share a name and are not the same height. Keeping the
    shorter one must not move the part off the symbol the taller tag named.
    A traced wire that already names a pin keeps the occurrence that owns that pin.
    A higher-rank motor must not take the part away from that contact.
    """

    if wanted and catalog is not None:
        current_hit = bool(_library_pin_ids(current, catalog) & wanted)
        incoming_hit = bool(_library_pin_ids(device, catalog) & wanted)
        if incoming_hit and not current_hit:
            return True
        if current_hit and not incoming_hit:
            return False
    current_h = float(current.get("h") or 0)
    incoming_h = float(device.get("h") or 0)
    if incoming_h > current_h + 0.4 and incoming_h > current_h * 1.08:
        return True
    if current_h > incoming_h + 0.4 and current_h > incoming_h * 1.08:
        return False
    return _prefer_incoming(current, device)


def mark_secondary_occurrences(
    devices: list[dict[str, Any]],
    wires: list[dict[str, Any]] | None = None,
    catalog: Any = None,
) -> None:
    """One dict per tag is the one ``place`` will use. The others keep their boxes."""

    best: dict[str, dict[str, Any]] = {}
    for device in devices:
        if not isinstance(device, dict) or device.get("unreadable") or device.get("endpoint"):
            continue
        key = device_tag_key(str(device.get("tag") or ""))
        if not key:
            continue
        current = best.get(key)
        wanted = _traced_pins(wires, key)
        if current is None or _prefer_occurrence(current, device, wanted, catalog):
            best[key] = device
    for device in devices:
        if not isinstance(device, dict):
            continue
        key = device_tag_key(str(device.get("tag") or ""))
        keeper = best.get(key)
        device["secondary"] = bool(keeper is not None and keeper is not device and key)


_WIRE_END_FIELDS = ("from_tag", "to_tag", "from_pin", "to_pin", "bus")


def _wire_text(value: Any) -> str:
    return str(value or "").strip()


def _wire_has_pins(wire: dict[str, Any]) -> bool:
    return bool(_wire_text(wire.get("from_pin")) and _wire_text(wire.get("to_pin")) and not wire.get("ambiguous"))


def _merge_wire_fields(current: dict[str, Any], incoming: dict[str, Any]) -> None:
    """Copy one wire onto another without blanking an end the other row named.

    A row with pins wins over a pinless label at any confidence. Confidence is
    the max of the two. The box stays with the row that won the ends.
    """

    current_pins = _wire_has_pins(current)
    incoming_pins = _wire_has_pins(incoming)
    current_conf = float(current.get("confidence") or 0)
    incoming_conf = float(incoming.get("confidence") or 0)
    if incoming_pins and not current_pins:
        donor: dict[str, Any] = incoming
        other: dict[str, Any] = current
    elif current_pins and not incoming_pins:
        donor = current
        other = incoming
    elif incoming_conf > current_conf:
        donor = incoming
        other = current
    else:
        donor = current
        other = incoming
    for key in _WIRE_END_FIELDS:
        donor_value = _wire_text(donor.get(key))
        other_value = _wire_text(other.get(key))
        if donor_value:
            current[key] = donor[key]
        elif other_value:
            current[key] = other[key]
    if "x" in donor and "y" in donor:
        current["x"] = donor["x"]
        current["y"] = donor["y"]
    for key in ("w", "h", "crop_box"):
        if donor.get(key) not in (None, "", []):
            current[key] = donor[key]
    current["confidence"] = max(current_conf, incoming_conf)
    if _wire_has_pins(current):
        current["ambiguous"] = False
        current["attached"] = True
        current["stub"] = False


def _merge_wire(wires: list[dict[str, Any]], wire: dict[str, Any]) -> None:
    number = str(wire.get("number") or "")
    if wire.get("unreadable") or not number:
        wires.append(wire)
        return
    for current in wires:
        if str(current.get("number") or "") != number:
            continue
        if _close(current, wire, 8.0):
            _merge_wire_fields(current, wire)
            return
    wires.append(wire)


def _absorb_pinless_labels(wires: list[dict[str, Any]]) -> None:
    """Join a pinless wire number to the nearest conductor that already has pins.

    The label and the conductor share a number and sit in one tile, or in two
    tiles whose page boxes overlap. The label does not stay as its own wire.
    """

    consumed: set[int] = set()
    for index, label in enumerate(wires):
        if _wire_has_pins(label) or not _wire_text(label.get("number")):
            continue
        best_index: int | None = None
        best_distance: float | None = None
        for other_index, other in enumerate(wires):
            if other_index == index or not _wire_has_pins(other):
                continue
            if _wire_text(other.get("number")) != _wire_text(label.get("number")):
                continue
            if not (_close(label, other, 8.0) or _tiles_share(label, other)):
                continue
            distance = _point_distance(label, other)
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_index = other_index
        if best_index is None:
            continue
        _merge_wire_fields(wires[best_index], label)
        consumed.add(index)
    if consumed:
        wires[:] = [wire for index, wire in enumerate(wires) if index not in consumed]


def _symbol_key(device: dict[str, Any]) -> str:
    return str(device.get("symbol") or device.get("type") or "").strip().lower().replace(" ", "_").replace("-", "_")


def _page_box(device: dict[str, Any]) -> tuple[float, float, float, float] | None:
    try:
        width = float(device.get("w") or 0)
        height = float(device.get("h") or 0)
        if width <= 0 or height <= 0:
            return None
        return (float(device.get("x") or 0), float(device.get("y") or 0), width, height)
    except (TypeError, ValueError):
        return None


def _boxes_overlap(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = left
    bx, by, bw, bh = right
    return min(ax + aw, bx + bw) - max(ax, bx) > 0.5 and min(ay + ah, by + bh) - max(ay, by) > 0.5


def _spatial_pair(
    current: dict[str, Any],
    device: dict[str, Any],
    symbol: str,
    box: tuple[float, float, float, float],
    tile: str,
) -> bool:
    other_tile = str(current.get("tile_id") or "")
    if not other_tile or other_tile == tile:
        return False
    if _symbol_key(current) != symbol:
        return False
    other_box = _page_box(current)
    return other_box is not None and _boxes_overlap(box, other_box)


def _note_spatial(drops: list[dict[str, Any]] | None, device: dict[str, Any]) -> None:
    if drops is None:
        return
    drops.append(
        {
            "tag": str(device.get("tag") or ""),
            "symbol_class": _symbol_key(device),
            "tile_id": str(device.get("tile_id") or ""),
            "confidence": float(device.get("confidence") or 0),
            "rule": "dedupe_spatial",
        }
    )


def _reject_spatial_duplicate(
    devices: list[dict[str, Any]],
    device: dict[str, Any],
    spatial_drops: list[dict[str, Any]] | None,
) -> bool:
    """Drop the lower-confidence device when two tiles read one symbol twice.

    The tags can differ. 10M read as a motor on top of 10G is that misread.
    True when ``device`` is the one that was dropped.
    """

    symbol = _symbol_key(device)
    box = _page_box(device)
    tile = str(device.get("tile_id") or "")
    if not symbol or box is None or not tile:
        return False
    incoming_conf = float(device.get("confidence") or 0)
    weaker: list[dict[str, Any]] = []
    stronger = False
    for current in devices:
        if not _spatial_pair(current, device, symbol, box, tile):
            continue
        current_conf = float(current.get("confidence") or 0)
        if incoming_conf < current_conf:
            stronger = True
        elif current_conf < incoming_conf:
            weaker.append(current)
    if stronger:
        _note_spatial(spatial_drops, device)
        return True
    for current in weaker:
        _note_spatial(spatial_drops, current)
        devices.remove(current)
    return False


def _tile_rect(wire: dict[str, Any]) -> tuple[float, float, float, float] | None:
    if not all(key in wire for key in ("tile_x", "tile_y", "tile_w", "tile_h")):
        return None
    try:
        width = float(wire["tile_w"])
        height = float(wire["tile_h"])
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (float(wire["tile_x"]), float(wire["tile_y"]), width, height)


def _tiles_share(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a_box = _tile_rect(left)
    b_box = _tile_rect(right)
    if a_box is None or b_box is None:
        return False
    return _boxes_overlap(a_box, b_box)


def _point_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    dx = float(left.get("x") or 0) - float(right.get("x") or 0)
    dy = float(left.get("y") or 0) - float(right.get("y") or 0)
    return dx * dx + dy * dy


def _dedupe_xrefs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out = []
    for item in items:
        key = (str(item.get("kind") or ""), str(item.get("tag") or ""), str(item.get("target") or item.get("text") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _close(a: dict[str, Any], b: dict[str, Any], tol: float) -> bool:
    if "x" not in a or "x" not in b:
        return True
    dx = float(a.get("x") or 0) - float(b.get("x") or 0)
    dy = float(a.get("y") or 0) - float(b.get("y") or 0)
    return dx * dx + dy * dy <= tol * tol


def _pixel_box(size: tuple[int, int], page: dict[str, Any], tile: dict[str, Any]) -> tuple[int, int, int, int]:
    width, height = size
    sx = width / max(float(page["width_mm"]), 1.0)
    sy = height / max(float(page["height_mm"]), 1.0)
    x0 = int(float(tile["x"]) * sx)
    y0 = int(float(tile["y"]) * sy)
    x1 = int((float(tile["x"]) + float(tile["w"])) * sx)
    y1 = int((float(tile["y"]) + float(tile["h"])) * sy)
    return (max(0, x0), max(0, y0), min(width - 1, x1), min(height - 1, y1))
