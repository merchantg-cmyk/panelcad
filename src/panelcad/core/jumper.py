"""Terminal jumpers as catalog parts.

``properties.jumper`` on the terminal stays the project source of truth, so
old files that only stored that property still load. Catalog devices
(``connection.jumper``, or a ``jumpers:`` table in a library YAML) supply
manufacturer part numbers. The BOM, parts report, vendor strip CSV, and
strip drawing derive the part from family, pitch, and how many terminals
the jumper spans.
"""

from __future__ import annotations

from typing import Any

from panelcad.core.electrical import LAYOUT_KINDS, _device, terminal_identity
from panelcad.core.models import Component, Device, Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.library.panel import effective_footprint, is_incabinet

_FAMILY_ALIASES = {
    "phoenix": "phoenix",
    "phoenixcontact": "phoenix",
    "ut": "phoenix",
    "st": "phoenix",
    "clipline": "phoenix",
    "fbs": "phoenix",
    "wago": "wago",
    "topjob": "wago",
    "2002": "wago",
    "weidmueller": "weidmueller",
    "weidmuller": "weidmueller",
    "wdu": "weidmueller",
    "zqv": "weidmueller",
    "dinkle": "dinkle",
    "automationdirect": "dinkle",
    "adc": "dinkle",
    "dnt": "dinkle",
    "dnt10": "dinkle",
    "generic": "generic",
    "iec": "generic",
}

_PITCH_TOL = 0.16


def is_jumper_device(device: Device | None) -> bool:
    if device is None:
        return False
    if device.category == "connection.jumper":
        return True
    accessory = str(device.properties.get("accessory") or "").strip().lower()
    if accessory == "jumper":
        return True
    return any(str(tag).lower() == "jumper" for tag in device.tags)


def normalize_family(value: str) -> str:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
    if token in _FAMILY_ALIASES:
        return _FAMILY_ALIASES[token]
    for key, family in _FAMILY_ALIASES.items():
        if key and key in token:
            return family
    return token or "generic"


def jumper_devices(catalog: Catalog) -> list[Device]:
    return [device for device in catalog.devices.values() if is_jumper_device(device)]


def _pitch_of(device: Device) -> float:
    raw = device.properties.get("pitch_mm")
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    width = float(device.panel.width_mm or 0.0)
    return width if width > 0 else 5.2


def _poles_of(device: Device) -> int:
    raw = device.properties.get("poles")
    try:
        poles = int(raw)
    except (TypeError, ValueError):
        poles = 0
    return poles if poles >= 2 else 2


def _cuttable(device: Device) -> bool:
    raw = device.properties.get("cut_to_length")
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in {"1", "true", "yes", "y", "cut"}


def _family_of(device: Device) -> str:
    return normalize_family(str(device.properties.get("jumper_family") or device.properties.get("family") or ""))


def infer_family(library_id: str, manufacturer: str = "") -> str:
    blob = f"{library_id} {manufacturer}"
    return normalize_family(blob) if any(key and key in "".join(ch.lower() for ch in blob if ch.isalnum()) for key in _FAMILY_ALIASES) else "generic"


def terminal_match_key(catalog: Catalog, comp: Component) -> tuple[str, float, str]:
    device = _device(catalog, comp)
    props = dict(comp.properties)
    if device is not None:
        props = {**device.properties, **comp.properties}
    manufacturer = str(props.get("manufacturer") or "")
    family = str(props.get("jumper_family") or props.get("family") or "").strip()
    if not family:
        family = infer_family(comp.library_id, manufacturer)
    pitch_raw = props.get("pitch_mm")
    pitch = 0.0
    if pitch_raw not in (None, ""):
        try:
            pitch = float(pitch_raw)
        except (TypeError, ValueError):
            pitch = 0.0
    if pitch <= 0 and device is not None:
        pitch = float(device.panel.width_mm or 0.0)
    if pitch <= 0:
        pitch = 5.2
    return (normalize_family(family), pitch, manufacturer)


