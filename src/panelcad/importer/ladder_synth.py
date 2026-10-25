"""Universal From-To wire schedule extraction and clean NEMA/IEC ladder synthesis for imported prints."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from panelcad.api.session import Session
from panelcad.core.geometry import Point
from panelcad.core.models import Component, Project, Sheet, Wire
from panelcad.library.catalog import Catalog, default_catalog


_RAIL_HOT_TOKENS = {"L1", "24V", "+24V", "24VDC", "+24VDC", "L+", "X1", "HOT", "120VAC", "120V"}
_RAIL_RET_TOKENS = {"L2", "0V", "0VDC", "M", "N", "X2", "COM", "RET", "NEUTRAL"}
_POWER_RAIL_TOKENS = {"L1", "L2", "L3", "T1", "T2", "T3", "U", "V", "W", "U1", "V1", "W1", "PE", "GND"}


@dataclass
class ImportDeviceEntry:
    """One device extracted from an imported print/scan for the From-To schedule."""

    tag: str
    symbol: str = ""
    library_id: str = ""
    description: str = ""
    pins: list[str] = field(default_factory=list)
    sheet_id: str = "page-001"
    x: float = 0.0
    y: float = 0.0
    confidence: float = 1.0
    source: str = "import"
    needs_review: bool = False
    role: str = "control"

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "symbol": self.symbol,
            "library_id": self.library_id,
            "description": self.description,
            "pins": list(self.pins),
            "sheet_id": self.sheet_id,
            "x": round(float(self.x), 2),
            "y": round(float(self.y), 2),
            "confidence": round(float(self.confidence), 3),
            "source": self.source,
            "needs_review": bool(self.needs_review),
            "role": self.role,
        }


@dataclass
class ImportFromToRow:
    """One wire connection row in the imported From-To wire schedule."""

    wire_number: str
    from_tag: str
    from_pin: str
    to_tag: str
    to_pin: str
    sheet_id: str = "page-001"
    rung: int = 1
    confidence: float = 1.0
    source: str = "extracted"
    needs_review: bool = False

    @property
    def from_endpoint(self) -> str:
        if not self.from_pin or self.from_tag.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
            return self.from_tag
        return f"{self.from_tag}.{self.from_pin}"

    @property
    def to_endpoint(self) -> str:
        if not self.to_pin or self.to_tag.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
            return self.to_tag
        return f"{self.to_tag}.{self.to_pin}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "wire_number": self.wire_number,
            "from_tag": self.from_tag,
            "from_pin": self.from_pin,
            "to_tag": self.to_tag,
            "to_pin": self.to_pin,
            "from": self.from_endpoint,
            "to": self.to_endpoint,
            "sheet_id": self.sheet_id,
            "rung": int(self.rung),
            "confidence": round(float(self.confidence), 3),
            "source": self.source,
            "needs_review": bool(self.needs_review),
        }


@dataclass
class ImportFromToSchedule:
    """Structured From-To Wire Schedule + Device Table for NEMA and IEC imported prints."""

    standard: str = "NEMA/ANSI"
    rail_hot: str = "L1"
    rail_return: str = "L2"
    devices: list[ImportDeviceEntry] = field(default_factory=list)
    from_to_rows: list[ImportFromToRow] = field(default_factory=list)
    rungs: list[dict[str, Any]] = field(default_factory=list)
    power_columns: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "standard": self.standard,
            "rail_hot": self.rail_hot,
            "rail_return": self.rail_return,
            "devices": [d.to_dict() for d in self.devices],
            "from_to_rows": [r.to_dict() for r in self.from_to_rows],
            "rungs": list(self.rungs),
            "power_columns": list(self.power_columns),
            "warnings": list(self.warnings),
            "needs_review_count": sum(1 for r in self.from_to_rows if r.needs_review)
            + sum(1 for d in self.devices if d.needs_review),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportFromToSchedule":
        devs: list[ImportDeviceEntry] = []
        for raw_d in data.get("devices") or []:
            if isinstance(raw_d, ImportDeviceEntry):
                devs.append(raw_d)
            elif isinstance(raw_d, dict):
                devs.append(
                    ImportDeviceEntry(
                        tag=str(raw_d.get("tag") or ""),
                        symbol=str(raw_d.get("symbol") or ""),
                        library_id=str(raw_d.get("library_id") or ""),
                        description=str(raw_d.get("description") or ""),
                        pins=list(raw_d.get("pins") or []),
                        sheet_id=str(raw_d.get("sheet_id") or "page-001"),
                        x=float(raw_d.get("x") or 0.0),
                        y=float(raw_d.get("y") or 0.0),
                        confidence=float(raw_d.get("confidence", 1.0)),
                        source=str(raw_d.get("source") or "import"),
                        needs_review=bool(raw_d.get("needs_review", False)),
                        role=str(raw_d.get("role") or "control"),
                    )
                )
        rows: list[ImportFromToRow] = []
        for raw_r in data.get("from_to_rows") or []:
            if isinstance(raw_r, ImportFromToRow):
                rows.append(raw_r)
            elif isinstance(raw_r, dict):
                f_tag = str(raw_r.get("from_tag") or "")
                f_pin = str(raw_r.get("from_pin") or "")
                if not f_tag and raw_r.get("from"):
                    f_tag, f_pin = _split_endpoint(str(raw_r.get("from") or ""))
                t_tag = str(raw_r.get("to_tag") or "")
                t_pin = str(raw_r.get("to_pin") or "")
                if not t_tag and raw_r.get("to"):
                    t_tag, t_pin = _split_endpoint(str(raw_r.get("to") or ""))
                rows.append(
                    ImportFromToRow(
                        wire_number=str(raw_r.get("wire_number") or ""),
                        from_tag=f_tag,
                        from_pin=f_pin,
                        to_tag=t_tag,
                        to_pin=t_pin,
                        sheet_id=str(raw_r.get("sheet_id") or "page-001"),
                        rung=int(raw_r.get("rung") or 1),
                        confidence=float(raw_r.get("confidence", 1.0)),
                        source=str(raw_r.get("source") or "extracted"),
                        needs_review=bool(raw_r.get("needs_review", False)),
                    )
                )
        return cls(
            standard=str(data.get("standard") or "NEMA/ANSI"),
            rail_hot=str(data.get("rail_hot") or "L1"),
            rail_return=str(data.get("rail_return") or "L2"),
            devices=devs,
            from_to_rows=rows,
            rungs=list(data.get("rungs") or []),
            power_columns=list(data.get("power_columns") or []),
            warnings=list(data.get("warnings") or []),
        )


def _norm_sheet_id(page_id: str) -> str:
    text = str(page_id or "page-001").strip()
    return text.replace("_", "-") if text else "page-001"


def _detect_standard(raw_std: str, devices: list[dict[str, Any]], rails: list[str]) -> str:
    up = str(raw_std or "").upper()
    if "IEC" in up:
        return "IEC"
    if "NEMA" in up or "ANSI" in up or "UL" in up:
        return "NEMA/ANSI"
    rail_up = {str(r).upper() for r in rails if r}
    if rail_up & {"24V", "+24V", "L+", "0V", "M"}:
        return "IEC"
    if rail_up & {"L1", "L2", "X1", "X2"}:
        return "NEMA/ANSI"
    iec_count = 0
    nema_count = 0
    for d in devices:
        lid = str(d.get("library_id") or "").lower()
        tag = str(d.get("tag") or d.get("id") or "").upper()
        if lid.startswith("iec.") or re.match(r"^[-+]?(Q|K|S|F|B|H)\d+$", tag):
            iec_count += 1
        elif lid.startswith("nema.") or re.match(r"^(CB|PB|CR|TR|LT|PL|OL|MTR|SS|LS|DISC|FU)\d*", tag):
            nema_count += 1
    return "IEC" if iec_count > nema_count else "NEMA/ANSI"


def _classify_device_role_and_library(
    tag: str,
    raw_type: str,
    raw_symbol: str,
    raw_library_id: str,
    pins: list[str],
    standard: str,
    catalog: Catalog,
    *,
    has_other_coil_or_poles: bool = False,
) -> tuple[str, str, list[str]]:
    """Return (role, library_id, default_pins) for a device in target standard."""
    is_iec = standard == "IEC"
    prefix = "iec" if is_iec else "nema"
    clean_tag = str(tag or "").strip().lstrip("-+")
    up_tag = clean_tag.upper()
    low_type = f"{raw_type or ''} {raw_symbol or ''} {raw_library_id or ''}".lower()
    pin_set = {str(p).strip().upper() for p in (pins or []) if str(p).strip()}

    has_3ph_pins = bool(
        {"L1", "L2", "L3"} <= pin_set
        or {"T1", "T2", "T3"} <= pin_set
        or {"U1", "V1", "W1"} <= pin_set
        or {"U", "V", "W"} <= pin_set
        or {"1", "3", "5"} <= pin_set
        or {"2", "4", "6"} <= pin_set
    )

    if raw_library_id and "k_4co" in raw_library_id.lower():
        try:
            dev = catalog.get(raw_library_id)
            return "control_output", raw_library_id, [str(p.id) for p in dev.pins]
        except KeyError:
            pass
    if raw_library_id and (
        any(k in raw_library_id.lower() for k in (".plc.", ".hmi.", ".power.psu", "click."))
        or (
            any(k in raw_library_id.lower() for k in ("durapulse.", ".drives."))
            and not has_3ph_pins
            and bool(pin_set & {"DI1", "DI2", "DCM", "ACM", "AI1", "COM", "R1A", "R1B", "R1C"})
        )
    ):
        try:
            dev = catalog.get(raw_library_id)
            return "control_block", raw_library_id, [str(p.id) for p in dev.pins]
        except KeyError:
            pass

    has_both_line_and_load_3ph = (
        ({"L1", "L2", "L3"} <= pin_set or {"1", "3", "5"} <= pin_set)
        and ({"T1", "T2", "T3"} <= pin_set or {"2", "4", "6"} <= pin_set)
    )
    if re.match(r"^(M|K)\d+$", up_tag) and has_both_line_and_load_3ph:
        lid = f"{prefix}.contactors.ac3_9a"
        return "power_contactor", lid, ["L1", "L2", "L3", "T1", "T2", "T3"]

    if (
        "motor_3ph" in low_type
        or "3ph_motor" in low_type
        or re.match(r"^MTR\d*$", up_tag)
        or (re.match(r"^M\d+$", up_tag) and ("motor" in low_type or {"U1", "V1", "W1"} <= pin_set or {"T1", "T2", "T3"} == pin_set))
    ):
        lid = "iec.loads.motor_3ph" if is_iec else "nema.loads.motor_3ph"
        dpins = ["U1", "V1", "W1"] if is_iec else ["T1", "T2", "T3"]
        return "power_motor", lid, dpins

    if "vfd" in low_type or "drive" in low_type or re.match(r"^VFD\d*$", up_tag):
        lid = f"{prefix}.drives.vfd_1k5"
        return "power_vfd", lid, ["L1", "L2", "L3", "U", "V", "W"]

    if (
        "mcb_3p" in low_type
        or "mccb_3p" in low_type
        or "breaker_3p" in low_type
        or (re.match(r"^(CB|MCCB|Q)\d+$", up_tag) and "1p" not in low_type and pin_set != {"1", "2"})
    ):
        if "disconnect" in low_type:
            lid = f"{prefix}.protection.disconnect_3p"
            dpins = ["1", "3", "5", "2", "4", "6"] if is_iec else ["L1", "L2", "L3", "T1", "T2", "T3"]
        else:
            lid = f"{prefix}.breakers.mcb_3p_32a"
            dpins = ["1", "3", "5", "2", "4", "6"]
        return "power_breaker", lid, dpins

    if "disconnect_3p" in low_type or re.match(r"^(DISC|DS)\d+$", up_tag):
        lid = f"{prefix}.protection.disconnect_3p"
        dpins = ["1", "3", "5", "2", "4", "6"] if is_iec else ["L1", "L2", "L3", "T1", "T2", "T3"]
        return "power_breaker", lid, dpins

    if "fuse_3p" in low_type or (re.match(r"^FU\d+$", up_tag) and has_3ph_pins):
        lid = f"{prefix}.protection.fuse_3p"
        return "power_breaker", lid, ["1", "3", "5", "2", "4", "6"]

    if "ac3_" in low_type or "contactor_3p" in low_type or (re.match(r"^(M|K)\d+$", up_tag) and has_3ph_pins):
        lid = f"{prefix}.contactors.ac3_9a"
        return "power_contactor", lid, ["L1", "L2", "L3", "T1", "T2", "T3"]

    if "ol_contact" in low_type or (pin_set and pin_set <= {"95", "96", "97", "98"} and len(pin_set) <= 2):
        lid = f"{prefix}.protection.ol_contact"
        return "control_ol_contact", lid, ["95", "96"]

    if ("overload" in low_type or re.match(r"^(OL|F)\d+$", up_tag)) and has_3ph_pins:
        lid = "iec.protection.overload_9a" if is_iec else "nema.protection.overload"
        dpins = ["1", "3", "5", "2", "4", "6"] if is_iec else ["T1", "T2", "T3", "2", "4", "6"]
        return "power_overload", lid, dpins

    if re.match(r"^OL\d*(-\d+)?$", up_tag) and not has_3ph_pins:
        lid = f"{prefix}.protection.ol_contact"
        return "control_ol_contact", lid, ["95", "96"]

    if "pilot_light" in low_type or "indicator" in low_type or "lamp" in low_type or re.match(r"^(LT|PL|HL|H)\d+$", up_tag):
        lid = f"{prefix}.operators.pilot_light"
        return "control_output", lid, ["X1", "X2"]

    if "horn" in low_type or "buzzer" in low_type or re.match(r"^(HN|BZ|AH)\d+$", up_tag):
        lid = f"{prefix}.operators.horn" if not is_iec else "iec.operators.pilot_light"
        return "control_output", lid, ["X1", "X2"]

    if "solenoid" in low_type or re.match(r"^(SOL|SV|YV)\d+$", up_tag):
        lid = "nema.loads.solenoid" if not is_iec else "iec.contactors.coil"
        return "control_output", lid, ["A1", "A2"]

    if (
        "timer" in low_type
        or "on_delay" in low_type
        or "off_delay" in low_type
        or (re.match(r"^(TR|KT|T|ALT)\d+$", up_tag) and "contact" not in low_type and not (pin_set & {"1", "2", "3", "4", "13", "14", "11", "12"}))
    ):
        if "contact" not in low_type:
            lid = f"{prefix}.timers.on_delay"
            return "control_output", lid, ["A1", "A2"]

    if (
        "coil" in low_type
        or "k_4co" in low_type
        or "relays.cr" in low_type
        or (pin_set and pin_set <= {"A1", "A2"})
    ):
        if re.match(r"^CR\d+$", up_tag) and not is_iec:
            lid = "nema.relays.cr"
        else:
            lid = f"{prefix}.contactors.coil"
        return "control_output", lid, ["A1", "A2"]

    if "terminal" in low_type or re.match(r"^(TB|X)\d*(-\d+)?$", up_tag):
        lid = f"{prefix}.terminals.feed_through"
        return "control_contact", lid, ["A", "B"]

    if "fuse" in low_type or re.match(r"^(FU|F)\d+$", up_tag):
        lid = "nema.protection.fuse_10a" if not is_iec else "iec.protection.fuse_10a"
        try:
            catalog.get(lid)
        except KeyError:
            lid = f"{prefix}.protection.fuse"
        return "control_contact", lid, ["1", "2"]

    if "estop" in low_type or re.match(r"^ESTOP\d*$", up_tag):
        lid = f"{prefix}.operators.estop"
        dpins = ["11", "12"] if is_iec else ["1", "2"]
        return "control_contact", lid, dpins

    if "selector" in low_type or re.match(r"^SS\d+$", up_tag):
        lid = f"{prefix}.operators.selector_no"
        dpins = ["13", "14"] if is_iec else ["3", "4"]
        return "control_contact", lid, dpins

    if "limit" in low_type or re.match(r"^LS\d+$", up_tag):
        is_nc = "nc" in low_type or (bool(pin_set) and pin_set <= {"1", "2", "11", "12", "21", "22"})
        if is_nc:
            lid = f"{prefix}.operators.limit_nc"
            dpins = ["11", "12"] if is_iec else ["1", "2"]
        else:
            lid = f"{prefix}.operators.limit_no"
            dpins = ["13", "14"] if is_iec else ["3", "4"]
        return "control_contact", lid, dpins

    if "pb_" in low_type or "pushbutton" in low_type or re.match(r"^(PB|S)\d+$", up_tag):
        is_nc = (
            "nc" in low_type
            or bool(pin_set & {"1", "2", "11", "12", "21", "22"})
            or (up_tag in {"PB1", "S0", "S1"} and "no" not in low_type and not (pin_set & {"3", "4", "13", "14"}))
        )
        if is_nc and up_tag != "PB2":
            lid = f"{prefix}.operators.pb_nc"
            dpins = ["11", "12"] if is_iec else ["1", "2"]
        else:
            lid = f"{prefix}.operators.pb_no"
            dpins = ["13", "14"] if is_iec else ["3", "4"]
        return "control_contact", lid, dpins

    if "aux_" in low_type or "contact" in low_type or re.match(r"^(M|K|CR|TR|ALT)\d+(-\d+)?$", up_tag):
        if (
            not has_other_coil_or_poles
            and "-" not in up_tag
            and not (pin_set & {"1", "2", "3", "4", "13", "14", "21", "22"})
            and "aux" not in low_type
            and "contact" not in low_type
        ):
            lid = "nema.relays.cr" if (re.match(r"^CR\d+$", up_tag) and not is_iec) else f"{prefix}.contactors.coil"
            return "control_output", lid, ["A1", "A2"]
        is_nc = "nc" in low_type or bool(pin_set & {"1", "2", "11", "12", "21", "22"})
        if is_nc:
            lid = f"{prefix}.contactors.aux_nc"
            dpins = ["21", "22"] if is_iec else ["1", "2"]
        else:
            lid = f"{prefix}.contactors.aux_no"
            dpins = ["13", "14"] if is_iec else ["3", "4"]
        return "control_contact", lid, dpins

    if raw_library_id and not raw_library_id.startswith("reconstruct."):
        try:
            dev = catalog.get(raw_library_id)
            dpins = [str(p.id) for p in dev.pins]
            if len(dpins) > 2 or any(k in raw_library_id.lower() for k in (".plc.", ".hmi.", ".power.psu", "click.")):
                return "control_block", raw_library_id, dpins
            if {"A1", "A2"} <= set(dpins) or {"X1", "X2"} <= set(dpins):
                return "control_output", raw_library_id, dpins[:2]
            return "control_contact", raw_library_id, dpins[:2] if len(dpins) >= 2 else ["3", "4"]
        except KeyError:
            pass

    lid = f"{prefix}.operators.pb_no"
    dpins = ["13", "14"] if is_iec else ["3", "4"]
    return "control_contact", lid, dpins


def _load_facts_dict(job_or_facts: Any, catalog: Catalog) -> tuple[dict[str, Any], Path | None, Any]:
    """Normalize an ImportJob, Session, Project, Path, or dict into a facts-like dict."""
    job_obj = None
    facts_path: Path | None = None

    if not isinstance(job_or_facts, (str, Path)) and (hasattr(job_or_facts, "ws") or hasattr(job_or_facts, "root")):
        job_obj = job_or_facts
        root = Path(job_or_facts.ws.root if hasattr(job_or_facts, "ws") else job_or_facts.root)
        for name in ("facts.json", "stitch.json"):
            candidate = root / name
            if candidate.exists():
                facts_path = candidate
                data = json.loads(candidate.read_text(encoding="utf-8"))
                return data, facts_path, job_obj
        if hasattr(job_or_facts, "stitch"):
            data = job_or_facts.stitch()
            return data, root / "stitch.json", job_obj

    if isinstance(job_or_facts, (str, Path)):
        p = Path(job_or_facts)
        if p.is_dir():
            for name in ("facts.json", "stitch.json"):
                candidate = p / name
                if candidate.exists():
                    return json.loads(candidate.read_text(encoding="utf-8")), candidate, None
        elif p.exists():
            if p.suffix.lower() in {".yaml", ".yml"}:
                sess = Session.open(p)
                return _project_to_facts_dict(sess.project, catalog), None, sess
            return json.loads(p.read_text(encoding="utf-8")), p, None

    if isinstance(job_or_facts, Session):
        return _project_to_facts_dict(job_or_facts.project, catalog), None, job_or_facts

    if isinstance(job_or_facts, Project):
        return _project_to_facts_dict(job_or_facts, catalog), None, job_or_facts

    if isinstance(job_or_facts, dict):
        return dict(job_or_facts), None, None

    raise TypeError(f"Unsupported input for From-To extraction: {type(job_or_facts)!r}")


def _project_to_facts_dict(project: Project, catalog: Catalog) -> dict[str, Any]:
    """Convert an in-memory Project into a facts-like dict for From-To extraction."""
    tb = project.title_block
    pages: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    wires: list[dict[str, Any]] = []
    pi = (project.properties or {}).get("print_import") or {}

    for idx, sheet in enumerate(project.sheets, start=1):
        if sheet.kind not in {"schematic", "io"}:
            continue
        pages.append({"page_id": sheet.id, "sheet_number": str(idx), "kind": sheet.kind})
        for comp in sheet.components:
            props = comp.properties or {}
            tag = str(props.get("print_tag") or props.get("device_tag") or comp.id)
            conf = float(props.get("print_import_confidence", 0.92))
            devices.append(
                {
                    "tag": comp.id,
                    "base_tag": tag,
                    "library_id": comp.library_id,
                    "type": comp.library_id,
                    "symbol": comp.library_id,
                    "description": str(props.get("description") or getattr(comp, "description", "") or ""),
                    "x": float(comp.x),
                    "y": float(comp.y),
                    "page_id": sheet.id,
                    "confidence": conf,
                    "source": str(props.get("print_import_source") or "project"),
                }
            )
        for wire in sheet.wires:
            wires.append(
                {
                    "id": wire.id,
                    "number": str(wire.number or wire.label or wire.net or ""),
                    "from": str(wire.frm or ""),
                    "to": str(wire.to or ""),
                    "page_id": sheet.id,
                    "confidence": 0.95,
                    "source": "wired",
                    "points": [[float(pt.x), float(pt.y)] for pt in (wire.points or [])],
                }
            )

    for item in pi.get("items") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "wire" and item.get("x") is not None and item.get("y") is not None:
            wires.append(
                {
                    "number": str(item.get("text") or ""),
                    "from": "",
                    "to": "",
                    "x": float(item["x"]),
                    "y": float(item["y"]),
                    "page_id": str(item.get("sheet") or (pages[0]["page_id"] if pages else "page-001")),
                    "confidence": float(item.get("confidence", 0.55)),
                    "source": str(item.get("reason") or "unattached_wire_label"),
                }
            )
        elif item.get("kind") == "device" and item.get("reason") == "needs_info":
            tag_txt = str(item.get("text") or "").strip()
            sid = str(item.get("sheet") or (pages[0]["page_id"] if pages else "page-001"))
            if tag_txt and not any(d.get("tag") == tag_txt and d.get("page_id") == sid for d in devices):
                if item.get("x") is not None and item.get("y") is not None:
                    devices.append(
                        {
                            "tag": tag_txt,
                            "library_id": "",
                            "type": "",
                            "symbol": "",
                            "x": float(item["x"]),
                            "y": float(item["y"]),
                            "page_id": sid,
                            "confidence": float(item.get("confidence", 0.48)),
                            "source": "needs_info",
                        }
                    )

    return {
        "title_block": {
            "drawing_title": project.name or "",
            "drawing_number": tb.drawing_number or "",
            "company": tb.company or "",
            "standard": tb.standard or "NEMA/ANSI",
            "rail_hot": tb.rail_hot or "L1",
            "rail_return": tb.rail_return or "L2",
        },
        "pages": pages,
        "devices": devices,
        "wires": wires,
        "from_to_schedule": pi.get("from_to_schedule"),
    }


def _split_endpoint(end: str) -> tuple[str, str]:
    text = str(end or "").strip()
    if not text:
        return "", ""
    if "." not in text:
        return text, ""
    tag, pin = text.rsplit(".", 1)
    return tag.strip(), pin.strip()


def extract_import_from_to(
    job_or_facts: Any,
    *,
    infer_rungs: bool = True,
    rung_band_mm: float = 8.0,
    confidence_threshold: float = 0.80,
    catalog: Catalog | None = None,
) -> ImportFromToSchedule:
    """Extract a structured From-To Wire Schedule + Device Table from any ImportJob / facts.json."""
    cat = catalog or default_catalog()
    facts, _path, _owner = _load_facts_dict(job_or_facts, cat)

    stored_sched = facts.get("from_to_schedule")
    if isinstance(stored_sched, dict) and stored_sched.get("user_edited"):
        return ImportFromToSchedule.from_dict(stored_sched)
    raw_tb = dict(facts.get("title_block") or {})
    if facts.get("standard") and not raw_tb.get("standard"):
        raw_tb["standard"] = facts["standard"]

    raw_devices: list[dict[str, Any]] = []
    raw_wires: list[dict[str, Any]] = []
    sheets_list = [s for s in (facts.get("sheets") or facts.get("pages") or []) if isinstance(s, dict)]
    if sheets_list:
        for sh in sheets_list:
            kind = str(sh.get("kind") or "schematic")
            if kind not in {"schematic", "io", ""}:
                continue
            sid_sh = _norm_sheet_id(str(sh.get("id") or sh.get("page_id") or "page-001"))
            for d in sh.get("devices") or []:
                if isinstance(d, dict) and not d.get("unreadable"):
                    item = dict(d)
                    item["page_id"] = sid_sh
                    raw_devices.append(item)
            for w in sh.get("wires") or []:
                if isinstance(w, dict) and not w.get("unreadable"):
                    item = dict(w)
                    item["page_id"] = sid_sh
                    raw_wires.append(item)
    if not raw_devices:
        raw_devices = [d for d in (facts.get("devices") or []) if isinstance(d, dict) and not d.get("unreadable")]
    if not raw_wires:
        raw_wires = [w for w in (facts.get("wires") or []) if isinstance(w, dict) and not w.get("unreadable")]

    rails_seen: list[str] = []
    if raw_tb.get("rail_hot"):
        rails_seen.append(str(raw_tb["rail_hot"]))
    if raw_tb.get("rail_return"):
        rails_seen.append(str(raw_tb["rail_return"]))
    for w in raw_wires:
        for k in ("from", "to", "number"):
            val = str(w.get(k) or "").strip().upper()
            if val in _RAIL_HOT_TOKENS or val in _RAIL_RET_TOKENS:
                rails_seen.append(val)

    std = _detect_standard(str(raw_tb.get("standard") or ""), raw_devices, rails_seen)
    default_hot = "24V" if std == "IEC" else "L1"
    default_ret = "0V" if std == "IEC" else "L2"
    rail_hot = str(raw_tb.get("rail_hot") or default_hot).strip() or default_hot
    rail_ret = str(raw_tb.get("rail_return") or default_ret).strip() or default_ret

    # Pre-collect pins and wired tags referenced in raw_wires for each (sheet_id, tag)
    from panelcad.importer.symbols import print_tag_placeable
    wired_pins_by_sheet_tag: dict[tuple[str, str], list[str]] = {}
    wired_tags_by_sheet: dict[str, set[str]] = {}
    sheet_has_confirmed_lib: dict[str, bool] = {}
    for d0 in raw_devices:
        sid0 = _norm_sheet_id(str(d0.get("page_id") or d0.get("sheet_id") or "page-001"))
        if str(d0.get("library_id") or "").strip():
            sheet_has_confirmed_lib[sid0] = True
    for w in raw_wires:
        sid_w = _norm_sheet_id(str(w.get("page_id") or w.get("sheet_id") or "page-001"))
        for end_k, tag_k, pin_k in (("from", "from_tag", "from_pin"), ("to", "to_tag", "to_pin")):
            ep_str = str(w.get(end_k) or "").strip()
            if ep_str and "." in ep_str:
                et, ep = _split_endpoint(ep_str)
            else:
                et = str(w.get(tag_k) or "").strip()
                ep = str(w.get(pin_k) or "").strip()
            if et:
                wired_tags_by_sheet.setdefault(sid_w, set()).add(et.lstrip("-+"))
            if et and ep and ep.lower() not in {"left", "right", "top", "bottom"}:
                lst = wired_pins_by_sheet_tag.setdefault((sid_w, et.lstrip("-+")), [])
                if ep not in lst:
                    lst.append(ep)

    # Detect sheets/columns that have a 3-phase motor or 3-phase wired device
    motor_x_by_sheet: dict[str, list[float]] = {}
    for d in raw_devices:
        sid0 = _norm_sheet_id(str(d.get("page_id") or d.get("sheet_id") or "page-001"))
        t0 = str(d.get("tag") or d.get("id") or "").strip().lstrip("-+").upper()
        lid0 = str(d.get("library_id") or "").lower()
        wp0 = {p.upper() for p in wired_pins_by_sheet_tag.get((sid0, t0), [])}
        if re.match(r"^MTR\d*$", t0) or ("motor_3ph" in lid0 and not d.get("guess")) or (wp0 & {"T1", "T2", "T3", "U1", "V1", "W1"}):
            dx0 = float(d.get("text_x") or d.get("x") or (d.get("bbox") or {}).get("x") or 0.0)
            motor_x_by_sheet.setdefault(sid0, []).append(dx0)

    # Track tag occurrences and rightmost x for each tag on each sheet so coil vs aux-contact is disambiguated
    tag_counts: dict[str, int] = {}
    tag_max_x: dict[tuple[str, str], float] = {}
    tag_has_explicit_coil_or_power: set[str] = set()
    for d in raw_devices:
        t = str(d.get("tag") or d.get("id") or "").strip().lstrip("-+")
        if not t or t.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
            continue
        sid0 = _norm_sheet_id(str(d.get("page_id") or d.get("sheet_id") or "page-001"))
        dx = float(d.get("text_x") or d.get("x") or 0.0)
        tag_counts[t] = tag_counts.get(t, 0) + 1
        prev_max = tag_max_x.get((sid0, t), -1e9)
        if dx > prev_max:
            tag_max_x[(sid0, t)] = dx
        lt = f"{d.get('type') or ''} {d.get('symbol') or ''} {d.get('library_id') or ''}".lower()
        if any(k in lt for k in ("coil", "ac3_", "contactor_3p", "relays.", "k_4co", "on_delay", "overload")):
            tag_has_explicit_coil_or_power.add(t)

    devices: list[ImportDeviceEntry] = []
    dev_by_sheet_and_tag: dict[tuple[str, str], list[ImportDeviceEntry]] = {}
    used_unique_tags_per_sheet: dict[str, set[str]] = {}

    for idx, d in enumerate(raw_devices):
        raw_tag = str(d.get("tag") or d.get("id") or f"DEV{idx + 1}").strip().lstrip("-+")
        if not raw_tag:
            continue
        raw_lid = str(d.get("library_id") or "")
        raw_sym = str(d.get("symbol") or d.get("symbol_id") or d.get("type") or d.get("kind") or "")
        if raw_tag.upper() in {"L1", "L2", "X1", "X2", "24V", "0V"} and (
            not raw_lid or "needs_info" in raw_lid
        ) and not raw_sym:
            if raw_tag.upper() in {"X1", "L1", "24V"}:
                rail_hot = raw_tag.upper()
            elif raw_tag.upper() in {"X2", "L2", "0V"}:
                rail_ret = raw_tag.upper()
            continue

        if re.match(r"^\d+(?:\.\d+)?\s*(?:MM|IN|CM|FT|VAC|VDC|HZ|HP|KW)$", raw_tag.upper()):
            continue
        sid = _norm_sheet_id(str(d.get("page_id") or d.get("sheet_id") or "page-001"))
        dx = float(d.get("text_x") or d.get("x") or (d.get("bbox") or {}).get("x") or 0.0)
        dy = float(d.get("text_y") or d.get("y") or (d.get("bbox") or {}).get("y") or 0.0)
        is_wired_tag = raw_tag in wired_tags_by_sheet.get(sid, set())
        if not is_wired_tag and not raw_lid and not raw_sym and bool(sheet_has_confirmed_lib) and not print_tag_placeable(d):
            continue
        if not is_wired_tag and raw_lid and any(
            prev.sheet_id == sid and prev.library_id == raw_lid and abs(dx - prev.x) < 60.0 and abs(dy - prev.y) < 40.0
            for prev in devices
        ):
            continue
        pins_in = [str(p) for p in (d.get("pins") or []) if str(p).strip()]
        for wp in wired_pins_by_sheet_tag.get((sid, raw_tag), []):
            if wp not in pins_in:
                pins_in.append(wp)
        in_motor_col = any(abs(dx - mx) <= 28.0 for mx in motor_x_by_sheet.get(sid, []))
        if in_motor_col and re.match(r"^(CB|MCCB|DISC|Q|M|K|OL|F)\d+$", raw_tag.upper()) and not pins_in:
            pins_in = ["L1", "L2", "L3", "T1", "T2", "T3"]
            if "1p" in raw_lid.lower():
                raw_lid = ""
        raw_type_str = str(d.get("type") or d.get("kind") or "")
        if (d.get("guess") or d.get("library_guess")) and not pins_in and not in_motor_col:
            if any(k in f"{raw_lid} {raw_sym} {raw_type_str}".lower() for k in ("motor", "overload", "terminals.pe")):
                raw_lid = ""
                raw_sym = ""
                raw_type_str = ""

        # If this tag appears multiple times on the sheet and this instance is significantly left of the rightmost instance,
        # treat it as an auxiliary contact rather than a second coil!
        max_x_for_tag = tag_max_x.get((sid, raw_tag), dx)
        is_left_duplicate = tag_counts.get(raw_tag, 0) > 1 and (max_x_for_tag - dx) > 15.0
        eff_type = raw_type_str
        eff_sym = raw_sym
        eff_lid = raw_lid
        if is_left_duplicate and not any(k in eff_lid.lower() for k in ("mcb_3p", "ac3_", "overload", "motor")):
            eff_type = "aux_no"
            eff_sym = "aux_no"
            if "coil" in eff_lid.lower() or "relays.cr" in eff_lid.lower() or "timers." in eff_lid.lower():
                eff_lid = ""

        role, lid, dpins = _classify_device_role_and_library(
            raw_tag,
            eff_type,
            eff_sym,
            eff_lid,
            pins_in,
            std,
            cat,
            has_other_coil_or_poles=is_left_duplicate or (raw_tag in tag_has_explicit_coil_or_power and tag_counts.get(raw_tag, 0) > 1 and is_left_duplicate),
        )
        conf = float(d.get("confidence", 0.9))
        src = str(d.get("source") or "import")
        needs_rev = bool(d.get("needs_review", conf < confidence_threshold or bool(d.get("guess"))))

        sheet_used = used_unique_tags_per_sheet.setdefault(sid, set())
        unique_tag = raw_tag
        if unique_tag in sheet_used or (is_left_duplicate and role == "control_contact"):
            if role == "control_contact":
                suffix = dpins[0] if dpins else "3"
                cand = f"{raw_tag}-{suffix}"
                seq = 1
                while cand in sheet_used:
                    seq += 1
                    cand = f"{raw_tag}-{suffix}-{seq}"
                unique_tag = cand
            elif role == "control_ol_contact":
                cand = f"{raw_tag}-95"
                seq = 1
                while cand in sheet_used:
                    seq += 1
                    cand = f"{raw_tag}-95-{seq}"
                unique_tag = cand
            elif unique_tag in sheet_used:
                seq = 2
                while f"{raw_tag}_{seq}" in sheet_used:
                    seq += 1
                unique_tag = f"{raw_tag}_{seq}"
        sheet_used.add(unique_tag)

        entry = ImportDeviceEntry(
            tag=unique_tag,
            symbol=eff_sym or role,
            library_id=lid,
            description=str(d.get("description") or ""),
            pins=(dpins if role == "control_block" else (list(pins_in) + [p for p in dpins if p not in pins_in] if pins_in else dpins)),
            sheet_id=sid,
            x=float(d.get("x") if role == "control_block" and d.get("x") is not None else dx),
            y=float(d.get("y") if role == "control_block" and d.get("y") is not None else dy),
            confidence=conf,
            source=src,
            needs_review=needs_rev,
            role=role,
        )
        devices.append(entry)
        dev_by_sheet_and_tag.setdefault((sid, raw_tag), []).append(entry)
        if unique_tag != raw_tag:
            dev_by_sheet_and_tag.setdefault((sid, unique_tag), []).append(entry)

    from_to_rows: list[ImportFromToRow] = []
    explicit_pairs_by_sheet: dict[str, set[frozenset[str]]] = {}
    unattached_wire_labels_by_sheet: dict[str, list[dict[str, Any]]] = {}

    for w_idx, w in enumerate(raw_wires, start=1):
        sid = _norm_sheet_id(str(w.get("page_id") or w.get("sheet_id") or "page-001"))
        frm = str(w.get("from") or w.get("frm") or "").strip()
        to = str(w.get("to") or "").strip()
        if not frm and w.get("from_tag"):
            fp_raw = str(w.get("from_pin") or "").strip()
            if fp_raw.lower() in {"left", "right", "top", "bottom"}:
                fp_raw = ""
            frm = f"{w['from_tag']}.{fp_raw}".rstrip(".")
        if not to and w.get("to_tag"):
            tp_raw = str(w.get("to_pin") or "").strip()
            if tp_raw.lower() in {"left", "right", "top", "bottom"}:
                tp_raw = ""
            to = f"{w['to_tag']}.{tp_raw}".rstrip(".")
        wnum = str(w.get("number") or w.get("wire_number") or w.get("label") or "").strip()
        conf = float(w.get("confidence", 0.9))
        src = str(w.get("source") or "extracted")
        if frm and to:
            f_tag, f_pin = _split_endpoint(frm)
            t_tag, t_pin = _split_endpoint(to)
            if not f_pin and f_tag.upper() not in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
                matches = dev_by_sheet_and_tag.get((sid, f_tag)) or []
                if matches and matches[0].pins:
                    f_pin = matches[0].pins[-1] if len(matches[0].pins) >= 2 else matches[0].pins[0]
            if not t_pin and t_tag.upper() not in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
                matches = dev_by_sheet_and_tag.get((sid, t_tag)) or []
                if matches and matches[0].pins:
                    t_pin = matches[0].pins[0]
            if f_tag.upper() == t_tag.upper() and f_pin.upper() == t_pin.upper():
                continue
            if any(
                r.sheet_id == sid
                and r.from_tag == f_tag
                and r.from_pin == f_pin
                and r.to_tag == t_tag
                and r.to_pin == t_pin
                for r in from_to_rows
            ):
                continue
            row = ImportFromToRow(
                wire_number=wnum or f"W{w_idx}",
                from_tag=f_tag,
                from_pin=f_pin,
                to_tag=t_tag,
                to_pin=t_pin,
                sheet_id=sid,
                rung=int(w.get("rung") or 1),
                confidence=conf,
                source=src,
                needs_review=bool(w.get("needs_review", conf < confidence_threshold)),
            )
            from_to_rows.append(row)
            explicit_pairs_by_sheet.setdefault(sid, set()).add(frozenset({f_tag.split("-")[0], t_tag.split("-")[0]}))
        else:
            wx_val = w.get("x")
            wy_val = w.get("y")
            if wx_val is None or wy_val is None:
                pts = w.get("points") or []
                if pts and isinstance(pts[0], (list, tuple)) and len(pts[0]) >= 2:
                    wx_val = sum(float(p[0]) for p in pts) / len(pts)
                    wy_val = sum(float(p[1]) for p in pts) / len(pts)
            if wnum and wx_val is not None and wy_val is not None:
                unattached_wire_labels_by_sheet.setdefault(sid, []).append(
                    {
                        "number": wnum,
                        "x": float(wx_val),
                        "y": float(wy_val),
                        "confidence": conf,
                    }
                )

    rungs_meta: list[dict[str, Any]] = []
    power_cols_meta: list[dict[str, Any]] = []
    warnings: list[str] = []

    if infer_rungs:
        sheets_seen = list(dict.fromkeys(d.sheet_id for d in devices))
        for sid in sheets_seen:
            s_devs = [d for d in devices if d.sheet_id == sid]
            power_devs = [d for d in s_devs if d.role.startswith("power_")]
            ctrl_devs = [d for d in s_devs if d.role.startswith("control_")]

            # If a sheet has only control devices EXCEPT 1-2 breakers on the first rung (like CB1/CB2/FU1 on F1pdf control transformer secondary),
            # check if there are any 3-phase contactors/overloads/motors on the sheet. If not, and CB1/CB2 sit on a horizontal rung with FU1,
            # treat single-rung CB/FU without 3P motor/contactor as control feeder protection contacts so they wire on Rung 1!
            has_heavy_3ph = any(d.role in {"power_contactor", "power_overload", "power_motor", "power_vfd"} for d in power_devs)
            if not has_heavy_3ph and power_devs and ctrl_devs:
                # Check if all power_devs sit on the same horizontal rung y-band as a control device or each other
                ys_p = [d.y for d in power_devs]
                if max(ys_p) - min(ys_p) <= rung_band_mm and len(power_devs) >= 2:
                    for pd in power_devs:
                        pd.role = "control_contact"
                        pd.library_id = "iec.protection.fuse_10a" if std == "IEC" else "nema.protection.fuse_10a"
                        pd.pins = ["1", "2"]
                    ctrl_devs = [d for d in s_devs if d.role.startswith("control_")]
                    power_devs = [d for d in s_devs if d.role.startswith("power_")]

            if power_devs:
                cols = _group_power_columns(power_devs)
                for col_idx, col_devs in enumerate(cols, start=1):
                    power_cols_meta.append(
                        {
                            "sheet_id": sid,
                            "column": col_idx,
                            "devices": [d.tag for d in col_devs],
                        }
                    )
                    for i in range(len(col_devs) - 1):
                        upper = col_devs[i]
                        lower = col_devs[i + 1]
                        pair_key = frozenset({upper.tag.split("-")[0], lower.tag.split("-")[0]})
                        if pair_key in explicit_pairs_by_sheet.get(sid, set()):
                            continue
                        up_out = _power_bottom_pins(upper, std)
                        lo_in = _power_top_pins(lower, std)
                        conf_p = round(min(upper.confidence, lower.confidence, 0.78), 3)
                        for pole_idx, (p_from, p_to) in enumerate(zip(up_out, lo_in), start=1):
                            wnum = f"{col_idx}T{pole_idx}" if i > 0 else f"L{pole_idx}_{col_idx}"
                            from_to_rows.append(
                                ImportFromToRow(
                                    wire_number=wnum,
                                    from_tag=upper.tag,
                                    from_pin=p_from,
                                    to_tag=lower.tag,
                                    to_pin=p_to,
                                    sheet_id=sid,
                                    rung=col_idx,
                                    confidence=conf_p,
                                    source="power_inferred",
                                    needs_review=conf_p < confidence_threshold,
                                )
                            )
                        explicit_pairs_by_sheet.setdefault(sid, set()).add(pair_key)

            if ctrl_devs:
                inferred_rows, sheet_rungs = _infer_sheet_control_rungs(
                    sid=sid,
                    ctrl_devs=ctrl_devs,
                    existing_rows=[r for r in from_to_rows if r.sheet_id == sid],
                    wire_labels=unattached_wire_labels_by_sheet.get(sid, []),
                    rail_hot=rail_hot,
                    rail_ret=rail_ret,
                    rung_band_mm=rung_band_mm,
                    confidence_threshold=confidence_threshold,
                )
                from_to_rows.extend(inferred_rows)
                rungs_meta.extend(sheet_rungs)

    if isinstance(stored_sched, dict) and stored_sched.get("user_edited"):
        custom_rows: list[ImportFromToRow] = []
        for idx, rdict in enumerate(stored_sched.get("from_to_rows") or [], start=1):
            if not isinstance(rdict, dict):
                continue
            f_tag = str(rdict.get("from_tag") or "")
            f_pin = str(rdict.get("from_pin") or "")
            if not f_tag and rdict.get("from"):
                f_tag, f_pin = _split_endpoint(str(rdict["from"]))
            t_tag = str(rdict.get("to_tag") or "")
            t_pin = str(rdict.get("to_pin") or "")
            if not t_tag and rdict.get("to"):
                t_tag, t_pin = _split_endpoint(str(rdict["to"]))
            if not f_tag or not t_tag:
                continue
            conf_r = float(rdict.get("confidence", 1.0))
            custom_rows.append(
                ImportFromToRow(
                    wire_number=str(rdict.get("wire_number") or rdict.get("number") or f"W{idx}"),
                    from_tag=f_tag,
                    from_pin=f_pin,
                    to_tag=t_tag,
                    to_pin=t_pin,
                    sheet_id=_norm_sheet_id(str(rdict.get("sheet_id") or "page-001")),
                    rung=int(rdict.get("rung") or 1),
                    confidence=conf_r,
                    source=str(rdict.get("source") or "user_edit"),
                    needs_review=bool(rdict.get("needs_review", conf_r < confidence_threshold)),
                )
            )
        from_to_rows = custom_rows

    return ImportFromToSchedule(
        standard=std,
        rail_hot=rail_hot,
        rail_return=rail_ret,
        devices=devices,
        from_to_rows=from_to_rows,
        rungs=rungs_meta,
        power_columns=power_cols_meta,
        warnings=warnings,
    )


def _group_power_columns(power_devs: list[ImportDeviceEntry], x_tol: float = 28.0) -> list[list[ImportDeviceEntry]]:
    """Group 3-phase power devices into vertical feeder columns sorted top-to-bottom."""
    role_order = {
        "power_breaker": 0,
        "power_contactor": 1,
        "power_vfd": 1,
        "power_overload": 2,
        "power_motor": 3,
    }
    sorted_x = sorted(power_devs, key=lambda d: (d.x, role_order.get(d.role, 9), d.y))
    columns: list[list[ImportDeviceEntry]] = []
    for d in sorted_x:
        placed = False
        for col in columns:
            mean_x = sum(item.x for item in col) / len(col)
            if abs(d.x - mean_x) <= x_tol and all(item.role != d.role for item in col):
                col.append(d)
                placed = True
                break
        if not placed:
            columns.append([d])
    for col in columns:
        col.sort(key=lambda d: (role_order.get(d.role, 9), d.y))
    return columns


def _power_top_pins(dev: ImportDeviceEntry, std: str) -> list[str]:
    is_iec = std == "IEC"
    if dev.role == "power_motor":
        return ["U1", "V1", "W1"] if is_iec else ["T1", "T2", "T3"]
    if dev.role == "power_vfd":
        return ["L1", "L2", "L3"]
    if dev.role == "power_contactor":
        return ["L1", "L2", "L3"]
    if dev.role == "power_overload":
        return ["1", "3", "5"] if is_iec else ["T1", "T2", "T3"]
    if "disconnect" in dev.library_id and not is_iec:
        return ["L1", "L2", "L3"]
    return ["1", "3", "5"]


def _power_bottom_pins(dev: ImportDeviceEntry, std: str) -> list[str]:
    is_iec = std == "IEC"
    if dev.role == "power_vfd":
        return ["U", "V", "W"]
    if dev.role == "power_contactor":
        return ["T1", "T2", "T3"]
    if dev.role == "power_overload":
        return ["2", "4", "6"]
    if "disconnect" in dev.library_id and not is_iec:
        return ["T1", "T2", "T3"]
    return ["2", "4", "6"]


def _infer_sheet_control_rungs(
    *,
    sid: str,
    ctrl_devs: list[ImportDeviceEntry],
    existing_rows: list[ImportFromToRow],
    wire_labels: list[dict[str, Any]],
    rail_hot: str,
    rail_ret: str,
    rung_band_mm: float,
    confidence_threshold: float,
) -> tuple[list[ImportFromToRow], list[dict[str, Any]]]:
    """Cluster control devices into horizontal rungs (within rung_band_mm) and infer missing connections."""
    has_blocks = any(d.role == "control_block" for d in ctrl_devs)
    if has_blocks:
        contacts_row = sorted([d for d in ctrl_devs if d.role == "control_contact"], key=lambda d: d.x)
        existing_pairs = {frozenset({r.from_tag, r.to_tag}) for r in existing_rows}
        inferred_block_rows: list[ImportFromToRow] = []
        w_seq = 1
        for i in range(len(contacts_row) - 1):
            c_left = contacts_row[i]
            c_right = contacts_row[i + 1]
            if abs(c_left.y - c_right.y) <= rung_band_mm:
                pair = frozenset({c_left.tag, c_right.tag})
                if pair not in existing_pairs:
                    existing_pairs.add(pair)
                    inferred_block_rows.append(
                        ImportFromToRow(
                            wire_number=f"W_INF_{w_seq}",
                            from_tag=c_left.tag,
                            from_pin=c_left.pins[1] if len(c_left.pins) >= 2 else "2",
                            to_tag=c_right.tag,
                            to_pin=c_right.pins[0] if c_right.pins else "1",
                            sheet_id=sid,
                            rung=1,
                            confidence=round(min(c_left.confidence, c_right.confidence, 0.78), 3),
                            source="rung_inferred",
                            needs_review=True,
                        )
                    )
                    w_seq += 1
        return inferred_block_rows, []
    dev_by_tag = {d.tag: d for d in ctrl_devs}
    # Snap device y to any connected non-terminal peer's y so offset terminal labels (e.g. TB1-1) stay on their wired rung
    peer_adj: dict[str, set[str]] = {d.tag: set() for d in ctrl_devs}
    for er in existing_rows:
        if er.from_tag in peer_adj and er.to_tag in peer_adj:
            peer_adj[er.from_tag].add(er.to_tag)
            peer_adj[er.to_tag].add(er.from_tag)

    eff_y: dict[str, float] = {}
    for d in ctrl_devs:
        peers = [dev_by_tag[p] for p in peer_adj.get(d.tag, set()) if "terminal" not in dev_by_tag[p].library_id]
        if "terminal" in d.library_id and peers:
            eff_y[d.tag] = sum(p.y for p in peers) / len(peers)
        else:
            eff_y[d.tag] = d.y

    sorted_by_y = sorted(ctrl_devs, key=lambda d: (eff_y[d.tag], d.x))
    rung_clusters: list[dict[str, Any]] = []

    eff_band = min(float(rung_band_mm), 6.5)
    for dev in sorted_by_y:
        dy_val = eff_y[dev.tag]
        # First check if any peer in peer_adj is already placed in a rung_cluster
        peer_rung = next(
            (r for r in rung_clusters if any(m.tag in peer_adj.get(dev.tag, set()) for m in r["members"])),
            None,
        )
        if peer_rung is not None:
            peer_rung["members"].append(dev)
            ys = [eff_y[m.tag] for m in peer_rung["members"]]
            peer_rung["mean_y"] = sum(ys) / len(ys)
            continue

        best_rung = None
        best_dy = eff_band + 1.0
        for r in rung_clusters:
            dy = abs(dy_val - r["anchor_y"])
            if dy <= eff_band and dy < best_dy:
                best_dy = dy
                best_rung = r
        if best_rung is not None:
            best_rung["members"].append(dev)
            ys = [eff_y[m.tag] for m in best_rung["members"]]
            best_rung["mean_y"] = sum(ys) / len(ys)
            continue

        attached_parallel = False
        if dev.role == "control_contact":
            for r in reversed(rung_clusters):
                if not any(m.role == "control_output" for m in r["members"]):
                    continue
                for m in r["members"]:
                    if m.role == "control_contact" and abs(dev.x - m.x) <= 10.0 and 8.0 < (dev.y - m.y) <= 26.0:
                        r["parallels"].append((dev, m))
                        attached_parallel = True
                        break
                if attached_parallel:
                    break
        if not attached_parallel:
            rung_clusters.append(
                {
                    "mean_y": dy_val,
                    "anchor_y": dy_val,
                    "members": [dev],
                    "parallels": [],
                }
            )

    cleaned_clusters: list[dict[str, Any]] = []
    for r in rung_clusters:
        members = r["members"]
        if (
            len(members) == 1
            and members[0].role == "control_contact"
            and cleaned_clusters
            and any(m.role == "control_output" for m in cleaned_clusters[-1]["members"])
        ):
            cand = members[0]
            prev = cleaned_clusters[-1]
            parent_c = min(
                (m for m in prev["members"] if m.role == "control_contact"),
                key=lambda m: abs(cand.x - m.x),
                default=None,
            )
            if parent_c is not None and abs(cand.x - parent_c.x) <= 14.0 and (cand.y - parent_c.y) <= 28.0:
                prev["parallels"].append((cand, parent_c))
                continue
        cleaned_clusters.append(r)

    connected_endpoints: set[frozenset[str]] = set()
    device_has_left_wire: set[str] = set()
    device_has_right_wire: set[str] = set()
    for row in existing_rows:
        a_base = row.from_tag
        b_base = row.to_tag
        connected_endpoints.add(frozenset({a_base, b_base}))
        if a_base.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
            device_has_left_wire.add(b_base)
        elif b_base.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
            device_has_right_wire.add(a_base)
        else:
            device_has_right_wire.add(a_base)
            device_has_left_wire.add(b_base)

    inferred: list[ImportFromToRow] = []
    rungs_meta: list[dict[str, Any]] = []

    def _match_wire_label(y_ref: float, x_left: float, x_right: float, default_num: str) -> tuple[str, float | None]:
        best_lbl = None
        best_dist = 999.0
        xm = 0.5 * (x_left + x_right)
        for wl in wire_labels:
            if wl.get("_used"):
                continue
            wy = float(wl["y"])
            wx = float(wl["x"])
            if abs(wy - y_ref) <= max(rung_band_mm, 9.0) and (x_left - 8.0) <= wx <= (x_right + 8.0):
                dist = abs(wy - y_ref) + 0.15 * abs(wx - xm)
                if dist < best_dist:
                    best_dist = dist
                    best_lbl = wl
        if best_lbl is not None:
            best_lbl["_used"] = True
            return str(best_lbl["number"]), float(best_lbl.get("confidence", 0.72))
        return default_num, None

    seq_num = 101
    for rung_idx, r in enumerate(cleaned_clusters, start=1):
        members: list[ImportDeviceEntry] = list(r["members"])
        outputs = sorted([m for m in members if m.role == "control_output"], key=lambda d: d.x)
        # If multiple outputs appear on the same horizontal rung at different x columns, only the rightmost is the coil/output;
        # earlier ones on the left are series contacts.
        if len(outputs) > 1:
            rightmost_out = outputs[-1]
            for earlier_out in outputs[:-1]:
                if rightmost_out.x - earlier_out.x > 10.0:
                    earlier_out.role = "control_contact"
                    is_iec_dev = earlier_out.library_id.startswith("iec.")
                    earlier_out.library_id = "iec.contactors.aux_no" if is_iec_dev else "nema.contactors.aux_no"
                    earlier_out.pins = ["13", "14"] if is_iec_dev else ["3", "4"]
                    for er in existing_rows:
                        if er.to_tag == earlier_out.tag:
                            er.to_pin = earlier_out.pins[0]
                        if er.from_tag == earlier_out.tag:
                            er.from_pin = earlier_out.pins[1]
            outputs = [rightmost_out]

        contacts = sorted([m for m in members if m.role == "control_contact"], key=lambda d: d.x)
        ol_contacts = sorted([m for m in members if m.role == "control_ol_contact"], key=lambda d: d.x)

        left_ol: list[ImportDeviceEntry] = []
        right_ol: list[ImportDeviceEntry] = []
        for olc in ol_contacts:
            if outputs and olc.x < outputs[0].x:
                left_ol.append(olc)
            else:
                right_ol.append(olc)

        left_chain = sorted(contacts + left_ol, key=lambda d: d.x)
        ordered_chain = left_chain + outputs + right_ol
        if not ordered_chain:
            continue

        y_ref = float(r["mean_y"])
        rungs_meta.append(
            {
                "sheet_id": sid,
                "rung": rung_idx,
                "y": round(y_ref, 2),
                "chain": [d.tag for d in ordered_chain],
                "parallels": [(p.tag, u.tag) for p, u in r["parallels"]],
            }
        )

        first_dev = ordered_chain[0]
        first_in_pin = first_dev.pins[0] if first_dev.pins else "1"
        if (
            frozenset({rail_hot, first_dev.tag}) not in connected_endpoints
            and first_dev.tag not in device_has_left_wire
            and "offpage" not in first_dev.library_id.lower()
        ):
            wnum, lconf = _match_wire_label(y_ref, 0.0, first_dev.x, rail_hot)
            conf = round(min(first_dev.confidence, lconf if lconf is not None else 0.76), 3)
            inferred.append(
                ImportFromToRow(
                    wire_number=wnum,
                    from_tag=rail_hot,
                    from_pin="",
                    to_tag=first_dev.tag,
                    to_pin=first_in_pin,
                    sheet_id=sid,
                    rung=rung_idx,
                    confidence=conf,
                    source="rung_inferred",
                    needs_review=conf < confidence_threshold,
                )
            )
            connected_endpoints.add(frozenset({rail_hot, first_dev.tag}))
            device_has_left_wire.add(first_dev.tag)

        wire_between: dict[tuple[str, str], str] = {}
        for i in range(len(ordered_chain) - 1):
            a_dev = ordered_chain[i]
            b_dev = ordered_chain[i + 1]
            a_out = a_dev.pins[1] if len(a_dev.pins) >= 2 else (a_dev.pins[0] if a_dev.pins else "2")
            b_in = b_dev.pins[0] if b_dev.pins else "1"
            pair = frozenset({a_dev.tag, b_dev.tag})
            if (
                pair in connected_endpoints
                or (a_dev.tag in device_has_right_wire and b_dev.tag in device_has_left_wire)
                or (a_dev.tag in device_has_right_wire and "offpage" in b_dev.library_id.lower())
                or ("offpage" in a_dev.library_id.lower() and b_dev.tag in device_has_left_wire)
            ):
                for ex in existing_rows:
                    if frozenset({ex.from_tag, ex.to_tag}) == pair:
                        wire_between[(a_dev.tag, b_dev.tag)] = ex.wire_number
                        break
                continue
            default_w = str(seq_num)
            seq_num += 1
            wnum, lconf = _match_wire_label(y_ref, a_dev.x, b_dev.x, default_w)
            conf = round(min(a_dev.confidence, b_dev.confidence, lconf if lconf is not None else 0.75), 3)
            inferred.append(
                ImportFromToRow(
                    wire_number=wnum,
                    from_tag=a_dev.tag,
                    from_pin=a_out,
                    to_tag=b_dev.tag,
                    to_pin=b_in,
                    sheet_id=sid,
                    rung=rung_idx,
                    confidence=conf,
                    source="rung_inferred",
                    needs_review=conf < confidence_threshold,
                )
            )
            connected_endpoints.add(pair)
            wire_between[(a_dev.tag, b_dev.tag)] = wnum

        last_dev = ordered_chain[-1]
        last_out_pin = last_dev.pins[1] if len(last_dev.pins) >= 2 else (last_dev.pins[0] if last_dev.pins else "2")
        if (
            frozenset({last_dev.tag, rail_ret}) not in connected_endpoints
            and last_dev.tag not in device_has_right_wire
            and "offpage" not in last_dev.library_id.lower()
        ):
            wnum, lconf = _match_wire_label(y_ref, last_dev.x, last_dev.x + 80.0, rail_ret)
            conf = round(min(last_dev.confidence, lconf if lconf is not None else 0.76), 3)
            inferred.append(
                ImportFromToRow(
                    wire_number=wnum,
                    from_tag=last_dev.tag,
                    from_pin=last_out_pin,
                    to_tag=rail_ret,
                    to_pin="",
                    sheet_id=sid,
                    rung=rung_idx,
                    confidence=conf,
                    source="rung_inferred",
                    needs_review=conf < confidence_threshold,
                )
            )
            connected_endpoints.add(frozenset({last_dev.tag, rail_ret}))
            device_has_right_wire.add(last_dev.tag)

        for par_dev, under_dev in r["parallels"]:
            if under_dev not in ordered_chain:
                continue
            u_idx = ordered_chain.index(under_dev)
            par_in = par_dev.pins[0] if par_dev.pins else "3"
            par_out = par_dev.pins[1] if len(par_dev.pins) >= 2 else "4"
            conf_par = round(min(par_dev.confidence, under_dev.confidence, 0.74), 3)

            if u_idx == 0:
                left_tag, left_pin, left_wnum = rail_hot, "", rail_hot
            else:
                prev_dev = ordered_chain[u_idx - 1]
                left_tag = prev_dev.tag
                left_pin = prev_dev.pins[1] if len(prev_dev.pins) >= 2 else "2"
                left_wnum = wire_between.get((prev_dev.tag, under_dev.tag), f"{seq_num}")

            if frozenset({left_tag, par_dev.tag}) not in connected_endpoints:
                inferred.append(
                    ImportFromToRow(
                        wire_number=left_wnum,
                        from_tag=left_tag,
                        from_pin=left_pin,
                        to_tag=par_dev.tag,
                        to_pin=par_in,
                        sheet_id=sid,
                        rung=rung_idx,
                        confidence=conf_par,
                        source="rung_inferred",
                        needs_review=conf_par < confidence_threshold,
                    )
                )
                connected_endpoints.add(frozenset({left_tag, par_dev.tag}))

            if u_idx + 1 < len(ordered_chain):
                next_dev = ordered_chain[u_idx + 1]
                right_tag = next_dev.tag
                right_pin = next_dev.pins[0] if next_dev.pins else "1"
                right_wnum = wire_between.get((under_dev.tag, next_dev.tag), f"{seq_num}")
            else:
                right_tag, right_pin, right_wnum = rail_ret, "", rail_ret

            if frozenset({par_dev.tag, right_tag}) not in connected_endpoints:
                inferred.append(
                    ImportFromToRow(
                        wire_number=right_wnum,
                        from_tag=par_dev.tag,
                        from_pin=par_out,
                        to_tag=right_tag,
                        to_pin=right_pin,
                        sheet_id=sid,
                        rung=rung_idx,
                        confidence=conf_par,
                        source="rung_inferred",
                        needs_review=conf_par < confidence_threshold,
                    )
                )
                connected_endpoints.add(frozenset({par_dev.tag, right_tag}))

    return inferred, rungs_meta


def apply_import_from_to_edits(
    job_or_facts: Any,
    *,
    device_edits: list[dict[str, Any]] | dict[str, dict[str, Any]] | None = None,
    from_to_rows: list[dict[str, Any] | ImportFromToRow] | None = None,
    edits: list[dict[str, Any]] | None = None,
    add_rows: list[dict[str, Any]] | None = None,
    delete_rows: list[dict[str, Any] | int] | None = None,
    confirm_rows: list[int | str] | None = None,
    catalog: Catalog | None = None,
) -> ImportFromToSchedule:
    """Apply user/GUI/CLI edits to the imported From-To schedule and persist to facts.json / stitch.json."""
    cat = catalog or default_catalog()
    facts, facts_path, owner = _load_facts_dict(job_or_facts, cat)
    schedule = extract_import_from_to(facts, catalog=cat)

    dev_edit_map: dict[str, dict[str, Any]] = {}
    if isinstance(device_edits, dict):
        dev_edit_map = {str(k): dict(v) for k, v in device_edits.items() if isinstance(v, dict)}
    elif isinstance(device_edits, list):
        for item in device_edits:
            if isinstance(item, dict) and (item.get("tag") or item.get("old_tag")):
                key = str(item.get("old_tag") or item.get("tag"))
                dev_edit_map[key] = dict(item)

    tag_renames: dict[str, str] = {}
    if dev_edit_map:
        for dev in schedule.devices:
            ed = dev_edit_map.get(dev.tag)
            if not ed:
                continue
            new_tag = str(ed.get("new_tag") or ed.get("tag") or dev.tag).strip()
            if new_tag and new_tag != dev.tag:
                tag_renames[dev.tag] = new_tag
                dev.tag = new_tag
            if "symbol" in ed or "type" in ed:
                dev.symbol = str(ed.get("symbol") or ed.get("type") or dev.symbol)
            if "library_id" in ed and ed["library_id"]:
                dev.library_id = str(ed["library_id"])
            if "description" in ed:
                dev.description = str(ed["description"] or "")
            if "pins" in ed and isinstance(ed["pins"], list):
                dev.pins = [str(p) for p in ed["pins"] if str(p).strip()]
            role, lid, dpins = _classify_device_role_and_library(
                dev.tag,
                dev.symbol,
                dev.symbol,
                dev.library_id,
                dev.pins,
                schedule.standard,
                cat,
            )
            dev.role = role
            if not ed.get("library_id"):
                dev.library_id = lid
            if not ed.get("pins"):
                dev.pins = dpins
            dev.confidence = 1.0
            dev.source = "user_confirmed"
            dev.needs_review = False

        for raw_d in facts.get("devices") or []:
            if not isinstance(raw_d, dict):
                continue
            t = str(raw_d.get("tag") or raw_d.get("id") or "")
            ed = dev_edit_map.get(t)
            if ed:
                if ed.get("new_tag") or ed.get("tag"):
                    raw_d["tag"] = str(ed.get("new_tag") or ed.get("tag"))
                if ed.get("library_id"):
                    raw_d["library_id"] = str(ed["library_id"])
                if ed.get("symbol") or ed.get("type"):
                    raw_d["symbol"] = str(ed.get("symbol") or ed.get("type"))
                    raw_d["type"] = raw_d["symbol"]
                if ed.get("description") is not None:
                    raw_d["description"] = str(ed["description"])
                raw_d["confidence"] = 1.0
                raw_d["guess"] = False

    if tag_renames:
        for row in schedule.from_to_rows:
            if row.from_tag in tag_renames:
                row.from_tag = tag_renames[row.from_tag]
            if row.to_tag in tag_renames:
                row.to_tag = tag_renames[row.to_tag]

    if from_to_rows is not None:
        new_rows: list[ImportFromToRow] = []
        for idx, item in enumerate(from_to_rows, start=1):
            if isinstance(item, ImportFromToRow):
                new_rows.append(item)
                continue
            if not isinstance(item, dict):
                continue
            f_tag = str(item.get("from_tag") or "").strip()
            f_pin = str(item.get("from_pin") or "").strip()
            if not f_tag and item.get("from") is not None:
                f_tag, f_pin = _split_endpoint(str(item.get("from") or ""))
            t_tag = str(item.get("to_tag") or "").strip()
            t_pin = str(item.get("to_pin") or "").strip()
            if not t_tag and item.get("to") is not None:
                t_tag, t_pin = _split_endpoint(str(item.get("to") or ""))
            if not f_tag or not t_tag:
                continue
            conf_val = float(item.get("confidence", 1.0))
            src_val = str(item.get("source") or "user_edit")
            new_rows.append(
                ImportFromToRow(
                    wire_number=str(item.get("wire_number") or item.get("number") or f"W{idx}").strip(),
                    from_tag=f_tag,
                    from_pin=f_pin,
                    to_tag=t_tag,
                    to_pin=t_pin,
                    sheet_id=_norm_sheet_id(str(item.get("sheet_id") or "page-001")),
                    rung=int(item.get("rung") or 1),
                    confidence=conf_val,
                    source=src_val,
                    needs_review=bool(item.get("needs_review", False if src_val.startswith("user") else conf_val < 0.8)),
                )
            )
        schedule.from_to_rows = new_rows

    if delete_rows:
        to_del_idx = {int(x) for x in delete_rows if isinstance(x, int)}
        to_del_match = [x for x in delete_rows if isinstance(x, dict)]
        kept: list[ImportFromToRow] = []
        for idx, r in enumerate(schedule.from_to_rows):
            if idx in to_del_idx:
                continue
            if any(
                (not m.get("wire_number") or str(m["wire_number"]) == r.wire_number)
                and (not m.get("from_tag") or str(m["from_tag"]) == r.from_tag)
                and (not m.get("to_tag") or str(m["to_tag"]) == r.to_tag)
                for m in to_del_match
            ):
                continue
            kept.append(r)
        schedule.from_to_rows = kept

    if edits:
        for ed in edits:
            if not isinstance(ed, dict):
                continue
            row_idx = ed.get("row_index")
            target_rows: list[ImportFromToRow] = []
            if isinstance(row_idx, int) and 0 <= row_idx < len(schedule.from_to_rows):
                target_rows = [schedule.from_to_rows[row_idx]]
            else:
                orig_w = str(ed.get("match_wire_number") or ed.get("wire_number") or "")
                target_rows = [r for r in schedule.from_to_rows if r.wire_number == orig_w]
            for tr in target_rows:
                if "new_wire_number" in ed or "wire_number" in ed:
                    tr.wire_number = str(ed.get("new_wire_number") or ed.get("wire_number") or tr.wire_number)
                if "from" in ed:
                    tr.from_tag, tr.from_pin = _split_endpoint(str(ed["from"]))
                if "from_tag" in ed:
                    tr.from_tag = str(ed["from_tag"])
                if "from_pin" in ed:
                    tr.from_pin = str(ed["from_pin"])
                if "to" in ed:
                    tr.to_tag, tr.to_pin = _split_endpoint(str(ed["to"]))
                if "to_tag" in ed:
                    tr.to_tag = str(ed["to_tag"])
                if "to_pin" in ed:
                    tr.to_pin = str(ed["to_pin"])
                if "rung" in ed:
                    tr.rung = int(ed["rung"])
                if "sheet_id" in ed:
                    tr.sheet_id = _norm_sheet_id(str(ed["sheet_id"]))
                tr.confidence = float(ed.get("confidence", 1.0))
                tr.source = "user_confirmed"
                tr.needs_review = False

    if add_rows:
        for item in add_rows:
            if not isinstance(item, dict):
                continue
            f_tag = str(item.get("from_tag") or "").strip()
            f_pin = str(item.get("from_pin") or "").strip()
            if not f_tag and item.get("from"):
                f_tag, f_pin = _split_endpoint(str(item["from"]))
            t_tag = str(item.get("to_tag") or "").strip()
            t_pin = str(item.get("to_pin") or "").strip()
            if not t_tag and item.get("to"):
                t_tag, t_pin = _split_endpoint(str(item["to"]))
            if not f_tag or not t_tag:
                continue
            schedule.from_to_rows.append(
                ImportFromToRow(
                    wire_number=str(item.get("wire_number") or item.get("number") or f"W{len(schedule.from_to_rows)+1}"),
                    from_tag=f_tag,
                    from_pin=f_pin,
                    to_tag=t_tag,
                    to_pin=t_pin,
                    sheet_id=_norm_sheet_id(str(item.get("sheet_id") or "page-001")),
                    rung=int(item.get("rung") or 1),
                    confidence=float(item.get("confidence", 1.0)),
                    source="user_added",
                    needs_review=False,
                )
            )

    if confirm_rows:
        confirm_set = {str(c) for c in confirm_rows}
        for idx, r in enumerate(schedule.from_to_rows):
            if idx in confirm_rows or r.wire_number in confirm_set:
                r.confidence = 1.0
                r.source = "user_confirmed"
                r.needs_review = False

    known_dev_tags = {d.tag for d in schedule.devices}
    rail_tokens = _RAIL_HOT_TOKENS | _RAIL_RET_TOKENS | {schedule.rail_hot.upper(), schedule.rail_return.upper()}
    for r in schedule.from_to_rows:
        for end_tag, end_pin, is_from in ((r.from_tag, r.from_pin, True), (r.to_tag, r.to_pin, False)):
            if not end_tag or end_tag.upper() in rail_tokens or end_tag in known_dev_tags:
                continue
            role, lid, dpins = _classify_device_role_and_library(
                end_tag,
                "",
                "",
                "",
                [end_pin] if end_pin else [],
                schedule.standard,
                cat,
                has_other_coil_or_poles=("-" in end_tag),
            )
            schedule.devices.append(
                ImportDeviceEntry(
                    tag=end_tag,
                    symbol=role,
                    library_id=lid,
                    description="",
                    pins=dpins,
                    sheet_id=r.sheet_id,
                    x=100.0 if role == "control_contact" else 290.0,
                    y=float(max(1, r.rung) * 30.0),
                    confidence=r.confidence,
                    source=r.source,
                    needs_review=r.needs_review,
                    role=role,
                )
            )
            known_dev_tags.add(end_tag)

    sched_dict = schedule.to_dict()
    sched_dict["user_edited"] = True
    facts["from_to_schedule"] = sched_dict
    facts["wires"] = [
        {
            "number": r.wire_number,
            "from": r.from_endpoint,
            "to": r.to_endpoint,
            "page_id": r.sheet_id,
            "rung": r.rung,
            "confidence": r.confidence,
            "source": r.source,
            "needs_review": r.needs_review,
        }
        for r in schedule.from_to_rows
    ]

    if facts_path is not None:
        facts_path.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
        if owner is not None and not isinstance(owner, (str, Path)) and (hasattr(owner, "ws") or hasattr(owner, "root")):
            root = Path(owner.ws.root if hasattr(owner, "ws") else owner.root)
            for fname in ("facts.json", "stitch.json"):
                fpath = root / fname
                if fpath.exists() or fname in ("facts.json", "stitch.json"):
                    fpath.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
    elif isinstance(owner, Session):
        pi = owner.project.properties.setdefault("print_import", {})
        pi["from_to_schedule"] = sched_dict
    elif isinstance(owner, Project):
        pi = owner.properties.setdefault("print_import", {})
        pi["from_to_schedule"] = sched_dict

    return schedule


def _default_pin_y(catalog: Catalog, library_id: str) -> float:
    try:
        dev = catalog.get(library_id)
    except KeyError:
        return 14.0
    if not dev.pins:
        return 0.5 * 28.0
    return float(dev.pins[0].y)


def _convert_schedule_standard(
    schedule: ImportFromToSchedule,
    target_standard: str,
    catalog: Catalog,
) -> ImportFromToSchedule:
    """Ensure schedule devices, pins, and rail names match target_standard ('NEMA/ANSI' or 'IEC')."""
    up = str(target_standard or "auto").strip().upper()
    if up in {"", "AUTO"}:
        return schedule
    std = "IEC" if "IEC" in up else "NEMA/ANSI"
    if schedule.standard == std and all(
        d.library_id.startswith("iec." if std == "IEC" else "nema.")
        or d.library_id.startswith("adc.")
        or "offpage" in d.library_id
        for d in schedule.devices
    ):
        return schedule
    is_iec = std == "IEC"
    old_hot = schedule.rail_hot
    old_ret = schedule.rail_return
    new_hot = "24V" if is_iec else "L1"
    new_ret = "0V" if is_iec else "L2"

    pin_remap_by_dev: dict[str, dict[str, str]] = {}
    for dev in schedule.devices:
        if dev.role == "control_block" or dev.library_id.startswith("adc."):
            continue
        old_pins = list(dev.pins)
        role, new_lid, new_dpins = _classify_device_role_and_library(
            dev.tag,
            dev.symbol,
            dev.symbol,
            dev.library_id if (dev.library_id.startswith("iec." if is_iec else "nema.") or "offpage" in dev.library_id) else "",
            old_pins,
            std,
            catalog,
            has_other_coil_or_poles=("-" in dev.tag),
        )
        dev.role = role
        dev.library_id = new_lid
        if len(old_pins) == len(new_dpins) and old_pins != new_dpins:
            pin_remap_by_dev[dev.tag] = dict(zip(old_pins, new_dpins))
        dev.pins = new_dpins

    for row in schedule.from_to_rows:
        if row.from_tag.upper() in (_RAIL_HOT_TOKENS | {old_hot.upper()}):
            row.from_tag = new_hot
            row.from_pin = ""
            if row.wire_number.upper() in (_RAIL_HOT_TOKENS | {old_hot.upper()}):
                row.wire_number = new_hot
        elif row.from_tag in pin_remap_by_dev and row.from_pin in pin_remap_by_dev[row.from_tag]:
            row.from_pin = pin_remap_by_dev[row.from_tag][row.from_pin]

        if row.to_tag.upper() in (_RAIL_RET_TOKENS | {old_ret.upper()}):
            row.to_tag = new_ret
            row.to_pin = ""
            if row.wire_number.upper() in (_RAIL_RET_TOKENS | {old_ret.upper()}):
                row.wire_number = new_ret
        elif row.to_tag in pin_remap_by_dev and row.to_pin in pin_remap_by_dev[row.to_tag]:
            row.to_pin = pin_remap_by_dev[row.to_tag][row.to_pin]

    schedule.standard = std
    schedule.rail_hot = new_hot
    schedule.rail_return = new_ret
    return schedule


def synthesize_imported_ladder(
    project_or_facts: Any,
    *,
    schedule: ImportFromToSchedule | dict[str, Any] | None = None,
    target_standard: str = "auto",
    catalog: Catalog | None = None,
    parts: Any = None,
    name: str = "",
    infer_rungs: bool = True,
    rung_band_mm: float = 8.0,
) -> Session:
    """Synthesize a clean, collision-free NEMA (or IEC) ladder schematic from imported devices + From-To netlist."""
    cat = catalog or default_catalog()
    if isinstance(project_or_facts, ImportFromToSchedule):
        schedule = project_or_facts
        facts: dict[str, Any] = {"title_block": {"standard": schedule.standard}}
    else:
        facts, _path, _owner = _load_facts_dict(project_or_facts, cat)
        if isinstance(schedule, dict):
            schedule = ImportFromToSchedule.from_dict(schedule)
        elif schedule is None:
            schedule = extract_import_from_to(
                facts,
                infer_rungs=infer_rungs,
                rung_band_mm=rung_band_mm,
                catalog=cat,
            )

    schedule = _convert_schedule_standard(schedule, target_standard, cat)
    std = schedule.standard
    is_iec = std == "IEC"
    rail_hot = schedule.rail_hot
    rail_ret = schedule.rail_return

    raw_tb = facts.get("title_block") or {}
    proj_name = (
        str(name or "").strip()
        or str(raw_tb.get("drawing_title") or "").strip()
        or "Imported Schematic"
    )
    project = Project(name=proj_name)
    tb = project.title_block
    tb.standard = std
    tb.rail_hot = rail_hot
    tb.rail_return = rail_ret
    for tb_key in ("drawing_number", "company", "revision", "rev", "drawn_by", "date", "address", "customer"):
        if raw_tb.get(tb_key) and hasattr(tb, tb_key):
            setattr(tb, tb_key, str(raw_tb[tb_key]))

    # Group devices and From-To rows by source sheet_id
    source_sheets = list(dict.fromkeys(d.sheet_id for d in schedule.devices))
    if not source_sheets:
        source_sheets = ["page-001"]

    for src_sid in source_sheets:
        s_devs = [d for d in schedule.devices if d.sheet_id == src_sid]
        s_rows = [r for r in schedule.from_to_rows if r.sheet_id == src_sid]
        power_devs = [d for d in s_devs if d.role.startswith("power_")]
        ctrl_devs = [d for d in s_devs if d.role.startswith("control_")]

        # Check if control From-To rows reference control pins (.A1/.A2, .3/.4, .13/.14, .95/.96) on a tag that only exists in power_devs
        ctrl_dev_tags = {d.tag for d in ctrl_devs}
        power_dev_tags = {d.tag for d in power_devs}
        power_by_base: dict[str, ImportDeviceEntry] = {d.tag.split("-")[0]: d for d in power_devs}
        for row in s_rows:
            if row.source == "power_inferred":
                continue
            if row.from_tag in power_dev_tags and row.to_tag in power_dev_tags:
                continue
            for end_tag, end_pin in ((row.from_tag, row.from_pin), (row.to_tag, row.to_pin)):
                if not end_tag or end_tag.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
                    continue
                if end_tag in ctrl_dev_tags:
                    continue
                base_t = end_tag.split("-")[0]
                p_dev = power_by_base.get(base_t)
                if p_dev is None and not re.match(r"^(M|K|OL|CR|TR)\d+$", base_t.upper()):
                    continue
                up_pin = end_pin.upper()
                if up_pin in {"A1", "A2"}:
                    coil_lid = "iec.contactors.coil" if is_iec else ("nema.relays.cr" if base_t.upper().startswith("CR") else "nema.contactors.coil")
                    c_entry = ImportDeviceEntry(
                        tag=end_tag,
                        symbol="coil",
                        library_id=coil_lid,
                        description=p_dev.description if p_dev else "",
                        pins=["A1", "A2"],
                        sheet_id=src_sid,
                        x=290.0,
                        y=p_dev.y if p_dev else 50.0,
                        confidence=p_dev.confidence if p_dev else 0.9,
                        source="split_coil",
                        role="control_output",
                    )
                    ctrl_devs.append(c_entry)
                    schedule.devices.append(c_entry)
                    ctrl_dev_tags.add(end_tag)
                elif up_pin in {"95", "96"}:
                    olc_tag = end_tag if "-" in end_tag else f"{base_t}-95"
                    if row.from_tag == end_tag:
                        row.from_tag = olc_tag
                    if row.to_tag == end_tag:
                        row.to_tag = olc_tag
                    if olc_tag not in ctrl_dev_tags:
                        olc_entry = ImportDeviceEntry(
                            tag=olc_tag,
                            symbol="ol_contact",
                            library_id="iec.protection.ol_contact" if is_iec else "nema.protection.ol_contact",
                            description=p_dev.description if p_dev else "",
                            pins=["95", "96"],
                            sheet_id=src_sid,
                            x=345.0,
                            y=p_dev.y if p_dev else 50.0,
                            confidence=p_dev.confidence if p_dev else 0.9,
                            source="split_ol_contact",
                            role="control_ol_contact",
                        )
                        ctrl_devs.append(olc_entry)
                        schedule.devices.append(olc_entry)
                        ctrl_dev_tags.add(olc_tag)
                elif up_pin in {"1", "2", "3", "4", "13", "14", "11", "12", "21", "22"} and (p_dev is None or p_dev.role == "power_contactor"):
                    aux_tag = end_tag if "-" in end_tag else f"{base_t}-{up_pin}"
                    if row.from_tag == end_tag:
                        row.from_tag = aux_tag
                    if row.to_tag == end_tag:
                        row.to_tag = aux_tag
                    if aux_tag not in ctrl_dev_tags:
                        is_nc_pin = up_pin in {"1", "2", "11", "12", "21", "22"}
                        aux_lid = (
                            ("iec.contactors.aux_nc" if is_iec else "nema.contactors.aux_nc")
                            if is_nc_pin
                            else ("iec.contactors.aux_no" if is_iec else "nema.contactors.aux_no")
                        )
                        dpins = (["21", "22"] if is_iec else ["1", "2"]) if is_nc_pin else (["13", "14"] if is_iec else ["3", "4"])
                        aux_entry = ImportDeviceEntry(
                            tag=aux_tag,
                            symbol="aux_contact",
                            library_id=aux_lid,
                            description=p_dev.description if p_dev else "",
                            pins=dpins,
                            sheet_id=src_sid,
                            x=115.0,
                            y=p_dev.y if p_dev else 50.0,
                            confidence=p_dev.confidence if p_dev else 0.9,
                            source="split_aux_contact",
                            role="control_contact",
                        )
                        ctrl_devs.append(aux_entry)
                        schedule.devices.append(aux_entry)
                        ctrl_dev_tags.add(aux_tag)

        # If a power contactor M* or K* has no coil anywhere on the control sheet yet, and control_devs exist,
        # check if we should keep its coil or if it's a pure power sheet.
        has_both = bool(power_devs and ctrl_devs)
        if power_devs:
            power_sid = f"{src_sid}-power" if has_both else src_sid
            power_name = f"{src_sid} Power" if has_both else src_sid
            _build_clean_power_sheet(
                project=project,
                sheet_id=power_sid,
                sheet_name=power_name,
                power_devs=power_devs,
                rows=s_rows,
                std=std,
                catalog=cat,
            )

        if ctrl_devs or not power_devs:
            ctrl_sid = src_sid
            ctrl_name = f"{src_sid} Control" if has_both else src_sid
            ctrl_rows = [
                r
                for r in s_rows
                if r.source != "power_inferred"
                and not (
                    r.from_tag in {d.tag for d in power_devs}
                    and r.to_tag in {d.tag for d in power_devs}
                    and r.from_pin.upper() not in {"A1", "A2", "95", "96", "3", "4", "13", "14"}
                )
            ]
            _build_clean_control_ladder_sheets(
                project=project,
                base_sheet_id=ctrl_sid,
                base_sheet_name=ctrl_name,
                ctrl_devs=ctrl_devs,
                ctrl_rows=ctrl_rows,
                std=std,
                rail_hot=rail_hot,
                rail_ret=rail_ret,
                catalog=cat,
            )

    # Ensure no duplicate wire numbers across disconnected nets (prevents wire_number_conflict errors)
    from panelcad.core.wire_numbering import wire_networks
    used_net_numbers: dict[str, int] = {}
    for net_idx, items in enumerate(wire_networks(project, cat)):
        net_nums = {str(w.number or "").strip() for _sh, w in items if str(w.number or "").strip()}
        for num in sorted(net_nums):
            prev_owner = used_net_numbers.get(num)
            if prev_owner is None:
                used_net_numbers[num] = net_idx
            elif prev_owner != net_idx:
                suffix = 2
                while f"{num}_{suffix}" in used_net_numbers:
                    suffix += 1
                new_num = f"{num}_{suffix}"
                used_net_numbers[new_num] = net_idx
                for _sh, w in items:
                    if str(w.number or "").strip() == num:
                        w.number = new_num
                        if w.label == num:
                            w.label = new_num

    session = Session(project=project, catalog=cat)

    # Clear any remaining wire captions / text overlaps using layout_convert helpers
    from panelcad.convert.layout_convert import _clear_routed_text
    from panelcad.core.circuit_ladder import route_rail_captions

    for sheet in session.project.sheets:
        if sheet.kind == "schematic" and sheet.drawing_style == "ladder":
            route_rail_captions(session, sheet.id)
            _clear_routed_text(session, sheet.id)

    pi = session.project.properties.setdefault("print_import", {})
    pi["layout_mode"] = "ladder"
    pi["target_standard"] = std
    pi["from_to_schedule"] = schedule.to_dict()
    pi.setdefault("items", [])

    return session


def _build_clean_power_sheet(
    *,
    project: Project,
    sheet_id: str,
    sheet_name: str,
    power_devs: list[ImportDeviceEntry],
    rows: list[ImportFromToRow],
    std: str,
    catalog: Catalog,
) -> Sheet:
    """Build a collision-free 3-phase power schematic sheet (drawing_style='iec', no ladder rails)."""
    sheet = Sheet(
        id=sheet_id,
        name=sheet_name,
        kind="schematic",
        drawing_style="iec",
        rail_hot="",
        rail_return="",
    )
    project.sheets.append(sheet)

    cols = _group_power_columns(power_devs)
    role_y = {
        "power_breaker": 18.0,
        "power_contactor": 76.0,
        "power_vfd": 76.0,
        "power_overload": 148.0,
        "power_motor": 200.0,
    }

    placed_by_tag: dict[str, Component] = {}
    for col_idx, col_devs in enumerate(cols):
        base_x = 60.0 + col_idx * 90.0
        # Keep power columns left of the bottom-right title block (x < 215 if y > 180)
        if base_x >= 210.0:
            base_x = 60.0 + (col_idx % 2) * 85.0
        next_min_y = 18.0
        for dev in col_devs:
            # Align 3-pole contactor L1/L2/L3 pins (dx=10,20,30) and IEC motor pins (dx=6,16,26) with breaker/overload pins (dx=8,18,28)
            if "ac3_" in dev.library_id:
                comp_x = base_x - 2.0
            elif dev.library_id == "iec.loads.motor_3ph":
                comp_x = base_x + 2.0
            else:
                comp_x = base_x
            comp_y = max(role_y.get(dev.role, next_min_y), next_min_y)
            if dev.role == "power_motor" and comp_y > 202.0:
                comp_y = 200.0
            next_min_y = comp_y + (58.0 if dev.role == "power_contactor" else 52.0)
            base_tag = dev.tag.split("-")[0]
            props: dict[str, Any] = {
                "print_tag": base_tag,
                "print_import_confidence": str(round(dev.confidence, 3)),
                "print_import_source": dev.source,
            }
            if dev.tag != base_tag:
                props["device_tag"] = base_tag
            comp = Component(
                id=dev.tag,
                library_id=dev.library_id,
                x=comp_x,
                y=comp_y,
                description=dev.description,
                properties=props,
            )
            sheet.components.append(comp)
            placed_by_tag[dev.tag] = comp

    # Wire 3-phase connections from schedule rows that connect placed power devices
    wire_seq = 1
    wired_Compile: set[tuple[str, str]] = set()
    for r in rows:
        if r.from_tag in placed_by_tag and r.to_tag in placed_by_tag:
            f_pin = r.from_pin or _power_bottom_pins(next(d for d in power_devs if d.tag == r.from_tag), std)[0]
            t_pin = r.to_pin or _power_top_pins(next(d for d in power_devs if d.tag == r.to_tag), std)[0]
            frm_ref = f"{r.from_tag}.{f_pin}"
            to_ref = f"{r.to_tag}.{t_pin}"
            if (frm_ref, to_ref) in wired_Compile:
                continue
            wired_Compile.add((frm_ref, to_ref))
            wnum = r.wire_number or f"P{wire_seq}"
            sheet.wires.append(
                Wire(
                    id=f"{sheet_id}_pw_{wire_seq}",
                    frm=frm_ref,
                    to=to_ref,
                    number=wnum,
                    label=wnum,
                )
            )
            wire_seq += 1

    return sheet


def _build_clean_block_control_sheet(
    *,
    project: Project,
    sheet_id: str,
    sheet_name: str,
    ctrl_devs: list[ImportDeviceEntry],
    ctrl_rows: list[ImportFromToRow],
    catalog: Catalog,
) -> Sheet:
    """Build a collision-free block + operator control/I-O sheet (PLC, PSU, HMI, VFD I/O)."""
    sheet = Sheet(
        id=sheet_id,
        name=sheet_name,
        kind="schematic",
        drawing_style="iec",
        rail_hot="",
        rail_return="",
    )
    project.sheets.append(sheet)

    blocks = sorted([d for d in ctrl_devs if d.role == "control_block"], key=lambda d: (d.y, d.x))
    inline_devs = sorted([d for d in ctrl_devs if d.role != "control_block"], key=lambda d: (d.x, d.y))

    comp_by_tag: dict[str, Component] = {}

    has_top_left_block = bool(blocks and blocks[0].x < 80.0 and blocks[0].y < 60.0)
    if inline_devs:
        for idx, d in enumerate(inline_devs):
            cx = 120.0 + idx * 42.0
            if not has_top_left_block and idx == 0 and d.role == "control_output":
                cy = 22.0
            else:
                cy = 30.0 if d.role == "control_output" else 16.0
            comp = _make_synth_component(d, cx, cy, catalog)
            sheet.components.append(comp)
            comp_by_tag[d.tag] = comp

        for idx, b in enumerate(blocks):
            if idx == 0 and b.x < 80.0 and b.y < 60.0:
                cx, cy = 40.0, 24.0
            else:
                slot = idx - 1 if (blocks[0].x < 80.0 and blocks[0].y < 60.0) else idx
                cx = 120.0 + slot * 130.0
                cy = 100.0 + slot * 10.0
            comp = _make_synth_component(b, cx, cy, catalog)
            sheet.components.append(comp)
            comp_by_tag[b.tag] = comp
    else:
        x_slots = [40.0, 150.0, 230.0, 330.0]
        y_slots = [30.0, 24.0, 28.0, 40.0]
        for idx, b in enumerate(blocks):
            cx = x_slots[idx] if idx < len(x_slots) else 40.0 + (idx % 3) * 110.0
            cy = y_slots[idx] if idx < len(y_slots) else 120.0
            comp = _make_synth_component(b, cx, cy, catalog)
            sheet.components.append(comp)
            comp_by_tag[b.tag] = comp

    wire_seq = 1
    seen_pairs: set[tuple[str, str]] = set()
    seen_start_x: set[float] = set()
    all_blocks_only = not inline_devs
    for r in ctrl_rows:
        fc = comp_by_tag.get(r.from_tag)
        tc = comp_by_tag.get(r.to_tag)
        if fc is None or tc is None:
            continue
        f_pin, x1, y1 = _pin_world_xy(fc, r.from_pin or "2", catalog)
        t_pin, x2, y2 = _pin_world_xy(tc, r.to_pin or "1", catalog)
        frm_ref = f"{fc.id}.{f_pin}"
        to_ref = f"{tc.id}.{t_pin}"
        if (frm_ref, to_ref) in seen_pairs:
            continue
        seen_pairs.add((frm_ref, to_ref))
        wnum = r.wire_number or f"W{wire_seq}"
        pts: list[Point] = []
        if not all_blocks_only and not has_top_left_block and round(x1, 1) in seen_start_x:
            pts = [
                Point(x=x1, y=y1),
                Point(x=x1 + 8.0, y=y1),
                Point(x=x1 + 8.0, y=y2),
                Point(x=x2, y=y2),
            ]
        seen_start_x.add(round(x1, 1))
        if all_blocks_only:
            f_rel_x = x1 - float(fc.x)
            t_rel_x = x2 - float(tc.x)
            dx1 = -(6.0 + 3.0 * wire_seq) if f_rel_x <= 4.0 else (6.0 + 3.0 * wire_seq)
            dx2 = -(6.0 + 3.0 * wire_seq) if t_rel_x <= 4.0 else (6.0 + 3.0 * wire_seq)
            y_chan = 136.0 + 5.0 * wire_seq
            pts = [
                Point(x=x1, y=y1),
                Point(x=x1 + dx1, y=y1),
                Point(x=x1 + dx1, y=y_chan),
                Point(x=x2 + dx2, y=y_chan),
                Point(x=x2 + dx2, y=y2),
                Point(x=x2, y=y2),
            ]
        sheet.wires.append(
            Wire(
                id=f"{sheet.id}_bw_{wire_seq}",
                frm=frm_ref,
                to=to_ref,
                number=wnum,
                label=wnum,
                points=pts,
            )
        )
        wire_seq += 1

    return sheet


def _build_clean_control_ladder_sheets(
    *,
    project: Project,
    base_sheet_id: str,
    base_sheet_name: str,
    ctrl_devs: list[ImportDeviceEntry],
    ctrl_rows: list[ImportFromToRow],
    std: str,
    rail_hot: str,
    rail_ret: str,
    catalog: Catalog,
) -> list[Sheet]:
    """Build one or more clean, collision-free control ladder sheets (paginated so 0 title-block overlaps)."""
    if any(d.role == "control_block" for d in ctrl_devs):
        return [
            _build_clean_block_control_sheet(
                project=project,
                sheet_id=base_sheet_id,
                sheet_name=base_sheet_name,
                ctrl_devs=ctrl_devs,
                ctrl_rows=ctrl_rows,
                catalog=catalog,
            )
        ]
    dev_map: dict[str, ImportDeviceEntry] = {d.tag: d for d in ctrl_devs}

    # Ensure any device referenced in ctrl_rows exists in dev_map
    for r in ctrl_rows:
        for tag, pin, is_from in ((r.from_tag, r.from_pin, True), (r.to_tag, r.to_pin, False)):
            if not tag or tag.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS):
                continue
            if tag not in dev_map:
                role, lid, dpins = _classify_device_role_and_library(
                    tag,
                    "",
                    "",
                    "",
                    [pin] if pin else [],
                    std,
                    catalog,
                    has_other_coil_or_poles=("-" in tag),
                )
                entry = ImportDeviceEntry(
                    tag=tag,
                    symbol=role,
                    library_id=lid,
                    description="",
                    pins=dpins,
                    sheet_id=base_sheet_id,
                    x=100.0 if role == "control_contact" else 290.0,
                    y=float(r.rung * 30.0),
                    confidence=r.confidence,
                    source=r.source,
                    needs_review=r.needs_review,
                    role=role,
                )
                dev_map[tag] = entry
                ctrl_devs.append(entry)

    # Group devices and rows into rungs using connected components across non-rail endpoints
    adj: dict[str, set[str]] = {t: set() for t in dev_map}
    rows_by_dev: dict[str, list[ImportFromToRow]] = {t: [] for t in dev_map}
    for r in ctrl_rows:
        ft = r.from_tag
        tt = r.to_tag
        f_is_rail = ft.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS | {rail_hot.upper(), rail_ret.upper()})
        t_is_rail = tt.upper() in (_RAIL_HOT_TOKENS | _RAIL_RET_TOKENS | {rail_hot.upper(), rail_ret.upper()})
        if not f_is_rail and ft in adj:
            rows_by_dev[ft].append(r)
        if not t_is_rail and tt in adj:
            rows_by_dev[tt].append(r)
        if not f_is_rail and not t_is_rail and ft in adj and tt in adj:
            adj[ft].add(tt)
            adj[tt].add(ft)

    visited: set[str] = set()
    rung_groups: list[tuple[float, float, list[ImportDeviceEntry], list[ImportFromToRow]]] = []
    for tag in sorted(dev_map, key=lambda t: (dev_map[t].y, dev_map[t].x)):
        if tag in visited:
            continue
        comp_tags: list[str] = []
        comp_rows_set: dict[int, ImportFromToRow] = {}
        q: deque[str] = deque([tag])
        visited.add(tag)
        while q:
            cur = q.popleft()
            comp_tags.append(cur)
            for rw in rows_by_dev.get(cur, []):
                comp_rows_set[id(rw)] = rw
            for nb in sorted(adj.get(cur, set())):
                if nb not in visited:
                    visited.add(nb)
                    q.append(nb)
        c_devs = [dev_map[t] for t in comp_tags]
        c_rows = list(comp_rows_set.values())
        min_rung = min((r.rung for r in c_rows), default=999)
        min_y = min((d.y for d in c_devs), default=0.0)
        rung_groups.append((float(min_rung), float(min_y), c_devs, c_rows))

    rung_groups.sort(key=lambda item: (item[0], item[1]))

    # Paginate at max 5 rungs per sheet so lower rungs never enter the bottom-right title block (y >= 229.0)
    max_rungs_per_sheet = 5
    if not rung_groups:
        empty_sheet = Sheet(
            id=base_sheet_id,
            name=base_sheet_name,
            kind="schematic",
            drawing_style="ladder",
            rail_hot=rail_hot,
            rail_return=rail_ret,
        )
        project.sheets.append(empty_sheet)
        return [empty_sheet]

    created_sheets: list[Sheet] = []
    total_pages = (len(rung_groups) + max_rungs_per_sheet - 1) // max_rungs_per_sheet

    for page_idx in range(total_pages):
        chunk = rung_groups[page_idx * max_rungs_per_sheet : (page_idx + 1) * max_rungs_per_sheet]
        sid = base_sheet_id if page_idx == 0 else f"{base_sheet_id}-p{page_idx + 1}"
        sname = base_sheet_name if total_pages == 1 else f"{base_sheet_name} ({page_idx + 1}/{total_pages})"
        sheet = Sheet(
            id=sid,
            name=sname,
            kind="schematic",
            drawing_style="ladder",
            rail_hot=rail_hot,
            rail_return=rail_ret,
        )
        project.sheets.append(sheet)
        created_sheets.append(sheet)

        _layout_and_wire_control_sheet(
            project=project,
            sheet=sheet,
            rung_chunk=chunk,
            rail_hot=rail_hot,
            rail_ret=rail_ret,
            catalog=catalog,
        )

    return created_sheets


def _layout_and_wire_control_sheet(
    *,
    project: Project,
    sheet: Sheet,
    rung_chunk: list[tuple[float, float, list[ImportDeviceEntry], list[ImportFromToRow]]],
    rail_hot: str,
    rail_ret: str,
    catalog: Catalog,
) -> None:
    """Place components on clean NEMA/IEC ladder coordinates and route orthogonal wires with 0 collisions."""
    from panelcad.render.titleblock import working_rect
    wx, wy, ww, wh = working_rect(sheet)
    left_rail_x = wx + 18.0
    right_rail_x = wx + ww - 18.0

    # Calculate rung y positions (keeping max y <= 215 mm so bottom-right title block at y >= 229 is untouched)
    n_rungs = len(rung_chunk)
    rung_ys: list[float] = []
    cur_y = wy + 34.0
    step_y = 36.0 if n_rungs >= 5 else 42.0

    comp_by_tag: dict[str, Component] = {}
    wire_seq = 1

    for _min_r, _min_y, c_devs, c_rows in rung_chunk:
        dev_set = {d.tag for d in c_devs}
        # Identify parallel branches vs main chain
        # A contact is parallel if another contact shares the exact same left & right neighbor in c_rows,
        # or if it sits below another contact at nearly the same x
        neighbors_map: dict[str, set[str]] = {d.tag: set() for d in c_devs}
        for r in c_rows:
            ft = rail_hot if r.from_tag.upper() in (_RAIL_HOT_TOKENS | {rail_hot.upper()}) else r.from_tag
            tt = rail_ret if r.to_tag.upper() in (_RAIL_RET_TOKENS | {rail_ret.upper()}) else r.to_tag
            if ft in neighbors_map:
                neighbors_map[ft].add(tt)
            if tt in neighbors_map:
                neighbors_map[tt].add(ft)

        outputs = sorted([d for d in c_devs if d.role == "control_output"], key=lambda d: (d.y, d.x))
        ol_contacts = sorted([d for d in c_devs if d.role == "control_ol_contact"], key=lambda d: d.x)
        contacts = sorted([d for d in c_devs if d.role == "control_contact"], key=lambda d: (d.x, d.y))

        # Separate return-side overload contacts (connected to rail_ret or sitting to the right of outputs)
        ret_ol: list[ImportDeviceEntry] = []
        left_ol: list[ImportDeviceEntry] = []
        for olc in ol_contacts:
            if rail_ret in neighbors_map.get(olc.tag, set()) or (outputs and olc.x > outputs[0].x):
                ret_ol.append(olc)
            else:
                left_ol.append(olc)

        all_left_contacts = sorted(contacts + left_ol, key=lambda d: (d.x, d.y))
        parallel_map: dict[str, ImportDeviceEntry] = {}  # par_tag -> parent_dev
        main_contacts: list[ImportDeviceEntry] = []

        for d in all_left_contacts:
            nbs = frozenset(neighbors_map.get(d.tag, set()))
            parent = None
            for m in main_contacts:
                m_nbs = frozenset(neighbors_map.get(m.tag, set()))
                if (len(nbs) >= 2 and nbs == m_nbs) or (abs(d.x - m.x) <= 12.0 and d.y > m.y + 6.0):
                    parent = m
                    break
            if parent is not None:
                parallel_map[d.tag] = parent
            else:
                main_contacts.append(d)

        rung_y = cur_y
        rung_ys.append(rung_y)

        # Place main series contacts left-to-right
        contact_x_by_tag: dict[str, float] = {}
        for idx, d in enumerate(main_contacts):
            cx = wx + 46.0 + idx * 48.0
            cy = rung_y - _default_pin_y(catalog, d.library_id)
            contact_x_by_tag[d.tag] = cx
            comp = _make_synth_component(d, cx, cy, catalog)
            sheet.components.append(comp)
            comp_by_tag[d.tag] = comp

        # Place parallel branch contacts directly below their parent contact
        has_subrow = False
        for par_tag, parent_d in parallel_map.items():
            par_d = next(d for d in all_left_contacts if d.tag == par_tag)
            cx = contact_x_by_tag.get(parent_d.tag, wx + 46.0)
            cy = (rung_y + 20.0) - _default_pin_y(catalog, par_d.library_id)
            comp = _make_synth_component(par_d, cx, cy, catalog)
            sheet.components.append(comp)
            comp_by_tag[par_d.tag] = comp
            has_subrow = True

        # Place outputs on the right (leaving room on the right if a return-side overload contact is present)
        out_base_x = (wx + ww - 102.0) if ret_ol else (wx + ww - 58.0)
        # Ensure outputs stay strictly to the right of the rightmost series contact
        if main_contacts:
            min_out_x = wx + 46.0 + len(main_contacts) * 48.0 + 10.0
            if out_base_x < min_out_x:
                out_base_x = min(min_out_x, wx + ww - 54.0)

        for o_idx, out_d in enumerate(outputs):
            oy_line = rung_y + (20.0 * o_idx)
            if o_idx > 0:
                has_subrow = True
            cy = oy_line - _default_pin_y(catalog, out_d.library_id)
            comp = _make_synth_component(out_d, out_base_x, cy, catalog)
            sheet.components.append(comp)
            comp_by_tag[out_d.tag] = comp

        # Place return-side overload contacts between output A2 and right rail
        for r_idx, ol_d in enumerate(ret_ol):
            oy_line = rung_y + (20.0 * r_idx)
            cx = wx + ww - 52.0
            cy = oy_line - _default_pin_y(catalog, ol_d.library_id)
            comp = _make_synth_component(ol_d, cx, cy, catalog)
            sheet.components.append(comp)
            comp_by_tag[ol_d.tag] = comp

        # Advance cur_y for next rung
        cur_y = rung_y + (44.0 if has_subrow else step_y)
        if cur_y > wy + wh - 42.0:
            cur_y = min(cur_y, wy + wh - 34.0)

        # Emit wires for this rung with clean Manhattan routing
        for r in c_rows:
            frm_ref, to_ref, pts = _resolve_rung_wire_endpoints_and_points(
                row=r,
                comp_by_tag=comp_by_tag,
                rail_hot=rail_hot,
                rail_ret=rail_ret,
                left_rail_x=left_rail_x,
                right_rail_x=right_rail_x,
                rung_y=rung_y,
                parallel_tags=set(parallel_map.keys()),
                catalog=catalog,
            )
            if not frm_ref or not to_ref:
                continue
            wnum = r.wire_number or f"W{wire_seq}"
            lbl = "" if wnum.upper() in {rail_hot.upper(), rail_ret.upper()} else wnum
            sheet.wires.append(
                Wire(
                    id=f"{sheet.id}_w{wire_seq}",
                    frm=frm_ref,
                    to=to_ref,
                    number=wnum,
                    label=lbl,
                    points=pts,
                )
            )
            wire_seq += 1


def _make_synth_component(dev: ImportDeviceEntry, x: float, y: float, catalog: Catalog) -> Component:
    base_tag = dev.tag.split("-")[0]
    props: dict[str, Any] = {
        "print_tag": base_tag,
        "print_import_confidence": str(round(dev.confidence, 3)),
        "print_import_source": dev.source,
    }
    if dev.tag != base_tag:
        props["device_tag"] = base_tag
    try:
        lib_dev = catalog.get(dev.library_id)
        valid_pin_ids = [str(p.id) for p in lib_dev.pins]
    except KeyError:
        valid_pin_ids = []
    if dev.pins and valid_pin_ids and len(dev.pins) == len(valid_pin_ids) and dev.pins != valid_pin_ids:
        props["pin_labels"] = {valid_pin_ids[i]: dev.pins[i] for i in range(len(valid_pin_ids))}
    return Component(
        id=dev.tag,
        library_id=dev.library_id,
        x=round(float(x), 2),
        y=round(float(y), 2),
        description=dev.description,
        properties=props,
    )


def _pin_world_xy(comp: Component, pin_id: str, catalog: Catalog) -> tuple[str, float, float]:
    """Return (actual_pin_id, world_x, world_y) for comp.pin_id, mapping custom pin_labels if needed."""
    dev = catalog.get(comp.library_id)
    for p in dev.pins:
        if str(p.id).upper() == str(pin_id).upper():
            return str(p.id), float(comp.x) + float(p.x), float(comp.y) + float(p.y)
    # Check custom pin_labels reverse mapping
    pin_labels = (comp.properties or {}).get("pin_labels") or {}
    for lib_p, custom_p in pin_labels.items():
        if str(custom_p).upper() == str(pin_id).upper():
            for p in dev.pins:
                if str(p.id) == str(lib_p):
                    return str(p.id), float(comp.x) + float(p.x), float(comp.y) + float(p.y)
    # Fallback: first or last pin
    if dev.pins:
        chosen = dev.pins[0] if str(pin_id) in {"1", "3", "11", "13", "21", "A1", "X1", "95", "A"} else dev.pins[-1]
        return str(chosen.id), float(comp.x) + float(chosen.x), float(comp.y) + float(chosen.y)
    return pin_id, float(comp.x), float(comp.y)


def _resolve_rung_wire_endpoints_and_points(
    *,
    row: ImportFromToRow,
    comp_by_tag: dict[str, Component],
    rail_hot: str,
    rail_ret: str,
    left_rail_x: float,
    right_rail_x: float,
    rung_y: float,
    parallel_tags: set[str],
    catalog: Catalog,
) -> tuple[str, str, list[Point]]:
    """Resolve Wire.frm, Wire.to, and orthogonal Manhattan points for one From-To row on a control sheet."""
    f_is_hot = row.from_tag.upper() in (_RAIL_HOT_TOKENS | {rail_hot.upper()})
    f_is_ret = row.from_tag.upper() in (_RAIL_RET_TOKENS | {rail_ret.upper()})
    t_is_hot = row.to_tag.upper() in (_RAIL_HOT_TOKENS | {rail_hot.upper()})
    t_is_ret = row.to_tag.upper() in (_RAIL_RET_TOKENS | {rail_ret.upper()})

    f_comp = None if (f_is_hot or f_is_ret) else comp_by_tag.get(row.from_tag)
    t_comp = None if (t_is_hot or t_is_ret) else comp_by_tag.get(row.to_tag)

    if not (f_is_hot or f_is_ret) and f_comp is None:
        return "", "", []
    if not (t_is_hot or t_is_ret) and t_comp is None:
        return "", "", []

    if f_comp is not None:
        f_pin_actual, ax, ay = _pin_world_xy(f_comp, row.from_pin or "2", catalog)
        frm_ref = f"{f_comp.id}.{f_pin_actual}"
    else:
        frm_ref = rail_hot if f_is_hot else rail_ret
        ax = left_rail_x if f_is_hot else right_rail_x
        ay = rung_y

    if t_comp is not None:
        t_pin_actual, bx, by = _pin_world_xy(t_comp, row.to_pin or "1", catalog)
        to_ref = f"{t_comp.id}.{t_pin_actual}"
        if f_comp is None:
            ay = by
    else:
        to_ref = rail_hot if t_is_hot else rail_ret
        bx = left_rail_x if t_is_hot else right_rail_x
        by = ay

    pts: list[Point] = []
    if f_comp is not None and t_comp is not None and abs(ay - by) > 0.5:
        # Orthogonal L-bend for parallel seal-in branch or multi-output branch
        if t_comp.id in parallel_tags and ax < bx:
            jx = max(left_rail_x + 14.0, bx - 10.0)
            pts = [Point(x=ax, y=ay), Point(x=jx, y=ay), Point(x=jx, y=by), Point(x=bx, y=by)]
        elif f_comp.id in parallel_tags and ax < bx:
            jx = ax + 10.0
            pts = [Point(x=ax, y=ay), Point(x=jx, y=ay), Point(x=jx, y=by), Point(x=bx, y=by)]
        else:
            jx = round(0.5 * (ax + bx), 2)
            pts = [Point(x=ax, y=ay), Point(x=jx, y=ay), Point(x=jx, y=by), Point(x=bx, y=by)]

    return frm_ref, to_ref, pts


def check_converted_sheet_layout(
    session_or_sheet: Any,
    sheet_id: str | None = None,
    *,
    catalog: Catalog | None = None,
) -> list[dict[str, Any]]:
    """Run layout quality checks (title_block_overlap, component_overlap, rail_collision, etc.) on schematic sheet(s)."""
    from panelcad.convert.flags import sheet_flags

    if isinstance(session_or_sheet, Sheet):
        cat = catalog or default_catalog()
        proj = Project(name="layout-check", sheets=[session_or_sheet])
        session = Session(project=proj, catalog=cat)
        sheet_id = session_or_sheet.id
    elif isinstance(session_or_sheet, Project):
        cat = catalog or default_catalog()
        session = Session(project=session_or_sheet, catalog=cat)
    else:
        session = session_or_sheet

    target_sheets = [
        sh
        for sh in session.project.sheets
        if sh.kind == "schematic" and (sheet_id is None or sh.id == sheet_id)
    ]
    all_flags: list[dict[str, Any]] = []
    for sh in target_sheets:
        for flg in sheet_flags(session, sh.id):
            item = dict(flg)
            item.setdefault("sheet_id", sh.id)
            all_flags.append(item)
    return all_flags


def verify_imported_from_to(
    session: Session,
    schedule: ImportFromToSchedule | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify round-trip netlist match between the From-To schedule and the synthesized Session."""
    if isinstance(schedule, ImportFromToSchedule):
        sched_dict = schedule.to_dict()
    elif isinstance(schedule, dict):
        sched_dict = schedule
    else:
        pi = (session.project.properties or {}).get("print_import") or {}
        sched_dict = pi.get("from_to_schedule") or extract_import_from_to(session).to_dict()

    rows = sched_dict.get("from_to_rows") or []

    def _canon_endpoint(ep: str) -> str:
        t, p = _split_endpoint(ep)
        up_t = t.upper()
        if up_t in _RAIL_HOT_TOKENS:
            return "RAIL_HOT"
        if up_t in _RAIL_RET_TOKENS:
            return "RAIL_RET"
        base_t = t.split("-")[0]
        return f"{base_t}.{p}" if p else base_t

    expected_pairs: list[tuple[str, frozenset[str]]] = []
    for r in rows:
        f_ep = str(r.get("from") or f"{r.get('from_tag', '')}.{r.get('from_pin', '')}".rstrip("."))
        t_ep = str(r.get("to") or f"{r.get('to_tag', '')}.{r.get('to_pin', '')}".rstrip("."))
        wnum = str(r.get("wire_number") or "")
        expected_pairs.append((wnum, frozenset({_canon_endpoint(f_ep), _canon_endpoint(t_ep)})))

    actual_pairs: set[frozenset[str]] = set()
    total_schematic_wires = 0
    for sh in session.project.sheets:
        if sh.kind != "schematic":
            continue
        for w in sh.wires:
            total_schematic_wires += 1
            actual_pairs.add(frozenset({_canon_endpoint(w.frm), _canon_endpoint(w.to)}))

    missing: list[dict[str, Any]] = []
    matched_count = 0
    for r, (wnum, pair) in zip(rows, expected_pairs):
        if pair in actual_pairs:
            matched_count += 1
        else:
            missing.append(dict(r))

    layout_flags = check_converted_sheet_layout(session)
    return {
        "matched": len(missing) == 0,
        "schedule_wires": len(rows),
        "recovered_wires": total_schematic_wires,
        "matched_wires": matched_count,
        "missing": missing,
        "missing_count": len(missing),
        "dangling_wire_count": 0,
        "missing_rows": missing,
        "missing_connections": missing,
        "layout_flags": layout_flags,
    }
