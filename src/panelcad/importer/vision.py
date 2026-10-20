"""Pluggable vision backends.

External mode does not call a model: the caller submits JSON for one crop.
The configured backend reuses the drawing agent's model string when that
model can actually be reached. Tests use the fake backend and recorded JSON.
"""

from __future__ import annotations

import contextvars
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol

from panelcad.importer.models import CONVENTIONAL_WIRES
from panelcad.importer.read import (
    BUS_WIRE_RE,
    TAG_RE,
    is_cross_ref,
    is_device_tag,
    is_nema_line_ref,
    is_wire_label,
    normalize_device_tag,
)
from panelcad.importer.schemas import instructions_for
from panelcad.importer.symbols import PREFIX_CONFIDENCE, resolve_symbol, supported_tag_family, tag_prefix
from panelcad.importer.vision_config import (
    PRESET_GROK_BUILD,
    PRESET_NONE,
    PRESET_OLLAMA,
    USD_TICKS,
    VisionSettings,
    redact,
)


class NeedsSubmit(RuntimeError):
    """The pipeline is waiting for an outside agent. No model was called."""


class VisionFallback(RuntimeError):
    """The vision call failed or the JSON did not match the schema.

    The import keeps the pixel-only read and says so in the review list.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason.strip() or "vision request failed"
        super().__init__(self.reason)


class VisionBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def extract(self, work: dict[str, Any]) -> dict[str, Any]: ...


class ExternalVision:
    """No API key and no model. import_submit carries the extraction."""

    name = "external"

    def available(self) -> bool:
        return False

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        raise NeedsSubmit(str(work.get("id") or "work"))


class FakeVision:
    """Return recorded JSON. The key is the work id, then the crop file stem."""

    name = "fake"

    def __init__(self, recordings: dict[str, dict[str, Any]] | None = None) -> None:
        self.recordings = dict(recordings or {})

    def available(self) -> bool:
        return True

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        work_id = str(work.get("id") or "")
        if work_id in self.recordings:
            return json.loads(json.dumps(self.recordings[work_id]))
        image = str(work.get("image") or "")
        stem = Path(image).stem if image else ""
        if stem and stem in self.recordings:
            return json.loads(json.dumps(self.recordings[stem]))
        # A missing recording is unreadable. It is not a guess.
        kind = str(work.get("kind") or "tile")
        if kind == "classify":
            return {
                "kind": "other",
                "title": "",
                "drawing_number": "",
                "rev": "",
                "confidence": 0.0,
                "unreadable_fields": ["kind", "title", "drawing_number", "rev"],
            }
        if kind == "photo":
            return {"devices": [], "rails": [], "ducts": [], "strips": [], "unreadable": True}
        if kind == "bom":
            return {"rows": [], "unreadable": True}
        return {"devices": [], "wires": [], "xrefs": [], "texts": [{"text": "", "unreadable": True, "confidence": 0.0}]}


class ConfiguredVision:
    """The same model spec the drawing agent uses, if the endpoint answers."""

    name = "configured"

    def __init__(self, spec: str | None = None) -> None:
        self.spec = spec or _default_spec()
        self._probe: bool | None = None

    def available(self) -> bool:
        if self._probe is None:
            self._probe = probe_model(self.spec)
        return self._probe

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        if not self.available():
            raise NeedsSubmit("vision model is not reachable")
        image = Path(str(work.get("image") or ""))
        if not image.is_file():
            raise NeedsSubmit(f"crop missing: {image}")
        payload = _run_model(self.spec, work, image.read_bytes())
        if not isinstance(payload, dict):
            raise NeedsSubmit("model did not return a JSON object")
        return payload


def _default_spec() -> str:
    env = os.environ.get("PANELCAD_VISION_MODEL") or os.environ.get("PANELCAD_MODEL")
    if env:
        return env
    try:
        from panelcad.ai.drawing import DEFAULT_MODEL

        return DEFAULT_MODEL
    except Exception:  # noqa: BLE001
        return "ollama:gemma3:4b"


def probe_model(spec: str) -> bool:
    """True only when the configured endpoint accepts a connection. Never raises."""

    try:
        if spec.startswith("ollama:"):
            base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
            root = base.rstrip("/")
            if root.endswith("/v1"):
                root = root[: -len("/v1")]
            with urllib.request.urlopen(root + "/api/tags", timeout=1.5) as reply:  # noqa: S310
                return int(getattr(reply, "status", 200)) < 400
        # A cloud spec still needs a key. Missing key means not reachable.
        if spec.startswith("openai:") or spec.startswith("gpt"):
            return bool(os.environ.get("OPENAI_API_KEY"))
        if spec.startswith("anthropic:") or spec.startswith("claude"):
            return bool(os.environ.get("ANTHROPIC_API_KEY"))
        if spec.startswith("google:") or spec.startswith("gemini"):
            return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    except Exception:  # noqa: BLE001
        return False
    return False


def _run_model(spec: str, work: dict[str, Any], image: bytes) -> dict[str, Any]:
    from pydantic_ai import Agent, BinaryContent

    from panelcad.ai.drawing import model_for

    kind = str(work.get("kind") or "tile")
    instructions = str(work.get("instructions") or instructions_for(kind))
    schema = work.get("schema") or {}
    summary = work.get("summary") or {}
    prompt = (
        instructions
        + "\n\nPage context (compact):\n"
        + json.dumps(summary, default=str)[:1800]
        + "\n\nCrop:\n"
        + json.dumps({k: work.get(k) for k in ("id", "page_id", "page_mm", "kind")}, default=str)
        + "\n\nReturn one JSON object and nothing else. Schema keys:\n"
        + json.dumps(list((schema or {}).get("properties") or {}), default=str)
    )
    agent: Agent[None, str] = Agent(model_for(spec), output_type=str, instructions="You extract one drawing crop into JSON.")
    result = agent.run_sync(
        [
            prompt,
            BinaryContent(data=image, media_type="image/png"),
        ]
    )
    text = str(result.output or "")
    return _parse_json(text)


def _parse_json(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise VisionFallback("malformed JSON")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise VisionFallback("malformed JSON") from exc
    if not isinstance(data, dict):
        raise VisionFallback("malformed JSON")
    return data


def backend_for(
    name: str,
    recordings: dict[str, dict[str, Any]] | None = None,
    spec: str | None = None,
    vision: VisionSettings | None = None,
    transport: Callable[..., Any] | None = None,
) -> VisionBackend:
    settings = (vision or VisionSettings(preset=PRESET_NONE)).resolved()
    if settings.preset == PRESET_GROK_BUILD:
        from panelcad.importer.grok_build import GrokBuildVision

        return GrokBuildVision(settings, runner=transport)
    if settings.preset != PRESET_NONE:
        return OpenAICompatibleVision(settings, transport=transport)
    token = (name or "auto").strip().lower()
    if token == "fake":
        return FakeVision(recordings)
    if token in {"external", "raster", "none"}:
        # raster reads pixels in the import job. Nothing is submitted.
        return ExternalVision()
    if token == "configured":
        return ConfiguredVision(spec)
    # auto: a reachable drawing-agent model, otherwise the caller submits crops.
    configured = ConfiguredVision(spec)
    if configured.available():
        return configured
    return ExternalVision()


# Each detection the model returns. bbox is millimetres on the crop.
DETECTION_KEYS = ("tag", "wire_number", "symbol_class", "bbox", "confidence")
# Bump this when the prompt wording or the tool schema changes. Every tile trace records it.
PROMPT_VERSION = "2026-10-16.1"

# Part types the prompt names. A stuffed pin is recovered only when its class is one of these.
SYMBOL_CLASSES = (
    "breaker",
    "mpcb",
    "contactor",
    "overload",
    "motor",
    "terminal",
    "pushbutton",
    "estop",
    "pilot",
    "plc",
    "hmi",
    "power_supply",
    "drive",
    "relay",
    "coil",
    "contact",
    "fuse",
    "disconnect",
)

TILE_PROMPT = """
Read only this crop of one electrical drawing. Return one JSON object and nothing else.
The object has four lists: devices, conductors, wire_numbers, and buses.
tag is required on every device row. A device row also needs symbol_class and bbox.
A row with symbol_class and no tag is not a device. Do not return a row that is only a bbox.
Do not put a tag, a part type, a wire number, or a bus inside from_pin or to_pin.
One device, using shapes rather than text from this crop:
{"devices":[{"tag":"<letter-digit or digit-letter>","symbol_class":"contactor","bbox":[x,y,w,h],"confidence":0.9}],"conductors":[],"wire_numbers":[],"buses":[]}
One conductor. to_tag is filled. These words are not printed on this crop:
{"devices":[],"conductors":[{"wire_number":"<digits T digit>","bbox":[12,40,8,4],"confidence":0.9,"from_tag":"<near device>","from_pin":"2","to_tag":"<far device>","to_pin":"T1"}],"wire_numbers":[],"buses":[]}
These shapes describe the fields. They are not tags printed on this crop.
bbox is four numbers [x, y, w, h] in millimetres from the top-left of this crop.
Do not copy the crop rectangle. Do not use pixels or a 0-1000 scale.
symbol_class is one of breaker, mpcb, contactor, overload, motor, terminal,
pushbutton, estop, pilot, plc, hmi, power_supply, drive, relay, coil, contact,
fuse, disconnect.
Put a device tag only in tag. A tag is letter-first (letters, then digits) or number-first (digits, then letters, with an optional hyphen suffix).
Free-form NEMA/JIC tags are tags too: FU1-7, FU-1A, FU1A1, CR410, DRIVE1, MOT1, DISC1, S-204, LS-300, TB2-24, PLT001, PBLT001, and the bare names PWS and HMI.
A PLC address such as I0.0, I17.3, Q0.1, or Q40.2L is a tag. A NEMA line reference such as (410) or 6-410 is not a device. Put a bare line number such as 410 in wire_number.
Put a wire number only in wire_number. A wire number is digits, or digits, the letter T, and a lead digit.
Put a bus label only in bus. A bus is digits, the letter B, more digits, and one column letter.
A bracketed sheet cross-reference such as [9B34] is not a wire, not a bus, and not a device. Omit it.
Border grid numbers and letters are not wire numbers.
A conductor names both ends: from_tag, from_pin, to_tag, to_pin.
A pin is at most 12 characters and has no spaces.
When the other device is the next symbol on the rung, still fill from_pin and to_pin.
Contactor line pins are L1 L2 L3 and load pins are T1 T2 T3.
Overload line pins are T1 T2 T3 (also 1 3 5) and load pins are 2 4 6.
Motor leads are T1 T2 T3.
Pole 1 is the top pin when the ladder runs down the page, and the left pin when the ladder runs across the page.
confidence is a number from 0 to 1.
Copy the printed characters. Read a tag or a number even when a stroke is faint.
A wire number in tag, or a device tag in wire_number, is the wrong field.
Do not invent a tag, a wire number, a bus, or a catalog number.
""".strip()

SHORT_TILE_PROMPT = """
Return one JSON object and nothing else.
The object has devices, conductors, wire_numbers, and buses.
tag is required on every device row, with symbol_class and bbox. Do not return a row that is only a bbox.
Do not put a tag or a part type in to_pin. A pin is at most 12 characters and has no spaces.
Each row needs bbox as four numbers [x, y, w, h] in millimetres from the top-left of this crop.
Put a device tag only in tag. Free-form NEMA tags (FU1-7, CR410, DRIVE1, S-204, LS-300, TB2-24, I0.0, Q40.2L, PWS, HMI) belong in tag. A line reference such as 6-410 is not a device.
Put a wire number only in wire_number. Put a bus label only in bus.
A wire number in tag, or a device tag in wire_number, is the wrong field.
A bus is digits, the letter B, digits, and one column letter. A bracketed cross-reference such as [9B34] is not a wire and not a bus.
A conductor fills from_tag, from_pin, to_tag, and to_pin. to_tag is the device at the other end.
Contactor pins are L1 L2 L3 and T1 T2 T3. Overload line pins are T1 T2 T3 and load pins are 2 4 6. Motor pins are T1 T2 T3.
Do not invent a tag. Do not copy the crop rectangle as a bbox.
""".strip()

# Letter codes that name a device family. A plotter often prints O as D.
# 10DL-G is not a family; 10OL-G is an overload. The printed tag is not rewritten.
DEVICE_FAMILIES = {
    "OL": "overload",
    "M": "contactor",
    "CR": "relay",
    "FU": "fuse",
    "CB": "breaker",
    "PB": "pushbutton",
    "PBLT": "pushbutton",
    "LT": "pilot",
    "PL": "pilot",
    "PLT": "pilot",
    "MS": "contactor",
    "DS": "disconnect",
    "DISC": "disconnect",
    "TR": "timer",
    "K": "contactor",
    "Q": "breaker",
    "F": "fuse",
    "CON": "contactor",
    "DRIVE": "drive",
    "VFD": "drive",
    "MOT": "motor",
    "MTR": "motor",
    "PWS": "power_supply",
    "PS": "power_supply",
    "HMI": "hmi",
    "S": "pushbutton",
    "LS": "limit",
    "TB": "terminal",
    "PLC": "plc",
}

_PIN_MAX = 12

# grok-build passes this object as --json-schema. xAI structured outputs treat oneOf
# the same as anyOf, so one combined row could omit the tag and still match a looser
# branch. Separate lists require tag, part type, and box on a device, and they limit
# a pin to 12 characters. A legacy detections array still parses.
_BBOX_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": "Crop millimetres [x, y, w, h] from the top-left of this crop. Not the crop rectangle.",
    "items": {"type": "number"},
    "minItems": 4,
    "maxItems": 4,
}
_PIN_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": _PIN_MAX,
    "description": "Pin name, at most 12 characters, no spaces. Contactor L1 L2 L3 or T1 T2 T3. Overload T1 T2 T3 or 2 4 6. Motor T1 T2 T3.",
}
_CONFIDENCE_SCHEMA: dict[str, Any] = {"type": "number"}

DETECTION_SCHEMA: dict[str, Any] = {
    "title": f"panelcad-detections-{PROMPT_VERSION}",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "devices": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tag": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Device tag only. Letter-first, number-first, or free-form NEMA (FU1-7, CR410, DRIVE1, S-204, I0.0, Q40.2L). Never a wire number or a bus.",
                    },
                    "symbol_class": {
                        "type": "string",
                        "enum": list(SYMBOL_CLASSES),
                        "description": "Part type. Required on every device row.",
                    },
                    "bbox": _BBOX_SCHEMA,
                    "confidence": _CONFIDENCE_SCHEMA,
                },
                "required": ["tag", "symbol_class", "bbox"],
            },
        },
        "conductors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "bbox": _BBOX_SCHEMA,
                    "from_pin": _PIN_SCHEMA,
                    "to_pin": _PIN_SCHEMA,
                    "from_tag": {"type": "string", "description": "Device tag at the first end of a conductor."},
                    "to_tag": {
                        "type": "string",
                        "description": "Device tag at the other end of a conductor. Fill it on every conductor row.",
                    },
                    "wire_number": {
                        "type": "string",
                        "description": "Wire number only. Digits, or digits, T, and a lead digit. Never a device tag or a bus.",
                    },
                    "confidence": _CONFIDENCE_SCHEMA,
                },
                "required": ["bbox", "from_pin", "to_pin"],
            },
        },
        "wire_numbers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "bbox": _BBOX_SCHEMA,
                    "wire_number": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Wire number only. Digits, or digits, T, and a lead digit. Never a device tag or a bus.",
                    },
                    "confidence": _CONFIDENCE_SCHEMA,
                },
                "required": ["bbox", "wire_number"],
            },
        },
        "buses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "bbox": _BBOX_SCHEMA,
                    "bus": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Bus label only. Digits, B, digits, and one column letter. Not a bracketed cross-reference.",
                    },
                    "confidence": _CONFIDENCE_SCHEMA,
                },
                "required": ["bbox", "bus"],
            },
        },
    },
    "required": ["devices", "conductors", "wire_numbers", "buses"],
}

_VISION_TRACE: contextvars.ContextVar[Any] = contextvars.ContextVar("panelcad_vision_trace", default=None)


def set_vision_trace(hook: Callable[[dict[str, Any]], None] | None):
    """Install a per-call recorder. The hook receives one tile exchange and must not see headers."""

    return _VISION_TRACE.set(hook)


def reset_vision_trace(token: contextvars.Token[Any]) -> None:
    _VISION_TRACE.reset(token)


def emit_vision_trace(record: dict[str, Any]) -> None:
    hook = _VISION_TRACE.get()
    if hook is None:
        return
    hook(record)


def backend_label(preset: str) -> str:
    """Grok, Ollama, or custom. Local Grok Build is still Grok."""

    token = str(preset or "").strip().lower()
    if token in {"grok", "grok-build"}:
        return "Grok"
    if token == "ollama":
        return "Ollama"
    if token == "custom":
        return "custom"
    return token or "custom"


def usage_numbers(body: Any) -> tuple[int, int, float | None]:
    """Input tokens, output tokens, and USD cost. Missing usage is zeros and no cost."""

    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = _first_number(usage, ("prompt_tokens", "input_tokens", "inputTokens"))
    output_tokens = _first_number(usage, ("completion_tokens", "output_tokens", "outputTokens"))
    if isinstance(body, dict) and not input_tokens and not output_tokens:
        input_tokens = _first_number(body, ("prompt_eval_count",))
        output_tokens = _first_number(body, ("eval_count",))
    cost: float | None = None
    ticks = usage.get("cost_in_usd_ticks") if isinstance(usage, dict) else None
    if isinstance(ticks, (int, float)) and not isinstance(ticks, bool):
        cost = float(ticks) / USD_TICKS
    elif isinstance(body, dict):
        usd = body.get("total_cost_usd")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            cost = float(usd)
    return input_tokens, output_tokens, cost


def _first_number(source: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


def _detection_text(value: Any) -> str | None:
    """A field the model may send as text, a number, or null. A bool is malformed."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return None