def strip_terminals(project: Project, catalog: Catalog, strip: str | None = None) -> list[tuple[Sheet, Component]]:
    """Schematic terminals in reading order. Same rule as the strip drawing."""

    found: list[tuple[int, float, float, str, Sheet, Component]] = []
    want = str(strip or "").strip()
    for index, sheet in enumerate(project.sheets):
        if sheet.kind in LAYOUT_KINDS:
            continue
        for comp in sheet.components:
            if str(comp.properties.get("accessory") or "").strip():
                continue
            ident = terminal_identity(comp, _device(catalog, comp))
            if ident is None:
                continue
            if want and ident[0] != want:
                continue
            found.append((index, float(comp.y), float(comp.x), comp.id, sheet, comp))
    found.sort(key=lambda row: (row[0], row[1], row[2], row[3]))
    return [(sheet, comp) for _index, _y, _x, _ref, sheet, comp in found]


def _strip_names(project: Project, catalog: Catalog) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for _sheet, comp in strip_terminals(project, catalog):
        ident = terminal_identity(comp, _device(catalog, comp))
        if ident is None or ident[0] in seen:
            continue
        seen.add(ident[0])
        names.append(ident[0])
    return names


def _score(device: Device, family: str, manufacturer: str) -> int:
    score = 0
    device_family = _family_of(device)
    if device_family == family:
        score += 100
    elif family == "generic" or device_family == "generic":
        score += 20
    mfr = str(device.properties.get("manufacturer") or "")
    if manufacturer and mfr and (
        manufacturer.lower() in mfr.lower() or mfr.lower() in manufacturer.lower()
    ):
        score += 40
    return score


def _line(device: Device, *, qty: int, poles: int, notes: str = "") -> dict[str, Any]:
    props = device.properties
    return {
        "library_id": device.library_id,
        "description": device.name,
        "manufacturer": str(props.get("manufacturer") or ""),
        "part_number": str(props.get("part_number") or ""),
        "catalog": str(props.get("catalog") or props.get("part_number") or ""),
        "category": device.category,
        "qty": int(qty),
        "poles": int(poles),
        "jumper_kind": str(props.get("jumper_kind") or "bridge"),
        "cut_to_length": _cuttable(device),
        "notes": notes,
        "device": device,
    }


def _unmatched(poles: int, family: str, pitch: float) -> dict[str, Any]:
    label = f"{poles}-pole jumper"
    return {
        "library_id": "",
        "description": label,
        "manufacturer": "",
        "part_number": label,
        "catalog": "",
        "category": "connection.jumper",
        "qty": 1,
        "poles": int(poles),
        "jumper_kind": "bridge",
        "cut_to_length": False,
        "notes": f"no catalog match for family={family} pitch={pitch:g} mm",
        "device": None,
    }


def pick_jumper(catalog: Catalog, comps: list[Component]) -> dict[str, Any]:
    """Choose a catalog jumper for one consecutive run."""

    if len(comps) < 2:
        raise ValueError("a jumper needs at least two terminals")
    family, pitch, manufacturer = terminal_match_key(catalog, comps[0])
    poles = len(comps)
    candidates = [device for device in jumper_devices(catalog) if abs(_pitch_of(device) - pitch) <= _PITCH_TOL]
    same_family = [device for device in candidates if _family_of(device) == family]
    if same_family:
        pool = same_family
    elif family == "generic":
        pool = candidates
    else:
        pool = [device for device in candidates if _family_of(device) == "generic"] or candidates
    if not pool:
        return _unmatched(poles, family, pitch)

    def best(group: list[Device]) -> Device:
        return max(group, key=lambda device: (_score(device, family, manufacturer), -abs(_poles_of(device) - poles)))

    exact = [device for device in pool if _poles_of(device) == poles]
    if exact:
        return _line(best(exact), qty=1, poles=poles)

    cuttable = [device for device in pool if _cuttable(device) and _poles_of(device) >= poles]
    if cuttable:
        cuttable.sort(key=lambda device: (_poles_of(device), -_score(device, family, manufacturer)))
        chosen = cuttable[0]
        return _line(chosen, qty=1, poles=poles, notes=f"cut to {poles} poles")

    two_pole = [device for device in pool if _poles_of(device) == 2]
    if two_pole:
        return _line(
            best(two_pole),
            qty=poles - 1,
            poles=poles,
            notes=f"{poles - 1} adjacent 2-pole bridges",
        )

    usable = [device for device in pool if _poles_of(device) <= poles]
    if usable:
        chosen = max(usable, key=lambda device: (_poles_of(device), _score(device, family, manufacturer)))
        taken = _poles_of(chosen)
        leftover = poles - taken + 1
        notes = f"{taken}-pole plus remainder"
        if leftover >= 2:
            rest = pick_jumper(catalog, comps[:leftover]) if leftover < poles else _line(chosen, qty=1, poles=poles)
            lines = [_line(chosen, qty=1, poles=taken), rest]
            return {**_line(chosen, qty=1, poles=poles, notes=notes), "lines": lines}
        return _line(chosen, qty=1, poles=poles, notes=notes)

    return _line(best(pool), qty=1, poles=poles)


