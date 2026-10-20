"""Bug-report settings.

Webhook order is the environment variable, then the settings file, then the
webhook embedded at release build time. Bot-token order is the same: environment,
then the settings file, then the embedded token. The embedded token is pinned
to the channel id stored beside it at build time and ignores a channel set
here or in the environment. A public build embeds neither. ``disabled`` or
``PANELCAD_BUG_REPORTS=off`` turns sending off even when an endpoint exists.
The webhook is the text fallback when a bot upload does not post.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from panelcad.diag.log import log_event

ENV_WEBHOOK = "PANELCAD_BUG_WEBHOOK"
ENV_BOT_TOKEN = "PANELCAD_SLACK_BOT_TOKEN"
ENV_CHANNEL = "PANELCAD_SLACK_CHANNEL"
ENV_RELAY = "PANELCAD_BUG_RELAY"
ENV_REPORTS = "PANELCAD_BUG_REPORTS"
ENV_AUTOSAVE = "PANELCAD_AUTOSAVE_SECONDS"
DEFAULT_AUTOSAVE_SECONDS = 300
_OFF_VALUES = frozenset({"off", "0", "false", "no"})


@dataclass
class BugSettings:
    webhook_url: str = ""
    slack_bot_token: str = ""
    slack_channel: str = ""
    relay_url: str = ""
    disabled: bool = False
    webhook_source: str = ""
    token_source: str = ""

    def with_env(self) -> BugSettings:
        """Environment, then this object, then the embedded webhook and bot.

        Call this on stored values. A second pass treats an already resolved
        default URL or token as a saved value. The embedded token uses the
        channel id from the build and ignores ``PANELCAD_SLACK_CHANNEL`` and
        the saved channel.
        """

        from panelcad.diag.endpoint import default_bot_token, default_webhook_url, embedded_channel_id

        disabled = _disabled(self.disabled)
        relay = _env_or(ENV_RELAY, self.relay_url)
        env_token = os.environ.get(ENV_BOT_TOKEN, "").strip()
        if env_token:
            token, token_source = env_token, "env"
        elif self.slack_bot_token.strip():
            token, token_source = self.slack_bot_token.strip(), "settings"
        else:
            token = default_bot_token()
            token_source = "default" if token else ""
        if token_source == "default":
            channel = embedded_channel_id()
        else:
            channel = _env_or(ENV_CHANNEL, self.slack_channel)
        env_hook = os.environ.get(ENV_WEBHOOK, "").strip()
        if env_hook:
            webhook, source = env_hook, "env"
        elif self.webhook_url.strip():
            webhook, source = self.webhook_url.strip(), "settings"
        else:
            webhook = default_webhook_url()
            source = "default" if webhook else ""
        return BugSettings(
            webhook_url=webhook,
            slack_bot_token=token,
            slack_channel=channel,
            relay_url=relay,
            disabled=disabled,
            webhook_source=source,
            token_source=token_source,
        )

    def env_overrides(self) -> dict[str, bool]:
        return {
            "webhook_url": bool(os.environ.get(ENV_WEBHOOK, "").strip()),
            "slack_bot_token": bool(os.environ.get(ENV_BOT_TOKEN, "").strip()),
            "slack_channel": bool(os.environ.get(ENV_CHANNEL, "").strip()),
            "relay_url": bool(os.environ.get(ENV_RELAY, "").strip()),
            "disabled": _env_reports_off(),
        }

    def destination_name(self) -> str:
        """``PanelCAD team``, ``your webhook``, ``Slack channel C… (bot)``, or ``off``."""

        if self.disabled:
            return "off"
        if self.relay_url.strip() or self.webhook_source in {"env", "settings"}:
            return "your webhook"
        if (
            self.token_source != "default"
            and self.slack_bot_token.strip()
            and self.slack_channel.strip()
        ):
            return f"Slack channel {self.slack_channel.strip()} (bot)"
        if (self.webhook_source == "default" and self.webhook_url.strip()) or (
            self.token_source == "default" and self.slack_bot_token.strip()
        ):
            return "PanelCAD team"
        return "off"


def destination_line(settings: BugSettings | None = None) -> str:
    cfg = settings if settings is not None else load_effective_settings()
    name = cfg.destination_name()
    if name == "off":
        return "Sending is not set up on this PC. Click Save and email the file to the office."
    return f"Reports go to: {name}"


def _env_reports_off() -> bool:
    return os.environ.get(ENV_REPORTS, "").strip().lower() in _OFF_VALUES


def _disabled(stored: bool) -> bool:
    """The off switch wins over every endpoint."""

    if _env_reports_off():
        return True
    return bool(stored)


def _env_or(key: str, stored: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or stored.strip()


def _read_settings() -> dict:
    """The file on disk, including keys this module does not own.

    A lock is not an empty file. The last good copy is used until the file
    can be read again, and it is not written back from that failure.
    """

    from panelcad.settings import peek, read_disk

    try:
        return read_disk()
    except OSError:
        try:
            return dict(peek())
        except OSError:
            return {}


def _write_settings(patch: dict) -> None:
    """Merge ``patch`` into the shared file. Other keys stay."""

    from panelcad.settings import update

    update(patch)


def stored_autosave_seconds() -> int:
    """Interval saved in settings.json. Ignores ``PANELCAD_AUTOSAVE_SECONDS``."""

    raw = _read_settings().get("autosave_seconds", DEFAULT_AUTOSAVE_SECONDS)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_AUTOSAVE_SECONDS


def autosave_env_set() -> bool:
    return bool(os.environ.get(ENV_AUTOSAVE, "").strip())


def autosave_seconds() -> int:
    """How often to copy a dirty drawing into the recovery folder. ``0`` is off."""

    env = os.environ.get(ENV_AUTOSAVE, "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return stored_autosave_seconds()


def set_autosave_seconds(seconds: int) -> int:
    """Write the autosave interval. Does not clear bug-report settings.

    A negative value is stored as ``0`` (off). The environment variable still
    overrides this file at runtime.
    """

    try:
        value = int(seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("autosave_seconds must be a whole number of seconds") from exc
    value = max(0, value)
    _write_settings({"autosave_seconds": value})
    log_event("settings", "autosave interval saved")
    return value


def load_stored_settings() -> BugSettings:
    data = _read_settings()
    bug = data.get("bug_reports") if isinstance(data, dict) else None
    if not isinstance(bug, dict):
        return BugSettings()
    return BugSettings(
        webhook_url=str(bug.get("webhook_url") or ""),
        slack_bot_token=str(bug.get("slack_bot_token") or ""),
        slack_channel=str(bug.get("slack_channel") or ""),
        relay_url=str(bug.get("relay_url") or ""),
        disabled=bool(bug.get("disabled")),
    )


def load_effective_settings() -> BugSettings:
    return load_stored_settings().with_env()


def save_settings(settings: BugSettings) -> None:
    """Write the fields the user typed. Does not copy environment secrets into the file."""

    _write_settings(
        {
            "bug_reports": {
                "webhook_url": settings.webhook_url.strip(),
                "slack_bot_token": settings.slack_bot_token.strip(),
                "slack_channel": settings.slack_channel.strip(),
                "relay_url": settings.relay_url.strip(),
                "disabled": bool(settings.disabled),
            }
        }
    )
    log_event("settings", "bug report settings saved")
