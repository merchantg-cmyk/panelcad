"""Run a weak model against PanelCAD's JSONL agent CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from panelcad.cli.agent import Agent
from panelcad.sharpen.cards import system_prompt
from panelcad.sharpen.clip import clip_reply
from panelcad.sharpen.parse import ParseError, parse_op
from panelcad.sharpen.tasks import Task, get_task

DEFAULT_MODEL = "gemma3:4b"
DEFAULT_HOST = "http://127.0.0.1:11434"
PACKAGE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_EXAMPLE = PACKAGE_ROOT / "examples" / "dol_starter.pcad.yaml"
SCRATCH_ROOT = PACKAGE_ROOT / ".sharpen"


class ModelClient(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> str: ...


WINDOW_KEEP = 3


class ChatTurn(NamedTuple):
    """One committed op + the RESULT/NEXT the model sees next."""

    op_json: str
    user: str


def compact_state(job: Path) -> dict[str, Any]:
    """YAML truth for the weak model. Pin/wire snapshot, not inspect."""

    path = Path(job)
    if not path.is_file():
        return {"exists": False, "sheets": []}
    from panelcad import Session

    session = Session.open(path)
    sheets: list[dict[str, Any]] = []
    for sheet in session.project.sheets:
        devices: list[dict[str, Any]] = []
        for comp in sheet.components:
            try:
                pins = [p.id for p in session.catalog.get(comp.library_id).pins][:16]
            except KeyError:
                pins = []
            devices.append({"ref": comp.id, "library_id": comp.library_id, "pins": pins})
        wires = [{"from": w.frm, "to": w.to, "net": w.net} for w in sheet.wires]
        wired: set[str] = set()
        for wire in wires:
            for end in (wire["from"], wire["to"]):
                token = str(end or "").split(".")[0]
                if token:
                    wired.add(token)
        sheets.append(
            {
                "id": sheet.id,
                "title": sheet.title,
                "kind": sheet.kind,
                "devices": devices,
                "wires": wires,
                "unwired": [row["ref"] for row in devices if row["ref"] not in wired],
            }
        )
    return {"exists": True, "active": session.active_sheet_id, "sheets": sheets}


def chat_window(
    *,
    system: str,
    task_user: str,
    job: Path,
    recent: list[ChatTurn],
    keep: int = WINDOW_KEEP,
) -> list[dict[str, str]]:
    """System + current STATE + last `keep` rewritten ops. No raw Gemma prose."""

    state = json.dumps(compact_state(job), default=str)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{task_user.rstrip()}\nSTATE: {state}"},
    ]
    for turn in recent[-keep:]:
        messages.append({"role": "assistant", "content": turn.op_json})
        messages.append({"role": "user", "content": turn.user})
    return messages


def _committed_json(msg: dict[str, Any]) -> str:
    skip = {"id", "_harness_error"}
    payload = {k: msg[k] for k in sorted(msg) if k not in skip}
    return json.dumps(payload, default=str)


class ScriptedClient:
    """Deterministic stand-in for tests. Each call pops the next reply."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.calls.append([{"role": m["role"], "content": m["content"]} for m in messages])
        if not self.replies:
            raise RuntimeError("scripted model has no more replies")
        return self.replies.pop(0)


