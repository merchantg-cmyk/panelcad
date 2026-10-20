"""Round-trip import/export for project, sheets, library, BOM, wires, title block, prints."""

from __future__ import annotations

import csv
import json
import shutil
from io import StringIO
from pathlib import Path
from typing import Any

import yaml

from panelcad.core.cables import pull_rows, termination_rows
from panelcad.core.models import Project, Sheet, TitleBlock
from panelcad.core.primitives import Primitive
from panelcad.io.bom import (
    csv_header_is_bom,
    grouped_rows,
    instance_rows,
    unrecognized_csv_message,
    write_bom_csv,
)
from panelcad.io.yaml_format import load_project, save_project
from panelcad.render.reports import wire_rows, write_csv

MANIFEST_NAME = "manifest.json"
PROJECT_YAML = "project.pcad.yaml"
PROJECT_JSON = "project.json"
# A photo or scan is a print. sniff reports these as "pdf", the kind File → Import
# already sends to Import print.
SCAN_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def _write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _prim_draw(prim: Any) -> dict[str, Any]:
    if not isinstance(prim, Primitive):
        prim = Primitive.model_validate(prim)
    if prim.type == "rect":
        item: dict[str, Any] = {"rect": [prim.x, prim.y, prim.w, prim.h]}
        if prim.fill:
            item["fill"] = True
        return item
    if prim.type == "line":
        return {"line": [prim.x, prim.y, prim.x2, prim.y2]}
    if prim.type == "circle":
        item = {"circle": [prim.x, prim.y, prim.r]}
        if prim.fill:
            item["fill"] = True
        return item
    if prim.type == "polyline":
        return {"polyline": [list(p) for p in prim.points]}
    if prim.type == "text":
        return {"text": [prim.x, prim.y, prim.text, prim.size]}
    return prim.model_dump()


def used_library_files(session: Any) -> dict[str, dict[str, Any]]:
    """Group in-use devices by library name for YAML dump."""
    ids = {c.library_id for sh in session.project.sheets for c in sh.components}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for lid in sorted(ids):
        device = session.catalog.get(lid)
        lib = lid.split(".", 1)[0]
        grouped.setdefault(lib, []).append(
            {
                "id": device.id,
                "name": device.name,
                "category": device.category,
                "ref_prefix": device.ref_prefix,
                "tags": list(device.tags),
                "schematic": {
                    "width": device.schematic_width,
                    "height": device.schematic_height,
                    "pins": [p.model_dump() for p in device.pins],
                    "draw": [_prim_draw(p) for p in device.primitives],
                },
                "panel": {
                    "w": device.panel.width_mm,
                    "h": device.panel.height_mm,
                    "d": device.panel.depth_mm,
                    "din": device.panel.din_rail,
                    "in_panel": device.panel.in_panel,
                    "mount": device.panel.mount,
                    "dims": device.panel.dim_source,
                },
                "ports": [p.model_dump() for p in device.ports],
                "props": dict(device.properties),
            }
        )
    return {lib: {"library": lib, "version": "0.1", "devices": devices} for lib, devices in grouped.items()}


def sniff(path: Path) -> str:
    path = Path(path)
    if path.is_dir():
        return "package"
    suffix = path.suffix.lower()
    name = path.name.lower()
    if name.endswith(".pcad.yaml") or name.endswith(".pcad.yml"):
        return "project"
    if suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a YAML mapping")
        if data.get("format") == "panelcad" or "sheets" in data:
            return "project"
        if "devices" in data:
            return "library"
        if "components" in data and "kind" in data:
            return "sheet"
        if "company" in data or "drawing_number" in data or "revisions" in data:
            return "title_block"
        return "yaml"
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and (data.get("format") == "panelcad" or "sheets" in data):
            return "project_json"
        return "json"
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = [h.strip() for h in next(reader, [])]
        lowered = [h.lower() for h in header if h]
        # Wires and I/O keep their own importers. A parts list is everything
        # else that names a part, a maker, a description, or a quantity.
        if "from" in lowered and "to" in lowered:
            return "wires"
        if "address" in lowered and ("module" in lowered or "plc" in lowered):
            return "io"
        if csv_header_is_bom(header):
            return "bom"
        raise ValueError(unrecognized_csv_message(header))
    if suffix == ".pdf" or suffix in SCAN_SUFFIXES:
        return "pdf"
    if suffix == ".svg":
        return "svg"
    raise ValueError(f"cannot detect format of {path.name}")