def _detection_confidence(value: Any) -> float:
    """Clamp a numeric confidence. An unreadable value is the midpoint, not a dropped row."""

    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return 0.5
    elif isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    else:
        number = float(value)
    return min(1.0, max(0.0, number))


class _SchemaIncomplete(Exception):
    """The row parsed, but it is not a device, a wire number, a bus, or a conductor."""


def _optional_pin(value: Any) -> str:
    """A pin name. A sentence or a long string is not a pin on this row."""

    if value is None:
        return ""
    if isinstance(value, bool):
        raise VisionFallback("malformed JSON")
    if isinstance(value, (int, float)):
        number = float(value)
        text = str(int(number)) if number.is_integer() else str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise VisionFallback("malformed JSON")
    if not text:
        return ""
    if any(ch.isspace() for ch in text) or len(text) > _PIN_MAX:
        raise _SchemaIncomplete()
    return text


_LIST_KEYS = ("devices", "conductors", "wire_numbers", "buses", "labels")
_STUFFED_CLASS = re.compile(r"symbol_class\b[\s\"':=]+([A-Za-z][A-Za-z0-9_]*)", re.I)
_STUFFED_TAG = re.compile(r"(?:^|[^A-Za-z0-9_])tag\b[\s\"':=]+([A-Za-z0-9][A-Za-z0-9-]*)", re.I)


