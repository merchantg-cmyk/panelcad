"""Enclosure thermal load, temperature rise, and cooling recommendation.

Reads the enclosure already on the panel sheet and the in-cabinet BOM.
It does not choose or resize the box.

Method
------
Effective cooling surface and the natural temperature rise follow the
installation formulas Rittal publishes for IEC 60890 (IEC 890) /
DIN VDE 0660 part 500:

    ΔT = Qv / (k · A)
    Qe = Qv − k · A · ΔT
    V  = f · Qv / ΔT          filter-fan airflow
    QH = k · A · ΔT           heater, unpowered enclosure

Constants and URLs are in ``CITATIONS``. Shop estimates are never labeled
as manufacturer data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from panelcad.core.jumper import is_jumper_device
from panelcad.core.models import Component, Device, Enclosure, Project, Sheet
from panelcad.library.catalog import Catalog
from panelcad.library.panel import NOT_IN_PANEL_CATEGORIES, effective_footprint
from panelcad.library.parts import CatalogPart, PartsCatalog

THERMAL_KEY = "thermal"

# 1 W = 3.412141633 BTU/h (International Table). 1 m³/h = 0.58857778 CFM.
BTU_H_PER_W = 3.412141633
CFM_PER_M3H = 35.314666721 / 60.0
HP_TO_KW = 0.745699872

# Shop caution when natural convection is inside the allowance but the tightest
# part margin is this close. Not an IEC 60890 figure and not a code value.
HEADROOM_CAUTION_K = 2.0

# Shop placeholders. Not manufacturer data. A user estimate replaces them.
DEFAULT_CATEGORY_ESTIMATES_W: dict[str, float] = {
    "control.plc": 5.0,
    "control.hmi": 8.0,
    "switching.contactor": 4.0,
    "switching.relay": 1.0,
    "switching.safety": 3.0,
    "switching.aux": 0.0,
    "timing.timer": 2.0,
    "protection.breaker": 4.0,
    "protection.mpcb": 6.0,
    "protection.overload": 2.0,
    "protection.fuse": 2.0,
    "protection.disconnect": 5.0,
    "operator.light": 1.0,
    "operator.pushbutton": 0.0,
    "operator.estop": 0.0,
    "operator.selector": 0.0,
    "operator.limit": 0.0,
    "power.supply": 10.0,
    "power.transformer": 8.0,
    "drive.vfd": 25.0,
    "drive.softstarter": 15.0,
    "switching.softstarter": 15.0,
    "connection.terminal": 0.0,
    "connection.network": 2.0,
    "connection.accessory": 0.0,
}

# Rittal sea-level bands (Technical System Catalog). Above 1000 m, the
# enclosure-cooling user guide steps.
_AIR_BANDS_M3K_PER_WH: tuple[tuple[float, float], ...] = (
    (100.0, 3.1),
    (250.0, 3.2),
    (500.0, 3.3),
    (750.0, 3.4),
    (1000.0, 3.5),
    (1500.0, 3.8),
    (2000.0, 4.1),
    (2500.0, 4.4),
    (3000.0, 4.8),
    (3500.0, 5.2),
)

_AREA_FORMULAS: dict[str, str] = {
    "free": "Single enclosure, free-standing on all sides: A = 1.8·H·(W+D) + 1.4·W·D",
    "wall": "Single enclosure, wall-mounted: A = 1.4·W·(H+D) + 1.8·H·D",
    "row_end_free": "First or last in a row, free-standing: A = 1.4·D·(W+H) + 1.8·W·H",
    "row_end_wall": "First or last in a row, wall-mounted: A = 1.4·H·(W+D) + 1.4·W·D",
    "row_mid_free": "Middle of a row, free-standing: A = 1.8·W·H + 1.4·W·D + H·D",
    "row_mid_wall": "Middle of a row, wall-mounted: A = 1.4·W·(H+D) + H·D",
    "row_mid_wall_covered": "Middle of a row, wall-mounted, covered roof: A = 1.4·W·H + 0.7·W·D + H·D",
}

CITATIONS: tuple[dict[str, str], ...] = (
    {
        "id": "iec-60890",
        "title": "IEC 60890:1987 — temperature-rise verification of low-voltage assemblies by calculation (also cited as IEC 890)",
        "url": "https://webstore.iec.ch/en/publication/3776",
        "use": "Installation method behind the effective cooling surface. Working formulas below are the ones Rittal publishes for that method.",
    },
    {
        "id": "rittal-catalog",
        "title": "Rittal Technical System Catalog — enclosure climate control",
        "url": "https://www.rittal.com/us_en/apps/download/img/uploads/US1113-BR-TR%20Technical%20Cooling%20System%20Catalog.pdf",
        "use": "ΔT and Qe, sheet-steel k = 5.5 W/m²K, fan factor f (3.1 m³·K/Wh at 0–100 m), heater QH = A·ΔT·k (doubled outdoors).",
    },
    {
        "id": "rittal-area",
        "title": "Rittal — Selecting and Operating Energy-Efficient Climate Products; enclosure cooling guide (IEC 890 area table)",
        "url": "https://www.rittal.com/us_en/apps/download/img/uploads/Selecting%20and%20Operating%20Energy-Efficient%20Climate%20Products%20for%20Industrial%20Enclosures.pdf",
        "use": "Wall-mounted example A = 1.4·W·(H+D) + 1.8·H·D and k = 5.5. The seven installation formulas are the IEC 890 table in Rittal's enclosure and process cooling guide.",
    },
    {
        "id": "rittal-guide",
        "title": "Rittal enclosure and process cooling user guide (area formulas and air-factor table)",
        "url": "https://www.rittal.com/imf/none/3_8419/3184840_Technical_system_catalogue_EN",
        "use": "Effective surface by installation type, and f = 3.1 … 3.5 m³·K/Wh with altitude.",
    },
    {
        "id": "k-materials",
        "title": "EVG — heating and cooling capacity (VDE 0660 part 500 / IEC 890 surface, material k)",
        "url": "https://www.evg.de/en/knowledge/-empty1",
        "use": "Painted sheet steel k ≈ 5.5, stainless ≈ 4.5, aluminium ≈ 12, aluminium double wall ≈ 4.5, polyester ≈ 3.5 W/m²K. Same installation formulas.",
    },
    {
        "id": "pfannenberg-cfm",
        "title": "Pfannenberg Filterfan catalog — required airflow",
        "url": "https://www.pfannenbergusa.com/wp-content/uploads/2019/01/Catalog_Final_012119.pdf",
        "use": "V [CFM] = 1.82 · PD[W] / ΔT[°C], which matches f = 3.1 m³/h per W/K converted to CFM.",
    },
    {
        "id": "rittal-filterfan",
        "title": "Rittal TopTherm filter fan — Type 12 / IP 54 as standard, Type 4/4X only with a hose-proof hood",
        "url": "https://www.rittal.com/com-en/products/PG20231215KLI101/PG20231215KLI102/PG20240718KLI001/PRO0299?variantId=3238100",
        "use": "IP 54 and NEMA Type 12 with the chopped-fibre or pleated filter. NEMA Type 3, 3R, 4, and 4X, and IP 56, only with that filter plus a hose-proof hood.",
    },
    {
        "id": "pfannenberg-type12",
        "title": "Pfannenberg PF 11000 Filterfan — IP 54 and NEMA Type 12",
        "url": "https://www.pfannenbergusa.com/product/thermal-management/filterfans/pf-series-filterfans/pf-11000/",
        "use": "System of protection IP 54 (EN 60529) and NEMA Type 12 to UL 50 with the standard filter.",
    },
    {
        "id": "hoffman-hf",
        "title": "nVent Hoffman HF side-mount filter fans",
        "url": "https://www.nvent.com/en-us/hoffman/products/hf-side-mount-filter-fans-0",
        "use": "Type 12 standard on all models. IP54 is a standard option on the models that list it (not HF13).",
    },
    {
        "id": "hoffman-shroud",
        "title": "nVent Hoffman Type 4/4X filter-fan shrouds",
        "url": "https://www.nvent.com/en-us/hoffman/products/filter-fan-shrouds-type-44x-0",
        "use": "Type 4/4X only when used with an HF or TFP Type 12 filter fan. Hoffman calls the shroud general ventilating use and recommends closed-loop cooling against condensation and corrosive atmospheres.",
    },
    {
        "id": "ul-50e",
        "title": "UL Solutions — environmental-rated enclosure accessories (UL 50 and UL 50E)",
        "url": "https://www.ul.com/services/environmental-rated-accessories-enclosures",
        "use": "Filter-fan kits, air conditioners, and heat exchangers are evaluated for the ability to maintain the Type or IP rating of the enclosure they are mounted on.",
    },
    {
        "id": "gs20-loss",
        "title": "AutomationDirect DURApulse GS20 airflow and power dissipation",
        "url": "https://cdn.automationdirect.com/static/specs/gs20drives.pdf",
        "use": "GS21-20P5 total dissipation 30.8 W (16.3 W heatsink + 14.5 W internal). IP20 ambient −20 to +50 °C without derating.",
    },
    {
        "id": "click-cpu",
        "title": "AutomationDirect CLICK PLUS C2-03CPU specifications",
        "url": "https://cdn.automationdirect.com/static/specs/c20xcpu.pdf",
        "use": "Power budget 130 mA at 24 VDC (3.12 W). Operating temperature 0–55 °C. The 20 W maximum includes attached modules and is not the CPU alone.",
    },
    {
        "id": "click-io",
        "title": "AutomationDirect CLICK C0-16ND3 specifications and family power budget",
        "url": "https://cdn.automationdirect.com/static/specs/c016nd3.pdf",
        "use": "C0-16ND3 bus power 40 mA (0.96 W) and 0–55 °C. C0-08TD1 logic 50 mA + field 15 mA (1.56 W) from the same published power-budget table.",
    },
    {
        "id": "1606-xle",
        "title": "Rockwell Automation 1606-XLE120E product page",
        "url": "https://www.rockwellautomation.com/en-us/products/details.1606-XLE120E.html",
        "use": "Typical power losses 7.3 W, already stored as library heat_w and watt_loss.",
    },
)

_NUM_RE = re.compile(r"([0-9]+(?:[.][0-9]+)?)")
_URL_RE = re.compile(r"^https?://", re.I)
_PLC_OR_DRIVE = ("control.plc", "drive.", "control.hmi")


@dataclass
class ThermalSettings:
    ambient_c: float = 40.0
    max_internal_c: float = 50.0
    min_internal_c: float | None = None
    min_ambient_c: float | None = None
    component_max_c: float = 40.0
    mount: str = ""
    material: str = ""
    nema: str = ""
    outdoor: bool = False
    altitude_m: float = 0.0
    vfd_efficiency: float = 0.97
    psu_efficiency: float = 0.88
    transformer_efficiency: float = 0.95
    estimates: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ambient_c": self.ambient_c,
            "max_internal_c": self.max_internal_c,
            "min_internal_c": self.min_internal_c,
            "min_ambient_c": self.min_ambient_c,
            "component_max_c": self.component_max_c,
            "mount": self.mount,
            "material": self.material,
            "nema": self.nema,
            "outdoor": self.outdoor,
            "altitude_m": self.altitude_m,
            "vfd_efficiency": self.vfd_efficiency,
            "psu_efficiency": self.psu_efficiency,
            "transformer_efficiency": self.transformer_efficiency,
            "estimates": dict(self.estimates),
        }


def citation_markdown() -> str:
    """Method, constants, and the maker watt-loss rows this library stores."""

    lines = [
        "# Enclosure thermal method and citations",
        "",
        "PanelCAD sizes the temperature rise from the enclosure that is already",
        "on the panel sheet. It does not change how the box is chosen.",
        "",
        "## Temperature rise and cooling",
        "",
        "Effective surface `A` uses the IEC 60890 (IEC 890) installation formulas",
        "published by Rittal. Natural rise is `ΔT = Qv / (k · A)`.",
        "Extra cooling is `Qe = Qv − k · A · ΔT`.",
        "Filter-fan airflow is Rittal's approximate `V = f · Qv / ΔT`",
        "(installed loss, not only the excess over surface dissipation).",
        "`f = 3.1 m³·K/Wh` from 0 to 100 m above sea level.",
        "CFM is that volume times 0.5886 (1 m³/h).",
        "Pfannenberg's `V [CFM] = 1.82 · W / ΔT[°C]` is the same sea-level factor.",
        "",
        "When ambient is at or above the allowed internal temperature, a fan",
        "cannot hold the limit. The report then gives air-conditioner capacity",
        "in watts and BTU/h (`1 W = 3.412 BTU/h`). An air-to-air heat exchanger",
        "also cannot cool below ambient; its specific capacity `Qe / ΔT` is",
        "reported only while a positive temperature difference remains.",
        "",
        "A heater is `QH = k · A · ΔT` for an unpowered enclosure, doubled when",
        "the outdoor flag is set (Rittal). It is calculated only if a minimum",
        "internal temperature is set.",
        "",
        "## Enclosure rating and the cooler",
        "",
        "When the natural rise does not fit the allowance, the enclosure type",
        "chooses the cooler:",
        "",
        "- NEMA 4, 4X, 6, 6P, and IP65 or tighter: closed-loop first",
        "  (heat exchanger in W/K, or an air conditioner in W and BTU/h).",
        "- NEMA 12 and IP54: a filter fan whose fan and filter are rated",
        "  Type 12 / IP54.",
        "- Other types, such as NEMA 1 or 3R: a filter fan, without calling it Type 12.",
        "",
        "If the uncontrolled rise already fits, the report recommends neither",
        "a fan nor a cooler.",
        "",
        "A filter-fan cutout keeps the enclosure rating only when the fan and",
        "filter themselves carry that rating. Cited standard fans are Type 12 /",
        "IP54 (Rittal TopTherm, Pfannenberg PF 11000, nVent Hoffman HF).",
        "Rittal reaches Type 3/3R/4/4X only with a hose-proof hood. Hoffman's",
        "Type 4/4X shroud still needs a Type 12 fan and is described as general",
        "ventilating use; Hoffman recommends closed-loop cooling where",
        "condensation or corrosion is the problem. No cited filter fan is",
        "Type 6, 6P, or IP65 or tighter. UL 50 and UL 50E evaluate the",
        "accessory for keeping the enclosure Type or IP rating.",
        "",
        "Heat-transfer coefficient `k`:",
        "",
        "| Material | k (W/m²K) | Source |",
        "| --- | --- | --- |",
        "| Painted sheet steel | 5.5 | Rittal Technical System Catalog |",
        "| Stainless steel | 4.5 | EVG, VDE 0660 part 500 / IEC 890 table |",
        "| Aluminium | 12 | EVG |",
        "| Aluminium, double wall | 4.5 | EVG |",
        "| Plastic / polyester | 3.5 | EVG |",
        "",
        "## Sources",
        "",
    ]
    for item in CITATIONS:
        lines.append(f"- **{item['title']}**")
        lines.append(f"  {item['url']}")
        lines.append(f"  {item['use']}")
        lines.append("")
    lines.extend(
        [
            "## Maker watt loss stored on library parts",
            "",
            "These are the maker's own figures. Anything else in a thermal report",
            "is labeled ESTIMATE.",
            "",
            "| Part | watt_loss | max ambient | Citation |",
            "| --- | --- | --- | --- |",
            "| GS21-20P5 | 30.8 W total | 50 °C IP20, no derating | GS20 spec PDF |",
            "| C2-03CPU | 3.12 W (130 mA × 24 V) | 55 °C | CLICK PLUS spec |",
            "| C0-16ND3 | 0.96 W (40 mA × 24 V) | 55 °C | CLICK spec power budget |",
            "| C0-08TD1 | 1.56 W ((50+15) mA × 24 V) | 55 °C | CLICK family power budget |",
            "| 1606-XLE120E | 7.3 W typical | (not on the watt-loss citation) | Rockwell product page |",
            "",
            "Existing `heat_w` values on other catalog parts (contactor dissipation,",
            "breaker power loss per pole, and so on) stay the published library",
            "figure. The thermal report calls those Published, with the part's",
            "source URL when the row has one.",
            "",
            "Parts with neither `watt_loss` nor `heat_w` use a user-editable shop",
            "estimate (category default, or an efficiency assumption of 97% for a",
            "drive, 88% for a power supply, and 95% for a transformer when output",
            "power is known). Those efficiencies are assumptions, not maker tables.",
            "",
        ]
    )
    return "\n".join(lines)


def air_factor(altitude_m: float) -> float:
    """Rittal volumetric factor f in m³·K/Wh."""

    height = max(0.0, float(altitude_m))
    for limit, factor in _AIR_BANDS_M3K_PER_WH:
        if height <= limit + 1e-9:
            return factor
    return _AIR_BANDS_M3K_PER_WH[-1][1]


def material_k(name: str) -> tuple[float, str, str]:
    token = (name or "steel").strip().lower().replace("_", " ").replace("-", " ")
    if "double" in token and "alum" in token:
        return 4.5, "aluminum_double", "Aluminium, double-walled"
    if "stainless" in token or token in {"ss", "ss304", "ss316"}:
        return 4.5, "stainless", "Stainless steel"
    if "alum" in token:
        return 12.0, "aluminum", "Aluminium"
    if any(word in token for word in ("plastic", "poly", "fiberglass", "nonmetal", "frp")):
        return 3.5, "plastic", "Plastic / polyester"
    return 5.5, "painted_steel", "Painted sheet steel"


def normalize_mount(value: str) -> str:
    token = "".join(ch.lower() for ch in str(value or "") if ch.isalnum())
    table = {
        "": "wall",
        "any": "wall",
        "wall": "wall",
        "wallmount": "wall",
        "wallmounted": "wall",
        "floor": "free",
        "free": "free",
        "freestanding": "free",
        "standalone": "free",
        "rowendfree": "row_end_free",
        "endfree": "row_end_free",
        "rowendwall": "row_end_wall",
        "rowend": "row_end_wall",
        "endwall": "row_end_wall",
        "rowmidfree": "row_mid_free",
        "midfree": "row_mid_free",
        "rowmidwallcovered": "row_mid_wall_covered",
        "covered": "row_mid_wall_covered",
        "rowmidwall": "row_mid_wall",
        "rowmid": "row_mid_wall",
        "middle": "row_mid_wall",
    }
    return table.get(token, "wall")


def normalize_nema(value: str) -> str:
    token = str(value or "").upper().replace("NEMA", "").replace("TYPE", "")
    token = token.replace(" ", "").replace("-", "").replace("_", "")
    if token in {"4X", "6P"}:
        return token
    if token in {"1", "3", "3R", "4", "6", "12", "13"}:
        return token
    return token


# Sealed types get closed-loop cooling first. Filter-rated types may use a
# filter fan only when the fan and filter carry the same rating.
_CLOSED_LOOP_TYPES = frozenset({"4", "4X", "6", "6P", "IP65", "IP66", "IP67", "IP68", "IP69", "IP69K"})
_FILTER_RATED_TYPES = frozenset({"12", "IP54"})


def protection_class(value: str) -> str:
    """``closed_loop``, ``filter_rated``, or ``open``.

    Closed-loop is NEMA 4/4X/6/6P and IP65 or tighter. Filter-rated is
    NEMA 12 and IP54 only. IP55 and NEMA 13 are not treated as Type 12.
    """

    token = normalize_nema(value)
    if token in _CLOSED_LOOP_TYPES:
        return "closed_loop"
    if token in _FILTER_RATED_TYPES:
        return "filter_rated"
    if token.startswith("IP"):
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits.isdigit() and int(digits) >= 65:
            return "closed_loop"
    return "open"


def rating_label(value: str) -> str:
    token = normalize_nema(value)
    if not token:
        return "this enclosure"
    if token.startswith("IP"):
        return token
    return f"NEMA {token}"


def format_margin(margin: float | None) -> str:
    """Signed margin against the displayed design temperature, e.g. ``+0.1 K``."""

    if margin is None:
        return ""
    return f"{float(margin):+.1f} K"


def effective_area_m2(width_mm: float, height_mm: float, depth_mm: float, mount: str) -> tuple[float, str, str]:
    """Return (area m², mount id, formula text). H, W, D are metres."""

    width = max(0.0, float(width_mm)) / 1000.0
    height = max(0.0, float(height_mm)) / 1000.0
    depth = max(0.0, float(depth_mm)) / 1000.0
    kind = normalize_mount(mount)
    w, h, d = width, height, depth
    areas = {
        "free": 1.8 * h * (w + d) + 1.4 * w * d,
        "wall": 1.4 * w * (h + d) + 1.8 * h * d,
        "row_end_free": 1.4 * d * (w + h) + 1.8 * w * h,
        "row_end_wall": 1.4 * h * (w + d) + 1.4 * w * d,
        "row_mid_free": 1.8 * w * h + 1.4 * w * d + h * d,
        "row_mid_wall": 1.4 * w * (h + d) + h * d,
        "row_mid_wall_covered": 1.4 * w * h + 0.7 * w * d + h * d,
    }
    return areas[kind], kind, _AREA_FORMULAS[kind]


def thermal_page_spans(n_devices: int, *, first: int = 12, rest: int = 18) -> list[tuple[int, int]]:
    if n_devices <= 0:
        return [(0, 0)]
    spans: list[tuple[int, int]] = []
    start = 0
    cap = first
    while start < n_devices:
        count = min(cap, n_devices - start)
        spans.append((start, count))
        start += count
        cap = rest
    return spans


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUM_RE.search(str(value).strip())
    if not match:
        return None
    return float(match.group(1))


def _truthy(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "y"}:
        return True
    if token in {"0", "false", "no", "n"}:
        return False
    return None


def _is_url(value: Any) -> bool:
    return bool(_URL_RE.match(str(value or "").strip()))


def settings_from(project: Project, overrides: dict[str, Any] | None = None) -> ThermalSettings:
    raw = project.properties.get(THERMAL_KEY) or {}
    data: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    for key, value in (overrides or {}).items():
        if value is None and key not in {"min_internal_c", "min_ambient_c", "mount", "material", "nema"}:
            continue
        data[key] = value
    estimates = data.get("estimates") or {}
    if not isinstance(estimates, dict):
        estimates = {}
    clean_estimates: dict[str, float] = {}
    for key, value in estimates.items():
        number = _num(value)
        if number is not None and number >= 0:
            clean_estimates[str(key)] = number
    known = {f.name for f in ThermalSettings.__dataclass_fields__.values()}
    fields = {key: value for key, value in data.items() if key in known and key != "estimates"}
    if "outdoor" in fields:
        fields["outdoor"] = bool(fields["outdoor"])
    for key in ("ambient_c", "max_internal_c", "component_max_c", "altitude_m", "vfd_efficiency", "psu_efficiency", "transformer_efficiency"):
        if key in fields and fields[key] is not None:
            fields[key] = float(fields[key])
    for key in ("min_internal_c", "min_ambient_c"):
        if key in fields:
            fields[key] = None if fields[key] in (None, "") else float(fields[key])
    for key in ("mount", "material", "nema"):
        if key in fields and fields[key] is not None:
            fields[key] = str(fields[key])
    return ThermalSettings(estimates=clean_estimates, **fields)


def chosen_enclosure(project: Project) -> tuple[Sheet | None, Enclosure | None]:
    """The sized enclosure. A page-outline placeholder is not a size."""

    from panelcad.core.layout import enclosure_is_sized

    for sheet in project.sheets:
        if sheet.kind == "panel_layout" and enclosure_is_sized(sheet.enclosure, sheet.page):
            return sheet, sheet.enclosure
    return None, None


def _counts(device: Device, comp: Component) -> bool:
    category = device.category or ""
    library_id = device.library_id or ""
    if category.startswith(("connection.notes", "connection.offpage", "connection.node")):
        return False
    if "offpage" in library_id or category == "connection.jumper":
        return False
    if is_jumper_device(device):
        return False
    for key in ("incabinet", "in_panel"):
        if key in (comp.properties or {}):
            flag = _truthy(comp.properties.get(key))
            if flag is False:
                return False
            if flag is True:
                return True
    if category in NOT_IN_PANEL_CATEGORIES or category.startswith("load."):
        return False
    footprint = effective_footprint(device, comp)
    if footprint.mount == "door":
        return True
    if footprint.mount in {"field", "none"} and not footprint.in_panel:
        return False
    return bool(footprint.in_panel)


def _catalog_part(parts: PartsCatalog | None, props: dict[str, Any]) -> CatalogPart | None:
    if parts is None:
        return None
    catalog = str(props.get("catalog") or props.get("part_number") or "").strip()
    if not catalog:
        return None
    manufacturer = str(props.get("manufacturer") or "").strip()
    try:
        return parts.get(catalog, manufacturer or None)
    except KeyError:
        return None


def _output_kw(props: dict[str, Any]) -> float | None:
    for key in ("kw", "kW", "power_kw", "Pn", "pn"):
        raw = props.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        text = str(raw)
        if re.search(r"hp|horse", text, re.I):
            horse = _num(raw)
            return None if horse is None else horse * HP_TO_KW
        if re.search(r"\bW\b", text) and not re.search(r"kW", text, re.I):
            watts = _num(raw)
            return None if watts is None else watts / 1000.0
        number = _num(raw)
        if number is not None:
            return number
    for key in ("hp", "HP", "horsepower"):
        horse = _num(props.get(key))
        if horse is not None:
            return horse * HP_TO_KW
    return None


def _category_estimate(category: str, settings: ThermalSettings) -> tuple[float, str]:
    specific = settings.estimates.get(f"category:{category}")
    if specific is not None:
        return specific, f"category:{category}"
    if category in DEFAULT_CATEGORY_ESTIMATES_W:
        return DEFAULT_CATEGORY_ESTIMATES_W[category], category
    family = category.split(".", 1)[0]
    for key, watts in DEFAULT_CATEGORY_ESTIMATES_W.items():
        if key == family or key.startswith(family + "."):
            return watts, key
    return 0.0, category or "other"


def _published_watts(
    props: dict[str, Any],
    part: CatalogPart | None,
    device: Device,
) -> tuple[float, str, str] | None:
    """(watts, url, note) from maker watt_loss or library heat_w."""

    part_loss = None if part is None else part.watt_loss
    part_url = "" if part is None else (part.watt_loss_source or part.source)
    device_loss = _num(device.properties.get("watt_loss"))
    device_url = str(device.properties.get("watt_loss_source") or device.properties.get("source") or "")
    comp_loss = _num(props.get("watt_loss")) if "watt_loss" in props and "watt_loss" not in device.properties else None
    # Component watt_loss counts as maker data only with a URL. Otherwise it is
    # handled as a user estimate by the caller via watt_estimate.
    comp_url = str(props.get("watt_loss_source") or "")
    if "watt_loss" in (props or {}) and _is_url(comp_url):
        watts = _num(props.get("watt_loss"))
        if watts is not None:
            note = str(props.get("watt_loss_note") or "Published watt loss on this device.")
            return watts, comp_url, note
    if part_loss is not None and _is_url(part_url):
        note = str(part.notes or "Published watt_loss on the catalog part.")
        return float(part_loss), part_url, note
    if device_loss is not None and _is_url(device_url):
        note = str(device.properties.get("watt_loss_note") or "Published watt loss on the library symbol.")
        return device_loss, device_url, note

    for source, url in (
        (_num(props.get("heat_w")), str(props.get("catalog_source") or props.get("source") or "")),
        (None if part is None else part.heat_w, "" if part is None else part.source),
        (_num(device.properties.get("heat_w")), str(device.properties.get("source") or "")),
    ):
        if source is None:
            continue
        cite = url if _is_url(url) else ""
        return float(source), cite, "Library heat_w (published dissipation)."
    return None


def _efficiency_estimate(props: dict[str, Any], category: str, settings: ThermalSettings) -> tuple[float, str] | None:
    cat = category.lower()
    library_id = str(props.get("library_id") or "")
    if cat.startswith("drive") or "vfd" in library_id:
        kw = _output_kw(props)
        if kw is None:
            return None
        eff = min(0.99, max(0.5, settings.vfd_efficiency))
        watts = kw * 1000.0 * (1.0 / eff - 1.0)
        return watts, (
            f"ESTIMATE from {kw:g} kW at {eff:.0%} efficiency "
            f"({watts:.2f} W). Not a maker watt-loss table."
        )
    if cat.startswith("power.supply"):
        volts = _num(props.get("Vout") or props.get("V"))
        amps = _num(props.get("Iout") or props.get("current"))
        if volts is None or amps is None:
            kw = _output_kw(props)
            if kw is None:
                return None
            output = kw * 1000.0
        else:
            output = volts * amps
        eff = min(0.99, max(0.5, settings.psu_efficiency))
        watts = output * (1.0 / eff - 1.0)
        return watts, (
            f"ESTIMATE from {output:g} W output at {eff:.0%} efficiency "
            f"({watts:.2f} W). Not a maker efficiency or watt-loss figure."
        )
    if cat.startswith("power.transformer"):
        va = _num(props.get("VA") or props.get("va") or props.get("rating_va"))
        if va is None:
            return None
        eff = min(0.99, max(0.5, settings.transformer_efficiency))
        watts = va * (1.0 / eff - 1.0)
        return watts, (
            f"ESTIMATE from {va:g} VA at {eff:.0%} efficiency "
            f"({watts:.2f} W). Not a maker loss figure."
        )
    return None


def _wants_ambient_check(category: str, published_limit: float | None) -> bool:
    if published_limit is not None:
        return True
    cat = category.lower()
    return cat.startswith(_PLC_OR_DRIVE) or "vfd" in cat


def part_rows(
    project: Project,
    catalog: Catalog,
    parts: PartsCatalog | None,
    settings: ThermalSettings,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for sheet in project.sheets:
        for comp in sheet.components:
            try:
                device = catalog.get(comp.library_id)
            except KeyError:
                continue
            if not _counts(device, comp):
                continue
            slot = merged.get(comp.id)
            props = dict(comp.properties or {})
            if slot is None:
                merged[comp.id] = {
                    "sheet": sheet,
                    "device": device,
                    "comp": comp,
                    "props": {**device.properties, **props},
                }
                continue
            slot["props"].update(props)
            if sheet.kind == "panel_layout" and slot["sheet"].kind != "panel_layout":
                slot["sheet"] = sheet
                slot["comp"] = comp
                slot["device"] = device
    rows: list[dict[str, Any]] = []
    for ref in sorted(merged):
        slot = merged[ref]
        device: Device = slot["device"]
        comp: Component = slot["comp"]
        props: dict[str, Any] = slot["props"]
        props["library_id"] = device.library_id
        part = _catalog_part(parts, props)
        category = device.category or ""
        published = _published_watts(props, part, device)
        library_watts = None if published is None else round(published[0], 4)
        user_estimate = _num(comp.properties.get("watt_estimate"))
        if user_estimate is None:
            user_estimate = _num(props.get("watt_estimate"))
        ref_estimate = settings.estimates.get(ref)
        if user_estimate is not None:
            watts = user_estimate
            basis = "estimate"
            citation = ""
            note = "User estimate on this device. Not manufacturer data."
        elif ref_estimate is not None and published is None:
            watts = ref_estimate
            basis = "estimate"
            citation = ""
            note = "User estimate stored on the thermal settings. Not manufacturer data."
        elif published is not None:
            watts, citation, note = published
            basis = "published"
        else:
            guessed = _efficiency_estimate(props, category, settings)
            if guessed is not None:
                watts, note = guessed
                basis = "estimate"
                citation = ""
            else:
                watts, key = _category_estimate(category, settings)
                basis = "estimate"
                citation = ""
                note = (
                    f"Shop estimate for {key} ({watts:g} W). "
                    "Not manufacturer data. Edit the watts to replace it."
                )
        limit, limit_basis, limit_url = _ambient_limit(props, part, device, category, settings)
        catalog_no = str(props.get("catalog") or props.get("part_number") or "")
        rows.append(
            {
                "ref": ref,
                "sheet": slot["sheet"].id,
                "library_id": device.library_id,
                "description": str(props.get("description") or device.name),
                "catalog": catalog_no,
                "manufacturer": str(props.get("manufacturer") or ""),
                "category": category,
                "watts": round(float(watts), 2),
                "basis": basis,
                "source_label": "Published" if basis == "published" else "ESTIMATE",
                "citation": citation,
                "note": note,
                "library_watts": None if library_watts is None else round(library_watts, 2),
                "max_ambient_c": limit,
                "max_ambient_basis": limit_basis,
                "max_ambient_url": limit_url,
                "x": comp.x,
                "y": comp.y,
            }
        )
    return rows


def _ambient_limit(
    props: dict[str, Any],
    part: CatalogPart | None,
    device: Device,
    category: str,
    settings: ThermalSettings,
) -> tuple[float | None, str, str]:
    candidates: list[tuple[float, str, str]] = []
    for value, url in (
        (_num(props.get("max_ambient_c")), str(props.get("watt_loss_source") or props.get("source") or "")),
        (None if part is None else part.max_ambient_c, "" if part is None else part.source),
        (_num(device.properties.get("max_ambient_c")), str(device.properties.get("watt_loss_source") or device.properties.get("source") or "")),
    ):
        if value is None:
            continue
        candidates.append((float(value), url if _is_url(url) else "", "published"))
    if candidates:
        watts, url, basis = candidates[0]
        return watts, basis, url
    if _wants_ambient_check(category, None):
        return settings.component_max_c, "user", ""
    return None, "", ""


def _finding(check: str, message: str, *, sheet: str, ref: str, x: float | None = None, y: float | None = None) -> dict[str, Any]:
    return {
        "check": check,
        "severity": "warning",
        "message": message,
        "sheet": sheet,
        "ref": ref,
        "x": x,
        "y": y,
    }


def _part_label(row: dict[str, Any]) -> str:
    catalog = str(row.get("catalog") or "").strip()
    if catalog:
        return catalog
    return str(row.get("ref") or "")


def cooling_target(rows: list[dict[str, Any]], user_max_c: float) -> dict[str, Any]:
    """Lower of the panel max and every part that has a max ambient.

    A published rating counts, and so does the user setting on an unrated
    PLC, drive, or HMI. The part is named only when it is stricter than the
    panel setting, so natural convection is not "enough" while a margin is
    negative.
    """

    target = float(user_max_c)
    holders: list[dict[str, Any]] = []
    for row in rows:
        limit = row.get("max_ambient_c")
        if limit is None:
            continue
        limit_c = float(limit)
        if limit_c < target - 0.05:
            target = limit_c
            holders = [row]
        elif holders and abs(limit_c - target) <= 0.05:
            holders.append(row)
    if not holders:
        return {"celsius": float(user_max_c), "parts": [], "governed_by": "user"}
    return {"celsius": target, "parts": holders, "governed_by": "part"}


def _join_names(names: list[str]) -> str:
    items = [name for name in names if name]
    if not items:
        return "the part"
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _allowance_set_by(target: dict[str, Any]) -> str:
    """Who set the real rise allowance, e.g. ``A1 (40 °C user rating)``."""

    parts = list(target.get("parts") or [])
    temp = float(target["celsius"])
    published = [row for row in parts if row.get("max_ambient_basis") == "published"]
    users = [row for row in parts if row.get("max_ambient_basis") != "published"]
    if published and not users:
        names = _join_names([_part_label(row) for row in published])
        return f"{names} ({temp:g} °C rating)"
    if users and not published:
        names = _join_names([str(row.get("ref") or "") for row in users])
        return f"{names} ({temp:g} °C user rating)"
    bits: list[str] = []
    if published:
        names = _join_names([_part_label(row) for row in published])
        bits.append(f"{names} ({temp:g} °C rating)")
    if users:
        names = _join_names([str(row.get("ref") or "") for row in users])
        bits.append(f"{names} ({temp:g} °C user rating)")
    return " and ".join(bits)


def _tightest_margin(rows: list[dict[str, Any]], internal: float) -> float | None:
    """Smallest part margin against the 1-decimal design temperature."""

    shown = round(round(float(internal), 2), 1)
    margins = [
        round(float(row["max_ambient_c"]) - shown, 1)
        for row in rows
        if row.get("max_ambient_c") is not None
    ]
    if not margins:
        return None
    return min(margins)


def _cooling_watts(excess: float) -> tuple[float, float]:
    """Watts and BTU/h at the same rounding the design check stores."""

    watts = round(max(0.0, float(excess)), 1)
    btu = round(max(0.0, float(excess)) * BTU_H_PER_W, 0)
    return watts, btu


def _governor_clause(target: dict[str, Any]) -> str:
    parts = list(target.get("parts") or [])
    if not parts:
        return ""
    temp = float(target["celsius"])
    published = [row for row in parts if row.get("max_ambient_basis") == "published"]
    users = [row for row in parts if row.get("max_ambient_basis") != "published"]
    if published:
        names = ", ".join(_part_label(row) for row in published)
        verb = "limits" if len(published) == 1 else "limit"
        return f"{names} rated {temp:g} °C {verb} the panel"
    names = ", ".join(str(row.get("ref") or "") for row in users)
    return f"{names} user max ambient {temp:g} °C limits the panel"


def _apply_margins(rows: list[dict[str, Any]], design_c: float | None) -> None:
    """Margin of each part's max ambient against the 1-decimal design temperature."""

    shown = None if design_c is None else round(float(design_c), 1)
    for row in rows:
        limit = row.get("max_ambient_c")
        if limit is None or shown is None:
            row["margin_k"] = None
        else:
            row["margin_k"] = round(float(limit) - shown, 1)


