"""Industrial control panel nameplate from the drawing.

The plate is generated. Every value can be overridden. Nothing here is a
listing mark, and a blank cell stays blank.

Markings that public sources require
-------------------------------------
NEC 409.110 (2023 text reproduced in the NFPA CMP-11 first-revision draft):
https://docinfofiles.nfpa.org/files/AboutTheCodes/70/70_A2025_NEC_P11_FD_PrelimFR.pdf
Free-access entry point (the viewer itself is a web app):
https://www.nfpa.org/codes-and-standards/nfpa-70-standard-development/70
https://link.nfpa.org/free-access/publications/70/2023

The same list is printed by UL LLC in "Understanding Industrial Control Panels"
(Jeffrey A. Fecteau, 2017):
https://code-authorities.ul.com/wp-content/uploads/sites/2/2017/07/UnderstandingICP_2017.pdf

(1) Manufacturer's name, trademark, or other descriptive marking.
(2) Supply voltage, phases, frequency, and full-load current for each
    incoming supply circuit. Items (2) and (3) are attached to the outside.
(3) More than one supply, where more than one disconnect is required to
    disconnect all circuits of 50 V or more: mark that more than one
    disconnecting means is required, and document where those means are.
(4) Short-circuit current rating, unless the panel contains only control
    circuit components. Informational note: UL 508A Supplement SB.
(5) "Suitable for use as service equipment" when it is service equipment.
(6) Wiring diagram, or the number of a separate diagram.
(7) Enclosure type number.

Full-load current
-----------------
UL 508A 49.2 is not reproduced here (the standard text is not public in this
repo). The plate uses an approximation of the usual reading of that section:
the sum of loads that can operate together, plus the primary current of
control transformers on the incoming supply. The working, the dialog, and a
design check all say to verify that sum per UL 508A 49.2. It is not presented
as the standard's rule.

That sum is the marked full-load current only after the engineer accepts it.
NEC 430.24 (125 percent of the largest motor) sizes feeder conductors; it is
a different number and is not written on this plate.

Motor amperes, when the drawing gives horsepower and no ampere rating, come
from NEC Table 430.250 (three-phase) or Table 430.248 (single-phase) through
``motor_flc``. IEC drawings do not receive those tables. UL 508A Table 50.1
is the standard's motor-current table; its cells are not public, so they are
not copied here.

Catalog or model, job number, largest motor or load, and maximum overcurrent
device stay flagged until the engineer confirms them. The field placeholder
is "Confirm before engraving". The note names NEC 409.110 and UL 508A and
does not cite a clause number, because no public clause requiring those
markings was found. That note is the tooltip and a working line.

Multiple-supply caution
-----------------------
The default template is the NFPA second-revision sentence for the new 409.23
marking: "WARNING — MULTIPLE DISCONNECTING MEANS REQUIRED TO DE-ENERGIZE ALL
SOURCES OF SUPPLY 50 VOLTS OR MORE". UL's 2018 summary of 55.4 says each
disconnect must identify the equipment it disconnects; that identification is
the disconnect-locations line, not a second invented sentence. The template
stays flagged "verify against UL 508A 55.4 / NEC 409" until the user confirms
it. Unconfirmed wording is never engraved as a finished label.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from panelcad.core.checks import parse_amps, parse_volts
from panelcad.core.models import Component, Device, Project
from panelcad.electrical.sccr import compute_sccr
from panelcad.library.catalog import Catalog

INCH_MM = 25.4
STORE_KEY = "nameplate"


SHOP_CONFIRM_PLACEHOLDER = "Confirm before engraving"


def _shop_confirm_note(what: str) -> str:
    """Full note for a shop field that is not a located NEC 409.110 or UL 508A marking."""

    return (
        f"Confirm the {what} before engraving. "
        "NEC 409.110 does not list this marking, and no UL 508A clause for it was found."
    )

# 3 x 5 in is the shop preset from the feature request: 5 in wide, 3 in high.
# It is not a size required by NEC 409.110 or UL 508A.
PRESETS: dict[str, tuple[float, float]] = {
    "3x5": (5.0, 3.0),
}

CITATIONS: tuple[dict[str, str], ...] = (
    {
        "id": "nec-409.110",
        "section": "NFPA 70 (2023) 409.110",
        "url": "https://docinfofiles.nfpa.org/files/AboutTheCodes/70/70_A2025_NEC_P11_FD_PrelimFR.pdf",
        "used_for": "Required nameplate markings and the multiple-supply warning.",
    },
    {
        "id": "ul-icp-2017",
        "section": "UL LLC, Understanding Industrial Control Panels (2017)",
        "url": "https://code-authorities.ul.com/wp-content/uploads/sites/2/2017/07/UnderstandingICP_2017.pdf",
        "used_for": "UL's public list of the NEC 409.110 markings.",
    },
    {
        "id": "ul-508a-49.2",
        "section": "UL 508A 49.2",
        "url": "",
        "used_for": "Full-load current approximation. The standard text is not in this repo. Verify the sum per UL 508A 49.2.",
    },
    {
        "id": "nec-430-250",
        "section": "NFPA 70 Tables 430.248 and 430.250",
        "url": "https://www.nfpa.org/codes-and-standards/nfpa-70-standard-development/70",
        "used_for": "Motor full-load current when the drawing gives horsepower and no amperes. IEC drawings skip these tables.",
    },
    {
        "id": "nameplate-shop-fields",
        "section": "NEC 409.110 / UL 508A",
        "url": "https://docinfofiles.nfpa.org/files/AboutTheCodes/70/70_A2025_NEC_P11_FD_PrelimFR.pdf",
        "used_for": (
            "Catalog or model, job number, largest motor or load, and maximum overcurrent device. "
            "Confirm before engraving. No public clause requiring these markings was found."
        ),
    },
    {
        "id": "ul-508a-55.4",
        "section": "UL 508A 55.4 / NEC 409",
        "url": "https://docinfofiles.nfpa.org/files/AboutTheCodes/70/70_A2025_NEC_P11_FD_PrelimFR.pdf",
        "used_for": "Multiple-disconnect caution template. UL 55.4: each disconnect identifies the equipment it disconnects. Confirm the wording before engraving.",
    },
    {
        "id": "ul-508a-sb",
        "section": "UL 508A Supplement SB",
        "url": "",
        "used_for": "Short-circuit current rating on the nameplate, with the voltage taken from the supply.",
    },
)

# NFPA second revision, new 409.23. A template, not a verified engraving,
# until the user sets caution_confirmed.
CAUTION_TEMPLATE = (
    "WARNING — MULTIPLE DISCONNECTING MEANS REQUIRED TO DE-ENERGIZE "
    "ALL SOURCES OF SUPPLY 50 VOLTS OR MORE"
)
CAUTION_VERIFY = "VERIFY AGAINST UL 508A 55.4 / NEC 409"
# Readable engraving size. Not a UL minimum — no public table is cited for it.
# The caution sentence already begins with WARNING, so the label does not add a second heading.
CAUTION_BODY_MM = 1.8

_SCHEMATIC = {"schematic", "io", "network", "terminal"}
_HZ_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(?:Hz|HZ|hz)?")
_PHASE_RE = re.compile(r"([123])\s*(?:PH|P|Ø)?", re.IGNORECASE)
_NUM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)")
_BARE_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# A library `supply` such as 24VDC or 3.3-27VDC is the device's own power, not an incoming circuit.
_VOLTAGE_SUPPLY_RE = re.compile(
    r"(?i)^\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?\s*v(?:ac|dc)?$"
)
_SUPPLY_FIELD_BASES = {"voltage", "phases", "frequency", "fla", "max_ocpd"}
_CONFIRM_BASES = {"catalog_model", "job_number", "largest_load", "max_ocpd"}
_FLC_NOTE = "Approximation — verify per UL 508A 49.2."


@dataclass
class Field:
    key: str
    label: str
    value: str = ""
    computed: str = ""
    raw: Any = None
    source: str = ""
    overridden: bool = False
    required: bool = False
    engineer_confirm: bool = False
    citation: str = ""
    note: str = ""
    hidden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "computed": self.computed,
            "raw": self.raw,
            "source": self.source,
            "overridden": self.overridden,
            "required": self.required,
            "engineer_confirm": self.engineer_confirm,
            "citation": self.citation,
            "note": self.note,
            "hidden": self.hidden,
        }


@dataclass
class WorkingLine:
    supply_id: str
    ref: str
    kind: str
    amperes: float | None
    included: bool
    text: str
    citation: str
    phases: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "supply_id": self.supply_id,
            "ref": self.ref,
            "kind": self.kind,
            "amperes": self.amperes,
            "included": self.included,
            "text": self.text,
            "citation": self.citation,
            "phases": self.phases,
        }


@dataclass
class Mark:
    """One engraving primitive. Coordinates are millimetres, origin top-left, Y down."""

    layer: str
    kind: str
    x: float
    y: float
    w: float = 0.0
    h: float = 0.0
    text: str = ""
    size: float = 2.2
    align: str = "start"
    r: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "kind": self.kind,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "text": self.text,
            "size": self.size,
            "align": self.align,
            "r": self.r,
        }


@dataclass
class Nameplate:
    width_mm: float
    height_mm: float
    preset: str
    fields: list[Field]
    lines: list[WorkingLine]
    marks: list[Mark]
    caution_marks: list[Mark]
    caution_size_mm: tuple[float, float] | None
    engineer_notes: list[str]
    citations: list[dict[str, str]]
    available_fault_ka: float | None
    calculated_sccr_ka: float | None
    issues: list[dict[str, str]] = field(default_factory=list)

    def field(self, key: str) -> Field | None:
        for item in self.fields:
            if item.key == key:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "width_in": self.width_mm / INCH_MM,
            "height_in": self.height_mm / INCH_MM,
            "preset": self.preset,
            "fields": [item.to_dict() for item in self.fields],
            "lines": [item.to_dict() for item in self.lines],
            "working": [item.text for item in self.lines],
            "marks": [item.to_dict() for item in self.marks],
            "caution": None
            if not self.caution_marks
            else {
                "width_mm": self.caution_size_mm[0] if self.caution_size_mm else 0,
                "height_mm": self.caution_size_mm[1] if self.caution_size_mm else 0,
                "wording": " ".join(mark.text for mark in self.caution_marks if mark.kind == "text"),
                "marks": [item.to_dict() for item in self.caution_marks],
                "engineer_confirm": True,
            },
            "engineer_notes": list(self.engineer_notes),
            "citations": list(self.citations),
            "available_fault_ka": self.available_fault_ka,
            "calculated_sccr_ka": self.calculated_sccr_ka,
            "issues": list(self.issues),
        }


def _store(project: Project) -> dict[str, Any]:
    raw = project.properties.get(STORE_KEY)
    return raw if isinstance(raw, dict) else {}


def _overrides(project: Project) -> dict[str, Any]:
    raw = _store(project).get("overrides")
    return raw if isinstance(raw, dict) else {}


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _truthy_flag(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    amps = parse_amps(value)
    if amps is not None:
        return amps
    volts = parse_volts(value)
    if volts is not None:
        return volts[0]
    match = _NUM_RE.search(str(value).replace(",", ""))
    if not match:
        return None
    return float(match.group(1))


def _first(props: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in props and not _blank(props.get(key)):
            return props.get(key)
    return None


def fmt_amps(value: float | None) -> str:
    if value is None:
        return ""
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text


def _engraved_override(key: str, text: str) -> str:
    """A bare number is engraved with its unit. Text that already has a unit stays as typed."""

    unit = {
        "fla": "A",
        "max_ocpd": "A",
        "largest_load": "A",
        "sccr": "kA",
        "voltage": "V",
        "frequency": "Hz",
    }.get(key.split(":", 1)[0])
    if unit is None or _BARE_NUMBER_RE.fullmatch(text) is None:
        return text
    return f"{fmt_amps(float(text))} {unit}"


# Short forms used when a column cannot hold the full label. Never cut a label with an ellipsis.
_LABEL_ABBREV = {
    "SHORT-CIRCUIT CURRENT RATING": "SCCR",
    "LARGEST MOTOR OR LOAD": "LARGEST LOAD FLA",
    "FULL-LOAD CURRENT": "FLC",
    "SUPPLY VOLTAGE": "VOLTAGE",
    "ENCLOSURE TYPE": "ENCL TYPE",
    "WIRING DIAGRAM": "DIAGRAM",
    "FREQUENCY": "FREQ",
    "DATE CODE": "DATE",
    "MAX OCPD": "MAX OCPD",
}


def _text_limit(width_mm: float, size_mm: float) -> int:
    return max(4, int(width_mm / max(size_mm, 0.8) / 0.62))


def _fits_text(text: str, width_mm: float, size_mm: float) -> bool:
    return len(text) <= _text_limit(width_mm, size_mm)


def _display_label(label: str, width_mm: float, size_mm: float) -> str:
    """Full label when it fits. Otherwise a chosen abbreviation, not an ellipsis."""

    raw = " ".join(str(label or "").split())
    if not raw or _fits_text(raw, width_mm, size_mm):
        return raw
    stem, sep, tail = raw.rpartition(" ")
    suffix = ""
    base = raw
    if sep and tail.isdigit():
        base = stem
        suffix = " " + tail
    short = _LABEL_ABBREV.get(base)
    if short:
        return short + suffix
    return raw


def _fit(text: str, width_mm: float, size_mm: float) -> str:
    raw = " ".join(str(text or "").split())
    if not raw:
        return ""
    n = max(4, int(width_mm / max(size_mm, 0.8) / 0.62))
    if len(raw) <= n:
        return raw
    return raw[: max(1, n - 1)] + "…"


def plate_size(project: Project) -> tuple[float, float, str]:
    """Return width_mm, height_mm, and the preset name (``custom`` when set by inches)."""

    store = _store(project)
    preset = str(store.get("preset") or "3x5").strip().lower().replace(" ", "")
    if preset in PRESETS and _blank(store.get("width_in")) and _blank(store.get("height_in")):
        width_in, height_in = PRESETS[preset]
        return width_in * INCH_MM, height_in * INCH_MM, preset
    if preset in PRESETS and store.get("width_in") in (None, "") and store.get("height_in") in (None, ""):
        width_in, height_in = PRESETS[preset]
        return width_in * INCH_MM, height_in * INCH_MM, preset
    width_in = _num(store.get("width_in"))
    height_in = _num(store.get("height_in"))
    if preset in PRESETS and (width_in is None or height_in is None):
        dw, dh = PRESETS[preset]
        width_in = dw if width_in is None else width_in
        height_in = dh if height_in is None else height_in
        if abs(width_in - dw) < 1e-6 and abs(height_in - dh) < 1e-6:
            return width_in * INCH_MM, height_in * INCH_MM, preset
    if width_in is None or height_in is None or width_in <= 0 or height_in <= 0:
        width_in, height_in = PRESETS["3x5"]
        return width_in * INCH_MM, height_in * INCH_MM, "3x5"
    return width_in * INCH_MM, height_in * INCH_MM, "custom"


def parse_size_token(token: str) -> tuple[str, float | None, float | None]:
    """``3x5`` or ``4x2.5`` (width x height, inches)."""

    text = str(token or "").strip().lower().replace(" ", "").replace("in", "")
    if text in PRESETS:
        return text, None, None
    if "x" not in text:
        raise ValueError("size must be 3x5 or a custom WxH in inches, such as 4x2")
    left, right = text.split("x", 1)
    width = _num(left)
    height = _num(right)
    if width is None or height is None or width <= 0 or height <= 0:
        raise ValueError("size must be positive inches, width x height")
    for name, (pw, ph) in PRESETS.items():
        if abs(width - pw) < 1e-6 and abs(height - ph) < 1e-6:
            return name, None, None
    return "custom", width, height


@dataclass
class _Part:
    ref: str
    sheet: str
    sheet_kind: str
    library_id: str
    name: str
    category: str
    props: dict[str, Any]
    device: Device | None


def _merged(device: Device | None, comp: Component) -> dict[str, Any]:
    props: dict[str, Any] = {}
    if device is not None:
        props.update(device.properties or {})
    props.update(comp.properties or {})
    return props


def _parts(project: Project, catalog: Catalog | None) -> list[_Part]:
    """One record per tag. Schematic properties win; a layout copy fills gaps."""

    by_ref: dict[str, _Part] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            device = None if catalog is None else catalog.devices.get(comp.library_id)
            props = _merged(device, comp)
            part = _Part(
                ref=comp.id,
                sheet=sheet.id,
                sheet_kind=sheet.kind,
                library_id=comp.library_id,
                name="" if device is None else device.name,
                category="" if device is None else (device.category or ""),
                props=props,
                device=device,
            )
            previous = by_ref.get(comp.id)
            if previous is None:
                by_ref[comp.id] = part
                continue
            if previous.sheet_kind in _SCHEMATIC and sheet.kind not in _SCHEMATIC:
                for key, value in props.items():
                    if _blank(previous.props.get(key)) and not _blank(value):
                        previous.props[key] = value
                continue
            if sheet.kind in _SCHEMATIC and previous.sheet_kind not in _SCHEMATIC:
                for key, value in previous.props.items():
                    if _blank(props.get(key)) and not _blank(value):
                        props[key] = value
                part.props = props
                by_ref[comp.id] = part
                continue
            for key, value in props.items():
                if _blank(previous.props.get(key)) and not _blank(value):
                    previous.props[key] = value
    return list(by_ref.values())


def _classify(part: _Part) -> str:
    cat = part.category.lower()
    lid = part.library_id.lower()
    role = str(part.props.get("role") or "").lower()
    if cat.startswith("power.transformer") or "transformer" in lid or role == "transformer":
        return "transformer"
    if cat.startswith("load.motor") or role == "motor" or ("motor" in lid and "load" in lid):
        return "motor"
    if cat.startswith("drive.") or role in {"drive", "vfd"} or "vfd" in lid:
        return "drive"
    if cat.startswith(("load.", "power.receptacle")) or cat == "power.load" or role in {
        "heater",
        "load",
        "receptacle",
    }:
        return "load"
    if cat.startswith("protection.") or role in {"breaker", "disconnect", "fuse", "main", "ocpd"}:
        return "ocpd"
    return "other"


def _looks_like_voltage(value: str) -> bool:
    """Device power ratings (24VDC, 3.3-27VDC, 100-240VAC) are not supply ids."""

    text = " ".join(value.split())
    if _VOLTAGE_SUPPLY_RE.match(text):
        return True
    if re.search(r"(?i)\d", text) and re.search(r"(?i)v(?:ac|dc)?\b", text):
        return True
    return False


def _supply_id(props: dict[str, Any]) -> str:
    """Incoming circuit id. A control device's library ``supply`` voltage is ignored."""

    for key in ("supply_id", "incoming", "supply"):
        raw = props.get(key)
        if _blank(raw):
            continue
        text = str(raw).strip()
        if _looks_like_voltage(text):
            continue
        return text
    return "1"


