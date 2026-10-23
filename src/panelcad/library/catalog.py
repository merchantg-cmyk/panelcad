from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from panelcad.core.block import parse_attributes
from panelcad.core.models import Device, PinDef
from panelcad.core.primitives import Primitive, expand_draw_item, land_leads_on_circle
from panelcad.library import yaml_load as _lib_yaml
from panelcad.library.panel import resolve_panel
from panelcad.library.paths import library_search_paths
from panelcad.library.ports import parse_port


class Catalog:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self._loaded_files: list[Path] = []
        self._device_files: dict[str, Path] = {}
        self.ref_prefix_maps: dict[str, dict[str, str]] = {}
        self.io_modules: dict[str, Any] = {}

    def load_defaults(self, project_dir: Path | None = None) -> None:
        from panelcad.core.refs import load_ref_prefix_maps

        for directory in library_search_paths(project_dir):
            self.load_dir(directory)
        self.ref_prefix_maps = load_ref_prefix_maps(project_dir)

    def load_dir(self, directory: Path) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.yaml")):
            self.load_file(path)
        for path in sorted(directory.glob("*.yml")):
            self.load_file(path)
        from panelcad.library.io_modules import finalize_io_modules

        finalize_io_modules(self)

    def load_file(self, path: Path) -> None:
        data = _lib_yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        from panelcad.core.refs import is_prefix_library_file

        if is_prefix_library_file(data):
            self._loaded_files.append(path)
            return
        from panelcad.library.parts import is_parts_library_file

        if is_parts_library_file(data):
            self._loaded_files.append(path)
            return
        from panelcad.library.io_modules import is_io_module_file, install_io_modules

        if is_io_module_file(data):
            install_io_modules(self, data, path)
            self._loaded_files.append(path)
            return
        if str(data.get("kind") or "") == "cutouts":
            self._loaded_files.append(path)
            return
        lib_name = str(data.get("library", "local"))
        for raw in list(data.get("devices") or []) + expand_jumper_tables(data):
            device = _device_from_raw(lib_name, raw)
            self.devices[device.library_id] = device
            self._device_files[device.library_id] = path
        self._loaded_files.append(path)

    def get(self, library_id: str) -> Device:
        try:
            return self.devices[library_id]
        except KeyError as exc:
            raise KeyError(f"unknown library_id '{library_id}'") from exc

    def search(self, query: str = "", standard: str | None = None) -> list[Device]:
        q = query.lower().strip()
        want = normalize_standard(standard, default="") if standard else ""
        items = [
            d
            for d in self.devices.values()
            if d.library_id not in {"reconstruct.needs_info", "reconstruct.measured_envelope"}
        ]
        if want:
            items = [d for d in items if d.standard == want]
        if q:
            compact = q.replace("/", "").replace("-", "").replace(" ", "")
            items = [
                d
                for d in items
                if q in d.library_id.lower()
                or q in d.name.lower()
                or q in d.category.lower()
                or compact in d.standard.lower().replace("/", "")
                or any(q in t.lower() for t in d.tags)
            ]
        return sorted(items, key=_search_key)

    def categories(self) -> list[str]:
        return sorted({d.category for d in self.devices.values()})

    def source_path(self, library_id: str) -> Path | None:
        return self._device_files.get(library_id)

    def save_device(self, device: Device, path: Path | str) -> Path:
        """Write or replace one device in a YAML library file. Reload it into this catalog."""

        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        lib_name = device.library_id.split(".", 1)[0] if "." in device.library_id else "local"
        if dest.is_file():
            data = _lib_yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
            lib_name = str(data.get("library") or lib_name)
            devices_raw = list(data.get("devices") or [])
        else:
            data = {"library": lib_name, "version": "0.1"}
            devices_raw = []
        raw = device_to_raw(device)
        replaced = False
        for i, existing in enumerate(devices_raw):
            if str(existing.get("id") or "") == device.id:
                devices_raw[i] = raw
                replaced = True
                break
        if not replaced:
            devices_raw.append(raw)
        data["library"] = lib_name
        data["devices"] = devices_raw
        dest.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        loaded = _device_from_raw(lib_name, raw)
        self.devices[loaded.library_id] = loaded
        self._device_files[loaded.library_id] = dest
        return dest


