"""Sequence tasks: one required op after another, with an exact NEXT JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panelcad.sharpen.tasks import Task


@dataclass(frozen=True)
class Atom:
    op: str
    payload: dict[str, Any] = field(default_factory=dict)
    key: str | None = None
    accept_error: tuple[str, ...] = ()


def _msg_matches(msg: dict[str, Any], atom: Atom) -> bool:
    if not atom.key:
        return True
    got = msg.get(atom.key)
    if got is None and atom.key == "sheet_id":
        got = msg.get("sheet")
    want = atom.payload.get(atom.key)
    if want is None and atom.key == "sheet_id":
        want = atom.payload.get("sheet")
    return str(got or "") == str(want or "")


def atom_ok(turns: list[dict[str, Any]], atom: Atom) -> bool:
    for turn in turns:
        if turn.get("op") != atom.op:
            continue
        if not _msg_matches(turn.get("msg") or {}, atom):
            continue
        reply = turn.get("reply") or {}
        if reply.get("ok"):
            return True
        err = str(reply.get("error") or "")
        if any(bit in err for bit in atom.accept_error):
            return True
    return False


def _dynamic_payload(atom: Atom, turns: list[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(atom.payload)
    if atom.op == "delete_wire" and not payload.get("wire_id"):
        for turn in reversed(turns):
            if turn.get("op") != "list_wires":
                continue
            wires = ((turn.get("reply") or {}).get("result") or {}).get("wires") or []
            if wires and isinstance(wires[0], dict) and wires[0].get("id"):
                payload["wire_id"] = wires[0]["id"]
                break
    if atom.op == "delete_rail" and not payload.get("rail_id"):
        for turn in reversed(turns):
            if turn.get("op") != "list_rails":
                continue
            rails = ((turn.get("reply") or {}).get("result") or {}).get("rails") or []
            if rails and isinstance(rails[0], dict) and rails[0].get("id"):
                payload["rail_id"] = rails[0]["id"]
                break
    if atom.op == "delete_component" and not payload.get("ref"):
        for turn in reversed(turns):
            if turn.get("op") != "duplicate_component":
                continue
            rid = ((turn.get("reply") or {}).get("result") or {}).get("id")
            if rid:
                payload["ref"] = rid
                break
    return payload


def seq_nudge(atoms: tuple[Atom, ...]):
    def nudge(turns: list[dict[str, Any]], job: Path) -> str:
        for atom in atoms:
            if atom_ok(turns, atom):
                continue
            body = {"op": atom.op, **_dynamic_payload(atom, turns)}
            return "Next op MUST be: " + json.dumps(body, default=str)
        return 'Checks pass. Send {"op":"done","summary":"ok"}'

    return nudge


def seq_check(atoms: tuple[Atom, ...]):
    def check(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
        missing = [atom.op for atom in atoms if not atom_ok(turns, atom)]
        if missing:
            return False, "missing ok ops: " + ",".join(missing)
        return True, ",".join(atom.op for atom in atoms)

    return check


def atom_task(
    task_id: str,
    atoms: tuple[Atom, ...],
    *,
    extra_ops: tuple[str, ...] = (),
    needs_open: bool = True,
    max_turns: int | None = None,
    intro: str = "",
) -> Task:
    ops: list[str] = []
    if needs_open:
        ops.append("open")
    else:
        ops.append("create_project")
    ops.append("save")
    for atom in atoms:
        ops.append(atom.op)
    ops.extend(extra_ops)
    allowed = tuple(dict.fromkeys(ops))
    steps = "\n".join(
        f"{i}. {json.dumps({'op': a.op, **a.payload}, default=str)}"
        for i, a in enumerate(atoms, start=1)
    )
    start = (
        "First op: open the scratch job."
        if needs_open
        else 'First op: {"op":"create_project","name":"Harness Draw"} then save.'
    )
    instruction = (
        f"{intro}{start} Then, in order, one JSON object per turn:\n{steps}\n"
        "Then save if you edited, then done. Follow NEXT exactly."
    )
    return Task(
        id=task_id,
        instruction=instruction,
        allowed=allowed,
        max_turns=max_turns or (len(atoms) + 8),
        check=seq_check(atoms),
        nudge=seq_nudge(atoms),
        needs_open=needs_open,
    )