def jumper_runs(project: Project, catalog: Catalog, strip: str | None = None) -> list[dict[str, Any]]:
    """Consecutive terminals that share ``properties.jumper``. One run → one part."""

    names = [str(strip).strip()] if str(strip or "").strip() else _strip_names(project, catalog)
    runs: list[dict[str, Any]] = []
    for name in names:
        if not name:
            continue
        terminals = strip_terminals(project, catalog, name)
        run_comps: list[Component] = []
        run_id = ""
        run_sheet = ""

        def flush() -> None:
            nonlocal run_comps, run_id, run_sheet
            if len(run_comps) >= 2 and run_id:
                picked = pick_jumper(catalog, run_comps)
                lines = list(picked.get("lines") or [picked])
                runs.append(
                    {
                        "id": run_id,
                        "strip": name,
                        "sheet": run_sheet,
                        "refs": [comp.id for comp in run_comps],
                        "poles": len(run_comps),
                        **{key: picked[key] for key in picked if key != "device"},
                        "lines": [{key: line[key] for key in line if key != "device"} for line in lines],
                    }
                )
            run_comps = []
            run_id = ""
            run_sheet = ""

        for sheet, comp in terminals:
            jid = str(comp.properties.get("jumper") or "").strip()
            if jid and jid == run_id:
                run_comps.append(comp)
                continue
            flush()
            if jid:
                run_comps = [comp]
                run_id = jid
                run_sheet = sheet.id
        flush()
    return runs


def jumper_bom_rows(project: Project, catalog: Catalog, strip: str | None = None) -> list[dict[str, str]]:
    """Instance-style BOM rows derived from jumper properties."""

    rows: list[dict[str, str]] = []
    for run in jumper_runs(project, catalog, strip=strip):
        span = "–".join((run["refs"][0], run["refs"][-1])) if run["refs"] else run["id"]
        for line in run.get("lines") or [run]:
            device = None
            library_id = str(line.get("library_id") or "")
            if library_id:
                try:
                    device = catalog.get(library_id)
                except KeyError:
                    device = None
            fp = effective_footprint(device, Component(id=run["id"], library_id=library_id, x=0, y=0)) if device else None
            notes = str(line.get("notes") or "")
            detail = f"{run['id']} on {run['strip']} {span}"
            if notes:
                detail = f"{detail}; {notes}"
            rows.append(
                {
                    "item": "",
                    "qty": str(int(line.get("qty") or 1)),
                    "sheet": str(run.get("sheet") or ""),
                    "ref": str(run["id"]),
                    "library_id": library_id,
                    "description": str(line.get("description") or ""),
                    "manufacturer": str(line.get("manufacturer") or ""),
                    "part_number": str(line.get("part_number") or ""),
                    "catalog": str(line.get("catalog") or ""),
                    "category": str(line.get("category") or "connection.jumper"),
                    "width_mm": f"{fp.width_mm:g}" if fp else "",
                    "height_mm": f"{fp.height_mm:g}" if fp else "",
                    "depth_mm": f"{fp.depth_mm:g}" if fp else "",
                    "din_rail": "no",
                    "incabinet": "yes" if device is None or is_incabinet(device, Component(id=run["id"], library_id=library_id, x=0, y=0)) else "no",
                    "in_panel": "yes",
                    "mount": "none",
                    "dim_source": fp.dim_source if fp else "",
                    "notes": detail,
                    "x": "",
                    "y": "",
                }
            )
    return rows