_STANDARD_RANK = {"IEC": 0, "NEMA/ANSI": 1, "OEM": 2}


def normalize_standard(value: str | None, *, default: str = "IEC") -> str:
    """Map library names and aliases onto IEC, NEMA/ANSI, or OEM.

    NEMA/ANSI is one North American group. ANSI, JIC, and NFPA all land there.
    """

    if value is None or str(value).strip() == "":
        return default
    token = "".join(ch.lower() for ch in str(value) if ch.isalnum())
    if token in {"nema", "ansi", "jic", "nfpa", "nemaansi"}:
        return "NEMA/ANSI"
    if token in {"oem", "adc"}:
        return "OEM"
    if token == "iec":
        return "IEC"
    return default


def _search_key(device: Device) -> tuple[int, str]:
    return (_STANDARD_RANK.get(device.standard, 9), device.library_id)


def _as_tag(value: Any) -> str:
    # YAML 1.1 parses unquoted no/yes/on/off as booleans.
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


_JUMPER_SCHEMATIC = {
    "width": 10,
    "height": 8,
    "pins": [],
    "draw": [{"rect": [1, 2, 8, 4]}],
}


def expand_jumper_tables(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn a compact ``jumpers:`` table into catalog devices you can extend."""

    rows: list[dict[str, Any]] = []
    for series in data.get("jumpers") or []:
        if not isinstance(series, dict):
            continue
        family = str(series.get("family") or "generic")
        try:
            pitch = float(series.get("pitch_mm") or 5.2)
        except (TypeError, ValueError):
            pitch = 5.2
        manufacturer = str(series.get("manufacturer") or "")
        kind = str(series.get("kind") or "bridge")
        cut_default = bool(series.get("cut_to_length") or False)
        pitch_slug = f"{pitch:g}".replace(".", "p")
        for item in series.get("items") or []:
            if not isinstance(item, dict):
                continue
            try:
                poles = int(item.get("poles") or 2)
            except (TypeError, ValueError):
                poles = 2
            poles = max(2, poles)
            device_id = str(item.get("id") or f"jumpers.{family}_{poles}p_{pitch_slug}")
            cut = bool(item["cut_to_length"]) if "cut_to_length" in item else cut_default
            part = str(item.get("part_number") or f"{poles}-pole jumper")
            name = str(item.get("name") or part)
            props: dict[str, Any] = {
                "accessory": "jumper",
                "jumper_family": family,
                "pitch_mm": pitch,
                "poles": poles,
                "jumper_kind": str(item.get("kind") or kind),
                "cut_to_length": cut,
                "manufacturer": str(item.get("manufacturer") or manufacturer),
                "part_number": part,
                "catalog": str(item.get("catalog") or part),
            }
            extra = item.get("props")
            if isinstance(extra, dict):
                props.update(extra)
            raw: dict[str, Any] = {
                "id": device_id,
                "name": name,
                "category": "connection.jumper",
                "ref_prefix": "J",
                "tags": ["jumper", kind, family],
                "schematic": dict(_JUMPER_SCHEMATIC),
                "panel": {
                    "w": max(1.0, pitch * max(poles - 1, 1)),
                    "h": 4.0,
                    "d": 3.0,
                    "din": False,
                    "in_panel": True,
                    "mount": "none",
                    "dims": "catalog",
                },
                "props": props,
            }
            standard = item.get("standard") or series.get("standard")
            if standard:
                raw["standard"] = standard
            rows.append(raw)
    return rows


def _device_from_raw(lib_name: str, raw: dict[str, Any]) -> Device:
    schematic = raw.get("schematic") or {}
    panel_raw = raw.get("panel") or {}
    pins = [PinDef.model_validate(p) for p in schematic.get("pins", [])]  # includes optional OEM label
    primitives: list[Primitive] = []
    for item in schematic.get("draw", []):
        primitives.append(expand_draw_item(item))
    primitives = land_leads_on_circle(str(raw.get("category", "")), pins, primitives)
    attributes = parse_attributes(schematic.get("attributes"))
    footprint_primitives: list[Primitive] = []
    footprint_attributes = []
    if isinstance(panel_raw, dict):
        for item in panel_raw.get("draw") or []:
            footprint_primitives.append(expand_draw_item(item))
        footprint_attributes = parse_attributes(panel_raw.get("attributes"))
    box = schematic.get("box")
    if box and len(box) == 4:
        w, h = float(box[2]), float(box[3])
    elif box and len(box) == 2:
        w, h = float(box[0]), float(box[1])
    else:
        w = float(schematic.get("width", 20))
        h = float(schematic.get("height", 40))
    category = str(raw.get("category", "misc"))
    panel = resolve_panel(category, panel_raw if isinstance(panel_raw, dict) else {})
    ports = [parse_port(p) for p in raw.get("ports") or [] if isinstance(p, dict)]
    device_id = str(raw["id"])
    return Device(
        library_id=f"{lib_name}.{device_id}",
        id=device_id,
        name=str(raw.get("name", device_id)),
        category=category,
        ref_prefix=str(raw.get("ref_prefix", "A")),
        tags=[_as_tag(t) for t in raw.get("tags", [])],
        schematic_width=w,
        schematic_height=h,
        pins=pins,
        primitives=primitives,
        attributes=attributes,
        ports=ports,
        panel=panel,
        footprint_primitives=footprint_primitives,
        footprint_attributes=footprint_attributes,
        block_kind=str(raw.get("block") or ""),
        properties=dict(raw.get("props") or raw.get("properties") or {}),
        standard=normalize_standard(raw.get("standard") or lib_name),
    )


def _primitive_to_draw(prim: Any) -> dict[str, Any]:
    if not isinstance(prim, Primitive):
        if isinstance(prim, dict):
            if any(k in prim for k in ("rect", "line", "circle", "arc", "polyline", "text")):
                return dict(prim)
            prim = expand_draw_item(prim)
        else:
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


def device_to_raw(device: Device) -> dict[str, Any]:
    pins: list[dict[str, Any]] = []
    for pin in device.pins:
        rec: dict[str, Any] = {"id": pin.id, "x": pin.x, "y": pin.y, "side": pin.side}
        if pin.electrical and pin.electrical != "passive":
            rec["electrical"] = pin.electrical
        if pin.label:
            rec["label"] = pin.label
        pins.append(rec)
    panel = device.panel
    raw: dict[str, Any] = {
        "id": device.id,
        "name": device.name,
        "category": device.category,
        "ref_prefix": device.ref_prefix,
        "tags": list(device.tags),
        "schematic": {
            "width": device.schematic_width,
            "height": device.schematic_height,
            "pins": pins,
            "draw": [_primitive_to_draw(p) for p in device.primitives],
        },
        "panel": {
            "w": panel.width_mm,
            "h": panel.height_mm,
            "d": panel.depth_mm,
            "din": panel.din_rail,
            "in_panel": panel.in_panel,
            "mount": panel.mount,
            "dims": panel.dim_source,
        },
    }
    if device.attributes:
        raw["schematic"]["attributes"] = [a.model_dump() for a in device.attributes]
    if device.footprint_primitives:
        raw["panel"]["draw"] = [_primitive_to_draw(p) for p in device.footprint_primitives]
    if device.footprint_attributes:
        raw["panel"]["attributes"] = [a.model_dump() for a in device.footprint_attributes]
    if device.block_kind:
        raw["block"] = device.block_kind
    if device.properties:
        raw["props"] = dict(device.properties)
    if device.ports:
        raw["ports"] = [p.model_dump() for p in device.ports]
    if device.standard and device.standard not in {"IEC", "NEMA/ANSI"}:
        raw["standard"] = device.standard
    return raw


def default_catalog(project_dir: Path | None = None) -> Catalog:
    cat = Catalog()
    cat.load_defaults(project_dir)
    return cat