def _format_ka(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _primary_supply(supply_ids: list[str]) -> str:
    if "1" in supply_ids or not supply_ids:
        return "1"
    return supply_ids[0]


def _field_key(base: str, supply_id: str, primary: str, multi: bool) -> str:
    """Supply 1 keeps unsuffixed keys so ``--set fla=18`` matches the plate."""

    if not multi or supply_id == primary:
        return base
    return f"{base}:{supply_id}"


def _canonical_override_key(key: str) -> str:
    base, sep, suffix = str(key).partition(":")
    if sep and suffix == "1":
        return base
    return str(key)


def _override_for(overrides: dict[str, Any], key: str) -> tuple[Any, bool]:
    if key in overrides:
        return overrides[key], True
    if ":" not in key and f"{key}:1" in overrides:
        return overrides[f"{key}:1"], True
    return None, False


def sccr_mark(
    ka: float | None,
    voltage: float | None,
    voltage_source: str,
    supply_voltage: float | None,
) -> tuple[str, bool, str]:
    """SCCR engraving. An assumed voltage is not printed as a rating.

    The voltage is engraved only when it is the supply voltage and the rating
    source is not an assumption. Otherwise the kiloamperes stand alone and the
    field stays flagged.
    """

    if ka is None:
        return "", True, ""
    amps = f"{_format_ka(ka)} kA"
    source = (voltage_source or "").strip()
    assumed = "assum" in source.lower() or "stand-in" in source.lower()
    matches = (
        voltage is not None
        and supply_voltage is not None
        and abs(float(voltage) - float(supply_voltage)) < 1.0
    )
    if not assumed and matches:
        return f"{amps} @ {_format_ka(supply_voltage)} V", False, ""
    if assumed:
        shown = _format_ka(voltage) if voltage is not None else "an assumed voltage"
        return (
            amps,
            True,
            f"SCCR voltage is {source or shown}, not taken from the supply. Verify before engraving a voltage.",
        )
    if voltage is not None and supply_voltage is not None and not matches:
        return (
            amps,
            True,
            f"SCCR was calculated at {_format_ka(voltage)} V ({source or 'rating'}). "
            f"That is not this supply's {_format_ka(supply_voltage)} V.",
        )
    if supply_voltage is None:
        return (
            amps,
            True,
            "SCCR voltage is not on the supply. Enter the supply voltage or confirm the rating voltage.",
        )
    return f"{amps} @ {_format_ka(supply_voltage)} V", False, ""


def _load_sccr(project: Project, catalog: Catalog | None) -> tuple[Any, float | None]:
    """Panel SCCR from the 0.5.0 calculator, plus any entered available fault current."""

    available = _num(project.properties.get("available_fault_ka"))
    rated = compute_sccr(project, catalog)
    if available is None:
        available = rated.available_fault_ka
    return rated, available


def _phases_of(props: dict[str, Any]) -> int | None:
    raw = _first(props, "phases", "phase", "poles")
    if _blank(raw):
        return None
    if isinstance(raw, (int, float)) and int(raw) in {1, 3}:
        return int(raw)
    match = _PHASE_RE.search(str(raw))
    if match and int(match.group(1)) in {1, 3}:
        return int(match.group(1))
    return None


def _hertz(value: Any) -> float | None:
    if _blank(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _HZ_RE.search(str(value))
    if not match:
        return None
    return float(match.group(1))


def _project_electrical(project: Project) -> tuple[float | None, int | None, float | None]:
    props = project.properties
    voltage = _num(_first(props, "voltage", "supply_voltage", "sccr_voltage"))
    phases = _phases_of(props)
    if phases is None:
        phases = _phases_of({"phases": _first(props, "supply_phases")})
    frequency = _hertz(_first(props, "frequency", "hz", "supply_frequency"))
    return voltage, phases, frequency


def _explicit_load_amps(props: dict[str, Any]) -> tuple[float | None, str]:
    for key in ("FLA", "fla", "amps", "In", "Ie", "current", "Iin", "input_a"):
        if key not in props:
            continue
        amps = parse_amps(props.get(key))
        if amps is None:
            amps = _num(props.get(key))
        if amps is not None:
            return amps, f"drawing {key}"
    return None, ""


def _va(props: dict[str, Any]) -> float | None:
    kva = _num(_first(props, "kva", "kVA", "KVA"))
    if kva is not None:
        return kva * 1000.0
    return _num(_first(props, "VA", "va", "Va"))


def _watts(props: dict[str, Any]) -> float | None:
    return _num(_first(props, "watts", "W", "power_w", "Pn_w"))


def _excluded(props: dict[str, Any]) -> str:
    flag = str(props.get("spare") or "").strip().lower()
    if flag in {"1", "true", "yes", "y", "spare"}:
        return "marked spare, so it is not a load that operates"
    simultaneous = _truthy_flag(props.get("simultaneous"))
    if simultaneous is False:
        return "marked simultaneous: false, so it is left out of the sum"
    return ""


def _interlock(props: dict[str, Any]) -> str:
    raw = _first(props, "interlock_group", "interlock", "exclusive_group")
    if _blank(raw):
        return ""
    return str(raw).strip()


def _load_voltage(props: dict[str, Any]) -> float | None:
    return _num(_first(props, "voltage", "volts", "Un", "Vin", "primary"))


def build_nameplate(project: Project, catalog: Catalog | None = None) -> Nameplate:
    """Read the drawing and the nameplate overrides. Does not write the project."""

    from panelcad.core.motor_sizing import is_iec_standard, motor_flc

    overrides = _overrides(project)
    store = _store(project)
    width_mm, height_mm, preset = plate_size(project)
    project_v, project_ph, project_hz = _project_electrical(project)
    iec = is_iec_standard(project.title_block.standard)
    parts = _parts(project, catalog)
    service = _truthy_flag(store.get("service_equipment"))
    if "service_equipment" in overrides:
        service = _truthy_flag(overrides.get("service_equipment"))
    control_only = _truthy_flag(store.get("control_circuit_only"))

    groups: dict[str, list[_Part]] = {}
    for part in parts:
        groups.setdefault(_supply_id(part.props), []).append(part)
    if not groups:
        groups["1"] = []
    supply_ids = sorted(groups, key=lambda item: (item != "1", item))

    lines: list[WorkingLine] = []
    fields: list[Field] = []
    notes: list[str] = [
        (
            "NEC 110.16 arc-flash marking needs the arc-flash boundary, the incident energy "
            "or the PPE level, and the assessment date. Those values are not calculated here, "
            "so no arc-flash label is generated."
        ),
        (
            "UL 508A Supplement SB also marks the size and type of a field-installed "
            "branch or feeder protective device when the panel requires one. "
            "Enter that text with nameplate_set field_bcpd if it applies. It is not assumed."
        ),
    ]

    def add_line(supply_id: str, ref: str, kind: str, amperes: float | None, included: bool, text: str, citation: str) -> None:
        lines.append(
            WorkingLine(supply_id, ref, kind, amperes, included, text, citation)
        )

    calculated: dict[str, float | None] = {}
    ocpd_amp: dict[str, float | None] = {}
    ocpd_ref: dict[str, str] = {}
    supply_voltage: dict[str, tuple[float | None, str]] = {}
    supply_phases: dict[str, tuple[int | None, str]] = {}
    supply_hz: dict[str, tuple[float | None, str]] = {}
    motors_present: dict[str, bool] = {}

    for supply_id in supply_ids:
        members = groups[supply_id]
        volts, volt_src = _supply_voltage(supply_id, members, project_v, supply_ids)
        phases, phase_src = _supply_phases(supply_id, members, project_ph, supply_ids)
        hertz, hertz_src = _supply_hertz(supply_id, members, project_hz, supply_ids)
        supply_voltage[supply_id] = (volts, volt_src)
        supply_phases[supply_id] = (phases, phase_src)
        supply_hz[supply_id] = (hertz, hertz_src)
        motors_present[supply_id] = any(_classify(part) == "motor" for part in members)

        pending: list[WorkingLine] = []
        for part in members:
            kind = _classify(part)
            if kind == "other":
                continue
            if kind == "ocpd":
                continue
            why = _excluded(part.props)
            if why:
                add_line(supply_id, part.ref, kind, None, False, f"{part.ref} {why}.", "")
                continue
            if kind == "transformer":
                pending.extend(_transformer_line(part, supply_id, volts, phases))
                continue
            if kind == "drive" and motors_present[supply_id] and _truthy_flag(part.props.get("count_as_load")) is not True:
                add_line(
                    supply_id,
                    part.ref,
                    "drive",
                    None,
                    False,
                    f"{part.ref} is a drive on a supply that already has a motor. "
                    "The motor ampere rating is the load in the 49.2 sum. "
                    "Set count_as_load to add a drive input current as well.",
                    "UL 508A 49.2",
                )
                continue
            if kind == "drive":
                pending.extend(_drive_line(part, supply_id, volts, phases))
                continue
            pending.extend(_load_line(part, supply_id, volts, phases, iec, motor_flc))

        group_of = {part.ref: _interlock(part.props) for part in members if _interlock(part.props)}
        pending = _apply_interlocks_from_parts(pending, group_of)
        lines.extend(pending)

        included = [line for line in pending if line.included]
        phase_counts = {
            line.phases
            for line in included
            if line.kind in {"motor", "load", "drive"} and line.phases in {1, 3} and line.amperes is not None
        }
        if len(phase_counts) > 1:
            calculated[supply_id] = None
            add_line(
                supply_id,
                "",
                "sum",
                None,
                False,
                f"Supply {supply_id} has both single-phase and three-phase load currents. "
                "They are not added as one line current. Enter the full-load current.",
                "UL 508A 49.2",
            )
        elif any(line.included and line.amperes is None for line in pending):
            calculated[supply_id] = None
            missing = ", ".join(line.ref for line in included if line.amperes is None)
            add_line(
                supply_id,
                "",
                "sum",
                None,
                False,
                f"Supply {supply_id} full-load current is incomplete because {missing or 'a load'} has no ampere rating.",
                "UL 508A 49.2",
            )
        elif included:
            total = sum(float(line.amperes or 0) for line in included)
            calculated[supply_id] = total
            detail = " + ".join(
                f"{line.ref} {fmt_amps(line.amperes)} A" for line in included if line.amperes is not None
            )
            add_line(
                supply_id,
                "",
                "sum",
                total,
                True,
                f"Supply {supply_id} full-load current ≈ {detail} = {fmt_amps(total)} A. "
                f"{_FLC_NOTE} Simultaneous loads plus control-transformer primary current on this input. "
                "Shown to 0.01 A. That display is not a code rounding step.",
                "UL 508A 49.2",
            )
        else:
            calculated[supply_id] = None
            add_line(
                supply_id,
                "",
                "sum",
                None,
                False,
                f"Supply {supply_id} has no load ampere rating to sum.",
                "UL 508A 49.2",
            )

        chosen_amps, chosen_ref, ocpd_note = _main_ocpd(members)
        ocpd_amp[supply_id] = chosen_amps
        ocpd_ref[supply_id] = chosen_ref
        if ocpd_note:
            notes.append(ocpd_note)

    rated, available_fault = _load_sccr(project, catalog)
    fields.extend(_identity_fields(project, overrides))
    multi = len(supply_ids) > 1
    primary = _primary_supply(supply_ids)
    for supply_id in supply_ids:
        suffix = "" if supply_id == primary else f" {supply_id}"
        volts, volt_src = supply_voltage[supply_id]
        phases, phase_src = supply_phases[supply_id]
        hertz, hertz_src = supply_hz[supply_id]
        fields.append(
            _resolved_field(
                overrides,
                _field_key("voltage", supply_id, primary, multi),
                f"SUPPLY VOLTAGE{suffix}",
                "" if volts is None else f"{fmt_amps(volts)} V",
                volts,
                volt_src,
                required=True,
                citation="NEC 409.110(2)",
            )
        )
        fields.append(
            _resolved_field(
                overrides,
                _field_key("phases", supply_id, primary, multi),
                f"PHASE{suffix}",
                "" if phases is None else str(phases),
                phases,
                phase_src,
                required=True,
                citation="NEC 409.110(2)",
            )
        )
        fields.append(
            _resolved_field(
                overrides,
                _field_key("frequency", supply_id, primary, multi),
                f"FREQUENCY{suffix}",
                "" if hertz is None else f"{fmt_amps(hertz)} Hz",
                hertz,
                hertz_src,
                required=True,
                citation="NEC 409.110(2)",
            )
        )
        calc = calculated[supply_id]
        fields.append(
            _resolved_field(
                overrides,
                _field_key("fla", supply_id, primary, multi),
                f"FULL-LOAD CURRENT{suffix}",
                "" if calc is None else f"{fmt_amps(calc)} A",
                calc,
                "UL 508A 49.2 approximation",
                required=True,
                citation="UL 508A 49.2; NEC 409.110(2)",
                engineer_confirm=True,
                note="" if calc is None else _FLC_NOTE,
            )
        )
        ocpd_a = ocpd_amp[supply_id]
        fields.append(
            _resolved_field(
                overrides,
                _field_key("max_ocpd", supply_id, primary, multi),
                f"MAX OCPD{suffix}",
                "" if ocpd_a is None else f"{fmt_amps(ocpd_a)} A",
                ocpd_a,
                ocpd_ref[supply_id],
                required=False,
                citation="",
                engineer_confirm=True,
                note=_shop_confirm_note("maximum overcurrent device"),
            )
        )

    sccr_ka = rated.panel_sccr_ka
    sccr_voltage = rated.voltage
    sccr_voltage_source = str(rated.voltage_source or "")
    supply_for_sccr = supply_voltage.get(primary, (None, ""))[0]
    sccr_text, sccr_flag, sccr_note = sccr_mark(sccr_ka, sccr_voltage, sccr_voltage_source, supply_for_sccr)
    sccr_source = "UL 508A Supplement SB"
    if rated.incomplete and sccr_ka is None:
        sccr_source = "SCCR incomplete"
    if control_only is True:
        sccr_flag = False
        sccr_note = "Control-circuit-only exception selected. SCCR marking is not required."
    elif sccr_ka is None:
        sccr_flag = True
        if not sccr_note:
            sccr_note = "No panel SCCR is available to engrave."
    fields.append(
        _resolved_field(
            overrides,
            "sccr",
            "SHORT-CIRCUIT CURRENT RATING",
            sccr_text,
            sccr_ka,
            sccr_source,
            required=control_only is not True,
            citation="NEC 409.110(4); UL 508A Supplement SB",
            engineer_confirm=sccr_flag,
            note=sccr_note,
        )
    )

    enclosure_text, enclosure_src = _enclosure_type(project)
    fields.append(
        _resolved_field(
            overrides,
            "enclosure_type",
            "ENCLOSURE TYPE",
            enclosure_text,
            enclosure_text,
            enclosure_src,
            required=True,
            citation="NEC 409.110(7)",
        )
    )
    drawing = str(project.title_block.drawing_number or "").strip()
    fields.append(
        _resolved_field(
            overrides,
            "drawing_number",
            "WIRING DIAGRAM",
            drawing,
            drawing,
            "title block drawing number",
            required=True,
            citation="NEC 409.110(6)",
        )
    )
    date_text, date_src = _date_code(project)
    fields.append(
        _resolved_field(
            overrides,
            "date_code",
            "DATE CODE",
            date_text,
            date_text,
            date_src,
            required=False,
            citation="",
            engineer_confirm=True,
            note="A public NEC 409.110 or UL 508A clause that requires a date code was not found. The field is on the plate because the shop asked for it.",
        )
    )
    largest_raw, largest_src = _largest_load(lines)
    fields.append(
        _resolved_field(
            overrides,
            "largest_load",
            "LARGEST MOTOR OR LOAD",
            "" if largest_raw is None else f"{fmt_amps(largest_raw)} A",
            largest_raw,
            largest_src,
            required=largest_raw is None and any(line.kind in {"motor", "load", "drive"} for line in lines),
            citation="",
            engineer_confirm=True,
            note=_shop_confirm_note("largest motor or load"),
        )
    )
    fields.append(_service_field(service))
    if service is None:
        notes.append(
            "NEC 409.110(5) requires the service-equipment marking only when the panel is "
            "service equipment. That has not been confirmed, so the marking is left off."
        )
    bcpd = overrides.get("field_bcpd", store.get("field_bcpd"))
    if not _blank(bcpd):
        fields.append(
            Field(
                key="field_bcpd",
                label="FIELD PROTECTIVE DEVICE",
                value=str(bcpd).strip(),
                raw=bcpd,
                source="override" if "field_bcpd" in overrides else "project",
                overridden="field_bcpd" in overrides,
                citation="UL 508A Supplement SB",
                engineer_confirm=True,
            )
        )

    high_supplies = _supplies_at_or_above_50(fields, supply_ids, primary)
    caution = len(high_supplies) >= 2
    locations = str(store.get("disconnect_locations") or overrides.get("disconnect_locations") or "").strip()
    caution_text = str(store.get("caution_text") or CAUTION_TEMPLATE).strip() or CAUTION_TEMPLATE
    caution_confirmed = _truthy_flag(store.get("caution_confirmed")) is True
    if caution and not locations:
        notes.append(
            "NEC 409.110(3) requires the location of the means that disconnect all circuits "
            "of 50 V or more to be documented and available. disconnect_locations is blank."
        )
    if len(supply_ids) >= 2 and not caution:
        notes.append(
            "More than one supply id is on the drawing, and fewer than two are known to be "
            "50 V or more. The multiple-disconnect caution is not printed until those voltages are known."
        )
    lines.extend(_shop_note_lines(fields))
    lines.extend(_override_lines(fields))

    plate = Nameplate(
        width_mm=width_mm,
        height_mm=height_mm,
        preset=preset,
        fields=fields,
        lines=lines,
        marks=[],
        caution_marks=[],
        caution_size_mm=None,
        engineer_notes=notes,
        citations=[dict(item) for item in CITATIONS],
        available_fault_ka=available_fault,
        calculated_sccr_ka=sccr_ka,
    )
    hole_d = _num(store.get("hole_diameter_in"))
    hole_inset = _num(store.get("hole_inset_in"))
    plate.marks = layout_holes(
        layout_marks(plate, _content_margin(width_mm, height_mm, hole_d, hole_inset)),
        width_mm,
        height_mm,
        hole_d,
        hole_inset,
    )
    if caution:
        plate.caution_marks, plate.caution_size_mm = layout_caution(
            locations, text=caution_text, confirmed=caution_confirmed
        )
    plate.issues = collect_issues(
        plate,
        ocpd_amp,
        ocpd_ref,
        caution,
        locations,
        available_fault,
        primary=primary,
        caution_confirmed=caution_confirmed,
    )
    return plate


def _is_primary_supply(supply_id: str, supply_ids: list[str]) -> bool:
    return supply_id == "1" or len(supply_ids) == 1


def _supply_voltage(
    supply_id: str,
    members: list[_Part],
    project_voltage: float | None,
    supply_ids: list[str],
) -> tuple[float | None, str]:
    """Incoming supply voltage. A 24 V control load does not replace it."""

    if project_voltage is not None and _is_primary_supply(supply_id, supply_ids):
        return project_voltage, "project supply voltage"
    line: list[float] = []
    low: list[float] = []
    has_motor = any(_classify(part) == "motor" for part in members)
    for part in members:
        kind = _classify(part)
        if kind not in {"motor", "load", "drive"}:
            continue
        if kind == "drive" and has_motor and _truthy_flag(part.props.get("count_as_load")) is not True:
            continue
        volts = _load_voltage(part.props)
        if volts is None:
            continue
        if kind in {"motor", "drive"} or volts >= 50:
            line.append(volts)
        else:
            low.append(volts)
    chosen = line or low
    if chosen and max(chosen) - min(chosen) < 1.0:
        return chosen[0], "load voltage on this supply"
    return None, ""


def _supply_phases(
    supply_id: str,
    members: list[_Part],
    project_phases: int | None,
    supply_ids: list[str],
) -> tuple[int | None, str]:
    if project_phases in {1, 3} and _is_primary_supply(supply_id, supply_ids):
        return project_phases, "project supply phases"
    found: list[int] = []
    for part in members:
        if _classify(part) not in {"motor", "drive", "load", "transformer"}:
            continue
        phases = _phases_of(part.props)
        if phases in {1, 3}:
            found.append(phases)
    if found and len(set(found)) == 1:
        return found[0], "load phases"
    return None, ""


def _supply_hertz(
    supply_id: str,
    members: list[_Part],
    project_hz: float | None,
    supply_ids: list[str],
) -> tuple[float | None, str]:
    if project_hz is not None and _is_primary_supply(supply_id, supply_ids):
        return project_hz, "project supply frequency"
    found: list[float] = []
    for part in members:
        if _classify(part) not in {"motor", "drive", "load", "transformer"}:
            continue
        hertz = _hertz(_first(part.props, "frequency", "hz", "Hz"))
        if hertz is not None:
            found.append(hertz)
    if found and max(found) - min(found) < 0.1:
        return found[0], "load frequency"
    return None, ""


def _transformer_line(
    part: _Part,
    supply_id: str,
    supply_voltage: float | None,
    supply_phases: int | None,
) -> list[WorkingLine]:
    va = _va(part.props)
    primary = _num(_first(part.props, "primary", "primary_voltage", "voltage"))
    if primary is None:
        primary = supply_voltage
        primary_src = "supply voltage"
    else:
        primary_src = "transformer primary voltage"
    phases = _phases_of(part.props)
    if phases not in {1, 3}:
        phases = supply_phases if supply_phases in {1, 3} else None
    if (
        primary is not None
        and supply_voltage is not None
        and abs(primary - supply_voltage) / max(supply_voltage, 1.0) > 0.15
    ):
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "transformer",
                None,
                False,
                f"{part.ref} primary is {fmt_amps(primary)} V, which is not this supply's {fmt_amps(supply_voltage)} V input. "
                "UL 508A 49.2 adds a control transformer connected to the input voltage, so this primary is not in the sum.",
                "UL 508A 49.2",
            )
        ]
    if va is None or primary is None or primary <= 0:
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "transformer",
                None,
                True,
                f"{part.ref} is a control transformer without both a VA rating and a primary voltage. "
                "Its primary current is not invented. The full-load sum stays incomplete.",
                "UL 508A 49.2",
            )
        ]
    if phases not in {1, 3}:
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "transformer",
                None,
                True,
                f"{part.ref} control-transformer primary is not computed. Phases are not set and the supply "
                "phase count is unknown. VA/V would understate a three-phase primary by √3, so the current is left blank.",
                "UL 508A 49.2",
            )
        ]
    if phases == 3:
        amps = va / (math.sqrt(3.0) * primary)
        formula = f"{fmt_amps(va)} VA / (√3 × {fmt_amps(primary)} V)"
    else:
        amps = va / primary
        formula = f"{fmt_amps(va)} VA / {fmt_amps(primary)} V"
    return [
        WorkingLine(
            supply_id,
            part.ref,
            "transformer",
            amps,
            True,
            f"{part.ref} control-transformer primary = {formula} = {fmt_amps(amps)} A ({primary_src}).",
            "UL 508A 49.2",
            phases,
        )
    ]


