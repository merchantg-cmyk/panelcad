"""Datasheet lookup through the AI account configured in Settings > AI.

xAI and OpenAI are asked through their responses API with the provider's web
search tool. Ollama and other OpenAI-compatible servers use chat completions
on the endpoint the user set. PanelCAD does not ship a key and does not send
Grok CLI identity headers. The API key is never logged.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Callable

from panelcad.ai.config import AiLookupConfig
from panelcad.version import __version__

log = logging.getLogger("panelcad.ai")

_PROMPT = """Find the manufacturer datasheet for {manufacturer} {part_number}.
Description from the vendor list: {description}

Return JSON only, no markdown:
{{"values":[{{"field":"width_mm|height_mm|depth_mm|mount|category","value":"","source":"https://...","page":"","table":"","confidence":"high|medium|low"}}]}}

Rules:
- Use only values printed in the manufacturer's datasheet.
- Every value needs an http(s) URL for that datasheet.
- page or table names the place on that datasheet when you can see it.
- mount is din_rail or panel only when the datasheet says panel mount, panel-mounted, or mounting: panel.
- The word panel alone is not a mount.
- width_mm, height_mm, and depth_mm include the unit printed on the datasheet.
- A number with no unit is not a millimetre.
- category is the library family the datasheet uses.
- If a value is not printed, omit that field.
- Do not estimate, convert, or fill a typical size.
"""

_SYSTEM = "You cite manufacturer datasheets. You do not estimate dimensions."

Transport = Callable[[urllib.request.Request], Any]


class DatasheetLookupClient:
    """One datasheet question. The key stays on this object and is never logged."""

    def __init__(
        self,
        config: AiLookupConfig,
        *,
        timeout: float = 45.0,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self._transport = transport

    def lookup(self, *, manufacturer: str = "", part_number: str = "", description: str = "") -> dict[str, Any]:
        prompt = _PROMPT.format(
            manufacturer=str(manufacturer or "").strip() or "(unknown maker)",
            part_number=str(part_number or "").strip() or "(unknown part)",
            description=str(description or "").strip() or "(none)",
        )
        try:
            text = self._complete(prompt)
        except Exception as exc:  # noqa: BLE001 — offer manual entry, do not log the key
            log.warning("datasheet lookup failed: %s", type(exc).__name__)
            return {
                "error": "Datasheet lookup failed. Enter dimensions from the datasheet.",
                "values": [],
            }
        data = _json_object(text)
        if not data:
            return {"values": []}
        return data

    def _complete(self, prompt: str) -> str:
        provider = self.config.provider
        base = self.config.resolved_endpoint()
        model = self.config.resolved_model()
        if not base or not model:
            raise RuntimeError("AI provider is not configured")
        if provider in {"xai", "openai"}:
            url = _join(base, "responses")
            payload: dict[str, Any] = {
                "model": model,
                "input": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "tools": [{"type": "web_search"}],
            }
        else:
            url = _join(base, "chat/completions")
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "stream": False,
            }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"PanelCAD/{__version__}",
        }
        key = self.config.api_key
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            if self._transport is not None:
                resp = self._transport(req)
                raw = resp.read()
            else:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"AI provider HTTP {exc.code}") from None
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise RuntimeError("AI provider returned no JSON") from None
        if not isinstance(parsed, dict):
            raise RuntimeError("AI provider returned no JSON") from None
        return _message_text(parsed)


def _join(base: str, suffix: str) -> str:
    root = base.rstrip("/")
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
            if isinstance(content, list):
                return "".join(
                    str(part.get("text") or "") for part in content if isinstance(part, dict)
                )
    return ""


def _json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
