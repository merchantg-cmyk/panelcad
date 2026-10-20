from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

EXAMPLE_JOB = Path(__file__).resolve().parents[1] / "examples" / "dol_starter.pcad.yaml"


def _private_job_tokens() -> tuple[str, ...] | None:
    """Names the harness must not contain. The list is not in this tree when absent."""

    path = Path(__file__).resolve().parents[1] / "scripts" / "public-snapshot.banned.txt"
    if not path.is_file():
        return None
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if text:
            words.append(text)
    return tuple(words) or None


def _copy_example(tmp_path: Path) -> Path:
    """Work on a temp copy so sharpen never writes the example's AI journal."""
    dest = tmp_path / EXAMPLE_JOB.name
    shutil.copy2(EXAMPLE_JOB, dest)
    return dest

from panelcad.sharpen.clip import clip_reply
from panelcad.sharpen.cards import CARDS
from panelcad.sharpen.from_print import (
    EXAMPLE_PDF,
    FROM_PRINT_OPS,
    INSTRUCTION,
    INSTRUCTION_CLOSE,
    INSTRUCTION_NEMA,
    INSTRUCTION_PLACE,
    INSTRUCTION_TITLE,
    INSTRUCTION_WIRE,
    PHASE_OPS,
    _FROM_PRINT_STANDARD,
    _FROM_PRINT_STYLE,
    _check_from_print,
    _is_net_ref,
    _nudge_from_print,
    _place_example,
    _prefer_control_pin,
    _print_hints,
    _role_library,
    _search_query,
    _three_wire_gap,
    build_instruction,
    _FROM_PRINT_PAGE_TEXT,
    _active_hints,
    _device_designators,
    _heading_place_gap,
    _power_device_page,
    _printed_fields,
    coerce_place,
    coerce_title,
    coerce_wire_pins,
    current_phase,
    from_print_page_text,
    from_print_phase_allowed,
    from_print_task,
    from_print_window_user,
    phase_instruction,
    read_pdf_page_text,
    render_pdf_page,
    rewrite_from_print,
    set_from_print_page_text,
    set_from_print_standard,
    set_from_print_style,
)
from panelcad.sharpen.loop import ScriptedClient, run_sharpen
from panelcad.sharpen.parse import ParseError, parse_op


def test_parse_flattens_args_wrapper():
    msg = parse_op(
        '{"op":"add_sheet","args":{"sheet_id":"panel","title":"Panel","kind":"panel_layout"}}'
    )
    assert msg["op"] == "add_sheet"
    assert msg["sheet_id"] == "panel"
    assert msg["kind"] == "panel_layout"
    assert "args" not in msg


def test_parse_keeps_add_sheet_id():
    msg = parse_op('{"op":"add_sheet","sheet_id":"panel","kind":"panel_layout"}')
    assert msg["op"] == "add_sheet"
    assert msg["sheet_id"] == "panel"
    assert msg.get("sheet") in (None, "panel")


def test_loop_rejects_identical_op_third_time(tmp_path: Path):
    client = ScriptedClient(
        [
            '{"op":"create_project","name":"Harness Draw"}',
            '{"op":"create_project","name":"Harness Draw"}',
            '{"op":"create_project","name":"Harness Draw"}',
            '{"op":"save"}',
            '{"op":"done","summary":"created"}',
        ]
    )
    result = run_sharpen(
        "fresh-create",
        client=client,
        via_cli=False,
        scratch=tmp_path / "loop",
        copy=False,
        fresh=True,
    )
    assert result.ok, result.reason
    looped = [
        t
        for t in result.turns
        if str((t.get("reply") or {}).get("error") or "").startswith("LOOP:")
    ]
    assert looped, result.turns


def test_loop_rejects_alternating_ops():
    from panelcad.sharpen.loop import _period2_loop

    turns = [
        {"fp": "place-pl1"},
        {"fp": "cleanup"},
        {"fp": "place-pl1"},
        {"fp": "cleanup"},
    ]
    assert _period2_loop(turns, "place-pl1") is True
    assert _period2_loop(turns[:2], "place-pl1") is False
    assert _period2_loop(turns, "wire") is False


def test_fresh_create_scripted(tmp_path: Path):
    client = ScriptedClient(
        [
            '{"op":"create_project","name":"Harness Draw"}',
            '{"op":"save"}',
            '{"op":"done","summary":"created"}',
        ]
    )
    result = run_sharpen(
        "fresh-create",
        client=client,
        via_cli=False,
        scratch=tmp_path / "fresh",
        copy=False,
        fresh=True,
    )
    assert result.ok, result.reason
    assert result.job.is_file()


def test_parse_fenced_json_and_aliases():
    msg = parse_op(
        '```json\n{"cmd":"place","lib":"iec.terminals.x_2p5","sheet_id":"io","ref":"X20","x":50,"y":180}\n```'
    )
    assert msg["op"] == "place"
    assert msg["library_id"] == "iec.terminals.x_2p5"
    assert msg["sheet"] == "io"


def test_parse_rejects_python_and_shell():
    try:
        parse_op("from panelcad import Session\nSession.open('x')")
        raise AssertionError("expected ParseError")
    except ParseError as exc:
        assert "Python" in str(exc) or "python" in str(exc).lower()
    try:
        parse_op("panelcad inspect job.pcad.yaml --json")
        raise AssertionError("expected ParseError")
    except ParseError as exc:
        assert "JSON" in str(exc)


def test_parse_allow_list():
    try:
        parse_op('{"op":"delete_component","ref":"X1"}', allowed=("open", "list_sheets"))
        raise AssertionError("expected ParseError")
    except ParseError as exc:
        assert "allow-list" in str(exc)


def test_from_print_ops_are_the_eight():
    assert FROM_PRINT_OPS == (
        "create_project",
        "set_title_block",
        "library_search",
        "place",
        "wire",
        "add_sheet",
        "set_active_sheet",
        "set_sheet_title",
        "cleanup",
        "validate",
        "save",
    )
    assert from_print_task().allowed == FROM_PRINT_OPS
    try:
        parse_op('{"op":"inspect_device","library_id":"iec.operators.pb_no"}', allowed=FROM_PRINT_OPS)
        raise AssertionError("expected ParseError")
    except ParseError as exc:
        assert "allow-list" in str(exc)


def test_from_print_cards_have_no_steering():
    banned = ("do this first", "inspect_device first", "follow next", "must be")
    for name in FROM_PRINT_OPS:
        text = CARDS.get(name, "").lower()
        for bit in banned:
            assert bit not in text, f"{name}: {text!r}"


def test_clip_inspect_drops_bulk():
    reply = {
        "id": 3,
        "ok": True,
        "result": {
            "name": "DOL",
            "sheets": [
                {
                    "id": "power",
                    "title": "Power",
                    "kind": "schematic",
                    "components": [
                        {
                            "id": "Q1",
                            "library_id": "iec.breakers.mcb_3p_32a",
                            "x": 1,
                            "y": 2,
                            "pins": {"2": {}, "4": {}, "6": {}},
                        }
                    ],
                    "wires": [{"id": "w1", "from": "Q1.2", "to": "K1.L1", "net": "L1", "points": [0] * 40}],
                }
            ],
        },
    }
    clipped = clip_reply(reply)
    sheet = clipped["result"]["sheets"][0]
    assert sheet["components"][0]["pins"] == ["2", "4", "6"]
    assert "points" not in sheet["wires"][0]


def test_list_sheets_scripted_does_not_touch_source(tmp_path: Path):
    src = _copy_example(tmp_path)
    before = src.read_bytes()
    client = ScriptedClient(
        [
            json.dumps({"op": "open", "path": str(src)}),
            json.dumps({"op": "list_sheets"}),
            json.dumps({"op": "done", "summary": "listed sheets"}),
        ]
    )
    result = run_sharpen(
        "list-sheets",
        project=src,
        client=client,
        via_cli=False,
        scratch=tmp_path / "runs",
    )
    assert result.ok, result.reason
    assert src.read_bytes() == before
    assert result.job != src
    assert "list_sheets" in [t.get("op") for t in result.turns]
    assert result.trace.is_file()


def test_place_terminal_rejects_lying_done(tmp_path: Path):
    src = _copy_example(tmp_path)
    client = ScriptedClient(
        [
            '{"op":"open","path":"x"}',
            '{"op":"cleanup","sheet":"io"}',
            '{"op":"done","summary":"placed X20"}',
            '{"op":"place","library_id":"iec.terminals.x_2p5","ref":"X20","x":50,"y":180,"sheet":"io"}',
            '{"op":"set_properties","ref":"X20","manufacturer":"Wago","part_number":"2002-1201","sheet":"io"}',
            '{"op":"cleanup","sheet":"io"}',
            '{"op":"validate"}',
            '{"op":"save"}',
            '{"op":"done","summary":"placed X20"}',
        ]
    )
    result = run_sharpen(
        "place-terminal",
        project=src,
        client=client,
        via_cli=False,
        scratch=tmp_path / "lie",
    )
    assert result.ok, result.reason
    assert any(t.get("done_rejected") for t in result.turns)


def test_nudge_moves_past_successful_place(tmp_path: Path):
    src = _copy_example(tmp_path)
    client = ScriptedClient(
        [
            '{"op":"open","path":"x"}',
            '{"op":"place","library_id":"iec.terminals.x_2p5","ref":"X20","x":50,"y":180,"sheet":"io"}',
            '{"op":"place","library_id":"iec.terminals.x_2p5","ref":"X20","x":50,"y":180,"sheet":"io"}',
            '{"op":"set_properties","ref":"X20","manufacturer":"Wago","part_number":"2002-1201","sheet":"io"}',
            '{"op":"cleanup","sheet":"io"}',
            '{"op":"validate"}',
            '{"op":"save"}',
            '{"op":"done","summary":"placed X20"}',
        ]
    )
    result = run_sharpen(
        "place-terminal",
        project=src,
        client=client,
        via_cli=False,
        scratch=tmp_path / "nudge",
    )
    assert result.ok, result.reason


def test_place_terminal_scripted(tmp_path: Path):
    src = _copy_example(tmp_path)
    client = ScriptedClient(
        [
            '{"op":"open","path":"ignored"}',
            '{"op":"set_active_sheet","sheet_id":"io"}',
            '{"op":"place","library_id":"iec.terminals.x_2p5","ref":"X20","x":50,"y":180,"sheet":"io"}',
            '{"op":"set_properties","ref":"X20","manufacturer":"Wago","part_number":"2002-1201","sheet":"io"}',
            '{"op":"cleanup","sheet":"io"}',
            '{"op":"validate"}',
            '{"op":"save"}',
            '{"op":"done","summary":"placed X20"}',
        ]
    )
    result = run_sharpen(
        "place-terminal",
        project=src,
        client=client,
        via_cli=False,
        scratch=tmp_path / "runs",
    )
    assert result.ok, result.reason


def test_list_sheets_via_real_agent_cli(tmp_path: Path):
    src = _copy_example(tmp_path)
    client = ScriptedClient(
        [
            '{"op":"open","path":"x"}',
            '{"op":"list_sheets"}',
            '{"op":"done","summary":"listed"}',
        ]
    )
    result = run_sharpen(
        "list-sheets",
        project=src,
        client=client,
        via_cli=True,
        scratch=tmp_path / "cli-runs",
        python=sys.executable,
    )
    assert result.ok, result.reason


