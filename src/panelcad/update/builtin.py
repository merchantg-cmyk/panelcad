"""Build-time default update feed.

``scripts/build-release.py`` reads ``PANELCAD_RELEASE_UPDATE_FEED`` and, when
that value is an https URL, writes it into the release zip as
``_release_feed.txt``. The file is gitignored and is not in the repository.
A checkout or a release built without the variable has no built-in feed.

The checker uses ``PANELCAD_UPDATE_FEED``, then the saved setting, then this
URL. A Google Drive direct link (``uc?export=download&id=``) is a normal https
URL here; the download step follows Drive's confirm page.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

EMBED_FILENAME = "_release_feed.txt"
RELEASE_FEED_ENV = "PANELCAD_RELEASE_UPDATE_FEED"
_MAX_URL_LEN = 2000

MISSING_FEED_WARNING = (
    "WARNING: PANELCAD_RELEASE_UPDATE_FEED is not set.\n"
    "WARNING: This release ships with no built-in update feed.\n"
    "WARNING: The update checker stays off until a feed is set.\n"
)


def feed_path() -> Path:
    """The file next to this module. Missing until a release build writes it."""

    return Path(__file__).resolve().with_name(EMBED_FILENAME)


def classify_release_feed(url: str) -> str:
    """Empty when ``url`` may be embedded. Otherwise a reason that omits the value."""

    text = url.strip()
    if not text:
        return "empty"
    if len(text) > _MAX_URL_LEN or any(ch.isspace() for ch in text):
        return "not an https URL"
    try:
        parsed = urlparse(text)
    except ValueError:
        return "not an https URL"
    if parsed.scheme != "https" or not parsed.hostname:
        return "not an https URL"
    if parsed.username or parsed.password:
        return "not an https URL"
    return ""


def default_feed_url() -> str:
    """Embedded feed, or ``""`` when the file is missing or not https."""

    path = feed_path()
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    line = ""
    for item in raw.splitlines():
        item = item.strip()
        if item:
            line = item
            break
    if classify_release_feed(line):
        return ""
    return line
