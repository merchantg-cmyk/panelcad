"""Pluggable bug-report senders.

A Slack bot with files:write uploads the screenshot and the report zip with
files.getUploadURLExternal and files.completeUploadExternal (files.upload is
retired). The message is the full report text. A comment longer than
``INITIAL_COMMENT_LIMIT`` is posted with chat.postMessage (chat:write) as a
reply on that file message, because completeUploadExternal does not document a
40,000-character initial_comment and can be called only once. If the upload
does not post, the webhook posts the same text. On a partial upload the
fallback text says the zip was not uploaded. The webhook URL and the bot token
come from the environment, then settings, then the release's embedded files.
The embedded token is pinned to the channel id written at build time.
``PANELCAD_BUG_RELAY`` (or
``bug_reports.relay_url``) receives that same JSON instead of the webhook host.
This module does not run a relay server.
"""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlencode

from panelcad.diag.endpoint import TEAM_CHANNEL, embedded_channel_id
from panelcad.diag.redact import redact_secrets, redact_text
from panelcad.diag.settings import BugSettings

SLACK_API = "https://slack.com/api"
SLACK_TEXT_LIMIT = 40_000
# chat.postMessage truncates above 40_000 characters. files.completeUploadExternal
# describes initial_comment as message text and does not document that limit.
# The method can be called only once, so a rejected comment drops the files.
# Longer reports use a short caption and a chat.postMessage thread reply.
INITIAL_COMMENT_LIMIT = 4_000
ZIP_NOT_UPLOADED = "The report zip was not uploaded."
SEND_LIMIT = 5
SEND_WINDOW_SECONDS = 10 * 60
_api_base = SLACK_API


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout: float = 30.0,
    ) -> tuple[int, bytes]:
        """Return HTTP status and response body. Do not follow a secret into a log."""


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout: float = 30.0,
    ) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        opener = _opener_for(url)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return int(resp.status), resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            return int(exc.code), raw


def _opener_for(url: str):
    import urllib.request

    local = url.startswith("http://127.0.0.1") or url.startswith("http://localhost")
    if local:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


@dataclass
class SendResult:
    ok: bool
    reporter: str
    error: str = ""
    detail: str = ""
    posted: bool = False
    uploaded: bool = False
    partial: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "ok": self.ok,
            "reporter": self.reporter,
            "error": redact_text(self.error),
            "detail": redact_text(self.detail),
            "posted": bool(self.ok and self.posted),
            "uploaded": bool(self.ok and self.uploaded),
        }


class Reporter(Protocol):
    name: str

    def send(self, summary: str, zip_path: Path | None = None) -> SendResult:
        """Post ``summary``. Upload ``zip_path`` when the transport can carry a file."""


ReporterFactory = Callable[[BugSettings, Transport | None], Reporter | None]

_factories: list[ReporterFactory] = []


def register_reporter(factory: ReporterFactory) -> None:
    """Add a sender. Return None from the factory when it is not configured."""

    _factories.append(factory)


def set_slack_api_base(url: str | None) -> None:
    """Point the bot at a local server in tests. Production stays on slack.com."""

    global _api_base
    _api_base = (url or SLACK_API).rstrip("/")


def clear_extra_reporters() -> None:
    """Test helper. Leaves the built-in webhook and bot factories in place."""

    set_slack_api_base(None)
    del _factories[2:]


def reporters_for(settings: BugSettings, transport: Transport | None = None) -> list[Reporter]:
    found: list[Reporter] = []
    for factory in _factories:
        item = factory(settings, transport)
        if item is not None:
            found.append(item)
    return found


def _clock() -> float:
    return time.time()


def rate_limit_path() -> Path:
    from panelcad.diag.paths import reports_dir

    return reports_dir() / "send-rate.json"


