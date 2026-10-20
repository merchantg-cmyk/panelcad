"""Per-tile vision traces and the import funnel.

Every scan tile writes one JSON file under ``<workspace>/.import/<run-id>/tiles/``.
``funnel.json`` and ``funnel.md`` say whether each device, wire, bus, and
connection was reported, parsed, accepted, merged, and built. A loss is one of
model_miss, parse_fail, code_reject, wrong_field, merge_loss, build_fail, or
wrong_connection. A pixel read is not a model miss. ``dedupe`` is only a second
copy of a survivor. A wire whose number landed on the wrong pins is
wrong_connection when a truth file is loaded.

Replies are stored as the model sent them. API keys and request headers are
never written.
"""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from panelcad.importer.read import BUS_WIRE_RE, is_wire_label
from panelcad.importer.vision import PROMPT_VERSION

# Letter-first only. Number-first tags such as 10M-G failed this rule and were dropped.
HISTORICAL_TAG_RE = re.compile(r"^[A-Z]{1,4}\d{1,4}[A-Z]?$")

LOSS_MODEL_MISS = "model_miss"
LOSS_OCR_MISS = "ocr_miss"
LOSS_PARSE_FAIL = "parse_fail"
LOSS_CODE_REJECT = "code_reject"
LOSS_WRONG_FIELD = "wrong_field"
LOSS_MERGE_LOSS = "merge_loss"
LOSS_BUILD_FAIL = "build_fail"
LOSS_WRONG_CONNECTION = "wrong_connection"
LOSSES = (
    LOSS_MODEL_MISS,
    LOSS_OCR_MISS,
    LOSS_PARSE_FAIL,
    LOSS_CODE_REJECT,
    LOSS_WRONG_FIELD,
    LOSS_MERGE_LOSS,
    LOSS_BUILD_FAIL,
    LOSS_WRONG_CONNECTION,
)
STAGES = ("reported", "parsed", "accepted", "merged", "built")

_DROP_REQUEST_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "api-key",
    "headers",
    "bearer",
    "x-api-key",
    "proxy-authorization",
    "cookie",
    "set-cookie",
}


def _safe(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "item")).strip("_")
    return text or "item"


def _jsonish(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)[:500]
    return value


def _pins_of(row: dict[str, Any]) -> list[str]:
    left_tag = str(row.get("from_tag") or "").strip()
    right_tag = str(row.get("to_tag") or "").strip()
    left_pin = str(row.get("from_pin") or "").strip()
    right_pin = str(row.get("to_pin") or "").strip()
    if not (left_tag and right_tag and left_pin and right_pin):
        return []
    return sorted([f"{left_tag}.{left_pin}".upper(), f"{right_tag}.{right_pin}".upper()])


def _bbox_of(row: dict[str, Any]) -> list[float]:
    raw = row.get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return []
    try:
        return [round(float(value), 2) for value in raw]
    except (TypeError, ValueError):
        return []


def _page_point(tile: dict[str, Any], bbox: list[float]) -> tuple[float, float]:
    origin_x = float(tile.get("x") or 0)
    origin_y = float(tile.get("y") or 0)
    if len(bbox) != 4:
        return origin_x, origin_y
    return origin_x + bbox[0] + bbox[2] / 2.0, origin_y + bbox[1] + bbox[3] / 2.0


def _candidates(row: dict[str, Any]) -> list[dict[str, Any]]:
    """One reported detection can be a device, a wire or bus, and a connection."""

    if not isinstance(row, dict):
        return []
    tag = str(row.get("tag") or "").strip()
    number = str(row.get("wire_number") or "").strip()
    bus = str(row.get("bus") or "").strip()
    bbox = _bbox_of(row)
    pins = _pins_of(row)
    found: list[dict[str, Any]] = []
    label = tag.upper()
    # A bus or wire number stored in tag is that conductor, not a device.
    misplaced = bool(tag) and not number and not bus and is_wire_label(tag)
    if misplaced:
        kind = "bus" if BUS_WIRE_RE.match(label) else "wire"
        found.append({"kind": kind, "text": label, "bbox": bbox, "pins": pins})
    elif tag:
        found.append({"kind": "device", "text": label, "bbox": bbox, "pins": []})
    if bus and bus.upper() != label:
        found.append({"kind": "bus", "text": bus.upper(), "bbox": bbox, "pins": pins})
    if number and number.upper() != label:
        kind = "bus" if BUS_WIRE_RE.match(number.upper()) else "wire"
        found.append({"kind": kind, "text": number.upper(), "bbox": bbox, "pins": pins})
    if pins:
        found.append({"kind": "connection", "text": "--".join(pins), "bbox": bbox, "pins": pins})
    return found


