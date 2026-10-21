from __future__ import annotations

import errno
from pathlib import Path

import yaml

from panelcad.core.models import Project
from panelcad.io.share import atomic_write_text


def describe_open_error(exc: BaseException, path: Path | str) -> str:
    """Plain-language Open failure. Parser and validation text stay in the log."""

    from panelcad.io.winpath import display_name

    name = display_name(path) or Path(path).name
    missing = isinstance(exc, FileNotFoundError) or (
        isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOENT
    )
    if missing:
        return f"Can't find {name}."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in {
        errno.EACCES,
        errno.EPERM,
        errno.EROFS,
    }:
        return f"Can't open {name}: you don't have permission to read it."
    line = _error_line(exc)
    where = f" (line {line})" if line else ""
    return (
        f"This file isn't a PanelCAD project or it's damaged{where}. "
        "Open an earlier copy, or Help → Report a bug."
    )


def _error_line(exc: BaseException) -> int | None:
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return None
    line = getattr(mark, "line", None)
    if isinstance(line, int) and line >= 0:
        return line + 1
    return None


def load_project(path: Path | str) -> Project:
    from panelcad.io.winpath import io_path

    raw = Path(io_path(path)).read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    return Project.model_validate(data)


def _default_wire_numbering(raw: dict | None) -> bool:
    if not raw:
        return True
    return (
        raw.get("scheme", "sheet_line") == "sheet_line"
        and raw.get("enabled", True) is True
        and int(raw.get("start") or 1) == 1
        and raw.get("split_feedthrough", False) in {False, None}
    )


def _default_phase_colors(raw: dict | None) -> bool:
    if not raw:
        return True
    from panelcad.core.models import PhaseColors

    try:
        got = PhaseColors.model_validate(raw)
    except (TypeError, ValueError):
        return False
    return got == PhaseColors()


def _default_cross_reference(raw: dict | None) -> bool:
    if not raw:
        return True
    try:
        pitch = float(raw.get("rung_pitch_mm", 12.7))
    except (TypeError, ValueError):
        pitch = 12.7
    return (
        raw.get("enabled", True) is True
        and raw.get("nc_mark", "underline") == "underline"
        and abs(pitch - 12.7) < 0.05
        and raw.get("rung_numbers", "used") == "used"
    )


def _prune_revision_control(data: dict) -> None:
    """Drop an unused revision store so older files stay byte-stable."""

    control = data.get("revision_control")
    if not isinstance(control, dict):
        data.pop("revision_control", None)
        return
    if not control.get("issued"):
        control.pop("issued", None)
    clouds = control.get("clouds")
    if isinstance(clouds, list):
        for cloud in clouds:
            if isinstance(cloud, dict) and not str(cloud.get("note") or "").strip():
                cloud.pop("note", None)
    if not control.get("clouds"):
        control.pop("clouds", None)
    if control.get("scheme", "letter") == "letter":
        control.pop("scheme", None)
    if control.get("clear_clouds_on_issue", True) is True:
        control.pop("clear_clouds_on_issue", None)
    if not control:
        data.pop("revision_control", None)


def _prune_reconstruct(data: dict) -> None:
    """Drop an unused as-built photo store so older files stay byte-stable."""

    store = data.get("reconstruct")
    if not isinstance(store, dict):
        data.pop("reconstruct", None)
        return
    if not store.get("photos") and not store.get("items") and not store.get("prints") and not store.get("checklist") and not store.get("mismatches"):
        data.pop("reconstruct", None)
        return
    for key in ("panel_sheet", "door_sheet", "schematic_sheet"):
        if not store.get(key):
            store.pop(key, None)
    if not store.get("mismatches"):
        store.pop("mismatches", None)
    if not store.get("checklist"):
        store.pop("checklist", None)
    if not store.get("prints"):
        store.pop("prints", None)


