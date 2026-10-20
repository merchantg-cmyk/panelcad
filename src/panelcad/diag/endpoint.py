"""Build-time bug-report endpoint and bot token.

``scripts/build-release.py`` reads ``PANELCAD_RELEASE_BUG_WEBHOOK`` and, when
that value is a Slack incoming webhook, writes it into the release zip as
``_report_endpoint.txt``. It reads ``PANELCAD_RELEASE_SLACK_BOT_TOKEN`` and,
when that value is an ``xoxb-…`` bot token, writes it into the zip as
``_report_bot.txt`` together with ``PANELCAD_RELEASE_SLACK_CHANNEL``. Both
files are gitignored and are not in the repository. A checkout, a public
build, or a release built without the variables has neither file.

The webhook value is an incoming webhook (``https://hooks.slack.com/services/...``).
A bot token is refused in that variable and ignored if one is left in the webhook
file. The bot file accepts only an ``xoxb-…`` token. The second line is the
channel id from the build environment. Settings and ``PANELCAD_SLACK_CHANNEL``
do not move that pin. No channel id is stored in this source file.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

ENDPOINT_FILENAME = "_report_endpoint.txt"
BOT_FILENAME = "_report_bot.txt"
RELEASE_WEBHOOK_ENV = "PANELCAD_RELEASE_BUG_WEBHOOK"
RELEASE_BOT_TOKEN_ENV = "PANELCAD_RELEASE_SLACK_BOT_TOKEN"
RELEASE_BOT_CHANNEL_ENV = "PANELCAD_RELEASE_SLACK_CHANNEL"
PUBLIC_BUILD_ENV = "PANELCAD_PUBLIC_BUILD"
# Display name for a build that embedded a bot. Not a workspace id.
TEAM_CHANNEL = "#panelcad-bugs"
_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{8,32}$")

# Incoming webhook only. Each path segment is the id Slack puts in the URL.
_WEBHOOK_RE = re.compile(
    r"^https://hooks\.slack\.com/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+$"
)
_TOKEN_RE = re.compile(r"xox[baprs]-", re.IGNORECASE)
# Split so the source tree does not contain a contiguous bot-token prefix.
_BOT_PREFIX = "xox" + "b-"
_BOT_TOKEN_RE = re.compile("^" + re.escape(_BOT_PREFIX) + r"[A-Za-z0-9-]{8,200}$")
_MAX_URL_LEN = 200

MISSING_ENDPOINT_WARNING = (
    "WARNING: PANELCAD_RELEASE_BUG_WEBHOOK is not set.\n"
    "WARNING: This release ships with no built-in bug-report endpoint.\n"
    "WARNING: Send will save reports on this computer only.\n"
)
MISSING_BOT_WARNING = (
    "WARNING: PANELCAD_RELEASE_SLACK_BOT_TOKEN is not set.\n"
    "WARNING: This release ships with no built-in Slack bot.\n"
    "WARNING: Bug reports will not upload the screenshot or the zip.\n"
)
MISSING_CHANNEL_ERROR = (
    "PANELCAD_RELEASE_SLACK_CHANNEL is required when a bot token is embedded. "
    "Set it in the environment or in release.local.env. "
    "The token was not embedded."
)


def endpoint_path() -> Path:
    """The webhook file next to this module. Missing until a release build writes it."""

    return Path(__file__).resolve().with_name(ENDPOINT_FILENAME)


def bot_path() -> Path:
    """The bot-token file next to this module. Missing until a release build writes it."""

    return Path(__file__).resolve().with_name(BOT_FILENAME)


def is_slack_incoming_webhook(url: str) -> bool:
    text = url.strip()
    if not text or len(text) > _MAX_URL_LEN or _TOKEN_RE.search(text):
        return False
    return _WEBHOOK_RE.fullmatch(text) is not None


def _loopback_http(url: str) -> bool:
    """Test servers. A release build never accepts these."""

    text = url.strip()
    if not text or len(text) > _MAX_URL_LEN or _TOKEN_RE.search(text):
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"127.0.0.1", "localhost"}


def classify_release_webhook(url: str) -> str:
    """Empty when ``url`` may be embedded. Otherwise a reason that omits the value."""

    text = url.strip()
    if not text:
        return "empty"
    if _TOKEN_RE.search(text) or text.lower().startswith("xox"):
        return "bot token"
    if is_slack_incoming_webhook(text):
        return ""
    return "not a https://hooks.slack.com/services/ URL"


def default_webhook_url() -> str:
    """Embedded endpoint, or ``""`` when the file is missing or unusable.

    A build only writes an incoming webhook. At runtime a leftover token is
    ignored. Tests may point the file at a loopback HTTP server.
    """

    line = _first_line(endpoint_path())
    if not line or len(line) > _MAX_URL_LEN or _TOKEN_RE.search(line):
        return ""
    if is_slack_incoming_webhook(line) or _loopback_http(line):
        return line
    return ""


def is_slack_channel_id(channel: str) -> bool:
    """True for a Slack conversation id. Names and tokens are not ids."""

    return _CHANNEL_ID_RE.fullmatch(channel.strip()) is not None


def classify_release_channel(channel: str) -> str:
    """Empty when ``channel`` may be embedded. Otherwise a reason that omits the value."""

    text = channel.strip()
    if not text:
        return "empty"
    if _TOKEN_RE.search(text) or text.lower().startswith("xox"):
        return "bot token"
    if not is_slack_channel_id(text):
        return "not a Slack channel id"
    return ""


def classify_release_bot_token(token: str) -> str:
    """Empty when ``token`` may be embedded. Otherwise a reason that omits the value."""

    text = token.strip()
    if not text:
        return "empty"
    if _BOT_TOKEN_RE.fullmatch(text) is None:
        return "not a Slack bot token"
    return ""


def release_bot_channel_allowed(channel: str) -> bool:
    """True when a build-time channel is a Slack conversation id."""

    return classify_release_channel(channel) == ""


def embedded_channel_id() -> str:
    """Channel id stored on the second line of the bot file, or ``""``."""

    lines = _nonempty_lines(bot_path())
    if len(lines) < 2 or classify_release_channel(lines[1]):
        return ""
    return lines[1]


def default_bot_token() -> str:
    """Embedded Slack bot token, or ``""`` when the file is missing or unusable.

    A token with no channel id on the second line is ignored, so a public
    checkout cannot be pointed at a channel this source file does not name.
    """

    lines = _nonempty_lines(bot_path())
    if not lines or classify_release_bot_token(lines[0]):
        return ""
    if len(lines) < 2 or classify_release_channel(lines[1]):
        return ""
    return lines[0]


def _nonempty_lines(path: Path) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if raw.startswith("\ufeff"):
        raw = raw[1:]
    return [item.strip() for item in raw.splitlines() if item.strip()]


def _first_line(path: Path) -> str:
    lines = _nonempty_lines(path)
    return lines[0] if lines else ""