def _row_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    raw_box = row.get("source_bbox")
    bbox = raw_box if isinstance(raw_box, list) else _bbox_of(row)
    tag = row.get("source_tag") if "source_tag" in row else row.get("tag")
    number = row.get("source_wire_number") if "source_wire_number" in row else row.get("wire_number")
    return (
        str(tag or "").strip().upper(),
        str(number or "").strip().upper(),
        tuple(bbox),
        tuple(_pins_of(row)),
    )


def _stage_name(item: dict[str, Any]) -> str:
    loss = str(item.get("loss") or "")
    if loss:
        return loss
    if item.get("built"):
        return "built"
    if item.get("merged"):
        return "merged"
    if item.get("accepted"):
        return "accepted"
    if item.get("parsed"):
        return "parsed"
    if item.get("reported"):
        return "reported"
    return ""


def _lost_at(item: dict[str, Any]) -> str:
    if item.get("built"):
        return ""
    if item.get("loss") == LOSS_MODEL_MISS or not item.get("reported"):
        return "reported"
    if not item.get("parsed"):
        return "parsed"
    if not item.get("accepted"):
        return "accepted"
    if not item.get("merged"):
        return "merged"
    return "built"


def _public_request(value: Any) -> Any:
    """Drop header-shaped keys and image bytes. Reply text is not passed through here."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            token = str(key).lower().replace("_", "-")
            if token in _DROP_REQUEST_KEYS or str(key).lower() in _DROP_REQUEST_KEYS:
                continue
            cleaned[str(key)] = _public_request(item)
        return cleaned
    if isinstance(value, list):
        return [_public_request(item) for item in value]
    if isinstance(value, str) and value.startswith("data:") and ";base64," in value[:80]:
        return {"omitted": "image"}
    return value


def _compatible(item: dict[str, Any], wire: dict[str, Any]) -> bool:
    pins = [str(pin).upper() for pin in item.get("pins") or []]
    wire_pins = _pins_of(wire)
    kind = str(item.get("kind") or "")
    number = str(wire.get("number") or "").strip().upper()
    if kind in {"wire", "bus"} and number != str(item.get("text") or "").upper():
        return False
    if kind == "connection":
        if not pins or not wire_pins or set(pins) != set(wire_pins):
            return False
        return True
    if pins and wire_pins and set(pins) != set(wire_pins):
        return False
    return kind in {"wire", "bus"}


def _distance(item: dict[str, Any], wire: dict[str, Any]) -> float:
    try:
        dx = float(item.get("page_x") or 0) - float(wire.get("x") or 0)
        dy = float(item.get("page_y") or 0) - float(wire.get("y") or 0)
    except (TypeError, ValueError):
        return 1e18
    return math.hypot(dx, dy)


def _closest(pool: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_dist = 1e18
    for wire in pool:
        if not _compatible(item, wire):
            continue
        dist = _distance(item, wire)
        if dist < best_dist:
            best_dist = dist
            best = wire
    return best


def _item_key(item: dict[str, Any]) -> tuple[Any, ...]:
    pins = tuple(str(pin).upper() for pin in item.get("pins") or [])
    if item.get("kind") == "connection":
        return ("connection", pins)
    return (str(item.get("kind") or ""), str(item.get("text") or "").upper())


def _after_keys(wire: dict[str, Any]) -> list[tuple[Any, ...]]:
    number = str(wire.get("number") or "").strip().upper()
    pins = tuple(_pins_of(wire))
    keys: list[tuple[Any, ...]] = []
    if number:
        kind = "bus" if BUS_WIRE_RE.match(number) else "wire"
        keys.append((kind, number))
    if len(pins) == 2:
        keys.append(("connection", pins))
    return keys


class ImportDiagnostics:
    def __init__(self, workspace: Path | str, run_id: str | None = None) -> None:
        self.run_id = str(run_id or "").strip() or uuid.uuid4().hex[:16]
        self.root = Path(workspace) / ".import" / self.run_id
        self.tiles_dir = self.root / "tiles"
        self.items: dict[str, dict[str, Any]] = {}
        self.truth_rows: list[dict[str, Any]] = []
        self.truth_path = ""
        self.prompt_version = PROMPT_VERSION
        self._built = False
        self._score_truth = False
        self._model_used = False
        self._last_report: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._seq = 0

    def load(self) -> None:
        file = self.root / "funnel.json"
        if file.is_file():
            data = json.loads(file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.prompt_version = str(data.get("prompt_version") or PROMPT_VERSION)
                self.truth_path = str(data.get("truth_path") or "")
                self.truth_rows = list(data.get("truth") or [])
                self._model_used = bool(data.get("model_used"))
                placed = data.get("placed_wires") if isinstance(data.get("placed_wires"), list) else []
                if placed:
                    self._last_report = {"wires": [dict(row) for row in placed if isinstance(row, dict)], "placed": [], "skipped": []}
                    self._built = True
                self.items = {}
                for item in data.get("items") or []:
                    if isinstance(item, dict) and item.get("id"):
                        self.items[str(item["id"])] = dict(item)
        if not self._model_used and self.tiles_dir.is_dir() and any(self.tiles_dir.glob("*.json")):
            self._model_used = True

    def record_exchange(self, record: dict[str, Any], *, page: dict[str, Any], tile: dict[str, Any]) -> None:
        """Write one tile file. ``record`` must already omit headers and keys."""

        page_id = str(page.get("id") or record.get("page_id") or "")
        tile_id = str(tile.get("id") or record.get("tile_id") or "")
        body = dict(record)
        body["prompt_version"] = str(body.get("prompt_version") or PROMPT_VERSION)
        body["schema_version"] = str(body.get("schema_version") or body["prompt_version"])
        body["page_id"] = page_id
        body["tile_id"] = tile_id
        body["bbox"] = {
            "x": float(tile.get("x") or 0),
            "y": float(tile.get("y") or 0),
            "w": float(tile.get("w") or 0),
            "h": float(tile.get("h") or 0),
        }
        body["rotation"] = int(page.get("rotation") or 0)
        dpi = page.get("dpi")
        body["dpi"] = float(dpi) if isinstance(dpi, (int, float)) and not isinstance(dpi, bool) else None
        request = body.get("request")
        body["request"] = _public_request(request) if isinstance(request, dict) else {}
        path = self.tiles_dir / f"{_safe(page_id)}__{_safe(tile_id)}.json"
        with self._lock:
            self._model_used = True
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")

    def trace_name(self, page_id: str, tile_id: str) -> str:
        return f"tiles/{_safe(page_id)}__{_safe(tile_id)}.json"

    def ingest_tile(
        self,
        *,
        page_id: str,
        tile_id: str,
        tile: dict[str, Any],
        reported_rows: list[Any] | None,
        violations: list[dict[str, Any]] | None,
        filtered_rows: list[dict[str, Any]] | None,
    ) -> None:
        """Mark reported, parsed, and accepted or code_reject for one tile."""

        bad_indexes = {int(item.get("index")) for item in (violations or []) if isinstance(item, dict) and "index" in item}
        filtered_by_id = {_row_identity(row): row for row in (filtered_rows or []) if isinstance(row, dict)}
        with self._lock:
            self._model_used = True
            for violation in violations or []:
                if not isinstance(violation, dict):
                    continue
                row = violation.get("row") if isinstance(violation.get("row"), dict) else {}
                pieces = _candidates(row) or [
                    {
                        "kind": "device",
                        "text": str(violation.get("reason") or "malformed JSON"),
                        "bbox": [],
                        "pins": [],
                    }
                ]
                for piece in pieces:
                    self._add(
                        page_id,
                        tile_id,
                        tile,
                        piece,
                        reported=True,
                        parsed=False,
                        accepted=False,
                        loss=LOSS_PARSE_FAIL,
                        rule=str(violation.get("rule") or "schema"),
                    )
            for index, row in enumerate(reported_rows or []):
                if index in bad_indexes or not isinstance(row, dict):
                    continue
                filtered = filtered_by_id.get(_row_identity(row))
                rule = str((filtered or {}).get("reject_rule") or "")
                field = str((filtered or {}).get("wrong_field") or "")
                if field:
                    loss = LOSS_WRONG_FIELD
                    rule = field
                    accepted = False
                elif rule:
                    loss = LOSS_CODE_REJECT
                    accepted = False
                else:
                    loss = ""
                    accepted = True
                for piece in _candidates(row):
                    self._add(
                        page_id,
                        tile_id,
                        tile,
                        piece,
                        reported=True,
                        parsed=True,
                        accepted=accepted,
                        loss=loss,
                        rule=rule,
                    )

    def ingest_pixel(
        self,
        *,
        page_id: str,
        devices: list[dict[str, Any]] | None = None,
        wires: list[dict[str, Any]] | None = None,
    ) -> None:
        """A pixel read is reported and merged. It is not a model miss."""

        tile = {"id": "pixel", "x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
        with self._lock:
            known = {
                (str(item.get("kind") or ""), str(item.get("text") or "").upper())
                for item in self.items.values()
                if item.get("page_id") == page_id and not item.get("truth_only")
            }
            for device in devices or []:
                if device.get("title_ink") or device.get("tag_unparsed") or device.get("unlocalised"):
                    continue
                tag = str(device.get("tag") or "").strip().upper()
                if not tag or ("device", tag) in known:
                    continue
                self._add(
                    page_id,
                    "pixel",
                    tile,
                    {"kind": "device", "text": tag, "bbox": [], "pins": []},
                    reported=True,
                    parsed=True,
                    accepted=True,
                    loss="",
                    rule="",
                    merged=True,
                    source="pixel",
                )
                known.add(("device", tag))
            for wire in wires or []:
                number = str(wire.get("number") or "").strip().upper()
                if not number or wire.get("unlocalised"):
                    continue
                kind = "bus" if BUS_WIRE_RE.match(number) else "wire"
                if (kind, number) in known:
                    continue
                pins = _pins_of(wire)
                self._add(
                    page_id,
                    "pixel",
                    tile,
                    {"kind": kind, "text": number, "bbox": [], "pins": pins},
                    reported=True,
                    parsed=True,
                    accepted=True,
                    loss="",
                    rule="",
                    merged=True,
                    source="pixel",
                )
                known.add((kind, number))

    def note_fallback(self, page: dict[str, Any], tile: dict[str, Any], reason: str) -> None:
        """A tile that raised. Schema violations already on the trace become parse_fail rows."""

        page_id = str(page.get("id") or "")
        tile_id = str(tile.get("id") or "")
        file = self.root / self.trace_name(page_id, tile_id)
        violations: list[dict[str, Any]] = []
        if file.is_file():
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            raw = data.get("schema_violations") if isinstance(data, dict) else None
            if isinstance(raw, list):
                violations = [item for item in raw if isinstance(item, dict)]
        if not violations and reason:
            violations = [{"index": 0, "loss": LOSS_PARSE_FAIL, "rule": "schema", "reason": reason, "row": None}]
        self.ingest_tile(
            page_id=page_id,
            tile_id=tile_id,
            tile=tile,
            reported_rows=[],
            violations=violations,
            filtered_rows=[],
        )

    def settle_page(
        self,
        *,
        page_id: str,
        before_devices: list[dict[str, Any]],
        after_devices: list[dict[str, Any]],
        before_wires: list[dict[str, Any]],
        after_wires: list[dict[str, Any]],
        width_mm: float,
        height_mm: float,
        catalog: Any,
        insets: tuple[float, float, float, float] | None = None,
        spatial_drops: list[dict[str, Any]] | None = None,
    ) -> None:
        """Junk wire rules are code_reject. Dedupe is only a second copy of a survivor."""

        from panelcad.importer.raster import wire_number_reject_reason

        spatial_tags = {
            str(row.get("tag") or "").strip().upper()
            for row in (spatial_drops or [])
            if str(row.get("rule") or "") == "dedupe_spatial" and str(row.get("tag") or "").strip()
        }
        with self._lock:
            counts: Counter[str] = Counter()
            present: set[str] = set()
            for device in after_devices:
                tag = str(device.get("tag") or "").strip().upper()
                if tag and not device.get("tag_unparsed") and not device.get("title_ink"):
                    counts[tag] += 1
                    present.add(tag)
            for item in self.items.values():
                if item.get("page_id") != page_id or item.get("truth_only") or item.get("loss"):
                    continue
                if item.get("source") == "pixel":
                    continue
                if item.get("kind") != "device" or not item.get("accepted"):
                    continue
                tag = str(item.get("text") or "").upper()
                if counts[tag] > 0:
                    counts[tag] -= 1
                    item["merged"] = True
                else:
                    item["merged"] = False
                    item["loss"] = LOSS_MERGE_LOSS
                    if tag in spatial_tags:
                        item["rule"] = "dedupe_spatial"
                    elif tag in present:
                        item["rule"] = "dedupe"
                    else:
                        item["rule"] = "dropped"
                    item["lost_at"] = "merged"

            pending: list[dict[str, Any]] = []
            for item in self.items.values():
                if item.get("page_id") != page_id or item.get("truth_only") or item.get("loss"):
                    continue
                if item.get("source") == "pixel":
                    continue
                if item.get("kind") not in {"wire", "bus", "connection"} or not item.get("parsed"):
                    continue
                source = _closest(before_wires, item)
                reason = ""
                if source is not None:
                    reason = wire_number_reject_reason(
                        source, before_devices, catalog, width_mm, height_mm, insets
                    )
                if reason:
                    item["accepted"] = False
                    item["merged"] = False
                    item["loss"] = LOSS_CODE_REJECT
                    item["rule"] = reason
                    item["lost_at"] = "accepted"
                    continue
                item["accepted"] = True
                pending.append(item)
            before_numbers = {
                str(wire.get("number") or "").strip().upper()
                for wire in before_wires
                if str(wire.get("number") or "").strip()
            }
            survivor_counts: Counter[tuple[Any, ...]] = Counter()
            for wire in after_wires:
                for key in _after_keys(wire):
                    survivor_counts[key] += 1
            groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
            for item in pending:
                groups.setdefault(_item_key(item), []).append(item)
            for key, group in groups.items():
                left = int(survivor_counts.get(key) or 0)
                survived = left > 0
                for item in group:
                    if left > 0:
                        left -= 1
                        item["merged"] = True
                        item["loss"] = ""
                        item["lost_at"] = ""
                        continue
                    item["merged"] = False
                    item["loss"] = LOSS_MERGE_LOSS
                    item["lost_at"] = "merged"
                    number = str(item.get("text") or "").upper()
                    if survived:
                        item["rule"] = "dedupe"
                    elif str(item.get("kind") or "") == "connection" or number in before_numbers:
                        item["rule"] = "attach_miss"
                    else:
                        item["rule"] = "dropped_field"

    def mark_built(self, report: dict[str, Any]) -> None:
        """Merged items that landed on the sheet are built. The rest are build_fail."""

        placed = {str(row.get("tag") or "").upper() for row in report.get("placed") or []}
        numbers = {
            str(row.get("number") or "").upper()
            for row in report.get("wires") or []
            if str(row.get("number") or "").strip()
        }
        pairs = set()
        for row in report.get("wires") or []:
            left = str(row.get("from") or "").upper()
            right = str(row.get("to") or "").upper()
            if left and right:
                pairs.add(frozenset({left, right}))
        skipped = {
            str(row.get("tag") or "").upper(): str(row.get("reason") or "")
            for row in report.get("skipped") or []
        }
        with self._lock:
            self._built = True
            self._last_report = dict(report)
            for item in self.items.values():
                if item.get("truth_only"):
                    continue
                # A bus or wire number recovered from the wrong field still counts once it is on the sheet.
                if item.get("loss") == LOSS_WRONG_FIELD and str(item.get("kind") or "") in {"wire", "bus"}:
                    text = str(item.get("text") or "").upper()
                    if text and text in numbers:
                        item["accepted"] = True
                        item["merged"] = True
                        item["built"] = True
                        item["loss"] = ""
                        item["rule"] = ""
                        item["lost_at"] = ""
                        continue
                if item.get("loss") or not item.get("merged"):
                    continue
                kind = str(item.get("kind") or "")
                text = str(item.get("text") or "").upper()
                ok = False
                rule = ""
                if kind == "device":
                    ok = text in placed
                    rule = skipped.get(text) or "not_placed"
                elif kind in {"wire", "bus"}:
                    ok = text in numbers
                    rule = "not_placed"
                elif kind == "connection":
                    pins = frozenset(str(pin).upper() for pin in item.get("pins") or [])
                    ok = bool(pins) and pins in pairs
                    rule = "not_placed"
                if ok:
                    item["built"] = True
                    item["loss"] = ""
                    item["rule"] = ""
                    item["lost_at"] = ""
                else:
                    item["built"] = False
                    item["loss"] = LOSS_BUILD_FAIL
                    item["rule"] = rule
                    item["lost_at"] = "built"
            self._penalize_wrong_connections(report)

    def _penalize_wrong_connections(self, report: dict[str, Any]) -> None:
        """A numbered wire on the wrong pins is not built when truth names the ends."""

        path = self.truth_path
        if not path or not Path(path).is_file():
            return
        expected: dict[str, set[frozenset[str]]] = {}
        for row in load_truth(path):
            pins = [str(pin).upper() for pin in row.get("pins") or []]
            text = str(row.get("text") or "").upper()
            if text and len(pins) == 2 and "--" not in text:
                expected.setdefault(text, set()).add(frozenset(pins))
        if not expected:
            return
        placed: dict[str, set[frozenset[str]]] = {}
        for row in report.get("wires") or []:
            number = str(row.get("number") or "").upper()
            left = str(row.get("from") or "").upper()
            right = str(row.get("to") or "").upper()
            if number and left and right:
                placed.setdefault(number, set()).add(frozenset({left, right}))
        with self._lock:
            for item in self.items.values():
                if item.get("truth_only"):
                    continue
                if str(item.get("kind") or "") not in {"wire", "bus"}:
                    continue
                text = str(item.get("text") or "").upper()
                if text not in expected or text not in placed:
                    continue
                if placed[text] & expected[text]:
                    continue
                item["built"] = False
                item["merged"] = True
                item["loss"] = LOSS_WRONG_CONNECTION
                item["rule"] = "pins"
                item["lost_at"] = "built"

    def score_truth(self, path: Path | str) -> dict[str, Any]:
        self.truth_path = str(path)
        self._score_truth = True
        return self.write()

    def write(self) -> dict[str, Any]:
        if self._last_report and self.truth_path and Path(self.truth_path).is_file():
            self._penalize_wrong_connections(self._last_report)
        if self.truth_path and (self._built or self._score_truth) and Path(self.truth_path).is_file():
            self._apply_truth(load_truth(self.truth_path))
        document = self.public()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "funnel.json").write_text(json.dumps(document, indent=2, default=str), encoding="utf-8")
        (self.root / "funnel.md").write_text(document["markdown"], encoding="utf-8")
        return document

    def public(self) -> dict[str, Any]:
        with self._lock:
            items = [dict(item) for item in self.items.values()]
        for item in items:
            item["stage"] = _stage_name(item)
            item["lost_at"] = _lost_at(item)
        counts = {stage: sum(1 for item in items if item.get(stage)) for stage in STAGES}
        loss_counts = {name: sum(1 for item in items if item.get("loss") == name) for name in LOSSES}
        losses = [item for item in items if item.get("loss")]
        document = {
            "run_id": self.run_id,
            "path": str(self.root),
            "prompt_version": self.prompt_version,
            "schema_version": self.prompt_version,
            "model_used": self._model_used,
            "truth_path": self.truth_path,
            "counts": counts,
            "loss_counts": loss_counts,
            "items": items,
            "losses": losses,
            "truth": list(self.truth_rows),
            "placed_wires": [
                {
                    "number": str(row.get("number") or ""),
                    "from": str(row.get("from") or ""),
                    "to": str(row.get("to") or ""),
                }
                for row in (self._last_report.get("wires") or [])
                if isinstance(row, dict)
            ],
        }
        document["markdown"] = render_markdown(document)
        return document

    def _apply_truth(self, wanted: list[dict[str, Any]]) -> None:
        with self._lock:
            self.items = {key: value for key, value in self.items.items() if not value.get("truth_only")}
            self.truth_rows = []
            for index, row in enumerate(wanted):
                match = self._match_truth(row)
                if match is None:
                    # A truth item nobody reported was not built and then lost.
                    # A model that ran missed it. Pixel-only OCR missed it.
                    if self._model_used:
                        loss = LOSS_MODEL_MISS
                        rule = ""
                    else:
                        loss = LOSS_OCR_MISS
                        rule = "ocr"
                    lost_at = "reported"
                    item = {
                        "id": f"truth:{row['kind']}:{str(row['text']).upper()}:{index}",
                        "kind": row["kind"],
                        "text": row["text"],
                        "page_id": "",
                        "tile_id": "",
                        "bbox": [],
                        "pins": list(row.get("pins") or []),
                        "page_x": 0.0,
                        "page_y": 0.0,
                        "reported": False,
                        "parsed": False,
                        "accepted": False,
                        "merged": False,
                        "built": False,
                        "loss": loss,
                        "rule": rule,
                        "lost_at": lost_at,
                        "stage": loss,
                        "trace": "",
                        "truth_only": True,
                    }
                    self.items[item["id"]] = item
                    self.truth_rows.append(dict(item))
                    continue
                lost = _lost_at(match)
                self.truth_rows.append(
                    {
                        "kind": row["kind"],
                        "text": row["text"],
                        "stage": "built" if match.get("built") else (match.get("loss") or _stage_name(match)),
                        "lost_at": lost,
                        "loss": "" if match.get("built") else str(match.get("loss") or ""),
                        "rule": "" if match.get("built") else str(match.get("rule") or ""),
                        "page_id": match.get("page_id") or "",
                        "tile_id": match.get("tile_id") or "",
                        "trace": match.get("trace") or "",
                    }
                )

    def _match_truth(self, row: dict[str, Any]) -> dict[str, Any] | None:
        kind = row["kind"]
        text = str(row.get("text") or "").upper()
        pins = [str(pin).upper() for pin in row.get("pins") or []]
        found: list[dict[str, Any]] = []
        for item in self.items.values():
            if item.get("truth_only") or item.get("kind") != kind:
                continue
            if pins and item.get("pins"):
                if {str(pin).upper() for pin in item["pins"]} == set(pins):
                    found.append(item)
                continue
            if str(item.get("text") or "").upper() == text:
                found.append(item)
        if not found:
            return None
        found.sort(
            key=lambda item: (
                bool(item.get("built")),
                bool(item.get("merged")),
                bool(item.get("accepted")),
                bool(item.get("parsed")),
            ),
            reverse=True,
        )
        return found[0]

    def _add(
        self,
        page_id: str,
        tile_id: str,
        tile: dict[str, Any],
        piece: dict[str, Any],
        *,
        reported: bool,
        parsed: bool,
        accepted: bool,
        loss: str,
        rule: str,
        merged: bool = False,
        source: str = "",
    ) -> None:
        self._seq += 1
        bbox = list(piece.get("bbox") or [])
        page_x, page_y = _page_point(tile, bbox)
        item = {
            "id": f"{piece['kind']}:{piece['text']}:{page_id}:{tile_id}:{self._seq}",
            "kind": piece["kind"],
            "text": piece["text"],
            "page_id": page_id,
            "tile_id": tile_id,
            "bbox": bbox,
            "pins": list(piece.get("pins") or []),
            "page_x": round(page_x, 2),
            "page_y": round(page_y, 2),
            "reported": reported,
            "parsed": parsed,
            "accepted": accepted,
            "merged": merged,
            "built": False,
            "loss": loss,
            "rule": rule,
            "source": source,
            "lost_at": _lost_at(
                {
                    "reported": reported,
                    "parsed": parsed,
                    "accepted": accepted,
                    "merged": merged,
                    "built": False,
                    "loss": loss,
                }
            ),
            "stage": "",
            "trace": self.trace_name(page_id, tile_id),
            "truth_only": False,
        }
        item["stage"] = _stage_name(item)
        self.items[item["id"]] = item


def render_markdown(document: dict[str, Any]) -> str:
    counts = document.get("counts") or {}
    losses = document.get("loss_counts") or {}
    lines = [
        f"# Import funnel {document.get('run_id') or ''}",
        "",
        f"Prompt `{document.get('prompt_version') or ''}`",
        "",
        "| Stage | Count |",
        "| --- | ---: |",
    ]
    for stage in STAGES:
        lines.append(f"| {stage} | {int(counts.get(stage) or 0)} |")
    lines.extend(["", "| Loss | Count |", "| --- | ---: |"])
    for name in LOSSES:
        lines.append(f"| {name} | {int(losses.get(name) or 0)} |")
    lines.extend(["", "## Lost items", ""])
    lost = [item for item in document.get("items") or [] if item.get("loss")]
    if not lost:
        lines.append("None.")
    else:
        for item in lost:
            rule = f" {item.get('rule')}" if item.get("rule") else ""
            where = ""
            if item.get("page_id") or item.get("tile_id"):
                where = f" ({item.get('page_id') or '—'} {item.get('tile_id') or '—'})"
            lines.append(f"- {item.get('loss')}{rule} {item.get('kind')} {item.get('text')}{where}")
    truth = document.get("truth") or []
    if truth:
        lines.extend(["", "## Ground truth", ""])
        for item in truth:
            if item.get("lost_at"):
                rule = f" {item.get('rule')}" if item.get("rule") else ""
                lines.append(
                    f"- {item.get('kind')} {item.get('text')}: lost at {item.get('lost_at')} ({item.get('loss')}{rule})"
                )
            else:
                lines.append(f"- {item.get('kind')} {item.get('text')}: built")
    lines.append("")
    return "\n".join(lines)


def load_truth(path: Path | str) -> list[dict[str, Any]]:
    """Load ground truth for funnel scoring.

    Device tags, wire numbers, and bus labels are the printed text.
    Connection endpoints are library pin ids. An overload's line pins are
    T1 T2 T3 and its load pins are 2 4 6. A contactor's line pins are L1 L2 L3
    and its load pins are T1 T2 T3. A motor's leads are T1 T2 T3.
    Pole 1 is the top pin when the ladder runs down the page, and the left pin
    when the ladder runs across the page.
    ``style: printed`` on a connection maps an overload's printed T1 T2 T3 onto
    library load pins 2 4 6, and a contactor pole 1 2 3 onto L1 L2 L3, before
    the pins are compared. The default is library pin ids, so a wire on the
    overload line pin T1 does not match the load pin 2.
    """

    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() in {".yaml", ".yml"}:
        import yaml

        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return normalize_truth(data)


def normalize_truth(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [_truth_item(item, "") for item in data if item not in (None, "")]
    if not isinstance(data, dict):
        raise ValueError("truth file must be a list or an object")
    rows: list[dict[str, Any]] = []
    for kind, key in (("device", "devices"), ("wire", "wires"), ("bus", "buses"), ("connection", "connections")):
        for item in data.get(key) or []:
            rows.append(_truth_item(item, kind))
    if not rows and data.get("items"):
        return normalize_truth(data.get("items"))
    return rows


_OL_PRINTED_LOAD = {"T1": "2", "T2": "4", "T3": "6"}
_CONTACTOR_POLE = {"1": "L1", "2": "L2", "3": "L3"}


def _library_pin(endpoint: str, style: str) -> str:
    text = str(endpoint or "").strip().upper()
    if "." not in text or str(style or "").strip().lower() != "printed":
        return text
    tag, pin = text.split(".", 1)
    pin = pin.strip().upper()
    if "OL" in tag and pin in _OL_PRINTED_LOAD:
        pin = _OL_PRINTED_LOAD[pin]
    elif re.search(r"\d+M(?:-|$)", tag) and pin in _CONTACTOR_POLE:
        pin = _CONTACTOR_POLE[pin]
    return f"{tag}.{pin}"


def _truth_item(item: Any, kind: str) -> dict[str, Any]:
    if isinstance(item, str):
        return {"kind": kind or "device", "text": item.strip(), "pins": []}
    if not isinstance(item, dict):
        return {"kind": kind or "device", "text": str(item), "pins": []}
    text = str(item.get("text") or item.get("tag") or item.get("number") or "").strip()
    pins: list[str] = []
    if item.get("from") and item.get("to"):
        style = str(item.get("style") or "")
        pins = sorted([_library_pin(str(item["from"]), style), _library_pin(str(item["to"]), style)])
        if not text:
            text = "--".join(pins)
    got = str(item.get("kind") or kind or ("connection" if pins else "device"))
    return {"kind": got, "text": text, "pins": pins}


def score_tag_rule(
    detections: list[dict[str, Any]],
    pattern: re.Pattern[str],
    root: Path | str,
    *,
    catalog: Any = None,
    tile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score reported rows with a tag rule. A letter-first rule rejects 10M-G as TAG_RE."""

    from panelcad.importer.vision import filter_vision_detections

    box = dict(tile or {"id": "tile_00", "x": 0.0, "y": 0.0, "w": 280.0, "h": 430.0})
    filtered = filter_vision_detections(
        detections,
        catalog,
        box,
        tag_re=pattern,
        unmatched_is_reject=True,
    )
    diag = ImportDiagnostics(root, run_id="historical-tag-re")
    diag.ingest_tile(
        page_id="page_001",
        tile_id=str(box.get("id") or "tile_00"),
        tile=box,
        reported_rows=detections,
        violations=[],
        filtered_rows=filtered,
    )
    return diag.write()


