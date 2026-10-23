"""Round-trip import/export for project, sheets, library, BOM, wires, title block, prints."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
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
    if prim.type == "arc":
        return {"arc": [prim.x, prim.y, prim.r, prim.start, prim.sweep]}
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


# Package section -> data files it owns. Project/sheet YAML, library files and
# troubleshoot.json always ship: they are what makes the folder re-importable.
_SECTION_FILES: dict[str, tuple[str, ...]] = {
    "bom": ("bom.csv", "bom_grouped.csv"),
    "wires": ("wires.csv", "panel_wires.csv"),
    "from_to": ("wires.csv", "panel_wires.csv"),
    "xref": ("cross_reference.csv",),
    "terminals": ("terminals.csv",),
    "checks": ("checks.csv", "audit.csv"),
    "plc_io": ("io.csv",),
    "interconnect": ("cables.csv", "cable_pull.csv", "cable_terminations.csv"),
}


def export_package(
    session: Any,
    dest: Path | str,
    *,
    pdf: bool = True,
    paper: str = "mixed",
    sections: list[str] | None = None,
    sheet_ids: list[str] | None = None,
) -> Path:
    """Write the package folder and return it. See export_package_report."""

    return export_package_report(
        session, dest, pdf=pdf, paper=paper, sections=sections, sheet_ids=sheet_ids
    )["path"]


def export_package_report(
    session: Any,
    dest: Path | str,
    *,
    pdf: bool = True,
    paper: str = "mixed",
    sections: list[str] | None = None,
    sheet_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Write the package folder.

    ``sections=None`` writes every data file (the historical behaviour). A list
    limits the report CSVs and SVG/DXF drawings to those sections, and the same
    list drives print.pdf. ``sheet_ids`` limits the drawings. A print failure is
    never hidden: it lands in ``warnings`` (and manifest.json) and no
    substitute PDF is written.
    """

    import logging

    from panelcad.core.troubleshoot import write_troubleshoot
    from panelcad.render.print_package import normalize_sections, normalize_sheet_ids

    log = logging.getLogger("panelcad.export_package")
    enabled = set(normalize_sections(sections)) if sections is not None else None
    chosen = normalize_sheet_ids(session.project, sheet_ids)

    def want(name: str) -> bool:
        if enabled is None:
            return True
        return any(name in files for key, files in _SECTION_FILES.items() if key in enabled)

    root = Path(dest)
    if root.suffix:
        root = root.with_suffix("")
    if root.exists() and root.is_file():
        raise ValueError(f"{root} exists and is a file")
    root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    def wrote(*rels: str) -> None:
        written.extend(rels)

    save_project(session.project, root / PROJECT_YAML, backup=False)
    (root / PROJECT_JSON).write_text(
        json.dumps(session.project.model_dump(mode="json", by_alias=True), indent=2),
        encoding="utf-8",
    )
    _write_yaml(root / "title_block.yaml", session.project.title_block.model_dump(mode="json"))
    wrote(PROJECT_YAML, PROJECT_JSON, "title_block.yaml")
    from panelcad.io.bom import with_needs_info_section

    if want("bom.csv"):
        write_bom_csv(
            root / "bom.csv",
            with_needs_info_section(
                instance_rows(session.project, session.catalog), session.project, session.catalog
            ),
        )
        write_bom_csv(
            root / "bom_grouped.csv",
            with_needs_info_section(
                grouped_rows(session.project, session.catalog), session.project, session.catalog
            ),
        )
        wrote("bom.csv", "bom_grouped.csv")
    if want("wires.csv"):
        write_csv(root / "wires.csv", wire_rows(session.project))
        wrote("wires.csv")
    if want("cross_reference.csv"):
        write_csv(root / "cross_reference.csv", session.cross_reference_rows())
        wrote("cross_reference.csv")
    if want("terminals.csv"):
        write_csv(root / "terminals.csv", session.terminal_strip_rows())
        wrote("terminals.csv")
    if want("audit.csv"):
        write_csv(root / "audit.csv", session.audit_schematic_panel())
        write_csv(root / "checks.csv", session.design_checks(include_suppressed=True))
        wrote("audit.csv", "checks.csv")
    if want("io.csv"):
        write_csv(root / "io.csv", session.io_rows())
        wrote("io.csv")
    write_troubleshoot(session.project, session.catalog, root / "troubleshoot.json")
    wrote("troubleshoot.json")
    if want("cables.csv"):
        write_csv(root / "cables.csv", session.cable_rows())
        write_csv(root / "cable_pull.csv", pull_rows(session.project))
        write_csv(root / "cable_terminations.csv", termination_rows(session.project))
        wrote("cables.csv", "cable_pull.csv", "cable_terminations.csv")
    if want("panel_wires.csv"):
        write_csv(root / "panel_wires.csv", session.panel_wire_annotations())
        wrote("panel_wires.csv")
    sheets_dir = root / "sheets"
    draw = enabled is None or "drawings" in enabled
    keep = None if chosen is None else set(chosen)
    # Re-exporting into the same folder must not leave files from an earlier,
    # wider selection behind (they would also be listed in manifest.json).
    # Only files an earlier export itself wrote (its manifest's ``written``
    # list, content unchanged since) are pruned, and only plain files directly
    # inside root, root/svg or root/dxf.
    stale = _previous_package_files(root)
    if enabled is not None:
        for owned in {name for files in _SECTION_FILES.values() for name in files}:
            if not want(owned):
                _unlink_package_file(root, owned, stale)
    for sheet in session.project.sheets:
        if not (draw and (keep is None or sheet.id in keep)):
            _unlink_package_file(root, f"svg/{sheet.id}.svg", stale)
            _unlink_package_file(root, f"dxf/{sheet.id}.dxf", stale)
    if not pdf:
        _unlink_package_file(root, "print.pdf", stale)
    for sheet in session.project.sheets:
        _write_yaml(sheets_dir / f"{sheet.id}.yaml", sheet.model_dump(mode="json", by_alias=True))
        wrote(f"sheets/{sheet.id}.yaml")
        if draw and (keep is None or sheet.id in keep):
            session.export_svg(root / "svg" / f"{sheet.id}.svg", sheet_id=sheet.id)
            session.export_dxf(root / "dxf" / f"{sheet.id}.dxf", sheet_id=sheet.id)
            wrote(f"svg/{sheet.id}.svg", f"dxf/{sheet.id}.dxf")
    for lib, payload in used_library_files(session).items():
        _write_yaml(root / "library" / f"{lib}.yaml", payload)
        wrote(f"library/{lib}.yaml")
    warnings: list[str] = []
    print_result: dict[str, Any] | None = None
    pdf_path: Path | None = root / "print.pdf"
    if pdf:
        # Build into a temp file beside print.pdf and swap it in atomically, so
        # a failed build never damages an existing print.pdf.
        fd, tmp_name = tempfile.mkstemp(prefix=".print-", suffix=".tmp.pdf", dir=root)
        os.close(fd)
        tmp_pdf = Path(tmp_name)
        try:
            print_result = session.print_package(
                tmp_pdf, paper=paper, sections=sections, sheet_ids=chosen
            )
            if not tmp_pdf.is_file() or tmp_pdf.stat().st_size == 0:
                raise RuntimeError("the PDF builder produced no file")
            os.replace(tmp_pdf, pdf_path)
            if isinstance(print_result, dict) and "path" in print_result:
                print_result["path"] = str(pdf_path)
            wrote("print.pdf")
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            message = f"print.pdf was not written: {exc}"
            log.warning("export package %s: %s", root, message)
            warnings.append(message)
            # A print.pdf an earlier export wrote (unchanged since) is stale now;
            # anything else, e.g. a hand-placed print.pdf, is left alone.
            _unlink_package_file(root, "print.pdf", stale)
            pdf_path = None
        finally:
            try:
                tmp_pdf.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover - reported only
                log.warning("export package %s: could not remove %s: %s", root, tmp_pdf.name, exc)
    else:
        pdf_path = None
    log.info(
        "export package %s: requested sections=%s sheets=%s pages=%s",
        root,
        "all" if sections is None else list(sections),
        "all" if chosen is None else chosen,
        print_result["pages"] if print_result else "no pdf",
    )
    files = sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())
    manifest: dict[str, Any] = {
        "format": "panelcad-package",
        "version": "0.1",
        "name": session.project.name,
        "sheets": [s.id for s in session.project.sheets],
        "files": files,
        "written": sorted(set(written)),
        "written_sha256": {rel: _sha256(root / rel) for rel in sorted(set(written)) if (root / rel).is_file()},
    }
    if sections is not None:
        manifest["sections"] = sorted(enabled or [])
    if chosen is not None:
        manifest["drawing_sheets"] = chosen
    if warnings:
        manifest["warnings"] = warnings
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "path": root,
        "pdf": pdf_path,
        "print": print_result,
        "warnings": warnings,
        "sections": None if enabled is None else [k for k in print_sections_order() if k in enabled],
        "sheet_ids": chosen,
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _previous_package_files(root: Path) -> dict[str, str]:
    """Files an earlier export of ``root`` itself wrote, with their sha256.

    Only the manifest's ``written`` list counts (never ``files``, which lists
    everything in the folder). A manifest without ``written`` (older PanelCAD)
    or without a hash for a file yields nothing for it, so it is never pruned.
    """

    try:
        data = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != "panelcad-package":
        return {}
    written = data.get("written")
    hashes = data.get("written_sha256")
    if not isinstance(written, list) or not isinstance(hashes, dict):
        return {}
    return {
        f: hashes[f]
        for f in written
        if isinstance(f, str) and isinstance(hashes.get(f), str)
    }


