"""Pluggable vision for REAL panel photos (not drawings).

Primary job: read nameplates, part labels, terminal numbers, and wire
numbers off a phone photo of a physical panel. Offline by default. Tests
inject a fake backend. When PR #28's ``panelcad.importer.vision`` lands,
``kind='photo'`` extracts plug in here without changing the Session API.

The extract payload matches the print-import photo shape so those helpers
can be reused later: devices / rails / ducts / strips, plus brand and
part_number fields this flow needs.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

PANEL_PHOTO_KIND = "photo"
NO_AI_MESSAGE = (
    "No AI connected, so labels aren't read automatically. "
    "Type the part number from each label."
)
PANEL_PHOTO_INSTRUCTIONS = """
This is a PHONE PHOTO of a physical industrial control panel, not a drawing.
Read only what is printed on hardware: nameplates, part labels, device tags,
terminal numbers, and wire numbers. Do not invent a part number, tag, or size.
If glare, blur, or a wire bundle hides a field, mark it unreadable.
Return JSON: devices[{tag, brand, part_number, label_text, nx, ny, nw, nh,
confidence, unreadable}], rails, ducts, strips[{tag, terminals[{id,nx,ny}],
wire_numbers[{number,nx,ny}]}], texts, unreadable.
nx/ny/nw/nh are fractions of the image (0..1).
""".strip()


class VisionBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    def extract(self, work: dict[str, Any]) -> dict[str, Any]: ...


class StubVision:
    """Offline default. Reads nothing. Does not guess."""

    name = "stub"

    def __init__(self, fallback_reason: str = "") -> None:
        self.fallback_reason = fallback_reason

    def available(self) -> bool:
        return True

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        return empty_photo_extract(unreadable=True)


class FakeVision:
    """Recorded JSON keyed by work id, then image stem. Missing = unreadable."""

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
        return empty_photo_extract(unreadable=True)


class CallableVision:
    """Test / host hook. ``fn(work) -> dict``."""

    name = "callable"

    def __init__(self, fn) -> None:
        self.fn = fn

    def available(self) -> bool:
        return True

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        return self.fn(work)


class OllamaVision:
    """Local Ollama chat with an image. Same default model as the drawing agent."""

    name = "ollama"

    def __init__(self, spec: str | None = None, timeout_s: float = 120.0) -> None:
        self.spec = spec or os.environ.get("PANELCAD_VISION_MODEL") or "gemma3:4b"
        self.base = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
        self.timeout_s = timeout_s
        self._probe: bool | None = None

    def available(self) -> bool:
        if self._probe is None:
            self._probe = _probe_ollama(self.base, self.spec, timeout_s=min(1.5, self.timeout_s))
        return self._probe

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        if not self.available():
            return empty_photo_extract(unreadable=True)
        image = Path(str(work.get("image") or ""))
        if not image.is_file():
            return empty_photo_extract(unreadable=True)
        payload = {
            "model": self.spec,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PANEL_PHOTO_INSTRUCTIONS},
                        {"type": "image_url", "image_url": {"url": _data_url(image)}},
                    ],
                }
            ],
            "temperature": 0,
        }
        req = urllib.request.Request(
            f"{self.base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return empty_photo_extract(unreadable=True)
        text = ""
        for choice in body.get("choices") or []:
            message = choice.get("message") or {}
            text = str(message.get("content") or "")
            if text:
                break
        parsed = _parse_json_object(text)
        if parsed is None:
            return empty_photo_extract(unreadable=True)
        return normalize_photo_extract(parsed)


def empty_photo_extract(*, unreadable: bool = False) -> dict[str, Any]:
    return {
        "devices": [],
        "rails": [],
        "ducts": [],
        "strips": [],
        "texts": [],
        "unreadable": bool(unreadable),
    }


def normalize_photo_extract(raw: dict[str, Any] | None) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    out = empty_photo_extract(unreadable=bool(data.get("unreadable")))
    out["devices"] = [dict(item) for item in data.get("devices") or [] if isinstance(item, dict)]
    out["rails"] = [dict(item) for item in data.get("rails") or [] if isinstance(item, dict)]
    out["ducts"] = [dict(item) for item in data.get("ducts") or [] if isinstance(item, dict)]
    out["strips"] = [dict(item) for item in data.get("strips") or [] if isinstance(item, dict)]
    out["texts"] = [dict(item) for item in data.get("texts") or [] if isinstance(item, dict)]
    return out


def vision_is_offline(backend: Any, note: str = "") -> bool:
    """True when no model is connected. The tech types the label instead."""

    if str(getattr(backend, "name", "") or "") == "stub":
        return True
    low = str(note or getattr(backend, "fallback_reason", "") or "").lower()
    return "unavailable" in low or "offline reader" in low or "no ai" in low


def get_backend(
    name: str | None = None,
    *,
    recordings: dict[str, dict[str, Any]] | None = None,
    fn=None,
) -> VisionBackend:
    """Pick a backend. ``importer`` tries PR #28; missing package falls through."""

    if recordings is not None:
        return FakeVision(recordings)
    if fn is not None:
        return CallableVision(fn)
    token = str(name or os.environ.get("PANELCAD_RECONSTRUCT_VISION") or "auto").strip().lower()
    if token in {"fake", "test"}:
        return FakeVision()
    if token in {"stub", "none", "off"}:
        return StubVision()
    if token in {"callable"} and fn is not None:
        return CallableVision(fn)
    if token in {"importer", "print-import", "print_import"}:
        imported = _importer_backend()
        if imported is not None:
            return imported
        return StubVision()
    if token in {"ollama", "local"}:
        backend = OllamaVision()
        return backend if backend.available() else StubVision("vision unavailable, using offline reader")
    if token == "auto":
        imported = _importer_backend()
        if imported is not None and imported.available():
            return imported
        ollama = OllamaVision()
        if ollama.available():
            return ollama
        return StubVision("vision unavailable, using offline reader")
    return StubVision()