def export_package(
    session: Any,
    dest: Path | str,
    *,
    pdf: bool = True,
    paper: str = "mixed",
    sections: list[str] | None = None,
) -> Path:
    from panelcad.core.troubleshoot import write_troubleshoot

    root = Path(dest)
    if root.suffix:
        root = root.with_suffix("")
    if root.exists() and root.is_file():
        raise ValueError(f"{root} exists and is a file")
    root.mkdir(parents=True, exist_ok=True)
    save_project(session.project, root / PROJECT_YAML, backup=False)
    (root / PROJECT_JSON).write_text(
        json.dumps(session.project.model_dump(mode="json", by_alias=True), indent=2),
        encoding="utf-8",
    )
    _write_yaml(root / "title_block.yaml", session.project.title_block.model_dump(mode="json"))
    from panelcad.io.bom import with_needs_info_section

    write_bom_csv(
        root / "bom.csv",
        with_needs_info_section(instance_rows(session.project, session.catalog), session.project, session.catalog),
    )
    write_bom_csv(
        root / "bom_grouped.csv",
        with_needs_info_section(
            grouped_rows(session.project, session.catalog), session.project, session.catalog
        ),
    )
    write_csv(root / "wires.csv", wire_rows(session.project))
    write_csv(root / "cross_reference.csv", session.cross_reference_rows())
    write_csv(root / "terminals.csv", session.terminal_strip_rows())
    write_csv(root / "audit.csv", session.audit_schematic_panel())
    write_csv(root / "checks.csv", session.design_checks(include_suppressed=True))
    write_csv(root / "io.csv", session.io_rows())
    write_troubleshoot(session.project, session.catalog, root / "troubleshoot.json")
    write_csv(root / "cables.csv", session.cable_rows())
    write_csv(root / "cable_pull.csv", pull_rows(session.project))
    write_csv(root / "cable_terminations.csv", termination_rows(session.project))
    write_csv(root / "panel_wires.csv", session.panel_wire_annotations())
    sheets_dir = root / "sheets"
    for sheet in session.project.sheets:
        _write_yaml(sheets_dir / f"{sheet.id}.yaml", sheet.model_dump(mode="json", by_alias=True))
        session.export_svg(root / "svg" / f"{sheet.id}.svg", sheet_id=sheet.id)
        session.export_dxf(root / "dxf" / f"{sheet.id}.dxf", sheet_id=sheet.id)
    for lib, payload in used_library_files(session).items():
        _write_yaml(root / "library" / f"{lib}.yaml", payload)
    pdf_path = root / "print.pdf"
    if pdf:
        try:
            session.print_package(pdf_path, paper=paper, sections=sections)
        except Exception:
            try:
                session.export_pdf(pdf_path)
            except Exception:
                pdf_path = None  # still a valid package without PDF
    files = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())
    manifest = {
        "format": "panelcad-package",
        "version": "0.1",
        "name": session.project.name,
        "sheets": [s.id for s in session.project.sheets],
        "files": files,
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return root


def import_library(session: Any, path: Path | str) -> list[str]:
    src = Path(path)
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    lib = str(data.get("library", "local"))
    ids = [f"{lib}.{d.get('id')}" for d in data.get("devices", []) if d.get("id")]
    session.catalog.load_file(src)
    if session.path is not None:
        dest = session.path.parent / "library" / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        session.catalog.load_file(dest)
    return ids


def import_sheet(session: Any, path: Path | str, *, replace: bool = True) -> str:
    src = Path(path)
    if src.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError(f"cannot import {src.name} as a sheet; expected YAML")
    try:
        text = src.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot import {src.name} as a sheet; expected YAML") from exc
    data = yaml.safe_load(text)
    sheet = Sheet.model_validate(data)
    existing = [s for s in session.project.sheets if s.id == sheet.id]
    if existing:
        if not replace:
            raise ValueError(f"sheet '{sheet.id}' already exists")
        idx = session.project.sheets.index(existing[0])
        session.project.sheets[idx] = sheet
    else:
        session.project.sheets.append(sheet)
    return sheet.id


def import_title_block(session: Any, path: Path | str) -> None:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("title block file must be a mapping")
    tb = TitleBlock.model_validate(data)
    session.project.title_block = tb


def import_wires(session: Any, path: Path | str) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8-sig")
    reader = csv.DictReader(StringIO(raw))
    added, skipped, errors = [], [], []
    for row in reader:
        if not row:
            continue
        norm = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        frm = norm.get("from") or norm.get("frm")
        to = norm.get("to")
        sheet_id = norm.get("sheet") or None
        net = norm.get("net") or None
        if not frm or not to:
            skipped.append("incomplete")
            continue
        sheet = session.sheet(sheet_id) if sheet_id else session.sheet()
        if any(w.frm == frm and w.to == to for w in sheet.wires):
            skipped.append(f"{frm}->{to}")
            continue
        try:
            session.active_sheet_id = sheet.id
            w = session.wire(
                frm,
                to,
                net=net,
                sheet_id=sheet.id,
                label=norm.get("label") or None,
                number=norm.get("number") or None,
                color=norm.get("color") or None,
                gauge=norm.get("gauge") or None,
                cable=norm.get("cable") or None,
            )
            added.append(w.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{frm}->{to}: {exc}")
    return {"added": added, "skipped": skipped, "errors": errors}


def import_project_file(session: Any, path: Path, *, replace: bool = True) -> None:
    kind = sniff(path)
    if kind == "project":
        project = load_project(path)
    elif kind == "project_json":
        project = Project.model_validate(json.loads(path.read_text(encoding="utf-8")))
    else:
        raise ValueError(f"{path} is not a PanelCAD project")
    if replace:
        session.project = project
        session.active_sheet_id = project.sheets[0].id if project.sheets else ""
        session.catalog.load_defaults(path.parent)
    else:
        for sheet in project.sheets:
            if any(s.id == sheet.id for s in session.project.sheets):
                continue
            session.project.sheets.append(sheet)
        tb = project.title_block
        if not session.project.title_block.company and tb.company:
            session.project.title_block = tb


def import_package(session: Any, root: Path | str, *, replace: bool = True) -> dict[str, Any]:
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"{root} is not a package directory")
    result: dict[str, Any] = {"kind": "package", "imported": []}
    proj = root / PROJECT_YAML
    if proj.is_file():
        import_project_file(session, proj, replace=replace)
        result["imported"].append("project")
    lib_dir = root / "library"
    if lib_dir.is_dir():
        for f in sorted(lib_dir.glob("*.yaml")):
            import_library(session, f)
            result["imported"].append(f"library:{f.name}")
    bom = root / "bom.csv"
    if bom.is_file():
        result["bom"] = session.import_bom(bom, place_missing=False)
        result["imported"].append("bom")
    wires = root / "wires.csv"
    if wires.is_file():
        result["wires"] = import_wires(session, wires)
        result["imported"].append("wires")
    tb = root / "title_block.yaml"
    if tb.is_file() and not replace:
        import_title_block(session, tb)
        result["imported"].append("title_block")
    return result


def import_any(session: Any, path: Path | str, *, replace: bool = False) -> dict[str, Any]:
    path = Path(path)
    kind = sniff(path)
    if kind == "svg":
        raise ValueError(
            f"cannot import {path.name} as a project file; use print import to read the SVG vectors"
        )
    if kind == "pdf":
        raise ValueError(
            f"cannot import {path.name} as a project file; use print import for a PDF or scan"
        )
    if kind == "package":
        return import_package(session, path, replace=replace)
    if kind in {"project", "project_json"}:
        import_project_file(session, path, replace=True)
        return {"kind": kind, "name": session.project.name}
    if kind == "library":
        ids = import_library(session, path)
        return {"kind": "library", "devices": len(ids)}
    if kind == "sheet":
        sid = import_sheet(session, path)
        return {"kind": "sheet", "id": sid}
    if kind == "title_block":
        import_title_block(session, path)
        return {"kind": "title_block"}
    if kind == "bom":
        return {"kind": "bom", **session.import_bom(path)}
    if kind == "wires":
        return {"kind": "wires", **import_wires(session, path)}
    if kind == "io":
        return {"kind": "io", **session.import_io_list(path)}
    raise ValueError(f"do not know how to import {path} ({kind})")
