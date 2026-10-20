"""AI activity journal + inbox so a live GUI can show what an agent is doing.

The AI never clicks the Qt window. It writes Session ops (or this inbox).
The GUI tails the journal and applies inbox lines to the same Session.
Sidecar files sit next to the project:

    job.pcad.yaml
    job.pcad.ai.jsonl         # activity log (append-only)
    job.pcad.ai-in.jsonl      # commands for the open GUI
    job.pcad.ai-in.cursor     # byte offset the GUI has consumed
    job.pcad.ai-prompt.jsonl  # human prompt line → AI
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "Grok 4.6"


def sidecar(project_path: Path | str | None, suffix: str) -> Path | None:
    if project_path is None:
        return None
    p = Path(project_path)
    from panelcad.diag.paths import is_under_recovery

    # Recovery copies are not the user's project. A journal there is pruned later
    # and would make Save look like it wrote the real file.
    if is_under_recovery(p):
        return None
    name = p.name
    if name.endswith(".yaml"):
        name = name[: -len(".yaml")]
    return p.with_name(name + suffix)


def journal_path(project_path: Path | str | None) -> Path | None:
    return sidecar(project_path, ".ai.jsonl")


def inbox_path(project_path: Path | str | None) -> Path | None:
    return sidecar(project_path, ".ai-in.jsonl")


def cursor_path(project_path: Path | str | None) -> Path | None:
    return sidecar(project_path, ".ai-in.cursor")


def prompt_path(project_path: Path | str | None) -> Path | None:
    return sidecar(project_path, ".ai-prompt.jsonl")


def prompt_cursor_path(project_path: Path | str | None) -> Path | None:
    return sidecar(project_path, ".ai-prompt.cursor")


def now_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_event(
    op: str,
    summary: str,
    *,
    sheet: str = "",
    sheet_title: str = "",
    sheet_kind: str = "",
    actor: dict[str, str] | None = None,
    status: str = "",
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "t": now_stamp(),
        "op": op,
        "summary": summary,
        "sheet": sheet,
        "sheet_title": sheet_title,
        "sheet_kind": sheet_kind,
        "actor": dict(actor or {}),
        "status": status,
    }
    if error:
        event["error"] = error
    if extra:
        event.update(extra)
    return event


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")


def read_jsonl(path: Path | None, limit: int = 200) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def send(project_path: Path | str, msg: dict[str, Any]) -> Path:
    """Append one agent op for the open GUI to apply."""
    dest = inbox_path(project_path)
    if dest is None:
        raise ValueError("no project path")
    append_jsonl(dest, msg)
    return dest


def _read_cursor(project_path: Path) -> int:
    cur = cursor_path(project_path)
    if cur is None or not cur.is_file():
        return 0
    raw = cur.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
        return int(data.get("bytes", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        try:
            return int(raw)
        except ValueError:
            return 0


def _write_cursor(project_path: Path, offset: int) -> None:
    cur = cursor_path(project_path)
    if cur is None:
        return
    cur.write_text(json.dumps({"bytes": offset}), encoding="utf-8")


def peek_inbox(project_path: Path | str | None) -> list[dict[str, Any]]:
    """Unread inbox ops. Does not move the cursor."""

    if project_path is None:
        return []
    path = Path(project_path)
    inbox = inbox_path(path)
    if inbox is None or not inbox.is_file():
        return []
    try:
        data = inbox.read_bytes()
    except OSError:
        return []
    offset = _read_cursor(path)
    if offset > len(data):
        offset = 0
    text = data[offset:].decode("utf-8", errors="replace")
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        payload = line.strip()
        if not payload:
            continue
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict):
            rows.append(msg)
    return rows


def inbox_pending(project_path: Path | str | None) -> bool:
    if project_path is None:
        return False
    path = Path(project_path)
    inbox = inbox_path(path)
    if inbox is None or not inbox.is_file():
        return False
    try:
        return inbox.stat().st_size > _read_cursor(path)
    except OSError:
        return False


def drain_inbox(project_path: Path | str | None, handle) -> list[dict[str, Any]]:
    """Apply new inbox lines via handle(msg) -> reply. handle is Agent.handle."""
    if project_path is None:
        return []
    path = Path(project_path)
    inbox = inbox_path(path)
    if inbox is None or not inbox.is_file():
        return []
    data = inbox.read_bytes()
    offset = _read_cursor(path)
    if offset > len(data):
        offset = 0
    chunk = data[offset:]
    if not chunk:
        return []
    text = chunk.decode("utf-8", errors="replace")
    replies: list[dict[str, Any]] = []
    consumed = 0
    for line in text.splitlines(keepends=True):
        consumed += len(line.encode("utf-8"))
        payload = line.strip()
        if not payload:
            continue
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            replies.append({"ok": False, "error": "invalid json", "raw": payload[:200]})
            continue
        if not isinstance(msg, dict):
            continue
        replies.append(handle(msg))
    _write_cursor(path, offset + consumed)
    return replies


def write_prompt(project_path: Path | str | None, text: str, *, sheet: str = "", sheet_title: str = "") -> dict[str, Any]:
    rec = {
        "t": now_stamp(),
        "text": text,
        "sheet": sheet,
        "sheet_title": sheet_title,
    }
    dest = prompt_path(project_path)
    if dest is not None:
        append_jsonl(dest, rec)
    return rec


def _read_prompt_cursor(project_path: Path) -> int:
    cur = prompt_cursor_path(project_path)
    if cur is None or not cur.is_file():
        return 0
    raw = cur.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
        return int(data.get("bytes", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        try:
            return int(raw)
        except ValueError:
            return 0


def _write_prompt_cursor(project_path: Path, offset: int) -> None:
    cur = prompt_cursor_path(project_path)
    if cur is None:
        return
    cur.write_text(json.dumps({"bytes": offset}), encoding="utf-8")


def drain_prompts(project_path: Path | str | None) -> list[dict[str, Any]]:
    """Unread lines from the human prompt file (AI consumes these)."""
    if project_path is None:
        return []
    path = Path(project_path)
    dest = prompt_path(path)
    if dest is None or not dest.is_file():
        return []
    data = dest.read_bytes()
    offset = _read_prompt_cursor(path)
    if offset > len(data):
        offset = 0
    chunk = data[offset:]
    if not chunk:
        return []
    text = chunk.decode("utf-8", errors="replace")
    rows: list[dict[str, Any]] = []
    consumed = 0
    for line in text.splitlines(keepends=True):
        consumed += len(line.encode("utf-8"))
        payload = line.strip()
        if not payload:
            continue
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            rec = {"text": payload}
        if isinstance(rec, dict):
            rows.append(rec)
    _write_prompt_cursor(path, offset + consumed)
    return rows