def _importer_backend() -> VisionBackend | None:
    """Reuse #28's vision helpers when that branch is merged. Never required."""

    try:
        from panelcad.importer.vision import ConfiguredVision, FakeVision as ImpFake
    except ImportError:
        return None

    class _ImporterAdapter:
        name = "importer"

        def __init__(self) -> None:
            try:
                self.inner = ConfiguredVision()
            except Exception:  # noqa: BLE001
                self.inner = ImpFake()

        def available(self) -> bool:
            try:
                return bool(self.inner.available())
            except Exception:  # noqa: BLE001
                return False

        def extract(self, work: dict[str, Any]) -> dict[str, Any]:
            payload = dict(work)
            payload.setdefault("kind", PANEL_PHOTO_KIND)
            try:
                raw = self.inner.extract(payload)
            except Exception:  # noqa: BLE001
                return empty_photo_extract(unreadable=True)
            return normalize_photo_extract(raw if isinstance(raw, dict) else None)

    return _ImporterAdapter()


def _probe_ollama(base: str, model: str, *, timeout_s: float) -> bool:
    req = urllib.request.Request(f"{base}/models", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return False
    names = []
    for item in body.get("data") or []:
        names.append(str(item.get("id") or ""))
    return not names or model in names or any(model in name for name in names)


def _data_url(path: Path) -> str:
    import base64

    suffix = path.suffix.lower().lstrip(".") or "png"
    mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    raw = path.read_bytes()
    return f"data:image/{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _parse_json_object(text: str) -> dict[str, Any] | None:
    blob = (text or "").strip()
    if not blob:
        return None
    if blob.startswith("```"):
        blob = blob.strip("`")
        if blob.lower().startswith("json"):
            blob = blob[4:]
        blob = blob.strip()
    start = blob.find("{")
    end = blob.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(blob[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