def claim_send_slot(now: float | None = None) -> str:
    """Record one send. Return a reason when this install is over the limit.

    Five sends per ten minutes. Older stamps are dropped. A broken file does
    not block the report.
    """

    moment = _clock() if now is None else now
    path = rate_limit_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    stamps: list[float] = []
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            payload = {}
        raw = payload.get("times") if isinstance(payload, dict) else None
        if isinstance(raw, list):
            for item in raw:
                try:
                    stamps.append(float(item))
                except (TypeError, ValueError):
                    continue
    window = moment - SEND_WINDOW_SECONDS
    recent = [item for item in stamps if item >= window]
    if len(recent) >= SEND_LIMIT:
        minutes = SEND_WINDOW_SECONDS // 60
        return f"rate limit: {SEND_LIMIT} reports in {minutes} minutes"
    recent.append(moment)
    try:
        path.write_text(json.dumps({"times": recent}) + "\n", encoding="utf-8")
    except OSError:
        return ""
    return ""


def dispatch_send(
    summary: str,
    zip_path: Path | None,
    *,
    settings: BugSettings | None = None,
    transport: Transport | None = None,
) -> dict[str, object]:
    from panelcad.diag.settings import load_effective_settings

    cfg = settings.with_env() if settings is not None else load_effective_settings()
    shown = str(zip_path or "")
    saved = redact_text(shown)
    outcome: dict[str, object] = {
        "attempted": True,
        "ok": False,
        "error": "",
        "results": [],
        "endpoint": cfg.webhook_source,
        "disabled": cfg.disabled,
        "destination": cfg.destination_name(),
        "reporter": "",
    }
    if cfg.disabled:
        outcome["error"] = "reporting turned off"
        outcome["message"] = delivery_message(outcome, shown)
        return outcome
    active = reporters_for(cfg, transport)
    if not active:
        outcome["error"] = f"Bug reports are not configured. Saved {saved}"
        outcome["message"] = delivery_message(outcome, shown)
        return outcome
    limited = claim_send_slot()
    if limited:
        outcome["error"] = limited
        outcome["reporter"] = "webhook" if _webhook_target(cfg) else "slack-bot"
        outcome["message"] = delivery_message(outcome, shown)
        return outcome
    results: list[dict[str, str | bool]] = []
    bots = [item for item in active if getattr(item, "name", "") == "slack-bot"]
    hooks = [item for item in active if getattr(item, "name", "") == "webhook"]
    extras = [item for item in active if getattr(item, "name", "") not in {"slack-bot", "webhook"}]
    primary_ok = False
    hook_summary = summary
    for reporter in bots:
        item = _invoke_reporter(reporter, summary, zip_path)
        results.append(item.as_dict())
        if item.partial and ZIP_NOT_UPLOADED not in hook_summary:
            hook_summary = f"{ZIP_NOT_UPLOADED}\n{hook_summary}"
        if item.ok:
            primary_ok = True
            break
    # The webhook is the text fallback when the bot upload did not post, and
    # the sender on an install that has no bot token.
    if hooks and not primary_ok:
        for reporter in hooks:
            item = _invoke_reporter(reporter, hook_summary, zip_path)
            results.append(item.as_dict())
            if item.ok:
                primary_ok = True
                break
    elif not bots and not hooks:
        primary_ok = True
    extra_ok = True
    for reporter in extras:
        item = _invoke_reporter(reporter, summary, zip_path)
        results.append(item.as_dict())
        if not item.ok:
            extra_ok = False
    ok = bool(results) and primary_ok and extra_ok
    error = ""
    if not ok:
        errors = [f"{row['reporter']}: {row['error']}" for row in results if not row.get("ok")]
        error = "; ".join(errors)
        if saved:
            error = f"{error}. Report saved at {saved}"
    outcome["ok"] = ok
    outcome["error"] = redact_text(error)
    outcome["results"] = results
    outcome["uploaded"] = any(bool(row.get("uploaded")) for row in results)
    outcome["message"] = delivery_message(outcome, shown)
    return outcome