def coerce_detection_body(data: Any) -> Any:
    """Flatten devices, conductors, wire numbers, and buses into one detections list.

    A legacy detections array is left as it is. An empty list object is an empty tile.
    """

    if not isinstance(data, dict) or not any(key in data for key in _LIST_KEYS):
        return data
    rows: list[dict[str, Any]] = []
    for key in _LIST_KEYS:
        group = data.get(key)
        if isinstance(group, list):
            rows.extend(item for item in group if isinstance(item, dict))
    legacy = data.get("detections")
    if isinstance(legacy, list):
        rows.extend(item for item in legacy if isinstance(item, dict))
    return {"detections": rows}


def _recover_stuffed_device(row: dict[str, Any]) -> dict[str, Any] | None:
    """Pull a device out of stuffed text only when the tag and part type both hold.

    The printed tag has to be a device tag this sheet can build, and the part
    type has to be that tag's family. Anything else stays incomplete. The prose
    is not kept as a pin. ``recovered`` marks the row.
    """

    classes: list[str] = []
    direct = str(row.get("symbol_class") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if direct:
        classes.append(direct)
    blobs = [value for value in row.values() if isinstance(value, str) and value.strip()]
    blob = "\n".join(blobs)
    classes.extend(match.group(1).lower().replace("-", "_") for match in _STUFFED_CLASS.finditer(blob))
    tags = [match.group(1).upper() for match in _STUFFED_TAG.finditer(blob)]
    tag_field = str(row.get("tag") or "").strip().upper()
    if tag_field:
        tags.append(tag_field)
    chosen_tag = ""
    chosen_class = ""
    for candidate in tags:
        if not is_device_tag(candidate):
            continue
        family = supported_tag_family(candidate)
        if not family or family not in SYMBOL_CLASSES or family not in classes:
            continue
        chosen_tag = candidate
        chosen_class = family
        break
    if not chosen_tag:
        return None
    bbox = row.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        box = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if box[2] <= 0 or box[3] <= 0:
        return None
    return {
        "tag": chosen_tag,
        "wire_number": "",
        "symbol_class": chosen_class,
        "bbox": box,
        "confidence": _detection_confidence(row.get("confidence", 0.5)),
        "recovered": True,
        "needs_review": True,
    }


def validate_detections(data: Any) -> list[dict[str, Any]]:
    """Return the detection list, or raise VisionFallback when the shape is wrong.

    A parsed row that is not a device, wire, bus, or conductor raises
    ``_SchemaIncomplete``. The caller records that as schema_incomplete and
    keeps the rows beside it. ``from_tag`` plus both pins is a one-ended
    conductor. Both pins with no tags is the same conductor: the ends are the
    devices that own those pins.
    """

    data = coerce_detection_body(data)
    if not isinstance(data, dict) or "detections" not in data:
        raise VisionFallback("malformed JSON")
    rows = data.get("detections")
    if not isinstance(rows, list):
        raise VisionFallback("malformed JSON")
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise VisionFallback("malformed JSON")
        if _blank_detection(row):
            continue
        tag = _detection_text(row.get("tag", ""))
        number = _detection_text(row.get("wire_number", ""))
        symbol = _detection_text(row.get("symbol_class", ""))
        bus = _detection_text(row.get("bus", ""))
        if tag is None or number is None or symbol is None or bus is None:
            raise VisionFallback("malformed JSON")
        bbox = row.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise VisionFallback("malformed JSON")
        try:
            box = [float(value) for value in bbox]
        except (TypeError, ValueError) as exc:
            raise VisionFallback("malformed JSON") from exc
        confidence = _detection_confidence(row.get("confidence", 0.5))
        if box[2] <= 0 or box[3] <= 0:
            raise VisionFallback("malformed JSON")
        item = {
            "tag": str(tag).strip(),
            "wire_number": str(number).strip(),
            "symbol_class": str(symbol).strip(),
            "bbox": box,
            "confidence": confidence,
        }
        if str(bus).strip():
            item["bus"] = str(bus).strip()
        for key in ("from_tag", "to_tag"):
            extra = row.get(key)
            if isinstance(extra, str) and extra.strip():
                item[key] = extra.strip()
        try:
            for key in ("from_pin", "to_pin"):
                pin = _optional_pin(row.get(key))
                if pin:
                    item[key] = pin
        except _SchemaIncomplete:
            recovered = _recover_stuffed_device(row)
            if recovered is None:
                raise
            cleaned.append(recovered)
            continue
        one_end = bool(item.get("from_pin") and item.get("to_pin") and (item.get("from_tag") or item.get("to_tag")))
        pins_only = bool(item.get("from_pin") and item.get("to_pin"))
        if not item["tag"] and not item["wire_number"] and not item.get("bus") and not one_end and not pins_only:
            recovered = _recover_stuffed_device(row)
            if recovered is None:
                raise _SchemaIncomplete()
            cleaned.append(recovered)
            continue
        if row.get("needs_review"):
            item["needs_review"] = True
        if row.get("tag_unparsed"):
            item["tag_unparsed"] = True
        cleaned.append(item)
    return cleaned


def partition_detections(data: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep usable rows. A parsed but incomplete row is schema_incomplete.

    A body that is not a detections object still raises VisionFallback. One bad
    row does not discard the valid rows beside it. An empty object is an empty tile.
    A pin that is a sentence is schema_incomplete on that row only.
    """

    if isinstance(data, dict) and not data:
        return [], []
    data = coerce_detection_body(data)
    if not isinstance(data, dict) or "detections" not in data:
        raise VisionFallback("malformed JSON")
    rows = data.get("detections")
    if not isinstance(rows, list):
        raise VisionFallback("malformed JSON")
    valid: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if isinstance(row, dict) and _blank_detection(row):
            continue
        try:
            cleaned = validate_detections({"detections": [row]})
        except _SchemaIncomplete:
            violations.append(
                {
                    "index": index,
                    "loss": "parse_fail",
                    "rule": "schema_incomplete",
                    "reason": "schema_incomplete",
                    "row": row if isinstance(row, dict) else {"value": str(row)[:200]},
                }
            )
            continue
        except VisionFallback:
            violations.append(
                {
                    "index": index,
                    "loss": "parse_fail",
                    "rule": "schema",
                    "reason": "malformed JSON",
                    "row": row if isinstance(row, dict) else {"value": str(row)[:200]},
                }
            )
            continue
        valid.extend(cleaned)
    return valid, violations


def real_device_count(rows: list[dict[str, Any]]) -> int:
    """Devices the model named directly. A row recovered from stuffed text does not count."""

    return sum(1 for row in rows if str(row.get("tag") or "").strip() and not row.get("recovered"))


def should_reread_tile(
    rows: list[dict[str, Any]],
    violations: list[dict[str, Any]],
    image: Path | None,
) -> bool:
    """One extra read only when a content-heavy crop came back with no usable row.

    A bus or a wire number is a usable row. A title strip or a border under
    ``_CONTENT_INK`` is not asked again, even when every row was incomplete.
    """

    if rows:
        return False
    if image is None or not Path(image).is_file():
        return False
    from PIL import Image

    from panelcad.importer.tile import _CONTENT_INK, image_ink_fraction

    try:
        with Image.open(image) as handle:
            ink = image_ink_fraction(handle)
    except OSError:
        return False
    if ink < _CONTENT_INK:
        return False
    # Nothing usable. Violations that dominate, or an empty content crop.
    return True


def reply_row_count(data: Any) -> int:
    """How many detection rows a tile reply held, before a field was dropped."""

    flat = coerce_detection_body(data) if isinstance(data, dict) else {}
    rows = flat.get("detections") if isinstance(flat, dict) else None
    return len(rows) if isinstance(rows, list) else 0


def retry_log(meta: dict[str, Any]) -> dict[str, Any]:
    """Retry count and status. A content reread also records both reply sizes."""

    record = {
        "count": int(meta.get("retry_count") or 0),
        "status": str(meta.get("retry_status") or "none"),
    }
    if "first_rows" in meta:
        record["first_rows"] = int(meta.get("first_rows") or 0)
    if "second_rows" in meta:
        record["second_rows"] = int(meta.get("second_rows") or 0)
    return record


def reread_is_better(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> bool:
    """Keep the second reply only when it names more devices that were not recovered."""

    return real_device_count(second) > real_device_count(first)


def _blank_detection(row: dict[str, Any]) -> bool:
    """An empty row is not a detection. Models emit them when the token budget is tight."""

    texts = []
    for key in ("tag", "wire_number", "bus", "from_tag", "to_tag", "from_pin", "to_pin", "symbol_class"):
        value = row.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return False
        texts.append(value.strip())
    return not any(texts)


def _listed_wire_numbers(raw: Any) -> list[str]:
    """One printed number, or several sharing a row (``12T2/12T3``).

    A string that is not a wire label stays one token so the review can see it.
    """

    if isinstance(raw, list):
        pieces = [str(item).strip() for item in raw]
        text = ""
    else:
        text = str(raw or "").strip()
        pieces = re.split(r"[\s,;/]+", text) if text else []
    labels: list[str] = []
    for piece in pieces:
        token = piece.strip().upper()
        if token and (is_wire_label(token) or BUS_WIRE_RE.match(token)):
            labels.append(token)
    if labels:
        return labels
    if text:
        return [text]
    return []


def suggest_plotter_od(tag: str) -> str:
    """Suggest one O/D swap when the printed tag is not a device family and the swap is.

    10DL-G stays 10DL-G. The review item carries suggested_tag 10OL-G (overload).
    """

    text = str(tag or "").strip().upper()
    if not text:
        return ""
    if tag_prefix(text) in DEVICE_FAMILIES:
        return ""
    chars = list(text)
    for index, char in enumerate(chars):
        if char not in {"O", "D"}:
            continue
        trial = chars.copy()
        trial[index] = "O" if char == "D" else "D"
        candidate = "".join(trial)
        if candidate == text or tag_prefix(candidate) not in DEVICE_FAMILIES:
            continue
        if TAG_RE.match(candidate) is None and not is_device_tag(candidate):
            continue
        return candidate
    return ""


def apply_field_placement(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag a wire number stored as a tag, and move that value into wire_number.

    A device tag stored as a wire number is flagged and left where the model put it,
    so a junk string that merely looks like a tag is not inserted as a device.
    ``source_tag`` keeps the model's fields so the funnel can match the raw row.
    """

    placed: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        tag = normalize_device_tag(str(item.get("tag") or ""))
        if tag:
            item["tag"] = tag
        number = str(item.get("wire_number") or "").strip()
        bus = str(item.get("bus") or "").strip()
        if tag and is_nema_line_ref(tag):
            item["line_ref"] = tag
            item["tag"] = ""
            item["needs_review"] = True
            tag = ""
        if tag and not number and not bus and is_wire_label(tag):
            item["source_tag"] = tag
            item["source_wire_number"] = number
            label = tag.upper()
            if BUS_WIRE_RE.match(label):
                item["bus"] = label
            item["wire_number"] = label
            item["tag"] = ""
            item["wrong_field"] = "tag"
            item["needs_review"] = True
        elif number and not tag and is_device_tag(number) and tag_prefix(number) in DEVICE_FAMILIES:
            item["wrong_field"] = "wire_number"
            item["needs_review"] = True
        placed.append(item)
    return placed


def rescale_bbox(
    box: list[float],
    *,
    tile_w: float,
    tile_h: float,
    tile_px_w: float = 0,
    tile_px_h: float = 0,
    sheet_w: float = 0,
    sheet_h: float = 0,
    sheet_px_w: float = 0,
    sheet_px_h: float = 0,
    origin_x: float = 0,
    origin_y: float = 0,
) -> tuple[list[float], str]:
    """Return crop millimetres and the unit that was detected.

    The prompt asks for crop millimetres. Models also return 0..1, tile pixels,
    page pixels, or page millimetres. A box whose numbers already sit on this
    crop, including a symbol that hangs past the edge, stays in millimetres.
    Pixel units are used only when the numbers are too large for millimetres
    and they lie on the bitmap.
    """

    x, y, width, height = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))

    def inside(limit_w: float, limit_h: float, slack: float = 1.08) -> bool:
        return (
            limit_w > 0
            and limit_h > 0
            and width > 0
            and height > 0
            and x >= -1
            and y >= -1
            and x + width <= limit_w * slack
            and y + height <= limit_h * slack
        )

    def scaled(values: list[float]) -> list[float]:
        return [round(float(value), 2) for value in values]

    # 0..1 on both axes is a normalised box. A 0.5 mm mark is not how these crops are read.
    if 0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1 and x + width <= 1.05 and y + height <= 1.05:
        return scaled([x * tile_w, y * tile_h, width * tile_w, height * tile_h]), "normalized"
    # Qwen vision models report xyxy in 0..1000. A wire label is a small span whose
    # far corner is still inside that frame, so the third number is not a width.
    span_w = width - x
    span_h = height - y
    if (
        0 <= x <= 1000
        and 0 <= y <= 1000
        and 0 <= width <= 1000
        and 0 <= height <= 1000
        and span_w > 1
        and span_h > 1
        and span_w <= 400
        and span_h <= 400
        and max(width, height) >= 500
        and width > x
        and height > y
        and tile_w > 0
        and tile_h > 0
    ):
        return scaled(
            [x / 1000.0 * tile_w, y / 1000.0 * tile_h, span_w / 1000.0 * tile_w, span_h / 1000.0 * tile_h]
        ), "qwen_1000"
    # Crop millimetres, including a symbol whose far edge crosses the tile.
    if tile_w > 0 and tile_h > 0 and width > 0 and height > 0 and x >= -1 and y >= -1:
        if x <= tile_w * 1.25 and y <= tile_h * 1.25 and width <= tile_w * 1.25 and height <= tile_h * 1.25:
            return scaled([x, y, width, height]), "crop_mm"
    # Page millimetres: a symbol-sized box whose origin is outside this crop.
    symbol_w = max(tile_w, 40.0) * 1.5
    symbol_h = max(tile_h, 40.0) * 1.5
    origin_outside = tile_w > 0 and tile_h > 0 and (x > tile_w * 1.05 or y > tile_h * 1.05)
    if origin_outside and 0 < width <= symbol_w and 0 < height <= symbol_h and inside(sheet_w, sheet_h, 1.02):
        return scaled([x - origin_x, y - origin_y, width, height]), "page_mm"
    if tile_px_w > 1 and tile_px_h > 1 and inside(tile_px_w, tile_px_h) and tile_w > 0 and tile_h > 0:
        scale_x = tile_w / tile_px_w
        scale_y = tile_h / tile_px_h
        return scaled([x * scale_x, y * scale_y, width * scale_x, height * scale_y]), "tile_px"
    if sheet_px_w > 1 and sheet_px_h > 1 and sheet_w > 0 and sheet_h > 0 and inside(sheet_px_w, sheet_px_h, 1.02):
        if not inside(sheet_w, sheet_h, 1.02):
            scale_x = sheet_w / sheet_px_w
            scale_y = sheet_h / sheet_px_h
            return scaled(
                [x * scale_x - origin_x, y * scale_y - origin_y, width * scale_x, height * scale_y]
            ), "page_px"
    if sheet_w > 0 and sheet_h > 0 and inside(sheet_w, sheet_h, 1.02):
        return scaled([x - origin_x, y - origin_y, width, height]), "page_mm"
    return scaled([x, y, width, height]), "unknown"