def _stamp_dir(properties: dict[str, Any] | None) -> Path | None:
    if not isinstance(properties, dict):
        return None
    info = properties.get("print_import") if isinstance(properties.get("print_import"), dict) else properties
    if not isinstance(info, dict):
        return None
    raw = str(info.get("diagnostics") or "")
    if raw and (Path(raw) / "funnel.json").is_file():
        return Path(raw)
    return None


def _stamp_from_yaml(path: Path) -> Path | None:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    return _stamp_dir(data.get("properties") if isinstance(data.get("properties"), dict) else None)


def _latest_import_run(folder: Path) -> Path | None:
    """The newest ``.import/<run>/funnel.json`` under a workspace folder."""

    root = folder / ".import"
    if not root.is_dir():
        return None
    found: list[tuple[float, Path]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return None
    for child in children:
        funnel = child / "funnel.json"
        if not child.is_dir() or not funnel.is_file():
            continue
        try:
            found.append((funnel.stat().st_mtime, child))
        except OSError:
            continue
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[-1][1]


def resolve_run_dir(
    run: str | Path | None = None,
    *,
    project_path: Path | str | None = None,
    properties: dict[str, Any] | None = None,
) -> Path:
    """A run directory, a funnel.json path, a project file, or a run id under ``.import``."""

    candidates: list[Path] = []
    if run:
        raw = Path(str(run))
        candidates.append(raw)
        if raw.suffix.lower() in {".yaml", ".yml"} and raw.is_file():
            stamped = _stamp_from_yaml(raw)
            if stamped is not None:
                return stamped
        if raw.is_file() and raw.name == "funnel.json":
            return raw.parent
        candidates.append(Path.cwd() / ".import" / raw.name)
        if project_path:
            candidates.append(Path(project_path).parent / ".import" / raw.name)
    stamped = _stamp_dir(properties)
    if stamped is not None:
        candidates.append(stamped)
    for cand in candidates:
        if cand.is_dir() and (cand / "funnel.json").is_file():
            return cand
        if cand.is_dir():
            latest = _latest_import_run(cand)
            if latest is not None:
                return latest
        if cand.is_file() and cand.name == "funnel.json":
            return cand.parent
    tried = ", ".join(str(item) for item in candidates) or "(none)"
    raise FileNotFoundError(f"no import diagnostics at {tried}")


def load_run(
    run: str | Path | None = None,
    *,
    truth: str | Path | None = None,
    project_path: Path | str | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = resolve_run_dir(run, project_path=project_path, properties=properties)
    data = json.loads((root / "funnel.json").read_text(encoding="utf-8"))
    if not truth:
        if "markdown" not in data and (root / "funnel.md").is_file():
            data["markdown"] = (root / "funnel.md").read_text(encoding="utf-8")
        return data
    workspace = root.parent.parent
    diag = ImportDiagnostics(workspace, run_id=root.name)
    diag.load()
    return diag.score_truth(truth)
