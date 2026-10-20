"""Build the zip a person can save, and the Slack text a sender posts.

The channel text includes the description, contact, steps, version, recent
actions, the traceback, and the tail of the log. The zip keeps the full log.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path

from panelcad.diag.log import install_path, pyside_version, read_log_text, tail_log
from panelcad.diag.paths import ensure_dir, reports_dir
from panelcad.diag.redact import redact_path, redact_text
from panelcad.version import __version__


@dataclass
class ReportRequest:
    title: str
    description: str
    steps: str = ""
    contact: str = ""
    include_log: bool = True
    include_system: bool = True
    include_screenshot: bool = False
    include_project: bool = False
    screenshot_png: bytes | None = None
    project_bytes: bytes | None = None
    project_source: str = ""
    traceback: str = ""
    created: str = ""
    crash_id: str = ""
    recent_actions: str = ""


@dataclass
class BuiltReport:
    payload: dict
    summary: str
    files: list[str] = field(default_factory=list)
    log_text: str = ""
    screenshot_png: bytes | None = None
    project_bytes: bytes | None = None


def computer_name() -> str:
    return platform.node().strip()


def machine_name_hash() -> str:
    """Short hash of the computer name. The webhook carries this, not a secret."""

    name = computer_name() or "unknown"
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


_git_sha: str | None = None


def git_sha() -> str:
    """HEAD when this tree is a checkout. ``PANELCAD_GIT_SHA`` wins. Otherwise ``unknown``."""

    override = os.environ.get("PANELCAD_GIT_SHA", "").strip()
    if override:
        return override
    global _git_sha
    if _git_sha is not None:
        return _git_sha
    root = Path(__file__).resolve().parents[3]
    sha = ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0:
        raw = proc.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F]{7,64}", raw):
            sha = raw.lower()
    _git_sha = sha or "unknown"
    return _git_sha


def system_info() -> dict[str, str]:
    return {
        "version": __version__,
        "os": platform.platform(),
        "python": platform.python_version(),
        "pyside6": pyside_version() or "not-loaded",
        "machine": platform.machine(),
        "computer": computer_name(),
        "install_path": redact_path(install_path()),
        "executable": redact_path(Path(__import__("sys").executable)),
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Windows ``tzname()`` returns the long display name. The recovery prompt and
# report.json show the short form, or a UTC offset when the name is unknown.
_TZ_ABBREV = {
    "eastern daylight time": "EDT",
    "eastern standard time": "EST",
    "central daylight time": "CDT",
    "central standard time": "CST",
    "mountain daylight time": "MDT",
    "mountain standard time": "MST",
    "us mountain standard time": "MST",
    "pacific daylight time": "PDT",
    "pacific standard time": "PST",
    "alaskan daylight time": "AKDT",
    "alaskan standard time": "AKST",
    "alaska daylight time": "AKDT",
    "alaska standard time": "AKST",
    "hawaiian standard time": "HST",
    "hawaii standard time": "HST",
    "atlantic daylight time": "ADT",
    "atlantic standard time": "AST",
    "gmt daylight time": "BST",
    "gmt standard time": "GMT",
    "greenwich mean time": "GMT",
    "british summer time": "BST",
    "coordinated universal time": "UTC",
    "india standard time": "IST",
    "china standard time": "CST",
    "tokyo standard time": "JST",
    "aus eastern daylight time": "AEDT",
    "aus eastern standard time": "AEST",
    "e. australia standard time": "AEST",
    "cen. australia standard time": "ACST",
    "w. australia standard time": "AWST",
    "new zealand daylight time": "NZDT",
    "new zealand standard time": "NZST",
}


def _zone_label(local: datetime) -> str:
    name = (local.tzname() or "").strip()
    if not name:
        return local.strftime("%z") or "UTC"
    mapped = _TZ_ABBREV.get(name.lower())
    if mapped:
        return mapped
    if " " not in name and len(name) <= 5:
        return name
    offset = local.utcoffset()
    if offset is None:
        return "UTC"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    minutes = abs(total) // 60
    hours, mins = divmod(minutes, 60)
    if mins:
        return f"UTC{sign}{hours}:{mins:02d}"
    return f"UTC{sign}{hours}"


def local_time_text(stamp: str, *, tz: tzinfo | None = None) -> str:
    """PC local time with a short zone abbreviation. ``created`` stays ISO UTC."""

    raw = str(stamp or "").strip()
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(tz) if tz is not None else moment.astimezone()
    return local.strftime("%Y-%m-%d %H:%M ") + _zone_label(local)


_FRAME_RE = re.compile(r'^File "(?P<path>.+)", line (?P<line>\d+)(?:, in (?P<func>.*?))?$')
_EXC_RE = re.compile(r"^[A-Za-z_][\w.]*(?::.*)?$")


def traceback_summary(text: str) -> tuple[str, str]:
    """Innermost frame and the exception line.

    The frame is ``basename:line in function``. The header
    ``Traceback (most recent call last):`` and source context are left out.
    """

    frame = ""
    exception = ""
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        match = _FRAME_RE.match(stripped)
        if match:
            path = match.group("path")
            base = re.split(r"[\\/]", path)[-1] or path
            func = (match.group("func") or "").strip()
            frame = f"{base}:{match.group('line')}"
            if func:
                frame = f"{frame} in {func}"
            continue
        if raw[:1] in {" ", "\t"}:
            continue
        if _EXC_RE.match(stripped):
            exception = stripped
    return redact_text(frame), redact_text(exception)


def _slug(text: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")
    return out[:40] or "report"


def _clip(text: str, limit: int) -> str:
    raw = text.strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 3] + "..."


# Slack rejects a message past this. The log and traceback shrink first.
POST_TEXT_LIMIT = 40_000
_LOG_TAIL_CHARS = 12_000
_ACTION_LINE = 240
_ACTION_LIMIT = 24


def recent_actions_text(source: object | None, *, limit: int = _ACTION_LIMIT) -> str:
    """Undo depth, undo command names, and the latest activity lines.

    Command names are the steps Ctrl+Z will walk, oldest first. The activity
    log is what the session recorded, including reads that did not change the drawing.
    """

    undo = getattr(source, "_undo", None) if source is not None else None
    redo = getattr(source, "_redo", None) if source is not None else None
    undo_n = len(undo) if isinstance(undo, list) else 0
    redo_n = len(redo) if isinstance(redo, list) else 0
    lines = [f"undo steps: {undo_n}; redo steps: {redo_n}"]
    names: list[str] = []
    named = getattr(source, "undo_command_names", None) if source is not None else None
    if callable(named):
        try:
            names = [str(item).strip() for item in named() if str(item).strip()]
        except Exception:  # noqa: BLE001
            names = []
    if not names and isinstance(undo, list):
        for entry in undo:
            if isinstance(entry, dict):
                label = str(entry.get("name") or "").strip()
                if label:
                    names.append(label)
    if names:
        shown = names[-limit:]
        lines.append("undo commands: " + ", ".join(shown))
    activity = getattr(source, "activity", None) if source is not None else None
    events = activity if isinstance(activity, list) else []
    for event in events[-limit:]:
        if not isinstance(event, dict):
            continue
        stamp = " ".join(str(event.get("t") or "").split())
        op = " ".join(str(event.get("op") or "").split())
        summary = " ".join(str(event.get("summary") or "").split())
        if len(summary) > _ACTION_LINE:
            summary = summary[: _ACTION_LINE - 1] + "…"
        bit = " ".join(part for part in (stamp, op, summary) if part)
        if bit:
            lines.append(bit)
    return "\n".join(lines)


def _fit_blocks(blocks: list[tuple[str, str]], limit: int) -> str:
    """Join sections. Shrink the log and traceback before the description and steps."""

    order = [key for key, _text in blocks]
    texts = {key: text for key, text in blocks}
    shrink = ("log", "traceback", "actions", "description", "steps", "header")

    def render() -> str:
        return "\n\n".join(texts[key] for key in order if texts.get(key, "").strip())

    for key in shrink:
        if key not in texts:
            continue
        guard = 0
        while len(render()) > limit and texts.get(key) and guard < 8:
            guard += 1
            overflow = len(render()) - limit
            body = texts[key]
            keep = len(body) - overflow - 1
            if keep < 16:
                texts[key] = ""
                break
            shortened = body[:keep].rstrip() + "…"
            if len(shortened) >= len(body):
                texts[key] = ""
                break
            texts[key] = shortened
    rendered = render()
    if len(rendered) <= limit:
        return rendered
    if limit <= 1:
        return rendered[:limit]
    return rendered[: limit - 1] + "…"


def message_title(request: ReportRequest) -> str:
    """Short Slack title. An empty title uses the first line of the description."""

    title = " ".join(redact_text(request.title).split())
    if not title:
        description = redact_text(request.description).strip()
        title = description.splitlines()[0].strip() if description else ""
    title = title.replace("*", "")
    return _clip(title, 120) or "PanelCAD bug report"


def build_summary(request: ReportRequest, zip_path: Path | str | None = None) -> str:
    """Channel message for the configured bug-report channel.

    Description, contact, and steps are included in full until the Slack text
    cap. The log tail and traceback shrink first. The zip still holds the
    whole log and the same fields.
    """

    frame, exc_line = traceback_summary(request.traceback)
    info = system_info()
    rows = [
        f"*{message_title(request)}*",
        f"version: {info['version']}",
        f"os: {info['os']}",
    ]
    contact = " ".join(redact_text(request.contact).split())
    if contact:
        rows.append(f"contact: {contact}")
    if request.include_system:
        name = computer_name()
        if name:
            rows.append(f"computer: {name}")
    if frame:
        rows.append(f"where: {frame}")
    if exc_line:
        rows.append(f"exception: {exc_line}")
    crash_id = request.crash_id.strip()
    if crash_id:
        rows.append(f"crash id: {crash_id}")
    rows.append(f"git: {git_sha()}")
    rows.append(f"machine: {machine_name_hash()}")
    if zip_path:
        rows.append(f"saved: {redact_path(zip_path)}")
        name = Path(zip_path).name
        if name:
            rows.append(f"zip: {name}")
    if request.include_screenshot and request.screenshot_png:
        rows.append("screenshot: screenshot.png")

    blocks: list[tuple[str, str]] = [("header", "\n".join(rows))]
    description = redact_text(request.description).strip()
    if description:
        blocks.append(("description", "What happened\n" + description))
    steps = redact_text(request.steps).strip()
    if steps:
        blocks.append(("steps", "Steps to reproduce\n" + steps))
    actions = redact_text(request.recent_actions).strip()
    if actions:
        blocks.append(("actions", "Recent actions\n" + actions))
    trace = redact_text(request.traceback).strip()
    if trace:
        blocks.append(("traceback", "Traceback\n" + trace))
    if request.include_log:
        log = tail_log(max_chars=_LOG_TAIL_CHARS).strip()
        if log:
            blocks.append(("log", "Recent log\n" + log))
    return _fit_blocks(blocks, POST_TEXT_LIMIT)


def build_report(request: ReportRequest) -> BuiltReport:
    files = ["report.json"]
    log_text = ""
    if request.include_log:
        log_text = read_log_text()
        if log_text:
            files.append("log.txt")
    screenshot = request.screenshot_png if request.include_screenshot else None
    if screenshot:
        files.append("screenshot.png")
    project = None
    if request.include_project and request.project_bytes:
        text = request.project_bytes.decode("utf-8", errors="replace")
        project = redact_text(text).encode("utf-8")
        files.append("project.pcad.yaml")
    created = request.created or _now()
    payload: dict = {
        "title": redact_text(request.title.strip()),
        "description": redact_text(request.description.strip()),
        "steps": redact_text(request.steps.strip()),
        "contact": redact_text(request.contact.strip()),
        "version": __version__,
        "created": created,
        "created_local": local_time_text(created),
        "files": list(files),
        "traceback": redact_text(request.traceback.strip()),
        "recent_actions": redact_text(request.recent_actions.strip()),
        "crash_id": request.crash_id.strip(),
        "project_source": redact_path(request.project_source) if request.include_project else "",
        "system": system_info() if request.include_system else None,
    }
    summary = build_summary(request)
    return BuiltReport(
        payload=payload,
        summary=summary,
        files=files,
        log_text=log_text,
        screenshot_png=screenshot,
        project_bytes=project,
    )


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def preview_text(request: ReportRequest) -> str:
    built = build_report(request)
    body = _dump(built.payload)
    names = "\n".join(f"- {name}" for name in built.files)
    summary = build_summary(request, reports_dir() / "bug-<when>.zip")
    return (
        "Slack / webhook message\n"
        f"{summary}\n\n"
        "Zip report.json\n"
        f"{body}\n\n"
        "Files\n"
        f"{names}\n"
    )


def write_report_zip(request: ReportRequest) -> Path:
    built = build_report(request)
    folder = ensure_dir(reports_dir())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = folder / f"bug-{stamp}-{_slug(request.title)}.zip"
    seq = 2
    while dest.exists():
        dest = folder / f"bug-{stamp}-{_slug(request.title)}-{seq}.zip"
        seq += 1
    import zipfile

    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", _dump(built.payload) + "\n")
        if built.log_text:
            log_body = built.log_text if built.log_text.endswith("\n") else built.log_text + "\n"
            archive.writestr("log.txt", log_body)
        if built.screenshot_png:
            archive.writestr("screenshot.png", built.screenshot_png)
        if built.project_bytes is not None:
            archive.writestr("project.pcad.yaml", built.project_bytes)
    return dest


def write_status_sidecar(zip_path: Path, send: dict) -> Path:
    """``<zip>.status.json`` so a saved-only report is obvious next to the zip."""

    results = send.get("results") if isinstance(send.get("results"), list) else []
    reporter = ""
    for row in results:
        if isinstance(row, dict) and row.get("ok") and row.get("reporter"):
            reporter = str(row["reporter"])
            break
    if not reporter:
        for row in results:
            if isinstance(row, dict) and row.get("reporter"):
                reporter = str(row["reporter"])
                break
    if not reporter:
        reporter = str(send.get("reporter") or "")
    status = "sent" if send.get("ok") else "saved_only"
    payload = {
        "status": status,
        "reporter": reporter,
        "time": _now(),
        "error": redact_text(str(send.get("error") or "")),
    }
    dest = Path(str(zip_path) + ".status.json")
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def create_bug_report(
    request: ReportRequest,
    *,
    send: bool = False,
    settings: object | None = None,
    transport: object | None = None,
) -> dict:
    from panelcad.diag.reporters import dispatch_send
    from panelcad.diag.settings import BugSettings

    if not request.description.strip():
        raise ValueError("description is required")
    if not request.title.strip():
        request.title = request.description.strip().splitlines()[0][:120]
    from panelcad.diag.redact import extra_secrets

    secrets: list[str] = []
    if isinstance(settings, BugSettings):
        for value in (settings.webhook_url, settings.relay_url, settings.slack_bot_token):
            text = str(value or "").strip()
            if len(text) >= 8:
                secrets.append(text)
    with extra_secrets(*secrets):
        zip_path = write_report_zip(request)
        summary = build_summary(request, zip_path)
        result: dict = {
            "path": str(zip_path),
            "sent": False,
            "send": {"attempted": False, "ok": False, "error": "", "results": []},
        }
        if send:
            cfg = settings if isinstance(settings, BugSettings) else None
            outcome = dispatch_send(summary, zip_path, settings=cfg, transport=transport)  # type: ignore[arg-type]
            result["send"] = outcome
            result["sent"] = bool(outcome.get("ok"))
        status_path = write_status_sidecar(zip_path, result["send"])
        result["status_path"] = str(status_path)
        from panelcad.diag.log import log_event

        logged = "sent" if result["sent"] else "saved_only"
        log_event("report", f"{logged} {redact_path(zip_path)}")
    return result