def _design_c(internal: float | None, target_c: float, kind: str) -> float | None:
    """Temperature the parts see if the cooling recommendation is followed."""

    if internal is None:
        return None
    if kind == "none":
        return round(internal, 2)
    return round(min(internal, target_c), 2)


def compute_heat_load(
    project: Project,
    catalog: Catalog,
    parts: PartsCatalog | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Thermal report for the enclosure already on the job. Does not resize it."""

    settings = settings_from(project, overrides)
    sheet, enclosure = chosen_enclosure(project)
    rows = part_rows(project, catalog, parts, settings)
    published = round(sum(row["watts"] for row in rows if row["basis"] == "published"), 2)
    estimated = round(sum(row["watts"] for row in rows if row["basis"] == "estimate"), 2)
    total = round(published + estimated, 2)
    estimate_refs = [row["ref"] for row in rows if row["basis"] == "estimate"]

    mount_token = settings.mount or (enclosure.mount if enclosure is not None else "") or "wall"
    material_token = settings.material or (enclosure.material if enclosure is not None else "") or "steel"
    nema = normalize_nema(settings.nema or (enclosure.nema_type if enclosure is not None else "") or "12")
    k, k_id, k_label = material_k(material_token)
    factor = air_factor(settings.altitude_m)

    if enclosure is None:
        area, mount_id, formula = 0.0, normalize_mount(mount_token), ""
        width = height = depth = 0.0
        name = ""
    else:
        area, mount_id, formula = effective_area_m2(
            enclosure.width_mm,
            enclosure.height_mm,
            enclosure.depth_mm,
            mount_token,
        )
        width, height, depth = enclosure.width_mm, enclosure.height_mm, enclosure.depth_mm
        name = enclosure.name

    user_allowed = settings.max_internal_c - settings.ambient_c
    target = cooling_target(rows, settings.max_internal_c)
    cap = float(target["celsius"])
    allowed = cap - settings.ambient_c
    surface = k * area
    rise = None if total <= 0 or surface <= 0 else total / surface
    internal = None if rise is None else settings.ambient_c + rise
    excess = None if surface <= 0 else total - surface * allowed

    recommendation = _recommend(
        total=total,
        allowed=allowed,
        user_allowed=user_allowed,
        excess=excess,
        rise=rise,
        internal=internal,
        factor=factor,
        nema=nema,
        surface=surface,
        settings=settings,
        area=area,
        k=k,
        governor=target,
        rows=rows,
    )
    design = _design_c(internal, cap, str(recommendation["kind"]))
    _apply_margins(rows, design)
    findings = _build_findings(
        rows,
        sheet,
        enclosure,
        design_c=design,
        kind=str(recommendation["kind"]),
        nema=nema,
        recommendation=recommendation,
    )
    assumptions = _assumptions(
        settings,
        mount_id=mount_id,
        formula=formula,
        k=k,
        k_label=k_label,
        factor=factor,
        area=area,
        published=published,
        estimated=estimated,
        enclosure_missing=enclosure is None,
    )
    return {
        "method": "IEC 60890 / Rittal effective surface",
        "formula": "ΔT = Qv / (k · A)",
        "area_formula": formula,
        "citations": [dict(item) for item in CITATIONS],
        "needs_info": enclosure is None,
        "enclosure": None
        if enclosure is None
        else {
            "sheet": sheet.id if sheet else "",
            "name": name,
            "width_mm": width,
            "height_mm": height,
            "depth_mm": depth,
            "mount": mount_id,
            "material": k_id,
            "material_label": k_label,
            "nema": nema,
            "protection": protection_class(nema),
        },
        "settings": settings.to_dict(),
        "surface_m2": round(area, 4),
        "k_w_m2k": k,
        "k_label": k_label,
        "air_factor_m3k_per_wh": factor,
        "altitude_m": settings.altitude_m,
        "ambient_c": settings.ambient_c,
        "max_internal_c": settings.max_internal_c,
        "min_internal_c": settings.min_internal_c,
        "allowed_rise_k": round(user_allowed, 2),
        "cooling_limit_c": round(cap, 2),
        "cooling_limit_refs": [str(row.get("ref") or "") for row in target["parts"]],
        "cooling_governed_by": target["governed_by"],
        "watts_published": published,
        "watts_estimate": estimated,
        "watts_total": total,
        "estimate_refs": estimate_refs,
        "estimate_count": len(estimate_refs),
        "rise_k": None if rise is None else round(rise, 2),
        "internal_c": None if internal is None else round(internal, 2),
        "design_c": design,
        "protection": protection_class(nema),
        "devices": rows,
        "recommendation": recommendation,
        "findings": findings,
        "assumptions": assumptions,
        "ok": enclosure is not None and (rise is None or rise <= (cap - settings.ambient_c) + 1e-6),
    }


def _recommend(
    *,
    total: float,
    allowed: float,
    user_allowed: float,
    excess: float | None,
    rise: float | None,
    internal: float | None,
    factor: float,
    nema: str,
    surface: float,
    settings: ThermalSettings,
    area: float,
    k: float,
    governor: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    heater = _heater_w(settings, area, k)
    base: dict[str, Any] = {
        "kind": "none",
        "summary": "No in-cabinet watt loss to remove.",
        "airflow_m3h": None,
        "airflow_cfm": None,
        "cooling_w": None,
        "cooling_btu_h": None,
        "heat_exchanger_w_per_k": None,
        "heater_w": heater,
        "nema4_fan": False,
        "seal_note": "",
    }
    if total <= 0 or surface <= 0 or excess is None or rise is None:
        if surface <= 0 and total > 0:
            base["summary"] = (
                "Needs info: no enclosure size is set, so rise and cooling are not calculated. "
                "A page outline is not a box size."
            )
        elif heater:
            base["summary"] = f"No heat load. Heater {heater:g} W to hold the minimum temperature with the enclosure unpowered."
        return base

    klass = protection_class(nema)
    label = rating_label(nema)
    clause = _governor_clause(governor)

    def finish(text: str) -> str:
        if heater:
            return text + f" Heater {heater:g} W for the minimum temperature (enclosure unpowered)."
        return text

    def lead(text: str) -> str:
        text = finish(text)
        if not clause or not text:
            return text
        return f"Cooling needed: {clause}; {text[0].lower()}{text[1:]}"

    if allowed <= 1e-9:
        cooling, btu = _cooling_watts(excess)
        if klass == "closed_loop":
            summary = (
                f"Closed-loop air conditioner {cooling:g} W ({btu:g} BTU/h) for {label}. "
                f"Ambient {settings.ambient_c:g} °C is at or above the {governor['celsius']:g} °C limit, "
                "so a filter fan or an air-to-air heat exchanger cannot hold it."
            )
            seal = f"{label} stays closed with an air conditioner. A filter fan would open the enclosure."
        else:
            summary = (
                f"Ambient {settings.ambient_c:g} °C is at or above the {governor['celsius']:g} °C internal limit, "
                f"so a filter fan cannot hold it. Air conditioner {cooling:g} W ({btu:g} BTU/h). "
                "An air-to-air heat exchanger also cannot cool below the outside air."
            )
            seal = ""
        base.update(
            {
                "kind": "air_conditioner",
                "summary": lead(summary),
                "cooling_w": cooling,
                "cooling_btu_h": btu,
                "nema4_fan": klass == "closed_loop",
                "seal_note": seal,
            }
        )
        return base

    if excess <= 1e-6:
        if governor.get("governed_by") == "part" and governor.get("parts"):
            margin = None if internal is None else _tightest_margin(list(governor["parts"]), internal)
            margin_bit = "" if margin is None else f"; margin {format_margin(margin)}"
            summary = (
                f"Natural convection is enough: rise {rise:.1f} K within the {allowed:.1f} K allowance "
                f"set by {_allowance_set_by(governor)}{margin_bit}."
            )
        else:
            summary = (
                f"Natural convection is enough. Rise {rise:.1f} K stays inside the {user_allowed:.1f} K allowance "
                f"({settings.ambient_c:g} °C ambient, {settings.max_internal_c:g} °C limit)."
            )
        text = finish(summary)
        tightest = None if internal is None else _tightest_margin(rows, internal)
        if tightest is not None and tightest < HEADROOM_CAUTION_K:
            text += (
                " The design is at the limit; a small filter fan or a larger enclosure gives headroom."
            )
        base["summary"] = text
        return base

    airflow = factor * total / allowed
    cfm = airflow * CFM_PER_M3H
    hx = excess / allowed
    cooling, btu = _cooling_watts(excess)
    if klass == "closed_loop":
        summary = (
            f"Closed-loop first for {label}: heat exchanger {hx:.2f} W/K, "
            f"or air conditioner {cooling:g} W ({btu:g} BTU/h), to hold {allowed:.1f} K. "
            "A filter-fan opening keeps this rating only if the fan and filter are rated for it. "
            "Standard filter fans are Type 12 / IP54."
        )
        base.update(
            {
                "kind": "closed_loop",
                "summary": lead(summary),
                "cooling_w": cooling,
                "cooling_btu_h": btu,
                "heat_exchanger_w_per_k": round(hx, 2),
                "nema4_fan": True,
                "seal_note": (
                    f"{label}: use a closed-loop heat exchanger or air conditioner. "
                    "An ordinary filter fan does not keep this rating."
                ),
            }
        )
        return base

    if klass == "filter_rated":
        summary = (
            f"Filter fan {airflow:.1f} m³/h ({cfm:.1f} CFM). "
            f"The fan and filter must be rated Type 12 / IP54, matching this enclosure, to hold {allowed:.1f} K. "
            f"Closed-loop alternative {cooling:g} W ({btu:g} BTU/h), heat exchanger {hx:.2f} W/K."
        )
    else:
        summary = (
            f"Filter fan {airflow:.1f} m³/h ({cfm:.1f} CFM) to hold {allowed:.1f} K. "
            f"Closed-loop alternative {cooling:g} W ({btu:g} BTU/h), heat exchanger {hx:.2f} W/K."
        )
    base.update(
        {
            "kind": "filter_fan",
            "summary": lead(summary),
            "airflow_m3h": round(airflow, 2),
            "airflow_cfm": round(cfm, 2),
            "cooling_w": cooling,
            "cooling_btu_h": btu,
            "heat_exchanger_w_per_k": round(hx, 2),
        }
    )
    return base


def _heater_w(settings: ThermalSettings, area: float, k: float) -> float | None:
    if settings.min_internal_c is None or area <= 0 or k <= 0:
        return None
    cold = settings.ambient_c if settings.min_ambient_c is None else settings.min_ambient_c
    delta = settings.min_internal_c - cold
    if delta <= 0:
        return 0.0
    watts = k * area * delta
    if settings.outdoor:
        watts *= 2.0
    return round(watts, 1)


def _build_findings(
    rows: list[dict[str, Any]],
    sheet: Sheet | None,
    enclosure: Enclosure | None,
    *,
    design_c: float | None,
    kind: str,
    nema: str,
    recommendation: dict[str, Any],
) -> list[dict[str, Any]]:
    if enclosure is None or sheet is None:
        return []
    findings: list[dict[str, Any]] = []
    if design_c is not None:
        shown = round(float(design_c), 1)
        for row in rows:
            margin = row.get("margin_k")
            limit = row.get("max_ambient_c")
            if margin is None or limit is None or float(margin) >= 0:
                continue
            if row["max_ambient_basis"] == "published":
                whose = f"the published max ambient {float(limit):g} °C"
            else:
                whose = f"the user max-ambient setting {float(limit):g} °C (no library rating)"
            findings.append(
                _finding(
                    "above_max_ambient",
                    (
                        f"{row['ref']}: design internal {shown:.1f} °C is above {whose} "
                        f"(margin {float(margin):+.1f} K)"
                        + (f" ({row['catalog']})" if row["catalog"] else "")
                    ),
                    sheet=sheet.id,
                    ref=row["ref"],
                    x=row.get("x"),
                    y=row.get("y"),
                )
            )
    if protection_class(nema) == "closed_loop" and kind in {"closed_loop", "air_conditioner"}:
        cooling = recommendation.get("cooling_w")
        btu = recommendation.get("cooling_btu_h")
        hx = recommendation.get("heat_exchanger_w_per_k")
        detail: list[str] = []
        if hx is not None:
            detail.append(f"heat exchanger {float(hx):g} W/K")
        if cooling is not None:
            watts = f"air conditioner {float(cooling):g} W"
            if btu is not None:
                watts += f" / {float(btu):g} BTU/h"
            detail.append(watts)
        findings.append(
            _finding(
                "nema4_filter_fan",
                (
                    f"{enclosure.name or 'enclosure'}: {rating_label(nema)} needs closed-loop cooling"
                    + (f" ({', '.join(detail)})" if detail else "")
                    + ". An ordinary filter fan does not keep this rating. "
                    "Standard filter fans are Type 12 / IP54, and UL 50 / UL 50E require the accessory itself to maintain the enclosure type."
                ),
                sheet=sheet.id,
                ref=enclosure.name or "enclosure",
                x=enclosure.x,
                y=enclosure.y,
            )
        )
    return findings


def _assumptions(
    settings: ThermalSettings,
    *,
    mount_id: str,
    formula: str,
    k: float,
    k_label: str,
    factor: float,
    area: float,
    published: float,
    estimated: float,
    enclosure_missing: bool,
) -> list[str]:
    lines = [
        "IEC 60890 / Rittal: temperature rise ΔT = Qv / (k · A) from the effective cooling surface. The box size is an input, not a result.",
        formula or "No enclosure is on a panel sheet yet.",
        f"k = {k:g} W/m²K for {k_label}. Painted steel 5.5 (Rittal); stainless 4.5, aluminium 12, plastic 3.5 (published IEC 890 material table).",
        (
            f"Filter-fan airflow V = f · Qv / ΔT with f = {factor:g} m³·K/Wh at {settings.altitude_m:g} m. "
            "This is Rittal's installed-loss formula; it does not subtract surface dissipation. "
            f"CFM = m³/h × {CFM_PER_M3H:.4f}."
        ),
        "If ambient is at or above the internal limit, the recommendation is an air conditioner in W and BTU/h (1 W = 3.412 BTU/h). An air-to-air heat exchanger cannot cool below ambient.",
        (
            "Enclosure rating selects the cooler when the natural rise does not fit. "
            "NEMA 4, 4X, 6, 6P, and IP65 or tighter get closed-loop cooling first. "
            "NEMA 12 and IP54 get a filter fan that must itself be Type 12 / IP54 "
            "(Rittal TopTherm, Pfannenberg PF, nVent Hoffman HF). "
            "A standard filter fan is not Type 4/4X; Rittal reaches those types only with a hose-proof hood, "
            "and Hoffman describes its Type 4/4X shroud as general ventilating use. "
            "No cited filter fan is Type 6 or IP65+. UL 50 and UL 50E require the accessory to maintain the enclosure rating."
        ),
        "Heater watts are k · A · ΔT for an unpowered enclosure (Rittal), doubled when outdoor is set, and only when a minimum internal temperature is set.",
        (
            f"Published loss {published:g} W. Shop estimates {estimated:g} W are labeled ESTIMATE and are not manufacturer data. "
            f"Drive / supply / transformer estimates use {settings.vfd_efficiency:.0%} / {settings.psu_efficiency:.0%} / {settings.transformer_efficiency:.0%} when output power is known and no maker loss is on the part."
        ),
        "Motors and other field devices are omitted. Door-mounted devices are included. Each tag is counted once.",
        (
            f"Mount {mount_id}, effective area {area:.3f} m². "
            "The cooling target is the lower of the panel max internal temperature and the lowest part max ambient. "
            "Natural convection is recommended only when every part margin is zero or positive. "
            "When a part limit is below the panel max, that headline names the governing limit and the real allowance. "
            f"A natural-convection margin under {HEADROOM_CAUTION_K:g} K adds a caution that the design is at the limit. "
            f"That {HEADROOM_CAUTION_K:g} K threshold is a shop setting, not a code value. "
            f"Margin is the part's max ambient minus the design temperature shown to 0.1 °C. "
            f"A library rating is used when the part publishes one. "
            f"A PLC, drive, or HMI with no library rating uses the user setting {settings.component_max_c:g} °C. "
            "Other parts with no rating are not checked."
        ),
    ]
    if enclosure_missing:
        lines.append(
            "Needs info: no enclosure size is set, so rise, fan, and heater are not calculated. "
            "A page outline is not a box size."
        )
    return lines