def _phase_count(part: _Part, supply_phases: int | None) -> int | None:
    own = _phases_of(part.props)
    if own in {1, 3}:
        return own
    if supply_phases in {1, 3}:
        return supply_phases
    return None


def _load_line(
    part: _Part,
    supply_id: str,
    supply_voltage: float | None,
    supply_phases: int | None,
    iec: bool,
    motor_flc: Any,
) -> list[WorkingLine]:
    kind = _classify(part)
    explicit, explicit_src = _explicit_load_amps(part.props)
    load_v = _load_voltage(part.props)
    phases = _phase_count(part, supply_phases)
    if kind == "motor":
        return _motor_line(part, supply_id, supply_voltage, phases, iec, motor_flc, explicit, explicit_src, load_v)
    if explicit is not None:
        if load_v is not None and supply_voltage is not None and load_v < 50 and supply_voltage >= 50:
            return [
                WorkingLine(
                    supply_id,
                    part.ref,
                    kind,
                    None,
                    False,
                    f"{part.ref} is {fmt_amps(explicit)} A at {fmt_amps(load_v)} V. "
                    "That current is on the load side of a control supply, not an extra line current on the incoming supply. "
                    "The control-transformer primary covers it when the transformer is in the sum.",
                    "UL 508A 49.2",
                )
            ]
        text = f"{part.ref} {explicit_src} {fmt_amps(explicit)} A is the load current in the sum."
        return [WorkingLine(supply_id, part.ref, kind, explicit, True, text, "drawing ampere rating", phases)]

    watts = _watts(part.props)
    volts = load_v or supply_voltage
    # The load's own phase count wins. A missing count uses the supply.
    # It is never assumed single-phase: W/V would understate a three-phase load by √3.
    own_phases = _phases_of(part.props)
    load_phases = own_phases if own_phases in {1, 3} else (supply_phases if supply_phases in {1, 3} else None)
    if watts is not None and volts is not None and volts > 0:
        if load_phases not in {1, 3}:
            return [
                WorkingLine(
                    supply_id,
                    part.ref,
                    kind,
                    None,
                    True,
                    f"{part.ref} has watts and voltage but no phase count, and the supply phase count is unknown. "
                    "The current is left blank. W/V would understate a three-phase load by √3.",
                    "UL 508A 49.2",
                )
            ]
        if load_phases == 3:
            amps = watts / (math.sqrt(3.0) * volts)
            formula = f"{fmt_amps(watts)} W / (√3 × {fmt_amps(volts)} V)"
        else:
            amps = watts / volts
            formula = f"{fmt_amps(watts)} W / {fmt_amps(volts)} V"
        if load_v is not None and supply_voltage is not None and load_v < 50 <= supply_voltage:
            return [
                WorkingLine(
                    supply_id,
                    part.ref,
                    kind,
                    None,
                    False,
                    f"{part.ref} is {formula} at {fmt_amps(load_v)} V, below the incoming supply. "
                    "It is not added again on top of a control-transformer primary.",
                    "UL 508A 49.2",
                )
            ]
        return [
            WorkingLine(
                supply_id,
                part.ref,
                kind,
                amps,
                True,
                f"{part.ref} load current = {formula} = {fmt_amps(amps)} A.",
                "load watts and voltage on the drawing",
                load_phases,
            )
        ]
    return [
        WorkingLine(
            supply_id,
            part.ref,
            kind,
            None,
            True,
            f"{part.ref} has no ampere rating on the drawing. The full-load sum stays incomplete until one is entered.",
            "UL 508A 49.2",
        )
    ]