class OllamaClient:
    """Native ``POST /api/chat``. Sharpen and scan vision share this client.

    ``transport`` is the test hook. It receives ``(method, url, payload, headers, timeout)``
    and returns the JSON body. Leave it unset to call Ollama.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        *,
        temperature: float = 0.0,
        num_ctx: int = 4096,
        timeout: float = 180.0,
        images: list[str] | None = None,
        num_predict: int | None = None,
        think: bool | None = None,
        response_format: dict[str, Any] | str | None = None,
        transport: Any = None,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.images = list(images or [])
        self.num_predict = num_predict
        self.think = think
        self.response_format = response_format
        self.transport = transport

    def complete(self, messages: list[dict[str, str]]) -> str:
        payload = self.chat(messages)
        message = payload.get("message") or {}
        return str(message.get("content") or "")

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        images: list[str] | None = None,
        response_format: dict[str, Any] | str | None = None,
        think: bool | None = None,
        num_predict: int | None = None,
    ) -> dict[str, Any]:
        """One native chat call. The return value is Ollama's JSON body."""

        use_images = self.images if images is None else list(images)
        payload_messages: list[dict[str, Any]] = []
        attached = False
        for message in messages:
            entry: dict[str, Any] = {"role": message["role"], "content": message["content"]}
            if not attached and message.get("role") == "user" and use_images:
                entry["images"] = use_images
                attached = True
            payload_messages.append(entry)
        options: dict[str, Any] = {"temperature": self.temperature, "num_ctx": self.num_ctx}
        predict = self.num_predict if num_predict is None else num_predict
        if predict:
            options["num_predict"] = int(predict)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "stream": False,
            "options": options,
        }
        fmt = self.response_format if response_format is None else response_format
        if fmt is not None:
            body["format"] = fmt
        think_flag = self.think if think is None else think
        if think_flag is not None:
            body["think"] = bool(think_flag)
        url = f"{self.host}/api/chat"
        if self.transport is not None:
            payload = self.transport(
                "POST",
                url,
                body,
                {"Content-Type": "application/json", "Accept": "application/json"},
                self.timeout,
            )
            if not isinstance(payload, dict):
                raise RuntimeError("Ollama reply was not a JSON object")
            return payload
        raw = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=raw,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if "exceed_context_size" in detail and self.num_ctx < 16384:
                self.num_ctx = 16384
                return self.chat(messages, images=use_images, response_format=fmt, think=think_flag, num_predict=predict)
            if use_images and ("image" in detail.lower() or "exceed_context_size" in detail):
                self.images = []
                return self.chat(messages, images=[], response_format=fmt, think=think_flag, num_predict=predict)
            raise RuntimeError(
                f"Ollama {exc.code} at {self.host}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Ollama not reachable at {self.host} ({exc}). "
                "Install Ollama, then: ollama pull gemma3:4b"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Ollama reply was not a JSON object")
        return payload


class InProcessAgent:
    """Same Session path as `panelcad agent`, without a subprocess."""

    def __init__(self) -> None:
        self._agent = Agent()

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        return self._agent.handle(msg)

    def close(self) -> None:
        return None


class CliAgent:
    """Real `python -m panelcad agent` JSONL process."""

    def __init__(self, python: str | None = None) -> None:
        exe = python or sys.executable
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("QT_QPA_PLATFORM", None)
        self.proc = subprocess.Popen(
            [exe, "-u", "-m", "panelcad", "agent"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        if self.proc.stdin is None or self.proc.stdout is None:
            raise RuntimeError("failed to start panelcad agent")

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        if self.proc.poll() is not None:
            err = (self.proc.stderr.read() if self.proc.stderr else "") or ""
            raise RuntimeError(f"panelcad agent exited {self.proc.returncode}: {err[-400:]}")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = (self.proc.stderr.read() if self.proc.stderr else "") or ""
            raise RuntimeError(f"panelcad agent closed stdout: {err[-400:]}")
        return json.loads(line)

    def close(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()


@dataclass
class SharpenResult:
    task: str
    ok: bool
    reason: str
    job: Path
    trace: Path
    model: str
    turns: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0


def run_sharpen(
    task_id: str,
    *,
    project: Path | None = None,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    via_cli: bool = False,
    client: ModelClient | None = None,
    scratch: Path | None = None,
    python: str | None = None,
    copy: bool = True,
    fresh: bool = False,
    max_turns: int | None = None,
) -> SharpenResult:
    task = get_task(task_id)
    run_dir = _run_dir(scratch)
    if fresh:
        job = run_dir / "job.pcad.yaml"
        source = job
    elif copy:
        source = Path(project) if project is not None else DEFAULT_EXAMPLE
        if not source.is_file():
            raise FileNotFoundError(f"PanelCAD job not found: {source}")
        job = run_dir / "job.pcad.yaml"
        shutil.copy2(source, job)
    else:
        if project is None:
            raise ValueError("copy=False requires project")
        job = Path(project)
        source = job
        if not job.is_file() and not fresh:
            raise FileNotFoundError(f"PanelCAD job not found: {job}")
    job_s = str(job)
    agent: InProcessAgent | CliAgent = CliAgent(python) if via_cli else InProcessAgent()
    if (
        getattr(task, "skill", None)
        and task.needs_open
        and job.is_file()
        and task.skill != "open"
    ):
        agent.handle({"op": "open", "path": job_s})
        from panelcad.sharpen.skills import prime_skill_session

        prime_skill_session(str(task.skill), agent)
    llm = client or OllamaClient(model=model, host=host)
    system = system_prompt(job_s, task.allowed)
    task_user = _user_task(task, job_s)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task_user},
    ]
    recent: list[ChatTurn] = []
    windowed = bool(getattr(task, "windowed", False))
    limit = max_turns if max_turns is not None else task.max_turns
    turns: list[dict[str, Any]] = []

    def _allowed_now() -> tuple[str, ...]:
        fn = getattr(task, "phase_allowed", None)
        if callable(fn):
            return fn(turns, job)
        return task.allowed

    def _task_user_now() -> str:
        fn = getattr(task, "window_user", None)
        if windowed and callable(fn):
            return fn(turns, job)
        return task_user

    def prompt() -> list[dict[str, str]]:
        if windowed:
            allowed_now = _allowed_now()
            sys = system
            if callable(getattr(task, "phase_allowed", None)):
                sys = system_prompt(job_s, allowed_now, short=True)
            return chat_window(
                system=sys, task_user=_task_user_now(), job=job, recent=recent
            )
        return messages

    def remember(assistant: str, user: str, *, wipe: bool = False) -> None:
        if windowed:
            recent.append(ChatTurn(assistant, user))
            if wipe:
                del recent[:-1]
            else:
                del recent[:-WINDOW_KEEP]
            return
        messages.append({"role": "assistant", "content": assistant})
        messages.append({"role": "user", "content": user})

    started = time.monotonic()
    try:
        for n in range(1, limit + 1):
            raw = llm.complete(prompt())
            turn: dict[str, Any] = {"n": n, "raw": raw}
            try:
                msg = parse_op(raw, allowed=_allowed_now())
            except ParseError as exc:
                turn["parse_error"] = str(exc)
                turns.append(turn)
                err_user = f"ERROR: {exc}"
                if task.nudge:
                    err_user += "\nNEXT: " + task.nudge(turns, job)
                remember(
                    '{"op":"parse_error"}' if windowed else raw,
                    err_user,
                )
                continue
            op = str(msg["op"])
            turn["op"] = op
            if op == "done":
                turn["msg"] = msg
                turn["fp"] = _op_fingerprint(msg)
                ok_now, why = task.check(turns + [turn], job)
                if not ok_now:
                    hint = task.nudge(turns + [turn], job) if task.nudge else why
                    turn["done_rejected"] = why
                    if _repeat_fingerprint(turns, turn["fp"]) >= 2:
                        hint = (
                            "LOOP: done already rejected. Send the missing op, not another done. "
                            + hint
                        )
                    turns.append(turn)
                    err_user = (
                        f"ERROR: done rejected — {why}. "
                        f"{hint} Do not summarize work you did not send as an op."
                    )
                    remember(
                        _committed_json(msg) if windowed else raw,
                        err_user,
                        wipe=windowed and hint.startswith("LOOP:"),
                    )
                    continue
                turns.append(turn)
                break
            msg = _bind_paths(msg, job)
            if task.rewrite:
                msg = task.rewrite(msg, job, turns)
            turn["path_rewritten"] = True
            fp = _op_fingerprint(msg)
            turn["fp"] = fp
            if _repeat_fingerprint(turns, fp) >= 2:
                msg["_harness_error"] = (
                    "LOOP: that exact op already ran twice. "
                    "Do not retry it. Send a different op. Follow NEXT."
                )
            elif _period2_loop(turns, fp):
                msg["_harness_error"] = (
                    "LOOP: you are alternating two ops. "
                    "Do not send either again. Follow NEXT."
                )
            msg["id"] = n
            turn["msg"] = msg
            if msg.get("_harness_error"):
                reply = {"id": n, "ok": False, "error": msg["_harness_error"]}
            else:
                reply = agent.handle(msg)
            turn["reply"] = clip_reply(reply, allowed=task.allowed)
            turns.append(turn)
            if (
                bool(reply.get("ok"))
                and op in {
                    "place",
                    "wire",
                    "cleanup",
                    "set_title_block",
                    "add_sheet",
                    "set_active_sheet",
                    "set_sheet_title",
                    "move",
                    "set_properties",
                    "delete_component",
                    "duplicate_component",
                    "rotate_component",
                    "delete_wire",
                    "set_wire_label",
                    "place_from_bom",
                    "close_sheet",
                    "open_sheet",
                    "delete_sheet",
                    "add_rail",
                    "delete_rail",
                    "set_enclosure",
                    "set_scale",
                    "set_display_size",
                    "set_picture",
                    "clear_picture",
                    "set_pins_locked",
                    "set_pin",
                    "reset_pins",
                    "set_panel",
                    "add_port",
                    "set_port",
                    "delete_port",
                    "set_address",
                    "set_network",
                    "set_pin_labels",
                    "layout_from_schematic",
                    "layout_io",
                    "layout_network",
                }
            ):
                try:
                    agent.handle({"op": "save", "path": job_s})
                except Exception:  # noqa: BLE001
                    pass
            payload = "RESULT: " + json.dumps(turn["reply"], default=str)
            if task.nudge:
                payload += "\nNEXT: " + task.nudge(turns, job)
            err = str((turn.get("reply") or {}).get("error") or "")
            if err.startswith("LOOP:"):
                payload += "\nDo not resend the last JSON. Different op required."
            remember(
                _committed_json(msg) if windowed else raw,
                payload,
                wipe=windowed and err.startswith("LOOP:"),
            )
        else:
            turns.append({"n": limit + 1, "op": "timeout", "raw": ""})
    finally:
        agent.close()

    ok, reason = task.check(turns, job)
    if turns and turns[-1].get("op") == "timeout" and ok:
        reason = "task checks passed but model never sent done"
    result = SharpenResult(
        task=task.id,
        ok=ok,
        reason=reason,
        job=job,
        trace=run_dir / f"{task.id}.trace.jsonl",
        model=model if client is None else type(client).__name__,
        turns=turns,
        elapsed_s=round(time.monotonic() - started, 2),
    )
    _write_trace(result, source)
    return result


def _user_task(task: Task, job: str) -> str:
    if getattr(task, "skill", None):
        return f"{task.instruction}\nJob: {job}\nOne JSON object per turn."
    if task.needs_open:
        start = f"Open this job (use op open):\n{job}\n"
    else:
        start = (
            f'Create a new project, then save to this path:\n{job}\n'
            'Use create_project then save. Do not open another drawing.\n'
        )
    return f"Task {task.id}: {task.instruction}\n{start}Work only on that file."


def _op_fingerprint(msg: dict[str, Any]) -> str:
    """Stable op+args key. id and harness errors do not count as a new op."""

    skip = {"id", "_harness_error"}
    payload = {k: msg[k] for k in sorted(msg) if k not in skip}
    return json.dumps(payload, default=str, sort_keys=True)


def _repeat_fingerprint(turns: list[dict[str, Any]], fp: str) -> int:
    """How many earlier turns already sent this exact op (not only trailing)."""

    return sum(1 for turn in turns if turn.get("fp") == fp)


def _period2_loop(turns: list[dict[str, Any]], fp: str) -> bool:
    """True when the new op would continue A,B,A,B oscillation."""

    if len(turns) < 3 or not fp:
        return False
    last = str(turns[-1].get("fp") or "")
    prev = str(turns[-2].get("fp") or "")
    older = str(turns[-3].get("fp") or "")
    return bool(fp == prev and last == older and fp != last)


def _bind_paths(msg: dict[str, Any], job: Path) -> dict[str, Any]:
    msg = dict(msg)
    op = str(msg.get("op") or "")
    if op in {"open", "save"}:
        msg["path"] = str(job)
        return msg
    if op in {
        "export_pdf",
        "export_svg",
        "export_dxf",
        "export_bom",
        "export_wire_list",
        "export_json",
        "export_package",
        "print_package",
    }:
        names = {
            "export_pdf": "skill.pdf",
            "export_svg": "skill.svg",
            "export_dxf": "skill.dxf",
            "export_bom": "skill.csv",
            "export_wire_list": "wires.csv",
            "export_json": "skill.json",
            "export_package": "pack",
            "print_package": "package.pdf",
        }
        dest = job.with_name(names[op])
        raw = msg.get("path")
        if not raw or Path(str(raw)).resolve() == job.resolve():
            msg["path"] = str(dest)
        return msg
    if op == "set_picture":
        pic = job.parent / "pic.png"
        if pic.is_file():
            msg["path"] = str(pic)
        return msg
    if op == "apply_cut_sheet_pictures" and not msg.get("folder"):
        msg["folder"] = str(job.parent)
    for key in ("path", "folder"):
        raw = msg.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            msg[key] = str((job.parent / path).resolve())
    return msg


def _run_dir(scratch: Path | None) -> Path:
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = SCRATCH_ROOT / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_trace(result: SharpenResult, source: Path) -> None:
    header = {
        "task": result.task,
        "ok": result.ok,
        "reason": result.reason,
        "model": result.model,
        "job": str(result.job),
        "source": str(source),
        "elapsed_s": result.elapsed_s,
        "turns": len(result.turns),
    }
    lines = [json.dumps(header, default=str)]
    for turn in result.turns:
        lines.append(json.dumps(turn, default=str))
    result.trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
