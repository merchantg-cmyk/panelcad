"""Locate a release feed: https URL, drive letter, UNC, or a local file or folder."""

from __future__ import annotations

import html
import http.cookiejar
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from panelcad.update.manifest import FEED_FILENAME, ReleaseManifest, UpdateError, parse_manifest
from panelcad.version import __version__

_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_DRIVE_HOSTS = {
    "drive.google.com",
    "docs.google.com",
    "drive.usercontent.google.com",
}
_HTML_MARKERS = (b"<!doctype", b"<html", b"<head", b"<body")
_HTML_TYPES = {"text/html", "application/xhtml+xml", "application/html"}
_HTML_CAP = 2_000_000
_READ_CHUNK = 1024 * 1024
_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.IGNORECASE | re.DOTALL)
_TAG_ATTRS = re.compile(
    r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""
)
_INPUT_RE = re.compile(r"<input\b([^>]*)/?>", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*(?:"([^"]+)"|'([^']+)')""", re.IGNORECASE)
_DOWNLOAD_URL_RE = re.compile(r'"downloadUrl"\s*:\s*"([^"]+)"')


def is_http_url(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("https://", "http://"))


def is_unc(value: str) -> bool:
    text = value.strip()
    return text.startswith(("\\\\", "//"))


def is_drive_path(value: str) -> bool:
    return bool(_DRIVE.match(value.strip()))


def classify_feed(location: str) -> str:
    """``https``, ``http``, ``unc``, or ``file``. Does not touch the network or the disk."""

    raw = location.strip()
    if not raw:
        raise UpdateError("Feed location is empty.")
    lowered = raw.lower()
    if lowered.startswith("https://"):
        return "https"
    if lowered.startswith("http://"):
        return "http"
    if lowered.startswith("file:"):
        return "file"
    if is_unc(raw):
        return "unc"
    return "file"


def _allowed_url(url: str) -> bool:
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https":
        return bool(parts.hostname)
    host = (parts.hostname or "").lower()
    return parts.scheme == "http" and host in _LOOPBACK