def _motor_line(
    part: _Part,
    supply_id: str,
    supply_voltage: float | None,
    phases: int | None,
    iec: bool,
    motor_flc: Any,
    explicit: float | None,
    explicit_src: str,
    load_v: float | None,
) -> list[WorkingLine]:
    hp = _num(_first(part.props, "hp", "HP", "horsepower"))
    volts = load_v or supply_voltage
    source = str(part.props.get("fla_source") or "").strip().lower()
    if iec:
        if explicit is not None and source != "nec-table":
            return [
                WorkingLine(
                    supply_id,
                    part.ref,
                    "motor",
                    explicit,
                    True,
                    f"{part.ref} {explicit_src} {fmt_amps(explicit)} A is the entered full-load current. "
                    "NEC tables are not used on an IEC drawing.",
                    "IEC nameplate current",
                    phases,
                )
            ]
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "motor",
                None,
                True,
                f"{part.ref} is on an IEC drawing. NEC Tables 430.248 and 430.250 are not applied. "
                "Enter the motor full-load current.",
                "IEC nameplate current",
            )
        ]
    if phases not in {1, 3}:
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "motor",
                None,
                True,
                f"{part.ref} has no phase count, and the supply phase count is unknown. "
                "The motor current is left blank.",
                "NEC Table 430.250",
            )
        ]
    looked = None if hp is None or volts is None else motor_flc(hp=hp, voltage=volts, phases=phases)
    off_table = hp is not None and volts is not None and looked is None
    if off_table:
        if explicit is not None and source != "nec-table":
            return [
                WorkingLine(
                    supply_id,
                    part.ref,
                    "motor",
                    explicit,
                    True,
                    f"{part.ref} {fmt_amps(hp)} hp is not a published NEC table row. "
                    f"The entered {fmt_amps(explicit)} A is used. A neighboring table row is not.",
                    "entered full-load current",
                    phases,
                )
            ]
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "motor",
                None,
                True,
                f"{part.ref} horsepower {fmt_amps(hp)} at {fmt_amps(volts)} V is not a published "
                f"{'single-phase' if phases == 1 else 'three-phase'} NEC table row. "
                "The plate does not snap to a neighboring horsepower. Enter the motor full-load current.",
                "NEC Table 430.248" if phases == 1 else "NEC Table 430.250",
            )
        ]
    if explicit is not None:
        text = f"{part.ref} {explicit_src} {fmt_amps(explicit)} A is the load current in the sum."
        return [WorkingLine(supply_id, part.ref, "motor", explicit, True, text, "drawing ampere rating", phases)]
    if looked is None:
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "motor",
                None,
                True,
                f"{part.ref} has no drawing ampere rating, and horsepower {hp if hp is not None else '—'} at "
                f"{fmt_amps(volts) if volts is not None else '—'} V is not a published "
                f"{'single-phase' if phases == 1 else 'three-phase'} NEC table cell. "
                "The full-load sum stays incomplete.",
                "NEC Table 430.248" if phases == 1 else "NEC Table 430.250",
            )
        ]
    _table_hp, column, amps = looked
    table = "NEC Table 430.248" if phases == 1 else "NEC Table 430.250"
    return [
        WorkingLine(
            supply_id,
            part.ref,
            "motor",
            float(amps),
            True,
            f"{part.ref} {fmt_amps(hp)} hp at {fmt_amps(volts)} V uses the {fmt_amps(column)} V column: "
            f"{fmt_amps(float(amps))} A ({table}).",
            table,
            phases,
        )
    ]