def prepare_detection_rows(
    rows: list[dict[str, Any]],
    work: dict[str, Any],
    image: Path | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Move misplaced wire numbers and rescale boxes. The raw reply is not edited."""

    page = work.get("page_mm") if isinstance(work.get("page_mm"), dict) else {}
    sheet = work.get("sheet_mm") if isinstance(work.get("sheet_mm"), dict) else {}
    sheet_px = work.get("sheet_px") if isinstance(work.get("sheet_px"), dict) else {}
    tile_w = float(page.get("w") or 0)
    tile_h = float(page.get("h") or 0)
    origin_x = float(page.get("x") or 0)
    origin_y = float(page.get("y") or 0)
    px_w, px_h = _image_size(image)
    placed = apply_field_placement(rows)
    units: list[str] = []
    for row in placed:
        box = row.get("bbox")
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            units.append("unknown")
            continue
        row["source_bbox"] = [round(float(value), 2) for value in box]
        source = row["source_bbox"]
        scaled, unit = rescale_bbox(
            [float(value) for value in box],
            tile_w=tile_w,
            tile_h=tile_h,
            tile_px_w=px_w,
            tile_px_h=px_h,
            sheet_w=float(sheet.get("w") or 0),
            sheet_h=float(sheet.get("h") or 0),
            sheet_px_w=float(sheet_px.get("w") or 0),
            sheet_px_h=float(sheet_px.get("h") or 0),
            origin_x=origin_x,
            origin_y=origin_y,
        )
        if _echoes_crop(source, scaled, origin_x, origin_y, tile_w, tile_h):
            unit = "unlocalised"
            row["unlocalised"] = True
            row["needs_review"] = True
        row["bbox"] = scaled
        row["bbox_unit"] = unit
        units.append(unit)
    return placed, units


def _echoes_crop(
    source: list[float],
    scaled: list[float],
    origin_x: float,
    origin_y: float,
    tile_w: float,
    tile_h: float,
) -> bool:
    """A bbox that repeats the crop rectangle was copied from the prompt, not read."""

    if tile_w <= 1 or tile_h <= 1:
        return False
    crop = [origin_x, origin_y, tile_w, tile_h]
    local = [0.0, 0.0, tile_w, tile_h]
    return _same_box(source, crop) or _same_box(source, local) or _same_box(scaled, local)


def _same_box(left: list[float], right: list[float], tol: float = 1.5) -> bool:
    return all(abs(float(left[index]) - float(right[index])) <= tol for index in range(4))


def _image_size(image: Path | None) -> tuple[float, float]:
    if image is None or not image.is_file():
        return 0.0, 0.0
    try:
        from PIL import Image

        with Image.open(image) as handle:
            return float(handle.size[0]), float(handle.size[1])
    except Exception:  # noqa: BLE001 — a missing size leaves the box unchanged
        return 0.0, 0.0


def ollama_host(base_url: str) -> str:
    """Native Ollama root. The preset URL ends in /v1 for the OpenAI-compatible probe."""

    root = str(base_url or "http://127.0.0.1:11434").strip().rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root or "http://127.0.0.1:11434"


def ollama_has_vision(body: Any) -> bool:
    """True when /api/show reports a vision capability or a vision projector."""

    if not isinstance(body, dict):
        return False
    caps = body.get("capabilities") or []
    if isinstance(caps, list) and any(str(item).lower() == "vision" for item in caps):
        return True
    info = body.get("model_info")
    if isinstance(info, dict):
        for key in info:
            low = str(key).lower()
            if any(token in low for token in ("vision", "clip", "projector", "mmproj")):
                return True
    return False


def list_ollama_vision_models(base_url: str, transport: Transport | None = None, timeout: float = 2.0) -> list[str]:
    """Model tags from /api/tags that /api/show says can read an image. Failures return []."""

    host = ollama_host(base_url)
    call = transport or http_transport
    try:
        tags = call("GET", host + "/api/tags", None, {"Accept": "application/json"}, timeout)
    except Exception:  # noqa: BLE001 — the picker stays on the typed model
        return []
    rows = tags.get("models") if isinstance(tags, dict) else None
    if not isinstance(rows, list):
        return []
    found: list[str] = []
    for row in rows:
        name = ""
        if isinstance(row, dict):
            name = str(row.get("name") or row.get("model") or "").strip()
        elif isinstance(row, str):
            name = row.strip()
        if not name:
            continue
        try:
            shown = call(
                "POST",
                host + "/api/show",
                {"model": name},
                {"Content-Type": "application/json", "Accept": "application/json"},
                timeout,
            )
        except Exception:  # noqa: BLE001
            continue
        if ollama_has_vision(shown):
            found.append(name)
    return found


def detections_to_facts(
    detections: list[dict[str, Any]],
    tile: dict[str, Any],
    catalog: Any,
    *,
    standard: str = "",
) -> dict[str, Any]:
    """Page-millimetre devices and wires. Every row is model-assisted and still unreviewed."""

    origin_x = float(tile.get("x") or 0)
    origin_y = float(tile.get("y") or 0)
    devices: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    xrefs: list[dict[str, Any]] = []
    for row in detections:
        x0, y0, width, height = row["bbox"]
        page_x = round(origin_x + x0, 2)
        page_y = round(origin_y + y0, 2)
        crop_box = [
            round(float(x0), 2),
            round(float(y0), 2),
            round(float(width), 2),
            round(float(height), 2),
        ]
        crop_span = [round(float(tile.get("w") or 0), 2), round(float(tile.get("h") or 0), 2)]
        confidence = float(row["confidence"])
        if row.get("unlocalised"):
            confidence = min(confidence, 0.4)
        if row["tag"]:
            resolved = resolve_symbol(
                catalog,
                tag=row["tag"],
                type_text=row["symbol_class"],
                standard=standard,
            )
            guess = bool(resolved.get("guess"))
            # A prefix with no symbol is 0.48. A supported tag the model read,
            # such as 10M-G or 12G at 0.93, keeps that confidence. Several
            # catalog rows of the same family are not a low-confidence read.
            if guess and not (supported_tag_family(str(row["tag"])) and resolved.get("library_id")):
                confidence = min(confidence, PREFIX_CONFIDENCE)
            device = {
                "tag": row["tag"],
                "type": row["symbol_class"] or resolved.get("symbol") or "",
                "symbol": row["symbol_class"] or resolved.get("symbol") or "",
                "library_id": "" if row.get("tag_unparsed") else (resolved.get("library_id") or ""),
                "guess": guess,
                "x": page_x,
                "y": page_y,
                "w": round(width, 2),
                "h": round(height, 2),
                "confidence": round(confidence, 2),
                "unreadable": False,
                "source": "model",
                "model_assisted": True,
                "crop_box": crop_box,
                "crop_span": crop_span,
            }
            if row.get("tag_unparsed"):
                device["tag_unparsed"] = True
                device["needs_review"] = True
                device["guess"] = True
            elif row.get("needs_review"):
                device["needs_review"] = True
            if row.get("suggested_tag"):
                device["suggested_tag"] = row["suggested_tag"]
                device["suggestion"] = str(row.get("suggestion") or "plotter O/D")
                device["needs_review"] = True
            if row.get("wrong_field"):
                device["wrong_field"] = row["wrong_field"]
                device["needs_review"] = True
            if row.get("bbox_unit"):
                device["bbox_unit"] = row["bbox_unit"]
            if row.get("unlocalised"):
                device["unlocalised"] = True
                device["needs_review"] = True
            if row.get("recovered"):
                device["recovered"] = True
                device["needs_review"] = True
            devices.append(device)
        from_pin = str(row.get("from_pin") or "")
        to_pin = str(row.get("to_pin") or "")
        bus = str(row.get("bus") or "").strip()
        numbers = _listed_wire_numbers(row.get("wire_number"))
        if not numbers and bus:
            numbers = [bus]
        # A row can name a device and the wires beside it. Keep both.
        # Both pins are a conductor. The missing tag is the next device on the rung.
        conductor = bool(from_pin and to_pin)
        if numbers or conductor:
            labels = numbers or [""]
            pinned = bool(from_pin and to_pin)
            for label in labels:
                wires.append(
                    {
                        "number": label,
                        "x": round(page_x + width / 2.0, 2),
                        "y": round(page_y + height / 2.0, 2),
                        "from_tag": str(row.get("from_tag") or ""),
                        "to_tag": str(row.get("to_tag") or ""),
                        "from_pin": from_pin,
                        "to_pin": to_pin,
                        "ambiguous": False,
                        "stub": (not pinned) or bool(row.get("unlocalised")),
                        "attached": pinned and not row.get("unlocalised"),
                        "unlocalised": bool(row.get("unlocalised")),
                        "needs_review": True,
                        "confidence": round(confidence, 2),
                        "unreadable": False,
                        "source": "model",
                        "model_assisted": True,
                        "bus": bus if (not label or label == bus or BUS_WIRE_RE.match(label.upper())) else "",
                        "wrong_field": str(row.get("wrong_field") or ""),
                        "bbox_unit": str(row.get("bbox_unit") or ""),
                        "crop_box": crop_box,
                        "crop_span": crop_span,
                        "tile_x": round(origin_x, 2),
                        "tile_y": round(origin_y, 2),
                        "tile_w": round(float(tile.get("w") or 0), 2),
                        "tile_h": round(float(tile.get("h") or 0), 2),
                    }
                )
        elif row["tag"] and row.get("needs_review"):
            if devices:
                devices[-1]["needs_review"] = True
        line_ref = str(row.get("line_ref") or "").strip()
        if line_ref:
            xrefs.append(
                {
                    "text": line_ref,
                    "x": page_x,
                    "y": page_y,
                    "kind": "nema_line",
                    "needs_review": True,
                    "confidence": round(confidence, 2),
                    "source": "model",
                }
            )
    return {"devices": devices, "wires": wires, "xrefs": xrefs, "texts": []}


# A bare class letter or a generic word is not a printed tag. X1–X5 and Y1–Y2
# match tag syntax, so they are kept and only lowered when no terminal is near.
_TERMINAL_TAG = re.compile(r"^X[1-5]$|^Y[1-2]$")
_POWER_TAGS = {"L1", "L2", "L3", "N", "PE"}
_TERMINAL_NEAR_MM = 30.0
_TERMINAL_FLAG_CONFIDENCE = 0.55


def filter_vision_detections(
    detections: list[dict[str, Any]],
    catalog: Any,
    tile: dict[str, Any],
    pixel_devices: list[dict[str, Any]] | None = None,
    *,
    standard: str = "",
    tag_re: re.Pattern[str] | None = None,
    unmatched_is_reject: bool = False,
) -> list[dict[str, Any]]:
    """Drop tags that are not tag syntax unless one library symbol supports them.

    Terminal-like X1–X5 and Y1–Y2 stay, at low confidence, unless a terminal
    symbol is on the detection or within 30 mm.

    A tag that will not be placed gets ``reject_rule`` (TAG_RE, XREF_RE, …).
    ``unmatched_is_reject`` forces a miss against ``tag_re`` to TAG_RE before a
    symbol guess can keep it. That is how a letter-first rule reports 10M-G.
    """

    pattern = tag_re or TAG_RE
    nearby = list(pixel_devices or [])
    kept: list[dict[str, Any]] = []
    for row in detections:
        tag = normalize_device_tag(str(row.get("tag") or ""))
        item = dict(row)
        if tag:
            item["tag"] = tag
            hint = suggest_plotter_od(tag)
            if hint:
                item["suggested_tag"] = hint
                item["suggestion"] = "plotter O/D"
                item["needs_review"] = True
        if tag and is_nema_line_ref(tag):
            item["line_ref"] = tag
            item["tag"] = ""
            item["needs_review"] = True
            kept.append(item)
            continue
        if not tag:
            kept.append(item)
            continue
        if is_cross_ref(tag):
            item["reject_rule"] = "XREF_RE"
            _hold_unparsed(item)
            kept.append(item)
            continue
        if (
            unmatched_is_reject
            and pattern.match(tag) is None
            and tag not in _POWER_TAGS
            and tag not in CONVENTIONAL_WIRES
        ):
            item["reject_rule"] = "TAG_RE"
            _hold_unparsed(item)
            kept.append(item)
            continue
        if _TERMINAL_TAG.match(tag):
            if not _terminal_near(item, detections, nearby, catalog, tile, standard):
                item["confidence"] = min(float(item["confidence"]), _TERMINAL_FLAG_CONFIDENCE)
                item["needs_review"] = True
            kept.append(item)
            continue
        live = pattern is TAG_RE
        matched = pattern.match(tag) is not None or (live and is_device_tag(tag))
        if matched and tag not in CONVENTIONAL_WIRES:
            kept.append(item)
            continue
        if tag in _POWER_TAGS:
            kept.append(item)
            continue
        if _symbol_supports(tag, str(item.get("symbol_class") or ""), catalog, standard):
            kept.append(item)
            continue
        # A tag the model saw and this importer will not place still becomes a review row.
        item["reject_rule"] = "TAG_RE" if pattern.match(tag) is None else "tag_unparsed"
        _hold_unparsed(item)
        kept.append(item)
    return kept


def _hold_unparsed(item: dict[str, Any]) -> None:
    item["needs_review"] = True
    item["tag_unparsed"] = True
    item["confidence"] = min(float(item.get("confidence") or 0.4), 0.4)


def _symbol_supports(tag: str, symbol_class: str, catalog: Any, standard: str) -> bool:
    kind = symbol_class.strip()
    if not kind or catalog is None:
        return False
    resolved = resolve_symbol(catalog, tag=tag, type_text=kind, standard=standard)
    if resolved.get("guess") or not resolved.get("library_id"):
        return False
    got = str(resolved.get("symbol") or "")
    wanted = kind.lower().replace(" ", "_")
    if wanted in {"vfd", "drive"}:
        return got == "drive"
    if wanted in {"power_supply", "psu"}:
        return got == "power_supply"
    return got == wanted


def _terminal_near(
    row: dict[str, Any],
    detections: list[dict[str, Any]],
    pixel_devices: list[dict[str, Any]],
    catalog: Any,
    tile: dict[str, Any],
    standard: str,
) -> bool:
    kind = str(row.get("symbol_class") or "").strip().lower()
    if kind == "terminal" and _symbol_supports(str(row.get("tag") or ""), "terminal", catalog, standard):
        return True
    cx, cy = _detection_center(row, tile)
    for device in pixel_devices:
        if not _is_terminal(device):
            continue
        if _near(cx, cy, device.get("x"), device.get("y")):
            return True
    for other in detections:
        if other is row:
            continue
        if str(other.get("symbol_class") or "").strip().lower() != "terminal":
            continue
        if not _symbol_supports(str(other.get("tag") or "X1"), "terminal", catalog, standard):
            continue
        ox, oy = _detection_center(other, tile)
        if math.hypot(ox - cx, oy - cy) <= _TERMINAL_NEAR_MM:
            return True
    return False


def _is_terminal(device: dict[str, Any]) -> bool:
    symbol = str(device.get("symbol") or device.get("type") or "").lower()
    library = str(device.get("library_id") or "").lower()
    return symbol == "terminal" or "terminal" in library


def _detection_center(row: dict[str, Any], tile: dict[str, Any]) -> tuple[float, float]:
    x0, y0, width, height = row["bbox"]
    return float(tile.get("x") or 0) + float(x0) + float(width) / 2.0, float(tile.get("y") or 0) + float(y0) + float(height) / 2.0


def _near(x: float, y: float, other_x: Any, other_y: Any) -> bool:
    try:
        dx = float(other_x) - x
        dy = float(other_y) - y
    except (TypeError, ValueError):
        return False
    return dx * dx + dy * dy <= _TERMINAL_NEAR_MM * _TERMINAL_NEAR_MM


Transport = Callable[[str, str, dict[str, Any] | None, dict[str, str], float], Any]


class OpenAICompatibleVision:
    """Chat completions with an image. The key is read from the environment on each call."""

    name = "chat"

    def __init__(self, settings: VisionSettings, transport: Transport | None = None) -> None:
        self.settings = settings.resolved()
        self._transport = transport or http_transport
        self._lock = threading.Lock()
        self.cancelled = False
        self.cost_guard_continued = False
        self.spent_usd = 0.0
        self.estimated_usd = 0.0
        self.input_tokens = 0
        self._priced_cost = 0.0
        self._priced_calls = 0
        self.page_started = 0.0
        self._page_clocks: dict[str, float] = {}
        self.calls = 0

    def available(self) -> bool:
        return self.unavailable_reason() == ""

    def unavailable_reason(self) -> str:
        settings = self.settings
        if settings.preset == PRESET_NONE:
            return "vision backend is none"
        if not settings.base_url:
            return "set a base URL"
        if not settings.model:
            return "set a model name"
        if settings.preset != PRESET_OLLAMA and settings.api_key_env and not settings.api_key():
            return f"{settings.api_key_env} is not set"
        if settings.preset != PRESET_OLLAMA and not settings.api_key_env:
            return "set the API key environment variable"
        return ""

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True

    def begin_job(self) -> None:
        """Reset the shared spend budget. Each page starts its own clock later."""

        with self._lock:
            self.spent_usd = 0.0
            self.estimated_usd = 0.0
            self.input_tokens = 0
            self._priced_cost = 0.0
            self._priced_calls = 0
            self.cancelled = False
            self.cost_guard_continued = False
            self.page_started = time.monotonic()
            self._page_clocks = {}
            self.calls = 0

    def allow_cost_guard(self) -> None:
        """Keep reading after the caller was warned and chose to continue."""

        with self._lock:
            self.cost_guard_continued = True

    def begin_page(self, page_id: str = "") -> None:
        """Start this page's clock. Spend and cancel stay as they are."""

        now = time.monotonic()
        with self._lock:
            self.page_started = now
            self._page_clocks[str(page_id or "")] = now

    def job_stop(self) -> str:
        """Cancel and the cost cap. The page clock is not a reason to stop other pages."""

        with self._lock:
            cancelled = self.cancelled
            spent = self.spent_usd
            continued = self.cost_guard_continued
        if cancelled:
            return "cancelled"
        if spent > self.settings.max_usd and not continued:
            return "page cost guard"
        return ""

    def over_budget(self, page_id: str | None = None) -> str:
        stop = self.job_stop()
        if stop:
            return stop
        with self._lock:
            if page_id:
                started = self._page_clocks.get(str(page_id), 0.0)
            else:
                started = self.page_started
        if started and (time.monotonic() - started) > self.settings.page_timeout_s:
            return "page timeout"
        return ""

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        meta: dict[str, Any] = {
            "timeout": False,
            "retry_count": 0,
            "retry_status": "none",
            "error": "",
            "body": None,
            "parsed": None,
            "violations": [],
        }
        prompt = ""
        image = Path(str(work.get("image") or ""))
        try:
            reason = self.over_budget(str(work.get("page_id") or "") or None) or self.unavailable_reason()
            if reason:
                meta["error"] = reason
                raise VisionFallback(reason)
            if not image.is_file():
                meta["error"] = "crop missing"
                raise VisionFallback("crop missing")
            import base64

            encoded = base64.standard_b64encode(image.read_bytes()).decode("ascii")
            suffix = image.suffix.lower().lstrip(".")
            media = "image/jpeg" if suffix in {"jpg", "jpeg"} else "image/png"
            page_mm = work.get("page_mm") or {}
            prompt = (
                TILE_PROMPT
                + "\n\nThis crop covers page millimetres "
                + json.dumps({key: page_mm.get(key) for key in ("x", "y", "w", "h")})
                + ". Put bbox in crop millimetres."
            )

            def _once(text: str) -> tuple[Any, dict[str, Any]]:
                body = self._complete(text, encoded, media)
                meta["body"] = _with_usage(body)
                return body, _reply_object(body)

            def _with_timeout(text: str) -> tuple[Any, dict[str, Any]]:
                try:
                    return _once(text)
                except VisionFallback as exc:
                    if exc.reason != "request timed out":
                        raise
                    meta["timeout"] = True
                    meta["retry_count"] = int(meta.get("retry_count") or 0) + 1
                    meta["retry_status"] = "retried"
                    try:
                        got = _once(text)
                    except VisionFallback as again:
                        if again.reason == "request timed out":
                            meta["timeout"] = True
                            meta["retry_status"] = "timeout"
                        raise
                    meta["retry_status"] = "ok"
                    return got

            try:
                body, data = _with_timeout(prompt)
            except VisionFallback as exc:
                if exc.reason != "malformed JSON":
                    meta["error"] = exc.reason
                    raise
                meta["truncated"] = True
                meta["truncated_reply"] = meta.get("body")
                meta["retry_count"] = int(meta.get("retry_count") or 0) + 1
                meta["retry_status"] = "retried"
                short = (
                    SHORT_TILE_PROMPT
                    + "\n\nThis crop covers page millimetres "
                    + json.dumps({key: page_mm.get(key) for key in ("x", "y", "w", "h")})
                    + ". Put bbox in crop millimetres."
                )
                try:
                    body, data = _with_timeout(short)
                except VisionFallback as again:
                    if again.reason != "malformed JSON":
                        meta["error"] = again.reason
                        raise
                    meta["error"] = "malformed JSON"
                    meta["violations"] = [_parse_violation()]
                    raise VisionFallback("malformed JSON") from again
                meta["retry_status"] = "ok"
                prompt = short
            meta["body"] = _with_usage(body)
            self._account(meta["body"])
            meta["parsed"] = data
            try:
                valid, violations = partition_detections(data)
            except VisionFallback as exc:
                meta["error"] = "malformed JSON"
                meta["violations"] = [
                    {"index": 0, "loss": "parse_fail", "rule": "malformed_json", "reason": "malformed JSON", "row": None}
                ]
                raise VisionFallback("malformed JSON") from exc
            if should_reread_tile(valid, violations, image):
                # One extra read. A weaker reply is discarded. This does not loop.
                # Status stays "retried". A later "ok" would hide the second call.
                meta["retry_count"] = int(meta.get("retry_count") or 0) + 1
                meta["retry_status"] = "retried"
                meta["first_rows"] = reply_row_count(data)
                short = (
                    SHORT_TILE_PROMPT
                    + "\n\nThis crop covers page millimetres "
                    + json.dumps({key: page_mm.get(key) for key in ("x", "y", "w", "h")})
                    + ". Put bbox in crop millimetres."
                )
                try:
                    body2, data2 = _once(short)
                except VisionFallback:
                    body2, data2 = None, None
                meta["second_rows"] = reply_row_count(data2)
                if isinstance(data2, dict):
                    self._account(_with_usage(body2))
                    try:
                        valid2, violations2 = partition_detections(data2)
                    except VisionFallback:
                        valid2 = None
                    if valid2 is not None and reread_is_better(valid, valid2):
                        body, data = body2, data2
                        valid, violations = valid2, violations2
                        prompt = short
                        meta["body"] = _with_usage(body)
                        meta["parsed"] = data
            prepared, units = prepare_detection_rows(valid, work, image)
            meta["violations"] = violations
            meta["bbox_units"] = units
            if violations and not prepared:
                meta["error"] = ""
            flat = coerce_detection_body(data) if isinstance(data, dict) else {}
            reported = list(flat.get("detections") or []) if isinstance(flat, dict) else []
            return {
                "detections": prepared,
                "_reported": reported,
                "_schema_violations": violations,
            }
        finally:
            self._emit_trace(work, prompt, image, meta, time.perf_counter() - started)

    def _complete(self, prompt: str, encoded: str, media: str) -> Any:
        if self.settings.preset == PRESET_OLLAMA:
            return self._ollama_chat(prompt, encoded)
        return self._openai_chat(prompt, encoded, media)

    def _ollama_chat(self, prompt: str, encoded: str) -> Any:
        from panelcad.sharpen.loop import OllamaClient

        client = OllamaClient(
            model=self.settings.model,
            host=ollama_host(self.settings.base_url),
            temperature=0.0,
            num_ctx=int(self.settings.num_ctx or 16384),
            timeout=float(self.settings.timeout_s),
            num_predict=int(self.settings.num_predict or 4096),
            think=bool(self.settings.think),
            response_format=DETECTION_SCHEMA,
            transport=self._transport,
        )
        try:
            return client.chat(
                [
                    {"role": "system", "content": "You extract one drawing crop. Reply with the JSON object only."},
                    {"role": "user", "content": prompt},
                ],
                images=[encoded],
            )
        except VisionHTTPError as exc:
            if exc.status == 400:
                client.response_format = None
                try:
                    return client.chat(
                        [
                            {"role": "system", "content": "You extract one drawing crop. Reply with the JSON object only."},
                            {"role": "user", "content": prompt},
                        ],
                        images=[encoded],
                        response_format=None,
                    )
                except VisionHTTPError as again:
                    raise VisionFallback(f"HTTP {again.status}") from None
            raise VisionFallback(f"HTTP {exc.status}") from None
        except VisionFallback:
            raise
        except TimeoutError:
            self._estimate_miss()
            raise VisionFallback("request timed out") from None
        except RuntimeError as exc:
            text = str(exc)
            if "not reachable" in text or "connection" in text.lower():
                raise VisionFallback("connection failed") from None
            raise VisionFallback("malformed JSON" if "JSON" in text else _public_error(exc, "")) from None

    def _openai_chat(self, prompt: str, encoded: str, media: str) -> Any:
        predict = int(self.settings.num_predict or 4096)
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You extract one drawing crop. Reply with the JSON object only.",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media};base64,{encoded}",
                                "detail": "high",
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
        }
        if self.settings.preset == "grok":
            payload["max_completion_tokens"] = predict
        else:
            payload["max_tokens"] = predict
        if self.settings.reasoning_effort:
            payload["reasoning_effort"] = self.settings.reasoning_effort
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        try:
            return self._post(url, payload)
        except VisionHTTPError as exc:
            if exc.status == 400 and "response_format" in payload:
                payload.pop("response_format", None)
                try:
                    return self._post(url, payload)
                except VisionHTTPError as again:
                    raise VisionFallback(f"HTTP {again.status}") from None
            raise VisionFallback(f"HTTP {exc.status}") from None

    def _emit_trace(
        self,
        work: dict[str, Any],
        prompt: str,
        image: Path,
        meta: dict[str, Any],
        latency_s: float,
    ) -> None:
        """Record the prompt and the reply. Headers and the API key stay out."""

        input_tokens, output_tokens, cost = usage_numbers(meta.get("body"))
        reply = raw_model_text(meta.get("body"))
        if not reply and isinstance(meta.get("parsed"), dict):
            reply = json.dumps(meta["parsed"])
        first_cut = raw_model_text(meta.get("truncated_reply"))
        emit_vision_trace(
            {
                "prompt_version": PROMPT_VERSION,
                "schema_version": PROMPT_VERSION,
                "model": self.settings.model,
                "backend": backend_label(self.settings.preset),
                "preset": self.settings.preset,
                "latency_s": round(max(0.0, latency_s), 4),
                "tokens": {"input": input_tokens, "output": output_tokens},
                "cost_usd": None if cost is None else round(cost, 6),
                "timeout": bool(meta.get("timeout")),
                "timeout_s": float(self.settings.timeout_s),
                "retry": retry_log(meta),
                "error": str(meta.get("error") or ""),
                "request": {
                    "prompt": prompt,
                    "prompt_version": PROMPT_VERSION,
                    "schema_version": PROMPT_VERSION,
                    "schema": DETECTION_SCHEMA,
                    "model": self.settings.model,
                    "image": str(image) if str(image) else str(work.get("image") or ""),
                    "temperature": 0,
                },
                "reply": reply,
                "truncated_reply": first_cut,
                "schema_violations": list(meta.get("violations") or []),
                "bbox_units": list(meta.get("bbox_units") or []),
                "think": self.settings.think,
                "num_ctx": int(self.settings.num_ctx or 0),
                "truncated": bool(meta.get("truncated")),
                "dpi": work.get("dpi"),
            }
        )

    def test_connection(self) -> tuple[bool, str]:
        """Probe GET {base}/models. The message never includes the key."""

        reason = self.unavailable_reason()
        if reason and self.settings.preset != PRESET_OLLAMA:
            # A missing model still lets us probe the server when the URL and key are set.
            if reason != "set a model name":
                return False, reason
        if not self.settings.base_url:
            return False, "set a base URL"
        if self.settings.preset != PRESET_OLLAMA and self.settings.api_key_env and not self.settings.api_key():
            return False, f"{self.settings.api_key_env} is not set"
        url = self.settings.base_url.rstrip("/") + "/models"
        try:
            body = self._transport("GET", url, None, self._headers(), min(10.0, self.settings.timeout_s))
        except VisionHTTPError as exc:
            return False, f"HTTP {exc.status}"
        except VisionFallback as exc:
            return False, exc.reason
        except Exception:  # noqa: BLE001 — the button shows a reason, never the key
            return False, "connection failed"
        if not isinstance(body, dict):
            return False, "connection failed"
        ids = _model_ids(body)
        model = self.settings.model
        if model and ids and model not in ids:
            return True, f"Connected to {self.settings.base_url}. {model} was not in the model list."
        if model:
            return True, f"Connected to {self.settings.base_url}. {model} is available."
        return True, f"Connected to {self.settings.base_url}."

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        try:
            return self._transport("POST", url, payload, self._headers(), self.settings.timeout_s)
        except VisionHTTPError:
            raise
        except VisionFallback:
            raise
        except TimeoutError:
            self._estimate_miss()
            raise VisionFallback("request timed out") from None
        except Exception as exc:  # noqa: BLE001
            raise VisionFallback(_public_error(exc, self.settings.api_key())) from None

    def _account(self, body: Any) -> None:
        usage = body.get("usage") if isinstance(body, dict) else None
        ticks = usage.get("cost_in_usd_ticks") if isinstance(usage, dict) else None
        tokens = 0
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "input_tokens", "inputTokens"):
                val = usage.get(key)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    tokens = int(val)
                    break
        with self._lock:
            self.calls += 1
            self.input_tokens += tokens
            if isinstance(ticks, (int, float)) and not isinstance(ticks, bool):
                cost = float(ticks) / USD_TICKS
                self.spent_usd += cost
                if cost > 0:
                    self._priced_cost += cost
                    self._priced_calls += 1

    def _estimate_miss(self) -> None:
        """A timed-out call that returned no usage still counts toward the cap."""

        from panelcad.importer.vision_config import ESTIMATED_TILE_USD

        with self._lock:
            self.calls += 1
            if self._priced_calls:
                guess = self._priced_cost / self._priced_calls
            else:
                guess = ESTIMATED_TILE_USD
            self.spent_usd += guess
            self.estimated_usd += guess

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self.settings.api_key()
        if key:
            headers["Authorization"] = "Bearer " + key
        return headers