def is_direct_download_url(url: str) -> bool:
    """True for a Google Drive ``uc?export=download&id=`` link, which is the file itself."""

    parts = urllib.parse.urlparse(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = urllib.parse.parse_qs(parts.query)
    exports = [item.lower() for item in query.get("export", [])]
    if "download" in exports:
        return True
    path = (parts.path or "").rstrip("/").lower()
    return host in _DRIVE_HOSTS and path in {"/uc", "/open", "/download"}


def require_remote_download_url(value: str) -> str:
    """Accept an https download URL. The refusal does not repeat the value."""

    text = value.strip()
    parsed = None
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        parsed = None
    if (
        not text
        or parsed is None
        or any(ch.isspace() for ch in text)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise UpdateError(
            "The download URL must be https. "
            "A Google Drive direct link looks like "
            "https://drive.google.com/uc?export=download&id=<id>."
        )
    return text


def _file_url_path(value: str) -> str:
    parsed = urllib.parse.urlparse(value.strip())
    path = urllib.request.url2pathname(parsed.path)
    # Windows file URLs look like file:///C:/dir/feed.json. url2pathname keeps the drive.
    if parsed.netloc and not path.startswith("\\\\"):
        path = "//" + parsed.netloc + path
    return path


def _with_manifest_name(location: str, *, sep: str) -> str:
    text = location.rstrip("/\\")
    if text.lower().endswith(".json"):
        return location
    return text + sep + FEED_FILENAME


def _windows_feed_path(value: str) -> str:
    text = value.strip().strip('"')
    if is_unc(text):
        text = text.replace("/", "\\")
        if not text.startswith("\\\\"):
            text = "\\\\" + text.lstrip("\\")
        return _with_manifest_name(text, sep="\\")
    text = text.replace("/", "\\")
    return _with_manifest_name(text, sep="\\")


def resolve_feed_location(location: str) -> str:
    """Return the manifest URL or path. A folder means ``panelcad-feed.json`` inside it."""

    raw = location.strip().strip('"')
    if not raw:
        raise UpdateError("Feed location is empty.")
    kind = classify_feed(raw)
    if kind in {"https", "http"}:
        if not _allowed_url(raw):
            raise UpdateError(
                "A remote update feed must be https. "
                f"Refusing {raw}."
            )
        if is_direct_download_url(raw):
            return raw
        parts = urllib.parse.urlparse(raw)
        path = parts.path or ""
        if path.lower().endswith(".json"):
            return raw
        base = raw if raw.endswith("/") else raw + "/"
        return base + FEED_FILENAME
    if raw.lower().startswith("file:"):
        raw = _file_url_path(raw)
    if is_unc(raw) or is_drive_path(raw):
        return _windows_feed_path(raw)
    path = Path(raw)
    if path.is_dir():
        return str(path / FEED_FILENAME)
    if path.is_file() or path.suffix.lower() == ".json":
        return str(path)
    return str(path / FEED_FILENAME)


def resolve_download(manifest_location: str, download: str) -> str:
    """Resolve a manifest ``download`` against the manifest's folder or URL."""

    target = download.strip()
    if not target:
        raise UpdateError("The release feed download location is empty.")
    if is_http_url(target):
        if not _allowed_url(target):
            raise UpdateError(
                f"The installer download must be https or a local path. Refusing {target}."
            )
        return target
    if target.lower().startswith("file:"):
        return _file_url_path(target)
    if is_unc(target) or is_drive_path(target) or os.path.isabs(target):
        return target
    if is_http_url(manifest_location):
        return urllib.parse.urljoin(manifest_location, target)
    if is_unc(manifest_location) or is_drive_path(manifest_location) or "\\" in manifest_location:
        parent = manifest_location.replace("/", "\\").rsplit("\\", 1)[0]
        name = target.replace("/", "\\")
        return parent + "\\" + name
    return str(Path(manifest_location).parent / target)


def load_feed_bytes(
    location: str,
    *,
    timeout: float = 20.0,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[bytes, str]:
    """Read the manifest bytes and return them with the resolved location."""

    resolved = resolve_feed_location(location)
    if is_http_url(resolved):
        return http_read(resolved, timeout=timeout, ssl_context=ssl_context), resolved
    path = Path(resolved)
    if not path.is_file():
        raise UpdateError(f"Feed not found: {resolved}")
    try:
        return path.read_bytes(), resolved
    except OSError as exc:
        raise UpdateError(f"Could not read the feed {resolved}: {exc}") from exc


def load_feed(
    location: str,
    *,
    timeout: float = 20.0,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[ReleaseManifest, str]:
    raw, resolved = load_feed_bytes(location, timeout=timeout, ssl_context=ssl_context)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"The release feed is not JSON ({resolved}): {exc}") from exc
    return parse_manifest(data), resolved


def _attr_map(blob: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _TAG_ATTRS.finditer(blob):
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4) or ""
        found[match.group(1).lower()] = html.unescape(value)
    return found


def _url_with_query(url: str, params: list[tuple[str, str]]) -> str:
    parts = urllib.parse.urlparse(url)
    merged = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params:
        merged[key] = value
    query = urllib.parse.urlencode(list(merged.items()))
    return urllib.parse.urlunparse(parts._replace(query=query, fragment=""))


def _same_url(left: str, right: str) -> bool:
    return left.split("#", 1)[0] == right.split("#", 1)[0]


def _form_confirm_urls(text: str, page_url: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for form in _FORM_RE.finditer(text):
        attrs = _attr_map(form.group(1))
        action = attrs.get("action", "").strip()
        if not action or action.lower().startswith(("javascript:", "data:")):
            continue
        params: list[tuple[str, str]] = []
        for inp in _INPUT_RE.finditer(form.group(2)):
            iattrs = _attr_map(inp.group(1))
            name = iattrs.get("name", "").strip()
            if not name:
                continue
            if iattrs.get("type", "hidden").lower() not in {"hidden", "submit"}:
                continue
            params.append((name, iattrs.get("value", "")))
        score = 0
        if attrs.get("id", "").lower() == "download-form":
            score += 10
        if "confirm" in {name.lower() for name, _ in params} or "confirm=" in action.lower():
            score += 5
        if score <= 0:
            continue
        found.append((score, _url_with_query(urllib.parse.urljoin(page_url, action), params)))
    found.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in found]


def _href_confirm_url(text: str, page_url: str) -> str | None:
    for match in _HREF_RE.finditer(text):
        href = html.unescape((match.group(1) or match.group(2) or "").strip())
        lowered = href.lower()
        if not href or lowered.startswith(("javascript:", "data:", "#")):
            continue
        if "confirm=" not in lowered and "export=download" not in lowered and "download?" not in lowered:
            continue
        url = urllib.parse.urljoin(page_url, href).split("#", 1)[0]
        if _same_url(url, page_url) or not _allowed_url(url):
            continue
        return url
    return None


def _download_url_field(text: str) -> str | None:
    match = _DOWNLOAD_URL_RE.search(text)
    if not match:
        return None
    return (
        match.group(1)
        .replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\/", "/")
    )


def _cookie_confirm_url(page_url: str, jar: http.cookiejar.CookieJar) -> str | None:
    token = ""
    for cookie in jar:
        name = cookie.name or ""
        if name == "download_warning" or name.startswith("download_warning_"):
            token = cookie.value or ""
            if token:
                break
    if not token:
        return None
    parts = urllib.parse.urlparse(page_url)
    file_id = (urllib.parse.parse_qs(parts.query).get("id") or [""])[0]
    if not file_id or not parts.hostname:
        return None
    query = urllib.parse.urlencode(
        {"export": "download", "confirm": token, "id": file_id}
    )
    return urllib.parse.urlunparse((parts.scheme, parts.netloc, "/uc", "", query, ""))


def _confirm_url(page: str, page_url: str, jar: http.cookiejar.CookieJar) -> str | None:
    """One Drive confirm hop: the download form, a confirm link, or the warning cookie."""

    text = html.unescape(page)
    candidates = _form_confirm_urls(text, page_url)
    href = _href_confirm_url(text, page_url)
    if href:
        candidates.append(href)
    field = _download_url_field(text)
    if field:
        candidates.append(field.split("#", 1)[0])
    cookie = _cookie_confirm_url(page_url, jar)
    if cookie:
        candidates.append(cookie)
    for url in candidates:
        if _allowed_url(url) and not _same_url(url, page_url):
            return url
    return None


def _looks_like_html(content_type: str, prefix: bytes) -> bool:
    sample = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
    if sample[:64].lower().startswith(_HTML_MARKERS):
        return True
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    return ctype in _HTML_TYPES and sample.startswith(b"<")


def _html_error(url: str) -> UpdateError:
    return UpdateError(
        "The download returned an HTML page instead of the file "
        f"({url}). Google Drive sometimes shows a confirmation or interstitial page "
        "instead of the release. The file was not kept."
    )


def _download_opener(
    ssl_context: ssl.SSLContext | None,
) -> tuple[urllib.request.OpenerDirector, http.cookiejar.CookieJar]:
    jar = http.cookiejar.CookieJar()
    handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(jar)]
    if ssl_context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    return urllib.request.build_opener(*handlers), jar


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": f"PanelCAD/{__version__}"})


def http_read(
    url: str,
    *,
    timeout: float,
    ssl_context: ssl.SSLContext | None = None,
    dest: Path | None = None,
    cancel: Callable[[], bool] | None = None,
) -> bytes:
    """GET an https URL (loopback http is allowed for tests). Optionally write ``dest``.

    Google Drive answers some ``uc?export=download`` links with a redirect and then
    an HTML confirmation page. One confirm hop is followed. A body that is still
    HTML is refused before any sha256 check, and nothing is written to ``dest``.
    """

    if not _allowed_url(url):
        raise UpdateError(f"Refusing to download {url}. Remote files must use https.")
    opener, jar = _download_opener(ssl_context)
    current = url
    hopped = False
    while True:
        if cancel is not None and cancel():
            raise UpdateError("Update cancelled.")
        try:
            response = opener.open(_request(current), timeout=timeout)
        except UpdateError:
            raise
        except urllib.error.HTTPError as exc:
            raise UpdateError(f"Could not download {url}: HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise UpdateError(f"Could not download {url}: {exc.reason}.") from exc
        except OSError as exc:
            raise UpdateError(f"Could not download {url}: {exc}") from exc
        try:
            final = response.geturl()
            if not _allowed_url(final):
                raise UpdateError(f"Refusing redirect from {url} to {final}.")
            content_type = response.headers.get("Content-Type", "")
            prefix = response.read(8192)
            if _looks_like_html(content_type, prefix):
                page = prefix + response.read(_HTML_CAP)
                confirm = None
                if not hopped:
                    confirm = _confirm_url(
                        page.decode("utf-8", errors="replace"),
                        final,
                        jar,
                    )
                if confirm:
                    hopped = True
                    current = confirm
                    continue
                raise _html_error(url)
            if dest is None:
                if cancel is not None and cancel():
                    raise UpdateError("Update cancelled.")
                return prefix + response.read()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as handle:
                handle.write(prefix)
                while True:
                    if cancel is not None and cancel():
                        raise UpdateError("Update cancelled.")
                    chunk = response.read(_READ_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
            return b""
        finally:
            response.close()