def _drive_line(part: _Part, supply_id: str, supply_voltage: float | None, supply_phases: int | None) -> list[WorkingLine]:
    phases = _phase_count(part, supply_phases)
    explicit, src = _explicit_load_amps(part.props)
    if explicit is None:
        return [
            WorkingLine(
                supply_id,
                part.ref,
                "drive",
                None,
                True,
                f"{part.ref} has no input current. Output current (Iout) is not used as the supply full-load current. "
                "Enter Iin or amps. The sum stays incomplete.",
                "UL 508A 49.2",
            )
        ]
    return [
        WorkingLine(
            supply_id,
            part.ref,
            "drive",
            explicit,
            True,
            f"{part.ref} {src} {fmt_amps(explicit)} A is the drive input in the sum.",
            "drawing ampere rating",
            phases,
        )
    ]


def _main_ocpd(members: list[_Part]) -> tuple[float | None, str, str]:
    devices = [part for part in members if _classify(part) == "ocpd"]
    if not devices:
        return None, "", ""
    marked = [
        part
        for part in devices
        if _truthy_flag(part.props.get("main")) is True or str(part.props.get("role") or "").lower() == "main"
    ]
    pool = marked or devices
    rated: list[tuple[_Part, float]] = []
    for part in pool:
        amps = parse_amps(_first(part.props, "In", "Ir", "amps", "rating"))
        if amps is None:
            amps = _num(_first(part.props, "In", "Ir", "amps", "rating"))
        if amps is not None:
            rated.append((part, amps))
    if not rated:
        return None, "", ""
    if marked:
        part, amps = max(rated, key=lambda item: item[1])
        return amps, part.ref, ""
    if len(devices) == 1:
        part, amps = rated[0]
        return amps, part.ref, ""
    part, amps = max(rated, key=lambda item: item[1])
    note = (
        f"{part.ref} is the largest protective-device rating on its supply ({fmt_amps(amps)} A). "
        "It is not marked main. Confirm it is the disconnect the full-load current is compared with."
    )
    return amps, part.ref, note


