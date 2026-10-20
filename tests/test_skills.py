"""One JSONL op is a skill. Weak-ctx budget + 10-in-a-row streak."""

from __future__ import annotations

import json
from pathlib import Path

from panelcad.sharpen.clip import MAX_CHARS, clip_reply
from panelcad.sharpen.cards import system_prompt
from panelcad.sharpen.loop import ScriptedClient, chat_window
import pytest

from panelcad.sharpen.skills import (
    SKILL_EXACT,
    SKILL_REPEAT,
    WEAK_NUM_CTX,
    WEAK_TEXT_CHARS,
    prompt_text_chars,
    run_skill_streak,
    seed_skill_job,
    skill_task,
)
from panelcad.sharpen.tasks import get_task


def test_weak_ctx_budget_leaves_room_for_one_image():
    assert WEAK_NUM_CTX == 8192
    assert WEAK_TEXT_CHARS <= 12_000


def test_clip_reply_caps_window_payload():
    assert MAX_CHARS <= 800
    blob = {"ok": True, "result": {"notes": "x" * 5000, "sheets": [{"id": "main"}]}}
    out = clip_reply(blob)
    assert len(json.dumps(out)) <= MAX_CHARS + 80


def test_export_bom_does_not_overwrite_job_yaml(tmp_path: Path):
    from panelcad.sharpen.loop import _bind_paths

    job = tmp_path / "job.pcad.yaml"
    job.write_text("name: x\nsheets: []\n", encoding="utf-8")
    msg = _bind_paths({"op": "export_bom", "path": str(job)}, job)
    assert Path(msg["path"]).resolve() != job.resolve()
    assert msg["path"].endswith("skill.csv")


def test_clip_drops_next_when_op_not_allowed():
    blob = {
        "ok": True,
        "result": {
            "name": "Skill Draw",
            "next": "ask_style",
            "style_needed": True,
        },
    }
    out = clip_reply(blob, allowed=("create_project",))
    result = out.get("result") or {}
    assert "next" not in result
    assert "style_needed" not in result
    keep = clip_reply(blob, allowed=("create_project", "ask_style"))
    assert (keep.get("result") or {}).get("next") == "ask_style"


def test_create_project_skill_is_one_op_and_windowed():
    task = skill_task("create_project")
    assert task.skill == "create_project"
    assert task.allowed == ("create_project",)
    assert task.windowed is True
    assert "save" not in task.allowed
    assert get_task("skill-create_project").id == "skill-create_project"


def test_create_project_skill_prompt_fits_weak_ctx(tmp_path: Path):
    task = skill_task("create_project")
    job = tmp_path / "job.pcad.yaml"
    system = system_prompt(str(job), task.allowed)
    user = f"{task.instruction}\nJob: {job}\nOne JSON object per turn."
    messages = chat_window(system=system, task_user=user, job=job, recent=[])
    n = prompt_text_chars(messages)
    assert n <= WEAK_TEXT_CHARS, f"create_project skill prompt {n} chars > {WEAK_TEXT_CHARS}"
    blob = json.dumps(messages)
    assert "then save" not in blob.lower()
    assert "send ask_style" not in blob.lower()


def test_create_project_skill_scripted_ok(tmp_path: Path):
    client = ScriptedClient(
        [
            '{"op":"create_project","name":"Skill Draw"}',
            '{"op":"done","summary":"created"}',
        ]
    )
    from panelcad.sharpen.loop import run_sharpen

    result = run_sharpen(
        "skill-create_project",
        client=client,
        via_cli=False,
        scratch=tmp_path / "one",
        copy=False,
        fresh=True,
    )
    assert result.ok, result.reason
    ops = [t.get("op") for t in result.turns if t.get("op")]
    assert "create_project" in ops
    assert "save" not in ops


def test_skill_streak_ten_in_a_row_ok(tmp_path: Path):
    assert SKILL_REPEAT == 10
    pair = [
        '{"op":"create_project","name":"Skill Draw"}',
        '{"op":"done","summary":"created"}',
    ]
    client = ScriptedClient(pair * SKILL_REPEAT)
    result = run_skill_streak(
        "create_project",
        n=SKILL_REPEAT,
        client=client,
        scratch=tmp_path / "ten",
    )
    assert result.ok is True
    assert result.streak == 10
    assert result.miss == 0


def test_skill_streak_miss_resets_and_fails_the_batch(tmp_path: Path):
    ok_pair = [
        '{"op":"create_project","name":"Skill Draw"}',
        '{"op":"done","summary":"created"}',
    ]
    miss = [
        '{"op":"place","library_id":"iec.breakers.mcb_3p_32a","ref":"Q1","x":1,"y":1}'
    ] * 6
    # two hits, one miss (burns max_turns), then seven hits — not 10 in a row
    replies = ok_pair * 2 + miss + ok_pair * 7
    client = ScriptedClient(replies)
    result = run_skill_streak(
        "create_project",
        n=10,
        client=client,
        scratch=tmp_path / "miss",
    )
    assert result.ok is False
    assert result.streak < 10
    assert result.miss >= 1