def _clean_cable(raw: dict) -> dict:
    """Drop empty cable fields so a project with no extras stays small."""

    item = dict(raw)
    for key in (
        "from_strip",
        "from_device",
        "to_device",
        "to_kind",
        "cable_type",
        "gauge",
        "shield_bond",
        "shield_source",
        "shield_note",
        "gland",
        "od",
        "od_source",
        "route",
        "part_number",
        "part_source",
    ):
        if not item.get(key):
            item.pop(key, None)
    if not item.get("from_terminals"):
        item.pop("from_terminals", None)
    if not item.get("to_terminals"):
        item.pop("to_terminals", None)
    if item.get("kind", "field") in {"", "field"}:
        item.pop("kind", None)
    for key in ("from_panel", "to_panel", "to_strip"):
        if not item.get(key):
            item.pop(key, None)
    if not item.get("conductors"):
        item.pop("conductors", None)
    if not item.get("pairs"):
        item.pop("pairs", None)
    if not item.get("shield"):
        item.pop("shield", None)
    if not item.get("drain"):
        item.pop("drain", None)
    if item.get("length") is None:
        item.pop("length", None)
        if item.get("length_unit", "ft") == "ft":
            item.pop("length_unit", None)
    elif item.get("length_unit", "ft") == "ft":
        item.pop("length_unit", None)
    if item.get("slack_pct") is None:
        item.pop("slack_pct", None)
    if item.get("show_tag", True) is True:
        item.pop("show_tag", None)
    lands = []
    for land in item.get("assignments") or []:
        kept = {"id": land.get("id", "")}
        for key, value in land.items():
            if key == "id":
                continue
            if value in ("", False, None):
                continue
            kept[key] = value
        lands.append(kept)
    if lands:
        item["assignments"] = lands
    else:
        item.pop("assignments", None)
    return item


def dump_project(project: Project) -> str:
    data = project.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not data.get("ref_prefixes"):
        data.pop("ref_prefixes", None)
    if _default_wire_numbering(data.get("wire_numbering")):
        data.pop("wire_numbering", None)
    if _default_cross_reference(data.get("cross_reference")):
        data.pop("cross_reference", None)
    _prune_revision_control(data)
    _prune_reconstruct(data)
    cables = data.get("cables") or []
    if not cables:
        data.pop("cables", None)
    else:
        data["cables"] = [_clean_cable(item) for item in cables]
    if _default_phase_colors(data.get("phase_colors")):
        data.pop("phase_colors", None)
    _prune_panels(data)
    for sheet in data.get("sheets", []):
        if not sheet.get("rails"):
            sheet.pop("rails", None)
        if not sheet.get("trays"):
            sheet.pop("trays", None)
        if not sheet.get("enclosure"):
            sheet.pop("enclosure", None)
        else:
            enc = sheet["enclosure"]
            for key in (
                "nema_type",
                "material",
                "catalog",
                "manufacturer",
                "mount",
                "panel_width_mm",
                "panel_height_mm",
            ):
                if not enc.get(key):
                    enc.pop(key, None)
            if not enc.get("sized"):
                enc.pop("sized", None)
        if not sheet.get("network"):
            sheet.pop("network", None)
        if not sheet.get("closed"):
            sheet.pop("closed", None)
        if not sheet.get("drawing_style"):
            sheet.pop("drawing_style", None)
        if not sheet.get("standard"):
            sheet.pop("standard", None)
        if not sheet.get("rail_hot"):
            sheet.pop("rail_hot", None)
        if not sheet.get("rail_return"):
            sheet.pop("rail_return", None)
        if not sheet.get("strip"):
            sheet.pop("strip", None)
        if int(sheet.get("strip_part") or 1) == 1:
            sheet.pop("strip_part", None)
        if not sheet.get("io_cont"):
            sheet.pop("io_cont", None)
        if not sheet.get("panel"):
            sheet.pop("panel", None)
        if sheet.get("kind") not in ("schematic", "network") and not sheet.get("wires"):
            sheet.pop("wires", None)
        for wire in sheet.get("wires") or []:
            for key in ("label", "number", "color", "gauge", "cable", "type"):
                if not wire.get(key):
                    wire.pop(key, None)
            if not wire.get("number_locked"):
                wire.pop("number_locked", None)
            if not wire.get("inferred"):
                wire.pop("inferred", None)
        for comp in sheet.get("components", []):
            if abs(float(comp.get("scale", 1.0)) - 1.0) < 1e-6:
                comp.pop("scale", None)
            if not comp.get("ports"):
                comp.pop("ports", None)
            if comp.get("pins_locked", True):
                comp.pop("pins_locked", None)
            if not comp.get("pin_overrides"):
                comp.pop("pin_overrides", None)
            if not comp.get("picture"):
                comp.pop("picture", None)
            if not comp.get("panel"):
                comp.pop("panel", None)
    specs = data.get("net_wire_specs") or {}
    cleaned: dict = {}
    if isinstance(specs, dict):
        for name, spec in specs.items():
            if not isinstance(spec, dict):
                continue
            item = {
                key: str(spec.get(key) or "").strip()
                for key in ("size", "color", "type")
                if str(spec.get(key) or "").strip()
            }
            if item:
                cleaned[str(name)] = item
    if cleaned:
        data["net_wire_specs"] = cleaned
    else:
        data.pop("net_wire_specs", None)
    _prune_share(data)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def omit_default_panel_fields(data: dict) -> None:
    """Drop empty multi-panel fields from a dumped project, including a revision snapshot."""

    _prune_panels(data)
    for sheet in data.get("sheets") or []:
        if isinstance(sheet, dict) and not sheet.get("panel"):
            sheet.pop("panel", None)
        for comp in (sheet.get("components") or []) if isinstance(sheet, dict) else []:
            if isinstance(comp, dict) and not comp.get("panel"):
                comp.pop("panel", None)
    cables = data.get("cables") or []
    if isinstance(cables, list):
        for cable in cables:
            if not isinstance(cable, dict):
                continue
            if cable.get("kind", "field") in {"", "field"}:
                cable.pop("kind", None)
            for key in ("from_panel", "to_panel", "to_strip"):
                if not cable.get(key):
                    cable.pop(key, None)
            if not cable.get("to_terminals"):
                cable.pop("to_terminals", None)