def _identity_fields(project: Project, overrides: dict[str, Any]) -> list[Field]:
    company = str(project.title_block.company or "").strip()
    trademark = str(project.properties.get("trademark") or "").strip()
    fields = [
        _resolved_field(
            overrides,
            "manufacturer",
            "MANUFACTURER",
            company,
            company,
            "title block company",
            required=False,
            citation="NEC 409.110(1)",
        ),
        _resolved_field(
            overrides,
            "trademark",
            "TRADEMARK",
            trademark,
            trademark,
            "project trademark",
            required=False,
            citation="NEC 409.110(1)",
        ),
        _resolved_field(
            overrides,
            "catalog_model",
            "CATALOG / MODEL",
            str(project.properties.get("catalog") or project.properties.get("model") or "").strip(),
            str(project.properties.get("catalog") or project.properties.get("model") or "").strip(),
            "project catalog or model",
            required=False,
            citation="",
            engineer_confirm=True,
            note=_shop_confirm_note("catalog or model"),
        ),
        _resolved_field(
            overrides,
            "job_number",
            "JOB NUMBER",
            str(_first(project.properties, "job", "job_number", "job_no") or "").strip(),
            str(_first(project.properties, "job", "job_number", "job_no") or "").strip(),
            "project job number",
            required=False,
            citation="",
            engineer_confirm=True,
            note=_shop_confirm_note("job number"),
        ),
    ]
    manufacturer = fields[0]
    trademark_field = fields[1]
    if not manufacturer.value and not trademark_field.value:
        manufacturer.required = True
        manufacturer.engineer_confirm = True
    return fields


def _resolved_field(
    overrides: dict[str, Any],
    key: str,
    label: str,
    computed: str,
    raw: Any,
    source: str,
    *,
    required: bool,
    citation: str,
    engineer_confirm: bool = False,
    note: str = "",
) -> Field:
    value, present = _override_for(overrides, key)
    if present:
        text = "" if value is None else str(value).strip()
        number = _num(value) if text else None
        if text:
            text = _engraved_override(key, text)
        return Field(
            key=key,
            label=label,
            value=text,
            computed=computed,
            raw=number if number is not None and key.split(":")[0] in {"voltage", "phases", "frequency", "fla", "sccr", "largest_load", "max_ocpd"} else value,
            source="override",
            overridden=True,
            required=required,
            engineer_confirm=False,
            citation=citation,
            note="",
        )
    return Field(
        key=key,
        label=label,
        value=computed,
        computed=computed,
        raw=raw,
        source=source,
        overridden=False,
        required=required,
        engineer_confirm=engineer_confirm,
        citation=citation,
        note=note,
    )


def _enclosure_type(project: Project) -> tuple[str, str]:
    found: list[tuple[str, str]] = []
    for sheet in project.sheets:
        if sheet.enclosure is None:
            continue
        text = str(sheet.enclosure.nema_type or "").strip()
        if text:
            found.append((text, sheet.id))
    if not found:
        return "", ""
    first, sheet_id = found[0]
    if any(text != first for text, _sid in found[1:]):
        return first, f"enclosure on {sheet_id} (other sheets differ — confirm)"
    return first, f"enclosure on {sheet_id}"


def _date_code(project: Project) -> tuple[str, str]:
    explicit = str(project.properties.get("date_code") or "").strip()
    if explicit:
        return explicit, "project date_code"
    for rev in reversed(project.title_block.revisions):
        if str(rev.date or "").strip():
            return str(rev.date).strip(), "title block revision date"
    return "", ""


def _largest_load(lines: list[WorkingLine]) -> tuple[float | None, str]:
    pool = [
        line
        for line in lines
        if line.included and line.amperes is not None and line.kind in {"motor", "load", "drive"}
    ]
    if not pool:
        return None, ""
    line = max(pool, key=lambda item: float(item.amperes or 0))
    return float(line.amperes or 0), line.ref


def _service_field(service: bool | None) -> Field:
    if service is True:
        return Field(
            key="service_equipment",
            label="SERVICE EQUIPMENT",
            value="SUITABLE FOR USE AS SERVICE EQUIPMENT",
            raw=True,
            source="nameplate setting",
            citation="NEC 409.110(5)",
        )
    if service is False:
        return Field(
            key="service_equipment",
            label="SERVICE EQUIPMENT",
            value="No",
            raw=False,
            source="nameplate setting",
            citation="NEC 409.110(5)",
            hidden=True,
            note="Not service equipment. The marking is omitted.",
        )
    return Field(
        key="service_equipment",
        label="SERVICE EQUIPMENT",
        value="",
        raw=None,
        source="",
        required=False,
        engineer_confirm=True,
        citation="NEC 409.110(5)",
        hidden=True,
        note="Confirm whether this panel is service equipment. The marking is required only when it is.",
    )


def _supplies_at_or_above_50(fields: list[Field], supply_ids: list[str], primary: str) -> list[str]:
    found: list[str] = []
    multi = len(supply_ids) > 1
    for supply_id in supply_ids:
        key = _field_key("voltage", supply_id, primary, multi)
        field = next((item for item in fields if item.key == key), None)
        if field is None or field.raw is None:
            continue
        try:
            volts = float(field.raw)
        except (TypeError, ValueError):
            volts = _num(field.value) or 0
        if volts >= 50:
            found.append(supply_id)
    return found


def _apply_interlocks_from_parts(
    lines: list[WorkingLine],
    group_of: dict[str, str],
) -> list[WorkingLine]:
    buckets: dict[tuple[str, str], list[WorkingLine]] = {}
    order: list[WorkingLine] = []
    for line in lines:
        group = group_of.get(line.ref, "")
        if group and line.included and line.kind in {"motor", "load", "drive"}:
            buckets.setdefault((line.supply_id, group), []).append(line)
        else:
            order.append(line)
    for (supply_id, group), members in buckets.items():
        known = [line for line in members if line.amperes is not None]
        unknown = [line for line in members if line.amperes is None]
        if unknown:
            for line in members:
                line.included = line.amperes is None
                if line.amperes is not None:
                    line.included = False
                    line.text += f" Interlock group {group}: a member has no current, so the largest cannot be chosen."
                else:
                    line.text += f" Interlock group {group} is incomplete."
            order.extend(members)
            continue
        winner = max(known, key=lambda item: float(item.amperes or 0))
        for line in members:
            if line is winner:
                line.text += f" Interlock group {group}: this is the largest, so it is the one in the sum."
            else:
                line.included = False
                line.text += (
                    f" Interlock group {group}: left out because {winner.ref} is larger "
                    f"({fmt_amps(winner.amperes)} A). Only loads able to operate together are summed."
                )
            order.append(line)
    return order