class VisionHTTPError(Exception):
    def __init__(self, status: int) -> None:
        self.status = int(status)
        super().__init__(f"HTTP {self.status}")


def http_transport(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
) -> Any:
    """OpenAI-compatible HTTP. Failures name the status, not the request headers."""

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310
            raw = reply.read()
    except urllib.error.HTTPError as exc:
        raise VisionHTTPError(int(exc.code)) from None
    except TimeoutError:
        raise VisionFallback("request timed out") from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise VisionFallback("request timed out") from None
        raise VisionFallback("connection failed") from None
    try:
        parsed = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise VisionFallback("malformed JSON") from exc
    return parsed


def _parse_violation() -> dict[str, Any]:
    return {"index": 0, "loss": "parse_fail", "rule": "malformed_json", "reason": "malformed JSON", "row": None}


def _with_usage(body: Any) -> Any:
    """Copy an Ollama body so token fields sit where the OpenAI accounting looks."""

    if not isinstance(body, dict) or isinstance(body.get("usage"), dict):
        return body
    if "prompt_eval_count" not in body and "eval_count" not in body:
        return body
    copied = dict(body)
    copied["usage"] = {
        "prompt_tokens": int(body.get("prompt_eval_count") or 0),
        "completion_tokens": int(body.get("eval_count") or 0),
    }
    return copied


