"""Canned PanelCAD tasks for the weak operator.

Success checks look at the JSONL trace and the job YAML — not at the
model's English summary. Add a task here when you want to sharpen a
new CLI path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from panelcad import Session
from panelcad.sharpen.cards import STARTER_OPS


@dataclass(frozen=True)
class Task:
    id: str
    instruction: str
    allowed: tuple[str, ...]
    max_turns: int
    check: Callable[[list[dict[str, Any]], Path], tuple[bool, str]]
    nudge: Callable[[list[dict[str, Any]], Path], str] | None = None
    needs_open: bool = True
    rewrite: Callable[..., dict[str, Any]] | None = None
    windowed: bool = False
    skill: str | None = None
    window_user: Callable[[list[dict[str, Any]], Path], str] | None = None
    phase_allowed: Callable[[list[dict[str, Any]], Path], tuple[str, ...]] | None = None


def _ops_seen(turns: list[dict[str, Any]]) -> list[str]:
    return [str(t.get("op")) for t in turns if t.get("op")]


def _ok_reply(turns: list[dict[str, Any]], op: str) -> dict[str, Any] | None:
    for turn in turns:
        if turn.get("op") != op:
            continue
        reply = turn.get("reply") or {}
        if reply.get("ok"):
            return reply
    return None


def _check_list_sheets(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
    reply = _ok_reply(turns, "list_sheets")
    if reply is None:
        return False, "never got an ok list_sheets reply"
    sheets = (reply.get("result") or {}).get("sheets") or []
    ids = {row.get("id") for row in sheets if isinstance(row, dict)}
    need = {"power", "control", "io"}
    missing = need - ids
    if missing:
        return False, f"list_sheets missing {sorted(missing)}; got {sorted(ids)}"
    if "open" not in _ops_seen(turns):
        return False, "did not open the job"
    return True, f"{len(ids)} sheets"


def _place_landed(turns: list[dict[str, Any]]) -> bool:
    for turn in turns:
        if turn.get("op") != "place":
            continue
        reply = turn.get("reply") or {}
        if reply.get("ok") and (reply.get("result") or {}).get("id") == "X20":
            return True
        if "already exists" in str(reply.get("error") or ""):
            return True
    return False


def _props_landed(turns: list[dict[str, Any]]) -> bool:
    for turn in turns:
        if turn.get("op") != "set_properties":
            continue
        reply = turn.get("reply") or {}
        if not reply.get("ok"):
            continue
        props = (reply.get("result") or {}).get("properties") or {}
        if str(props.get("manufacturer") or "") == "Wago" and str(props.get("part_number") or "") == "2002-1201":
            return True
    return False


def _nudge_place_terminal(turns: list[dict[str, Any]], job: Path) -> str:
    if not _place_landed(turns):
        return (
            "X20 is not on sheet io. Next op MUST be: "
            '{"op":"place","library_id":"iec.terminals.x_2p5","ref":"X20","x":50,"y":180,"sheet":"io"}'
        )
    if not _props_landed(turns):
        return (
            "X20 is already placed. Do not place again. Next op MUST be: "
            '{"op":"set_properties","ref":"X20","manufacturer":"Wago","part_number":"2002-1201","sheet":"io"}'
        )
    seen = _ops_seen(turns)
    if "cleanup" not in seen:
        return 'Properties set. Next op MUST be {"op":"cleanup","sheet":"io"}'
    if "validate" not in seen:
        return 'Next op MUST be {"op":"validate"}'
    if "save" not in seen:
        return 'Next op MUST be {"op":"save"}'
    return 'Checks pass. Send {"op":"done","summary":"placed X20"}'


def _check_place_terminal(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
    if not job.is_file():
        return False, f"job missing: {job}"
    session = Session.open(job)
    sheet = session.sheet("io")
    try:
        x20 = sheet.component("X20")
    except Exception as exc:  # noqa: BLE001
        if _place_landed(turns):
            return False, f"X20 was placed in the session but not saved to disk ({exc})"
        return False, f"X20 not on io sheet: {exc}"
    props = x20.properties or {}
    if str(props.get("manufacturer") or "") != "Wago":
        return False, f"X20 manufacturer is {props.get('manufacturer')!r}, want Wago"
    if str(props.get("part_number") or "") != "2002-1201":
        return False, f"X20 part_number is {props.get('part_number')!r}, want 2002-1201"
    if "validate" not in _ops_seen(turns):
        return False, "placed X20 but never validated"
    if "save" not in _ops_seen(turns):
        return False, "never saved"
    return True, "X20 on io, Wago 2002-1201, validated, saved"


def _nudge_title_block(turns: list[dict[str, Any]], job: Path) -> str:
    saw_set = False
    for turn in turns:
        if turn.get("op") == "set_title_block" and (turn.get("reply") or {}).get("ok"):
            saw_set = True
            break
    if not saw_set:
        return (
            "Title block not updated. Next op MUST be: "
            '{"op":"set_title_block","drawing_number":"E-AI-1","rev":"C"}'
        )
    if "save" not in _ops_seen(turns):
        return 'Title block set. Next op MUST be {"op":"save"}'
    return 'Checks pass. Send {"op":"done","summary":"title block E-AI-1 rev C"}'


def _check_title_block(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
    session = Session.open(job)
    tb = session.get_title_block()
    if tb.get("drawing_number") != "E-AI-1":
        return False, f"drawing_number={tb.get('drawing_number')!r}, want E-AI-1"
    revs = tb.get("revisions") or []
    rev = (revs[0] or {}).get("rev") if revs else tb.get("rev")
    if str(rev) != "C":
        return False, f"rev={rev!r}, want C"
    if "save" not in _ops_seen(turns):
        return False, "title block set but never saved"
    return True, "title block E-AI-1 rev C"


LIST_SHEETS = Task(
    id="list-sheets",
    instruction=(
        'Open the job. List every sheet (id, title, kind). '
        'Then send {"op":"done"}. Do not place parts. Do not change the drawing.'
    ),
    allowed=STARTER_OPS,
    max_turns=8,
    check=_check_list_sheets,
)

PLACE_TERMINAL = Task(
    id="place-terminal",
    instruction=(
        "Do these ops in order. One JSON object per turn. Do not skip place.\n"
        "1. open the scratch job\n"
        "2. place: library_id=iec.terminals.x_2p5 ref=X20 x=50 y=180 sheet=io\n"
        "3. set_properties: ref=X20 manufacturer=Wago part_number=2002-1201 sheet=io\n"
        "4. cleanup sheet=io\n"
        "5. validate\n"
        "6. save\n"
        "7. done\n"
        "A done summary is not enough — X20 must exist in the YAML. "
        "Do not invent other devices."
    ),
    allowed=STARTER_OPS,
    max_turns=16,
    check=_check_place_terminal,
    nudge=_nudge_place_terminal,
)

TITLE_BLOCK = Task(
    id="title-block",
    instruction=(
        "Do these ops in order. One JSON object per turn.\n"
        "1. open the scratch job\n"
        "2. set_title_block drawing_number=E-AI-1 rev=C\n"
        "3. save\n"
        "4. done\n"
        "Do not place parts. A done summary is not enough — the YAML must change."
    ),
    allowed=STARTER_OPS,
    max_turns=10,
    check=_check_title_block,
    nudge=_nudge_title_block,
)

TASKS: dict[str, Task] = {
    LIST_SHEETS.id: LIST_SHEETS,
    PLACE_TERMINAL.id: PLACE_TERMINAL,
    TITLE_BLOCK.id: TITLE_BLOCK,
}


def all_tasks() -> dict[str, Task]:
    from panelcad.sharpen.from_print import from_print_task
    from panelcad.sharpen.skills import skill_tasks
    from panelcad.sharpen.suite import SUITE_TASKS

    task = from_print_task()
    return {**TASKS, **SUITE_TASKS, **skill_tasks(), task.id: task}


def get_task(task_id: str) -> Task:
    blob = all_tasks()
    try:
        return blob[task_id]
    except KeyError as exc:
        known = ", ".join(blob)
        raise ValueError(f"unknown task '{task_id}'. Known: {known}") from exc