def collect_issues(
    plate: Nameplate,
    ocpd_amp: dict[str, float | None],
    ocpd_ref: dict[str, str],
    caution: bool,
    locations: str,
    available_fault_ka: float | None,
    *,
    primary: str = "1",
    caution_confirmed: bool = False,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for item in plate.fields:
        if item.required and not str(item.value).strip():
            extra = f" {item.note}" if item.note else ""
            cite = f" ({item.citation})" if item.citation else ""
            issues.append(
                {
                    "check": "nameplate_missing_field",
                    "severity": "warning",
                    "message": f"Nameplate {item.label} is blank.{cite}{extra}",
                    "target": item.key,
                    "ref": "",
                }
            )
    catalog = plate.field("catalog_model")
    job = plate.field("job_number")
    if catalog is not None and job is not None and not catalog.value and not job.value:
        issues.append(
            {
                "check": "nameplate_missing_field",
                "severity": "warning",
                "message": (
                    "Nameplate catalog/model and job number are both blank. "
                    "Confirm before engraving. NEC 409.110 does not list a model or job designation, "
                    "and no UL 508A clause for it was found."
                ),
                "target": "designation",
                "ref": "",
            }
        )
    if caution and not locations:
        issues.append(
            {
                "check": "nameplate_missing_field",
                "severity": "warning",
                "message": (
                    "More than one supply is 50 V or more. NEC 409.110(3) requires the disconnect "
                    "locations to be documented. disconnect_locations is blank."
                ),
                "target": "disconnect_locations",
                "ref": "",
            }
        )
    sccr = plate.field("sccr")
    if available_fault_ka is not None and sccr is not None:
        marked = sccr.raw if isinstance(sccr.raw, (int, float)) else _num(sccr.value)
        if marked is None:
            issues.append(
                {
                    "check": "nameplate_sccr_below_available",
                    "severity": "warning",
                    "message": (
                        f"Nameplate SCCR is blank, so it cannot be shown to meet the available fault "
                        f"current of {_format_ka(available_fault_ka)} kA (NEC 409.110(4), NEC 409.22)."
                    ),
                    "target": "sccr",
                    "ref": "",
                }
            )
        elif float(marked) + 1e-9 < float(available_fault_ka):
            issues.append(
                {
                    "check": "nameplate_sccr_below_available",
                    "severity": "warning",
                    "message": (
                        f"Nameplate SCCR {_format_ka(float(marked))} kA is below the available fault "
                        f"current of {_format_ka(available_fault_ka)} kA (NEC 409.110(4), NEC 409.22)."
                    ),
                    "target": "sccr",
                    "ref": "",
                }
            )
    for item in plate.fields:
        base = item.key.split(":", 1)[0]
        if item.overridden or not str(item.value).strip():
            continue
        if base in _CONFIRM_BASES and item.engineer_confirm:
            issues.append(
                {
                    "check": "nameplate_unconfirmed",
                    "severity": "warning",
                    "message": f"Nameplate {item.label} is not confirmed. Confirm it before engraving.",
                    "target": item.key,
                    "ref": "",
                }
            )
        if base == "fla" and _FLC_NOTE in (item.note or ""):
            issues.append(
                {
                    "check": "nameplate_flc_approximate",
                    "severity": "warning",
                    "message": f"Nameplate {item.label} is an approximation. {_FLC_NOTE}",
                    "target": item.key,
                    "ref": "",
                }
            )
        if base == "sccr" and item.engineer_confirm and item.note:
            issues.append(
                {
                    "check": "nameplate_unconfirmed",
                    "severity": "warning",
                    "message": f"Nameplate SCCR voltage is not confirmed. {item.note}",
                    "target": "sccr",
                    "ref": "",
                }
            )
    if caution and not caution_confirmed:
        issues.append(
            {
                "check": "nameplate_unconfirmed",
                "severity": "warning",
                "message": (
                    "Caution wording is a template. Verify against UL 508A 55.4 / NEC 409 before engraving."
                ),
                "target": "caution_text",
                "ref": "",
            }
        )
    for key, amps in ocpd_amp.items():
        if amps is None:
            continue
        fla_key = "fla" if key == primary else f"fla:{key}"
        fla_field = plate.field(fla_key) or (plate.field("fla") if key == primary else None)
        if fla_field is None:
            continue
        fla = fla_field.raw if isinstance(fla_field.raw, (int, float)) else _num(fla_field.value)
        if fla is None:
            continue
        if float(fla) > float(amps) + 0.05:
            issues.append(
                {
                    "check": "nameplate_fla_above_ocpd",
                    "severity": "warning",
                    "message": (
                        f"Nameplate full-load current {fmt_amps(float(fla))} A on supply {key} "
                        f"is above {ocpd_ref.get(key) or 'the main device'} rated {fmt_amps(float(amps))} A."
                    ),
                    "target": fla_field.key,
                    "ref": ocpd_ref.get(key, ""),
                }
            )
    return issues


def _on_plate(item: Field) -> bool:
    """Rows engraved on the plate. Identity fields are the title and the footer."""

    if item.hidden or item.key in {
        "manufacturer",
        "trademark",
        "catalog_model",
        "job_number",
        "service_equipment",
    }:
        return False
    base = item.key.split(":", 1)[0]
    if base in {"voltage", "phases", "frequency", "fla", "sccr", "enclosure_type", "drawing_number", "date_code"}:
        return True
    return bool(item.value or item.required)


def _content_margin(width: float, height: float, diameter_in: float | None, inset_in: float | None) -> float:
    """Keep engraved text off the corner mounting holes."""

    margin = 2.8
    if diameter_in is None or inset_in is None or diameter_in <= 0 or inset_in <= 0:
        return margin
    radius = diameter_in * INCH_MM / 2.0
    inset = inset_in * INCH_MM
    if inset < radius or inset > min(width, height) / 2.0:
        return margin
    return max(margin, inset + radius + 1.2)


def layout_marks(plate: Nameplate, margin: float = 2.8) -> list[Mark]:
    """Place plate text inside the cut outline. Y grows downward."""

    width = plate.width_mm
    height = plate.height_mm
    marks: list[Mark] = [Mark("CUT", "rect", 0.0, 0.0, width, height)]
    y = margin + 0.4
    manufacturer = plate.field("manufacturer")
    trademark = plate.field("trademark")
    title = (manufacturer.value if manufacturer else "") or (trademark.value if trademark else "")
    title_size = 3.2 if height >= 70 else 2.6
    title_width = width - 2 * margin
    title_x = margin + 0.8
    if title:
        marks.append(Mark("TEXT", "text", title_x, y + title_size, text=_fit(title, title_width - 0.8, title_size), size=title_size))
    y += title_size + 1.6
    if trademark and trademark.value and manufacturer and manufacturer.value and trademark.value != manufacturer.value:
        marks.append(
            Mark(
                "TEXT",
                "text",
                margin,
                y + 2.2,
                text=_fit(trademark.value, width - 2 * margin, 2.2),
                size=2.2,
            )
        )
        y += 3.2
    marks.append(Mark("TEXT", "line", margin, y, w=width - 2 * margin))
    y += 1.8

    rows = [item for item in plate.fields if _on_plate(item)]
    service = plate.field("service_equipment")
    catalog = plate.field("catalog_model")
    job = plate.field("job_number")
    left = f"CAT {catalog.value}" if catalog and catalog.value else ""
    right = f"JOB {job.value}" if job and job.value else ""
    foot_text = "   ".join(bit for bit in (left, right) if bit)
    foot_flag = ""
    for item in (catalog, job):
        if item is not None and item.value and item.engineer_confirm and not item.overridden:
            foot_flag = "CONFIRM"
    service_line = bool(service is not None and service.value and not service.hidden)
    foot_size = 1.6
    footer_lines = int(service_line) + int(bool(foot_text))
    footer_h = 1.2 + footer_lines * (foot_size + 1.5)
    bottom = height - margin - footer_h
    _paint_rows(
        marks,
        rows,
        x0=margin,
        y0=y,
        width=width,
        margin=margin,
        bottom=bottom,
    )

    cursor = height - margin - 0.6
    foot_badge = 12.0 if foot_flag else 0.0
    if foot_text:
        marks.append(
            Mark(
                "TEXT",
                "text",
                margin + 0.4,
                cursor,
                text=_fit(foot_text, width - 2 * margin - foot_badge - 1.2, foot_size),
                size=foot_size,
            )
        )
        if foot_flag:
            _badge(marks, width - margin - foot_badge, cursor - foot_size, foot_badge, foot_size + 0.8, foot_flag)
        cursor -= foot_size + 1.5
    if service_line and service is not None:
        marks.append(
            Mark(
                "TEXT",
                "text",
                margin,
                cursor,
                text=_fit(service.value, width - 2 * margin, foot_size),
                size=foot_size,
            )
        )
    return marks


def _flag_label(item: Field) -> str:
    if item.overridden:
        return "OVR"
    if not str(item.value).strip() or not item.engineer_confirm:
        return ""
    base = item.key.split(":", 1)[0]
    if base == "fla" and _FLC_NOTE in (item.note or ""):
        return "VERIFY"
    if base in _CONFIRM_BASES or (base == "sccr" and item.note):
        return "CONFIRM"
    return ""


def _supply_blocks(rows: list[Field]) -> tuple[list[list[Field]], list[Field]]:
    """One block per incoming supply, then the fields that are not per-supply."""

    grouped: dict[str, list[Field]] = {}
    order: list[str] = []
    shared: list[Field] = []
    for item in rows:
        base, _sep, suffix = item.key.partition(":")
        if base in _SUPPLY_FIELD_BASES:
            sid = suffix or "1"
            if sid not in grouped:
                order.append(sid)
                grouped[sid] = []
            grouped[sid].append(item)
        else:
            shared.append(item)
    return [grouped[sid] for sid in order], shared


def _type_for(row_h: float) -> tuple[float, float]:
    size = min(2.15, row_h * 0.50)
    label_size = min(size * 0.88, row_h * 0.44)
    return size, label_size


# Gap after the longest label in a column, measured with the font the page draws.
_VALUE_GAP_MM = 4.0


def _value_column(labels: list[str], label_size: float, cap: float) -> float:
    """Where values start: the real width of the longest label, plus a visible gap."""

    if not labels:
        return min(16.0, cap)
    from panelcad.render.qt_painter import text_advance_mm

    widest = max(text_advance_mm(text, label_size) for text in labels)
    return min(widest + _VALUE_GAP_MM, cap)


def _paint_field_column(
    marks: list[Mark],
    fields: list[Field],
    x: float,
    y: float,
    col_w: float,
    row_h: float,
    size: float,
    label_size: float,
    bottom: float,
    value_x: float | None = None,
) -> None:
    if not fields:
        return
    cap = max(16.0, col_w * 0.52)
    shown = [_display_label(item.label, cap - 0.6, label_size) for item in fields]
    label_w = value_x if value_x is not None else _value_column(shown, label_size, cap)
    for index, item in enumerate(fields):
        baseline = y + index * row_h + row_h * 0.72
        if baseline > bottom:
            continue
        flag = _flag_label(item)
        # A typed value is engraved as itself. OVR stays in the dialog and the working.
        if flag == "OVR":
            flag = ""
        flag_w = 13.0 if flag in {"CONFIRM", "VERIFY"} else (8.0 if flag else 0.0)
        marks.append(
            Mark(
                "TEXT",
                "text",
                x,
                baseline,
                text=shown[index],
                size=label_size,
            )
        )
        room = col_w - label_w - flag_w - 0.8
        marks.append(
            Mark(
                "TEXT",
                "text",
                x + label_w,
                baseline,
                text=_fit(item.value or "—", max(6.0, room), size),
                size=size,
            )
        )
        if flag:
            _badge(marks, x + col_w - flag_w, baseline - size, flag_w, size + 0.7, flag)


def _paint_rows(
    marks: list[Mark],
    rows: list[Field],
    *,
    x0: float,
    y0: float,
    width: float,
    margin: float,
    bottom: float,
) -> None:
    """Each supply is one block. Columns never split a supply's own rows."""

    if not rows:
        return
    supply_blocks, shared = _supply_blocks(rows)
    avail = max(6.0, bottom - y0)
    span = width - 2 * margin
    side_by_side = len(supply_blocks) >= 2 and span >= 80
    if side_by_side:
        gutter = 3.0
        col_w = (span - gutter) / 2.0
        bands = [supply_blocks[index : index + 2] for index in range(0, len(supply_blocks), 2)]
        band_rows = [max(len(block) for block in band) for band in bands]
        total = sum(band_rows) + len(shared)
        row_h = min(5.2, avail / max(1, total))
        size, label_size = _type_for(row_h)
        y = y0
        for band, rows_in_band in zip(bands, band_rows):
            for col, block in enumerate(band):
                _paint_field_column(
                    marks, block, x0 + col * (col_w + gutter), y, col_w, row_h, size, label_size, bottom
                )
            y += rows_in_band * row_h
        if shared:
            _paint_field_column(marks, shared, x0, y, span, row_h, size, label_size, bottom)
        return
    beside = bool(supply_blocks) and bool(shared) and span >= 80 and avail / max(1, len(rows)) < 3.5
    if beside:
        gutter = 3.0
        col_w = (span - gutter) / 2.0
        total = max(len(supply_blocks[0]), len(shared))
        row_h = min(5.2, avail / max(1, total))
        size, label_size = _type_for(row_h)
        _paint_field_column(marks, supply_blocks[0], x0, y0, col_w, row_h, size, label_size, bottom)
        _paint_field_column(marks, shared, x0 + col_w + gutter, y0, col_w, row_h, size, label_size, bottom)
        return
    blocks = supply_blocks + ([shared] if shared else [])
    total = sum(len(block) for block in blocks)
    row_h = min(5.2, avail / max(1, total))
    size, label_size = _type_for(row_h)
    cap = max(16.0, span * 0.52)
    shown_all = [
        _display_label(item.label, cap - 0.6, label_size) for block in blocks for item in block
    ]
    value_x = _value_column(shown_all, label_size, cap)
    y = y0
    for block in blocks:
        _paint_field_column(
            marks, block, x0, y, span, row_h, size, label_size, bottom, value_x=value_x
        )
        y += len(block) * row_h


def layout_holes(marks: list[Mark], width: float, height: float, diameter_in: float | None, inset_in: float | None) -> list[Mark]:
    if diameter_in is None or inset_in is None or diameter_in <= 0 or inset_in <= 0:
        return marks
    diameter = diameter_in * INCH_MM
    inset = inset_in * INCH_MM
    radius = diameter / 2.0
    if inset < radius or inset > min(width, height) / 2.0:
        return marks
    kept = [mark for mark in marks if mark.layer != "HOLE"]
    for cx, cy in (
        (inset, inset),
        (width - inset, inset),
        (inset, height - inset),
        (width - inset, height - inset),
    ):
        kept.append(Mark("HOLE", "circle", cx, cy, r=radius))
    return kept


def _badge(marks: list[Mark], x: float, y: float, w: float, h: float, text: str = "OVR") -> None:
    box_h = max(2.6, h)
    marks.append(Mark("TEXT", "rect", x, y, w, box_h))
    marks.append(
        Mark(
            "TEXT",
            "text",
            x + w / 2.0,
            y + box_h * 0.72,
            text=text,
            size=1.15 if len(text) > 3 else 1.4,
            align="middle",
        )
    )


def layout_caution(
    locations: str,
    text: str = "",
    confirmed: bool = False,
) -> tuple[list[Mark], tuple[float, float]]:
    """Caution label. Unconfirmed wording carries the verify line; it is not a finished engraving."""

    width = 5.0 * INCH_MM
    margin = 4.0
    body = CAUTION_BODY_MM
    body_text = (text or CAUTION_TEMPLATE).strip() or CAUTION_TEMPLATE
    lines: list[tuple[str, float]] = []
    if not confirmed:
        lines.append((CAUTION_VERIFY, body))
    lines.append((body_text, body))
    if locations.strip():
        lines.append((f"Disconnect locations: {locations.strip()}", body))
    marks = [Mark("CUT", "rect", 0.0, 0.0, width, 10.0)]
    y = margin
    for content, size in lines:
        for part in _wrap(content, width - 2 * margin, size):
            y += size + 1.0
            marks.append(Mark("TEXT", "text", margin, y, text=part, size=size))
        y += 0.7
    # Baseline plus descent stays inside the cut. Snap up to a whole PDF point
    # so the page edge and the cut are the same line (a shorter page left the
    # bottom stroke fully inside while the other three were clipped in half).
    height = _snap_pdf_mm(y + margin + body * 0.5)
    marks[0] = Mark("CUT", "rect", 0.0, 0.0, width, height)
    return marks, (width, height)


def _snap_pdf_mm(mm: float) -> float:
    """Smallest millimetre length that is a multiple of 6 PDF points and not shorter than ``mm``.

    6 pt is 25 px at 300 dpi. A height such as 55 pt is 229.17 px, and the
    rasterizer's extra row makes the bottom stroke one pixel heavier than the
    other three.
    """

    points = math.ceil(mm / INCH_MM * 72.0 - 1e-9)
    remainder = points % 6
    if remainder:
        points += 6 - remainder
    return points * INCH_MM / 72.0


def _wrap(text: str, width_mm: float, size: float) -> list[str]:
    limit = max(8, int(width_mm / max(size, 0.8) / 0.62))
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = word if not current else f"{current} {word}"
        if len(trial) <= limit:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def title_block_lines(project: Project, catalog: Catalog | None) -> list[str]:
    """Two short lines for the sheet title block. Empty strings are omitted by the caller."""

    if catalog is None:
        return []
    plate = build_nameplate(project, catalog)
    voltage = _first_value(plate, "voltage")
    phases = _first_value(plate, "phases")
    frequency = _first_value(plate, "frequency")
    fla = _first_value(plate, "fla")
    # Phases alone (a motor's pole count) is not a supply marking. 0.5.0 left this blank.
    bits: list[str] = []
    if voltage or fla:
        bits = [bit for bit in (voltage, f"{phases} PH" if phases else "", frequency, f"FLA {fla}" if fla else "") if bit]
    sccr = _first_value(plate, "sccr")
    enclosure = _first_value(plate, "enclosure_type")
    second = [bit for bit in (f"SCCR {sccr}" if sccr else "", f"TYPE {enclosure}" if enclosure else "") if bit]
    return ["  ".join(bits), "  ".join(second)]


def _shop_note_lines(fields: list[Field]) -> list[WorkingLine]:
    """The full shop-field note. The dialog placeholder stays short."""

    lines: list[WorkingLine] = []
    for item in fields:
        base = item.key.split(":", 1)[0]
        note = str(item.note or "").strip()
        if base not in _CONFIRM_BASES or not note:
            continue
        supply_id = item.key.split(":", 1)[1] if ":" in item.key else ""
        lines.append(WorkingLine(supply_id, "", "note", None, False, f"{item.label}: {note}", ""))
    return lines


def _override_lines(fields: list[Field]) -> list[WorkingLine]:
    """Say what was typed. The plate engraves the value and does not print OVR."""

    lines: list[WorkingLine] = []
    for item in fields:
        if not item.overridden or item.hidden:
            continue
        value = str(item.value or "").strip()
        if not value:
            continue
        computed = str(item.computed or "").strip()
        if computed and computed != value:
            detail = f"Overridden: {value} (computed {computed})"
        else:
            detail = f"Overridden: {value}"
        supply_id = item.key.split(":", 1)[1] if ":" in item.key else ""
        lines.append(WorkingLine(supply_id, "", "override", None, False, f"{item.label}: {detail}", ""))
    return lines


def _first_value(plate: Nameplate, prefix: str) -> str:
    for item in plate.fields:
        if item.key == prefix or item.key.startswith(prefix + ":"):
            return item.value
    return ""


def apply_nameplate_update(project: Project, updates: dict[str, Any]) -> None:
    """Write overrides and plate options onto ``project.properties['nameplate']``."""

    store = dict(_store(project))
    overrides = dict(_overrides(project))
    option_keys = {
        "preset",
        "width_in",
        "height_in",
        "hole_diameter_in",
        "hole_inset_in",
        "service_equipment",
        "control_circuit_only",
        "disconnect_locations",
        "field_bcpd",
        "caution_text",
        "caution_confirmed",
    }
    flag_keys = {"service_equipment", "control_circuit_only", "caution_confirmed"}
    clear = updates.get("clear") or []
    if isinstance(clear, str):
        clear = [part.strip() for part in clear.split(",") if part.strip()]
    for raw_key in clear:
        key = _canonical_override_key(str(raw_key))
        overrides.pop(key, None)
        overrides.pop(str(raw_key), None)
        if key in option_keys:
            store.pop(key, None)
    unknown = []
    for raw_key, value in updates.items():
        if raw_key in {"clear", "id", "op"}:
            continue
        key = _canonical_override_key(str(raw_key))
        if key in option_keys:
            if key in flag_keys:
                flag = _truthy_flag(value)
                if value in {"", None}:
                    store.pop(key, None)
                else:
                    if flag is None:
                        raise ValueError(f"{key} must be yes or no")
                    store[key] = flag
            elif value in {"", None}:
                store.pop(key, None)
            else:
                store[key] = value
            continue
        if _known_override_key(key):
            if value is None:
                overrides.pop(key, None)
            else:
                overrides[key] = value
            continue
        unknown.append(str(raw_key))
    if unknown:
        raise ValueError(f"unknown nameplate field {unknown[0]}")
    if store.get("preset") == "3x5":
        store.pop("width_in", None)
        store.pop("height_in", None)
    # Drop a legacy primary-supply suffix once the canonical key is stored.
    for legacy in [item for item in list(overrides) if item.endswith(":1")]:
        base = legacy.split(":", 1)[0]
        overrides.setdefault(base, overrides.pop(legacy))
    store["overrides"] = overrides
    project.properties[STORE_KEY] = store


def _known_override_key(key: str) -> bool:
    base = key.split(":", 1)[0]
    return base in {
        "manufacturer",
        "trademark",
        "catalog_model",
        "job_number",
        "voltage",
        "phases",
        "frequency",
        "fla",
        "sccr",
        "enclosure_type",
        "drawing_number",
        "date_code",
        "largest_load",
        "max_ocpd",
        "field_bcpd",
        "service_equipment",
    }


NAMEPLATE_PROMPT = "Fill in the nameplate before printing: Reports → Nameplate…"


def nameplate_checks_active(project: Project) -> bool:
    """Blank projects wait. A placed part, or opening the nameplate, starts the checks."""

    if isinstance(project.properties.get(STORE_KEY), dict):
        return True
    return any(sheet.components for sheet in project.sheets)


def note_nameplate_started(project: Project) -> None:
    """Remember that Reports → Nameplate was opened, without inventing field values."""

    if isinstance(project.properties.get(STORE_KEY), dict):
        return
    project.properties[STORE_KEY] = {"started": True}


def nameplate_findings(project: Project, catalog: Catalog | None = None) -> list[Any]:
    from panelcad.core.checks import Finding

    if not nameplate_checks_active(project):
        return [
            Finding(
                "nameplate_missing_field",
                "info",
                NAMEPLATE_PROMPT,
                target="nameplate",
            )
        ]
    plate = build_nameplate(project, catalog)
    findings = []
    for issue in plate.issues:
        findings.append(
            Finding(
                issue["check"],
                issue.get("severity") or "warning",  # type: ignore[arg-type]
                issue["message"],
                ref=issue.get("ref") or "",
                target=issue.get("target") or "",
            )
        )
    return findings