def attach_jumper_catalog(
    catalog: Catalog,
    jumpers: list[dict[str, Any]],
    items: list[dict[str, Any]],
    project: Project | None = None,
) -> None:
    """Fill drawn jumper bars with the catalog part for their spanned terminals."""

    live_by_id: dict[str, Component] = {}
    if project is not None:
        for _sheet, comp in strip_terminals(project, catalog):
            live_by_id[comp.id] = comp
    by_ref = {str(item.get("ref") or ""): item for item in items}
    for jumper in jumpers:
        refs = [str(ref) for ref in (jumper.get("refs") or []) if str(ref)]
        if not refs:
            continue
        live: list[Component] = []
        for ref in refs:
            if ref in live_by_id:
                live.append(live_by_id[ref])
                continue
            item = by_ref.get(ref) or {}
            live.append(Component(id=ref, library_id=str(item.get("library_id") or ""), x=0.0, y=0.0))
        try:
            picked = pick_jumper(catalog, live)
        except (ValueError, KeyError):
            continue
        jumper["library_id"] = str(picked.get("library_id") or "")
        jumper["part_number"] = str(picked.get("part_number") or "")
        jumper["catalog"] = str(picked.get("catalog") or "")
        jumper["manufacturer"] = str(picked.get("manufacturer") or "")
        jumper["description"] = str(picked.get("description") or "")
        jumper["qty"] = int(picked.get("qty") or 1)
        jumper["poles"] = int(picked.get("poles") or len(refs))
        jumper["notes"] = str(picked.get("notes") or "")
        jumper["label"] = _bar_label(jumper)
    layout_jumper_labels(jumpers, items)


# Same as the strip renderer: dots sit min(8, 0.18 h) down the block.
_DOT_FROM_TOP = 8.0
_DOT_FROM_TOP_FRAC = 0.18
_DOT_RADIUS = 1.05
_DOT_RADIUS_FRAC = 0.16
_LABEL_CHAR = 0.52
_LABEL_GAP = 2.4
_LABEL_SIZE = 2.0


def _short_part(jumper: dict[str, Any]) -> str:
    """Catalog / FBS-style id that fits on a 5.2 mm bar. Skip long descriptions."""

    catalog = str(jumper.get("catalog") or "").strip()
    part = str(jumper.get("part_number") or "").strip()
    for candidate in (part, catalog):
        if not candidate:
            continue
        if candidate.lower().startswith("jumper "):
            continue
        if len(candidate) <= 14:
            return candidate
    return ""


def _bar_label(jumper: dict[str, Any]) -> str:
    ident = str(jumper.get("id") or "")
    short = _short_part(jumper)
    if ident and short and int(jumper.get("poles") or 0) >= 3:
        return f"{ident} {short}"
    return ident


def _text_width(text: str, size: float) -> float:
    return max(0.0, len(text) * _LABEL_CHAR * float(size))


def _label_box(jumper: dict[str, Any]) -> tuple[float, float, float, float]:
    size = float(jumper.get("label_size") or _LABEL_SIZE)
    width = _text_width(str(jumper.get("label") or ""), size)
    cx = float(jumper.get("label_x") or 0.0)
    bottom = float(jumper.get("label_y") or 0.0)
    return (cx - width / 2.0, cx + width / 2.0, bottom - size, bottom)


def _boxes_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float], pad: float = 0.8) -> bool:
    return a[0] < b[1] + pad and b[0] < a[1] + pad and a[2] < b[3] + pad and b[2] < a[3] + pad