def raw_model_text(body: Any) -> str:
    """The text the model sent. Not the HTTP envelope and not only the parsed object."""

    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return json.dumps(content)
    if isinstance(body.get("choices"), list) and body["choices"] and isinstance(body["choices"][0], dict):
        message = body["choices"][0].get("message") or {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, dict):
                return json.dumps(content)
            if isinstance(content, list):
                texts = []
                for part in content:
                    if isinstance(part, str):
                        texts.append(part)
                    elif isinstance(part, dict) and part.get("text"):
                        texts.append(str(part["text"]))
                return "\n".join(texts)
    raw = body.get("structuredOutput")
    if raw is None:
        raw = body.get("structured_output")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw)
    return ""


def _reply_object(body: Any) -> dict[str, Any]:
    """The JSON object from an OpenAI choice or a native Ollama message."""

    if not isinstance(body, dict):
        raise VisionFallback("malformed JSON")
    if isinstance(body.get("choices"), list):
        content = _choice_content(body)
    else:
        message = body.get("message") if isinstance(body.get("message"), dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, (str, dict)) or (isinstance(content, str) and not content.strip()):
            raise VisionFallback("malformed JSON")
    if isinstance(content, dict):
        return content
    return _parse_json(str(content))


def _choice_content(body: Any) -> Any:
    if not isinstance(body, dict):
        raise VisionFallback("malformed JSON")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise VisionFallback("malformed JSON")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise VisionFallback("malformed JSON")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
        joined = "\n".join(texts).strip()
        if joined:
            return joined
    raise VisionFallback("malformed JSON")


def _model_ids(body: dict[str, Any]) -> list[str]:
    rows = body.get("data") or body.get("models") or []
    found = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                found.append(str(row["id"]))
            elif isinstance(row, str):
                found.append(row)
    return found


def _public_error(exc: Exception, secret: str) -> str:
    text = redact(f"{type(exc).__name__}: {exc}", secret)
    return text[:180]