def _invoke_reporter(reporter: Reporter, summary: str, zip_path: Path | None) -> SendResult:
    try:
        return reporter.send(summary, zip_path)
    except Exception as exc:  # noqa: BLE001
        return SendResult(ok=False, reporter=getattr(reporter, "name", "reporter"), error=str(exc))


class WebhookReporter:
    """Incoming webhook. Text only — the screenshot and zip need the bot token."""

    name = "webhook"

    def __init__(self, url: str, transport: Transport | None = None) -> None:
        self._url = url.strip()
        self._transport = transport or UrllibTransport()

    def send(self, summary: str, zip_path: Path | None = None) -> SendResult:
        # Webhooks cannot attach a file. The text is the report; the zip stays local.
        document = slack_document(summary, zip_path)
        body = json.dumps(document).encode("utf-8")
        try:
            status, raw = self._transport.request(
                "POST",
                self._url,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, reporter=self.name, error=redact_secrets(str(exc)))
        if status < 200 or status >= 300:
            return SendResult(ok=False, reporter=self.name, error=f"webhook HTTP {status}")
        text = raw.decode("utf-8", errors="replace").strip()
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and payload.get("ok") is False:
                return SendResult(
                    ok=False,
                    reporter=self.name,
                    error=str(payload.get("error") or "webhook rejected the report"),
                )
        return SendResult(ok=True, reporter=self.name, detail="posted text summary", posted=True)


