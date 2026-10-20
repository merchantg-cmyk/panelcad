"""Plain-language next step from the user's own Settings > AI account.

PanelCAD does not ship a key. The provider, endpoint, model, and key are the
ones saved under Settings > AI (or the PANELCAD_AI_* overrides of that page).
The model is asked to stay inside the guide payload. A reply that names a
missing wire or terminal, or that suggests a prohibited action, is discarded.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from panelcad.ai.config import configuration_reason
from panelcad.core.models import Project
from panelcad.core.troubleshoot import citation_errors, prose_violations
from panelcad.library.catalog import Catalog
from panelcad.version import __version__

log = logging.getLogger("panelcad.ai")

Transport = Callable[[urllib.request.Request], Any]

DROPPED = (
    "That reply was set aside. It did not stay on the drawing, or it suggested "
    "a prohibited action. Use the checklist. Leave safety devices, overloads, "
    "and PLC points in the circuit."
)

_SYSTEM = (
    "You explain the next PanelCAD troubleshooting check to a new technician. "
    "Use only facts in the user JSON. Do not add wire numbers, terminals, "
    "voltages, or PLC addresses. If expected.needs_info is true, say the "
    "drawing does not state the value. Do not tell anyone to jumper, bypass, "
    "force, defeat, or override a device, an E-stop, a safety relay, a guard, "
    "an interlock, an overload, or a PLC point. A safety device that is open "
    "means: find why the safety circuit is open and reset it per the machine's "
    "procedure. Write at most six short sentences."
)


class ExplainCall:
    """One explain request the UI thread can cancel."""

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._lock = threading.Lock()
        self._close: Callable[[], None] | None = None

    def cancel(self) -> None:
        self._flag.set()
        with self._lock:
            close = self._close
        if close is not None:
            try:
                close()
            except Exception:
                pass

    def cancelled(self) -> bool:
        return self._flag.is_set()

    def attach(self, close: Callable[[], None]) -> None:
        with self._lock:
            self._close = close
        if self._flag.is_set():
            try:
                close()
            except Exception:
                pass

    def detach(self) -> None:
        with self._lock:
            self._close = None


class _ExplainCancelled(Exception):
    """The user cancelled before a reply was accepted."""


def explain_reason() -> str:
    """Why Explain is off. Empty when Settings > AI can be called."""

    raw = configuration_reason()
    if not raw:
        return ""
    if "turned off" in raw.casefold():
        return (
            "Explain is off: turn on AI in Settings > AI. "
            "The account and key there are yours. PanelCAD does not include one."
        )
    return (
        "Explain is off: connect your own AI account in Settings > AI. "
        "PanelCAD does not include a key."
    )


def explain_guide(
    project: Project,
    catalog: Catalog,
    payload: dict[str, Any],
    *,
    transport: Transport | None = None,
    timeout: float = 45.0,
    cancel: ExplainCall | None = None,
) -> dict[str, Any]:
    """Ask the configured account to explain the current guide step."""

    reason = explain_reason()
    if reason:
        return {"ok": False, "text": "", "reason": reason}
    if cancel is not None and cancel.cancelled():
        return {"ok": False, "text": "", "reason": "", "cancelled": True}
    from panelcad.ai.config import load_ai_lookup

    config = load_ai_lookup()
    prompt = _prompt(payload)
    try:
        text = _complete(config, prompt, transport=transport, timeout=timeout, cancel=cancel)
    except _ExplainCancelled:
        return {"ok": False, "text": "", "reason": "", "cancelled": True}
    except Exception as exc:  # noqa: BLE001 — never log the key
        log.warning("troubleshoot explain failed: %s", type(exc).__name__)
        return {
            "ok": False,
            "text": "",
            "reason": "The AI account did not answer. Use the checklist.",
        }
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return {
            "ok": False,
            "text": "",
            "reason": "The AI account returned no explanation. Use the checklist.",
        }
    wrapped = {"message": cleaned}
    if prose_violations(wrapped) or citation_errors(project, wrapped, catalog):
        return {"ok": False, "text": "", "reason": DROPPED, "discarded": True}
    return {"ok": True, "text": cleaned, "reason": ""}


def _prompt(payload: dict[str, Any]) -> str:
    safety = payload.get("safety") or {}
    current = payload.get("next_check") or {}
    expected = current.get("expected") or {}
    slim = {
        "safety": {
            "loto": safety.get("loto") or "",
            "arc_flash": safety.get("arc_flash") or "",
            "drawing_disclaimer": safety.get("drawing_disclaimer") or "",
        },
        "ref": payload.get("ref") or "",
        "symptom_title": payload.get("symptom_title") or "",
        "how": payload.get("how") or "",
        "path": payload.get("path") or "",
        "instruction": payload.get("instruction") or "",
        "fault_text": payload.get("fault_text") or "",
        "next_check": {
            "energized": current.get("energized") or "",
            "device": current.get("device") or "",
            "terminal": current.get("terminal") or "",
            "wire": current.get("wire") or "",
            "page_zone": current.get("page_zone") or "",
            "meter_leads": current.get("meter_leads") or "",
            "expected": {
                "text": expected.get("text"),
                "needs_info": bool(expected.get("needs_info")),
            },
            "plc": current.get("plc"),
        },
        "causes": [str(row.get("text") or "") for row in (payload.get("causes") or [])[:6]],
        "cross_refs": list(payload.get("cross_refs") or [])[:8],
    }
    return "Explain the next check from this JSON only:\n" + json.dumps(slim, indent=2)


def _complete(
    config: Any,
    prompt: str,
    *,
    transport: Transport | None,
    timeout: float,
    cancel: ExplainCall | None = None,
) -> str:
    provider = config.provider
    base = config.resolved_endpoint()
    model = config.resolved_model()
    if not base or not model:
        raise RuntimeError("AI provider is not configured")
    if provider in {"xai", "openai"}:
        url = _join(base, "responses")
        body: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
    else:
        url = _join(base, "chat/completions")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
        }
    raw_body = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"PanelCAD/{__version__}",
    }
    key = str(config.api_key or "")
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=raw_body, headers=headers, method="POST")
    if cancel is not None and cancel.cancelled():
        raise _ExplainCancelled()
    try:
        if transport is not None:
            resp = transport(req)
            if cancel is not None and cancel.cancelled():
                raise _ExplainCancelled()
            raw = resp.read()
        else:
            raw = _read_response(req, timeout=timeout, cancel=cancel)
    except _ExplainCancelled:
        raise
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"AI provider HTTP {exc.code}") from None
    if cancel is not None and cancel.cancelled():
        raise _ExplainCancelled()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise RuntimeError("AI provider returned no JSON") from None
    if not isinstance(parsed, dict):
        raise RuntimeError("AI provider returned no JSON")
    return _message_text(parsed)


def _read_response(
    req: urllib.request.Request,
    *,
    timeout: float,
    cancel: ExplainCall | None,
) -> bytes:
    """Read the provider reply. Cancel closes the socket and returns at once."""

    if cancel is None:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            cancel.attach(resp.close)
            try:
                if cancel.cancelled():
                    box["cancelled"] = True
                    return
                box["raw"] = resp.read()
            finally:
                cancel.detach()
                try:
                    resp.close()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 — mapped below; the key is not logged
            if cancel.cancelled():
                box["cancelled"] = True
            else:
                box["error"] = exc

    worker = threading.Thread(target=target, name="panelcad-explain-http", daemon=True)
    worker.start()
    deadline = time.monotonic() + max(0.0, timeout)
    while worker.is_alive():
        if cancel.cancelled():
            worker.join(timeout=0.2)
            raise _ExplainCancelled()
        if time.monotonic() >= deadline:
            cancel.cancel()
            worker.join(timeout=0.2)
            raise TimeoutError("AI provider timed out")
        worker.join(0.05)
    if box.get("cancelled") or cancel.cancelled():
        raise _ExplainCancelled()
    if "error" in box:
        raise box["error"]
    raw = box.get("raw")
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    raise RuntimeError("AI provider returned nothing")


def _join(base: str, suffix: str) -> str:
    root = str(base or "").rstrip("/")
    tail = suffix.lstrip("/")
    if root.endswith("/" + tail) or root.endswith(tail):
        return root
    return root + "/" + tail


def _message_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if isinstance(output, list):
        chunks: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("text"):
                        chunks.append(str(part["text"]))
            elif isinstance(item.get("text"), str):
                chunks.append(str(item["text"]))
        if chunks:
            return "".join(chunks)
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""
