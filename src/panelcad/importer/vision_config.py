"""Scan-vision presets. The API key stays in an environment variable.

xAI chat completions (OpenAI-compatible, image input):
https://docs.x.ai/developers/rest-api-reference/inference/chat-completions
POST https://api.x.ai/v1/chat/completions

Vision-capable model id used by the Grok preset, from the image-understanding
examples (not a guessed name):
https://docs.x.ai/developers/model-capabilities/images/understanding
model ``grok-4.7``
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 10_000_000_000 ticks = 1 USD. Documented on the chat-completions usage object.
USD_TICKS = 10_000_000_000

XAI_BASE_URL = "https://api.x.ai/v1"
XAI_MODEL = "grok-4.7"
XAI_KEY_ENV = "XAI_API_KEY"
XAI_ENDPOINT_DOC = "https://docs.x.ai/developers/rest-api-reference/inference/chat-completions"
XAI_MODEL_DOC = "https://docs.x.ai/developers/model-capabilities/images/understanding"

OLLAMA_BASE_URL = "http://127.0.0.1:11434/v1"
# The rest of PanelCAD already defaults the local model to gemma3:4b.
OLLAMA_MODEL = "gemma3:4b"
# A 14–27B vision model on a 16 GB GPU can spend minutes on one tile.
OLLAMA_TIMEOUT_S = 300.0
# One page of tiles, not the whole multi-page job.
OLLAMA_PAGE_TIMEOUT_S = 3600.0
OLLAMA_NUM_CTX = 16384
# A nine-field row is about 110 tokens. 1200 cut gemma and qwen off mid-tile.
DEFAULT_NUM_PREDICT = 4096
# One local model that spills off the GPU should not take three queued tiles.
OLLAMA_WORKERS = 1

# Local Grok Build CLI. Flags that were checked against the published reference
# are fixed in grok_build.py. grok --help could not be run here (no binary).
# Unverified flags go in grok_extra_args. There is no --no-mcp switch.
GROK_CLI_DOC = "https://docs.x.ai/build/cli/reference"
GROK_HEADLESS_DOC = "https://docs.x.ai/build/cli/headless-scripting"

PRESET_NONE = "none"
PRESET_GROK = "grok"
PRESET_GROK_BUILD = "grok-build"
PRESET_OLLAMA = "ollama"
PRESET_CUSTOM = "custom"
PRESETS = (PRESET_NONE, PRESET_GROK, PRESET_GROK_BUILD, PRESET_OLLAMA, PRESET_CUSTOM)

PRESET_LABELS = {
    PRESET_NONE: "None (pixel only)",
    PRESET_GROK: "xAI Grok",
    PRESET_GROK_BUILD: "Local Grok Build",
    PRESET_OLLAMA: "Ollama (local)",
    PRESET_CUSTOM: "Custom",
}

DEFAULT_TIMEOUT_S = 90.0
DEFAULT_PAGE_TIMEOUT_S = 180.0
DEFAULT_MAX_USD = 2.0
DEFAULT_WORKERS = 3
MAX_WORKERS = 8
# A dense starter tile took about 120 s and 8.9k output tokens. The old
# 120 s limit plus one retry (240 s) dropped that tile. 600 s is the
# per-request default. An explicit timeout is left alone. The page limit
# still caps the whole print.
GROK_BUILD_TIMEOUT_S = 600.0
GROK_BUILD_PAGE_TIMEOUT_S = 3600.0
# A killed tile often prints no usage. Live priced tiles were about $0.01–$0.02.
ESTIMATED_TILE_USD = 0.02

SETTINGS_PATH = Path.home() / ".panelcad" / "vision.json"

# Never written to the settings file or the import manifest.
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "bearer",
    "key",
    "token",
    "secret",
}


@dataclass
class VisionSettings:
    """What the user selected. ``api_key_env`` is a variable name, not the key."""

    preset: str = PRESET_NONE
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    page_timeout_s: float = DEFAULT_PAGE_TIMEOUT_S
    max_usd: float = DEFAULT_MAX_USD
    workers: int = DEFAULT_WORKERS
    grok_extra_args: str = ""
    # Delete only this tile's folder under ~/.grok/sessions. Default on.
    clean_grok_sessions: bool = True
    # ``--tools none`` saved about 3.4K tokens on grok 1.0.46. Off until checked live.
    grok_tools_none: bool = False
    # Ollama context length. 0 means the preset default (16384 for Ollama).
    num_ctx: int = 0
    # Completion cap. 0 means 4096. Ollama receives this as num_predict.
    num_predict: int = 0
    # None means the preset default: thinking off for Ollama, unset elsewhere.
    think: bool | None = None
    # /v1 servers. Empty means do not send the field. Ollama uses ``think`` instead.
    reasoning_effort: str = ""

    def resolved(self) -> VisionSettings:
        """Apply preset defaults. An explicit model or URL is kept."""

        preset = _preset_name(self.preset)
        base = self.base_url.strip()
        model = self.model.strip()
        env = self.api_key_env.strip()
        timeout_s = _positive(self.timeout_s, DEFAULT_TIMEOUT_S)
        page_timeout_s = _positive(self.page_timeout_s, DEFAULT_PAGE_TIMEOUT_S)
        if preset == PRESET_GROK:
            base = base or XAI_BASE_URL
            model = model or XAI_MODEL
            env = env or XAI_KEY_ENV
        elif preset == PRESET_GROK_BUILD:
            # The CLI uses the signed-in model. No key is stored or sent.
            base, model, env = "", "", ""
            if timeout_s == DEFAULT_TIMEOUT_S:
                timeout_s = GROK_BUILD_TIMEOUT_S
            if page_timeout_s == DEFAULT_PAGE_TIMEOUT_S:
                page_timeout_s = GROK_BUILD_PAGE_TIMEOUT_S
        elif preset == PRESET_OLLAMA:
            base = base or OLLAMA_BASE_URL
            model = model or OLLAMA_MODEL
            if timeout_s == DEFAULT_TIMEOUT_S:
                timeout_s = OLLAMA_TIMEOUT_S
            if page_timeout_s == DEFAULT_PAGE_TIMEOUT_S:
                page_timeout_s = OLLAMA_PAGE_TIMEOUT_S
        elif preset == PRESET_NONE:
            base, model, env = "", "", ""
        num_ctx = _ctx(self.num_ctx)
        if preset == PRESET_OLLAMA and num_ctx <= 0:
            num_ctx = OLLAMA_NUM_CTX
        think = self.think
        if preset == PRESET_OLLAMA and think is None:
            think = False
        return VisionSettings(
            preset=preset,
            base_url=base.rstrip("/"),
            model=model,
            api_key_env=env,
            timeout_s=timeout_s,
            page_timeout_s=page_timeout_s,
            max_usd=_positive(self.max_usd, DEFAULT_MAX_USD),
            workers=_workers(self.workers),
            grok_extra_args=_scrub_extra(self.grok_extra_args),
            clean_grok_sessions=_flag(self.clean_grok_sessions, True),
            grok_tools_none=_flag(self.grok_tools_none, False),
            num_ctx=num_ctx,
            num_predict=_predict(self.num_predict),
            think=think,
            reasoning_effort=_effort(self.reasoning_effort),
        )

    def public_dict(self) -> dict[str, Any]:
        """Safe to store. The key itself is not a field."""

        item = self.resolved()
        payload = {
            "preset": item.preset,
            "base_url": item.base_url,
            "model": item.model,
            "api_key_env": item.api_key_env,
            "timeout_s": item.timeout_s,
            "page_timeout_s": item.page_timeout_s,
            "max_usd": item.max_usd,
            "workers": item.workers,
            "grok_extra_args": item.grok_extra_args,
            "clean_grok_sessions": item.clean_grok_sessions,
            "grok_tools_none": item.grok_tools_none,
            "num_ctx": item.num_ctx,
            "num_predict": item.num_predict,
            "think": item.think,
            "reasoning_effort": item.reasoning_effort,
        }
        return {key: value for key, value in payload.items() if key not in _SECRET_KEYS}

    def api_key(self) -> str:
        env = self.resolved().api_key_env
        if not env:
            return ""
        return os.environ.get(env) or ""

    def label(self) -> str:
        return PRESET_LABELS.get(self.resolved().preset, self.preset)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> VisionSettings:
        return VisionSettings(
            preset=_preset_name(str(raw.get("preset") or PRESET_NONE)),
            base_url=str(raw.get("base_url") or ""),
            model=str(raw.get("model") or ""),
            api_key_env=str(raw.get("api_key_env") or ""),
            timeout_s=_positive(raw.get("timeout_s"), DEFAULT_TIMEOUT_S),
            page_timeout_s=_positive(raw.get("page_timeout_s"), DEFAULT_PAGE_TIMEOUT_S),
            max_usd=_positive(raw.get("max_usd"), DEFAULT_MAX_USD),
            workers=_workers(raw.get("workers")),
            grok_extra_args=_scrub_extra(str(raw.get("grok_extra_args") or "")),
            clean_grok_sessions=_flag(raw.get("clean_grok_sessions"), True),
            grok_tools_none=_flag(raw.get("grok_tools_none"), False),
            num_ctx=_ctx(raw.get("num_ctx")),
            num_predict=_predict(raw.get("num_predict")) if raw.get("num_predict") not in (None, "", 0) else 0,
            think=_think(raw.get("think")) if "think" in raw else None,
            reasoning_effort=_effort(raw.get("reasoning_effort")),
        )


def preset_defaults(name: str) -> VisionSettings:
    return VisionSettings(preset=_preset_name(name)).resolved()


def vision_from_options(
    *,
    preset: str = "none",
    base_url: str = "",
    model: str = "",
    api_key_env: str = "",
    workers: int | None = None,
    timeout_s: float | None = None,
    page_timeout_s: float | None = None,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    think: Any = None,
    reasoning_effort: str = "",
    clean_grok_sessions: bool = True,
    grok_tools_none: bool = False,
    max_usd: float | None = None,
) -> VisionSettings:
    """Build settings for CLI, JSONL, and Session. Omitted clocks keep the preset default."""

    fields: dict[str, Any] = {
        "preset": preset,
        "base_url": base_url,
        "model": model,
        "api_key_env": api_key_env,
        "workers": (_preset_workers(preset) if workers is None else workers),
        "clean_grok_sessions": clean_grok_sessions,
        "grok_tools_none": grok_tools_none,
        "reasoning_effort": reasoning_effort or "",
    }
    if timeout_s is not None:
        fields["timeout_s"] = timeout_s
    if page_timeout_s is not None:
        fields["page_timeout_s"] = page_timeout_s
    if num_ctx is not None:
        fields["num_ctx"] = num_ctx
    if num_predict is not None:
        fields["num_predict"] = num_predict
    if think is not None:
        fields["think"] = think if isinstance(think, bool) else _think(think)
    if max_usd is not None:
        fields["max_usd"] = max_usd
    return VisionSettings(**fields).resolved()


def load_settings(path: Path | str | None = None) -> VisionSettings:
    file = Path(path) if path else SETTINGS_PATH
    if not file.is_file():
        return VisionSettings()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return VisionSettings()
    if not isinstance(raw, dict):
        return VisionSettings()
    return VisionSettings.from_dict(raw)


def save_settings(settings: VisionSettings, path: Path | str | None = None) -> Path:
    file = Path(path) if path else SETTINGS_PATH
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.public_dict()
    file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return file


def redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def _preset_name(value: str) -> str:
    token = str(value or PRESET_NONE).strip().lower().replace("_", "-")
    if token in {"grok-build", "grokbuild", "local-grok", "local grok", "grok build", "local grok build"}:
        return PRESET_GROK_BUILD
    if token in {"xai", "x.ai", "grok"}:
        return PRESET_GROK
    if token in {"local", "ollama"}:
        return PRESET_OLLAMA
    if token in PRESETS:
        return token
    return PRESET_NONE


def _flag(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _preset_workers(preset: str) -> int:
    """Ollama defaults to one worker. Other presets keep three."""

    if _preset_name(preset) == PRESET_OLLAMA:
        return OLLAMA_WORKERS
    return DEFAULT_WORKERS


def _workers(value: Any) -> int:
    if value is None or value == "":
        return DEFAULT_WORKERS
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WORKERS
    return max(1, min(MAX_WORKERS, number))


def _scrub_extra(text: str) -> str:
    raw = str(text or "").strip()
    low = raw.lower()
    if any(word in low for word in ("api_key", "apikey", "bearer", "secret", "password", "token")):
        return ""
    return raw


def _ctx(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _predict(value: Any) -> int:
    if value is None or value == "" or value == 0:
        return DEFAULT_NUM_PREDICT
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_NUM_PREDICT
    return number if number > 0 else DEFAULT_NUM_PREDICT


def _think(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off", "none"}:
        return False
    return None


def _effort(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"none", "low", "medium", "high"}:
        return token
    return ""


def _positive(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return number
