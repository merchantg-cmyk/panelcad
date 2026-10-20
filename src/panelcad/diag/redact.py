"""Replace home directories and bug-report secrets before anything is stored."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from panelcad.diag.paths import settings_path

_TOKEN_RE = re.compile(r"xox[a-z]-[A-Za-z0-9-]{6,}", re.IGNORECASE)
_APP_TOKEN_RE = re.compile(r"xapp-\d-[A-Za-z0-9-]{8,}", re.IGNORECASE)
_WEBHOOK_RE = re.compile(r"https://hooks\.slack\.com/\S+")
_DISCORD_WEBHOOK_RE = re.compile(
    r"https://(?:discord(?:app)?\.com)/api/webhooks/\S+",
    re.IGNORECASE,
)
# Only the last segment of a hook-shaped URL. The rest of a docs link stays.
_CUSTOM_HOOK_RE = re.compile(
    r"(https?://[^\s\"'<>]+/(?:hooks?|webhooks?|services)(?:/[^\s\"'<>/]+)*/)"
    r"([^\s\"'<>/]{8,})",
    re.IGNORECASE,
)
_GITHUB_RE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b")
_GITHUB_PAT_RE = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b")
_AI_KEY_RE = re.compile(r"\b(?:sk|xai)-[A-Za-z0-9_-]{12,}\b")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{12,}")
_USERINFO_RE = re.compile(r"(?i)(https?://)[^\s/:@]+:[^\s/@]+@")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|token|secret|api[_-]?key|access[_-]?token)=)[^&#\s\"']+"
)
_ASSIGNED_SECRET_RE = re.compile(
    r"(?i)((?:[A-Za-z0-9_.-]*?)(?:api[_-]?key|access[_-]?token|bot[_-]?token|"
    r"slack[_-]?token|webhook(?:_url|_token)?|secret|password)\s*[=:]\s*)([^\s\"']{8,})"
)
_SECRET_KEYS = (
    "PANELCAD_BUG_WEBHOOK",
    "PANELCAD_SLACK_BOT_TOKEN",
    "PANELCAD_BUG_RELAY",
    "PANELCAD_AI_API_KEY",
)
_FILE_SECRET_KEYS = ("webhook_url", "slack_bot_token", "relay_url")
_extra_secrets: ContextVar[tuple[str, ...]] = ContextVar("panelcad_redact_extra", default=())


@contextmanager
def extra_secrets(*values: str):
    """Redact these exact strings for the duration of one report.

    A webhook passed on the call, and not yet saved, is still a secret.
    """

    found = [item.strip() for item in values if item and len(item.strip()) >= 8]
    token = _extra_secrets.set(_extra_secrets.get() + tuple(found))
    try:
        yield
    finally:
        _extra_secrets.reset(token)


def _configured_secrets() -> list[str]:
    found: list[str] = []
    for key in _SECRET_KEYS:
        value = os.environ.get(key, "").strip()
        if len(value) >= 8:
            found.append(value)
    path = settings_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            data = {}
        if isinstance(data, dict):
            bug = data.get("bug_reports")
            if isinstance(bug, dict):
                for key in _FILE_SECRET_KEYS:
                    value = str(bug.get(key) or "").strip()
                    if len(value) >= 8:
                        found.append(value)
            ai = data.get("ai")
            if isinstance(ai, dict):
                value = str(ai.get("api_key") or "").strip()
                if len(value) >= 8:
                    found.append(value)
                endpoint = str(ai.get("endpoint") or "").strip()
                if len(endpoint) >= 8 and (
                    "://" in endpoint and ("@" in endpoint.split("://", 1)[-1] or "?" in endpoint)
                ):
                    found.append(endpoint)
    try:
        from panelcad.diag.endpoint import default_bot_token, default_webhook_url

        embedded = default_webhook_url().strip()
        embedded_bot = default_bot_token().strip()
    except Exception:  # noqa: BLE001 — redaction must not raise
        embedded = ""
        embedded_bot = ""
    if len(embedded) >= 8:
        found.append(embedded)
    if len(embedded_bot) >= 8:
        found.append(embedded_bot)
    found.extend(_extra_secrets.get())
    return found


def redact_secrets(text: str) -> str:
    """Replace webhook URLs, tokens, and the user's AI API key.

    Home directories stay so a path remains usable. Slack bot tokens, webhook
    URLs, AI API keys, and similar credentials become ``[redacted]``.
    """

    if not text:
        return ""
    out = str(text)
    for secret in sorted(set(_configured_secrets()), key=len, reverse=True):
        if secret and secret in out:
            out = out.replace(secret, "[redacted]")
    out = _TOKEN_RE.sub("[redacted]", out)
    out = _APP_TOKEN_RE.sub("[redacted]", out)
    out = _WEBHOOK_RE.sub("[redacted]", out)
    out = _DISCORD_WEBHOOK_RE.sub("[redacted]", out)
    out = _CUSTOM_HOOK_RE.sub(r"\1[redacted]", out)
    out = _GITHUB_RE.sub("[redacted]", out)
    out = _GITHUB_PAT_RE.sub("[redacted]", out)
    out = _AI_KEY_RE.sub("[redacted]", out)
    out = _BEARER_RE.sub(r"\1[redacted]", out)
    out = _USERINFO_RE.sub(r"\1[redacted]@", out)
    out = _QUERY_SECRET_RE.sub(r"\1[redacted]", out)
    out = _ASSIGNED_SECRET_RE.sub(r"\1[redacted]", out)
    return out


_DRIVE_ROOT = re.compile(r"^[A-Za-z]:\\?$")


def _home_roots(home: Path | None) -> list[Path]:
    """Homes to strip. An explicit ``home`` is the only root; otherwise use the process home and profile env."""

    if home is not None:
        return [home]
    found: list[Path] = []
    seen: set[str] = set()
    candidates = [Path.home()]
    for key in ("USERPROFILE", "HOME"):
        raw = os.environ.get(key, "").strip().strip('"')
        if raw:
            candidates.append(Path(raw))
    for item in candidates:
        key = str(item).rstrip("\\/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(item)
    return found


def _home_variants(home: Path) -> list[str]:
    raws = [str(home)]
    try:
        expanded = str(home.expanduser())
    except (OSError, RuntimeError, ValueError):
        expanded = ""
    if expanded:
        raws.append(expanded)
    variants: list[str] = []
    for raw in raws:
        if not raw or raw in {".", "/", "\\"}:
            continue
        trimmed = raw.rstrip("\\/")
        if len(trimmed) < 2 or _DRIVE_ROOT.fullmatch(trimmed):
            continue
        for item in (trimmed, trimmed.replace("\\", "/"), trimmed.replace("/", "\\")):
            if item not in variants:
                variants.append(item)
    variants.sort(key=len, reverse=True)
    return variants


def redact_text(text: str, *, home: Path | None = None) -> str:
    """Replace secret values and the user home directory.

    Home is written as ``~``. ``C:\\Users\\<name>\\...`` becomes ``~\\...``.
    Webhook URLs, Slack tokens, and the AI API key become ``[redacted]``.
    """

    if not text:
        return ""
    out = redact_secrets(text)
    variants: list[str] = []
    for root in _home_roots(home):
        for item in _home_variants(root):
            if item not in variants:
                variants.append(item)
    variants.sort(key=len, reverse=True)
    for variant in variants:
        pattern = re.compile(re.escape(variant) + r"(?=$|[\\/])", re.IGNORECASE)
        out = pattern.sub("~", out)
    return out


def redact_path(path: Path | str | None, *, home: Path | None = None) -> str:
    if path is None:
        return ""
    return redact_text(str(path), home=home)