def _dot_geometry(items_by_ref: dict[str, dict[str, Any]], refs: list[str]) -> tuple[float, float, float] | None:
    blocks = [items_by_ref[ref] for ref in refs if ref in items_by_ref]
    if not blocks:
        return None
    top = min(float(block["y"]) for block in blocks)
    height = min(float(block["h"]) for block in blocks)
    width = min(float(block["w"]) for block in blocks)
    radius = min(_DOT_RADIUS, width * _DOT_RADIUS_FRAC)
    dot_y = top + min(_DOT_FROM_TOP, height * _DOT_FROM_TOP_FRAC)
    return (top, radius, dot_y)


def _label_candidates(jumper: dict[str, Any]) -> list[str]:
    ident = str(jumper.get("id") or "")
    short = _short_part(jumper)
    poles = int(jumper.get("poles") or 0)
    texts: list[str] = []
    if ident and short and poles >= 3:
        texts.append(f"{ident} {short}")
        head = short.split()[0]
        if head and head != short and f"{ident} {head}" not in texts:
            texts.append(f"{ident} {head}")
    if ident:
        texts.append(ident)
    return texts


def layout_jumper_labels(jumpers: list[dict[str, Any]], items: list[dict[str, Any]]) -> None:
    """Place labels above the connection dots, abbreviated on a narrow span."""

    by_ref = {str(item.get("ref") or ""): item for item in items}
    for jumper in jumpers:
        refs = [str(ref) for ref in (jumper.get("refs") or []) if str(ref)]
        x1 = float(jumper.get("x1") or 0.0)
        x2 = float(jumper.get("x2") or 0.0)
        span = abs(x2 - x1)
        size = _LABEL_SIZE
        label = str(jumper.get("id") or "")
        for text in _label_candidates(jumper):
            if _text_width(text, size) <= max(2.4, span - 2.0):
                label = text
                break
        else:
            size = 1.5
            label = str(jumper.get("id") or "")
            if _text_width(label, size) > max(2.0, span - 0.8):
                size = 1.35
        geo = _dot_geometry(by_ref, refs)
        if geo is not None:
            _top, radius, dot_y = geo
            label_y = dot_y - radius - _LABEL_GAP
        else:
            label_y = float(jumper.get("y") or 0.0) - 4.0
        jumper["label"] = label
        jumper["label_x"] = (x1 + x2) / 2.0
        jumper["label_y"] = label_y
        jumper["label_size"] = size

    by_row: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for jumper in jumpers:
        by_row.setdefault((int(jumper.get("part") or 1), int(jumper.get("row") or 0)), []).append(jumper)
    for group in by_row.values():
        ordered = sorted(group, key=lambda item: float(item.get("label_x") or 0.0))
        for left, right in zip(ordered, ordered[1:], strict=False):
            if not _boxes_overlap(_label_box(left), _label_box(right)):
                continue
            left["label"] = str(left.get("id") or left.get("label") or "")
            right["label"] = str(right.get("id") or right.get("label") or "")
            if _boxes_overlap(_label_box(left), _label_box(right)):
                right["label_y"] = float(right["label_y"]) - float(right.get("label_size") or _LABEL_SIZE) - 0.8


def _next_jumper_id(project: Project) -> str:
    used = {
        str(comp.properties.get("jumper") or "")
        for sheet in project.sheets
        for comp in sheet.components
    }
    n = 1
    while f"J{n}" in used:
        n += 1
    return f"J{n}"