def test_recover_from_bad_json_then_succeed(tmp_path: Path):
    src = _copy_example(tmp_path)
    client = ScriptedClient(
        [
            "I will now list the sheets.",
            '{"op":"open"}',
            '{"op":"list_sheets"}',
            '{"op":"done","summary":"ok"}',
        ]
    )
    result = run_sharpen(
        "list-sheets",
        project=src,
        client=client,
        via_cli=False,
        scratch=tmp_path / "runs",
    )
    assert result.ok, result.reason
    assert any(t.get("parse_error") for t in result.turns)


def test_render_example_print_pdf(tmp_path: Path):
    dest = tmp_path / "page.png"
    render_pdf_page(EXAMPLE_PDF, 1, dest)
    assert dest.is_file()
    assert dest.stat().st_size > 1000


def test_from_print_harness_is_job_agnostic():
    from panelcad.sharpen import from_print as fp_mod

    text = INSTRUCTION + from_print_task().instruction
    text += Path(fp_mod.__file__).read_text(encoding="utf-8")
    banned = _private_job_tokens()
    if banned is not None:
        for word in banned:
            assert word not in text
    assert "iec.operators.pb_no" in INSTRUCTION
    assert "operators" in INSTRUCTION.lower()


def test_coerce_wire_pins_is_generic(tmp_path: Path):
    from panelcad import Session

    job = tmp_path / "job.pcad.yaml"
    session = Session.create("Any Print")
    session.place("iec.contactors.ac3_9a", ref="K1", x=10, y=10)
    session.place("iec.loads.motor_3ph", ref="M1", x=40, y=40)
    session.save(job)
    turns = [
        {
            "op": "place",
            "msg": {"ref": "K1", "library_id": "iec.contactors.ac3_9a"},
            "reply": {"ok": True, "result": {"id": "K1", "library_id": "iec.contactors.ac3_9a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "M1", "library_id": "iec.loads.motor_3ph"},
            "reply": {"ok": True, "result": {"id": "M1", "library_id": "iec.loads.motor_3ph"}},
        },
    ]
    msg = coerce_wire_pins({"op": "wire", "from": "K1", "to": "M1", "net": "T1"}, job, turns)
    assert str(msg["from"]).startswith("K1.")
    assert str(msg["to"]).startswith("M1.")
    fuse_turns = [
        {
            "op": "place",
            "msg": {"ref": "FU1", "library_id": "iec.protection.fuse_10a"},
            "reply": {"ok": True, "result": {"id": "FU1", "library_id": "iec.protection.fuse_10a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "START", "library_id": "iec.operators.pb_no"},
            "reply": {"ok": True, "result": {"id": "START", "library_id": "iec.operators.pb_no"}},
        },
    ]
    bad = coerce_wire_pins(
        {"op": "wire", "from": "FU1.L1", "to": "START.X1", "net": "L1"}, job, fuse_turns
    )
    assert bad["from"].startswith("FU1.")
    assert bad["from"] != "FU1.L1"
    banned = _private_job_tokens()
    if banned is not None:
        assert banned[0] not in json.dumps(msg)


def test_coerce_place_uses_print_roles_not_job_names():
    start = coerce_place(
        {"op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "START", "x": 1, "y": 1},
        Path("x"),
        [],
    )
    assert start["library_id"] == "iec.operators.pb_no"
    stop = coerce_place(
        {"op": "place", "library_id": "iec.relays.k_4co_24vdc", "ref": "STOP", "x": 1, "y": 1},
        Path("x"),
        [],
    )
    assert stop["library_id"] == "iec.operators.pb_nc"
    line = coerce_place(
        {"op": "place", "library_id": "iec.breakers.mcb_3p_32a", "ref": "L1", "x": 1, "y": 1},
        Path("x"),
        [],
    )
    assert line.get("_harness_error")
    assert "net" in line["_harness_error"].lower()


def test_role_library_and_net_label_block():
    roles = {
        "START": "iec.operators.pb_no",
        "STOP": "iec.operators.pb_nc",
        "RESET": "iec.operators.pb_no",
        "ESTOP": "iec.operators.estop",
        "E-STOP": "iec.operators.estop",
        "FU1": "iec.protection.fuse_10a",
        "FU2": "iec.protection.fuse_10a",
        "F1": "iec.protection.overload_9a",
        "OL": "iec.protection.overload_9a",
        "OL1": "iec.protection.overload_9a",
        "GND": "iec.terminals.pe_4",
        "PE": "iec.terminals.pe_4",
        "PLC": "iec.plc.compact_16dio",
        "CPU": "iec.plc.compact_16dio",
        "HMI": "iec.hmi.panel_7in",
        "VFD": "iec.drives.vfd_1k5",
        "GS20": "iec.drives.vfd_1k5",
    }
    for ref, lib in roles.items():
        assert _role_library(ref) == lib, ref
        out = rewrite_from_print(
            {"op": "place", "library_id": "iec.contactors.ac3_9a", "ref": ref, "x": 1, "y": 1},
            Path("x"),
            [],
        )
        assert out["library_id"] == lib, ref
        assert not out.get("_harness_error"), ref
    for net in ("L1", "L2", "L3", "N"):
        assert _is_net_ref(net)
        blocked = rewrite_from_print(
            {"op": "place", "library_id": "iec.breakers.mcb_3p_32a", "ref": net, "x": 1, "y": 1},
            Path("x"),
            [],
        )
        assert blocked.get("_harness_error"), net
        assert "net" in blocked["_harness_error"].lower()
    assert _role_library("K1") == "iec.contactors.ac3_9a"
    assert _role_library("Q1") == "iec.breakers.mcb_3p_32a"
    assert _role_library("MCR") == "iec.relays.k_4co_24vdc"
    assert _role_library("CR1") == "iec.relays.k_4co_24vdc"
    assert _role_library("M1") is None


def test_multipage_check_rejects_empty_sheets(tmp_path: Path):
    from panelcad import Session
    from panelcad.sharpen.from_print import _FROM_PRINT_PAGE_COUNT, _check_from_print

    job = tmp_path / "job.pcad.yaml"
    session = Session.create("POWER SUPPLY")
    session.place("nema.protection.fuse_10a", ref="FU1", x=10, y=10)
    session.place("nema.power.control_transformer", ref="T", x=40, y=10)
    session.place("nema.protection.fuse_10a", ref="FU2", x=70, y=10)
    session.place("nema.operators.pilot_light", ref="PL1", x=100, y=10)
    session.wire("FU1.2", "T.H1")
    session.wire("T.X1", "FU2.1")
    session.add_sheet("p2", title="Page 2")
    session.add_sheet("p3", title="Page 3")
    session.save(job)
    turns = [
        {"op": "create_project", "reply": {"ok": True}},
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        {
            "op": "place",
            "msg": {"ref": "FU1", "library_id": "nema.protection.fuse_10a"},
            "reply": {"ok": True, "result": {"id": "FU1", "library_id": "nema.protection.fuse_10a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "T", "library_id": "nema.power.control_transformer"},
            "reply": {"ok": True, "result": {"id": "T", "library_id": "nema.power.control_transformer"}},
        },
        {"op": "add_sheet", "reply": {"ok": True}},
        {"op": "add_sheet", "reply": {"ok": True}},
        {"op": "save", "reply": {"ok": True}},
    ]
    token = _FROM_PRINT_PAGE_COUNT.set(3)
    try:
        ok, reason = _check_from_print(turns, job)
        assert ok is False
        assert "no devices" in reason or "only has" in reason
    finally:
        _FROM_PRINT_PAGE_COUNT.reset(token)


def test_nema_ansi_roles_and_instruction():
    from panelcad.sharpen.from_print import from_print_standard

    token = set_from_print_standard("ANSI")
    try:
        assert from_print_standard() == "NEMA/ANSI"
        assert build_instruction() == INSTRUCTION_NEMA
        assert "nema.operators.pb_no" in from_print_task().instruction
        assert "iec.operators.pb_no" not in from_print_task().instruction
        assert _role_library("K1") == "nema.contactors.coil"
        assert _role_library("Q1") == "nema.protection.disconnect_3p"
        assert _role_library("MCR") == "nema.contactors.coil"
        assert _role_library("START") == "nema.operators.pb_no"
        assert _role_library("OL") == "nema.protection.overload"
        start = coerce_place(
            {"op": "place", "library_id": "iec.operators.pb_no", "ref": "START", "x": 1, "y": 1},
            Path("x"),
            [],
        )
        assert start["library_id"] == "nema.operators.pb_no"
        coil = coerce_place(
            {"op": "place", "library_id": "iec.breakers.mcb_3p_32a", "ref": "M1", "x": 1, "y": 1},
            Path("x"),
            [],
        )
        assert coil["library_id"] == "nema.contactors.coil"
        raw = coerce_place(
            {"op": "place", "library_id": "iec.loads.motor_3ph", "ref": "MOT", "x": 1, "y": 1},
            Path("x"),
            [],
        )
        assert raw["library_id"] == "nema.loads.motor_3ph"
        search = rewrite_from_print({"op": "library_search", "query": "coil"}, Path("x"), [])
        assert search["standard"] == "NEMA/ANSI"
        title = coerce_title({"op": "set_title_block", "drawing_number": "FIG.1"}, Path("x"), [])
        assert title["standard"] == "NEMA/ANSI"
        assert title["coil_style"] == "square"
        from panelcad.sharpen.from_print import _FROM_PRINT_STYLE, parse_pages, set_from_print_style

        style_tok = set_from_print_style("ladder")
        try:
            titled = coerce_title({"op": "set_title_block", "drawing_number": "FIG.1"}, Path("x"), [])
            assert titled["drawing_style"] == "ladder"
            created = rewrite_from_print({"op": "create_project", "name": "Power"}, Path("x"), [])
            assert created["standard"] == "NEMA/ANSI"
            assert created["drawing_style"] == "ladder"
        finally:
            _FROM_PRINT_STYLE.reset(style_tok)
        assert parse_pages("all", 1, 3) == [1, 2, 3]
        assert parse_pages("1-3", 1, 5) == [1, 2, 3]
        assert parse_pages(None, 2, 5) == [2]
    finally:
        _FROM_PRINT_STANDARD.reset(token)


def test_rewrite_wire_uses_trace_pins_not_disk():
    turns = [
        {
            "op": "place",
            "msg": {"ref": "FU1", "library_id": "iec.protection.fuse_10a"},
            "reply": {"ok": True, "result": {"id": "FU1", "library_id": "iec.protection.fuse_10a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "START", "library_id": "iec.operators.pb_no"},
            "reply": {"ok": True, "result": {"id": "START", "library_id": "iec.operators.pb_no"}},
        },
    ]
    wired = rewrite_from_print(
        {"op": "wire", "from": "FU1.L1", "to": "START.X1", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert wired["from"].startswith("FU1.")
    assert wired["from"] != "FU1.L1"
    assert wired["to"].startswith("START.")
    assert wired["to"] != "START.X1"
    net_end = rewrite_from_print(
        {"op": "wire", "from": "L1", "to": "FU1.1", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert net_end.get("_harness_error")
    coil_turns = turns + [
        {
            "op": "place",
            "msg": {"ref": "M", "library_id": "iec.contactors.ac3_9a"},
            "reply": {"ok": True, "result": {"id": "M", "library_id": "iec.contactors.ac3_9a"}},
        }
    ]
    coil = rewrite_from_print(
        {"op": "wire", "from": "START.14", "to": "M.L1", "net": "L1"},
        Path("missing.pcad.yaml"),
        coil_turns,
    )
    assert coil["to"] == "M.A1"
    assert _prefer_control_pin("iec.contactors.ac3_9a", "L1", "iec.operators.pb_no") == "A1"
    assert _prefer_control_pin("iec.contactors.ac3_9a", "A1", "iec.loads.motor_3ph") == "T1"
    assert _prefer_control_pin("nema.contactors.coil", "A2", "nema.loads.motor_3ph") == "A2"
    shorted = rewrite_from_print(
        {"op": "wire", "from": "M.A1", "to": "M.A2", "net": "L2"},
        Path("missing.pcad.yaml"),
        coil_turns,
    )
    assert shorted.get("_harness_error")
    loop = rewrite_from_print(
        {"op": "wire", "from": "M.A2", "to": "M.A2", "net": "L3"},
        Path("missing.pcad.yaml"),
        coil_turns,
    )
    assert loop.get("_harness_error")
    once = [
        {
            "op": "wire",
            "msg": {"from": "START.14", "to": "M.A1"},
            "reply": {"ok": True, "result": {"from": "START.14", "to": "M.A1"}},
        }
    ]
    dup = rewrite_from_print(
        {"op": "wire", "from": "M.A1", "to": "START.14", "net": "L1"},
        Path("missing.pcad.yaml"),
        coil_turns + once,
    )
    assert dup.get("_harness_error")


def test_check_rejects_single_family_junk(tmp_path: Path):
    job = tmp_path / "missing.pcad.yaml"

    def place(ref: str, lib: str) -> dict:
        return {
            "op": "place",
            "msg": {"op": "place", "ref": ref, "library_id": lib},
            "reply": {"ok": True, "result": {"id": ref, "library_id": lib}},
        }

    def wire(frm: str = "A.1", to: str = "B.1") -> dict:
        return {
            "op": "wire",
            "msg": {"op": "wire", "from": frm, "to": to},
            "reply": {"ok": True, "result": {"from": frm, "to": to}},
        }

    base = [
        {"op": "create_project", "reply": {"ok": True}},
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
    ]
    junk = base + [
        place("K1", "iec.contactors.ac3_9a"),
        place("K2", "iec.contactors.ac3_9a"),
        place("K3", "iec.contactors.ac3_9a"),
        place("K4", "iec.contactors.ac3_9a"),
        wire(),
        wire(),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(junk, job)
    assert ok is False
    assert "famil" in reason.lower()

    mixed = base + [
        place("START", "iec.operators.pb_no"),
        place("STOP", "iec.operators.pb_nc"),
        place("FU1", "iec.protection.fuse_10a"),
        place("M1", "iec.loads.motor_3ph"),
        wire("FU1.1", "START.13"),
        wire("FU1.2", "M1.U1"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(mixed, job)
    assert ok is True, reason

    no_coil = base + [
        place("START", "iec.operators.pb_no"),
        place("STOP", "iec.operators.pb_nc"),
        place("FU1", "iec.protection.fuse_10a"),
        place("X1", "iec.terminals.x_2p5"),
        wire(),
        wire(),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(no_coil, job)
    assert ok is True, reason

    coil_no_ol = base + [
        place("START", "iec.operators.pb_no"),
        place("STOP", "iec.operators.pb_nc"),
        place("FU1", "iec.protection.fuse_10a"),
        place("M", "iec.contactors.ac3_9a"),
        wire("START.14", "M.A1"),
        wire("FU1.2", "STOP.11"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(coil_no_ol, job)
    assert ok is True, reason

    ungrounded = base + [
        place("START", "iec.operators.pb_no"),
        place("STOP", "iec.operators.pb_nc"),
        place("FU1", "iec.protection.fuse_10a"),
        place("M", "iec.contactors.ac3_9a"),
        place("OL", "iec.protection.overload_9a"),
        wire("START.14", "M.A1"),
        wire("M.A2", "OL.95"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(ungrounded, job)
    assert ok is True, reason

    three_wire_ok = base + [
        place("START", "iec.operators.pb_no"),
        place("STOP", "iec.operators.pb_nc"),
        place("FU1", "iec.protection.fuse_10a"),
        place("M", "iec.contactors.ac3_9a"),
        place("OL", "iec.protection.overload_9a"),
        place("GND", "iec.terminals.pe_4"),
        wire("FU1.2", "STOP.11"),
        wire("STOP.12", "START.13"),
        wire("START.14", "M.A1"),
        wire("M.A2", "OL.95"),
        wire("OL.96", "GND.A"),
        wire("M.13", "START.13"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(three_wire_ok, job)
    assert ok is True, reason

    three_wire_heading = [
        {
            "op": "create_project",
            "msg": {"op": "create_project", "name": "3-WIRE CONTROL: FUSING IN 1 LINE ONLY"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        place("START", "nema.operators.pb_no"),
        place("STOP", "nema.operators.pb_nc"),
        place("FU1", "nema.protection.fuse_10a"),
        place("K1", "nema.contactors.coil"),
        place("OL", "nema.protection.ol_contact"),
        wire("FU1.2", "STOP.1"),
        wire("STOP.2", "START.3"),
        wire("START.4", "K1.A1"),
        wire("K1.A2", "OL.95"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(three_wire_heading, job)
    assert ok is True, reason

    coil_unwired = base + [
        place("START", "iec.operators.pb_no"),
        place("STOP", "iec.operators.pb_nc"),
        place("FU1", "iec.protection.fuse_10a"),
        place("M", "iec.contactors.ac3_9a"),
        place("OL", "iec.protection.overload_9a"),
        place("GND", "iec.terminals.pe_4"),
        wire("FU1.2", "STOP.11"),
        wire("STOP.12", "START.13"),
        wire("START.14", "OL.95"),
        wire("OL.96", "GND.A"),
        wire("FU1.1", "STOP.11"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(coil_unwired, job)
    assert ok is False
    assert "coil" in reason.lower()

    estop_page = [
        {
            "op": "create_project",
            "msg": {"name": "Emergency Stop Circuits"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        place("START", "iec.operators.pb_no"),
        place("STOP", "iec.operators.pb_nc"),
        place("FU1", "iec.protection.fuse_10a"),
        place("M", "iec.contactors.ac3_9a"),
        wire("FU1.1", "M.A1"),
        wire("M.A1", "START.13"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(estop_page, job)
    assert ok is False
    assert "E-STOP" in reason or "starter" in reason.lower() or "emergency" in reason.lower()

    star = base + [
        place("Q1", "iec.breakers.mcb_3p_32a"),
        place("K1", "iec.contactors.ac3_9a"),
        place("F1", "iec.protection.fuse_10a"),
        place("M1", "iec.loads.motor_3ph"),
        wire("Q1.2", "K1.A1"),
        wire("F1.1", "K1.A1"),
        wire("M1.U1", "K1.A1"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(star, job)
    assert ok is False
    assert "star" in reason.lower()

    invent_vfd = [
        {
            "op": "create_project",
            "msg": {"name": "Power"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        place("Q1", "iec.breakers.mcb_3p_32a"),
        place("K1", "iec.contactors.ac3_9a"),
        place("M1", "iec.loads.motor_3ph"),
        place("U1", "iec.drives.vfd_1k5"),
        wire("Q1.2", "K1.L1"),
        wire("K1.T1", "M1.U1"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(invent_vfd, job)
    assert ok is False
    assert "VFD" in reason or "drive" in reason.lower()


def test_from_print_nudge_starts_with_create(tmp_path: Path):
    hint = _nudge_from_print([], tmp_path / "job.pcad.yaml")
    assert "create_project" in hint
    banned = _private_job_tokens()
    if banned is not None:
        assert banned[0] not in hint
    ok, reason = _check_from_print([], tmp_path / "missing.pcad.yaml")
    assert ok is False
    assert "create" in reason.lower() or "device" in reason.lower()
    after_title = _nudge_from_print(
        [{"op": "create_project"}, {"op": "save"}, {"op": "set_title_block"}],
        tmp_path / "job.pcad.yaml",
    )
    assert "library_search" in after_title

    def placed(ref: str, lib: str) -> dict:
        return {
            "op": "place",
            "msg": {"op": "place", "ref": ref, "library_id": lib},
            "reply": {"ok": True, "result": {"id": ref, "library_id": lib}},
        }

    after_coil = _nudge_from_print(
        [
            {"op": "create_project"},
            {"op": "save"},
            {"op": "set_title_block"},
            {"op": "library_search"},
            placed("START", "iec.operators.pb_no"),
            placed("STOP", "iec.operators.pb_nc"),
            placed("FU1", "iec.protection.fuse_10a"),
            placed("M", "iec.contactors.ac3_9a"),
        ],
        tmp_path / "job.pcad.yaml",
    )
    assert "wire" in after_coil.lower() or "coil" in after_coil.lower()

    start_turn = placed("START", "iec.operators.pb_no")
    estop = coerce_place(
        {"op": "place", "library_id": "iec.operators.estop", "ref": "E-STOP", "x": 1, "y": 1},
        tmp_path / "job.pcad.yaml",
        [start_turn],
    )
    assert estop["library_id"] == "iec.operators.estop"
    assert not estop.get("_harness_error")

    title = coerce_title(
        {
            "op": "set_title_block",
            "drawing_number": "E-001",
            "company": "Acme Process Controls",
        },
        tmp_path / "job.pcad.yaml",
        [],
    )
    assert title["company"] == "Acme Process Controls"
    assert title["drawing_number"] == "E-001"

    coil = coerce_place(
        {"op": "place", "library_id": "iec.breakers.mcb_3p_32a", "ref": "M", "x": 1, "y": 1},
        tmp_path / "job.pcad.yaml",
        [start_turn],
    )
    assert coil["library_id"] == "iec.contactors.ac3_9a"
    assert not coil.get("_harness_error")


def test_print_hints_3wire_from_heading():
    hints = _print_hints("FIG. 3 - 3-WIRE CONTROL: FUSING IN BOTH PRIMARY LINES")
    assert "3wire" in hints
    assert "plc" not in hints


def test_star_third_wire_on_same_pin_rejected():
    turns = [
        {
            "op": "place",
            "msg": {"ref": "FU1", "library_id": "iec.protection.fuse_10a"},
            "reply": {"ok": True, "result": {"id": "FU1", "library_id": "iec.protection.fuse_10a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "K1", "library_id": "iec.contactors.ac3_9a"},
            "reply": {"ok": True, "result": {"id": "K1", "library_id": "iec.contactors.ac3_9a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "M1", "library_id": "iec.loads.motor_3ph"},
            "reply": {"ok": True, "result": {"id": "M1", "library_id": "iec.loads.motor_3ph"}},
        },
        {
            "op": "place",
            "msg": {"ref": "OL1", "library_id": "iec.protection.overload_9a"},
            "reply": {"ok": True, "result": {"id": "OL1", "library_id": "iec.protection.overload_9a"}},
        },
        {
            "op": "wire",
            "msg": {"from": "FU1.1", "to": "K1.A1"},
            "reply": {"ok": True, "result": {"from": "FU1.1", "to": "K1.A1"}},
        },
        {
            "op": "wire",
            "msg": {"from": "FU1.1", "to": "M1.U1"},
            "reply": {"ok": True, "result": {"from": "FU1.1", "to": "M1.U1"}},
        },
    ]
    third = coerce_wire_pins(
        {"op": "wire", "from": "FU1.1", "to": "OL1.95", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert third.get("_harness_error")
    assert "too many" in third["_harness_error"].lower()


def _placed(ref: str, lib: str) -> dict:
    return {
        "op": "place",
        "msg": {"op": "place", "ref": ref, "library_id": lib},
        "reply": {"ok": True, "result": {"id": ref, "library_id": lib}},
    }


def _wired(frm: str, to: str) -> dict:
    return {
        "op": "wire",
        "msg": {"op": "wire", "from": frm, "to": to},
        "reply": {"ok": True, "result": {"from": frm, "to": to}},
    }


def test_rewrite_rejects_fuse_to_coil_when_operators_placed():
    """Fuse→coil A1 skips STOP/START. Rewrite must reject it; do not lecture Gemma."""

    turns = [
        _placed("START", "nema.operators.pb_no"),
        _placed("STOP", "nema.operators.pb_nc"),
        _placed("FU1", "nema.protection.fuse_10a"),
        _placed("K1", "nema.contactors.coil"),
        _placed("OL", "nema.protection.overload"),
    ]
    skipped = rewrite_from_print(
        {"op": "wire", "from": "FU1.1", "to": "K1.A1", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert skipped.get("_harness_error")
    err = skipped["_harness_error"].lower()
    assert "fuse" in err or "jump" in err or "series" in err or "bypass" in err
    legal = rewrite_from_print(
        {"op": "wire", "from": "FU1.2", "to": "STOP.1", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert not legal.get("_harness_error"), legal


def test_check_rejects_fuse_coil_bypass_job():
    """The 20260826T203615Z sheet: series operators plus a fuse-to-coil jump."""

    job = Path("missing.pcad.yaml")
    turns = [
        {"op": "create_project", "reply": {"ok": True}},
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        _placed("K1", "nema.contactors.coil"),
        _placed("START", "nema.operators.pb_no"),
        _placed("STOP", "nema.operators.pb_nc"),
        _placed("FU1", "nema.protection.fuse_10a"),
        _placed("OL", "nema.protection.overload"),
        _wired("FU1.1", "K1.A1"),
        _wired("FU1.2", "STOP.1"),
        _wired("STOP.2", "START.3"),
        _wired("START.4", "K1.A1"),
        _wired("K1.A2", "OL.95"),
        _wired("K1.3", "START.3"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(turns, job)
    assert ok is False
    assert "fuse" in reason.lower() or "jump" in reason.lower() or "bypass" in reason.lower() or "series" in reason.lower()


def test_ladder_cr_is_a_coil_not_a_relay_box():
    token = set_from_print_style("ladder")
    try:
        assert _role_library("CR1") == "iec.contactors.coil"
        assert _role_library("CR410") == "iec.contactors.coil"
        assert "k_4co" not in (_role_library("MCR") or "")
        std = set_from_print_standard("NEMA/ANSI")
        try:
            assert _role_library("CR410") == "nema.contactors.coil"
            assert _role_library("STACK1") == "nema.operators.stack_light"
            assert _role_library("TOUCH1") == "nema.operators.pb_illum"
        finally:
            _FROM_PRINT_STANDARD.reset(std)
    finally:
        _FROM_PRINT_STYLE.reset(token)
    assert _role_library("CR1") == "iec.relays.k_4co_24vdc"


def test_generic_load_box_and_note_are_rejected():
    from panelcad.sharpen.from_print import rewrite_from_print

    load = rewrite_from_print(
        {"op": "place", "library_id": "iec.power.load_24v", "ref": "U9", "x": 1, "y": 1},
        Path("x"),
        [],
    )
    assert load.get("_harness_error")
    assert "rectangle" in load["_harness_error"].lower()
    note = rewrite_from_print(
        {"op": "place", "library_id": "iec.notes.block", "ref": "N1", "x": 1, "y": 1},
        Path("x"),
        [],
    )
    assert note.get("_harness_error")
    assert "note" in note["_harness_error"].lower()


def test_ladder_ol_role_is_contact_not_heater():
    token = set_from_print_style("ladder")
    try:
        assert _role_library("OL") in {
            "iec.protection.ol_contact",
            "nema.protection.ol_contact",
        }
        std = set_from_print_standard("NEMA/ANSI")
        try:
            assert _role_library("OL") == "nema.protection.ol_contact"
        finally:
            _FROM_PRINT_STANDARD.reset(std)
    finally:
        _FROM_PRINT_STYLE.reset(token)


def test_three_wire_gap_hold_in_asks_for_aux_contact():
    token = set_from_print_standard("NEMA/ANSI")
    try:
        turns = [
            _placed("START", "nema.operators.pb_no"),
            _placed("STOP", "nema.operators.pb_nc"),
            _placed("FU1", "nema.protection.fuse_10a"),
            _placed("K1", "nema.contactors.coil"),
            _placed("OL", "nema.protection.ol_contact"),
            _wired("FU1.2", "STOP.1"),
            _wired("STOP.2", "START.3"),
            _wired("START.4", "K1.A1"),
            _wired("K1.A2", "OL.95"),
        ]
        gap = _three_wire_gap(turns, Path("missing.pcad.yaml"))
        assert gap
        assert "aux_no" in gap
        assert "K1.3" not in gap
        assert "K1.13" not in gap
    finally:
        _FROM_PRINT_STANDARD.reset(token)


def test_three_wire_gap_nema_hold_in_uses_aux_3():
    token = set_from_print_standard("NEMA/ANSI")
    try:
        turns = [
            _placed("START", "nema.operators.pb_no"),
            _placed("STOP", "nema.operators.pb_nc"),
            _placed("FU1", "nema.protection.fuse_10a"),
            _placed("K1", "nema.contactors.coil"),
            _placed("OL", "nema.protection.overload"),
            _wired("FU1.2", "STOP.1"),
            _wired("STOP.2", "START.3"),
            _wired("START.4", "K1.A1"),
            _wired("K1.A2", "OL.95"),
        ]
        gap = _three_wire_gap(turns, Path("missing.pcad.yaml"))
        assert gap
        assert "aux_no" in gap
        assert "K1.13" not in gap
    finally:
        _FROM_PRINT_STANDARD.reset(token)


def test_ladder_check_requires_cleanup():
    token = set_from_print_style("ladder")
    try:
        turns = [
            {"op": "create_project", "reply": {"ok": True}},
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("START", "nema.operators.pb_no"),
            _placed("STOP", "nema.operators.pb_nc"),
            _placed("FU1", "nema.protection.fuse_10a"),
            _placed("K1", "nema.contactors.coil"),
            _placed("OL", "nema.protection.overload"),
            _wired("FU1.2", "STOP.1"),
            _wired("STOP.2", "START.3"),
            _wired("START.4", "K1.A1"),
            _wired("K1.A2", "OL.95"),
            _wired("K1.3", "START.3"),
            {"op": "save", "reply": {"ok": True}},
        ]
        ok, reason = _check_from_print(turns, Path("missing.pcad.yaml"))
        assert ok is False
        assert "cleanup" in reason.lower()
    finally:
        _FROM_PRINT_STYLE.reset(token)


def test_rewrite_coil_to_ol_lands_on_a2():
    turns = [
        _placed("START", "nema.operators.pb_no"),
        _placed("K1", "nema.contactors.coil"),
        _placed("OL", "nema.protection.overload"),
        _wired("START.4", "K1.A1"),
    ]
    msg = rewrite_from_print(
        {"op": "wire", "from": "K1.A1", "to": "OL.95", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert not msg.get("_harness_error"), msg
    assert msg["from"] == "K1.A2"


def test_rewrite_coil_to_ol_contact_lands_on_a2():
    turns = [
        _placed("START", "nema.operators.pb_no"),
        _placed("K1", "nema.contactors.coil"),
        _placed("OL", "nema.protection.ol_contact"),
        _wired("START.4", "K1.A1"),
    ]
    msg = rewrite_from_print(
        {"op": "wire", "from": "K1.A1", "to": "OL.95", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert not msg.get("_harness_error"), msg
    assert msg["from"] == "K1.A2"


def test_rewrite_rejects_pe_jumper_to_itself():
    turns = [_placed("PE1", "nema.terminals.pe")]
    msg = rewrite_from_print(
        {"op": "wire", "from": "PE1.A", "to": "PE1.B", "net": "PE"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert msg.get("_harness_error")


def test_nudge_unwired_pe_does_not_example_self_jumper(tmp_path: Path):
    hint = _nudge_from_print(
        [
            {"op": "create_project", "reply": {"ok": True}},
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("OL", "nema.protection.overload"),
            _placed("PE1", "nema.terminals.pe"),
            _placed("K1", "nema.contactors.coil"),
            _placed("START", "nema.operators.pb_no"),
            _placed("STOP", "nema.operators.pb_nc"),
            _wired("STOP.2", "START.3"),
            _wired("START.4", "K1.A1"),
            _wired("K1.A2", "OL.95"),
        ],
        tmp_path / "job.pcad.yaml",
    )
    compact = hint.replace(" ", "")
    assert '"from":"PE1.A","to":"PE1.B"' not in compact


def test_rewrite_rejects_add_sheet_on_single_page():
    msg = rewrite_from_print(
        {"op": "add_sheet", "sheet_id": "page2", "title": "Page 2", "kind": "schematic"},
        Path("missing.pcad.yaml"),
        [],
    )
    assert msg.get("_harness_error")
    err = msg["_harness_error"].lower()
    assert "add_sheet" in err or "one page" in err or "single" in err


def test_nudge_already_exists_does_not_offer_add_sheet_on_one_page(tmp_path: Path):
    hint = _nudge_from_print(
        [
            {"op": "create_project", "reply": {"ok": True}},
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("K1", "nema.contactors.coil"),
            {
                "op": "place",
                "msg": {"ref": "K1"},
                "reply": {"ok": False, "error": "component 'K1' already exists on main"},
            },
        ],
        tmp_path / "job.pcad.yaml",
    )
    assert "add_sheet" not in hint.lower()


def test_place_example_does_not_default_to_motor():
    assert _place_example(set())[0].endswith("contactors.coil") or "pb_no" in _place_example(set())[0]
    assert _place_example(set())[1] != "M1"
    lib, ref = _place_example({"relay"})
    assert "motor" not in lib
    assert ref != "M1"
    lib, _ref = _place_example({"motor"})
    assert "motor" in lib


def test_check_rejects_invented_motor_on_relay_heading():
    job = Path("missing.pcad.yaml")
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        _placed("PB1", "nema.operators.pb_no"),
        _placed("CR1", "nema.contactors.coil"),
        _placed("PL1", "nema.operators.pilot_light"),
        _placed("M1", "nema.loads.motor_3ph"),
        _wired("PB1.3", "CR1.A1"),
        _wired("CR1.A2", "PL1.X1"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(turns, job)
    assert ok is False
    assert "motor" in reason.lower()


def test_rewrite_rejects_motor_on_relay_logic():
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        }
    ]
    msg = rewrite_from_print(
        {"op": "place", "library_id": "nema.loads.motor_3ph", "ref": "M1", "x": 80, "y": 80},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert msg.get("_harness_error"), msg
    assert "motor" in msg["_harness_error"].lower()


def test_rewrite_keeps_aux_library_when_ref_looks_like_cr3():
    msg = rewrite_from_print(
        {"op": "place", "library_id": "nema.contactors.aux_no", "ref": "CR3", "x": 1, "y": 1},
        Path("x"),
        [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            }
        ],
    )
    assert "aux" in msg["library_id"]
    lib, ref = _place_example({"relay"}, [
        _placed("PB1", "nema.operators.pb_no"),
        _placed("CR1", "nema.contactors.coil"),
    ], Path("missing.pcad.yaml"))
    assert "aux" in lib
    assert "3-4" in ref or "34" in ref.replace("-", "")


def test_role_cr_aux_is_not_a_second_coil():
    assert _role_library("CR1-3-4") == "iec.contactors.aux_no"
    assert _role_library("CR1") == "iec.relays.k_4co_24vdc"
    out = rewrite_from_print(
        {"op": "place", "library_id": "nema.contactors.coil", "ref": "CR1-3-4", "x": 1, "y": 1},
        Path("x"),
        [],
    )
    assert out["library_id"].endswith("contactors.aux_no")


def test_place_example_relay_asks_for_aux_or_lamp_once_coil_exists():
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        },
        _placed("PB1", "nema.operators.pb_no"),
        _placed("CR1", "nema.contactors.coil"),
    ]
    lib, ref = _place_example({"relay"}, turns, Path("missing.pcad.yaml"))
    assert "motor" not in lib
    assert "fuse" not in lib
    assert "aux" in lib or "pilot" in lib or "light" in lib


def test_nudge_unwired_coil_does_not_example_aux_to_a1():
    hint = _nudge_from_print(
        [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("PB1", "nema.operators.pb_no"),
            _placed("CR1", "nema.contactors.coil"),
            _placed("CR1-3-4", "nema.contactors.aux_no"),
            _placed("PL1", "nema.operators.pilot_light"),
            _wired("CR1-3-4.4", "PL1.X1"),
            _wired("L1", "CR1-3-4.3"),
            _wired("PL1.X2", "L2"),
        ],
        Path("missing.pcad.yaml"),
    )
    compact = hint.replace(" ", "")
    assert "CR1.A1" in compact
    assert "CR1-3-4" not in hint.split("Example")[-1]


def test_circuit_laws_two_wires_contact_feeds_load_returns_to_l2():
    token = set_from_print_style("ladder")
    try:
        dangling = [
            {
                "op": "create_project",
                "msg": {"name": "Control"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("PB1", "nema.operators.pb_no"),
            _placed("CR1", "nema.contactors.coil"),
            _placed("FU1", "nema.protection.fuse_10a"),
            _wired("PB1.4", "CR1.A1"),
            {"op": "save", "reply": {"ok": True}},
            {"op": "cleanup", "reply": {"ok": True}},
        ]
        ok, reason = _check_from_print(dangling, Path("missing.pcad.yaml"))
        assert ok is False
        assert "two" in reason.lower() or "wire" in reason.lower()

        no_load = [
            {
                "op": "create_project",
                "msg": {"name": "Control"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("FU1", "nema.protection.fuse_10a"),
            _placed("PB1", "nema.operators.pb_no"),
            _placed("PB2", "nema.operators.pb_no"),
            _wired("L1", "FU1.1"),
            _wired("FU1.2", "PB1.3"),
            _wired("PB1.4", "PB2.3"),
            _wired("PB2.4", "L2"),
            {"op": "save", "reply": {"ok": True}},
            {"op": "cleanup", "reply": {"ok": True}},
        ]
        ok, reason = _check_from_print(no_load, Path("missing.pcad.yaml"))
        assert ok is False
        assert "load" in reason.lower() or "coil" in reason.lower() or "lamp" in reason.lower()

        ok_rungs = [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("PB1", "nema.operators.pb_no"),
            _placed("CR1", "nema.contactors.coil"),
            _placed("CR1-3-4", "nema.contactors.aux_no"),
            _placed("PL1", "nema.operators.pilot_light"),
            _wired("L1", "PB1.3"),
            _wired("PB1.4", "CR1.A1"),
            _wired("CR1.A2", "L2"),
            _wired("L1", "CR1-3-4.3"),
            _wired("CR1-3-4.4", "PL1.X1"),
            _wired("PL1.X2", "L2"),
            {"op": "save", "reply": {"ok": True}},
            {"op": "cleanup", "reply": {"ok": True}},
        ]
        ok, reason = _check_from_print(ok_rungs, Path("missing.pcad.yaml"))
        assert ok is True, reason

        # Both ends on L2 — no IF from the hot rail.
        no_hot = [
            {
                "op": "create_project",
                "msg": {"name": "Control"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("PB1", "nema.operators.pb_no"),
            _placed("CR1", "nema.contactors.coil"),
            _placed("FU1", "nema.protection.fuse_10a"),
            _wired("L2", "FU1.1"),
            _wired("FU1.2", "PB1.3"),
            _wired("PB1.4", "CR1.A1"),
            _wired("CR1.A2", "L2"),
            {"op": "save", "reply": {"ok": True}},
            {"op": "cleanup", "reply": {"ok": True}},
        ]
        ok, reason = _check_from_print(no_hot, Path("missing.pcad.yaml"))
        assert ok is False
        assert "l1" in reason.lower() or "if" in reason.lower() or "assign" in reason.lower()

        # Coil returns through a finished lamp rung (sneak via L1). Not a circuit.
        sneak = [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("PB1", "nema.operators.pb_no"),
            _placed("CR1", "nema.contactors.coil"),
            _placed("CR1-3-4", "nema.contactors.aux_no"),
            _placed("PL1", "nema.operators.pilot_light"),
            _wired("L1", "PB1.3"),
            _wired("PB1.4", "CR1.A1"),
            _wired("CR1.A2", "L1"),
            _wired("L1", "CR1-3-4.3"),
            _wired("CR1-3-4.4", "PL1.X1"),
            _wired("PL1.X2", "L2"),
            {"op": "save", "reply": {"ok": True}},
            {"op": "cleanup", "reply": {"ok": True}},
        ]
        ok, reason = _check_from_print(sneak, Path("missing.pcad.yaml"))
        assert ok is False
        assert "l2" in reason.lower() or "return" in reason.lower()
    finally:
        _FROM_PRINT_STYLE.reset(token)


def test_check_fails_when_only_aux_is_wired_not_the_coil():
    job = Path("missing.pcad.yaml")
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        _placed("PB1", "nema.operators.pb_no"),
        _placed("CR1", "nema.contactors.coil"),
        _placed("CR1-3-4", "nema.contactors.aux_no"),
        _placed("PL1", "nema.operators.pilot_light"),
        _wired("CR1-3-4.4", "PL1.X1"),
        _wired("L1", "CR1-3-4.3"),
        _wired("PL1.X2", "L2"),
        {"op": "save", "reply": {"ok": True}},
        {"op": "cleanup", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(turns, job)
    assert ok is False
    assert "coil" in reason.lower()


def test_check_fails_unwired_lamp_on_relay_sheet():
    job = Path("missing.pcad.yaml")
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        _placed("PB1", "nema.operators.pb_no"),
        _placed("CR1", "nema.contactors.coil"),
        _placed("CR1-3-4", "nema.contactors.aux_no"),
        _placed("PL1", "nema.operators.pilot_light"),
        _wired("PB1.3", "CR1.A1"),
        _wired("CR1.A2", "L2"),
        {"op": "save", "reply": {"ok": True}},
        {"op": "cleanup", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(turns, job)
    assert ok is False
    assert "lamp" in reason.lower() or "pilot" in reason.lower()


def test_nudge_unwired_lamp_asks_for_aux_to_lamp():
    hint = _nudge_from_print(
        [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("PB1", "nema.operators.pb_no"),
            _placed("CR1", "nema.contactors.coil"),
            _placed("CR1-3-4", "nema.contactors.aux_no"),
            _placed("PL1", "nema.operators.pilot_light"),
            _wired("PB1.3", "CR1.A1"),
            _wired("CR1.A2", "L2"),
        ],
        Path("missing.pcad.yaml"),
    )
    compact = hint.replace(" ", "")
    assert "PL1" in hint
    assert "aux" in hint.lower() or "CR1-3-4" in hint
    assert "CR1.A1" not in compact


def test_rewrite_rejects_coil_pin_to_aux():
    turns = [
        _placed("CR1", "nema.contactors.coil"),
        _placed("CR1-3-4", "nema.contactors.aux_no"),
        _placed("PL1", "nema.operators.pilot_light"),
    ]
    msg = rewrite_from_print(
        {"op": "wire", "from": "CR1.A1", "to": "CR1-3-4.3", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert msg.get("_harness_error"), msg


def test_rewrite_rejects_lamp_in_series_with_coil():
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        },
        _placed("CR1", "nema.contactors.coil"),
        _placed("PL1", "nema.operators.pilot_light"),
    ]
    msg = rewrite_from_print(
        {"op": "wire", "from": "CR1.A1", "to": "PL1.X1", "net": "L1"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert msg.get("_harness_error"), msg
    assert "lamp" in msg["_harness_error"].lower() or "load" in msg["_harness_error"].lower()


def test_rewrite_rejects_fuse_on_relay_logic():
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        }
    ]
    msg = rewrite_from_print(
        {"op": "place", "library_id": "nema.protection.fuse_10a", "ref": "FU1", "x": 40, "y": 40},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert msg.get("_harness_error"), msg
    assert "fuse" in msg["_harness_error"].lower()


def test_check_relay_heading_wants_lamp_not_a_fourth_family():
    job = Path("missing.pcad.yaml")
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        _placed("PB1", "nema.operators.pb_no"),
        _placed("CR1", "nema.contactors.coil"),
        _placed("CR3", "nema.contactors.aux_no"),
        _wired("PB1.3", "CR1.A1"),
        _wired("CR1.A2", "L2"),
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(turns, job)
    assert ok is False
    assert "lamp" in reason.lower() or "pilot" in reason.lower()


def test_nudge_relay_heading_does_not_example_a_motor():
    hint = _nudge_from_print(
        [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
        ],
        Path("missing.pcad.yaml"),
    )
    assert "motor_3ph" not in hint
    assert '"ref":"M1"' not in hint.replace(" ", "")


def test_rewrite_hold_in_on_bare_coil_asks_for_aux():
    token = set_from_print_standard("NEMA/ANSI")
    try:
        turns = [
            _placed("K1", "nema.contactors.coil"),
            _placed("START", "nema.operators.pb_no"),
        ]
        msg = rewrite_from_print(
            {"op": "wire", "from": "K1.3", "to": "START.3", "net": "L1"},
            Path("missing.pcad.yaml"),
            turns,
        )
        assert msg.get("_harness_error"), msg
        err = msg["_harness_error"].lower()
        assert "aux" in err
        assert msg.get("from") != "K1.A1"
    finally:
        _FROM_PRINT_STANDARD.reset(token)


def test_rewrite_ladder_ol_heater_becomes_contact():
    style = set_from_print_style("ladder")
    std = set_from_print_standard("NEMA/ANSI")
    try:
        msg = rewrite_from_print(
            {
                "op": "place",
                "library_id": "nema.protection.overload",
                "ref": "OL",
                "x": 200,
                "y": 160,
            },
            Path("missing.pcad.yaml"),
            [],
        )
        assert msg["library_id"] == "nema.protection.ol_contact"
    finally:
        _FROM_PRINT_STANDARD.reset(std)
        _FROM_PRINT_STYLE.reset(style)


def test_search_query_relay_is_coil_not_motor():
    assert _search_query({"relay"}) == "coil"
    assert _search_query(set()) == "coil"
    assert _search_query({"motor"}) == "motor"
    assert _search_query({"motor", "3wire"}) == "coil"
    assert _search_query({"transformer"}) == "transformer"
    hint = _nudge_from_print(
        [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
        ],
        Path("missing.pcad.yaml"),
    )
    assert '"query":"coil"' in hint.replace(" ", "")
    assert '"query":"motor"' not in hint.replace(" ", "")


def test_rewrite_rejects_invented_pe_and_start_on_relay_heading():
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "RELAY LOGIC"},
            "reply": {"ok": True},
        }
    ]
    pe = rewrite_from_print(
        {"op": "place", "library_id": "nema.terminals.pe", "ref": "PE1", "x": 200, "y": 200},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert pe.get("_harness_error"), pe
    start = rewrite_from_print(
        {"op": "place", "library_id": "nema.operators.pb_no", "ref": "START", "x": 80, "y": 80},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert start.get("_harness_error"), start
    reset = rewrite_from_print(
        {"op": "place", "library_id": "nema.operators.pb_nc", "ref": "RESET", "x": 80, "y": 80},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert reset.get("_harness_error"), reset
    pb = rewrite_from_print(
        {"op": "place", "library_id": "nema.operators.pb_no", "ref": "PB1", "x": 80, "y": 80},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert not pb.get("_harness_error"), pb


def test_nudge_two_wire_aux_already_on_lamp_asks_for_l1_not_coil():
    """Aux→lamp is already a load. Missing end is L1, not the illegal aux→coil A1 hang."""

    token = set_from_print_style("ladder")
    try:
        turns = [
            {
                "op": "create_project",
                "msg": {"name": "RELAY LOGIC"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("PB1", "nema.operators.pb_no"),
            _placed("CR1", "nema.contactors.coil"),
            _placed("CR1-3-4", "nema.contactors.aux_no"),
            _placed("PL1", "nema.operators.pilot_light"),
            _wired("CR1-3-4.4", "PL1.X1"),
            _wired("PB1.4", "CR1.A1"),
            _wired("CR1.A2", "L2"),
        ]
        hint = _nudge_from_print(turns, Path("missing.pcad.yaml"))
        example = hint.split("Example")[-1].replace(" ", "")
        assert "L1" in example
        assert "CR1.A1" not in example
    finally:
        _FROM_PRINT_STYLE.reset(token)


def test_motor_starter_heading_is_3wire_control():
    hints = _print_hints("MOTOR STARTER")
    assert "motor" in hints
    assert "3wire" in hints
    assert _search_query(hints) == "coil"


def test_ladder_rejects_motor_on_control_rails():
    token = set_from_print_style("ladder")
    try:
        turns = [
            {
                "op": "create_project",
                "msg": {"name": "MOTOR STARTER"},
                "reply": {"ok": True},
            },
            _placed("M1", "nema.loads.motor_3ph"),
            _placed("STOP", "nema.operators.pb_nc"),
            _placed("M", "nema.contactors.coil"),
            _placed("FU1", "nema.protection.fuse_10a"),
        ]
        rail = rewrite_from_print(
            {"op": "wire", "from": "L1", "to": "M1.T1", "net": "L1", "sheet": "main"},
            Path("missing.pcad.yaml"),
            turns,
        )
        assert rail.get("_harness_error"), rail
        assert "motor" in rail["_harness_error"].lower()
        fuse = rewrite_from_print(
            {"op": "wire", "from": "FU1.1", "to": "M1.T1", "net": "L1", "sheet": "main"},
            Path("missing.pcad.yaml"),
            turns,
        )
        assert fuse.get("_harness_error"), fuse
        second = rewrite_from_print(
            {"op": "place", "library_id": "nema.loads.motor_1ph", "ref": "M2", "x": 80, "y": 80},
            Path("missing.pcad.yaml"),
            turns,
        )
        assert second.get("_harness_error"), second

        on_rails = [
            {
                "op": "create_project",
                "msg": {"name": "MOTOR STARTER"},
                "reply": {"ok": True},
            },
            {"op": "save", "reply": {"ok": True}},
            {"op": "set_title_block", "reply": {"ok": True}},
            {"op": "library_search", "reply": {"ok": True}},
            _placed("STOP", "nema.operators.pb_nc"),
            _placed("START", "nema.operators.pb_no"),
            _placed("M", "nema.contactors.coil"),
            _placed("M1", "nema.loads.motor_3ph"),
            _wired("L1", "STOP.1"),
            _wired("STOP.2", "START.3"),
            _wired("START.4", "M.A1"),
            _wired("M.A2", "L2"),
            _wired("L1", "M1.T1"),
            {"op": "save", "reply": {"ok": True}},
            {"op": "cleanup", "reply": {"ok": True}},
        ]
        ok, reason = _check_from_print(on_rails, Path("missing.pcad.yaml"))
        assert ok is False
        assert "motor" in reason.lower()

        parked = [
            t
            for t in on_rails
            if not (
                t.get("op") == "wire"
                and "M1"
                in str((t.get("msg") or {}).get("to") or "")
                + str((t.get("msg") or {}).get("from") or "")
            )
        ]
        ok, reason = _check_from_print(parked, Path("missing.pcad.yaml"))
        assert "motor placed but not wired" not in reason.lower()
    finally:
        _FROM_PRINT_STYLE.reset(token)


def test_rewrite_rejects_invented_pe_on_3wire_heading():
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "Overcurrent Protection for 3-Wire Control Circuits"},
            "reply": {"ok": True},
        }
    ]
    msg = rewrite_from_print(
        {"op": "place", "library_id": "nema.terminals.pe", "ref": "PE1", "x": 200, "y": 200},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert msg.get("_harness_error"), msg
    assert "pe" in msg["_harness_error"].lower() or "gnd" in msg["_harness_error"].lower()


def test_repeat_fingerprint_counts_non_trailing():
    from panelcad.sharpen.loop import _repeat_fingerprint

    turns = [
        {"fp": "fuse-jump"},
        {"fp": "fuse-jump"},
        {"fp": "place-pe"},
    ]
    assert _repeat_fingerprint(turns, "fuse-jump") == 2


def test_nema_contact_pin_alias_13_to_3():
    token = set_from_print_standard("NEMA/ANSI")
    try:
        turns = [
            _placed("START", "nema.operators.pb_no"),
            _placed("K1", "nema.contactors.coil"),
        ]
        msg = coerce_wire_pins(
            {"op": "wire", "from": "START.13", "to": "K1.A1", "net": "L1"},
            Path("missing.pcad.yaml"),
            turns,
        )
        assert not msg.get("_harness_error"), msg
        assert msg["from"] == "START.3"
    finally:
        _FROM_PRINT_STANDARD.reset(token)


def test_unknown_motor_pin_uses_free_lead():
    turns = [
        {
            "op": "place",
            "msg": {"ref": "K1", "library_id": "iec.contactors.ac3_9a"},
            "reply": {"ok": True, "result": {"id": "K1", "library_id": "iec.contactors.ac3_9a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "M1", "library_id": "iec.loads.motor_3ph"},
            "reply": {"ok": True, "result": {"id": "M1", "library_id": "iec.loads.motor_3ph"}},
        },
        {
            "op": "wire",
            "msg": {"from": "K1.T1", "to": "M1.U1"},
            "reply": {"ok": True, "result": {"from": "K1.T1", "to": "M1.U1"}},
        },
    ]
    msg = coerce_wire_pins(
        {"op": "wire", "from": "K1.T2", "to": "M1.U2", "net": "T2"},
        Path("missing.pcad.yaml"),
        turns,
    )
    assert not msg.get("_harness_error"), msg
    assert str(msg["to"]).upper() != "M1.U1"


def test_nudge_3wire_heading_asks_for_start(tmp_path: Path):
    from panelcad import Session

    job = tmp_path / "job.pcad.yaml"
    session = Session.create("FIG. 3 - 3-WIRE CONTROL: FUSING IN BOTH PRIMARY LINES")
    session.save(job)
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "FIG. 3 - 3-WIRE CONTROL: FUSING IN BOTH PRIMARY LINES"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        {
            "op": "place",
            "msg": {"ref": "M1", "library_id": "iec.loads.motor_3ph"},
            "reply": {"ok": True, "result": {"id": "M1", "library_id": "iec.loads.motor_3ph"}},
        },
        {
            "op": "place",
            "msg": {"ref": "FU1", "library_id": "iec.protection.fuse_10a"},
            "reply": {"ok": True, "result": {"id": "FU1", "library_id": "iec.protection.fuse_10a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "MCR1", "library_id": "iec.contactors.ac3_9a"},
            "reply": {"ok": True, "result": {"id": "MCR1", "library_id": "iec.contactors.ac3_9a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "OL1", "library_id": "iec.protection.overload_9a"},
            "reply": {"ok": True, "result": {"id": "OL1", "library_id": "iec.protection.overload_9a"}},
        },
        {
            "op": "wire",
            "msg": {"from": "FU1.1", "to": "MCR1.A1"},
            "reply": {"ok": True, "result": {"from": "FU1.1", "to": "MCR1.A1"}},
        },
        {
            "op": "wire",
            "msg": {"from": "FU1.1", "to": "M1.U1"},
            "reply": {"ok": True, "result": {"from": "FU1.1", "to": "M1.U1"}},
        },
    ]
    hint = _nudge_from_print(turns, job)
    assert "START" in hint
    assert "pb_no" in hint


def test_check_3wire_heading_requires_coil(tmp_path: Path):
    job = tmp_path / "missing.pcad.yaml"
    turns = [
        {
            "op": "create_project",
            "msg": {"name": "3-WIRE CONTROL: FUSING IN 1 LINE ONLY"},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
        {"op": "library_search", "reply": {"ok": True}},
        {
            "op": "place",
            "msg": {"ref": "M1", "library_id": "iec.loads.motor_3ph"},
            "reply": {"ok": True, "result": {"id": "M1", "library_id": "iec.loads.motor_3ph"}},
        },
        {
            "op": "place",
            "msg": {"ref": "FU1", "library_id": "iec.protection.fuse_10a"},
            "reply": {"ok": True, "result": {"id": "FU1", "library_id": "iec.protection.fuse_10a"}},
        },
        {
            "op": "place",
            "msg": {"ref": "START", "library_id": "iec.operators.pb_no"},
            "reply": {"ok": True, "result": {"id": "START", "library_id": "iec.operators.pb_no"}},
        },
        {
            "op": "place",
            "msg": {"ref": "STOP", "library_id": "iec.operators.pb_nc"},
            "reply": {"ok": True, "result": {"id": "STOP", "library_id": "iec.operators.pb_nc"}},
        },
        {
            "op": "wire",
            "msg": {"from": "FU1.1", "to": "M1.U1"},
            "reply": {"ok": True, "result": {"from": "FU1.1", "to": "M1.U1"}},
        },
        {
            "op": "wire",
            "msg": {"from": "START.13", "to": "M1.U1"},
            "reply": {"ok": True, "result": {"from": "START.13", "to": "M1.U1"}},
        },
        {"op": "save", "reply": {"ok": True}},
    ]
    ok, reason = _check_from_print(turns, job)
    assert ok is False
    assert "coil" in reason.lower()
    hint = _nudge_from_print(turns, job)
    assert "contactors.ac3_9a" in hint or "K1" in hint


def test_compact_state_missing_job(tmp_path: Path):
    from panelcad.sharpen.loop import compact_state

    missing = tmp_path / "nope.pcad.yaml"
    state = compact_state(missing)
    assert state["exists"] is False
    assert state["sheets"] == []


def test_compact_state_devices_pins_wires(tmp_path: Path):
    from panelcad import Session
    from panelcad.sharpen.loop import compact_state

    job = tmp_path / "job.pcad.yaml"
    session = Session.create("Relay Logic")
    session.place("iec.relays.k_4co_24vdc", ref="CR1", x=80, y=80)
    session.place("iec.operators.pb_no", ref="PB1", x=40, y=80)
    session.wire("PB1.13", "CR1.A1", net="L1")
    session.save(job)
    state = compact_state(job)
    assert state["exists"] is True
    assert state["active"]
    main = state["sheets"][0]
    refs = {d["ref"]: d for d in main["devices"]}
    assert "CR1" in refs
    assert "PB1" in refs
    assert "A1" in refs["CR1"]["pins"]
    assert any(w["from"].startswith("PB1.") and w["to"].startswith("CR1.") for w in main["wires"])
    assert "CR1" not in main["unwired"]
    assert "PB1" not in main["unwired"]
    dumped = json.dumps(state)
    assert "pin_overrides" not in dumped
    assert "properties" not in dumped


def test_chat_window_keeps_last_three_rewritten_ops(tmp_path: Path):
    from panelcad.sharpen.loop import WINDOW_KEEP, ChatTurn, chat_window

    job = tmp_path / "missing.pcad.yaml"
    recent = [
        ChatTurn('{"op":"create_project","name":"A"}', "RESULT: 1"),
        ChatTurn('{"op":"save"}', "RESULT: 2"),
        ChatTurn('{"op":"set_title_block"}', "RESULT: 3"),
        ChatTurn('{"op":"library_search","query":"relay"}', "RESULT: 4"),
        ChatTurn('{"op":"place","ref":"CR1"}', "RESULT: 5"),
    ]
    messages = chat_window(
        system="SYS",
        task_user="TASK",
        job=job,
        recent=recent,
    )
    assert WINDOW_KEEP == 3
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("TASK")
    assert "STATE:" in messages[1]["content"]
    assistants = [m["content"] for m in messages if m["role"] == "assistant"]
    assert assistants == [
        '{"op":"set_title_block"}',
        '{"op":"library_search","query":"relay"}',
        '{"op":"place","ref":"CR1"}',
    ]
    assert '{"op":"create_project","name":"A"}' not in json.dumps(messages)
    assert "RESULT: 1" not in json.dumps(messages)
    assert "RESULT: 2" not in json.dumps(messages)


def test_from_print_window_drops_raw_prose_and_injects_state(tmp_path: Path):
    from panelcad.sharpen.loop import ScriptedClient, run_sharpen

    assert from_print_task().windowed is True
    client = ScriptedClient(
        [
            "Here is my plan for the motor starter kit.",
            '{"op":"create_project","name":"Relay Logic"}',
            '{"op":"save"}',
            '{"op":"set_title_block","drawing_number":"FIG. P2","rev":"","company":""}',
            '{"op":"library_search","query":"relay"}',
            '{"op":"place","library_id":"iec.relays.k_4co_24vdc","ref":"CR1","x":80,"y":80,"sheet":"main"}',
            '{"op":"place","library_id":"iec.operators.pb_no","ref":"PB1","x":40,"y":80,"sheet":"main"}',
            '{"op":"place","library_id":"iec.operators.pilot_light","ref":"PL1","x":200,"y":80,"sheet":"main"}',
            '{"op":"done","summary":"placed"}',
        ]
    )
    run_sharpen(
        "from-print",
        client=client,
        via_cli=False,
        scratch=tmp_path / "window",
        copy=False,
        fresh=True,
        max_turns=9,
    )
    assert client.calls, "ScriptedClient must record complete() payloads"
    blob = json.dumps(client.calls)
    assert "Here is my plan for the motor starter kit." not in blob
    last = client.calls[-1]
    assert last[0]["role"] == "system"
    assert last[1]["role"] == "user"
    assert "STATE:" in last[1]["content"]
    assistants = [m for m in last if m["role"] == "assistant"]
    assert len(assistants) <= 3
    for msg in assistants:
        assert msg["content"].lstrip().startswith("{")
        assert "motor starter kit" not in msg["content"]
    placed = json.dumps(last[1])
    assert "CR1" in placed or "CR1" in json.dumps(last)


def test_suite_task_keeps_full_transcript(tmp_path: Path):
    src = _copy_example(tmp_path)
    client = ScriptedClient(
        [
            '{"op":"open","path":"x"}',
            '{"op":"list_sheets"}',
            '{"op":"done","summary":"listed sheets"}',
        ]
    )
    result = run_sharpen(
        "list-sheets",
        project=src,
        client=client,
        via_cli=False,
        scratch=tmp_path / "suite-window",
    )
    assert result.ok, result.reason
    assert from_print_task().windowed is True
    assert len(client.calls) >= 3
    last = client.calls[-1]
    assistants = [m["content"] for m in last if m["role"] == "assistant"]
    assert len(assistants) >= 2
    assert any("list_sheets" in c for c in assistants)
    assert any("open" in c for c in assistants)


def _phase_turns_title() -> list[dict]:
    return [
        {"op": "create_project", "msg": {"name": "Power"}, "reply": {"ok": True}},
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
    ]


def test_current_phase_from_turns_and_job(tmp_path: Path):
    job = tmp_path / "missing.pcad.yaml"
    assert current_phase([], job) == "TITLE"
    assert current_phase(
        [{"op": "create_project", "reply": {"ok": True}}], job
    ) == "TITLE"
    title = _phase_turns_title()
    assert current_phase(title, job) == "PLACE"

    placed = title + [
        {"op": "library_search", "reply": {"ok": True}},
        _placed("CR1", "iec.contactors.coil"),
        _placed("PB1", "iec.operators.pb_no"),
        _placed("FU1", "iec.protection.fuse_10a"),
    ]
    assert current_phase(placed, job) == "WIRE"

    wired = placed + [
        _wired("PB1.13", "CR1.A1"),
        _wired("FU1.2", "PB1.11"),
    ]
    assert current_phase(wired, job) == "CLOSE"


def test_phase_cards_are_split_and_generic():
    assert "PHASE TITLE" in INSTRUCTION_TITLE
    assert "copy printed boxes" in INSTRUCTION_TITLE
    assert "Role words" not in INSTRUCTION_TITLE
    assert "iec.plc.compact_16dio" not in INSTRUCTION_TITLE
    assert "Role words" in INSTRUCTION_PLACE
    assert "iec.operators.pb_no" in INSTRUCTION_PLACE
    assert "Never A1" in INSTRUCTION_WIRE
    assert "iec.plc.compact_16dio" not in INSTRUCTION_WIRE
    assert "Do not place or wire" in INSTRUCTION_CLOSE
    assert "Role words" not in INSTRUCTION_CLOSE
    banned = _private_job_tokens()
    if banned is not None:
        assert banned[0] not in INSTRUCTION
    assert PHASE_OPS["TITLE"] == (
        "create_project",
        "save",
        "set_title_block",
        "set_sheet_title",
    )
    assert PHASE_OPS["WIRE"] == ("wire",)
    union = tuple(op for ops in PHASE_OPS.values() for op in ops if op != "done")
    for op in FROM_PRINT_OPS:
        assert op in union
    title = phase_instruction("TITLE")
    place = phase_instruction("PLACE")
    wire = phase_instruction("WIRE")
    close = phase_instruction("CLOSE")
    assert title != place != wire != close
    assert "PHASE TITLE" in title
    assert "PHASE PLACE" in place
    assert "PHASE WIRE" in wire
    assert "PHASE CLOSE" in close


def test_from_print_window_uses_current_phase_card_only(tmp_path: Path):
    assert from_print_task().windowed is True
    client = ScriptedClient(
        [
            '{"op":"create_project","name":"Power"}',
            '{"op":"save"}',
            '{"op":"set_title_block","drawing_number":"FIG. P2","rev":"","company":""}',
            '{"op":"library_search","query":"coil"}',
            '{"op":"place","library_id":"iec.contactors.coil","ref":"CR1","x":80,"y":80,"sheet":"main"}',
            '{"op":"place","library_id":"iec.operators.pb_no","ref":"PB1","x":40,"y":80,"sheet":"main"}',
            '{"op":"place","library_id":"iec.protection.fuse_10a","ref":"FU1","x":40,"y":160,"sheet":"main"}',
            '{"op":"wire","from":"PB1.13","to":"CR1.A1","net":"L1","sheet":"main"}',
            '{"op":"wire","from":"FU1.2","to":"PB1.11","net":"L1","sheet":"main"}',
            '{"op":"cleanup"}',
            '{"op":"validate"}',
            '{"op":"save"}',
            '{"op":"done","summary":"recreated"}',
        ]
    )
    run_sharpen(
        "from-print",
        client=client,
        via_cli=False,
        scratch=tmp_path / "phases",
        copy=False,
        fresh=True,
        max_turns=13,
    )
    assert len(client.calls) >= 8

    def user_text(call: list[dict[str, str]]) -> str:
        return next(m["content"] for m in call if m["role"] == "user")

    def system_text(call: list[dict[str, str]]) -> str:
        return next(m["content"] for m in call if m["role"] == "system")

    first_user = user_text(client.calls[0])
    first_sys = system_text(client.calls[0])
    assert "PHASE TITLE" in first_user
    assert "STATE:" in first_user
    assert "Role words" not in first_user
    assert "iec.plc.compact_16dio" not in first_user
    assert "create_project" in first_sys
    assert "library_search" not in first_sys

    place_user = user_text(client.calls[3])
    place_sys = system_text(client.calls[3])
    assert "PHASE PLACE" in place_user
    assert "Role words" in place_user
    assert "PHASE TITLE" not in place_user
    assert "library_search" in place_sys
    assert "- wire:" not in place_sys.lower()

    wire_idx = None
    close_idx = None
    for i, call in enumerate(client.calls):
        blob = user_text(call)
        if "PHASE WIRE" in blob and wire_idx is None:
            wire_idx = i
        if "PHASE CLOSE" in blob and close_idx is None:
            close_idx = i
    assert wire_idx is not None, json.dumps(
        [user_text(c)[:80] for c in client.calls], indent=2
    )
    wire_user = user_text(client.calls[wire_idx])
    assert "Never A1" in wire_user or "A1–A2" in wire_user or "A1-A2" in wire_user
    assert "iec.plc.compact_16dio" not in wire_user
    assert "Role words" not in wire_user
    assert "library_search" not in system_text(client.calls[wire_idx])

    assert close_idx is not None and close_idx > wire_idx
    close_user = user_text(client.calls[close_idx])
    assert "PHASE CLOSE" in close_user
    assert "Do not place or wire" in close_user
    assert "Role words" not in close_user
    assert "iec.plc.compact_16dio" not in close_user
    assert "cleanup" in system_text(client.calls[close_idx])


def _title_ops(name: str) -> list[dict]:
    return [
        {
            "op": "create_project",
            "msg": {"name": name},
            "reply": {"ok": True},
        },
        {"op": "save", "reply": {"ok": True}},
        {"op": "set_title_block", "reply": {"ok": True}},
    ]


def test_text_layer_name_mismatch_stays_in_title():
    """A text layer keeps TITLE until the project name shares a printed word."""

    raw = read_pdf_page_text(EXAMPLE_PDF, 1)
    assert raw.strip()
    token = set_from_print_page_text("PAGE 1\n" + raw.strip())
    job = Path("missing.pcad.yaml")
    try:
        fields = _printed_fields(from_print_page_text())
        assert fields["drawing_number"]
        assert fields["company"]
        assert fields["heading_words"]
        wrong = _title_ops("Water Filter")
        assert current_phase(wrong, job) == "TITLE"
        window = from_print_window_user(wrong, job)
        assert "PRINTED PAGE" in window
        assert fields["project"].split()[0] in window
        nudged = _nudge_from_print(wrong[:2], job)
        assert fields["drawing_number"] in nudged
        assert fields["company"] in nudged
        assert "Water Filter" not in nudged
        fixed = rewrite_from_print(
            {"op": "create_project", "name": "Water Filter"},
            job,
            [],
        )
        assert "Water" not in str(fixed.get("name"))
        assert _real_words_overlap(str(fixed.get("name")), fields["heading_words"])
        copied = _title_ops(str(fields["project"] or fields["title"]))
        assert current_phase(copied, job) == "PLACE"
        block = coerce_title(
            {
                "op": "set_title_block",
                "drawing_number": "235-0-15",
                "company": "PanelCAD",
            },
            job,
            wrong,
        )
        assert block["drawing_number"] == fields["drawing_number"]
        assert block["company"] == fields["company"]
        assert _real_words_overlap(str(block.get("name")), fields["heading_words"])
    finally:
        _FROM_PRINT_PAGE_TEXT.reset(token)


def _real_words_overlap(name: str, words: set[str]) -> bool:
    found = {w.upper() for w in __import__("re").findall(r"[A-Za-z]{3,}", name)}
    return bool(found & set(words))


def test_power_page_text_does_not_demand_start_stop():
    """Power-circuit text must not turn a STARTER+MOTOR job name into a 3-wire kit."""

    raw = read_pdf_page_text(EXAMPLE_PDF, 1)
    token = set_from_print_page_text("PAGE 1\n" + raw.strip())
    job = Path("missing.pcad.yaml")
    try:
        assert _power_device_page(from_print_page_text())
        letters = {kind for _ref, kind in _device_designators(from_print_page_text())}
        assert letters <= {"Q", "K", "F", "M"}
        assert "Q" in letters and "M" in letters
        turns = _title_ops("DOL Motor Starter") + [
            {"op": "library_search", "reply": {"ok": True}},
        ]
        hints = _active_hints(turns, job)
        assert "3wire" not in hints
        gap = _heading_place_gap(turns, job) or ""
        assert "START" not in gap
        assert "STOP" not in gap
        nudge = _nudge_from_print(turns, job)
        assert "pb_no" not in nudge
        assert "START" not in nudge
        assert "STOP" not in nudge
        pushed = rewrite_from_print(
            {
                "op": "place",
                "library_id": "iec.operators.pb_no",
                "ref": "PB1",
                "x": 40,
                "y": 40,
            },
            job,
            turns,
        )
        assert pushed.get("_harness_error")
        relay = rewrite_from_print(
            {
                "op": "place",
                "library_id": "iec.relays.k_4co_24vdc",
                "ref": "CR1",
                "x": 80,
                "y": 40,
            },
            job,
            turns,
        )
        assert relay.get("_harness_error")
        contactor = rewrite_from_print(
            {
                "op": "place",
                "library_id": "iec.contactors.ac3_9a",
                "ref": "K1",
                "x": 80,
                "y": 80,
            },
            job,
            turns,
        )
        assert not contactor.get("_harness_error"), contactor
        placed = turns + [
            _placed("Q1", "iec.breakers.mcb_3p_32a"),
            _placed("K1", "iec.contactors.ac3_9a"),
            _placed("F1", "iec.protection.overload_9a"),
            _placed("M1", "iec.loads.motor_3ph"),
        ]
        wire_hint = _nudge_from_print(placed, job)
        assert "START" not in wire_hint
        assert "pb_no" not in wire_hint
        ok, reason = _check_from_print(placed, job)
        assert ok is False
        assert "3-wire" not in reason.lower()
        assert "no start" not in reason.lower()
        synthetic = set_from_print_page_text(
            "COMPANY\nNorthwind Pumps\n"
            "PROJECT Widget Line TITLE\n"
            "Power DESCRIPTION Motor power circuit DWG No. B-42 STANDARD\n"
            "IEC\nQ\nQ1\nK\nK1\nM\nM1\nL1 L2 L3\n"
        )
        try:
            hint = _nudge_from_print(_title_ops("Widget Line")[:2], Path("missing.pcad.yaml"))
            assert "B-42" in hint
            assert "Northwind Pumps" in hint
            assert "E-001" not in hint
        finally:
            _FROM_PRINT_PAGE_TEXT.reset(synthetic)
    finally:
        _FROM_PRINT_PAGE_TEXT.reset(token)


def test_no_text_layer_keeps_image_only_title_and_3wire():
    """Scanned pages still finish TITLE on any name, and MOTOR STARTER stays 3-wire."""

    assert from_print_page_text() == ""
    job = Path("missing.pcad.yaml")
    assert current_phase(_phase_turns_title(), job) == "PLACE"
    assert current_phase(_title_ops("Water Filter"), job) == "PLACE"
    starter = _title_ops("MOTOR STARTER") + [
        {"op": "library_search", "reply": {"ok": True}},
    ]
    gap = _heading_place_gap(starter, job) or ""
    assert "START" in gap
    assert "3wire" in _active_hints(starter, job)
    assert "3wire" in _print_hints("MOTOR STARTER")
    same = rewrite_from_print(
        {"op": "create_project", "name": "Water Filter"},
        job,
        [],
    )
    assert same["name"] == "Water Filter"


def _wired_net(frm: str, to: str, net: str) -> dict:
    return {
        "op": "wire",
        "msg": {"op": "wire", "from": frm, "to": to, "net": net},
        "reply": {"ok": True, "result": {"from": frm, "to": to, "net": net}},
    }


_THREE_PHASE_TEXT = """\
COMPANY
Northwind Pumps
PROJECT Widget Line TITLE
Power DESCRIPTION Motor power circuit DWG No. B-42
Q1
K1
F1
M1
L1 L2 L3
T1 T2 T3
U1 V1 W1
"""


def _one_pole_turns() -> list[dict]:
    return _title_ops("Widget Line") + [
        {"op": "library_search", "reply": {"ok": True}},
        _placed("Q1", "iec.breakers.mcb_3p_32a"),
        _placed("K1", "iec.contactors.ac3_9a"),
        _placed("F1", "iec.protection.overload_9a"),
        _placed("M1", "iec.loads.motor_3ph"),
        _wired_net("Q1.2", "K1.L1", "L1"),
        _wired_net("K1.T1", "F1.1", "T1"),
        _wired_net("F1.2", "M1.U1", "U1"),
        {"op": "save", "reply": {"ok": True}},
    ]


def test_power_page_one_pole_stays_in_wire():
    """L1/T1/U1 alone is not the three phases named on the page. No cleanup yet."""

    token = set_from_print_page_text(_THREE_PHASE_TEXT)
    job = Path("missing.pcad.yaml")
    try:
        turns = _one_pole_turns()
        assert current_phase(turns, job) == "WIRE"
        assert "cleanup" not in from_print_phase_allowed(turns, job)
        ok, reason = _check_from_print(turns, job)
        assert ok is False
        assert "L2" in reason or "pole" in reason.lower()
        nudge = _nudge_from_print(turns, job).replace(" ", "")
        assert '"net":"L2"' in nudge
        assert '"net":"L1"' not in nudge
        blocked = rewrite_from_print({"op": "cleanup"}, job, turns)
        assert blocked.get("_harness_error")
        assert "cleanup" in blocked["_harness_error"].lower()
    finally:
        _FROM_PRINT_PAGE_TEXT.reset(token)


def test_page_without_l2_l3_keeps_old_wire_minimum():
    """No L2/L3 in the text layer: two wires can still leave WIRE. 3-wire kits stay."""

    token = set_from_print_page_text("Power circuit")
    job = Path("missing.pcad.yaml")
    try:
        wired = _phase_turns_title() + [
            {"op": "library_search", "reply": {"ok": True}},
            _placed("CR1", "iec.contactors.coil"),
            _placed("PB1", "iec.operators.pb_no"),
            _placed("FU1", "iec.protection.fuse_10a"),
            _wired("PB1.13", "CR1.A1"),
            _wired("FU1.2", "PB1.11"),
            {"op": "save", "reply": {"ok": True}},
        ]
        assert current_phase(wired, job) == "CLOSE"
        ok, reason = _check_from_print(wired, job)
        assert ok is True, reason
    finally:
        _FROM_PRINT_PAGE_TEXT.reset(token)
    starter = _title_ops("MOTOR STARTER") + [
        {"op": "library_search", "reply": {"ok": True}},
    ]
    gap = _heading_place_gap(starter, job) or ""
    assert "START" in gap
    assert "3wire" in _active_hints(starter, job)