def test_set_title_block_skill_is_one_op_and_windowed():
    task = skill_task("set_title_block")
    assert task.skill == "set_title_block"
    assert task.allowed == ("set_title_block",)
    assert task.windowed is True
    assert task.needs_open is True
    assert "open" not in task.allowed
    assert get_task("skill-set_title_block").id == "skill-set_title_block"


def test_set_title_block_skill_prompt_fits_weak_ctx(tmp_path: Path):
    task = skill_task("set_title_block")
    job = tmp_path / "job.pcad.yaml"
    system = system_prompt(str(job), task.allowed)
    user = f"{task.instruction}\nJob: {job}\nOne JSON object per turn."
    messages = chat_window(system=system, task_user=user, job=job, recent=[])
    n = prompt_text_chars(messages)
    assert n <= WEAK_TEXT_CHARS, f"set_title_block skill prompt {n} chars > {WEAK_TEXT_CHARS}"
    blob = json.dumps(messages)
    assert "send ask_style" not in blob.lower()
    assert '"op":"open"' not in blob


def test_set_title_block_skill_scripted_ok(tmp_path: Path):
    from panelcad import Session
    from panelcad.sharpen.loop import run_sharpen

    seed = tmp_path / "seed.pcad.yaml"
    Session.create("Skill Draw").save(seed)
    client = ScriptedClient(
        [
            '{"op":"set_title_block","drawing_number":"FIG. 1","rev":"A"}',
            '{"op":"done","summary":"titled"}',
        ]
    )
    result = run_sharpen(
        "skill-set_title_block",
        project=seed,
        client=client,
        via_cli=False,
        scratch=tmp_path / "title",
        copy=True,
        fresh=False,
    )
    assert result.ok, result.reason
    ops = [t.get("op") for t in result.turns if t.get("op")]
    assert "set_title_block" in ops
    assert "open" not in ops
    tb = Session.open(result.job).get_title_block()
    assert str(tb.get("drawing_number") or "") == "FIG. 1"


@pytest.mark.parametrize("op", list(SKILL_EXACT))
def test_remaining_from_print_skill_is_one_op(op: str):
    task = skill_task(op)
    assert task.skill == op
    assert task.allowed == (op,)
    assert task.windowed is True
    assert get_task(f"skill-{op}").id == f"skill-{op}"


@pytest.mark.parametrize("op", list(SKILL_EXACT))
def test_remaining_from_print_skill_scripted_ok(op: str, tmp_path: Path):
    from panelcad.sharpen.loop import run_sharpen

    scratch = tmp_path / op
    scratch.mkdir()
    job = scratch / "job.pcad.yaml"
    seed_skill_job(op, job)
    payload = json.dumps(SKILL_EXACT[op], default=str)
    client = ScriptedClient(
        [
            payload,
            f'{{"op":"done","summary":"{op}"}}',
        ]
    )
    result = run_sharpen(
        f"skill-{op}",
        project=job,
        client=client,
        via_cli=False,
        scratch=scratch,
        copy=False,
        fresh=False,
    )
    assert result.ok, result.reason
    ops = [t.get("op") for t in result.turns if t.get("op")]
    assert op in ops


def test_library_search_skill_is_one_op_and_windowed():
    task = skill_task("library_search")
    assert task.skill == "library_search"
    assert task.allowed == ("library_search",)
    assert task.windowed is True
    assert get_task("skill-library_search").id == "skill-library_search"


def test_library_search_skill_scripted_ok(tmp_path: Path):
    from panelcad import Session
    from panelcad.sharpen.loop import run_sharpen

    seed = tmp_path / "seed.pcad.yaml"
    Session.create("Skill Draw").save(seed)
    client = ScriptedClient(
        [
            '{"op":"library_search","query":"relay"}',
            '{"op":"done","summary":"searched"}',
        ]
    )
    result = run_sharpen(
        "skill-library_search",
        project=seed,
        client=client,
        via_cli=False,
        scratch=tmp_path / "search",
        copy=True,
        fresh=False,
    )
    assert result.ok, result.reason
    ops = [t.get("op") for t in result.turns if t.get("op")]
    assert "library_search" in ops


def test_create_project_result_does_not_nudge_ask_style(tmp_path: Path):
    from panelcad.sharpen.loop import run_sharpen

    client = ScriptedClient(
        [
            '{"op":"create_project","name":"Skill Draw"}',
            '{"op":"done","summary":"created"}',
        ]
    )
    run_sharpen(
        "skill-create_project",
        client=client,
        via_cli=False,
        scratch=tmp_path / "no-ask",
        copy=False,
        fresh=True,
    )
    blob = json.dumps(client.calls)
    assert "ask_style" not in blob