class SlackBotReporter:
    """Bot token upload: getUploadURLExternal, POST the bytes, completeUploadExternal.

    The screenshot is its own image file. The zip is a second file on the same
    message. ``files.upload`` is not called.
    """

    name = "slack-bot"

    def __init__(
        self,
        token: str,
        channel: str,
        transport: Transport | None = None,
        api_base: str = SLACK_API,
    ) -> None:
        self._token = token.strip()
        self._channel = channel.strip()
        self._transport = transport or UrllibTransport()
        self._api = api_base.rstrip("/")

    def send(self, summary: str, zip_path: Path | None = None) -> SendResult:
        if zip_path is None or not Path(zip_path).is_file():
            return SendResult(ok=False, reporter=self.name, error="no report zip to upload")
        path = Path(zip_path)
        try:
            files = _upload_files(path)
        except OSError as exc:
            return SendResult(ok=False, reporter=self.name, error=redact_text(str(exc)))
        if not files:
            return SendResult(ok=False, reporter=self.name, error="no report zip to upload")
        comment = slack_document(summary, path)["text"]
        uploaded: list[tuple[str, str]] = []
        failure: SendResult | None = None
        for filename, data in files:
            ticket = self._open_upload(filename, len(data))
            if isinstance(ticket, SendResult):
                failure = ticket
                break
            upload_url, file_id = ticket
            posted = self._put_bytes(upload_url, data)
            if posted is not None:
                failure = posted
                break
            uploaded.append((file_id, filename))
        if failure is not None:
            zip_done = any(name == path.name for _file_id, name in uploaded)
            if uploaded and not zip_done:
                # The screenshot can still be shared. The zip was not.
                self._complete(uploaded, _title_line(comment))
                failure.partial = True
            return failure
        return self._complete(uploaded, comment)

    def _headers(self, content_type: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": content_type,
        }

    def _open_upload(self, filename: str, length: int) -> tuple[str, str] | SendResult:
        body = urlencode({"filename": filename, "length": str(length)}).encode("utf-8")
        try:
            status, raw = self._transport.request(
                "POST",
                f"{self._api}/files.getUploadURLExternal",
                headers=self._headers("application/x-www-form-urlencoded"),
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, reporter=self.name, error=redact_text(str(exc)))
        payload = _json_obj(raw)
        if status < 200 or status >= 300 or not payload.get("ok"):
            return SendResult(
                ok=False,
                reporter=self.name,
                error=str(payload.get("error") or f"getUploadURLExternal HTTP {status}"),
            )
        upload_url = str(payload.get("upload_url") or "")
        file_id = str(payload.get("file_id") or "")
        if not upload_url or not file_id:
            return SendResult(ok=False, reporter=self.name, error="upload URL missing")
        return upload_url, file_id

    def _put_bytes(self, upload_url: str, data: bytes) -> SendResult | None:
        try:
            status, _raw = self._transport.request(
                "POST",
                upload_url,
                headers={"Content-Type": "application/octet-stream"},
                body=data,
            )
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, reporter=self.name, error=redact_text(str(exc)))
        if status < 200 or status >= 300:
            return SendResult(ok=False, reporter=self.name, error=f"upload HTTP {status}")
        return None

    def _complete(self, files: list[tuple[str, str]], summary: str) -> SendResult:
        caption, thread_text = _file_caption(summary)
        body = json.dumps(
            {
                "files": [{"id": file_id, "title": title} for file_id, title in files],
                "channel_id": _slack_channel_id(self._channel),
                "initial_comment": caption,
            }
        ).encode("utf-8")
        try:
            status, raw = self._transport.request(
                "POST",
                f"{self._api}/files.completeUploadExternal",
                headers=self._headers("application/json; charset=utf-8"),
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, reporter=self.name, error=redact_text(str(exc)))
        payload = _json_obj(raw)
        if status < 200 or status >= 300 or not payload.get("ok"):
            return SendResult(
                ok=False,
                reporter=self.name,
                error=str(payload.get("error") or f"completeUploadExternal HTTP {status}"),
            )
        if thread_text:
            follow = self._post_thread(thread_text, _share_ts(payload))
            if follow is not None:
                return follow
        detail = files[-1][0] if files else ""
        return SendResult(ok=True, reporter=self.name, detail=detail, posted=True, uploaded=True)

    def _post_thread(self, text: str, thread_ts: str) -> SendResult | None:
        """Post the full report as a reply. None when Slack accepted it."""

        payload: dict[str, str] = {
            "channel": _slack_channel_id(self._channel),
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        body = json.dumps(payload).encode("utf-8")
        try:
            status, raw = self._transport.request(
                "POST",
                f"{self._api}/chat.postMessage",
                headers=self._headers("application/json; charset=utf-8"),
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, reporter=self.name, error=redact_text(str(exc)))
        parsed = _json_obj(raw)
        if status < 200 or status >= 300 or not parsed.get("ok"):
            return SendResult(
                ok=False,
                reporter=self.name,
                error=str(parsed.get("error") or f"chat.postMessage HTTP {status}"),
            )
        return None


def _slack_channel_id(channel: str) -> str:
    """Map the built-in channel name to the id embedded at build time.

    Any other value is used as given. A public build has no embedded id, so
    the name is not rewritten.
    """

    text = channel.strip()
    pinned = embedded_channel_id()
    if pinned and text in {TEAM_CHANNEL, TEAM_CHANNEL.lstrip("#"), pinned}:
        return pinned
    return text


def _screenshot_from_zip(path: Path) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                data = archive.read("screenshot.png")
            except KeyError:
                return None
    except (OSError, zipfile.BadZipFile):
        return None
    return data or None


def _upload_files(path: Path) -> list[tuple[str, bytes]]:
    """Image first, then the zip, so Slack shows the screenshot on the message."""

    files: list[tuple[str, bytes]] = []
    shot = _screenshot_from_zip(path)
    if shot:
        files.append(("screenshot.png", shot))
    data = path.read_bytes()
    if data:
        files.append((path.name, data))
    return files


def _title_line(summary: str) -> str:
    for line in summary.splitlines():
        if line.strip():
            return cap_slack_text(line.strip(), INITIAL_COMMENT_LIMIT)
    return "PanelCAD bug report"


def _file_caption(summary: str) -> tuple[str, str]:
    """``(initial_comment, thread text)``. The thread text is empty when it fits."""

    if len(summary) <= INITIAL_COMMENT_LIMIT:
        return summary, ""
    first = _title_line(summary)
    note = "Full report is in the thread."
    caption = f"{first}\n{note}" if first else note
    return cap_slack_text(caption, INITIAL_COMMENT_LIMIT), summary


def _share_ts(payload: dict) -> str:
    """Message ts from a files.completeUploadExternal file share, if Slack sent one."""

    files = payload.get("files")
    if not isinstance(files, list):
        return ""
    for item in files:
        if not isinstance(item, dict):
            continue
        shares = item.get("shares")
        if not isinstance(shares, dict):
            continue
        for group in shares.values():
            if not isinstance(group, dict):
                continue
            for entries in group.values():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict):
                        ts = str(entry.get("ts") or "").strip()
                        if ts:
                            return ts
    return ""


