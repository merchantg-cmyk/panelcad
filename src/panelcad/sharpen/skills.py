"""One JSONL op is a skill. Same ops for every model. Weak ctx is the bar.

A skill is 10/10 only when a weak model (gemma3:4b) hits it ten times
in a row. A miss resets the streak. Start with from-print ops.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panelcad.sharpen.from_print import FROM_PRINT_OPS
from panelcad.sharpen.loop import DEFAULT_HOST, DEFAULT_MODEL, ModelClient, SharpenResult, run_sharpen
from panelcad.sharpen.tasks import Task

WEAK_NUM_CTX = 8192
WEAK_TEXT_CHARS = 12_000
SKILL_REPEAT = 10

FROM_PRINT_SKILLS: tuple[str, ...] = FROM_PRINT_OPS


def prompt_text_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def _create_project_ok(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
    for turn in turns:
        if turn.get("op") != "create_project":
            continue
        if (turn.get("reply") or {}).get("ok"):
            return True, "create_project ok"
    return False, "create_project never ok"


def _create_project_nudge(turns: list[dict[str, Any]], job: Path) -> str:
    if _create_project_ok(turns, job)[0]:
        return 'Send {"op":"done","summary":"created"}. No other op.'
    return (
        'Next op MUST be: {"op":"create_project","name":"Skill Draw"}. '
        "Do not save. Do not send any other op."
    )


def _title_ok(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
    landed = False
    for turn in turns:
        if turn.get("op") != "set_title_block":
            continue
        if (turn.get("reply") or {}).get("ok"):
            landed = True
            break
    if not landed:
        return False, "set_title_block never ok"
    from panelcad import Session

    tb = Session.open(job).get_title_block()
    dwg = str(tb.get("drawing_number") or "")
    revs = tb.get("revisions") or []
    rev = ""
    if revs and isinstance(revs[0], dict):
        rev = str(revs[0].get("rev") or "")
    if dwg == "FIG. 1" and rev == "A":
        return True, "set_title_block ok"
    return False, f"title dwg={dwg!r} rev={rev!r}"


def _title_nudge(turns: list[dict[str, Any]], job: Path) -> str:
    if _title_ok(turns, job)[0]:
        return 'Send {"op":"done","summary":"titled"}. No other op.'
    return (
        'Next op MUST be: {"op":"set_title_block","drawing_number":"FIG. 1","rev":"A"}. '
        "Do not save. Do not send any other op."
    )


def _search_ok(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
    for turn in turns:
        if turn.get("op") != "library_search":
            continue
        reply = turn.get("reply") or {}
        if not reply.get("ok"):
            continue
        query = str((turn.get("msg") or {}).get("query") or "").lower()
        devices = (reply.get("result") or {}).get("devices") or []
        if "relay" in query or devices:
            return True, "library_search ok"
    return False, "library_search never ok"


def _search_nudge(turns: list[dict[str, Any]], job: Path) -> str:
    if _search_ok(turns, job)[0]:
        return 'Send {"op":"done","summary":"searched"}. No other op.'
    return (
        'Next op MUST be: {"op":"library_search","query":"relay"}. '
        "Do not save. Do not send any other op."
    )


def skill_task(op: str) -> Task:
    """Tiny windowed task that scores one JSONL op."""

    if op == "create_project":
        return Task(
            id="skill-create_project",
            instruction=(
                'Send exactly {"op":"create_project","name":"Skill Draw"}. '
                'Then {"op":"done","summary":"created"}. '
                "Do not send save or any other op."
            ),
            allowed=("create_project",),
            max_turns=6,
            check=_create_project_ok,
            nudge=_create_project_nudge,
            needs_open=False,
            windowed=True,
            skill="create_project",
        )
    if op == "set_title_block":
        return Task(
            id="skill-set_title_block",
            instruction=(
                'Send exactly {"op":"set_title_block","drawing_number":"FIG. 1","rev":"A"}. '
                'Then {"op":"done","summary":"titled"}. '
                "Do not send save or any other op."
            ),
            allowed=("set_title_block",),
            max_turns=6,
            check=_title_ok,
            nudge=_title_nudge,
            needs_open=True,
            windowed=True,
            skill="set_title_block",
        )
    if op == "library_search":
        return Task(
            id="skill-library_search",
            instruction=(
                'Send exactly {"op":"library_search","query":"relay"}. '
                'Then {"op":"done","summary":"searched"}. '
                "Do not send save or any other op."
            ),
            allowed=("library_search",),
            max_turns=6,
            check=_search_ok,
            nudge=_search_nudge,
            needs_open=True,
            windowed=True,
            skill="library_search",
        )
    if op in SKILL_EXACT:
        body = json.dumps(SKILL_EXACT[op], default=str)
        return Task(
            id=f"skill-{op}",
            instruction=(
                f"Send exactly {body}. "
                f'Then {{"op":"done","summary":"{op}"}}. '
                + (
                    "Do not send any other op."
                    if op in {"save", "open"}
                    or str(op).startswith("export_")
                    or str(op).startswith("import_")
                    else "Do not send save or any other op."
                )
            ),
            allowed=(op,),
            max_turns=6,
            check=_exact_ok(op),
            nudge=_exact_nudge(op),
            needs_open=True,
            windowed=True,
            skill=op,
        )
    known = ", ".join(("create_project", "set_title_block", "library_search", *SKILL_EXACT))
    raise ValueError(f"skill {op!r} is not built yet. Built: {known}")


SKILL_EXACT: dict[str, dict[str, Any]] = {
    "place": {
        "op": "place",
        "library_id": "iec.relays.k_4co_24vdc",
        "ref": "CR1",
        "x": 80,
        "y": 80,
    },
    "wire": {"op": "wire", "from": "PB1.13", "to": "CR1.A1"},
    "add_sheet": {"op": "add_sheet", "sheet_id": "io", "title": "I/O", "kind": "io"},
    "set_active_sheet": {"op": "set_active_sheet", "sheet_id": "io"},
    "set_sheet_title": {"op": "set_sheet_title", "title": "Relay Logic"},
    "cleanup": {"op": "cleanup"},
    "validate": {"op": "validate"},
    "save": {"op": "save"},
    "help": {"op": "help"},
    "list_sheets": {"op": "list_sheets"},
    "get_title_block": {"op": "get_title_block"},
    "open": {"op": "open"},
    "ask_style": {"op": "ask_style"},
    "inspect": {"op": "inspect"},
    "list_components": {"op": "list_components"},
    "list_wires": {"op": "list_wires"},
    "inspect_device": {
        "op": "inspect_device",
        "library_id": "iec.relays.k_4co_24vdc",
    },
    "move": {"op": "move", "ref": "CR1", "x": 100, "y": 100},
    "set_properties": {
        "op": "set_properties",
        "ref": "CR1",
        "manufacturer": "Wago",
    },
    "export_pdf": {"op": "export_pdf"},
    "hello": {"op": "hello", "name": "Gemma"},
    "say": {"op": "say", "text": "ok"},
    "idle": {"op": "idle"},
    "list_prompts": {"op": "list_prompts"},
    "read_prompts": {"op": "read_prompts"},
    "prompt": {"op": "prompt", "text": "status"},
    "close_sheet": {"op": "close_sheet", "sheet_id": "extra"},
    "open_sheet": {"op": "open_sheet", "sheet_id": "extra"},
    "delete_sheet": {"op": "delete_sheet", "sheet_id": "extra"},
    "delete_component": {"op": "delete_component", "ref": "CR1"},
    "duplicate_component": {"op": "duplicate_component", "ref": "CR1"},
    "rotate_component": {"op": "rotate_component", "ref": "CR1", "degrees": 90},
    "delete_wire": {"op": "delete_wire", "wire_id": "w1"},
    "set_wire_label": {
        "op": "set_wire_label",
        "from": "PB1.13",
        "to": "CR1.A1",
        "label": "L1",
    },
    "undo": {"op": "undo"},
    "redo": {"op": "redo"},
    "place_from_bom": {
        "op": "place_from_bom",
        "library_id": "iec.relays.k_4co_24vdc",
        "ref": "CR1",
        "x": 40,
        "y": 40,
        "sheet": "io",
    },
    "save_library_device": {
        "op": "save_library_device",
        "id": "skill_relay",
        "library_id": "iec.relays.k_4co_24vdc",
        "name": "Skill relay",
    },
    "export_svg": {"op": "export_svg"},
    "export_dxf": {"op": "export_dxf"},
    "export_bom": {"op": "export_bom"},
    "export_wire_list": {"op": "export_wire_list"},
    "export_json": {"op": "export_json"},
    "export_package": {"op": "export_package"},
    "print_package": {"op": "print_package"},
    "import_bom": {"op": "import_bom", "path": "bom.csv"},
    "import_wires": {"op": "import_wires", "path": "wires.csv"},
    "import_library": {"op": "import_library", "path": "relays.yaml"},
    "import_any": {"op": "import_any", "path": "bom.csv"},
    "import_sheet": {"op": "import_sheet", "path": "sheet.yaml"},
    "add_rail": {
        "op": "add_rail",
        "rail_id": "r-test",
        "x": 40,
        "y": 80,
        "length_mm": 300,
        "sheet": "panel",
    },
    "delete_rail": {"op": "delete_rail", "rail_id": "r-test", "sheet": "panel"},
    "list_rails": {"op": "list_rails", "sheet": "panel"},
    "set_enclosure": {
        "op": "set_enclosure",
        "width_mm": 400,
        "height_mm": 400,
        "depth_mm": 200,
        "name": "Skill box",
        "sheet": "panel",
    },
    "layout_from_schematic": {"op": "layout_from_schematic", "sheet": "panel"},
    "recommend_cabinet": {"op": "recommend_cabinet", "sheet": "panel", "apply": False},
    "size_enclosure": {"op": "size_enclosure", "sheet": "panel", "apply": False},
    "recommend_enclosure": {"op": "recommend_enclosure", "sheet": "panel"},
    "apply_enclosure": {"op": "apply_enclosure", "sheet": "panel", "place_parts": False},
    "set_scale": {"op": "set_scale", "ref": "CR1", "scale": 1.1},
    "set_display_size": {
        "op": "set_display_size",
        "ref": "CR1",
        "width_mm": 54,
        "height_mm": 80,
    },
    "set_picture": {"op": "set_picture", "ref": "CR1"},
    "clear_picture": {"op": "clear_picture", "ref": "CR1"},
    "apply_cut_sheet_pictures": {"op": "apply_cut_sheet_pictures"},
    "request_oem_update": {"op": "request_oem_update", "refs": ["CR1"]},
    "update_oem": {
        "op": "update_oem",
        "ref": "CR1",
        "width_mm": 54,
        "height_mm": 80,
        "depth_mm": 70,
        "manufacturer": "Wago",
    },
    "set_pins_locked": {"op": "set_pins_locked", "ref": "CR1", "locked": True},
    "set_pin": {
        "op": "set_pin",
        "ref": "CR1",
        "pin_id": "A1",
        "x": 8,
        "y": 55,
        "side": "left",
    },
    "reset_pins": {"op": "reset_pins", "ref": "CR1"},
    "set_panel": {
        "op": "set_panel",
        "ref": "CR1",
        "width_mm": 54,
        "height_mm": 80,
        "depth_mm": 70,
        "din_rail": True,
    },
    "add_port": {
        "op": "add_port",
        "ref": "A1",
        "port_id": "COM1",
        "kind": "serial",
        "medium": "RS-485",
    },
    "list_ports": {"op": "list_ports", "ref": "A1"},
    "set_port": {"op": "set_port", "ref": "A1", "port_id": "COM1", "protocol": "Modbus"},
    "delete_port": {"op": "delete_port", "ref": "A1", "port_id": "COM1"},
    "set_address": {
        "op": "set_address",
        "ref": "A1",
        "ip": "192.168.1.10",
        "node": "1",
    },
    "set_network": {
        "op": "set_network",
        "name": "Plant LAN",
        "medium": "ethernet",
        "sheet": "net",
    },
    "layout_network": {"op": "layout_network", "sheet": "net"},
    "layout_io": {"op": "layout_io", "sheet": "io"},
    "set_pin_labels": {
        "op": "set_pin_labels",
        "ref": "CR1",
        "labels": {"A1": "COIL+"},
    },
}


def _skill_effect(op: str, job: Path) -> tuple[bool, str]:
    if op in {
        "cleanup",
        "validate",
        "save",
        "help",
        "list_sheets",
        "get_title_block",
        "open",
        "ask_style",
        "inspect",
        "list_components",
        "list_wires",
        "inspect_device",
        "export_pdf",
        "export_svg",
        "export_dxf",
        "export_bom",
        "export_wire_list",
        "export_json",
        "export_package",
        "print_package",
        "import_bom",
        "import_wires",
        "import_library",
        "import_any",
        "import_sheet",
        "hello",
        "say",
        "idle",
        "list_prompts",
        "read_prompts",
        "prompt",
        "list_rails",
        "list_ports",
        "recommend_cabinet",
        "size_enclosure",
        "recommend_enclosure",
        "apply_enclosure",
        "apply_cut_sheet_pictures",
        "request_oem_update",
        "undo",
        "redo",
        "save_library_device",
        "layout_from_schematic",
        "layout_network",
        "layout_io",
        "duplicate_component",
        "rotate_component",
        "set_scale",
        "set_display_size",
        "set_picture",
        "clear_picture",
        "update_oem",
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
        "set_wire_label",
        "place_from_bom",
        "add_rail",
        "delete_rail",
        "set_enclosure",
        "close_sheet",
        "open_sheet",
        "delete_sheet",
        "delete_component",
        "delete_wire",
    }:
        return True, f"{op} ok"
    from panelcad import Session

    session = Session.open(job)
    if op == "place":
        refs = [comp.id for sheet in session.project.sheets for comp in sheet.components]
        if "CR1" in refs:
            return True, "place ok"
        return False, f"CR1 missing {refs}"
    if op == "wire":
        pairs = [(w.frm, w.to) for sheet in session.project.sheets for w in sheet.wires]
        if ("PB1.13", "CR1.A1") in pairs:
            return True, "wire ok"
        return False, f"wires {pairs}"
    if op == "add_sheet":
        ids = [sheet.id for sheet in session.project.sheets]
        if "io" in ids:
            return True, "add_sheet ok"
        return False, f"sheets {ids}"
    if op == "set_active_sheet":
        if session.active_sheet_id == "io":
            return True, "set_active_sheet ok"
        return False, f"active={session.active_sheet_id}"
    if op == "set_sheet_title":
        title = session.sheet().title
        if title == "Relay Logic":
            return True, "set_sheet_title ok"
        return False, f"title={title!r}"
    if op == "move":
        refs = {c.id: c for sh in session.project.sheets for c in sh.components}
        comp = refs.get("CR1")
        if comp is None:
            return False, "CR1 missing"
        if abs(float(comp.x) - 100) < 0.5 and abs(float(comp.y) - 100) < 0.5:
            return True, "move ok"
        return False, f"CR1 at {comp.x},{comp.y}"
    if op == "set_properties":
        refs = {c.id: c for sh in session.project.sheets for c in sh.components}
        comp = refs.get("CR1")
        if comp is None:
            return False, "CR1 missing"
        maker = str((comp.properties or {}).get("manufacturer") or "")
        if maker == "Wago":
            return True, "set_properties ok"
        return False, f"manufacturer={maker!r}"
    return True, f"{op} ok"


def _exact_ok(op: str):
    def check(turns: list[dict[str, Any]], job: Path) -> tuple[bool, str]:
        for turn in turns:
            if turn.get("op") != op:
                continue
            reply = turn.get("reply") or {}
            if not reply.get("ok"):
                continue
            if op == "set_active_sheet":
                rid = str((reply.get("result") or {}).get("id") or "")
                if rid == "io":
                    return True, "set_active_sheet ok"
                return False, f"active reply {rid!r}"
            return _skill_effect(op, job)
        return False, f"{op} never ok"

    return check


def _exact_nudge(op: str):
    body = json.dumps(SKILL_EXACT[op], default=str)

    def nudge(turns: list[dict[str, Any]], job: Path) -> str:
        if _exact_ok(op)(turns, job)[0]:
            return f'Send {{"op":"done","summary":"{op}"}}. No other op.'
        return f"Next op MUST be: {body}. Do not save. Do not send any other op."

    return nudge


def seed_skill_job(op: str, path: Path) -> None:
    from panelcad import Session
    from panelcad.sharpen.suite import PNG_1X1

    dest = Path(path).parent
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "pic.png").write_bytes(PNG_1X1)
    session = Session.create("Skill Draw")

    def _cr1() -> None:
        session.place("iec.relays.k_4co_24vdc", ref="CR1", x=80, y=80)

    def _pair() -> None:
        session.place("iec.operators.pb_no", ref="PB1", x=40, y=80)
        _cr1()
        session.wire("PB1.13", "CR1.A1", wire_id="w1")

    def _extra() -> None:
        session.add_sheet("extra", title="Extra", kind="schematic", make_active=False)

    def _panel() -> None:
        session.add_sheet("panel", title="Panel", kind="panel_layout", make_active=False)

    def _io() -> None:
        session.add_sheet("io", title="I/O", kind="io", make_active=False)

    def _net() -> None:
        session.add_sheet("net", title="Ethernet", kind="network", make_active=False)

    def _plc() -> None:
        session.place("iec.plc.compact_16dio", ref="A1", x=80, y=40)

    if op in {"wire", "list_wires", "delete_wire", "set_wire_label"}:
        _pair()
    elif op == "set_active_sheet":
        _io()
    elif op in {
        "move",
        "set_properties",
        "list_components",
        "inspect",
        "delete_component",
        "duplicate_component",
        "rotate_component",
        "undo",
        "redo",
        "set_scale",
        "set_display_size",
        "set_picture",
        "request_oem_update",
        "update_oem",
        "set_pins_locked",
        "set_pin",
        "reset_pins",
        "set_panel",
        "set_pin_labels",
    }:
        _cr1()
    elif op == "clear_picture":
        _cr1()
        session.set_picture("CR1", dest / "pic.png")
    elif op == "close_sheet" or op == "delete_sheet":
        _extra()
    elif op == "open_sheet":
        _extra()
        session.project.sheet("extra").closed = True
    elif op == "place_from_bom":
        _cr1()
        _io()
    elif op in {"add_rail", "list_rails", "set_enclosure", "recommend_cabinet", "size_enclosure", "recommend_enclosure", "apply_enclosure"}:
        _panel()
    elif op == "delete_rail":
        _panel()
        session.add_rail("r-test", x=40, y=80, length_mm=300, sheet_id="panel")
    elif op == "layout_from_schematic":
        _cr1()
        _panel()
    elif op in {"add_port", "list_ports", "set_port", "delete_port", "set_address"}:
        _plc()
        if op in {"set_port", "delete_port"}:
            session.add_port("A1", "COM1", kind="serial", medium="RS-485")
    elif op == "set_network" or op == "layout_network":
        _net()
        if op == "layout_network":
            _plc()
    elif op == "layout_io":
        _io()
        session.place("iec.plc.compact_16dio", ref="A1", x=80, y=40, sheet_id="io")
    elif op in {"import_bom", "import_any"}:
        (dest / "bom.csv").write_text(
            "qty,library_id,manufacturer,part_number\n"
            "1,iec.terminals.x_2p5,Wago,2002-1201\n",
            encoding="utf-8-sig",
        )
    elif op == "import_wires":
        _pair()
        session.save(path)
        session.export_wire_list(dest / "wires.csv")
        return
    elif op == "import_library":
        from shutil import copy2

        src = Path(__file__).resolve().parents[3] / "libraries" / "builtin" / "relays.yaml"
        copy2(src, dest / "relays.yaml")
    elif op == "import_sheet":
        import yaml

        from panelcad.core.models import Page, Sheet

        extra = Sheet(id="imported", title="Imported", kind="schematic", page=Page())
        (dest / "sheet.yaml").write_text(
            yaml.safe_dump(extra.model_dump(mode="python"), sort_keys=False),
            encoding="utf-8",
        )
    session.save(path)


def prime_skill_session(op: str, agent: Any) -> None:
    """Mutations that only exist in the live Session (undo stack)."""

    if op == "undo":
        agent.handle({"op": "move", "ref": "CR1", "x": 100, "y": 100})
    elif op == "redo":
        agent.handle({"op": "move", "ref": "CR1", "x": 100, "y": 100})
        agent.handle({"op": "undo"})


def skill_tasks() -> dict[str, Task]:
    out: dict[str, Task] = {}
    for op in ("create_project", "set_title_block", "library_search", *SKILL_EXACT):
        task = skill_task(op)
        out[task.id] = task
    return out


@dataclass
class StreakResult:
    op: str
    n: int
    ok: bool
    streak: int
    miss: int
    runs: list[SharpenResult] = field(default_factory=list)


def run_skill_streak(
    op: str,
    *,
    n: int = SKILL_REPEAT,
    client: ModelClient | None = None,
    scratch: Path | None = None,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    via_cli: bool = False,
) -> StreakResult:
    """Run `n` trials. ok only if every trial hits. A miss resets streak."""

    from panelcad.sharpen.loop import OllamaClient, _run_dir

    task = skill_task(op)
    root = _run_dir(scratch)
    llm = client or OllamaClient(model=model, host=host, num_ctx=WEAK_NUM_CTX)
    runs: list[SharpenResult] = []
    streak = 0
    miss = 0
    for i in range(n):
        trial = root / f"{i:02d}"
        if not task.needs_open:
            result = run_sharpen(
                task.id,
                client=llm,
                model=model,
                host=host,
                via_cli=via_cli,
                scratch=trial,
                copy=False,
                fresh=True,
            )
        else:
            trial.mkdir(parents=True, exist_ok=True)
            job = trial / "job.pcad.yaml"
            seed_skill_job(op, job)
            result = run_sharpen(
                task.id,
                project=job,
                client=llm,
                model=model,
                host=host,
                via_cli=via_cli,
                scratch=trial,
                copy=False,
                fresh=False,
            )
        runs.append(result)
        if result.ok:
            streak += 1
        else:
            miss += 1
            streak = 0
    return StreakResult(
        op=op,
        n=n,
        ok=miss == 0 and len(runs) == n,
        streak=streak,
        miss=miss,
        runs=runs,
    )
