"""Headless Local Grok Build (``grok``) for one scan tile.

Flags used here are named in the CLI reference and the headless guide:

https://docs.x.ai/build/cli/reference
https://docs.x.ai/build/cli/headless-scripting

This environment has no ``grok`` binary, so ``grok --help`` was not run.
A flag that is not in those pages belongs in ``VisionSettings.grok_extra_args``
rather than being guessed. Do not put a credential in that field.

``--prompt-file`` is the headless trigger. The file is a JSON array of ACP
content blocks, so the image stays off the command line. ``--prompt-json`` is
not used: a real tile exceeds the Windows command-line limit. ``-p`` is not
passed either; it is an alternative trigger and would put the prompt in argv.

``--permission-mode dontAsk`` refuses tool calls. ``--tools`` and
``--disallowed-tools`` do not remove the built-in terminal, so they are not
passed. Under dontAsk the built-in read, grep, web, todo, subagent, and skill
tools still run unless they are denied, so the launch also denies Read, Grep,
Glob, Edit, Write, WebFetch, and WebSearch. The image is inline, so the model
does not need to read a file.

``--sandbox strict`` is passed on Linux and macOS. It is omitted on
Windows, where the CLI warns that sandbox is unsupported. ``GROK_HOME`` is
left alone because an empty home is not signed in. PanelCAD does not read,
copy, or store ``auth.json``.

Once per import the job runs ``grok inspect --json`` and writes
``<job tmp>/.grok/config.toml``. That file disables every plugin grok reported
as enabled and every MCP server that is enabled, whatever its source. A
missing ``enabled`` field means the server is on. A failed or unexpected
inspect is logged and the tiles still run. ``--tools none`` is passed only
when the setting is on.

Every tile in the job shares that one temporary directory as ``--cwd``.
Each tile gets its own ``--session-id``. grok stores the session at
``~/.grok/sessions/<url-encoded cwd>/<uuid>``. When cleanup is on, only that
uuid folder is removed. The encoded-cwd parent goes with it when nothing else
is left, including a parent whose only leftover is ``prompt_history.jsonl``.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote

from panelcad.importer.vision import (
    DETECTION_SCHEMA,
    PROMPT_VERSION,
    SHORT_TILE_PROMPT,
    TILE_PROMPT,
    VisionFallback,
    backend_label,
    coerce_detection_body,
    emit_vision_trace,
    partition_detections,
    prepare_detection_rows,
    reply_row_count,
    reread_is_better,
    retry_log,
    should_reread_tile,
)
from panelcad.importer.vision_config import ESTIMATED_TILE_USD, VisionSettings

Runner = Callable[[list[str], str | None, float], tuple[int, str, str]]
log = logging.getLogger(__name__)

SIGN_IN_MESSAGE = (
    "Local Grok Build is not signed in. Run grok login on this PC. "
    "PanelCAD does not store credentials."
)
NOT_FOUND_MESSAGE = (
    "Local Grok Build CLI was not found on PATH or in ~/.grok/bin. "
    "PanelCAD does not store credentials."
)
REMINDER = (
    "Do not use tools. Do not run a terminal or call MCP. "
    "Return only the JSON object with devices, conductors, wire_numbers, and buses. "
    "Do not put a tag or a part type in to_pin."
)
# Replaces the CLI's long default system prompt. The image is already inline.
SYSTEM_PROMPT = (
    "Extract one electrical drawing crop into JSON with devices, conductors, wire_numbers, and buses. "
    "A device needs a tag, a part type, and a box. Do not put those in a pin. "
    "Reply with the JSON object only. "
    "Border grid numbers, letters, and bracketed cross-references such as [9B34] are not wire numbers. "
    "A tag may be number-first: digits, then letters, with an optional hyphen suffix. "
    "That sentence is not a tag printed on this page. "
    "Do not use tools, files, or the terminal."
)

# A bare prefix matches every invocation. dontAsk still runs read/grep/web
# unless these are denied. Names are the ones in the permissions guide.
_DENY = (
    "Bash",
    "Read",
    "Grep",
    "Glob",
    "Edit",
    "Write",
    "WebFetch",
    "WebSearch",
    "MCPTool",
)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_BARE_TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_RETRY_SCALE = 0.5

# Extra arguments must not turn tool approval back on or move the prompt onto argv.
_BLOCKED_FLAGS = {
    "--permission-mode",
    "--always-approve",
    "--yolo",
    "--prompt-json",
    "--prompt-file",
    "--tools",
    "--disallowed-tools",
    "--json-schema",
    "--output-format",
    "--cwd",
    "--effort",
    "--max-turns",
    "--deny",
    "--allow",
    "--sandbox",
    "--system-prompt-override",
    "--session-id",
    "--no-plan",
}
_TAKES_VALUE = {
    "--permission-mode",
    "--prompt-json",
    "--prompt-file",
    "--tools",
    "--disallowed-tools",
    "--json-schema",
    "--output-format",
    "--cwd",
    "--effort",
    "--max-turns",
    "--deny",
    "--allow",
    "--sandbox",
    "--system-prompt-override",
    "--session-id",
}
_SECRET_WORDS = ("api_key", "apikey", "bearer", "secret", "password", "token")
_SIGNIN_MARKERS = (
    "not signed in",
    "not logged in",
    "please log in",
    "please sign in",
    "login required",
    "log in required",
    "authentication required",
    "you are not authenticated",
    "sign in to",
    "run grok login",
)
_TOOL_MARKERS = ("run_terminal", "tool_use", "tool_call", "powershell", "mcp")

def find_grok() -> Path | None:
    """``grok`` on PATH, then ``~/.grok/bin/grok.exe``, then ``~/.grok/bin/grok``."""

    found = shutil.which("grok")
    if found:
        return Path(found)
    home = Path.home() / ".grok" / "bin"
    for name in ("grok.exe", "grok"):
        candidate = home / name
        if candidate.is_file():
            return candidate
    return None


def safe_extra_argv(text: str) -> list[str]:
    """Split optional flags. Drop anything that would weaken dontAsk or carry a secret."""

    raw = str(text or "").strip()
    if not raw:
        return []
    low = raw.lower()
    if any(word in low for word in _SECRET_WORDS):
        return []
    parts = shlex.split(raw, posix=True)
    kept: list[str] = []
    skip_value = False
    for part in parts:
        if skip_value:
            skip_value = False
            continue
        name = part.split("=", 1)[0]
        if name in _BLOCKED_FLAGS or name in {"--always-approve", "--yolo"}:
            if name in _TAKES_VALUE and "=" not in part:
                skip_value = True
            continue
        kept.append(part)
    return kept


class GrokBuildVision:
    """One ``grok`` process per tile. A runner replaces the process in tests."""

    name = "grok-build"

    def __init__(self, settings: VisionSettings, runner: Runner | None = None) -> None:
        self.settings = settings.resolved()
        self._runner = runner
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen[bytes]] = []
        self.cancelled = False
        self.cost_guard_continued = False
        self.spent_usd = 0.0
        self.estimated_usd = 0.0
        self.input_tokens = 0
        self._priced_cost = 0.0
        self._priced_calls = 0
        self.page_started = 0.0
        self._page_clocks: dict[str, float] = {}
        self.calls = 0
        self._work: str | None = None
        self._quiet_ready = False

    def available(self) -> bool:
        return self.unavailable_reason() == ""

    def unavailable_reason(self) -> str:
        if self._runner is not None:
            return ""
        if find_grok() is None:
            return NOT_FOUND_MESSAGE
        return ""

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            procs = list(self._procs)
        for proc in procs:
            _terminate(proc)

    def begin_job(self) -> None:
        """One temporary cwd, one inspect, and a fresh page budget.

        The vision pool must not call this. A cancel during OCR has to stay set.
        """

        with self._lock:
            self.spent_usd = 0.0
            self.estimated_usd = 0.0
            self.input_tokens = 0
            self._priced_cost = 0.0
            self._priced_calls = 0
            self.cancelled = False
            self.cost_guard_continued = False
            self.page_started = time.monotonic()
            self._page_clocks = {}
            self.calls = 0
            if not self._work or not Path(self._work).is_dir():
                self._work = tempfile.mkdtemp(prefix="panelcad-grok-")
            work = Path(self._work)
            ready = self._quiet_ready
        if not ready:
            self._write_quiet(work)
            with self._lock:
                self._quiet_ready = True

    def begin_page(self, page_id: str = "") -> None:
        """Start this page's clock without resetting spend or the grok work directory."""

        now = time.monotonic()
        with self._lock:
            self.page_started = now
            clocks = getattr(self, "_page_clocks", None)
            if not isinstance(clocks, dict):
                clocks = {}
                self._page_clocks = clocks
            clocks[str(page_id or "")] = now

    def allow_cost_guard(self) -> None:
        """Keep reading after the caller was warned and chose to continue."""

        with self._lock:
            self.cost_guard_continued = True

    def job_stop(self) -> str:
        with self._lock:
            cancelled = self.cancelled
            spent = self.spent_usd
            continued = self.cost_guard_continued
        if cancelled:
            return "cancelled"
        if spent > self.settings.max_usd and not continued:
            return "page cost guard"
        return ""

    def finish_job(self) -> None:
        """Drop the shared cwd. Remove its empty session parent when cleanup is on."""

        with self._lock:
            work = self._work
            self._work = None
            self._quiet_ready = False
        if not work:
            return
        path = Path(work)
        if self.settings.clean_grok_sessions and path.exists():
            try:
                resolved = str(path.resolve())
            except OSError:
                resolved = ""
            if resolved:
                root = (Path.home() / ".grok" / "sessions").resolve()
                cwd_text = session_cwd_text(path)
                if cwd_text:
                    _remove_empty_session_parent(root / encoded_cwd_name(path), cwd_text, root)
        shutil.rmtree(path, ignore_errors=True)

    def _write_quiet(self, work: Path) -> None:
        """Disable the plugins and MCP servers ``grok inspect`` reports. Never fatal."""

        try:
            raw = run_grok_inspect(self)
        except Exception as exc:  # noqa: BLE001 — a missed inspect must not stop the import
            log.warning("grok inspect failed (%s). Tiles run without a project config.", exc)
            return
        if not str(raw or "").strip():
            return
        try:
            text = quiet_config_from_inspect(raw)
        except ValueError as exc:
            log.warning("grok inspect JSON was unexpected (%s). Tiles run without a project config.", exc)
            return
        if not text:
            return
        dest = work / ".grok"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "config.toml").write_text(text, encoding="utf-8")

    def over_budget(self, page_id: str | None = None) -> str:
        stop = self.job_stop()
        if stop:
            return stop
        with self._lock:
            if page_id:
                started = getattr(self, "_page_clocks", {}).get(str(page_id), 0.0)
            else:
                started = self.page_started
        if started and (time.monotonic() - started) > self.settings.page_timeout_s:
            return "page timeout"
        return ""

    def extract(self, work: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        meta: dict[str, Any] = {
            "timeout": False,
            "retry_count": 0,
            "retry_status": "none",
            "error": "",
            "body": None,
            "parsed": None,
            "violations": [],
        }
        prompt = ""
        image = Path(str(work.get("image") or ""))
        try:
            reason = self.over_budget(str(work.get("page_id") or "") or None) or self.unavailable_reason()
            if reason:
                meta["error"] = reason
                raise VisionFallback(reason)
            if not image.is_file():
                meta["error"] = "crop missing"
                raise VisionFallback("crop missing")
            encoded, mime = _encode_file(image)
            prompt = _tile_prompt(work)
            first = self._call_once(prompt, encoded, mime, remind=False)
            if first is None:
                meta["timeout"] = True
                meta["retry_count"] = 1
                meta["retry_status"] = "retried"
                reason = self.over_budget(str(work.get("page_id") or "") or None)
                if reason:
                    meta["error"] = reason
                    raise VisionFallback(reason)
                encoded, mime = _encode_scaled(image, _RETRY_SCALE)
                first = self._call_once(prompt, encoded, mime, remind=False)
            if first is None:
                meta["timeout"] = True
                meta["retry_status"] = "timeout"
                meta["error"] = "request timed out"
                raise VisionFallback("request timed out")
            if meta["retry_count"]:
                meta["retry_status"] = "ok"
            if first["signed_out"]:
                meta["error"] = SIGN_IN_MESSAGE
                raise VisionFallback(SIGN_IN_MESSAGE)
            self._charge(first["payload"])
            structured = first["structured"]
            if structured is None and first["tool"]:
                meta["retry_count"] = int(meta["retry_count"]) + 1
                meta["retry_status"] = "retried"
                second = self._call_once(prompt, encoded, mime, remind=True)
                if second is None:
                    meta["timeout"] = True
                    meta["retry_status"] = "timeout"
                    meta["error"] = "request timed out"
                    raise VisionFallback("request timed out")
                if second["signed_out"]:
                    meta["error"] = SIGN_IN_MESSAGE
                    raise VisionFallback(SIGN_IN_MESSAGE)
                self._charge(second["payload"])
                structured = second["structured"]
                first = second
                meta["retry_status"] = "ok"
            meta["body"] = first.get("payload")
            meta["raw_text"] = str(first.get("raw_text") or "")
            if not isinstance(structured, dict):
                meta["error"] = first["reason"] or "no structured answer"
                meta["violations"] = [
                    {
                        "index": 0,
                        "loss": "parse_fail",
                        "rule": "schema",
                        "reason": meta["error"],
                        "row": None,
                    }
                ]
                raise VisionFallback(meta["error"])
            meta["parsed"] = structured
            try:
                valid, violations = partition_detections(structured)
            except VisionFallback as exc:
                meta["error"] = exc.reason
                meta["violations"] = [
                    {"index": 0, "loss": "parse_fail", "rule": "schema", "reason": exc.reason, "row": None}
                ]
                raise
            if should_reread_tile(valid, violations, image):
                # One extra read. A weaker reply is discarded. This does not loop.
                # Status stays "retried". A later "ok" would hide the second call.
                meta["retry_count"] = int(meta["retry_count"]) + 1
                meta["retry_status"] = "retried"
                meta["first_rows"] = reply_row_count(structured)
                second = self._call_once(_short_tile_prompt(work), encoded, mime, remind=False)
                structured2 = second.get("structured") if isinstance(second, dict) else None
                meta["second_rows"] = reply_row_count(structured2)
                if second is not None:
                    self._charge(second["payload"])
                    if not second["signed_out"] and isinstance(structured2, dict):
                        try:
                            valid2, violations2 = partition_detections(structured2)
                        except VisionFallback:
                            valid2 = None
                        if valid2 is not None and reread_is_better(valid, valid2):
                            structured = structured2
                            valid, violations = valid2, violations2
                            first = second
                            prompt = _short_tile_prompt(work)
                            meta["body"] = first.get("payload")
                            meta["raw_text"] = str(first.get("raw_text") or "")
                            meta["parsed"] = structured
            prepared, units = prepare_detection_rows(valid, work, image)
            meta["violations"] = violations
            meta["bbox_units"] = units
            flat = coerce_detection_body(structured)
            reported = list(flat.get("detections") or []) if isinstance(flat, dict) else []
            return {
                "detections": prepared,
                "_reported": reported,
                "_schema_violations": violations,
            }
        finally:
            self._emit_trace(work, prompt, image, meta, time.perf_counter() - started)

    def _emit_trace(
        self,
        work: dict[str, Any],
        prompt: str,
        image: Path,
        meta: dict[str, Any],
        latency_s: float,
    ) -> None:
        """Record the prompt and the model's text. The image bytes and argv stay out."""

        body = meta.get("body") if isinstance(meta.get("body"), dict) else {}
        cost, tokens = _cost_and_tokens(body)
        output_tokens = 0
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        for key in ("completion_tokens", "output_tokens", "outputTokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                output_tokens = int(value)
                break
        reply = str(meta.get("raw_text") or "")
        if not reply:
            from panelcad.importer.vision import raw_model_text

            reply = raw_model_text(body)
        if not reply and isinstance(meta.get("parsed"), dict):
            reply = json.dumps(meta["parsed"])
        emit_vision_trace(
            {
                "prompt_version": PROMPT_VERSION,
                "schema_version": PROMPT_VERSION,
                "model": self.settings.model or "grok",
                "backend": backend_label("grok-build"),
                "preset": "grok-build",
                "latency_s": round(max(0.0, latency_s), 4),
                "tokens": {"input": int(tokens or 0), "output": output_tokens},
                "cost_usd": None if cost is None else round(float(cost), 6),
                "timeout": bool(meta.get("timeout")),
                "timeout_s": float(self.settings.timeout_s),
                "retry": retry_log(meta),
                "error": str(meta.get("error") or ""),
                "request": {
                    "prompt": prompt,
                    "prompt_version": PROMPT_VERSION,
                    "schema_version": PROMPT_VERSION,
                    "schema": DETECTION_SCHEMA,
                    "model": self.settings.model or "grok",
                    "image": str(image) if str(image) else str(work.get("image") or ""),
                },
                "reply": reply,
                "truncated_reply": str(meta.get("truncated_reply") or ""),
                "schema_violations": list(meta.get("violations") or []),
                "bbox_units": list(meta.get("bbox_units") or []),
                "think": None,
                "num_ctx": 0,
                "truncated": bool(meta.get("truncated")),
                "dpi": work.get("dpi"),
            }
        )

    def test_connection(self) -> tuple[bool, str]:
        """``grok models`` so a signed-out CLI is visible. No credential is stored."""

        exe = find_grok()
        if exe is None:
            return False, NOT_FOUND_MESSAGE
        try:
            code, out, err = self._invoke([str(exe), "models"], None, timeout=20.0)
        except TimeoutError:
            return False, "request timed out"
        except FileNotFoundError:
            return False, NOT_FOUND_MESSAGE
        except OSError:
            return False, NOT_FOUND_MESSAGE
        if _signed_out(err, out, parsed=False):
            return False, SIGN_IN_MESSAGE
        if code != 0:
            return False, "grok models failed. If this PC is signed out, run grok login. PanelCAD does not store credentials."
        return True, f"Signed in. Found grok at {exe}. PanelCAD does not store credentials."

    def _call_once(self, prompt: str, encoded: str, mime: str, *, remind: bool) -> dict[str, Any] | None:
        """One launch. ``None`` means the tile timed out and the caller may retry once."""

        try:
            return self._call(prompt, encoded, mime, remind=remind)
        except VisionFallback as exc:
            if exc.reason == "request timed out":
                return None
            raise

    def _call(self, prompt: str, encoded: str, mime: str, *, remind: bool) -> dict[str, Any]:
        blocks: list[dict[str, str]] = [
            {"type": "text", "text": prompt},
            {"type": "image", "data": encoded, "mimeType": mime},
        ]
        if remind:
            blocks.append({"type": "text", "text": REMINDER})
        session_id = str(uuid.uuid4())
        root, owned = self._workdir()
        try:
            prompt_path = root / f"prompt-{session_id}.json"
            prompt_path.write_text(json.dumps(blocks), encoding="utf-8")
            argv = self._argv(prompt_path, root, session_id)
            try:
                code, out, err = self._invoke(argv, str(root))
            except TimeoutError as exc:
                out = str(getattr(exc, "out", "") or "")
                _structured, payload, _parsed = parse_grok_result(out)
                self._charge(payload, missing=True)
                raise VisionFallback("request timed out") from None
        except FileNotFoundError:
            raise VisionFallback(NOT_FOUND_MESSAGE) from None
        except OSError:
            raise VisionFallback(NOT_FOUND_MESSAGE) from None
        finally:
            if self.settings.clean_grok_sessions:
                delete_grok_session(session_id, root)
            if owned:
                shutil.rmtree(root, ignore_errors=True)
        structured, payload, parsed = parse_grok_result(out)
        signed_out = _signed_out(err, out, parsed=parsed)
        tool = structured is None and _tool_attempt(payload, out, err)
        reason = ""
        if structured is None and not signed_out:
            reason = "no structured answer" if tool or code == 0 else f"grok exited {code}"
        raw_text = ""
        if isinstance(payload, dict):
            field = payload.get("structuredOutput")
            if field is None:
                field = payload.get("structured_output")
            if isinstance(field, str) and field.strip():
                raw_text = field
            elif isinstance(field, dict) and _has_detection_rows(field):
                raw_text = json.dumps(field)
            elif isinstance(structured, dict) and _has_detection_rows(structured):
                raw_text = json.dumps(structured)
            else:
                for key in ("text", "result", "output", "response"):
                    piece = payload.get(key)
                    if isinstance(piece, str) and piece.strip():
                        raw_text = piece
                        break
        return {
            "structured": structured,
            "payload": payload,
            "raw_text": raw_text,
            "signed_out": signed_out,
            "tool": tool,
            "reason": reason,
        }

    def _argv(self, prompt_path: Path, cwd: Path, session_id: str) -> list[str]:
        exe = find_grok()
        program = str(exe) if exe is not None else "grok"
        argv = [
            program,
            "--prompt-file",
            str(prompt_path),
            "--json-schema",
            json.dumps(DETECTION_SCHEMA, separators=(",", ":")),
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--effort",
            "low",
            "--max-turns",
            "5",
            "--cwd",
            str(cwd),
            "--no-auto-update",
            "--no-subagents",
            "--no-memory",
            "--no-plan",
            "--disable-web-search",
            "--system-prompt-override",
            SYSTEM_PROMPT,
            "--session-id",
            session_id,
        ]
        argv.extend(_sandbox_argv())
        for name in _DENY:
            argv.extend(["--deny", name])
        if self.settings.grok_tools_none:
            argv.extend(["--tools", "none"])
        argv.extend(safe_extra_argv(self.settings.grok_extra_args))
        return argv

    def _invoke(
        self,
        argv: list[str],
        cwd: str | None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        limit = float(self.settings.timeout_s if timeout is None else timeout)
        if self._runner is not None:
            try:
                return self._runner(argv, cwd, limit)
            except TimeoutError:
                raise
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        with self._lock:
            self._procs.append(proc)
        try:
            try:
                out, err = proc.communicate(timeout=limit)
            except subprocess.TimeoutExpired:
                _terminate(proc)
                try:
                    out_b, err_b = proc.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    out_b, err_b = b"", b""
                raise _InvokeTimeout(
                    out_b.decode("utf-8", "replace"),
                    err_b.decode("utf-8", "replace"),
                ) from None
        finally:
            with self._lock:
                if proc in self._procs:
                    self._procs.remove(proc)
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def _workdir(self) -> tuple[Path, bool]:
        """The job directory, or a private one when ``begin_job`` was not called."""

        with self._lock:
            if self._work and Path(self._work).is_dir():
                return Path(self._work), False
        return Path(tempfile.mkdtemp(prefix="panelcad-grok-")), True

    def _charge(self, payload: Any, *, missing: bool = False) -> None:
        """Add a real cost, or an estimate when a killed call printed no usage."""

        cost, tokens = _cost_and_tokens(payload)
        with self._lock:
            self.calls += 1
            self.input_tokens += tokens
            if cost is None and missing:
                if self._priced_calls:
                    guess = self._priced_cost / self._priced_calls
                else:
                    guess = ESTIMATED_TILE_USD
                self.spent_usd += guess
                self.estimated_usd += guess
                return
            if cost is None:
                return
            self.spent_usd += cost
            if cost > 0:
                self._priced_cost += cost
                self._priced_calls += 1


def parse_grok_result(text: str) -> tuple[Any, dict[str, Any], bool]:
    """Return ``(detections object, payload, parsed)``.

    Reasoning models fill ``structuredOutput``. The headless result also uses
    ``structured_output``. Current builds often leave that field null and put
    the JSON in ``text`` or ``result``. An empty structured object must not
    hide a text body that actually has rows. A JSONL stream keeps the last
    event that carries rows, not an earlier null.
    """

    events = _grok_events(text)
    if not events:
        return None, {}, False
    for event in reversed(events):
        found = _event_detections(event)
        if _has_detection_rows(found):
            return found, event, True
    # A stream can split the JSON across text events. Join before an empty
    # structured object is allowed to count as the answer.
    joined = _coerce_detections("\n".join(_event_text_chunks(events)))
    if _has_detection_rows(joined):
        return joined, events[-1], True
    for event in reversed(events):
        found = _structured_field(event)
        if isinstance(found, dict):
            return found, event, True
    return None, events[-1], True


def _grok_events(text: str) -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    loaded: Any = None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, dict):
        return [loaded]
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        piece = line.strip()
        if not piece:
            continue
        try:
            item = json.loads(piece)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    if events:
        return events
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        item = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [item] if isinstance(item, dict) else []


def _structured_field(event: dict[str, Any]) -> Any:
    for key in ("structuredOutput", "structured_output"):
        if key not in event:
            continue
        value = event.get(key)
        if value in (None, ""):
            continue
        return _coerce_detections(value)
    return None


def _event_text_chunks(events: list[dict[str, Any]]) -> list[str]:
    chunks: list[str] = []
    for event in events:
        for key in ("text", "result", "output", "response", "data"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                chunks.append(value)
        content = event.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    piece = block.get("text") or block.get("data")
                    if isinstance(piece, str) and piece.strip():
                        chunks.append(piece)
                elif isinstance(block, str) and block.strip():
                    chunks.append(block)
    return chunks


def _event_detections(event: dict[str, Any]) -> Any:
    structured = _structured_field(event)
    if _has_detection_rows(structured):
        return structured
    if _has_detection_rows(event):
        flat = coerce_detection_body(event)
        if isinstance(flat, dict) and isinstance(flat.get("detections"), list):
            return flat
    for key in ("text", "result", "output", "response", "data"):
        found = _coerce_detections(event.get(key))
        if _has_detection_rows(found):
            return found
    content = event.get("content")
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            found = _coerce_detections(block.get("text") or block.get("data"))
            if _has_detection_rows(found):
                return found
            piece = block.get("text") or block.get("data")
            if isinstance(piece, str):
                chunks.append(piece)
        if chunks:
            found = _coerce_detections("".join(chunks))
            if _has_detection_rows(found):
                return found
    message = event.get("message")
    if isinstance(message, dict):
        found = _event_detections(message)
        if _has_detection_rows(found):
            return found
    return structured if isinstance(structured, dict) else None


def _detection_body(value: dict[str, Any]) -> dict[str, Any] | None:
    """The detections object, including devices/conductors/wire_numbers/buses."""

    if not any(key in value for key in ("detections", "devices", "conductors", "wire_numbers", "buses", "labels")):
        return None
    flat = coerce_detection_body(value)
    if isinstance(flat, dict) and isinstance(flat.get("detections"), list):
        return flat
    return None


def _coerce_detections(value: Any) -> Any:
    if isinstance(value, dict):
        found_body = _detection_body(value)
        if found_body is not None:
            return found_body
        for key in ("structuredOutput", "structured_output", "text", "result"):
            found = _coerce_detections(value.get(key))
            if isinstance(found, dict) and isinstance(found.get("detections"), list):
                return found
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return _detection_body(data)
    return None


_ROW_KEYS = (
    "tag",
    "wire_number",
    "bus",
    "from_tag",
    "to_tag",
    "from_pin",
    "to_pin",
    "symbol_class",
)


def _has_detection_rows(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    flat = coerce_detection_body(value)
    rows = flat.get("detections") if isinstance(flat, dict) else None
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if any(str(row.get(key) or "").strip() for key in _ROW_KEYS):
            return True
    return False


def _tile_prompt(work: dict[str, Any]) -> str:
    return _prompt_with_box(TILE_PROMPT, work)


def _short_tile_prompt(work: dict[str, Any]) -> str:
    return _prompt_with_box(SHORT_TILE_PROMPT, work)


def _prompt_with_box(text: str, work: dict[str, Any]) -> str:
    page_mm = work.get("page_mm") or {}
    box = {key: page_mm.get(key) for key in ("x", "y", "w", "h")}
    return text + "\n\nThis crop covers page millimetres " + json.dumps(box) + ". Put bbox in crop millimetres."


def _signed_out(stderr: str, stdout: str, *, parsed: bool) -> bool:
    if _has_signin(stderr):
        return True
    if not parsed and _has_signin(stdout):
        return True
    return False


def _has_signin(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _SIGNIN_MARKERS)


def _tool_attempt(payload: Any, stdout: str, stderr: str) -> bool:
    parts = [str(stdout or ""), str(stderr or "")]
    if isinstance(payload, dict):
        stop = str(payload.get("stopReason") or payload.get("stop_reason") or "").strip().lower()
        # dontAsk refuses the call and the CLI reports stopReason "cancelled".
        if stop in {"cancelled", "canceled"} or "tool" in stop:
            return True
        parts.append(json.dumps(payload))
    blob = "\n".join(parts).lower()
    return any(marker in blob for marker in _TOOL_MARKERS)


def _windows() -> bool:
    return sys.platform == "win32"


def _sandbox_argv() -> list[str]:
    """``strict`` on Linux and macOS. Windows warns and does not sandbox."""

    if _windows():
        return []
    return ["--sandbox", "strict"]


class _InvokeTimeout(TimeoutError):
    """A killed grok process. ``out`` is whatever it printed before the kill."""

    def __init__(self, out: str = "", err: str = "") -> None:
        super().__init__("request timed out")
        self.out = out
        self.err = err


def quiet_config_from_inspect(payload: Any) -> str:
    """TOML that turns off enabled plugins and enabled MCP servers.

    Source may be a string or an object. A missing ``enabled`` field means
    the server is on. An empty string means there is nothing to disable. A
    shape that is not the inspect object raises ``ValueError``.
    """

    data = _inspect_object(payload)
    plugins = _enabled_plugin_names(data.get("plugins", []))
    servers = _enabled_mcp_servers(data.get("mcpServers", []))
    lines: list[str] = []
    if plugins:
        lines.append("[plugins]")
        lines.append("disabled = [" + ", ".join(_toml_string(name) for name in plugins) + "]")
    if plugins and servers:
        lines.append("")
    for name in servers:
        key = name if _BARE_TOML_KEY.fullmatch(name) else _toml_string(name)
        lines.append(f"[mcp_servers.{key}]")
        lines.append('command = "cmd"')
        lines.append("enabled = false")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def run_grok_inspect(client: GrokBuildVision) -> str:
    """``grok inspect --json``. A test runner is not an inspect call.

    The runner stands in for tile launches. Feeding it inspect would stall
    tests that treat the first call as a tile. Failure is the caller's to log.
    """

    if client._runner is not None:
        return ""
    exe = find_grok()
    if exe is None:
        raise FileNotFoundError(NOT_FOUND_MESSAGE)
    code, out, err = client._invoke([str(exe), "inspect", "--json"], client._work, timeout=20.0)
    if not str(out or "").strip():
        detail = str(err or "").strip() or f"grok inspect exited {code}"
        raise RuntimeError(detail)
    return out


def _inspect_object(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            raise ValueError("inspect output is empty")
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError:
            data = None
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                break
            if data is None:
                raise ValueError("inspect output is not JSON") from None
    elif isinstance(payload, dict):
        data = payload
    else:
        raise ValueError("inspect output is not an object")
    if not isinstance(data, dict):
        raise ValueError("inspect output is not an object")
    if "plugins" in data and data["plugins"] is not None and not isinstance(data["plugins"], list):
        raise ValueError("plugins is not a list")
    servers = data.get("mcpServers", [])
    if servers is not None and not isinstance(servers, (list, dict)):
        raise ValueError("mcpServers is not a list or object")
    return data


def _enabled_plugin_names(plugins: Any) -> list[str]:
    if plugins is None:
        return []
    if not isinstance(plugins, list):
        raise ValueError("plugins is not a list")
    names: list[str] = []
    for item in plugins:
        if not isinstance(item, dict):
            raise ValueError("plugin entry is not an object")
        if item.get("enabled") is not True:
            continue
        name = str(item.get("name") or item.get("id") or "").strip()
        if name:
            names.append(name)
    return names


def _enabled_mcp_servers(servers: Any) -> list[str]:
    """Every enabled server. ``source`` may be a string or an object."""

    if servers is None:
        return []
    names: list[str] = []
    if isinstance(servers, dict):
        items = [(str(name), value) for name, value in servers.items()]
    elif isinstance(servers, list):
        items = []
        for item in servers:
            if not isinstance(item, dict):
                raise ValueError("mcp server entry is not an object")
            name = str(item.get("name") or item.get("id") or "").strip()
            items.append((name, item))
    else:
        raise ValueError("mcpServers is not a list or object")
    for name, item in items:
        if not isinstance(item, dict):
            raise ValueError("mcp server entry is not an object")
        if item.get("enabled") is False:
            continue
        if name:
            names.append(name)
    return names


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _encode_file(image: Path) -> tuple[str, str]:
    suffix = image.suffix.lower().lstrip(".")
    mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else "image/png"
    encoded = base64.standard_b64encode(image.read_bytes()).decode("ascii")
    return encoded, mime


def _encode_scaled(image: Path, scale: float) -> tuple[str, str]:
    """A smaller PNG for the one timeout retry. The first call keeps the crop."""

    from PIL import Image

    opened = Image.open(image)
    width = max(1, int(opened.width * scale))
    height = max(1, int(opened.height * scale))
    opened = opened.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    opened.save(buf, format="PNG")
    return base64.standard_b64encode(buf.getvalue()).decode("ascii"), "image/png"


def _cost_and_tokens(payload: Any) -> tuple[float | None, int]:
    if not isinstance(payload, dict):
        return None, 0
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost: float | None = None
    usd = payload.get("total_cost_usd")
    if isinstance(usd, (int, float)) and not isinstance(usd, bool):
        cost = float(usd)
    else:
        ticks = usage.get("cost_in_usd_ticks") if isinstance(usage, dict) else None
        if isinstance(ticks, (int, float)) and not isinstance(ticks, bool):
            from panelcad.importer.vision_config import USD_TICKS

            cost = float(ticks) / USD_TICKS
    tokens = 0
    for source in (payload, usage if isinstance(usage, dict) else {}):
        for key in ("input_tokens", "prompt_tokens", "inputTokens"):
            val = source.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                tokens = int(val)
                break
        if tokens:
            break
    return cost, tokens


def session_cwd_text(cwd: Path | str) -> str:
    """Resolved cwd grok encodes, without a Windows ``\\\\?\\`` prefix."""

    try:
        resolved = str(Path(cwd).resolve())
    except OSError:
        return ""
    if resolved.startswith("\\\\?\\"):
        resolved = resolved[4:]
    return resolved


def encoded_cwd_name(cwd: Path | str) -> str:
    """The single path component grok uses under ``~/.grok/sessions``."""

    text = session_cwd_text(cwd)
    if not text:
        return ""
    return quote(text, safe="")


def _path_key(path: Path | str) -> str:
    """Compare session paths when Windows changes case or adds ``\\\\?\\``."""

    text = os.path.normcase(os.path.normpath(str(path)))
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return text


def delete_grok_session(session_id: str, cwd: Path | str | None = None) -> bool:
    """Remove ``sessions/<encoded cwd>/<uuid>``. Never another job's folder.

    The encoded-cwd parent is removed only when it is empty and its decoded
    name is ``cwd``. A bare uuid, a symlink, or a path that leaves the
    sessions root is left alone.
    """

    token = str(session_id or "").strip()
    if cwd is None or not _UUID_RE.fullmatch(token):
        return False
    root = (Path.home() / ".grok" / "sessions").resolve()
    resolved_cwd = session_cwd_text(cwd)
    if not resolved_cwd:
        return False
    parent_name = quote(resolved_cwd, safe="")
    if not parent_name or parent_name in {".", ".."} or "/" in parent_name or "\\" in parent_name:
        return False
    parent = root / parent_name
    if parent.is_symlink() or not parent.is_dir():
        return False
    try:
        resolved_parent = parent.resolve()
    except OSError:
        return False
    if _path_key(resolved_parent.parent) != _path_key(root) or os.path.normcase(unquote(parent.name)) != os.path.normcase(resolved_cwd):
        return False
    folder = parent / token
    if folder.is_symlink() or not folder.is_dir():
        return False
    try:
        if _path_key(folder.resolve().parent) != _path_key(resolved_parent):
            return False
    except OSError:
        return False
    shutil.rmtree(folder)
    _remove_empty_session_parent(parent, resolved_cwd, root)
    return True


def _remove_empty_session_parent(parent: Path, resolved_cwd: str, root: Path | None = None) -> None:
    """Remove this job's encoded-cwd folder when only prompt history remains."""

    if root is None:
        root = (Path.home() / ".grok" / "sessions").resolve()
    if parent.is_symlink() or not parent.is_dir():
        return
    try:
        if _path_key(parent.resolve().parent) != _path_key(root) or os.path.normcase(unquote(parent.name)) != os.path.normcase(resolved_cwd):
            return
        leftovers = list(parent.iterdir())
        if len(leftovers) == 1:
            only = leftovers[0]
            if only.name == "prompt_history.jsonl" and only.is_file() and not only.is_symlink():
                only.unlink()
                leftovers = []
        if leftovers:
            return
        parent.rmdir()
    except OSError:
        return


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """Kill the grok process and the children it started."""

    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            proc.kill()
        except OSError:
            return
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            return
