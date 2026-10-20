"""All-actions suite against a new PanelCAD drawing.

The harness stays in this package. A suite run creates one scratch job
under PanelCAD/.sharpen/ and walks every agent op in short tasks a 4B
model can hold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from panelcad.cli.agent import OPS
from panelcad.sharpen.seq import Atom, atom_task

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

A = Atom

SUITE_TASKS = {
    t.id: t
    for t in (
        atom_task(
            "fresh-create",
            (
                A("create_project", {"name": "Harness Draw"}),
                A("save"),
            ),
            needs_open=False,
            intro="Start a brand-new drawing. Do not open an old example. ",
        ),
        atom_task(
            "docs",
            (
                A("help"),
                A("hello", {"model": "gemma3:4b", "name": "Gemma"}),
                A("say", {"text": "operator online"}),
                A("idle"),
            ),
        ),
        atom_task(
            "sheets",
            (
                A(
                    "add_sheet",
                    {"sheet_id": "panel", "title": "Panel", "kind": "panel_layout"},
                    key="sheet_id",
                    accept_error=("already exists",),
                ),
                A(
                    "add_sheet",
                    {"sheet_id": "io", "title": "I/O", "kind": "io"},
                    key="sheet_id",
                    accept_error=("already exists",),
                ),
                A(
                    "add_sheet",
                    {"sheet_id": "net", "title": "Ethernet", "kind": "network"},
                    key="sheet_id",
                    accept_error=("already exists",),
                ),
                A(
                    "add_sheet",
                    {"sheet_id": "extra", "title": "Extra", "kind": "schematic"},
                    key="sheet_id",
                    accept_error=("already exists",),
                ),
                A("list_sheets"),
                A("set_sheet_title", {"title": "Main power", "sheet": "main"}),
                A("close_sheet", {"sheet": "extra"}),
                A("open_sheet", {"sheet": "extra"}),
                A("inspect", {"sheet": "main"}),
                A("save"),
            ),
        ),
        atom_task(
            "search",
            (
                A("library_search", {"query": "contactor"}),
                A("inspect_device", {"library_id": "iec.breakers.mcb_3p_32a"}),
                A("inspect_device", {"library_id": "iec.contactors.ac3_9a"}),
            ),
        ),
        atom_task(
            "devices",
            (
                A("set_active_sheet", {"sheet": "main"}),
                A(
                    "place",
                    {
                        "library_id": "iec.breakers.mcb_3p_32a",
                        "ref": "Q1",
                        "x": 40,
                        "y": 40,
                        "sheet": "main",
                    },
                    key="ref",
                    accept_error=("already exists",),
                ),
                A(
                    "place",
                    {
                        "library_id": "iec.contactors.ac3_9a",
                        "ref": "K1",
                        "x": 40,
                        "y": 120,
                        "sheet": "main",
                    },
                    key="ref",
                    accept_error=("already exists",),
                ),
                A("move", {"ref": "Q1", "x": 45, "y": 42, "sheet": "main"}),
                A(
                    "set_properties",
                    {"ref": "Q1", "manufacturer": "ABB", "part_number": "S203-C32", "sheet": "main"},
                ),
                A("rotate_component", {"ref": "K1", "degrees": 90, "sheet": "main"}),
                A("duplicate_component", {"ref": "K1", "sheet": "main"}),
                A("set_scale", {"ref": "Q1", "scale": 1.1, "sheet": "main"}),
                A("list_components", {"sheet": "main"}),
                A("cleanup", {"sheet": "main"}),
                A("save"),
            ),
        ),
        atom_task(
            "wires",
            (
                A("wire", {"from": "Q1.2", "to": "K1.L1", "net": "L1", "sheet": "main"}),
                A("list_wires", {"sheet": "main"}),
                A(
                    "set_wire_label",
                    {"wire_id": "w1", "from": "Q1.2", "to": "K1.L1", "label": "L1", "sheet": "main"},
                ),
                A("save"),
            ),
        ),
        atom_task(
            "pins",
            (
                A("set_pins_locked", {"ref": "Q1", "locked": False, "sheet": "main"}),
                A("set_pin", {"ref": "Q1", "pin_id": "2", "x": 8, "y": 55, "side": "bottom", "sheet": "main"}),
                A("set_pin_labels", {"ref": "Q1", "labels": {"2": "L1-LOAD"}, "sheet": "main"}),
                A("reset_pins", {"ref": "Q1", "sheet": "main"}),
                A("set_pins_locked", {"ref": "Q1", "locked": True, "sheet": "main"}),
                A("save"),
            ),
        ),
        atom_task(
            "panel",
            (
                A("set_active_sheet", {"sheet": "panel"}),
                A(
                    "set_enclosure",
                    {
                        "width_mm": 400,
                        "height_mm": 400,
                        "depth_mm": 200,
                        "name": "Harness box",
                        "sheet": "panel",
                    },
                ),
                A("add_rail", {"rail_id": "r-test", "x": 40, "y": 80, "length_mm": 300, "sheet": "panel"}),
                A("list_rails", {"sheet": "panel"}),
                A("layout_from_schematic", {"sheet": "panel"}),
                A("recommend_cabinet", {"sheet": "panel", "apply": False}),
                A("size_enclosure", {"sheet": "panel", "apply": False}),
                A("recommend_enclosure", {"sheet": "panel"}),
                A("apply_enclosure", {"sheet": "panel", "place_parts": False}),
                A(
                    "set_panel",
                    {
                        "ref": "Q1",
                        "width_mm": 54,
                        "height_mm": 80,
                        "depth_mm": 70,
                        "din_rail": True,
                        "sheet": "panel",
                    },
                ),
                A("save"),
            ),
        ),
        atom_task(
            "photo",
            (
                A("set_display_size", {"ref": "Q1", "width_mm": 54, "height_mm": 80, "sheet": "panel"}),
                A("set_picture", {"ref": "Q1", "path": "pic.png", "sheet": "panel"}),
                A("clear_picture", {"ref": "Q1", "sheet": "panel"}),
                A("request_oem_update", {"refs": ["Q1"]}),
                A(
                    "update_oem",
                    {
                        "ref": "Q1",
                        "width_mm": 54,
                        "height_mm": 80,
                        "depth_mm": 70,
                        "manufacturer": "ABB",
                        "part_number": "S203-C32",
                    },
                ),
                A("apply_cut_sheet_pictures"),
                A("save"),
            ),
        ),
        atom_task(
            "ports",
            (
                A(
                    "place",
                    {
                        "library_id": "iec.plc.compact_16dio",
                        "ref": "A1",
                        "x": 80,
                        "y": 40,
                        "sheet": "main",
                    },
                    key="ref",
                    accept_error=("already exists",),
                ),
                A("add_port", {"ref": "A1", "port_id": "COM1", "kind": "serial", "medium": "RS-485"}),
                A("list_ports", {"ref": "A1"}),
                A("set_port", {"ref": "A1", "port_id": "COM1", "protocol": "Modbus"}),
                A("set_address", {"ref": "A1", "ip": "192.168.1.10", "node": "1"}),
                A("set_network", {"name": "Plant LAN", "medium": "ethernet", "sheet": "net"}),
                A("layout_network", {"sheet": "net", "medium": "ethernet"}),
                A("delete_port", {"ref": "A1", "port_id": "COM1"}),
                A("save"),
            ),
        ),
        atom_task(
            "io",
            (
                A(
                    "place",
                    {
                        "library_id": "iec.plc.compact_16dio",
                        "ref": "A1",
                        "x": 80,
                        "y": 40,
                        "sheet": "io",
                    },
                    key="sheet",
                    accept_error=("already exists",),
                ),
                A("layout_io", {"sheet": "io"}),
                A("cleanup", {"sheet": "io"}),
                A("validate"),
                A("save"),
            ),
        ),
        atom_task(
            "title",
            (
                A("get_title_block"),
                A(
                    "set_title_block",
                    {
                        "company": "Harness Lab",
                        "drawing_number": "E-HARNESS-1",
                        "rev": "A",
                    },
                ),
                A("save"),
            ),
        ),
        atom_task(
            "export",
            (
                A("export_svg", {"path": "sheet.svg", "sheet": "main"}),
                A("export_pdf", {"path": "job.pdf"}),
                A("export_dxf", {"path": "sheet.dxf", "sheet": "main"}),
                A("export_bom", {"path": "bom.csv"}),
                A("export_wire_list", {"path": "wires.csv"}),
                A("export_json", {"path": "job.json"}),
                A("export_package", {"path": "pack"}),
                A("print_package", {"path": "package.pdf"}),
            ),
        ),
        atom_task(
            "roundtrip",
            (
                A("import_bom", {"path": "bom.csv"}),
                A("import_wires", {"path": "wires.csv"}),
                A("import_library", {"path": "relays.yaml"}),
                A("import_any", {"path": "bom.csv"}),
                A(
                    "import_sheet",
                    {"path": "pack/sheets/main.yaml"},
                    accept_error=("already exists", "exists"),
                ),
                A("save"),
            ),
        ),
        atom_task(
            "delete",
            (
                A("list_wires", {"sheet": "main"}),
                A("delete_wire", {"sheet": "main"}),
                A("duplicate_component", {"ref": "Q1", "sheet": "main"}),
                A("delete_component", {"sheet": "main"}),
                A("list_rails", {"sheet": "panel"}),
                A("delete_rail", {"sheet": "panel"}),
                A("delete_sheet", {"sheet": "extra"}),
                A("save"),
            ),
        ),
        atom_task(
            "prompts",
            (
                A("list_prompts"),
                A("read_prompts"),
                A("prompt", {"text": "status"}),
            ),
        ),
        atom_task(
            "finish",
            (
                A("validate"),
                A("save"),
            ),
        ),
    )
}

SUITE_ORDER: tuple[str, ...] = tuple(SUITE_TASKS)


def prepare_run_dir(run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pic.png").write_bytes(PNG_1X1)
    src = PACKAGE_ROOT / "libraries" / "builtin" / "relays.yaml"
    if src.is_file():
        (run_dir / "relays.yaml").write_bytes(src.read_bytes())
    return run_dir / "job.pcad.yaml"


def run_suite(
    *,
    model: str = "gemma3:4b",
    host: str = "http://127.0.0.1:11434",
    via_cli: bool = True,
    client: Any | None = None,
    scratch: Path | None = None,
    python: str | None = None,
    stop_on_fail: bool = False,
) -> dict[str, Any]:
    from panelcad.sharpen.loop import _run_dir, run_sharpen

    run_dir = scratch or _run_dir(None)
    job = prepare_run_dir(run_dir)
    results = []
    for i, task_id in enumerate(SUITE_ORDER):
        fresh = i == 0
        result = run_sharpen(
            task_id,
            project=None if fresh else job,
            model=model,
            host=host,
            via_cli=via_cli,
            client=client,
            scratch=run_dir,
            python=python,
            copy=False,
            fresh=fresh,
        )
        results.append(result)
        if stop_on_fail and not result.ok:
            break
    seen: set[str] = set()
    for result in results:
        for turn in result.turns:
            op = turn.get("op")
            reply = turn.get("reply") or {}
            if op and (reply.get("ok") or turn.get("op") == "done"):
                seen.add(str(op))
    missing = sorted(OPS - seen - {"done"})
    summary = {
        "ok": all(r.ok for r in results),
        "job": str(job),
        "run_dir": str(run_dir),
        "tasks": [
            {
                "id": r.task,
                "ok": r.ok,
                "reason": r.reason,
                "turns": len(r.turns),
                "elapsed_s": r.elapsed_s,
                "ops": [t.get("op") for t in r.turns if t.get("op")],
                "trace": str(r.trace),
            }
            for r in results
        ],
        "ops_ok": sorted(seen),
        "ops_missing": missing,
        "elapsed_s": round(sum(r.elapsed_s for r in results), 2),
    }
    (run_dir / "suite.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