def _prune_panels(data: dict) -> None:
    """Drop an unused panel list so a single-panel file stays byte-stable."""

    if not str(data.get("active_panel") or "").strip():
        data.pop("active_panel", None)
    if not str(data.get("location_system") or "").strip():
        data.pop("location_system", None)
    panels = data.get("panels")
    if not isinstance(panels, list) or not panels:
        data.pop("panels", None)
        return
    cleaned = []
    for raw in panels:
        if not isinstance(raw, dict):
            continue
        item = {"id": str(raw.get("id") or "").strip()}
        if not item["id"]:
            continue
        for key in ("tag", "name", "enclosure_id", "system"):
            text = str(raw.get(key) or "").strip()
            if text:
                item[key] = text
        cleaned.append(item)
    if cleaned:
        data["panels"] = cleaned
    else:
        data.pop("panels", None)


def _prune_share(data: dict) -> None:
    """Drop an unused shared-folder log so older files stay byte-stable."""

    last = data.get("last_saved")
    if not isinstance(last, dict) or not any(
        str(last.get(key) or "").strip() for key in ("user", "host", "time")
    ):
        data.pop("last_saved", None)
    else:
        kept = {"user", "host", "time", "summary"}
        for key in list(last):
            if key not in kept or not str(last.get(key) or "").strip():
                last.pop(key, None)
    rows = data.get("activity")
    if not isinstance(rows, list) or not rows:
        data.pop("activity", None)
        return
    cleaned = []
    for row in rows[-100:]:
        if not isinstance(row, dict):
            continue
        item = {
            key: str(row.get(key) or "").strip()
            for key in ("user", "host", "time", "summary")
            if str(row.get(key) or "").strip()
        }
        if item.get("time") or item.get("summary"):
            cleaned.append(item)
    if cleaned:
        data["activity"] = cleaned
    else:
        data.pop("activity", None)


def save_project(project: Project, path: Path | str, *, backup: bool = True) -> None:
    from panelcad.io.winpath import io_path

    atomic_write_text(io_path(path), dump_project(project), backup=backup)