def _unlink_package_file(root: Path, rel: str, listed: dict[str, str]) -> None:
    """Delete root/rel only if an earlier export wrote it (its manifest's
    ``written`` list), its content is unchanged since (sha256), and it is a plain
    file directly in root, root/svg or root/dxf (no '..', separators or links)."""

    if rel not in listed:
        return
    parts = rel.split("/")
    if len(parts) == 2:
        folder, name = parts
        if folder not in ("svg", "dxf"):
            return
    elif len(parts) == 1:
        folder, name = "", parts[0]
    else:
        return
    if not name or name in (".", "..") or "\\" in name or name.startswith("."):
        return
    if name.lower() in {PROJECT_YAML.lower(), PROJECT_JSON.lower(), MANIFEST_NAME.lower(),
                        "troubleshoot.json", "title_block.yaml"}:
        return
    parent = root / folder if folder else root
    if parent.is_symlink() or not parent.is_dir():
        return
    target = parent / name
    if target.is_symlink() or not target.is_file():
        return
    try:
        real_root = root.resolve()
        real_parent = parent.resolve()
        if target.resolve().parent != real_parent or not (
            real_parent == real_root or real_parent.parent == real_root
        ):
            return
        if _sha256(target) != listed[rel]:
            return  # replaced or edited since PanelCAD wrote it
    except OSError:
        return
    try:
        target.unlink()
    except OSError as exc:  # e.g. open in a PDF viewer on Windows
        import logging

        logging.getLogger("panelcad.export_package").warning(
            "export package %s: %s skipped: file in use (%s)", root, rel, exc
        )


def print_sections_order() -> tuple[str, ...]:
    from panelcad.render.print_package import SECTIONS

    return SECTIONS


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
        touch = getattr(session, "_touch_drawing", None)
        if callable(touch):
            touch()
    else:
        for sheet in project.sheets:
            if any(s.id == sheet.id for s in session.project.sheets):
                continue
            session.project.sheets.append(sheet)
        tb = project.title_block
        if not session.project.title_block.company and tb.company:
            session.project.title_block = tb
        touch = getattr(session, "_touch_drawing", None)
        if callable(touch):
            touch()


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