def cap_slack_text(text: str, limit: int = SLACK_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def slack_document(summary: str, zip_path: Path | None = None) -> dict[str, str]:
    """JSON a Slack incoming webhook or a relay accepts. The URL is not included."""

    from panelcad.diag.report import git_sha, machine_name_hash
    from panelcad.version import __version__

    filename = Path(zip_path).name if zip_path else ""
    version = __version__
    sha = git_sha()
    machine = machine_name_hash()
    body = redact_secrets(summary).strip()
    extras: list[str] = []
    if f"version: {version}" not in body:
        extras.append(f"version: {version}")
    if f"git: {sha}" not in body:
        extras.append(f"git: {sha}")
    if f"machine: {machine}" not in body:
        extras.append(f"machine: {machine}")
    if filename and f"zip: {filename}" not in body:
        extras.append(f"zip: {filename}")
    footer = "\n".join(extras)
    if footer:
        room = SLACK_TEXT_LIMIT - len(footer) - (1 if body else 0)
        if room < 1:
            text = cap_slack_text(footer)
        else:
            clipped = cap_slack_text(body, room)
            text = f"{clipped}\n{footer}" if clipped else footer
    else:
        text = cap_slack_text(body)
    return {
        "text": text,
        "version": version,
        "git": sha,
        "machine": machine,
        "zip": filename,
    }


def delivery_message(send: dict, path: str) -> str:
    """One status line for the dialog. The webhook URL is never included."""

    if send.get("disabled"):
        return "Saved only (reporting turned off)"
    shown = path.strip()
    if not send.get("attempted"):
        lines = ["Saved on this computer:"]
        if shown:
            lines.append(shown)
        return "\n".join(lines)
    team = send.get("destination") == "PanelCAD team"
    if send.get("ok") and team:
        return f"Sent to the built-in bug-report channel. Zip saved at {shown}"
    if send.get("ok"):
        return f"Sent. Zip saved at {shown}"
    reason = _failure_reason(send)
    if shown:
        return f"Not sent: {reason}. Saved only at {shown}"
    return f"Not sent: {reason}."


def _failure_reason(send: dict) -> str:
    raw = str(send.get("error") or "").strip()
    for marker in (". Report saved", ". Saved "):
        if marker in raw:
            raw = raw.split(marker, 1)[0].strip()
            break
    return redact_secrets(raw) or "send failed"


def _json_obj(raw: bytes) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _webhook_target(settings: BugSettings) -> str:
    """Relay when set, otherwise the resolved webhook. The zip is not posted."""

    relay = settings.relay_url.strip()
    if relay:
        return relay
    return settings.webhook_url.strip()


def _webhook_factory(settings: BugSettings, transport: Transport | None) -> Reporter | None:
    if settings.disabled:
        return None
    url = _webhook_target(settings)
    if not url:
        return None
    return WebhookReporter(url, transport=transport)


def _bot_factory(settings: BugSettings, transport: Transport | None) -> Reporter | None:
    if settings.disabled:
        return None
    token = settings.slack_bot_token.strip()
    channel = settings.slack_channel.strip()
    if not token or not channel:
        return None
    return SlackBotReporter(token, channel, transport=transport, api_base=_api_base)


register_reporter(_webhook_factory)
register_reporter(_bot_factory)
