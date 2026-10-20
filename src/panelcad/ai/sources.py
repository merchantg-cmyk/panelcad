"""Fetch a datasheet URL and see whether it actually contains the proposal.

The model may name a URL. A value is cited only when that URL returns 200 and
the page or PDF text contains the value, in millimetres or as an exact inch
conversion (1 in = 25.4 mm). Tests pass ``fetch`` and do not use the network.
"""

from __future__ import annotations

import html
import io
import re
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Callable

_INCH_MM = Decimal("25.4")
_EXACT = Decimal("0.0001")
_MAX_BYTES = 12_000_000

_MM = re.compile(
    r"(?<![\d.])(\d+(?:[.,]\d+)?)\s*(?:mm|millimetres|millimeters|millimetre|millimeter)\b",
    re.IGNORECASE,
)
_INCH = re.compile(
    r"(?<![\d.])(\d+(?:[.,]\d+)?)\s*(inches|inch|in)\b|(?<![\d.])(\d+(?:[.,]\d+)?)\s*(?:\"|″|”)",
    re.IGNORECASE,
)
_INCH_SKIP = re.compile(r"\s+(?:the|a|an|this|that|to|of)\b", re.IGNORECASE)

Fetch = Callable[..., tuple[Any, Any]]


def default_source_fetch(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
    """GET one http(s) URL. The User-Agent is PanelCAD, not a browser or a CLI."""

    from panelcad.version import __version__

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"PanelCAD/{__version__}",
            "Accept": "text/html,application/pdf,application/octet-stream,*/*",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", 200) or 200)
            body = resp.read(_MAX_BYTES + 1)
            ctype = ""
            headers = getattr(resp, "headers", None)
            if headers is not None:
                ctype = str(headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""
    if len(body) > _MAX_BYTES:
        return code, ""
    return code, extract_document_text(ctype, body, url)


def extract_document_text(content_type: str, body: bytes, url: str) -> str:
    """Visible text from an HTML page or a PDF. Empty when the file cannot be read."""

    kind = (content_type or "").lower()
    path = str(url or "").lower().split("?", 1)[0]
    if "pdf" in kind or path.endswith(".pdf") or body.startswith(b"%PDF"):
        return _pdf_text(body)
    decoded = body.decode("utf-8", errors="replace")
    sample = decoded[:4000].lower()
    if "text/html" in kind or "<html" in sample or "<body" in sample:
        return _html_text(decoded)
    return decoded


def verify_citations(items: list[dict[str, str]], fetch: Fetch, timeout: float) -> list[dict[str, str]]:
    """Mark each proposal cited or unverified. Does not invent a value."""

    cache: dict[str, tuple[Any, Any] | None] = {}
    checked: list[dict[str, str]] = []
    for item in items:
        url = str(item.get("source") or "")
        if url not in cache:
            try:
                cache[url] = fetch(url, timeout=timeout)
            except Exception:  # noqa: BLE001 — the proposal stays unverified
                cache[url] = None
        checked.append(_mark(item, cache[url]))
    return checked


def citation_matches(field: str, value: str, text: str) -> bool:
    """True when the datasheet text contains this proposed value."""

    body = str(text or "")
    if not body.strip():
        return False
    if field in {"width_mm", "height_mm", "depth_mm"}:
        return _dimension_in_text(value, body)
    if field == "mount":
        return _mount_in_text(value, body)
    return str(value or "").casefold() in body.casefold()


def _mark(item: dict[str, str], result: tuple[Any, Any] | None) -> dict[str, str]:
    row = dict(item)
    if result is None:
        row["status"] = "unverified"
        row["reason"] = "could not fetch datasheet"
        return row
    try:
        code, text = result
        code_n = int(code)
    except (TypeError, ValueError):
        row["status"] = "unverified"
        row["reason"] = "could not fetch datasheet"
        return row
    if code_n != 200:
        row["status"] = "unverified"
        row["reason"] = f"HTTP {code_n}"
        return row
    body = str(text or "")
    if not body.strip():
        row["status"] = "unverified"
        row["reason"] = "could not read datasheet text"
        return row
    if citation_matches(str(row.get("field") or ""), str(row.get("value") or ""), body):
        row["status"] = "cited"
        row["reason"] = ""
        return row
    row["status"] = "unverified"
    row["reason"] = "value not in datasheet text"
    return row


def _dimension_in_text(value: str, text: str) -> bool:
    try:
        target = Decimal(str(value).replace(",", "."))
    except Exception:  # noqa: BLE001
        return False
    for match in _MM.finditer(text):
        if _same(target, _decimal(match.group(1))):
            return True
    for match in _INCH.finditer(text):
        unit = (match.group(2) or "").lower()
        if unit == "in":
            after = text[match.end() : match.end() + 12]
            if _INCH_SKIP.match(after):
                continue
        number = match.group(1) or match.group(3)
        inches = _decimal(number)
        if inches is None:
            continue
        if _same(target, inches * _INCH_MM):
            return True
    return False


# A bare "panel" (control panel, front panel) is not mounting evidence.
_PANEL_MOUNT = re.compile(
    r"\b(?:"
    r"panel[\s-]*mount(?:ed|ing)?"
    r"|back[\s-]*plate"
    r"|mount(?:ing)?(?:\s+(?:type|method|style))?\s*[:=]\s*panel"
    r")\b"
)


def _mount_in_text(value: str, text: str) -> bool:
    """True when the datasheet states this mounting.

    Panel mounting needs an explicit panel-mount phrase. The word panel on
    its own is not that phrase.
    """

    low = text.casefold()
    if value == "din_rail":
        return bool(re.search(r"\bdin[\s-]*rail\b", low) or re.search(r"\bts[\s-]*35\b", low))
    if value == "panel":
        return _PANEL_MOUNT.search(low) is not None
    return value.casefold() in low


def _same(left: Decimal, right: Decimal | None) -> bool:
    if right is None:
        return False
    return abs(left - right) <= _EXACT


def _decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    try:
        return Decimal(text.replace(",", "."))
    except Exception:  # noqa: BLE001
        return None


def _pdf_text(body: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:  # noqa: BLE001 — an unreadable PDF is not a citation
        return ""


def _html_text(raw: str) -> str:
    text = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)
