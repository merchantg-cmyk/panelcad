"""Datasheet-lookup settings. The key belongs to the person using PanelCAD.

Nothing in this module embeds a shared or built-in key. Environment variables
override the file. Callers that log a failure must not log ``api_key``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

CONNECT_REASON = "Connect an AI in Settings > AI to look up datasheets."
TURNED_OFF_REASON = "AI lookup is turned off. Enter dimensions from the datasheet."

_PROVIDERS = frozenset({"xai", "openai", "compatible", "ollama"})
_ON = frozenset({"1", "on", "true", "yes"})
_OFF = frozenset({"0", "off", "false", "no"})

_DEFAULT_ENDPOINT = {
    "xai": "https://api.x.ai/v1",
    "openai": "https://api.openai.com/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}
_DEFAULT_MODEL = {
    "xai": "grok-4",
    "openai": "gpt-4.1",
    "ollama": "gemma3:4b",
}


@dataclass
class AiLookupConfig:
    """What Settings > AI saved, after environment overrides when those apply."""

    enabled: bool = False
    provider: str = ""
    endpoint: str = ""
    api_key: str = ""
    model: str = ""

    def resolved_endpoint(self) -> str:
        raw = self.endpoint.strip()
        if self.provider == "ollama" and not raw:
            raw = os.environ.get("OLLAMA_BASE_URL", "").strip()
        if not raw:
            raw = _DEFAULT_ENDPOINT.get(self.provider, "")
        return _normalize_base(self.provider, raw)

    def resolved_model(self) -> str:
        text = self.model.strip()
        if text:
            return text
        return _DEFAULT_MODEL.get(self.provider, "")

    def to_stored(self) -> dict[str, str | bool]:
        return {
            "datasheet_lookup": bool(self.enabled),
            "provider": self.provider,
            "endpoint": self.endpoint,
            "api_key": self.api_key,
            "model": self.model,
        }


def load_ai_lookup(*, stored_only: bool = False) -> AiLookupConfig:
    """Read Settings > AI. Pass ``stored_only`` for the settings form."""

    from panelcad.settings import get

    raw = get("ai")
    stored = raw if isinstance(raw, dict) else {}
    enabled = bool(stored.get("datasheet_lookup"))
    provider = str(stored.get("provider") or "").strip().lower()
    endpoint = str(stored.get("endpoint") or "").strip()
    api_key = str(stored.get("api_key") or "").strip()
    model = str(stored.get("model") or "").strip()
    if stored_only:
        return AiLookupConfig(enabled, provider, endpoint, api_key, model)
    flag = os.environ.get("PANELCAD_AI_LOOKUP", "").strip().lower()
    if flag in _ON:
        enabled = True
    elif flag in _OFF:
        enabled = False
    provider_env = os.environ.get("PANELCAD_AI_PROVIDER", "").strip().lower()
    if provider_env:
        provider = provider_env
    endpoint_env = os.environ.get("PANELCAD_AI_ENDPOINT", "").strip() or os.environ.get(
        "PANELCAD_AI_BASE_URL", ""
    ).strip()
    if endpoint_env:
        endpoint = endpoint_env
    key_env = os.environ.get("PANELCAD_AI_API_KEY", "").strip()
    if key_env:
        api_key = key_env
    model_env = os.environ.get("PANELCAD_AI_MODEL", "").strip()
    if model_env:
        model = model_env
    return AiLookupConfig(enabled, provider, endpoint, api_key, model)


def save_ai_lookup(config: AiLookupConfig) -> None:
    """Write the AI block. Other settings keys stay on disk."""

    from panelcad.settings import get, update

    current = dict(get("ai") or {})
    current.update(config.to_stored())
    update({"ai": current})


def configuration_reason(config: AiLookupConfig | None = None) -> str:
    """Why lookup is off. Empty when the saved account can be called."""

    cfg = load_ai_lookup() if config is None else config
    if not cfg.enabled:
        if cfg.provider or cfg.api_key or cfg.endpoint or cfg.model:
            return "AI datasheet lookup is turned off. Turn it on in Settings > AI."
        return CONNECT_REASON
    if cfg.provider not in _PROVIDERS:
        return CONNECT_REASON
    if cfg.provider in {"xai", "openai"} and not cfg.api_key:
        return CONNECT_REASON
    if cfg.provider == "compatible" and not (cfg.resolved_endpoint() and cfg.resolved_model()):
        return CONNECT_REASON
    if cfg.provider == "ollama" and not cfg.resolved_endpoint():
        return CONNECT_REASON
    return ""


def _normalize_base(provider: str, endpoint: str) -> str:
    base = str(endpoint or "").strip().rstrip("/")
    if not base:
        return ""
    if provider == "ollama" and "/v1" not in base and base.count("/") <= 2:
        return base + "/v1"
    return base