def _all_by_id(project: Project) -> dict[str, list[Component]]:
    found: dict[str, list[Component]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            found.setdefault(comp.id, []).append(comp)
    return found


def _set_jumper(comps: list[Component], label: str) -> None:
    for comp in comps:
        if label:
            comp.properties["jumper"] = label
        else:
            comp.properties.pop("jumper", None)


def _drop_orphans(project: Project, catalog: Catalog) -> list[str]:
    counts: dict[str, int] = {}
    for _sheet, comp in strip_terminals(project, catalog):
        label = str(comp.properties.get("jumper") or "").strip()
        if label:
            counts[label] = counts.get(label, 0) + 1
    dropped: list[str] = []
    for label, count in counts.items():
        if count >= 2:
            continue
        dropped.append(label)
        for sheet in project.sheets:
            for comp in sheet.components:
                if str(comp.properties.get("jumper") or "") == label:
                    comp.properties.pop("jumper", None)
    return dropped


def apply_jumper(
    project: Project,
    catalog: Catalog | None,
    refs: list[str],
    group: str | None = None,
) -> dict[str, Any]:
    """Mark a consecutive span as jumpered. ``refs`` may be the ends of the span."""

    want = [str(ref).strip() for ref in refs if str(ref).strip()]
    if len(want) < 2:
        raise ValueError("a jumper needs at least two terminals")
    if catalog is None:
        found: list[tuple[Sheet, Component]] = []
        for sheet in project.sheets:
            for comp in sheet.components:
                if comp.id in want:
                    found.append((sheet, comp))
        missing = [ref for ref in want if ref not in {comp.id for _s, comp in found}]
        if missing:
            raise KeyError(f"no terminal {', '.join(missing)}")
        label = (group or "").strip() or _next_jumper_id(project)
        for _sheet, comp in found:
            comp.properties["jumper"] = label
        return {"jumper": label, "refs": [comp.id for _s, comp in found], "parts": []}

    by_id = {comp.id: (sheet, comp) for sheet, comp in strip_terminals(project, catalog)}
    missing = [ref for ref in want if ref not in by_id]
    if missing:
        raise KeyError(f"no terminal {', '.join(missing)}")
    strips = set()
    for ref in want:
        ident = terminal_identity(by_id[ref][1], _device(catalog, by_id[ref][1]))
        if ident is None:
            raise ValueError(f"{ref} is not a terminal")
        strips.add(ident[0])
    if len(strips) != 1:
        raise ValueError("a jumper must stay on one strip")
    name = next(iter(strips))
    ordered = [comp.id for _sheet, comp in strip_terminals(project, catalog, name)]
    indexes = [ordered.index(ref) for ref in want]
    lo, hi = min(indexes), max(indexes)
    span = ordered[lo : hi + 1]
    if len(span) < 2:
        raise ValueError("a jumper needs at least two terminals")

    existing = {
        str(by_id[ref][1].properties.get("jumper") or "").strip()
        for ref in want
        if str(by_id[ref][1].properties.get("jumper") or "").strip()
    }
    label = (group or "").strip()
    if not label:
        label = existing.pop() if len(existing) == 1 else _next_jumper_id(project)

    copies = _all_by_id(project)
    for sheet in project.sheets:
        for comp in sheet.components:
            if str(comp.properties.get("jumper") or "") == label and comp.id not in set(span):
                ident = terminal_identity(comp, _device(catalog, comp))
                if ident is None or ident[0] == name:
                    comp.properties.pop("jumper", None)
    for ref in span:
        _set_jumper(copies.get(ref, []), label)
    _drop_orphans(project, catalog)
    parts = [run for run in jumper_runs(project, catalog, name) if run["id"] == label]
    return {"jumper": label, "refs": span, "parts": parts, "strip": name}


def clear_jumpers(
    project: Project,
    catalog: Catalog | None,
    refs: list[str] | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """Remove jumper marks. Clears a named group, or the selected terminals."""

    label = (group or "").strip()
    want = [str(ref).strip() for ref in (refs or []) if str(ref).strip()]
    if not label and not want:
        raise ValueError("clear a jumper group or at least one terminal")
    cleared: list[str] = []
    groups: set[str] = set()
    if label:
        for sheet in project.sheets:
            for comp in sheet.components:
                if str(comp.properties.get("jumper") or "") == label:
                    comp.properties.pop("jumper", None)
                    if comp.id not in cleared:
                        cleared.append(comp.id)
        groups.add(label)
    else:
        copies = _all_by_id(project)
        for ref in want:
            for comp in copies.get(ref, []):
                old = str(comp.properties.get("jumper") or "").strip()
                if old:
                    groups.add(old)
                comp.properties.pop("jumper", None)
            if ref not in cleared:
                cleared.append(ref)
    if catalog is not None:
        groups.update(_drop_orphans(project, catalog))
    return {"cleared": cleared, "groups": sorted(groups)}
