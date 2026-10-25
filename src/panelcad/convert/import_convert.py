"""Import -> From-To Review -> IEC-to-NEMA Convert pipeline.

For IEC PDFs and scanned schematic prints:
1. Extract IEC components & draft From-To wire list from vector PDF (`pypdf` /
   `panelcad.importer`) or raster scan via `panelcad.importer.vision` (no
   Tesseract or Poppler dependency).
2. Let the user (or caller/GUI/API) inspect and correct the From-To rows and
   device list (`apply_from_to_corrections`).
3. Reconstruct a clean native PanelCAD IEC `Session` / `Project` from the
   reviewed From-To rows (`build_iec_project_from_imported_from_to`).
4. Run the IEC -> NEMA conversion engine (`convert_imported_iec_print`),
   preserving netlist/From-To equivalence, zero layout flags, and propagating
   any uncorrected low-confidence vision items as `ConversionDecision` records
   (`reason_code="imported_low_confidence"`, `needs_review=True`).
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from panelcad.convert.plan import ConversionDecision
from panelcad.convert.table import parse_device_tag
from panelcad.core.from_to import from_to_rows
from panelcad.core.models import Component, Project, Sheet, Wire
from panelcad.library.catalog import Catalog, default_catalog

DEFAULT_IMPORT_CONFIDENCE_THRESHOLD: float = 0.80

_RAIL_NAMES: frozenset[str] = frozenset(
    {"L+", "0V", "L1", "L2", "L3", "N", "PE", "GND", "24V", "+24V", "0VDC", "+24VDC"}
)
_POWER_PINS: frozenset[str] = frozenset(
    {
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "L1",
        "L2",
        "L3",
        "T1",
        "T2",
        "T3",
        "U1",
        "V1",
        "W1",
        "U2",
        "V2",
        "W2",
        "U",
        "V",
        "W",
    }
)
_POWER_RAILS: frozenset[str] = frozenset({"L1", "L2", "L3", "PE", "N"})
_CONTROL_RAILS: frozenset[str] = frozenset({"L+", "0V", "24V", "+24V", "0VDC", "+24VDC"})


def clean_iec_tag(raw: str | None) -> str:
    """Normalize an IEC tag by stripping leading ``-`` or ``=`` prefixes while keeping child suffixes."""

    token = str(raw or "").strip()
    if not token:
        return ""
    if token.upper() in _RAIL_NAMES:
        u = token.upper()
        if u in {"+24V", "24V", "+24VDC"}:
            return "L+"
        if u in {"0VDC"}:
            return "0V"
        return u
    # Strip IEC location/function prefix like =A1-K1 -> K1 or -K1 -> K1
    if "=" in token and "-" in token:
        token = token.split("-", 1)[1].strip()
    while token.startswith(("-", "+", "=")):
        token = token[1:].strip()
    # Normalize terminal colon X1:1 -> X1-1 when used as a component id, unless caller splits tag:pin first
    return token


def split_endpoint_token(endpoint: str, *, known_device_ids: set[str] | None = None) -> tuple[str, str]:
    """Split an endpoint string like ``'-K1:13'``, ``'K1-14:13'``, ``'S1.13'``, or ``'L+'`` into ``(tag, pin)``."""

    raw = str(endpoint or "").strip()
    if not raw:
        return ("", "")
    upper = raw.upper()
    if upper in _RAIL_NAMES:
        return (clean_iec_tag(upper), "")

    if ":" in raw:
        left, right = raw.rsplit(":", 1)
        tag = clean_iec_tag(left)
        pin = right.strip()
        # Special case: terminal strip tag like -X1:1 where pin wasn't separately given or X1:1:A
        if ":" in left:
            t_left, t_mid = left.rsplit(":", 1)
            tag = f"{clean_iec_tag(t_left)}-{t_mid.strip()}"
        return (tag, pin)

    if "." in raw:
        left, right = raw.rsplit(".", 1)
        return (clean_iec_tag(left), right.strip())

    cleaned = clean_iec_tag(raw)
    if known_device_ids and cleaned in known_device_ids:
        return (cleaned, "")
    return (cleaned, "")


def _infer_iec_library_id(
    tag: str,
    *,
    role: str = "",
    library_id: str = "",
    pins: Sequence[str] = (),
    catalog: Catalog | None = None,
) -> tuple[str, str]:
    """Return ``(library_id, role)`` in the IEC catalog for an extracted device."""

    cat = catalog or default_catalog()
    if library_id and library_id in cat:
        inferred_role = role or _role_from_library_id(library_id)
        return (library_id, inferred_role)

    clean = clean_iec_tag(tag)
    pin_set = {str(p).strip().upper() for p in pins if str(p).strip()}
    role_low = str(role or "").strip().lower().replace("-", "_")

    # Explicit role hints
    if role_low in {"breaker", "mccb", "circuit_breaker"}:
        return ("iec.breakers.mccb_3p_100a", "breaker")
    if role_low in {"mpcb", "motor_protection_circuit_breaker"}:
        return ("iec.breakers.mpcb_3p", "mpcb")
    if role_low in {"disconnect", "isolator"}:
        return ("iec.protection.disconnect_3p", "disconnect")
    if role_low in {"fuse"}:
        if pin_set & {"3", "4", "5", "6"}:
            return ("iec.protection.fuse_3p", "fuse")
        return ("iec.protection.fuse", "fuse")
    if role_low in {"overload", "thermal_overload", "ol"}:
        if pin_set <= {"95", "96", "97", "98"} and pin_set:
            return ("iec.protection.ol_contact", "overload")
        return ("iec.protection.overload_9a", "overload")
    if role_low in {"ol_contact", "overload_contact"}:
        return ("iec.protection.ol_contact", "overload")
    if role_low in {"motor", "motor_3ph"}:
        return ("iec.loads.motor_3ph", "motor")
    if role_low in {"contactor", "power_contactor", "starter"}:
        if pin_set and pin_set <= {"A1", "A2"}:
            return ("iec.contactors.coil", "starter_coil")
        return ("iec.contactors.ac3_9a", "contactor")
    if role_low in {"coil", "starter_coil", "contactor_coil"}:
        return ("iec.contactors.coil", "starter_coil")
    if role_low in {"relay", "control_relay", "cr"}:
        return ("iec.relays.cr", "relay")
    if role_low in {"aux_no", "contact_no", "no_contact"}:
        return ("iec.contactors.aux_no", "aux_no")
    if role_low in {"aux_nc", "contact_nc", "nc_contact"}:
        return ("iec.contactors.aux_nc", "aux_nc")
    if role_low in {"estop", "e_stop", "emergency_stop"}:
        return ("iec.operators.estop", "estop")
    if role_low in {"pb_nc", "stop_pb", "pushbutton_nc"}:
        return ("iec.operators.pb_nc", "pb_nc")
    if role_low in {"pb_no", "start_pb", "pushbutton_no", "pushbutton"}:
        return ("iec.operators.pb_no", "pb_no")
    if role_low in {"pilot_light", "indicator", "lamp"}:
        return ("iec.operators.pilot_light", "pilot_light")
    if role_low in {"horn", "buzzer", "alarm"}:
        return ("iec.operators.horn", "horn")
    if role_low in {"timer", "on_delay"}:
        return ("iec.timers.on_delay", "timer")
    if role_low in {"terminal", "feed_through"}:
        return ("iec.terminals.feed_through", "terminal")

    # Child contact syntax like K1-14, K1-21, K1-43, F1-95, KT1-15
    child_match = re.match(r"^([A-Za-z]+)(\d+)[-:](.+)$", clean)
    if child_match:
        prefix = child_match.group(1).upper()
        suffix = child_match.group(3).strip()
        if prefix in {"X", "TB"}:
            return ("iec.terminals.feed_through", "terminal")
        if prefix == "F" or suffix in {"95", "96", "95-96"}:
            return ("iec.protection.ol_contact", "overload")
        if prefix in {"KT", "T"} or suffix in {"15", "18", "15-18"}:
            return ("iec.timers.contact_notc", "timer_contact")
        if suffix.endswith(("1", "2")) or suffix in {"21", "22", "31", "32", "11", "12"}:
            return ("iec.contactors.aux_nc", "aux_nc")
        return ("iec.contactors.aux_no", "aux_no")

    parsed = parse_device_tag(clean)
    prefix = parsed[0].upper() if parsed else clean[:1].upper()

    if prefix == "Q":
        return ("iec.breakers.mccb_3p_100a", "breaker")
    if prefix == "F":
        if pin_set and pin_set <= {"95", "96"}:
            return ("iec.protection.ol_contact", "overload")
        if pin_set & {"95", "96"} or len(pin_set & {"1", "2", "3", "4", "5", "6"}) >= 2:
            return ("iec.protection.overload_9a", "overload")
        if pin_set <= {"1", "2"} and pin_set:
            return ("iec.protection.fuse", "fuse")
        return ("iec.protection.overload_9a", "overload")
    if prefix == "M":
        return ("iec.loads.motor_3ph", "motor")
    if prefix in {"K", "KM", "KA"}:
        if pin_set & {"L1", "L2", "L3", "T1", "T2", "T3"}:
            return ("iec.contactors.ac3_9a", "contactor")
        if pin_set and pin_set <= {"13", "14", "23", "24", "33", "34", "43", "44"}:
            return ("iec.contactors.aux_no", "aux_no")
        if pin_set and pin_set <= {"11", "12", "21", "22", "31", "32", "41", "42"}:
            return ("iec.contactors.aux_nc", "aux_nc")
        if prefix == "KA":
            return ("iec.relays.cr", "relay")
        return ("iec.contactors.coil", "starter_coil")
    if prefix in {"KT", "T"}:
        if pin_set and pin_set <= {"15", "18"}:
            return ("iec.timers.contact_notc", "timer_contact")
        return ("iec.timers.on_delay", "timer")
    if prefix == "S":
        if pin_set and pin_set <= {"11", "12", "21", "22"}:
            if {"21", "22"} <= pin_set and {"11", "12"} <= pin_set:
                return ("iec.operators.estop", "estop")
            return ("iec.operators.pb_nc", "pb_nc")
        return ("iec.operators.pb_no", "pb_no")
    if prefix in {"H", "P"}:
        return ("iec.operators.pilot_light", "pilot_light")
    if prefix in {"X", "TB"}:
        return ("iec.terminals.feed_through", "terminal")
    if prefix == "B":
        return ("iec.operators.pressure_no", "pressure_switch")
    if prefix == "Y":
        return ("iec.loads.solenoid", "solenoid")

    return ("iec.relays.cr", role or "relay")


def _role_from_library_id(library_id: str) -> str:
    lid = str(library_id or "").lower()
    if ".breakers.mpcb" in lid:
        return "mpcb"
    if ".breakers." in lid:
        return "breaker"
    if "disconnect" in lid:
        return "disconnect"
    if "overload" in lid or "ol_contact" in lid:
        return "overload"
    if "fuse" in lid:
        return "fuse"
    if ".contactors.ac3" in lid:
        return "contactor"
    if ".contactors.coil" in lid:
        return "starter_coil"
    if ".contactors.aux_nc" in lid:
        return "aux_nc"
    if ".contactors.aux_no" in lid:
        return "aux_no"
    if ".relays." in lid:
        return "relay"
    if ".loads.motor" in lid:
        return "motor"
    if ".operators.estop" in lid:
        return "estop"
    if ".operators.pb_nc" in lid:
        return "pb_nc"
    if ".operators.pb_no" in lid:
        return "pb_no"
    if ".operators.pilot_light" in lid:
        return "pilot_light"
    if ".timers." in lid:
        return "timer"
    if ".terminals." in lid:
        return "terminal"
    return ""


@dataclass
class ImportedDeviceRecord:
    """One IEC device extracted from an imported PDF or scan."""

    tag: str
    sheet_id: str = "main"
    role: str = ""
    library_id: str = ""
    pins: list[str] = field(default_factory=list)
    label: str = ""
    description: str = ""
    confidence: float = 1.0
    needs_review: bool = False
    reason_code: str = ""
    x: float | None = None
    y: float | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.tag = clean_iec_tag(self.tag)
        self.sheet_id = str(self.sheet_id or "main").strip() or "main"
        self.confidence = float(self.confidence if self.confidence is not None else 1.0)
        if not self.library_id or not self.role:
            lib_id, inferred_role = _infer_iec_library_id(
                self.tag,
                role=self.role,
                library_id=self.library_id,
                pins=self.pins,
            )
            if not self.library_id:
                self.library_id = lib_id
            if not self.role:
                self.role = inferred_role

    def to_dict(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "sheet_id": self.sheet_id,
            "role": self.role,
            "library_id": self.library_id,
            "pins": list(self.pins),
            "label": self.label,
            "description": self.description,
            "confidence": round(float(self.confidence), 4),
            "needs_review": bool(self.needs_review),
            "reason_code": self.reason_code,
            "x": self.x,
            "y": self.y,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "ImportedDeviceRecord") -> "ImportedDeviceRecord":
        if isinstance(data, ImportedDeviceRecord):
            return data
        raw_conf = data.get("confidence", 1.0)
        conf = float(raw_conf) if raw_conf is not None else 1.0
        return cls(
            tag=clean_iec_tag(str(data.get("tag") or data.get("id") or "")),
            sheet_id=str(data.get("sheet_id") or data.get("sheet") or "main"),
            role=str(data.get("role") or data.get("type") or ""),
            library_id=str(data.get("library_id") or ""),
            pins=[str(p) for p in (data.get("pins") or [])],
            label=str(data.get("label") or ""),
            description=str(data.get("description") or ""),
            confidence=conf,
            needs_review=bool(data.get("needs_review", False)),
            reason_code=str(data.get("reason_code") or ""),
            x=float(data["x"]) if data.get("x") is not None else None,
            y=float(data["y"]) if data.get("y") is not None else None,
            properties=dict(data.get("properties") or {}),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass
class ImportedFromToRow:
    """One draft From-To wire connection extracted from an imported IEC print or scan."""

    wire_number: str = ""
    from_tag: str = ""
    from_pin: str = ""
    to_tag: str = ""
    to_pin: str = ""
    sheet_id: str = "main"
    potential: str = ""
    confidence: float = 1.0
    needs_review: bool = False
    reason_code: str = ""
    note: str = ""
    row_id: str = ""

    def __post_init__(self) -> None:
        self.wire_number = str(self.wire_number or "").strip()
        self.from_tag = clean_iec_tag(self.from_tag)
        self.from_pin = str(self.from_pin or "").strip()
        self.to_tag = clean_iec_tag(self.to_tag)
        self.to_pin = str(self.to_pin or "").strip()
        self.sheet_id = str(self.sheet_id or "main").strip() or "main"
        self.potential = str(self.potential or "").strip()
        self.confidence = float(self.confidence if self.confidence is not None else 1.0)
        if self.needs_review and not self.reason_code:
            self.reason_code = "imported_low_confidence"

    @property
    def frm(self) -> str:
        if self.from_tag in _RAIL_NAMES or not self.from_pin:
            return self.from_tag
        return f"{self.from_tag}:{self.from_pin}"

    @property
    def to(self) -> str:
        if self.to_tag in _RAIL_NAMES or not self.to_pin:
            return self.to_tag
        return f"{self.to_tag}:{self.to_pin}"

    @property
    def from_endpoint(self) -> str:
        if self.from_tag in _RAIL_NAMES or not self.from_pin:
            return self.from_tag
        return f"{self.from_tag}.{self.from_pin}"

    @property
    def to_endpoint(self) -> str:
        if self.to_tag in _RAIL_NAMES or not self.to_pin:
            return self.to_tag
        return f"{self.to_tag}.{self.to_pin}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "wire_number": self.wire_number,
            "from_tag": self.from_tag,
            "from_pin": self.from_pin,
            "to_tag": self.to_tag,
            "to_pin": self.to_pin,
            "from": self.frm,
            "to": self.to,
            "sheet_id": self.sheet_id,
            "potential": self.potential,
            "confidence": round(float(self.confidence), 4),
            "needs_review": bool(self.needs_review),
            "reason_code": self.reason_code,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "ImportedFromToRow") -> "ImportedFromToRow":
        if isinstance(data, ImportedFromToRow):
            return data
        from_tag = str(data.get("from_tag") or "").strip()
        from_pin = str(data.get("from_pin") or "").strip()
        to_tag = str(data.get("to_tag") or "").strip()
        to_pin = str(data.get("to_pin") or "").strip()
        if not from_tag and (data.get("from") or data.get("frm")):
            from_tag, from_pin = split_endpoint_token(str(data.get("from") or data.get("frm")))
        if not to_tag and data.get("to"):
            to_tag, to_pin = split_endpoint_token(str(data.get("to")))
        raw_conf = data.get("confidence", 1.0)
        conf = float(raw_conf) if raw_conf is not None else 1.0
        needs_review = bool(data.get("needs_review", False))
        reason_code = str(data.get("reason_code") or ("imported_low_confidence" if needs_review else ""))
        return cls(
            wire_number=str(data.get("wire_number") or data.get("number") or ""),
            from_tag=from_tag,
            from_pin=from_pin,
            to_tag=to_tag,
            to_pin=to_pin,
            sheet_id=str(data.get("sheet_id") or data.get("sheet") or data.get("from_sheet") or "main"),
            potential=str(data.get("potential") or data.get("net") or ""),
            confidence=conf,
            needs_review=needs_review,
            reason_code=reason_code,
            note=str(data.get("note") or data.get("message") or ""),
            row_id=str(data.get("row_id") or data.get("id") or ""),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


@dataclass
class ImportedIECPrintDraft:
    """Draft extraction result from an IEC PDF or scan prior to NEMA conversion."""

    source_path: str = ""
    title: str = "Imported IEC Print"
    standard: str = "IEC"
    sheets: list[dict[str, Any]] = field(default_factory=list)
    devices: list[ImportedDeviceRecord] = field(default_factory=list)
    from_to_rows: list[ImportedFromToRow] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[ConversionDecision] = field(default_factory=list)
    confidence_threshold: float = DEFAULT_IMPORT_CONFIDENCE_THRESHOLD
    source_project_data: dict[str, Any] | None = None

    @property
    def from_to(self) -> list[ImportedFromToRow]:
        return self.from_to_rows

    @property
    def rows(self) -> list[ImportedFromToRow]:
        return self.from_to_rows

    def unresolved_rows(self) -> list[ImportedFromToRow]:
        threshold = float(self.confidence_threshold or DEFAULT_IMPORT_CONFIDENCE_THRESHOLD)
        return [r for r in self.from_to_rows if r.needs_review or r.confidence < threshold]

    def unresolved_devices(self) -> list[ImportedDeviceRecord]:
        threshold = float(self.confidence_threshold or DEFAULT_IMPORT_CONFIDENCE_THRESHOLD)
        return [d for d in self.devices if d.needs_review or d.confidence < threshold]

    def refresh_decisions(self) -> list[ConversionDecision]:
        """Rebuild ``decisions`` from current low-confidence devices and From-To rows."""

        threshold = float(self.confidence_threshold or DEFAULT_IMPORT_CONFIDENCE_THRESHOLD)
        decs: list[ConversionDecision] = []
        for dev in self.devices:
            if dev.confidence < threshold or dev.needs_review:
                dev.needs_review = True
                dev.reason_code = dev.reason_code or "imported_low_confidence"
                decs.append(
                    ConversionDecision(
                        reason_code="imported_low_confidence",
                        component_id=dev.tag,
                        sheet_id=dev.sheet_id,
                        before={"tag": dev.tag, "role": dev.role, "library_id": dev.library_id},
                        after={"tag": dev.tag, "library_id": dev.library_id},
                        needs_review=True,
                        confidence=dev.confidence,
                        message=(
                            f"Imported device '{dev.tag}' on sheet '{dev.sheet_id}' was extracted "
                            f"with low confidence ({dev.confidence:.2f} < {threshold:.2f}). "
                            "Verify tag and symbol before trusting the converted drawing."
                        ),
                        chunk_id="import",
                    )
                )
        for idx, row in enumerate(self.from_to_rows):
            if not row.row_id:
                row.row_id = f"row_{idx + 1}"
            if row.confidence < threshold or row.needs_review:
                row.needs_review = True
                row.reason_code = row.reason_code or "imported_low_confidence"
                comp_ref = row.from_tag if row.from_tag not in _RAIL_NAMES else row.to_tag
                decs.append(
                    ConversionDecision(
                        reason_code="imported_low_confidence",
                        component_id=comp_ref or row.wire_number or f"row_{idx + 1}",
                        sheet_id=row.sheet_id,
                        before={
                            "row_id": row.row_id,
                            "wire_number": row.wire_number,
                            "from": row.frm,
                            "to": row.to,
                        },
                        after={
                            "wire_number": row.wire_number,
                            "from": row.frm,
                            "to": row.to,
                        },
                        needs_review=True,
                        confidence=row.confidence,
                        message=(
                            row.note
                            or (
                                f"Imported From-To wire '{row.wire_number or row.row_id}' "
                                f"({row.frm} -> {row.to}) on sheet '{row.sheet_id}' has low vision "
                                f"confidence ({row.confidence:.2f} < {threshold:.2f}). "
                                "Confirm or edit this From-To connection."
                            )
                        ),
                        chunk_id="import",
                    )
                )
        self.decisions = decs
        return decs

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "title": self.title,
            "standard": self.standard,
            "sheets": [dict(s) for s in self.sheets],
            "devices": [d.to_dict() for d in self.devices],
            "from_to_rows": [r.to_dict() for r in self.from_to_rows],
            "from_to": [r.to_dict() for r in self.from_to_rows],
            "notes": [dict(n) for n in self.notes],
            "decisions": [d.to_dict() for d in self.decisions],
            "confidence_threshold": self.confidence_threshold,
            "needs_review_count": sum(1 for d in self.decisions if d.needs_review),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | "ImportedIECPrintDraft") -> "ImportedIECPrintDraft":
        if isinstance(data, ImportedIECPrintDraft):
            return data
        raw_rows = data.get("from_to_rows") if data.get("from_to_rows") is not None else data.get("from_to") or data.get("wires") or []
        draft = cls(
            source_path=str(data.get("source_path") or ""),
            title=str(data.get("title") or "Imported IEC Print"),
            standard=str(data.get("standard") or "IEC"),
            sheets=[dict(s) for s in (data.get("sheets") or [])],
            devices=[ImportedDeviceRecord.from_dict(d) for d in (data.get("devices") or [])],
            from_to_rows=[ImportedFromToRow.from_dict(r) for r in raw_rows],
            notes=[dict(n) if isinstance(n, dict) else {"text": str(n)} for n in (data.get("notes") or [])],
            decisions=[ConversionDecision.from_dict(d) for d in (data.get("decisions") or [])],
            confidence_threshold=float(
                data.get("confidence_threshold", DEFAULT_IMPORT_CONFIDENCE_THRESHOLD)
                or DEFAULT_IMPORT_CONFIDENCE_THRESHOLD
            ),
            source_project_data=data.get("source_project_data"),
        )
        if not draft.decisions:
            draft.refresh_decisions()
        return draft

    def get(self, key: str, default: Any = None) -> Any:
        if key in {"from_to_rows", "from_to", "rows"}:
            return self.from_to_rows
        if key == "devices":
            return self.devices
        if key == "decisions":
            return self.decisions
        return self.to_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key in {"from_to_rows", "from_to", "rows"}:
            return self.from_to_rows
        if key == "devices":
            return self.devices
        if key == "decisions":
            return self.decisions
        return self.to_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict() or key in {"from_to_rows", "from_to", "rows", "devices", "decisions"}

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()

    def values(self):
        return self.to_dict().values()


class RecordedImportVisionBackend:
    """Deterministic vision backend for IEC scan/PDF import tests (no network, no Tesseract)."""

    def __init__(self, payload: dict[str, Any] | Path | str | Callable[..., dict[str, Any]]) -> None:
        if isinstance(payload, (Path, str)) and Path(payload).is_file():
            self.payload: Any = json.loads(Path(payload).read_text(encoding="utf-8"))
        elif isinstance(payload, str):
            self.payload = json.loads(payload)
        else:
            self.payload = payload

    def extract_print(self, path: Path | str) -> dict[str, Any]:
        if callable(self.payload):
            return dict(self.payload(path) or {})
        if isinstance(self.payload, dict):
            key = Path(path).name
            if key in self.payload and isinstance(self.payload[key], dict):
                return dict(self.payload[key])
            if "*" in self.payload and isinstance(self.payload["*"], dict):
                return dict(self.payload["*"])
            if "default" in self.payload and isinstance(self.payload["default"], dict):
                return dict(self.payload["default"])
            return dict(self.payload)
        return {}


def _extract_from_vision_payload(
    payload: dict[str, Any],
    *,
    default_sheet_id: str = "main",
    confidence_threshold: float = DEFAULT_IMPORT_CONFIDENCE_THRESHOLD,
    catalog: Catalog | None = None,
) -> tuple[list[dict[str, Any]], list[ImportedDeviceRecord], list[ImportedFromToRow], list[dict[str, Any]]]:
    """Parse structured vision/recorded payload into sheets, devices, From-To rows, and notes."""

    cat = catalog or default_catalog()
    sheets_meta: list[dict[str, Any]] = []
    devices: list[ImportedDeviceRecord] = []
    rows: list[ImportedFromToRow] = []
    notes: list[dict[str, Any]] = []

    pages = payload.get("pages") if isinstance(payload.get("pages"), list) else None
    page_entries = pages if pages is not None else [payload]

    for p_idx, page in enumerate(page_entries):
        if not isinstance(page, dict):
            continue
        sh_id = str(
            page.get("sheet_id")
            or page.get("id")
            or (f"page_{p_idx + 1}" if len(page_entries) > 1 else default_sheet_id)
        ).strip()
        sh_title = str(page.get("title") or sh_id)
        sh_kind = str(page.get("kind") or "schematic")
        sheets_meta.append({"id": sh_id, "title": sh_title, "kind": sh_kind})

        for raw_dev in page.get("devices") or []:
            if not isinstance(raw_dev, dict):
                continue
            if raw_dev.get("unreadable"):
                continue
            raw_tag = str(raw_dev.get("tag") or raw_dev.get("id") or "").strip()
            if not raw_tag:
                continue
            dev_rec = ImportedDeviceRecord.from_dict(
                {
                    **raw_dev,
                    "tag": raw_tag,
                    "sheet_id": str(raw_dev.get("sheet_id") or raw_dev.get("sheet") or sh_id),
                }
            )
            if dev_rec.confidence < confidence_threshold:
                dev_rec.needs_review = True
                dev_rec.reason_code = dev_rec.reason_code or "imported_low_confidence"
            devices.append(dev_rec)

        raw_wire_list = (
            page.get("from_to_rows")
            or page.get("from_to")
            or page.get("connections")
            or page.get("wires")
            or []
        )
        for w_idx, raw_w in enumerate(raw_wire_list):
            if not isinstance(raw_w, dict):
                continue
            # Skip bare spatial wire labels that have neither endpoints nor from/to tags
            has_endpoints = any(
                raw_w.get(k)
                for k in ("from_tag", "to_tag", "from", "frm", "to", "endpoints")
            )
            if not has_endpoints:
                continue
            if raw_w.get("endpoints") and isinstance(raw_w["endpoints"], (list, tuple)) and len(raw_w["endpoints"]) >= 2:
                ep_a, ep_b = str(raw_w["endpoints"][0]), str(raw_w["endpoints"][1])
                ft, fp = split_endpoint_token(ep_a)
                tt, tp = split_endpoint_token(ep_b)
                row_data = {
                    **raw_w,
                    "from_tag": ft,
                    "from_pin": fp,
                    "to_tag": tt,
                    "to_pin": tp,
                }
            else:
                row_data = dict(raw_w)
            row_data.setdefault("sheet_id", sh_id)
            row_data.setdefault("row_id", f"row_{len(rows) + 1}")
            row = ImportedFromToRow.from_dict(row_data)
            if row.confidence < confidence_threshold:
                row.needs_review = True
                row.reason_code = row.reason_code or "imported_low_confidence"
            rows.append(row)

        for raw_note in page.get("notes") or page.get("texts") or []:
            if isinstance(raw_note, dict):
                if raw_note.get("unreadable"):
                    continue
                txt = str(raw_note.get("text") or "").strip()
                if txt:
                    notes.append({"sheet_id": sh_id, "text": txt, **{k: v for k, v in raw_note.items() if k != "text"}})
            elif isinstance(raw_note, str) and raw_note.strip():
                notes.append({"sheet_id": sh_id, "text": raw_note.strip()})

    # Ensure every device referenced in From-To rows has a device record
    known_tags = {d.tag: d for d in devices}
    pins_by_tag: dict[str, list[str]] = {}
    sheet_by_tag: dict[str, str] = {}
    for r in rows:
        for t, p in ((r.from_tag, r.from_pin), (r.to_tag, r.to_pin)):
            if not t or t in _RAIL_NAMES:
                continue
            pins_by_tag.setdefault(t, [])
            if p and p not in pins_by_tag[t]:
                pins_by_tag[t].append(p)
            sheet_by_tag.setdefault(t, r.sheet_id)

    for tag, tag_pins in pins_by_tag.items():
        if tag in known_tags:
            dev = known_tags[tag]
            for p in tag_pins:
                if p not in dev.pins:
                    dev.pins.append(p)
            # Refine library_id if pins clarify power vs coil vs contact
            lib_id, inferred_role = _infer_iec_library_id(
                dev.tag,
                role=dev.role,
                library_id="",
                pins=dev.pins,
                catalog=cat,
            )
            if dev.library_id in {"iec.contactors.coil", "iec.relays.cr"} and lib_id != dev.library_id:
                dev.library_id = lib_id
                dev.role = dev.role or inferred_role
        else:
            lib_id, inferred_role = _infer_iec_library_id(tag, pins=tag_pins, catalog=cat)
            new_dev = ImportedDeviceRecord(
                tag=tag,
                sheet_id=sheet_by_tag.get(tag, default_sheet_id),
                role=inferred_role,
                library_id=lib_id,
                pins=list(tag_pins),
                confidence=1.0,
            )
            devices.append(new_dev)
            known_tags[tag] = new_dev

    return (sheets_meta, devices, rows, notes)


def _extract_from_panelcad_project(
    project: Project,
    catalog: Catalog,
    *,
    source_path: str = "",
    confidence_threshold: float = DEFAULT_IMPORT_CONFIDENCE_THRESHOLD,
    preserve_source_project: bool = False,
) -> ImportedIECPrintDraft:
    """Build an ``ImportedIECPrintDraft`` from a reconstructed or loaded PanelCAD ``Project``."""

    sheets_meta: list[dict[str, Any]] = []
    devices: list[ImportedDeviceRecord] = []
    rows: list[ImportedFromToRow] = []
    notes: list[dict[str, Any]] = []

    for sheet in project.sheets:
        if sheet.kind not in {"schematic", ""}:
            continue
        sheets_meta.append(
            {
                "id": sheet.id,
                "title": sheet.title or sheet.id,
                "kind": sheet.kind or "schematic",
                "drawing_style": sheet.drawing_style,
                "rail_hot": sheet.rail_hot,
                "rail_return": sheet.rail_return,
            }
        )
        seen_tags_in_project = {d.tag for d in devices}
        for comp in sheet.components:
            if not preserve_source_project:
                # Skip pin labels or off-page artifacts misread as standalone devices by vector importer
                if comp.id.upper() in {"A1", "A2", "L1", "L2", "L3", "T1", "T2", "T3", "U1", "V1", "W1", "13", "14", "21", "22", "95", "96"}:
                    continue
                if re.match(r"^OP\d+$", comp.id.upper()):
                    continue
                if comp.id in seen_tags_in_project:
                    continue
            seen_tags_in_project.add(comp.id)
            raw_lib = comp.library_id
            if not preserve_source_project and (raw_lib == "reconstruct.needs_info" or raw_lib.startswith("nema.") or raw_lib.startswith("oem.") or raw_lib.startswith("adc.")):
                raw_lib = ""
            try:
                dev_def = catalog.get(raw_lib or comp.library_id)
                pin_ids = [p.id for p in dev_def.pins]
            except Exception:  # noqa: BLE001
                pin_ids = []
            conf = float(comp.properties.get("confidence", 1.0) or 1.0)
            needs_rev = bool(comp.properties.get("needs_review", False)) or conf < confidence_threshold
            devices.append(
                ImportedDeviceRecord(
                    tag=comp.id,
                    sheet_id=sheet.id,
                    role=str(comp.properties.get("role") or _role_from_library_id(raw_lib)),
                    library_id=raw_lib,
                    pins=pin_ids,
                    label=str(comp.properties.get("label") or comp.id),
                    description=str(comp.properties.get("description") or ""),
                    confidence=conf,
                    needs_review=needs_rev,
                    reason_code="imported_low_confidence" if needs_rev else "",
                    x=comp.x,
                    y=comp.y,
                    properties=dict(comp.properties),
                )
            )
        for comp in sheet.components:
            note_txt = str(comp.properties.get("note") or comp.properties.get("text") or comp.properties.get("description") or "").strip()
            if comp.library_id == "iec.notes.block" and note_txt:
                notes.append({"sheet_id": sheet.id, "text": note_txt, "x": comp.x, "y": comp.y})

        # Extract direct sheet wires (including rail connections and device-to-device connections)
        for w_idx, wire in enumerate(sheet.wires):
            ft, fp = split_endpoint_token(wire.frm)
            tt, tp = split_endpoint_token(wire.to)
            if not ft or not tt:
                continue
            rows.append(
                ImportedFromToRow(
                    row_id=f"{sheet.id}:{wire.id or (w_idx + 1)}",
                    wire_number=str(wire.number or wire.net or "").strip(),
                    from_tag=ft,
                    from_pin=fp,
                    to_tag=tt,
                    to_pin=tp,
                    sheet_id=sheet.id,
                    potential=str(wire.net or wire.number or "").strip(),
                    confidence=1.0,
                    needs_review=False,
                )
            )

    # Also include any cross-sheet or net-bridged From-To connections from `from_to_rows`
    existing_pairs: set[tuple[str, str]] = set()
    for r in rows:
        a, b = r.frm.upper(), r.to.upper()
        existing_pairs.add((a, b))
        existing_pairs.add((b, a))

    for ft_row in from_to_rows(project, catalog):
        frm_str = str(ft_row.get("from") or "")
        to_str = str(ft_row.get("to") or "")
        if not frm_str or not to_str:
            continue
        pair = (frm_str.upper(), to_str.upper())
        if pair in existing_pairs:
            # Make sure wire_number is populated on the matching row if it was blank
            w_num = str(ft_row.get("wire_number") or "").strip()
            if w_num:
                for r in rows:
                    if {r.frm.upper(), r.to.upper()} == {frm_str.upper(), to_str.upper()} and not r.wire_number:
                        r.wire_number = w_num
            continue
        ft, fp = split_endpoint_token(frm_str)
        tt, tp = split_endpoint_token(to_str)
        existing_pairs.add(pair)
        existing_pairs.add((pair[1], pair[0]))
        rows.append(
            ImportedFromToRow(
                row_id=f"ft_{len(rows) + 1}",
                wire_number=str(ft_row.get("wire_number") or "").strip(),
                from_tag=ft,
                from_pin=fp,
                to_tag=tt,
                to_pin=tp,
                sheet_id=str(ft_row.get("from_sheet") or "main"),
                potential=str(ft_row.get("wire_number") or "").strip(),
                confidence=1.0,
                needs_review=False,
            )
        )

    draft = ImportedIECPrintDraft(
        source_path=source_path,
        title=project.name or "Imported IEC Print",
        standard="IEC",
        sheets=sheets_meta,
        devices=devices,
        from_to_rows=rows,
        notes=notes,
        confidence_threshold=confidence_threshold,
        source_project_data=project.model_dump(mode="json") if preserve_source_project else None,
    )
    draft.refresh_decisions()
    return draft


_WIRE_LINE_RE = re.compile(
    r"(?:WIRE\s*)?(?P<num>[A-Za-z0-9_+-]+)?\s*[:|-]?\s*"
    r"(?P<frm>-?[A-Za-z0-9_+-]+(?::[A-Za-z0-9_+-]+|\.[A-Za-z0-9_+-]+)?|L\+|0V|L1|L2|L3|PE|N)"
    r"\s*(?:->|→|--|to)\s*"
    r"(?P<to>-?[A-Za-z0-9_+-]+(?::[A-Za-z0-9_+-]+|\.[A-Za-z0-9_+-]+)?|L\+|0V|L1|L2|L3|PE|N)"
    r"(?:\s*\(conf(?:idence)?\s*=\s*(?P<conf>[0-9.]+)\))?",
    re.IGNORECASE,
)

_DEV_TOKEN_RE = re.compile(r"^-?(?:Q|K|KM|KA|KT|F|S|M|H|X|B|Y)\d+(?:[-:][A-Za-z0-9]+)?$")


def _extract_from_pypdf_text(
    pdf_path: Path,
    *,
    confidence_threshold: float = DEFAULT_IMPORT_CONFIDENCE_THRESHOLD,
    catalog: Catalog | None = None,
) -> ImportedIECPrintDraft | None:
    """Fallback text-based extractor using ``pypdf.PdfReader`` for synthetic text PDFs."""

    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    meta = reader.metadata or {}
    raw_pcad = meta.get("/PanelCADProject") if hasattr(meta, "get") else None
    if isinstance(raw_pcad, str) and raw_pcad.strip().startswith("{"):
        try:
            proj_data = json.loads(raw_pcad)
            proj = Project.model_validate(proj_data)
            return _extract_from_panelcad_project(
                proj,
                catalog or default_catalog(),
                source_path=str(pdf_path),
                confidence_threshold=confidence_threshold,
                preserve_source_project=True,
            )
        except Exception:  # noqa: BLE001
            pass
    for meta_key in ("/Subject", "/Keywords", "/Producer", "/CustomFromTo"):
        raw_meta = meta.get(meta_key) if hasattr(meta, "get") else None
        if isinstance(raw_meta, str) and raw_meta.strip().startswith("{"):
            try:
                data = json.loads(raw_meta)
                if isinstance(data, dict) and (data.get("devices") or data.get("from_to") or data.get("wires") or data.get("pages")):
                    sh, devs, rows, notes = _extract_from_vision_payload(
                        data,
                        confidence_threshold=confidence_threshold,
                        catalog=catalog,
                    )
                    draft = ImportedIECPrintDraft(
                        source_path=str(pdf_path),
                        title=str(data.get("title") or pdf_path.stem),
                        sheets=sh,
                        devices=devs,
                        from_to_rows=rows,
                        notes=notes,
                        confidence_threshold=confidence_threshold,
                    )
                    draft.refresh_decisions()
                    return draft
            except Exception:  # noqa: BLE001
                pass

    sheets_meta: list[dict[str, Any]] = []
    devices: list[ImportedDeviceRecord] = []
    rows: list[ImportedFromToRow] = []
    notes: list[dict[str, Any]] = []
    seen_devs: set[tuple[str, str]] = set()

    for idx, page in enumerate(reader.pages):
        sh_id = f"page_{idx + 1}"
        text = page.extract_text() or ""
        sheets_meta.append({"id": sh_id, "title": f"Sheet {idx + 1}", "kind": "schematic"})
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m = _WIRE_LINE_RE.search(line)
            if m and ("->" in line or "→" in line or " to " in line.lower()):
                w_num = str(m.group("num") or "").strip()
                ft, fp = split_endpoint_token(m.group("frm"))
                tt, tp = split_endpoint_token(m.group("to"))
                conf = float(m.group("conf")) if m.group("conf") else 1.0
                if ft and tt:
                    row = ImportedFromToRow(
                        row_id=f"row_{len(rows) + 1}",
                        wire_number=w_num,
                        from_tag=ft,
                        from_pin=fp,
                        to_tag=tt,
                        to_pin=tp,
                        sheet_id=sh_id,
                        potential=w_num,
                        confidence=conf,
                        needs_review=conf < confidence_threshold,
                        reason_code="imported_low_confidence" if conf < confidence_threshold else "",
                    )
                    rows.append(row)
                    continue
            for token in line.split():
                tok_clean = token.strip(",;()")
                if _DEV_TOKEN_RE.match(tok_clean):
                    tag = clean_iec_tag(tok_clean)
                    if (sh_id, tag) not in seen_devs:
                        seen_devs.add((sh_id, tag))
                        devices.append(ImportedDeviceRecord(tag=tag, sheet_id=sh_id))

    if not devices and not rows:
        return None

    # Sync any devices referenced only in rows
    known_tags = {d.tag: d for d in devices}
    for r in rows:
        for t, p in ((r.from_tag, r.from_pin), (r.to_tag, r.to_pin)):
            if not t or t in _RAIL_NAMES:
                continue
            if t not in known_tags:
                dev = ImportedDeviceRecord(tag=t, sheet_id=r.sheet_id, pins=[p] if p else [])
                devices.append(dev)
                known_tags[t] = dev
            elif p and p not in known_tags[t].pins:
                known_tags[t].pins.append(p)

    draft = ImportedIECPrintDraft(
        source_path=str(pdf_path),
        title=pdf_path.stem,
        sheets=sheets_meta,
        devices=devices,
        from_to_rows=rows,
        notes=notes,
        confidence_threshold=confidence_threshold,
    )
    draft.refresh_decisions()
    return draft


def _call_vision_backend(
    vision_backend: Any,
    path: Path,
) -> dict[str, Any] | None:
    """Invoke a pluggable vision backend (callable, dict, RecordedImportVisionBackend, FakeVision, etc.)."""

    if vision_backend is None:
        return None
    if isinstance(vision_backend, RecordedImportVisionBackend):
        return vision_backend.extract_print(path)
    if isinstance(vision_backend, dict):
        if path.name in vision_backend and isinstance(vision_backend[path.name], dict):
            return dict(vision_backend[path.name])
        return dict(vision_backend)
    if callable(vision_backend):
        res = vision_backend(path)
        if isinstance(res, dict):
            return dict(res)
    if hasattr(vision_backend, "extract_print") and callable(vision_backend.extract_print):
        res = vision_backend.extract_print(path)
        if isinstance(res, dict):
            return dict(res)
    if hasattr(vision_backend, "recordings") and isinstance(vision_backend.recordings, dict):
        rec = vision_backend.recordings
        if path.name in rec and isinstance(rec[path.name], dict):
            return dict(rec[path.name])
        if "*" in rec and isinstance(rec["*"], dict):
            return dict(rec["*"])
        if any(k in rec for k in ("devices", "from_to", "from_to_rows", "wires", "pages")):
            return dict(rec)
    if hasattr(vision_backend, "extract") and callable(vision_backend.extract):
        try:
            res = vision_backend.extract("from_to", path, {"path": str(path)})
            if isinstance(res, dict) and any(k in res for k in ("devices", "from_to", "from_to_rows", "wires", "pages")):
                return dict(res)
        except Exception:  # noqa: BLE001
            pass
    return None


def import_iec_print_to_from_to(
    path_or_paths: Path | str | Sequence[Path | str] | dict[str, Any] | ImportedIECPrintDraft,
    *,
    vision_backend: Any = None,
    confidence_threshold: float = DEFAULT_IMPORT_CONFIDENCE_THRESHOLD,
    catalog: Catalog | None = None,
    workspace: Path | str | None = None,
) -> ImportedIECPrintDraft:
    """Stage 1: Extract IEC components and a draft From-To wire list from an IEC PDF or scan.

    Never uses or requires Tesseract or Poppler. Uses PanelCAD's native PDF vector/text
    reader (`pypdf` / `panelcad.importer`) and `panelcad.importer.vision` backends.
    """

    cat = catalog or default_catalog()

    if isinstance(path_or_paths, ImportedIECPrintDraft):
        path_or_paths.confidence_threshold = confidence_threshold
        path_or_paths.refresh_decisions()
        return path_or_paths

    if isinstance(path_or_paths, dict):
        sh, devs, rows, notes = _extract_from_vision_payload(
            path_or_paths,
            confidence_threshold=confidence_threshold,
            catalog=cat,
        )
        draft = ImportedIECPrintDraft(
            source_path=str(path_or_paths.get("source_path") or ""),
            title=str(path_or_paths.get("title") or "Imported IEC Print"),
            sheets=sh,
            devices=devs,
            from_to_rows=rows,
            notes=notes,
            confidence_threshold=confidence_threshold,
        )
        draft.refresh_decisions()
        return draft

    if isinstance(path_or_paths, (Path, str)):
        paths = [Path(path_or_paths)]
    else:
        paths = [Path(p) for p in path_or_paths]

    if not paths:
        raise ValueError("No input path provided to import_iec_print_to_from_to")
    primary = paths[0]
    if not primary.exists():
        raise FileNotFoundError(f"IEC print file not found: {primary}")

    # 1. Direct JSON / YAML project or recorded payload file
    suffix = primary.suffix.lower()
    if suffix == ".json":
        payload = json.loads(primary.read_text(encoding="utf-8"))
        sh, devs, rows, notes = _extract_from_vision_payload(
            payload,
            confidence_threshold=confidence_threshold,
            catalog=cat,
        )
        draft = ImportedIECPrintDraft(
            source_path=str(primary),
            title=str(payload.get("title") or primary.stem),
            sheets=sh,
            devices=devs,
            from_to_rows=rows,
            notes=notes,
            confidence_threshold=confidence_threshold,
        )
        draft.refresh_decisions()
        return draft

    if suffix in {".yaml", ".yml"}:
        from panelcad.api.session import Session

        sess = Session.open(primary)
        return _extract_from_panelcad_project(
            sess.project,
            sess.catalog,
            source_path=str(primary),
            confidence_threshold=confidence_threshold,
            preserve_source_project=True,
        )

    # 2. If a vision_backend is provided with direct structured extraction for this scan/PDF
    if vision_backend is not None:
        vision_payload = _call_vision_backend(vision_backend, primary)
        if vision_payload and any(k in vision_payload for k in ("devices", "from_to", "from_to_rows", "wires", "pages")):
            sh, devs, rows, notes = _extract_from_vision_payload(
                vision_payload,
                confidence_threshold=confidence_threshold,
                catalog=cat,
            )
            draft = ImportedIECPrintDraft(
                source_path=str(primary),
                title=str(vision_payload.get("title") or primary.stem),
                sheets=sh,
                devices=devs,
                from_to_rows=rows,
                notes=notes,
                confidence_threshold=confidence_threshold,
            )
            draft.refresh_decisions()
            return draft

    # 3. For PDFs, try PanelCAD's native vector PDF importer first, then pypdf text fallback
    if suffix == ".pdf":
        # Check embedded pypdf metadata / text wire definitions first if present
        pypdf_draft = _extract_from_pypdf_text(
            primary,
            confidence_threshold=confidence_threshold,
            catalog=cat,
        )
        if pypdf_draft is not None and (pypdf_draft.source_project_data is not None or pypdf_draft.from_to_rows):
            return pypdf_draft

        try:
            from panelcad.importer.pipeline import ImportJob

            with tempfile.TemporaryDirectory(prefix="pcad-iec-import-") as tmp_dir:
                ws_dir = Path(workspace) if workspace else (Path(tmp_dir) / "ws")
                job = ImportJob.start(paths, ws_dir, backend=vision_backend or "external")
                status = job.status()
                if not status.get("pending"):
                    built = job.build()
                    draft = _extract_from_panelcad_project(
                        built.project,
                        built.catalog,
                        source_path=str(primary),
                        confidence_threshold=confidence_threshold,
                    )
                    if draft.devices or draft.from_to_rows:
                        return draft
        except Exception:  # noqa: BLE001
            pass

        if pypdf_draft is not None:
            return pypdf_draft

    # 4. For raster scans (.png, .jpg, etc.) or non-vector PDFs with an ImportJob vision backend
    try:
        from panelcad.importer.pipeline import ImportJob

        with tempfile.TemporaryDirectory(prefix="pcad-scan-import-") as tmp_dir:
            ws_dir = Path(workspace) if workspace else (Path(tmp_dir) / "ws")
            job = ImportJob.start(paths, ws_dir, backend=vision_backend or "fake")
            status = job.status()
            if not status.get("pending"):
                built = job.build()
                draft = _extract_from_panelcad_project(
                    built.project,
                    built.catalog,
                    source_path=str(primary),
                    confidence_threshold=confidence_threshold,
                )
                # Also pull any low-confidence review items from job.review_items()
                for item in job.review_items():
                    conf = float(item.get("confidence", 1.0) or 1.0)
                    if conf < confidence_threshold or item.get("reason") in {"low_confidence", "ambiguous", "unreadable"}:
                        txt = clean_iec_tag(str(item.get("text") or ""))
                        draft.decisions.append(
                            ConversionDecision(
                                reason_code="imported_low_confidence",
                                component_id=txt or None,
                                sheet_id=str(item.get("page_id") or "main"),
                                before={"text": txt, "kind": item.get("kind")},
                                after={"text": txt},
                                needs_review=True,
                                confidence=conf,
                                message=f"Low-confidence scan item '{txt or item.get('id')}' ({conf:.2f}).",
                                chunk_id="import",
                            )
                        )
                return draft
    except Exception:  # noqa: BLE001
        pass

    return ImportedIECPrintDraft(
        source_path=str(primary),
        title=primary.stem,
        confidence_threshold=confidence_threshold,
    )


def _match_row_index(rows: list[ImportedFromToRow], selector: Any) -> int | None:
    """Locate a row index in ``rows`` by index, ``row_id``, ``wire_number``, or endpoint match."""

    if isinstance(selector, int):
        return selector if 0 <= selector < len(rows) else None
    if isinstance(selector, str):
        sel = selector.strip()
        for idx, r in enumerate(rows):
            if r.row_id == sel or r.wire_number == sel:
                return idx
            if f"{r.frm}->{r.to}" == sel.replace(" ", ""):
                return idx
        return None
    if isinstance(selector, dict):
        if "index" in selector and isinstance(selector["index"], int):
            idx = selector["index"]
            if 0 <= idx < len(rows):
                return idx
        if selector.get("row_id"):
            for idx, r in enumerate(rows):
                if r.row_id == str(selector["row_id"]):
                    return idx
        # Match by match_from / match_to or old_from / old_to or from / to or wire_number
        m_wire = str(selector.get("match_wire_number") or selector.get("old_wire_number") or "").strip()
        m_from = str(selector.get("match_from") or selector.get("old_from") or "").strip()
        m_to = str(selector.get("match_to") or selector.get("old_to") or "").strip()
        if m_from or m_to or m_wire:
            ft_m, fp_m = split_endpoint_token(m_from) if m_from else ("", "")
            tt_m, tp_m = split_endpoint_token(m_to) if m_to else ("", "")
            for idx, r in enumerate(rows):
                if m_wire and r.wire_number != m_wire:
                    continue
                if ft_m and r.from_tag != ft_m:
                    continue
                if fp_m and r.from_pin != fp_m:
                    continue
                if tt_m and r.to_tag != tt_m:
                    continue
                if tp_m and r.to_pin != tp_m:
                    continue
                return idx
        if selector.get("wire_number"):
            w_target = str(selector["wire_number"]).strip()
            matches = [idx for idx, r in enumerate(rows) if r.wire_number == w_target]
            if len(matches) == 1:
                return matches[0]
            # If multiple rows share a wire number, disambiguate by from_tag or to_tag
            s_from_tag = clean_iec_tag(str(selector.get("from_tag") or ""))
            s_to_tag = clean_iec_tag(str(selector.get("to_tag") or ""))
            for idx in matches:
                if (s_from_tag and rows[idx].from_tag == s_from_tag) or (s_to_tag and rows[idx].to_tag == s_to_tag):
                    return idx
            if matches:
                return matches[0]
    return None


def _apply_single_row_edit(row: ImportedFromToRow, edit: dict[str, Any]) -> None:
    """Mutate ``row`` in-place according to ``edit`` and clear ``needs_review`` unless explicitly kept."""

    if "wire_number" in edit or "number" in edit:
        row.wire_number = str(edit.get("wire_number") if "wire_number" in edit else edit.get("number") or "").strip()
    if "from" in edit or "frm" in edit or "new_from" in edit:
        raw_frm = str(edit.get("new_from") or edit.get("from") or edit.get("frm") or "")
        ft, fp = split_endpoint_token(raw_frm)
        row.from_tag, row.from_pin = ft, fp
    if "to" in edit or "new_to" in edit:
        raw_to = str(edit.get("new_to") or edit.get("to") or "")
        tt, tp = split_endpoint_token(raw_to)
        row.to_tag, row.to_pin = tt, tp
    if "from_tag" in edit:
        row.from_tag = clean_iec_tag(str(edit["from_tag"]))
    if "from_pin" in edit:
        row.from_pin = str(edit["from_pin"]).strip()
    if "to_tag" in edit:
        row.to_tag = clean_iec_tag(str(edit["to_tag"]))
    if "to_pin" in edit:
        row.to_pin = str(edit["to_pin"]).strip()
    if "sheet_id" in edit or "sheet" in edit:
        row.sheet_id = str(edit.get("sheet_id") or edit.get("sheet") or row.sheet_id).strip()
    if "potential" in edit:
        row.potential = str(edit["potential"]).strip()
    if "note" in edit:
        row.note = str(edit["note"])

    # User correction resolves low-confidence review state unless needs_review=True is explicitly passed
    row.confidence = float(edit.get("confidence", 1.0))
    row.needs_review = bool(edit.get("needs_review", False))
    row.reason_code = "imported_low_confidence" if row.needs_review else ""


def apply_from_to_corrections(
    draft: ImportedIECPrintDraft | dict[str, Any],
    corrections: dict[str, Any] | list[dict[str, Any]] | None = None,
    *,
    edits: list[dict[str, Any]] | None = None,
    add_rows: list[dict[str, Any] | ImportedFromToRow] | None = None,
    delete_rows: list[Any] | None = None,
    confirm_rows: list[Any] | None = None,
    replace_rows: list[dict[str, Any] | ImportedFromToRow] | None = None,
    device_edits: list[dict[str, Any]] | None = None,
    catalog: Catalog | None = None,
) -> ImportedIECPrintDraft:
    """Stage 2: Apply user review/corrections to the extracted From-To rows and device list.

    Edits or confirmed rows have their confidence set to 1.0 and ``needs_review`` cleared.
    Any edited endpoints invalidate ``source_project_data`` so the IEC project is freshly
    reconstructed from the corrected From-To table.
    """

    cat = catalog or default_catalog()
    working = ImportedIECPrintDraft.from_dict(draft)
    mutated = False

    if isinstance(corrections, list):
        # If every entry has from/to or from_tag/to_tag and no match selector, check if it's edit vs replace
        has_selectors = any(
            isinstance(c, dict)
            and any(k in c for k in ("index", "row_id", "match_from", "match_to", "old_from", "old_to", "action", "op"))
            for c in corrections
        )
        if has_selectors:
            edits = list(edits or []) + list(corrections)
        else:
            # Check if corrections match existing wire_numbers for targeted edits
            all_match_existing = all(
                isinstance(c, dict) and _match_row_index(working.from_to_rows, c) is not None
                for c in corrections
            )
            if all_match_existing and len(corrections) < len(working.from_to_rows):
                edits = list(edits or []) + list(corrections)
            elif all_match_existing:
                edits = list(edits or []) + list(corrections)
            else:
                replace_rows = list(corrections)
    elif isinstance(corrections, dict):
        if "replace_rows" in corrections or "from_to_rows" in corrections:
            replace_rows = corrections.get("replace_rows") or corrections.get("from_to_rows")
        if "edits" in corrections:
            edits = list(edits or []) + list(corrections.get("edits") or [])
        if "add_rows" in corrections or "add" in corrections:
            add_rows = list(add_rows or []) + list(corrections.get("add_rows") or corrections.get("add") or [])
        if "delete_rows" in corrections or "delete" in corrections:
            delete_rows = list(delete_rows or []) + list(corrections.get("delete_rows") or corrections.get("delete") or [])
        if "confirm_rows" in corrections or "confirm" in corrections:
            confirm_rows = list(confirm_rows or []) + list(corrections.get("confirm_rows") or corrections.get("confirm") or [])
        if "devices" in corrections or "device_edits" in corrections:
            device_edits = list(device_edits or []) + list(
                corrections.get("device_edits") or corrections.get("devices") or []
            )
        # Support single-edit shorthand dict passed directly as corrections
        if any(
            k in corrections
            for k in ("row_id", "index", "wire_number", "from_tag", "to_tag", "from", "to", "new_from", "new_to")
        ) and not any(
            k in corrections
            for k in ("edits", "add_rows", "delete_rows", "confirm_rows", "replace_rows", "from_to_rows")
        ):
            edits = list(edits or []) + [corrections]

    if replace_rows is not None:
        mutated = True
        new_rows: list[ImportedFromToRow] = []
        for idx, raw_r in enumerate(replace_rows):
            row = ImportedFromToRow.from_dict(raw_r)
            if not row.row_id:
                row.row_id = f"row_{idx + 1}"
            new_rows.append(row)
        working.from_to_rows = new_rows

    if delete_rows:
        mutated = True
        to_delete: set[int] = set()
        for sel in delete_rows:
            idx = _match_row_index(working.from_to_rows, sel)
            if idx is not None:
                to_delete.add(idx)
        working.from_to_rows = [r for i, r in enumerate(working.from_to_rows) if i not in to_delete]

    if edits:
        mutated = True
        for edit in edits:
            op = str(edit.get("op") or edit.get("action") or "edit").lower()
            if op == "delete":
                idx = _match_row_index(working.from_to_rows, edit)
                if idx is not None:
                    working.from_to_rows.pop(idx)
                continue
            if op == "add":
                row = ImportedFromToRow.from_dict(edit)
                row.confidence = float(edit.get("confidence", 1.0))
                row.needs_review = bool(edit.get("needs_review", False))
                row.reason_code = "imported_low_confidence" if row.needs_review else ""
                if not row.row_id:
                    row.row_id = f"row_{len(working.from_to_rows) + 1}"
                working.from_to_rows.append(row)
                continue
            if op == "confirm":
                idx = _match_row_index(working.from_to_rows, edit)
                if idx is not None:
                    working.from_to_rows[idx].confidence = 1.0
                    working.from_to_rows[idx].needs_review = False
                    working.from_to_rows[idx].reason_code = ""
                continue

            idx = _match_row_index(working.from_to_rows, edit)
            if idx is not None:
                _apply_single_row_edit(working.from_to_rows[idx], edit)
            else:
                # If no existing row matched, add as a new corrected row
                row = ImportedFromToRow.from_dict(edit)
                row.confidence = float(edit.get("confidence", 1.0))
                row.needs_review = bool(edit.get("needs_review", False))
                row.reason_code = "imported_low_confidence" if row.needs_review else ""
                if not row.row_id:
                    row.row_id = f"row_{len(working.from_to_rows) + 1}"
                working.from_to_rows.append(row)

    if add_rows:
        mutated = True
        for raw_add in add_rows:
            row = ImportedFromToRow.from_dict(raw_add)
            if isinstance(raw_add, dict) and "needs_review" not in raw_add and "confidence" not in raw_add:
                row.confidence = 1.0
                row.needs_review = False
                row.reason_code = ""
            if not row.row_id:
                row.row_id = f"row_{len(working.from_to_rows) + 1}"
            working.from_to_rows.append(row)

    if confirm_rows:
        for sel in confirm_rows:
            if sel == "*" or sel == "all":
                for r in working.from_to_rows:
                    r.confidence = 1.0
                    r.needs_review = False
                    r.reason_code = ""
                for d in working.devices:
                    d.confidence = 1.0
                    d.needs_review = False
                    d.reason_code = ""
            else:
                idx = _match_row_index(working.from_to_rows, sel)
                if idx is not None:
                    working.from_to_rows[idx].confidence = 1.0
                    working.from_to_rows[idx].needs_review = False
                    working.from_to_rows[idx].reason_code = ""
                elif isinstance(sel, str):
                    clean_sel = clean_iec_tag(sel)
                    for d in working.devices:
                        if d.tag == clean_sel:
                            d.confidence = 1.0
                            d.needs_review = False
                            d.reason_code = ""

    if device_edits:
        mutated = True
        by_tag = {d.tag: d for d in working.devices}
        for dev_edit in device_edits:
            old_tag = clean_iec_tag(str(dev_edit.get("old_tag") or dev_edit.get("match_tag") or dev_edit.get("tag") or ""))
            new_tag = clean_iec_tag(str(dev_edit.get("new_tag") or dev_edit.get("tag") or old_tag))
            if old_tag in by_tag:
                dev = by_tag[old_tag]
                if new_tag and new_tag != old_tag:
                    dev.tag = new_tag
                    for r in working.from_to_rows:
                        if r.from_tag == old_tag:
                            r.from_tag = new_tag
                        if r.to_tag == old_tag:
                            r.to_tag = new_tag
                if "role" in dev_edit:
                    dev.role = str(dev_edit["role"])
                if "library_id" in dev_edit:
                    dev.library_id = str(dev_edit["library_id"])
                elif "role" in dev_edit:
                    dev.library_id, dev.role = _infer_iec_library_id(dev.tag, role=dev.role, pins=dev.pins, catalog=cat)
                if "label" in dev_edit:
                    dev.label = str(dev_edit["label"])
                dev.confidence = float(dev_edit.get("confidence", 1.0))
                dev.needs_review = bool(dev_edit.get("needs_review", False))
                dev.reason_code = "imported_low_confidence" if dev.needs_review else ""
            elif new_tag:
                new_dev = ImportedDeviceRecord.from_dict({**dev_edit, "tag": new_tag})
                new_dev.confidence = float(dev_edit.get("confidence", 1.0))
                new_dev.needs_review = bool(dev_edit.get("needs_review", False))
                working.devices.append(new_dev)
                by_tag[new_tag] = new_dev

    if mutated:
        working.source_project_data = None

    # Synchronize device records with any new tags or pins introduced by From-To edits
    by_tag = {d.tag: d for d in working.devices}
    referenced_tags: set[str] = set()
    for r in working.from_to_rows:
        for t, p in ((r.from_tag, r.from_pin), (r.to_tag, r.to_pin)):
            if not t or t in _RAIL_NAMES:
                continue
            referenced_tags.add(t)
            if t not in by_tag:
                lib_id, role = _infer_iec_library_id(t, pins=[p] if p else [], catalog=cat)
                dev = ImportedDeviceRecord(
                    tag=t,
                    sheet_id=r.sheet_id,
                    role=role,
                    library_id=lib_id,
                    pins=[p] if p else [],
                    confidence=1.0,
                    needs_review=False,
                )
                working.devices.append(dev)
                by_tag[t] = dev
            else:
                if p and p not in by_tag[t].pins:
                    by_tag[t].pins.append(p)

    # Remove orphan auto-generated device tags that were replaced by a typo correction
    if mutated:
        working.devices = [
            d for d in working.devices if d.tag in referenced_tags or not working.from_to_rows
        ]

    working.refresh_decisions()
    return working


def _is_power_device(dev: ImportedDeviceRecord) -> bool:
    lid = str(dev.library_id or "").lower()
    if any(
        tok in lid
        for tok in (
            ".breakers.",
            ".contactors.ac3",
            ".protection.overload_",
            ".protection.disconnect",
            ".protection.fuse_3p",
            ".loads.motor",
            ".drives.",
        )
    ):
        return True
    pin_set = {str(p).upper() for p in dev.pins if p}
    if pin_set & {"L1", "L2", "L3", "T1", "T2", "T3", "U1", "V1", "W1", "3", "4", "5", "6"}:
        return True
    return False


def _is_power_row(row: ImportedFromToRow, dev_map: dict[str, ImportedDeviceRecord]) -> bool:
    if row.from_tag in _POWER_RAILS or row.to_tag in _POWER_RAILS:
        return True
    if row.from_pin.upper() in _POWER_PINS or row.to_pin.upper() in _POWER_PINS:
        # Check that it's not a 1-pole fuse or terminal unless connected to power device
        f_dev = dev_map.get(row.from_tag)
        t_dev = dev_map.get(row.to_tag)
        if (f_dev and _is_power_device(f_dev)) or (t_dev and _is_power_device(t_dev)):
            return True
    if row.wire_number.upper() in {"L1", "L2", "L3", "T1", "T2", "T3", "U1", "V1", "W1", "PE"}:
        return True
    return False


def _resolve_component_pin(
    comp_id: str,
    raw_pin: str,
    library_id: str,
    catalog: Catalog,
    used_pins: dict[str, set[str]],
) -> tuple[str, dict[str, str]]:
    """Map an imported pin label onto an actual symbol pin id, returning ``(pin_id, pin_label_overrides)``."""

    pin = str(raw_pin or "").strip()
    overrides: dict[str, str] = {}
    try:
        dev = catalog.get(library_id)
    except Exception:  # noqa: BLE001
        return (pin or "1", overrides)

    valid_ids = [p.id for p in dev.pins]
    if not valid_ids:
        return (pin, overrides)
    if pin in valid_ids:
        used_pins.setdefault(comp_id, set()).add(pin)
        return (pin, overrides)
    upper_map = {p.upper(): p for p in valid_ids}
    if pin.upper() in upper_map:
        actual = upper_map[pin.upper()]
        used_pins.setdefault(comp_id, set()).add(actual)
        return (actual, overrides)

    # Map IEC auxiliary contact numbers (23/24, 33/34, 43/44 -> 13/14; 11/12, 31/32, 41/42 -> 21/22)
    if valid_ids == ["13", "14"]:
        if pin.endswith("3") or pin in {"1", "13", "23", "33", "43"}:
            actual = "13"
        elif pin.endswith("4") or pin in {"2", "14", "24", "34", "44"}:
            actual = "14"
        else:
            used = used_pins.get(comp_id, set())
            actual = "13" if "13" not in used else "14"
        if pin and pin != actual:
            overrides[actual] = pin
        used_pins.setdefault(comp_id, set()).add(actual)
        return (actual, overrides)

    if valid_ids == ["21", "22"]:
        if pin.endswith("1") or pin in {"1", "11", "21", "31", "41"}:
            actual = "21"
        elif pin.endswith("2") or pin in {"2", "12", "22", "32", "42"}:
            actual = "22"
        else:
            used = used_pins.get(comp_id, set())
            actual = "21" if "21" not in used else "22"
        if pin and pin != actual:
            overrides[actual] = pin
        used_pins.setdefault(comp_id, set()).add(actual)
        return (actual, overrides)

    if valid_ids == ["A", "B"]:
        used = used_pins.get(comp_id, set())
        actual = "A" if "A" not in used else "B"
        used_pins.setdefault(comp_id, set()).add(actual)
        return (actual, overrides)

    used = used_pins.get(comp_id, set())
    for cand in valid_ids:
        if cand not in used:
            used_pins.setdefault(comp_id, set()).add(cand)
            if pin and pin != cand:
                overrides[cand] = pin
            return (cand, overrides)
    return (valid_ids[0], overrides)


def build_iec_project_from_imported_from_to(
    draft: ImportedIECPrintDraft | dict[str, Any],
    *,
    name: str | None = None,
    catalog: Catalog | None = None,
):
    """Stage 2b: Reconstruct a native PanelCAD IEC ``Session`` from the reviewed From-To draft.

    - If the draft originated from a vector PanelCAD PDF and no destructive edits were made,
      preserves the exact sheet geometry while attaching any ``imported_from_to_decisions``.
    - Otherwise constructs clean 3-phase power and/or control sheets in IEC standard with
      non-overlapping coordinates, proper child contact metadata (`parent`, `terminals`,
      `pin_labels`), and wires every reviewed From-To connection.
    """

    from panelcad.api.session import Session

    cat = catalog or default_catalog()
    working = ImportedIECPrintDraft.from_dict(draft)
    working.refresh_decisions()
    unresolved_decs = [d.to_dict() for d in working.decisions if d.needs_review]

    # Fast path if source_project_data is intact and was not invalidated by user edits
    if isinstance(working.source_project_data, dict) and working.source_project_data.get("sheets"):
        proj = Project.model_validate(working.source_project_data)
        if name:
            proj.name = name
        proj.properties["standard"] = "IEC"
        for sh in proj.sheets:
            if sh.kind in {"schematic", ""} and sh.drawing_style not in {"iec", "nema"}:
                sh.drawing_style = "iec"
        if unresolved_decs:
            proj.properties["imported_from_to_decisions"] = unresolved_decs
            proj.properties["last_conversion_decisions"] = list(unresolved_decs)
        else:
            proj.properties.pop("imported_from_to_decisions", None)
            proj.properties.pop("last_conversion_decisions", None)
        proj.properties["imported_from_to"] = working.to_dict()
        return Session(project=proj, catalog=cat)

    sess = Session.create(name or working.title or "Imported IEC Print")
    sess.project.properties["standard"] = "IEC"

    # Check if we need to split devices that appear in both 3-phase power rows and 24V control coil/aux rows
    dev_map: dict[str, ImportedDeviceRecord] = {d.tag: ImportedDeviceRecord.from_dict(d) for d in working.devices}
    rows = [ImportedFromToRow.from_dict(r) for r in working.from_to_rows]

    # Detect tags like K1 or F1 that have both 3-phase power pins (1..6, L1..T3) AND control pins (A1/A2, 13/14, 95/96)
    pins_per_tag: dict[str, set[str]] = {}
    for r in rows:
        for t, p in ((r.from_tag, r.from_pin), (r.to_tag, r.to_pin)):
            if t and t not in _RAIL_NAMES and p:
                pins_per_tag.setdefault(t, set()).add(p.upper())

    for tag, pset in list(pins_per_tag.items()):
        has_power = bool(pset & _POWER_PINS)
        has_coil = bool(pset & {"A1", "A2"})
        has_aux_no = bool(pset & {"13", "14", "23", "24", "33", "34", "43", "44"})
        has_aux_nc = bool(pset & {"11", "12", "21", "22", "31", "32", "41", "42"})
        has_ol_nc = bool(pset & {"95", "96"})

        # If a contactor K1 is used in control rows with 13/14 AND A1/A2 (or power), split auxiliary contact into K1-14
        if (has_coil or has_power) and has_aux_no:
            aux_tag = f"{tag}-14"
            if aux_tag not in dev_map:
                dev_map[aux_tag] = ImportedDeviceRecord(
                    tag=aux_tag,
                    sheet_id="control",
                    role="aux_no",
                    library_id="iec.contactors.aux_no",
                    pins=["13", "14"],
                    properties={"parent": tag, "terminals": "13-14"},
                )
            for r in rows:
                if r.from_tag == tag and r.from_pin.upper() in {"13", "14", "23", "24", "33", "34", "43", "44"}:
                    r.from_tag = aux_tag
                if r.to_tag == tag and r.to_pin.upper() in {"13", "14", "23", "24", "33", "34", "43", "44"}:
                    r.to_tag = aux_tag

        if (has_coil or has_power) and has_aux_nc:
            aux_nc_tag = f"{tag}-21"
            if aux_nc_tag not in dev_map:
                dev_map[aux_nc_tag] = ImportedDeviceRecord(
                    tag=aux_nc_tag,
                    sheet_id="control",
                    role="aux_nc",
                    library_id="iec.contactors.aux_nc",
                    pins=["21", "22"],
                    properties={"parent": tag, "terminals": "21-22"},
                )
            for r in rows:
                if r.from_tag == tag and r.from_pin.upper() in {"11", "12", "21", "22", "31", "32", "41", "42"}:
                    r.from_tag = aux_nc_tag
                if r.to_tag == tag and r.to_pin.upper() in {"11", "12", "21", "22", "31", "32", "41", "42"}:
                    r.to_tag = aux_nc_tag

        if has_power and has_ol_nc:
            ol_tag = f"{tag}-95"
            if ol_tag not in dev_map:
                dev_map[ol_tag] = ImportedDeviceRecord(
                    tag=ol_tag,
                    sheet_id="control",
                    role="overload",
                    library_id="iec.protection.ol_contact",
                    pins=["95", "96"],
                    properties={"parent": tag, "terminals": "95-96"},
                )
            for r in rows:
                if r.from_tag == tag and r.from_pin.upper() in {"95", "96"}:
                    r.from_tag = ol_tag
                if r.to_tag == tag and r.to_pin.upper() in {"95", "96"}:
                    r.to_tag = ol_tag

    power_rows = [r for r in rows if _is_power_row(r, dev_map)]
    control_rows = [r for r in rows if r not in power_rows]

    power_tags: set[str] = set()
    for r in power_rows:
        for t in (r.from_tag, r.to_tag):
            if t and t not in _RAIL_NAMES:
                power_tags.add(t)
    for d in dev_map.values():
        if _is_power_device(d) and d.tag not in {r.from_tag for r in control_rows} | {r.to_tag for r in control_rows}:
            power_tags.add(d.tag)

    control_tags: set[str] = set()
    for r in control_rows:
        for t in (r.from_tag, r.to_tag):
            if t and t not in _RAIL_NAMES:
                control_tags.add(t)
    for d in dev_map.values():
        if d.tag not in power_tags:
            control_tags.add(d.tag)

    # If a contactor (e.g. K1) is in both power_tags (L1..T3) and control_tags (A1/A2),
    # keep K1 as ac3_9a on power sheet and place K1 coil or keep K1 on control if no power sheet.
    both_tags = power_tags & control_tags
    for bt in list(both_tags):
        # In PanelCAD component IDs are unique per project unless split; if K1 has power poles AND A1/A2 coil on control,
        # check if we should keep power + control onseparate sheets or single sheet.
        # Wait: in PanelCAD `Component.id` is unique per sheet, and `convert_standard` handles K1 on power sheet + K1 coil or K1 on one sheet!
        # Wait: let's check if `check_project` allows duplicate component IDs across sheets.
        # `check_project` checks `duplicate_component_id` across the project!
        # So in PanelCAD, a contactor on the power sheet is `KM1` or `K1` and on the control sheet its coil is `K1` (if power is `KM1`) or if power is `K1`, `iec.contactors.ac3_9a` has pins `L1..T3` AND `A1, A2, 13, 14`!
        pass

    sheets_created: list[Sheet] = []
    used_pins: dict[str, set[str]] = {}

    # Determine sheet partitioning
    if power_tags:
        # Clean separation between 3-phase power sheet and control sheet(s)
        power_sheet = sess.project.sheets[0]
        power_sheet.id = "power"
        power_sheet.title = "Power"
        power_sheet.drawing_style = "iec"
        power_sheet.rail_hot = ""
        power_sheet.rail_return = ""
        sheets_created.append(power_sheet)

        # Order power devices top-to-bottom: breaker (Q) -> contactor (K/KM) -> overload (F) -> motor (M)
        def _power_sort_key(tag: str) -> tuple[int, str]:
            d = dev_map.get(tag)
            lid = (d.library_id if d else "").lower()
            if "breaker" in lid or "disconnect" in lid or tag.startswith("Q"):
                return (0, tag)
            if "contactor" in lid or tag.startswith("K"):
                return (1, tag)
            if "overload" in lid or tag.startswith("F"):
                return (2, tag)
            if "motor" in lid or tag.startswith("M"):
                return (3, tag)
            return (4, tag)

        ordered_power = sorted(power_tags, key=_power_sort_key)
        # Group into feeder columns (each column has at most one Q, one K, one F, one M)
        power_y_slots = [55.0, 105.0, 150.0, 200.0]
        col_slots: list[dict[int, str]] = []
        for ptag in ordered_power:
            rank = _power_sort_key(ptag)[0]
            slot_idx = min(rank, 3)
            placed = False
            for col in col_slots:
                if slot_idx not in col:
                    col[slot_idx] = ptag
                    placed = True
                    break
            if not placed:
                col_slots.append({slot_idx: ptag})

        for c_idx, col in enumerate(col_slots):
            cx = 100.0 + c_idx * 90.0
            for s_idx, ptag in sorted(col.items()):
                d = dev_map.get(ptag) or ImportedDeviceRecord(tag=ptag, sheet_id="power")
                lib_id, role = _infer_iec_library_id(
                    ptag,
                    role=d.role,
                    library_id=d.library_id,
                    pins=d.pins,
                    catalog=cat,
                )
                if lib_id == "iec.contactors.coil":
                    lib_id = "iec.contactors.ac3_9a"
                    role = "contactor"
                props = dict(d.properties)
                if role:
                    props.setdefault("role", role)
                if d.label:
                    props.setdefault("label", d.label)
                if d.description:
                    props.setdefault("description", d.description)
                power_sheet.components.append(
                    Component(
                        id=ptag,
                        library_id=lib_id,
                        x=cx,
                        y=power_y_slots[s_idx],
                        properties=props,
                    )
                )

        for w_idx, r in enumerate(power_rows):
            ft, fp = r.from_tag, r.from_pin
            tt, tp = r.to_tag, r.to_pin
            if ft not in _RAIL_NAMES:
                comp_f = power_sheet.component(ft)
                fp, ov_f = _resolve_component_pin(ft, fp, comp_f.library_id, cat, used_pins)
                if ov_f:
                    lbls = dict(comp_f.properties.get("pin_labels") or {})
                    lbls.update(ov_f)
                    comp_f.properties["pin_labels"] = lbls
            if tt not in _RAIL_NAMES:
                comp_t = power_sheet.component(tt)
                tp, ov_t = _resolve_component_pin(tt, tp, comp_t.library_id, cat, used_pins)
                if ov_t:
                    lbls = dict(comp_t.properties.get("pin_labels") or {})
                    lbls.update(ov_t)
                    comp_t.properties["pin_labels"] = lbls
            frm_ep = ft if ft in _RAIL_NAMES or not fp else f"{ft}.{fp}"
            to_ep = tt if tt in _RAIL_NAMES or not tp else f"{tt}.{tp}"
            power_sheet.wires.append(
                Wire(
                    id=f"pw_{w_idx + 1}",
                    frm=frm_ep,
                    to=to_ep,
                    number=r.wire_number,
                    net=r.potential or r.wire_number,
                )
            )
    else:
        # Either control-only or shared tags: put all non-power rows in control flow
        control_rows = rows
        control_tags = power_tags | control_tags

    if control_tags or not sheets_created:
        if not sheets_created:
            ctrl_sheet = sess.project.sheets[0]
            ctrl_sheet.id = "main" if not working.sheets else str(working.sheets[0].get("id") or "main")
            ctrl_sheet.title = "Control" if not working.sheets else str(working.sheets[0].get("title") or "Control")
        else:
            ctrl_sheet = sess.add_sheet("control", title="Control")
        ctrl_sheet.drawing_style = "iec"
        ctrl_sheet.rail_hot = "L+"
        ctrl_sheet.rail_return = "0V"
        sheets_created.append(ctrl_sheet)

        # Group control tags by sheet if multiple control sheets exist in draft
        # For clean layout, place coils/lights at x=340.0 and switches/contacts at x=80.0, 150.0, 220.0
        load_tags: list[str] = []
        contact_tags: list[str] = []
        for ctag in sorted(control_tags):
            d = dev_map.get(ctag) or ImportedDeviceRecord(tag=ctag, sheet_id=ctrl_sheet.id)
            c_lib = d.library_id
            c_pins = list(d.pins)
            if ctag in both_tags:
                c_lib = "iec.contactors.coil"
                c_pins = ["A1", "A2"]
            lib_id, role = _infer_iec_library_id(
                ctag,
                role=d.role,
                library_id=c_lib,
                pins=c_pins,
                catalog=cat,
            )
            d.library_id = lib_id
            d.role = role
            dev_map[ctag] = d
            if any(
                tok in lib_id
                for tok in (".coil", ".relays.", ".pilot_light", ".horn", ".solenoid", ".timers.on_delay", ".timers.off_delay")
            ):
                load_tags.append(ctag)
            else:
                contact_tags.append(ctag)

        # Place contacts across rungs (y=50, 75, 100, 125, 150, 175) and columns (x=80, 150, 220)
        rung_ys = [50.0, 75.0, 100.0, 125.0, 150.0, 175.0, 200.0]
        col_xs = [80.0, 150.0, 220.0, 275.0]
        for idx, ctag in enumerate(contact_tags):
            d = dev_map[ctag]
            r_idx = idx % len(rung_ys)
            c_idx = (idx // len(rung_ys)) % len(col_xs)
            cx = col_xs[c_idx]
            cy = rung_ys[r_idx]
            props = dict(d.properties)
            if d.role:
                props.setdefault("role", d.role)
            # Populate parent & terminals on child contacts like K1-14, K1-21, F1-95
            m_child = re.match(r"^([A-Za-z]+\d+)[-:](.+)$", ctag)
            if m_child:
                props.setdefault("parent", m_child.group(1))
                suffix = m_child.group(2)
                if suffix in {"13", "14"}:
                    props.setdefault("terminals", "13-14")
                elif suffix in {"21", "22"}:
                    props.setdefault("terminals", "21-22")
                elif suffix in {"43", "44"}:
                    props.setdefault("terminals", "43-44")
                    props.setdefault("pin_labels", {"13": "43", "14": "44"})
                elif suffix in {"95", "96"}:
                    props.setdefault("terminals", "95-96")
            if d.label:
                props.setdefault("label", d.label)
            if d.description:
                props.setdefault("description", d.description)
            ctrl_sheet.components.append(
                Component(
                    id=ctag,
                    library_id=d.library_id,
                    x=cx,
                    y=cy,
                    properties=props,
                )
            )

        for idx, ltag in enumerate(load_tags):
            d = dev_map[ltag]
            cy = rung_ys[idx % len(rung_ys)]
            cx = 340.0 if idx < len(rung_ys) else 300.0
            props = dict(d.properties)
            if d.role:
                props.setdefault("role", d.role)
            if d.label:
                props.setdefault("label", d.label)
            if d.description:
                props.setdefault("description", d.description)
            ctrl_sheet.components.append(
                Component(
                    id=ltag,
                    library_id=d.library_id,
                    x=cx,
                    y=cy,
                    properties=props,
                )
            )

        # Ensure every wire endpoint has a rail connection if needed and resolve pins
        connected_tags_on_ctrl: set[str] = set()
        has_hot_rail_wire = False
        has_ret_rail_wire = False

        for w_idx, r in enumerate(control_rows):
            ft, fp = r.from_tag, r.from_pin
            tt, tp = r.to_tag, r.to_pin
            if ft in {"L+", "+24V", "24V"} or tt in {"L+", "+24V", "24V"}:
                has_hot_rail_wire = True
            if ft in {"0V", "0VDC"} or tt in {"0V", "0VDC"}:
                has_ret_rail_wire = True

            if ft not in _RAIL_NAMES:
                connected_tags_on_ctrl.add(ft)
                comp_f = ctrl_sheet.component(ft)
                fp, ov_f = _resolve_component_pin(ft, fp, comp_f.library_id, cat, used_pins)
                if ov_f:
                    lbls = dict(comp_f.properties.get("pin_labels") or {})
                    lbls.update(ov_f)
                    comp_f.properties["pin_labels"] = lbls
            if tt not in _RAIL_NAMES:
                connected_tags_on_ctrl.add(tt)
                comp_t = ctrl_sheet.component(tt)
                tp, ov_t = _resolve_component_pin(tt, tp, comp_t.library_id, cat, used_pins)
                if ov_t:
                    lbls = dict(comp_t.properties.get("pin_labels") or {})
                    lbls.update(ov_t)
                    comp_t.properties["pin_labels"] = lbls

            frm_ep = ft if ft in _RAIL_NAMES or not fp else f"{ft}.{fp}"
            to_ep = tt if tt in _RAIL_NAMES or not tp else f"{tt}.{tp}"
            ctrl_sheet.wires.append(
                Wire(
                    id=f"cw_{w_idx + 1}",
                    frm=frm_ep,
                    to=to_ep,
                    number=r.wire_number,
                    net=r.potential or r.wire_number,
                )
            )

        # If the imported From-To rows only listed device-to-device wires (because rail wires L+ / 0V
        # are omitted from standard From-To reports), automatically connect open supply pins to L+ and
        # open return pins (A2, X2) to 0V so the ladder rungs are complete!
        if not has_hot_rail_wire and contact_tags:
            for ctag in contact_tags:
                comp = ctrl_sheet.component(ctag)
                try:
                    dev_def = cat.get(comp.library_id)
                    first_pin = dev_def.pins[0].id if dev_def.pins else ""
                except Exception:  # noqa: BLE001
                    first_pin = ""
                if first_pin and first_pin not in used_pins.get(ctag, set()):
                    ctrl_sheet.wires.append(
                        Wire(
                            id=f"rail_hot_{ctag}",
                            frm=f"{ctag}.{first_pin}",
                            to="L+",
                            number="",
                        )
                    )
                    used_pins.setdefault(ctag, set()).add(first_pin)

        if not has_ret_rail_wire and load_tags:
            for ltag in load_tags:
                comp = ctrl_sheet.component(ltag)
                try:
                    dev_def = cat.get(comp.library_id)
                    last_pin = dev_def.pins[-1].id if dev_def.pins else ""
                except Exception:  # noqa: BLE001
                    last_pin = ""
                if last_pin and last_pin not in used_pins.get(ltag, set()):
                    ctrl_sheet.wires.append(
                        Wire(
                            id=f"rail_ret_{ltag}",
                            frm=f"{ltag}.{last_pin}",
                            to="0V",
                            number="",
                        )
                    )
                    used_pins.setdefault(ltag, set()).add(last_pin)

    # Store extracted sheet notes in project.properties["notes"]
    note_strings = [str(n.get("text") or "").strip() for n in working.notes if str(n.get("text") or "").strip()]
    if note_strings:
        sess.project.properties["notes"] = note_strings

    if unresolved_decs:
        sess.project.properties["imported_from_to_decisions"] = unresolved_decs
        sess.project.properties["last_conversion_decisions"] = list(unresolved_decs)
    else:
        sess.project.properties.pop("imported_from_to_decisions", None)
        sess.project.properties.pop("last_conversion_decisions", None)

    sess.project.properties["imported_from_to"] = working.to_dict()
    return sess



@dataclass
class _ImportedVerificationView:
    same_netlist: bool = True
    sheet_flags: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if key == "same_netlist":
            return self.same_netlist
        if key == "sheet_flags":
            return self.sheet_flags
        return self.report[key]

    def get(self, key: str, default: Any = None) -> Any:
        if key == "same_netlist":
            return self.same_netlist
        if key == "sheet_flags":
            return self.sheet_flags
        return self.report.get(key, default)


class ImportedConversionResult(dict):
    """Dictionary result of ``convert_imported_iec_print`` that also supports attribute access."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def convert_imported_iec_print(
    path_or_draft: Path | str | Sequence[Path | str] | dict[str, Any] | ImportedIECPrintDraft,
    target: str = "NEMA/ANSI",
    *,
    out: Path | str | None = None,
    workspace: Path | str | None = None,
    corrections: dict[str, Any] | list[dict[str, Any]] | None = None,
    edits: list[dict[str, Any]] | None = None,
    add_rows: list[dict[str, Any]] | None = None,
    delete_rows: list[str] | None = None,
    confirm_rows: list[str] | None = None,
    device_edits: list[dict[str, Any]] | None = None,
    vision_backend: Any = None,
    mode: str = "rules_only",
    ai_assist: bool = False,
    ai_backend: Any = None,
    confidence_threshold: float = DEFAULT_IMPORT_CONFIDENCE_THRESHOLD,
    keep_numbers: bool = False,
    wire_mode: str = "line_ref",
    session: Any = None,
    catalog: Catalog | None = None,
) -> dict[str, Any]:
    """Stage 3: End-to-end Import -> From-To Review/Corrections -> IEC-to-NEMA Convert.

    Returns the conversion summary dict augmented with:
    - ``session``: the converted ``Session``
    - ``imported_draft``: the reviewed ``ImportedIECPrintDraft``
    - ``from_to_rows``: the reviewed From-To rows
    - ``decisions``: all conversion + low-confidence import decisions
    """

    from panelcad.convert.engine import convert_standard
    from panelcad.convert.flags import sheet_flags

    cat = catalog or (session.catalog if session is not None else default_catalog())
    draft = import_iec_print_to_from_to(
        path_or_draft,
        vision_backend=vision_backend,
        confidence_threshold=confidence_threshold,
        catalog=cat,
    )
    if any(x is not None for x in (corrections, edits, add_rows, delete_rows, confirm_rows, device_edits)):
        draft = apply_from_to_corrections(
            draft,
            corrections,
            edits=edits,
            add_rows=add_rows,
            delete_rows=delete_rows,
            confirm_rows=confirm_rows,
            device_edits=device_edits,
            catalog=cat,
        )

    iec_session = build_iec_project_from_imported_from_to(draft, catalog=cat)
    if session is not None:
        session.project = iec_session.project
        target_session = session
    else:
        target_session = iec_session

    # Ensure source_path is set on target_session so convert_standard can place a default workspace if needed
    if out is not None and target_session.path is None:
        out_path = Path(out)
        target_session.path = out_path.with_name(f"{out_path.stem}.source-iec.pcad.yaml")
    elif target_session.path is None and draft.source_path:
        src_p = Path(draft.source_path)
        if src_p.parent.exists():
            target_session.path = src_p.with_name(f"{src_p.stem}.source-iec.pcad.yaml")

    ws_arg = workspace
    temp_ws_ctx = None
    if ws_arg is None and target_session.path is None:
        temp_ws_ctx = tempfile.TemporaryDirectory(prefix="pcad-import-convert-ws-")
        ws_arg = Path(temp_ws_ctx.name) / "convert-ws"

    try:
        result = convert_standard(
            target_session,
            target,
            out=out,
            workspace=ws_arg,
            keep_numbers=keep_numbers,
            wire_mode=wire_mode,
            mode=mode,
            ai_assist=ai_assist,
            ai_backend=ai_backend,
            confidence_threshold=confidence_threshold,
        )
    finally:
        if temp_ws_ctx is not None:
            temp_ws_ctx.cleanup()

    # Collect per-sheet geometry flags to verify clean layout
    all_sheet_flags: list[dict[str, Any]] = []
    for sh in target_session.project.sheets:
        if sh.kind in {"schematic", ""}:
            all_sheet_flags.extend(sheet_flags(target_session, sh))

    # Merge any remaining imported_low_confidence decisions
    unresolved_import_decs = [d.to_dict() for d in draft.decisions if d.needs_review]
    if unresolved_import_decs:
        existing_decs = list(target_session.project.properties.get("last_conversion_decisions") or [])
        seen = {(d.get("reason_code"), d.get("component_id"), d.get("message")) for d in existing_decs}
        for imp_d in unresolved_import_decs:
            key = (imp_d.get("reason_code"), imp_d.get("component_id"), imp_d.get("message"))
            if key not in seen:
                seen.add(key)
                existing_decs.append(imp_d)
        target_session.project.properties["last_conversion_decisions"] = existing_decs
        if out is not None:
            target_session.save(out)

    merged_decisions = list(result.get("decisions") or [])
    seen_res = {(d.get("reason_code"), d.get("component_id"), d.get("message")) for d in merged_decisions}
    for imp_d in unresolved_import_decs:
        key = (imp_d.get("reason_code"), imp_d.get("component_id"), imp_d.get("message"))
        if key not in seen_res:
            seen_res.add(key)
            merged_decisions.append(imp_d)

    decision_objs = [
        d if isinstance(d, ConversionDecision) else ConversionDecision.from_dict(d)
        for d in merged_decisions
    ]
    verification_obj = _ImportedVerificationView(
        same_netlist=bool(result.get("same_netlist", True)),
        sheet_flags=all_sheet_flags,
        report=dict(result),
    )
    out_dict = ImportedConversionResult(result)
    out_dict["session"] = target_session
    out_dict["project"] = target_session.project
    out_dict["converted_project"] = target_session.project
    out_dict["imported_draft"] = draft
    out_dict["from_to_rows"] = [r.to_dict() for r in draft.from_to_rows]
    out_dict["converted_from_to_rows"] = from_to_rows(target_session.project, target_session.catalog)
    out_dict["decisions"] = decision_objs
    out_dict["sheet_flags"] = all_sheet_flags
    out_dict["verification"] = verification_obj
    return out_dict


from panelcad.importer.ladder_synth import (
    ImportDeviceEntry,
    ImportFromToRow,
    ImportFromToSchedule,
    apply_import_from_to_edits,
    check_converted_sheet_layout,
    extract_import_from_to,
    synthesize_imported_ladder,
    verify_imported_from_to,
)

__all__ = [
    "DEFAULT_IMPORT_CONFIDENCE_THRESHOLD",
    "ImportDeviceEntry",
    "ImportFromToRow",
    "ImportFromToSchedule",
    "ImportedDeviceRecord",
    "ImportedFromToRow",
    "ImportedIECPrintDraft",
    "RecordedImportVisionBackend",
    "apply_from_to_corrections",
    "apply_import_from_to_edits",
    "build_iec_project_from_imported_from_to",
    "check_converted_sheet_layout",
    "clean_iec_tag",
    "convert_imported_iec_print",
    "extract_import_from_to",
    "import_iec_print_to_from_to",
    "split_endpoint_token",
    "synthesize_imported_ladder",
    "verify_imported_from_to",
]
