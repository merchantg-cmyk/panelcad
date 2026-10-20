"""Verified PLC I/O module terminal maps.

Addresses follow the same vendor rules as ``panelcad.core.plc_tags``:

* Do-more / BRX: X, Y, WX, WY in decimal from the module base (default 0).
  https://hosteng.com/DmDHelp/Content/Data_Types/Builtin_Data_Blocks.htm
* CLICK: X or Y, then the module position, then the point as two digits
  (X101 is point 1 on the first expansion module). Position 0 is the CPU.
  https://cdn.automationdirect.com/static/manuals/c0userm/c0userm.pdf
* CompactLogix 5069: Local:slot:I.Ptxx.Data and Local:slot:O.Ptxx.Data
  (5069-UM004H-EN-P March 2026 pp. 121, 126, 129). Analog is
  Local:slot:I.Chxx.Data and Local:slot:O.Chxx.Data (5069-UM005F-EN-P
  December 2024 p. 139). xx is two digits. Slot is user-editable (default 1).
* S7-1200: %I / %Q byte.bit and %IW / %QW. The byte is ``address_base``
  (default 0) because the process image is assigned in the device configuration.
  https://docs.tia.siemens.cloud/r/en-us/v21/declaring-plc-tags/editing-plc-tag-tables/exporting-and-importing-plc-tags/format-of-the-export-file-.xlsx
* Micro800 expansion: ``_IO_Xx_DI_yy`` / ``_IO_Xx_DO_yy``, slot 1–4, point 00–15.
  2080-UM003: https://literature.rockwellautomation.com/idc/groups/literature/documents/um/2080-um003_-en-p.pdf
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panelcad.core.models import Device

_DISCRETE_IN = {"di_sink", "di_source", "di_sink_source"}
_DISCRETE_OUT = {"do_sink", "do_source", "do_relay"}
_ANALOG_IN = {"ai", "ai_voltage", "ai_current"}
_ANALOG_OUT = {"ao", "ao_voltage", "ao_current"}

FIELD_OK: dict[str, set[str]] = {
    "di_sink": {"pushbutton", "limit_switch", "prox_3wire", "spare"},
    "di_source": {"pushbutton", "limit_switch", "prox_3wire", "spare"},
    "di_sink_source": {"pushbutton", "limit_switch", "prox_3wire", "spare"},
    "do_sink": {"relay_coil", "solenoid", "pilot_light", "spare"},
    "do_source": {"relay_coil", "solenoid", "pilot_light", "spare"},
    "do_relay": {"relay_coil", "solenoid", "pilot_light", "spare"},
    "ai": {"transmitter_2wire", "transmitter_4wire", "spare"},
    "ai_voltage": {"transmitter_2wire", "transmitter_4wire", "spare"},
    "ai_current": {"transmitter_2wire", "transmitter_4wire", "spare"},
    "ao": {"spare"},
    "ao_voltage": {"spare"},
    "ao_current": {"spare"},
}


def is_io_module_file(data: dict[str, Any]) -> bool:
    return str(data.get("kind") or "") == "io_modules"


@dataclass
class IoPoint:
    pin: str
    terminal: str
    index: int
    pair: str = ""
    name: str = ""


@dataclass
class IoGroup:
    common: str
    points: list[str]


@dataclass
class IoPower:
    pin: str
    terminal: str
    role: str
    point: str = ""


@dataclass
class IoModule:
    id: str
    catalog: str = ""
    manufacturer: str = ""
    platform: str = ""
    series: str = ""
    library_id: str = ""
    verified: bool = False
    message: str = ""
    source: str = ""
    point_type: str = ""
    supply: str = "24VDC"
    address_style: str = ""
    address_base: int = 0
    io_type: str = ""
    return_note: str = ""
    points: list[IoPoint] = field(default_factory=list)
    groups: list[IoGroup] = field(default_factory=list)
    power: list[IoPower] = field(default_factory=list)
    unwired: list[IoPower] = field(default_factory=list)
    terminal_order: list[list[str]] = field(default_factory=list)
    panel: dict[str, Any] = field(default_factory=dict)
    accessories: list[dict[str, str]] = field(default_factory=list)

    def io_kind(self) -> str:
        if self.io_type:
            return self.io_type.upper()
        if self.point_type in _DISCRETE_IN:
            return "DI"
        if self.point_type in _DISCRETE_OUT:
            return "DO"
        if self.point_type in _ANALOG_IN:
            return "AI"
        if self.point_type in _ANALOG_OUT:
            return "AO"
        return "DI"

    def point(self, pin: str) -> IoPoint:
        for item in self.points:
            if item.pin == pin:
                return item
        raise KeyError(pin)

    def terminal_map(self) -> list[dict[str, str]]:
        """Every cited terminal: signal, common, power, earth, or nc."""

        rows: list[dict[str, str]] = []
        grouped: dict[str, str] = {}
        for group in self.groups:
            for pin in group.points:
                grouped[pin] = group.common
        for item in self.points:
            rows.append(
                {
                    "pin": item.pin,
                    "terminal": item.terminal,
                    "role": "point",
                    "common": grouped.get(item.pin, ""),
                    "index": str(item.index),
                }
            )
        for item in self.power:
            rows.append(
                {
                    "pin": item.pin,
                    "terminal": item.terminal,
                    "role": item.role,
                    "common": item.pin if item.role == "common" else "",
                    "index": "",
                }
            )
        for item in self.unwired:
            rows.append(
                {
                    "pin": item.pin,
                    "terminal": item.terminal,
                    "role": item.role or "nc",
                    "common": "",
                    "index": "",
                }
            )
        return rows


def _point(raw: dict[str, Any]) -> IoPoint:
    return IoPoint(
        pin=str(raw.get("pin") or ""),
        terminal=str(raw.get("terminal") or raw.get("pin") or ""),
        index=int(raw.get("index") or 0),
        pair=str(raw.get("pair") or ""),
        name=str(raw.get("name") or ""),
    )


def _power(raw: dict[str, Any]) -> IoPower:
    return IoPower(
        pin=str(raw.get("pin") or ""),
        terminal=str(raw.get("terminal") or raw.get("pin") or ""),
        role=str(raw.get("role") or "common"),
        point=str(raw.get("point") or ""),
    )


def module_from_raw(raw: dict[str, Any]) -> IoModule:
    return IoModule(
        id=str(raw.get("id") or ""),
        catalog=str(raw.get("catalog") or ""),
        manufacturer=str(raw.get("manufacturer") or ""),
        platform=str(raw.get("platform") or ""),
        series=str(raw.get("series") or ""),
        library_id=str(raw.get("library_id") or ""),
        verified=bool(raw.get("verified")),
        message=str(raw.get("message") or "").strip(),
        source=str(raw.get("source") or ""),
        point_type=str(raw.get("point_type") or ""),
        supply=str(raw.get("supply") or "24VDC"),
        address_style=str(raw.get("address_style") or ""),
        address_base=int(raw.get("address_base") or 0),
        io_type=str(raw.get("io_type") or ""),
        return_note=str(raw.get("return_note") or "").strip(),
        points=[_point(item) for item in raw.get("points") or [] if isinstance(item, dict)],
        groups=[
            IoGroup(common=str(item.get("common") or ""), points=[str(p) for p in item.get("points") or []])
            for item in raw.get("groups") or []
            if isinstance(item, dict)
        ],
        power=[_power(item) for item in raw.get("power") or [] if isinstance(item, dict)],
        unwired=[_power(item) for item in raw.get("unwired") or [] if isinstance(item, dict)],
        terminal_order=[
            [str(cell) for cell in row] for row in raw.get("terminal_order") or [] if isinstance(row, list)
        ],
        panel=dict(raw.get("panel") or {}) if isinstance(raw.get("panel"), dict) else {},
        accessories=[
            {
                "catalog": str(item.get("catalog") or ""),
                "description": str(item.get("description") or ""),
                "note": str(item.get("note") or ""),
            }
            for item in raw.get("accessories") or []
            if isinstance(item, dict) and str(item.get("catalog") or "").strip()
        ],
    )


def unverified_message(module: IoModule) -> str:
    if module.message:
        return module.message
    name = module.catalog or module.id
    return (
        f"{name} has no verified I/O terminal map from a manufacturer installation manual. "
        "PanelCAD will not auto-draw it."
    )


def require_verified(module: IoModule) -> None:
    if not module.verified:
        raise ValueError(unverified_message(module))


def format_point_address(
    module: IoModule,
    point: IoPoint,
    *,
    slot: int = 1,
    position: int = 1,
    address_base: int | None = None,
) -> str:
    """Platform address drawn on the sheet and stored on the I/O point.

    Pin ids stay free of dots so ``parse_pin_ref`` can split ``REF.PIN``.
    """

    kind = module.io_kind()
    index = int(point.index)
    base = module.address_base if address_base is None else int(address_base)
    style = module.address_style
    if style == "domore":
        prefix = {"DI": "X", "DO": "Y", "AI": "WX", "AO": "WY"}[kind]
        return f"{prefix}{base + index}"
    if style == "click":
        # Point numbers on the module are 1–16. Position 1 is the first module after the CPU.
        prefix = "X" if kind in {"DI", "AI"} else "Y"
        return f"{prefix}{int(position)}{index + 1:02d}"
    if style == "logix":
        from panelcad.core.plc_tags import _logix_address

        return _logix_address(kind, int(slot), index)
    if style == "tia":
        from panelcad.core.plc_tags import _tia_address

        if kind in {"DI", "DO"}:
            number = base * 8 + index
        else:
            # Analog words are even byte addresses. Each channel takes one word.
            number = base + index * 2
        return _tia_address(kind, number)
    if style == "micro800":
        # 2080-UM003 global variables. Slot is the expansion slot, 1–4.
        token = "DI" if kind == "DI" else "DO" if kind == "DO" else "AI" if kind == "AI" else "AO"
        return f"_IO_X{int(slot)}_{token}_{index:02d}"
    return point.pin


def point_address(
    module: IoModule,
    point: IoPoint,
    *,
    slot: int = 1,
    position: int = 1,
    address_base: int | None = None,
) -> str:
    """The one address string used by the sheet, the I/O list, and tag export."""

    return format_point_address(
        module, point, slot=slot, position=position, address_base=address_base
    )


def find_module(catalog: Any, key: str) -> IoModule:
    text = str(key or "").strip()
    modules: dict[str, IoModule] = getattr(catalog, "io_modules", {}) or {}
    if text in modules:
        return modules[text]
    folded = text.casefold()
    for module in modules.values():
        if module.id.casefold() == folded or module.library_id.casefold() == folded:
            return module
        if module.catalog and module.catalog.casefold() == folded and module.verified:
            return module
    try:
        device = catalog.get(text)
    except KeyError:
        device = None
    if device is not None:
        for module in modules.values():
            if module.library_id == device.library_id:
                return module
        from panelcad.core.electrical import symbol_role

        if symbol_role(device) == "plc":
            raise ValueError(
                f"{device.library_id} has no verified I/O terminal map. "
                "PanelCAD will not auto-draw it."
            )
    known = ", ".join(sorted(module.id for module in modules.values() if module.verified))
    raise ValueError(f"unknown I/O module '{text}'. Verified modules: {known}")


def list_modules(catalog: Any, platform: str | None = None) -> list[IoModule]:
    want = str(platform or "").strip().casefold()
    rows = list(getattr(catalog, "io_modules", {}).values())
    if want:
        rows = [row for row in rows if row.platform.casefold() == want]
    return sorted(rows, key=lambda row: (row.platform, not row.verified, row.id))


def install_io_modules(catalog: Any, data: dict[str, Any], path: Path) -> None:
    from panelcad.library.catalog import _device_from_raw

    for raw in data.get("modules") or []:
        if not isinstance(raw, dict):
            continue
        module = module_from_raw(raw)
        if not module.id:
            continue
        if module.verified and not module.library_id:
            device = _device_from_raw("io", _symbol_raw(module))
            catalog.devices[device.library_id] = device
            catalog._device_files[device.library_id] = path
            module.library_id = device.library_id
        catalog.io_modules[module.id] = module


def finalize_io_modules(catalog: Any) -> None:
    """Drop verification when a cited library symbol is missing terminals.

    Device YAML can load after this file, so the check waits until the directory is in.
    """

    for module in catalog.io_modules.values():
        if not module.verified or not module.library_id:
            continue
        if str(module.library_id).startswith("io."):
            continue
        missing = _missing_pins(catalog, module)
        if not missing:
            continue
        module.verified = False
        module.message = (
            f"{module.catalog or module.id} is missing terminals {', '.join(missing)} "
            f"on {module.library_id}. PanelCAD will not auto-draw it."
        )


def _missing_pins(catalog: Any, module: IoModule) -> list[str]:
    try:
        device: Device = catalog.get(module.library_id)
    except KeyError:
        return [module.library_id or module.id]
    have = {pin.id for pin in device.pins}
    need = [item.pin for item in module.points + module.power]
    return [pin for pin in need if pin not in have]


def _symbol_raw(module: IoModule) -> dict[str, Any]:
    pins: list[dict[str, Any]] = []
    y = 16.0
    for item in module.points:
        electrical = "output" if module.io_kind() in {"DO", "AO"} else "input"
        side = "right" if module.point_type in {"do_source", "do_relay", "ao", "ao_voltage", "ao_current"} else "left"
        pins.append(
            {
                "id": item.pin,
                "x": 92.0 if side == "right" else 0.0,
                "y": y,
                "side": side,
                "electrical": electrical,
                "label": item.terminal,
            }
        )
        y += 10.0
    for item in module.power:
        pins.append(
            {
                "id": item.pin,
                "x": 0.0,
                "y": y,
                "side": "left",
                "electrical": "power",
                "label": item.terminal,
            }
        )
        y += 8.0
    # Unwired / NC terminals stay on the module record and the sheet note.
    # They are not extra I/O points on the symbol.
    height = max(40.0, y + 8.0)
    raw: dict[str, Any] = {
        "id": module.id,
        "name": module.catalog or module.id,
        "category": "control.plc",
        "ref_prefix": "A",
        "tags": ["plc", "io", module.platform, module.point_type],
        "schematic": {
            "width": 92,
            "height": height,
            "pins": pins,
            "draw": [{"rect": [0, 0, 92, height]}],
        },
        "props": {
            "manufacturer": module.manufacturer,
            "catalog": module.catalog,
            "part_number": module.catalog,
            "source": module.source,
            "io_module": module.id,
            "verified": True,
        },
    }
    if module.panel:
        raw["panel"] = module.panel
    return raw
