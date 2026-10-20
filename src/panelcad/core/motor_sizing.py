"""Motor branch-circuit and conductor sizing from published NEC and UL tables.

Every ampere in this module is copied from a table cell. Nothing here is
interpolated, scaled by voltage ratio, or filled in for a blank cell.

NFPA 70 table numbers are the citation for the cells that were checked
against NFPA's own 2026-cycle documents: 430.248, 430.250, 430.52, 430.22,
430.24, 310.16, and 240.6(A). No secondary site is cited.

UL 508A is not freely viewable. Every UL 508A ampacity and the 29.6
termination rule is marked unverified until an official UL or maker source
is in hand. Table 430.72(B) and the 430.32(A)(1) 115% / 125% limits are
marked the same way: this module does not cite an official NFPA text for them.

* NFPA 70 Table 430.250 (three-phase) and Table 430.248 (single-phase).
  Horsepower above 200 and the 2300 V column are not encoded.
  1-1/2 hp at 575 V is 2.4 A.
* NFPA 70 Table 430.52(C)(1) percentages. Design B premium-efficiency is not
  a separate row here; select ``design_b_energy_efficient`` for the 1100%
  instantaneous-trip row.
* NFPA 70 430.22 (125% of table full-load current), 430.24 (feeder), and
  430.6(A)(1) (table current for conductors and branch short-circuit
  protection; nameplate current for the overload).
* Table 430.72(B), copper columns. Not verified against an official NFPA
  text. Column A for 14/12/10 AWG is Note 1 (see 310.16 and 240.4(D)), not a
  number invented for the cell.
* NFPA 70 Table 310.16 copper 60°C and 75°C columns through 500 kcmil.
  14 AWG is 15 A at 60°C and 20 A at 75°C. 600 kcmil and larger are not
  encoded. The 90°C column is not used: termination rules size from 60°C
  or 75°C.
* NFPA 70 240.6(A) standard ampere ratings. Fuses add 1, 3, 6, 10, and 601.
* UL 508A Table 28.1 copper. Not verified against the standard. 18 and 16 AWG
  are not encoded on this table. 1/0 and larger have a blank 60°C cell.
  The installed gauge is the larger of NEC Table 310.16 and this table at
  the termination column.
* UL 508A 29.6, not verified against the standard. When a component has no
  termination temperature marking, the 60°C column is used at 100 A or less
  and the 75°C column above 100 A. That is the default. A marked 60°C or
  75°C termination overrides it.
* UL 508A internal control-circuit conductors, not verified against the
  standard: 18 AWG = 7 A, 16 AWG = 10 A.

IEC projects do not get an NEC full-load table. Pass kW and the nameplate or
other user current. This module will not invent IEC amperes.
"""

from __future__ import annotations

import re
from typing import Any

HP_TO_KW = 0.746  # 1 hp = 746 W. Not an NEC ampere.

# Shown wherever a UL 508A number or an unverified NEC rule reaches the user.
UL_UNVERIFIED = "UL 508A value, not verified against the standard"
NEC_UNVERIFIED = "not verified against an official NFPA text"

# Table 430.250 induction-type squirrel-cage and wound-rotor motors.
# hp, then amperes at 115, 200, 208, 230, 460, 575. None is a blank cell.
_V3 = (115, 200, 208, 230, 460, 575)
_TABLE_430_250: tuple[tuple[float, tuple[float | None, ...]], ...] = (
    (0.5, (4.4, 2.5, 2.4, 2.2, 1.1, 0.9)),
    (0.75, (6.4, 3.7, 3.5, 3.2, 1.6, 1.3)),
    (1.0, (8.4, 4.8, 4.6, 4.2, 2.1, 1.7)),
    (1.5, (12.0, 6.9, 6.6, 6.0, 3.0, 2.4)),
    (2.0, (13.6, 7.8, 7.5, 6.8, 3.4, 2.7)),
    (3.0, (None, 11.0, 10.6, 9.6, 4.8, 3.9)),
    (5.0, (None, 17.5, 16.7, 15.2, 7.6, 6.1)),
    (7.5, (None, 25.3, 24.2, 22.0, 11.0, 9.0)),
    (10.0, (None, 32.2, 30.8, 28.0, 14.0, 11.0)),
    (15.0, (None, 48.3, 46.2, 42.0, 21.0, 17.0)),
    (20.0, (None, 62.1, 59.4, 54.0, 27.0, 22.0)),
    (25.0, (None, 78.2, 74.8, 68.0, 34.0, 27.0)),
    (30.0, (None, 92.0, 88.0, 80.0, 40.0, 32.0)),
    (40.0, (None, 120.0, 114.0, 104.0, 52.0, 41.0)),
    (50.0, (None, 150.0, 143.0, 130.0, 65.0, 52.0)),
    (60.0, (None, 177.0, 169.0, 154.0, 77.0, 62.0)),
    (75.0, (None, 221.0, 211.0, 192.0, 96.0, 77.0)),
    (100.0, (None, 285.0, 273.0, 248.0, 124.0, 99.0)),
    (125.0, (None, 359.0, 343.0, 312.0, 156.0, 125.0)),
    (150.0, (None, 414.0, 396.0, 360.0, 180.0, 144.0)),
    (200.0, (None, 552.0, 528.0, 480.0, 240.0, 192.0)),
)

# Table 430.248 single-phase. hp, then 115, 200, 208, 230.
_V1 = (115, 200, 208, 230)
_TABLE_430_248: tuple[tuple[float, tuple[float, ...]], ...] = (
    (1.0 / 6.0, (4.4, 2.5, 2.4, 2.2)),
    (0.25, (5.8, 3.3, 3.2, 2.9)),
    (1.0 / 3.0, (7.2, 4.1, 4.0, 3.6)),
    (0.5, (9.8, 5.6, 5.4, 4.9)),
    (0.75, (13.8, 7.9, 7.6, 6.9)),
    (1.0, (16.0, 9.2, 8.8, 8.0)),
    (1.5, (20.0, 11.5, 11.0, 10.0)),
    (2.0, (24.0, 13.8, 13.2, 12.0)),
    (3.0, (34.0, 19.6, 18.7, 17.0)),
    (5.0, (56.0, 32.2, 30.8, 28.0)),
    (7.5, (80.0, 46.0, 44.0, 40.0)),
    (10.0, (100.0, 57.5, 55.0, 50.0)),
)

# Table 430.52(C)(1): nontime-delay fuse, dual-element fuse, instantaneous trip, inverse-time breaker.
_TABLE_430_52: dict[str, tuple[int, int, int, int]] = {
    "single_phase": (300, 175, 800, 250),
    "squirrel_cage": (300, 175, 800, 250),
    "design_b_energy_efficient": (300, 175, 1100, 250),
    "synchronous": (300, 175, 800, 250),
    "wound_rotor": (150, 150, 800, 150),
    "dc": (150, 150, 250, 150),
}
_DEVICE_INDEX = {
    "nontime_delay_fuse": 0,
    "dual_element_fuse": 1,
    "instantaneous_trip": 2,
    "inverse_time_breaker": 3,
}
_DEVICE_LABEL = {
    "nontime_delay_fuse": "nontime-delay fuse",
    "dual_element_fuse": "dual-element (time-delay) fuse",
    "instantaneous_trip": "instantaneous-trip breaker",
    "inverse_time_breaker": "inverse-time breaker",
}
_MOTOR_LABEL = {
    "single_phase": "single-phase",
    "squirrel_cage": "squirrel-cage (other than Design B energy-efficient)",
    "design_b_energy_efficient": "Design B energy-efficient",
    "synchronous": "synchronous",
    "wound_rotor": "wound-rotor",
    "dc": "DC (constant voltage)",
}

# 240.6(A). Inverse-time breakers start at 15 A. Fuses add 1, 3, 6, 10, and 601.
_BREAKER_SIZES: tuple[int, ...] = (
    15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 110, 125, 150, 175, 200,
    225, 250, 300, 350, 400, 450, 500, 600, 700, 800, 1000, 1200, 1600, 2000,
    2500, 3000, 4000, 5000, 6000,
)
_FUSE_SIZES: tuple[int, ...] = (1, 3, 6, 10, *_BREAKER_SIZES, 601)

# Table 310.16 copper. (label, 60°C amperes, 75°C amperes). Through 500 kcmil.
_NEC_310_16: tuple[tuple[str, int, int], ...] = (
    ("14 AWG", 15, 20),
    ("12 AWG", 20, 25),
    ("10 AWG", 30, 35),
    ("8 AWG", 40, 50),
    ("6 AWG", 55, 65),
    ("4 AWG", 70, 85),
    ("3 AWG", 85, 100),
    ("2 AWG", 95, 115),
    ("1 AWG", 110, 130),
    ("1/0 AWG", 125, 150),
    ("2/0 AWG", 145, 175),
    ("3/0 AWG", 165, 200),
    ("4/0 AWG", 195, 230),
    ("250 kcmil", 215, 255),
    ("300 kcmil", 240, 285),
    ("350 kcmil", 260, 310),
    ("400 kcmil", 280, 335),
    ("500 kcmil", 320, 380),
)

# UL 508A Table 28.1 copper. Not verified against the standard.
# None is a blank 60°C cell (1/0 and larger). 18 and 16 AWG are not encoded.
_UL_508A_28_1: tuple[tuple[str, int | None, int], ...] = (
    ("14 AWG", 15, 15),
    ("12 AWG", 20, 20),
    ("10 AWG", 30, 30),
    ("8 AWG", 40, 50),
    ("6 AWG", 55, 65),
    ("4 AWG", 70, 85),
    ("3 AWG", 85, 100),
    ("2 AWG", 95, 115),
    ("1 AWG", 110, 130),
    ("1/0 AWG", None, 150),
    ("2/0 AWG", None, 175),
    ("3/0 AWG", None, 200),
    ("4/0 AWG", None, 230),
    ("250 kcmil", None, 255),
    ("300 kcmil", None, 285),
    ("350 kcmil", None, 310),
    ("400 kcmil", None, 335),
    ("500 kcmil", None, 380),
    ("600 kcmil", None, 420),
    ("700 kcmil", None, 460),
    ("750 kcmil", None, 475),
    ("800 kcmil", None, 490),
    ("900 kcmil", None, 520),
    ("1000 kcmil", None, 545),
    ("1250 kcmil", None, 590),
)

# Table 430.72(B) copper: (column A separate, column B within enclosure, column C beyond).
# Not verified against an official NFPA text.
# None is Note 1 — the value specified in 310.15 / Table 310.16, as applicable.
_TABLE_430_72: dict[int, tuple[int | None, int | None, int | None]] = {
    18: (7, 25, 7),
    16: (10, 40, 10),
    14: (None, 100, 45),
    12: (None, 120, 60),
    10: (None, 160, 90),
}

# UL 508A internal control-circuit conductor ampacity. Not verified against the standard.
_UL_CONTROL_AMPACITY = {18: 7.0, 16: 10.0}

_DEVICE_ALIASES = {
    "breaker": "inverse_time_breaker",
    "inverse": "inverse_time_breaker",
    "inverse_time": "inverse_time_breaker",
    "inverse-time": "inverse_time_breaker",
    "inverse_time_breaker": "inverse_time_breaker",
    "dual": "dual_element_fuse",
    "dual_element": "dual_element_fuse",
    "dual-element": "dual_element_fuse",
    "dual_element_fuse": "dual_element_fuse",
    "time_delay": "dual_element_fuse",
    "time-delay": "dual_element_fuse",
    "fuse": "dual_element_fuse",
    "nontime": "nontime_delay_fuse",
    "nontime_delay": "nontime_delay_fuse",
    "nontime-delay": "nontime_delay_fuse",
    "nontime_delay_fuse": "nontime_delay_fuse",
    "instantaneous": "instantaneous_trip",
    "instantaneous_trip": "instantaneous_trip",
    "mcp": "instantaneous_trip",
}
_MOTOR_ALIASES = {
    "squirrel": "squirrel_cage",
    "squirrel_cage": "squirrel_cage",
    "induction": "squirrel_cage",
    "design_b": "design_b_energy_efficient",
    "design_b_energy_efficient": "design_b_energy_efficient",
    "design-b": "design_b_energy_efficient",
    "synchronous": "synchronous",
    "wound_rotor": "wound_rotor",
    "wound-rotor": "wound_rotor",
    "dc": "dc",
    "single_phase": "single_phase",
    "single-phase": "single_phase",
}

_HP_TOKEN = re.compile(r"^\s*(\d+)\s*[-/]\s*(\d+)\s*$")


def fmt_amps(value: float) -> str:
    """Compact ampere text: 7.6, 14, 9.5."""

    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text


def parse_hp(value: Any) -> float | None:
    """Accept 5, 7.5, ``1/2``, ``1-1/2``, or ``7 1/2``."""

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace("hp", "").strip()
    mixed = re.match(r"^(\d+)\s+(\d+)\s*/\s*(\d+)$", text)
    if mixed:
        return float(mixed.group(1)) + float(mixed.group(2)) / float(mixed.group(3))
    slash = _HP_TOKEN.match(text)
    if slash:
        return float(slash.group(1)) / float(slash.group(2))
    return float(text)


def is_iec_standard(standard: str | None) -> bool:
    return str(standard or "").strip().upper().startswith("IEC")


def _match_row(hp: float, rows: tuple[tuple[float, tuple], ...]) -> tuple[float, tuple] | None:
    for key, amps in rows:
        if abs(float(key) - float(hp)) <= 0.02:
            return float(key), amps
    return None


def nearest_table_hp(hp: float, *, phases: int = 3) -> float | None:
    """Closest published horsepower. Used only when a kW conversion is off-row."""

    rows = _TABLE_430_248 if int(phases) == 1 else _TABLE_430_250
    if not rows:
        return None
    return min(rows, key=lambda row: abs(row[0] - float(hp)))[0]


def table_voltage(voltage: float, *, phases: int = 3) -> tuple[float, str] | None:
    """Rated-motor column for a system voltage.

    Exact columns win. NEC Table 430.248/430.250 permit the listed currents
    for 110–120 V and 220–240 V systems. A 480 V system uses the 460 V motor
    column, and 575/600 V uses the 575 V column. Those are column selections,
    not a voltage-ratio scaling of the amperes. 400 V is not a column.
    """

    volts = float(voltage)
    columns = _V1 if int(phases) == 1 else _V3
    for col in columns:
        if abs(volts - col) < 0.51:
            return float(col), "exact"
    if 110.0 <= volts <= 120.0 and 115 in columns:
        return 115.0, "110-120 V uses the 115 V column"
    if 220.0 <= volts <= 240.0 and 230 in columns:
        return 230.0, "220-240 V uses the 230 V column"
    if int(phases) != 1 and 440.0 <= volts <= 480.0:
        return 460.0, "480 V system uses the 460 V rated-motor column"
    if int(phases) != 1 and 550.0 <= volts <= 600.0:
        return 575.0, "600 V system uses the 575 V rated-motor column"
    return None


def motor_flc(
    *,
    hp: float | None = None,
    voltage: float | None = None,
    phases: int = 3,
) -> tuple[float, float, float] | None:
    """Return ``(table_hp, column_volts, amperes)`` or None when the cell is blank.

    Horsepower must be a published row. This function does not snap to a
    neighbor and does not scale amperes.
    """

    if hp is None or voltage is None:
        return None
    phase_n = int(phases)
    if phase_n not in {1, 3}:
        return None
    rows = _TABLE_430_248 if phase_n == 1 else _TABLE_430_250
    matched = _match_row(float(hp), rows)
    if matched is None:
        return None
    table_hp, amps = matched
    mapped = table_voltage(float(voltage), phases=phase_n)
    if mapped is None:
        return None
    column, _why = mapped
    headings = _V1 if phase_n == 1 else _V3
    cell = amps[headings.index(int(column))]
    if cell is None:
        return None
    return table_hp, column, float(cell)


def normalize_device(value: Any) -> str | None:
    key = str(value or "inverse_time_breaker").strip().lower().replace(" ", "_")
    return _DEVICE_ALIASES.get(key)


def normalize_motor_type(value: Any, *, phases: int) -> str | None:
    if value is None or value == "":
        return "single_phase" if int(phases) == 1 else "squirrel_cage"
    key = str(value).strip().lower().replace(" ", "_")
    return _MOTOR_ALIASES.get(key)


def normalize_wire_table(value: Any, temp_c: int | None = None) -> tuple[str, int] | None:
    """Return ``(table id, 60 or 75)``. ``nec-60`` / ``ul-75`` set the column."""

    text = str(value or "nec_310_16").strip().lower().replace(" ", "_")
    temp = 75 if temp_c is None else int(temp_c)
    if text in {"nec-60", "nec_60", "nec_310_16_60"}:
        return "nec_310_16", 60
    if text in {"nec-75", "nec_75", "nec_310_16_75", "nec", "nec_310_16"}:
        if text in {"nec", "nec_310_16"} and temp_c is not None:
            return "nec_310_16", temp
        if text in {"nec-75", "nec_75", "nec_310_16_75"}:
            return "nec_310_16", 75
        return "nec_310_16", temp
    if text in {"ul-60", "ul_60", "ul_508a_60"}:
        return "ul_508a_28_1", 60
    if text in {"ul-75", "ul_75", "ul_508a_75", "ul", "ul_508a", "ul_508a_28_1"}:
        if text in {"ul", "ul_508a", "ul_508a_28_1"} and temp_c is not None:
            return "ul_508a_28_1", temp
        if text in {"ul-75", "ul_75", "ul_508a_75"}:
            return "ul_508a_28_1", 75
        return "ul_508a_28_1", temp
    return None


def _wire_rows(table: str) -> tuple[tuple[str, int | None, int | None], ...]:
    if table == "ul_508a_28_1":
        return _UL_508A_28_1
    return _NEC_310_16


def _gauge_key(text: str) -> str | None:
    raw = str(text or "").strip().lower().replace(" ", "")
    raw = raw.replace("awg", "").replace("kcmil", "").replace("mcm", "").replace("#", "")
    if raw in {"1/0", "2/0", "3/0", "4/0", "0"}:
        return "1/0" if raw in {"1/0", "0"} else raw
    if not raw.isdigit():
        return None
    return raw


def ampacity_of(gauge: str, *, table: str = "nec_310_16", temp_c: int = 75) -> float | None:
    """Published ampacity for one gauge in the selected column, or None."""

    key = _gauge_key(gauge)
    if key is None or int(temp_c) not in {60, 75}:
        return None
    for label, a60, a75 in _wire_rows(table):
        if _gauge_key(label) == key:
            amp = a60 if int(temp_c) == 60 else a75
            return None if amp is None else float(amp)
    return None


def select_wire(required_a: float, *, table: str = "nec_310_16", temp_c: int = 75) -> dict[str, Any] | None:
    """Smallest conductor whose published ampacity is at least ``required_a``."""

    if int(temp_c) not in {60, 75}:
        return None
    for label, a60, a75 in _wire_rows(table):
        amp = a60 if int(temp_c) == 60 else a75
        if amp is None:
            continue
        if float(amp) + 1e-9 >= float(required_a):
            if table == "ul_508a_28_1":
                cite = f"UL 508A Table 28.1, copper, {int(temp_c)}°C column. {UL_UNVERIFIED}"
            else:
                cite = f"NEC Table 310.16, copper, {int(temp_c)}°C column"
            return {
                "gauge": label,
                "ampacity_a": float(amp),
                "table": table,
                "temp_c": int(temp_c),
                "citation": cite,
            }
    return None


# Smaller rank is the physically larger conductor. AWG descends, then kcmil ascends.
_GAUGE_RANK = {
    "18": 180,
    "16": 160,
    "14": 140,
    "12": 120,
    "10": 100,
    "8": 80,
    "6": 60,
    "4": 40,
    "3": 30,
    "2": 20,
    "1": 10,
    "1/0": 0,
    "2/0": -10,
    "3/0": -20,
    "4/0": -30,
    "250": -40,
    "300": -50,
    "350": -60,
    "400": -70,
    "500": -80,
    "600": -90,
    "700": -100,
    "750": -110,
    "800": -120,
    "900": -130,
    "1000": -140,
    "1250": -150,
}


def _gauge_rank(gauge: str) -> int:
    key = _gauge_key(gauge)
    if key is None or key not in _GAUGE_RANK:
        return 10_000
    return _GAUGE_RANK[key]


def larger_gauge(a: str, b: str) -> str:
    """The physically larger of two published sizes."""

    return a if _gauge_rank(a) <= _gauge_rank(b) else b


def _known_wire_request(value: Any) -> bool:
    text = str(value or "auto").strip().lower().replace(" ", "_")
    return text in {
        "",
        "auto",
        "nec",
        "nec_310_16",
        "nec-60",
        "nec_60",
        "nec_310_16_60",
        "nec-75",
        "nec_75",
        "nec_310_16_75",
        "ul",
        "ul_508a",
        "ul_508a_28_1",
        "ul-60",
        "ul_60",
        "ul_508a_60",
        "ul-75",
        "ul_75",
        "ul_508a_75",
        "marked-60",
        "marked-75",
    }


def termination_temperature(
    component_amps: float,
    *,
    termination_c: int | None = None,
    temp_c: int | None = None,
    wire_table: str = "auto",
) -> tuple[int, str]:
    """UL 508A 29.6 column, or an explicit 60°C / 75°C termination mark.

    Unmarked: 60°C at 100 A or less, 75°C above 100 A. ``termination_c``,
    ``temp_c``, or a ``nec-60`` / ``ul-75`` table name is the mark.
    """

    marked = termination_c if termination_c is not None else temp_c
    if marked in (60, 75):
        return int(marked), f"Terminations marked {int(marked)}°C."
    text = str(wire_table or "auto").strip().lower().replace(" ", "_")
    if text in {"nec-60", "nec_60", "nec_310_16_60", "ul-60", "ul_60", "ul_508a_60", "marked-60"}:
        return 60, "Terminations marked 60°C."
    if text in {"nec-75", "nec_75", "nec_310_16_75", "ul-75", "ul_75", "ul_508a_75", "marked-75"}:
        return 75, "Terminations marked 75°C."
    amps = float(component_amps)
    if amps <= 100.0:
        return 60, (
            f"UL 508A 29.6 ({UL_UNVERIFIED}): no termination temperature is marked, and the branch device "
            f"is {fmt_amps(amps)} A (100 A or less), so the 60°C column is used."
        )
    return 75, (
        f"UL 508A 29.6 ({UL_UNVERIFIED}): no termination temperature is marked, and the branch device "
        f"is {fmt_amps(amps)} A (above 100 A), so the 75°C column is used."
    )


def select_branch_wire(required_a: float, temp_c: int) -> dict[str, Any] | None:
    """Larger of NEC Table 310.16 and UL 508A Table 28.1 at one temperature column."""

    if int(temp_c) not in {60, 75}:
        return None
    nec = select_wire(required_a, table="nec_310_16", temp_c=int(temp_c))
    ul = select_wire(required_a, table="ul_508a_28_1", temp_c=int(temp_c))
    if nec is None or ul is None:
        return None
    chosen = larger_gauge(str(nec["gauge"]), str(ul["gauge"]))
    nec_same = ampacity_of(chosen, table="nec_310_16", temp_c=int(temp_c))
    ul_same = ampacity_of(chosen, table="ul_508a_28_1", temp_c=int(temp_c))
    limits = [amp for amp in (nec_same, ul_same) if amp is not None]
    if chosen == nec["gauge"] and chosen == ul["gauge"]:
        governed = "both"
        citation = (
            f"NEC Table 310.16 and UL 508A Table 28.1, copper, {int(temp_c)}°C. "
            f"Both require {chosen}. {UL_UNVERIFIED}."
        )
    elif chosen == ul["gauge"]:
        governed = "ul_508a_28_1"
        citation = (
            f"UL 508A Table 28.1 {int(temp_c)}°C requires {chosen} "
            f"({fmt_amps(ul['ampacity_a'])} A). {UL_UNVERIFIED}. "
            f"NEC Table 310.16 {int(temp_c)}°C would allow {nec['gauge']} "
            f"({fmt_amps(nec['ampacity_a'])} A). The larger conductor governs."
        )
    else:
        governed = "nec_310_16"
        citation = (
            f"NEC Table 310.16 {int(temp_c)}°C requires {chosen} "
            f"({fmt_amps(nec['ampacity_a'])} A). "
            f"UL 508A Table 28.1 {int(temp_c)}°C would allow {ul['gauge']} "
            f"({fmt_amps(ul['ampacity_a'])} A). {UL_UNVERIFIED}. The larger conductor governs."
        )
    return {
        "gauge": chosen,
        "ampacity_a": min(limits) if limits else None,
        "table": "nec_310_16+ul_508a_28_1",
        "temp_c": int(temp_c),
        "governed_by": governed,
        "citation": citation,
        "nec_310_16": nec,
        "ul_508a_28_1": ul,
    }


def _next_standard(amps: float, *, fuse: bool) -> float | None:
    sizes = _FUSE_SIZES if fuse else _BREAKER_SIZES
    target = round(float(amps), 2)
    for size in sizes:
        if float(size) + 1e-9 >= target - 0.051:
            return float(size)
    return None


def branch_device_max(
    flc: float,
    *,
    device: str = "inverse_time_breaker",
    motor_type: str = "squirrel_cage",
) -> dict[str, Any]:
    """Table 430.52(C)(1) maximum, then the next 240.6(A) size for fuses and inverse-time breakers.

    Instantaneous-trip breakers are adjustable settings. The maximum is the
    table percentage itself (430.52(C)(3)), not the next standard frame.
    """

    kind = normalize_device(device)
    motor = normalize_motor_type(motor_type, phases=3 if motor_type != "single_phase" else 1)
    if kind is None or motor is None or motor not in _TABLE_430_52:
        raise ValueError(f"unknown device '{device}' or motor type '{motor_type}'")
    percent = _TABLE_430_52[motor][_DEVICE_INDEX[kind]]
    calculated = round(float(flc) * percent / 100.0, 2)
    fuse = kind in {"nontime_delay_fuse", "dual_element_fuse"}
    if kind == "instantaneous_trip":
        rating = calculated
        rule = "NEC 430.52(C)(3): the instantaneous-trip setting shall not exceed the table percentage"
    else:
        rating = _next_standard(calculated, fuse=fuse)
        rule = (
            "NEC 430.52(C)(1)(a): where the table value is not a standard rating in 240.6(A), "
            "the next higher standard rating is permitted"
        )
    return {
        "device": kind,
        "device_label": _DEVICE_LABEL[kind],
        "motor_type": motor,
        "motor_label": _MOTOR_LABEL[motor],
        "percent": percent,
        "calculated_a": calculated,
        "rating_a": rating,
        "permitted_max_a": rating,
        "rule": rule,
        "citation": "NEC Table 430.52(C)(1)",
    }


def overload_limits(
    current_a: float,
    *,
    service_factor: float | None = None,
    temp_rise_c: float | None = None,
) -> dict[str, Any]:
    """430.32(A)(1). The relay is set to the nameplate current.

    Trip is not over 125% when the service factor is 1.15 or greater, or the
    temperature rise is 40°C or less. Otherwise 115%. When neither marking is
    given, 115% is used (the "all other motors" row). The percentages are not
    verified against an official NFPA text.
    """

    current = float(current_a)
    sf = None if service_factor is None else float(service_factor)
    rise = None if temp_rise_c is None else float(temp_rise_c)
    if (sf is not None and sf >= 1.15) or (rise is not None and rise <= 40.0):
        percent = 125
        why = "service factor 1.15 or greater, or temperature rise 40°C or less"
    else:
        percent = 115
        why = (
            "all other motors"
            if sf is None and rise is None
            else "service factor below 1.15 and temperature rise above 40°C, or those markings were not both in the 125% case"
        )
    return {
        "setting_a": round(current, 2),
        "trip_percent": percent,
        "trip_max_a": round(current * percent / 100.0, 2),
        "rule": f"NEC 430.32(A)(1), {why}; {NEC_UNVERIFIED}",
        "citation": f"NEC 430.32(A)(1); {NEC_UNVERIFIED}",
    }


def feeder_ampacity(flcs: list[float]) -> dict[str, Any]:
    """NEC 430.24 for two or more motors: 125% of the largest FLC plus the other FLCs."""

    values = [round(float(item), 2) for item in flcs if item is not None]
    if not values:
        raise ValueError("feeder sizing needs at least one motor full-load current")
    largest = max(values)
    others = round(sum(values) - largest, 2)
    required = round(1.25 * largest + others, 2)
    return {
        "flcs_a": values,
        "largest_a": largest,
        "others_a": others,
        "required_a": required,
        "rule": "NEC 430.24: 125% of the largest motor full-load current plus the sum of the other motors' full-load currents",
        "citation": "NEC 430.24",
    }


def control_circuit(awg: int = 16) -> dict[str, Any]:
    """Table 430.72(B) copper and the UL 508A internal control ampacity.

    Both sets of numbers are unverified against an official text.
    """

    size = int(awg)
    if size not in _TABLE_430_72:
        raise ValueError(f"Table 430.72(B) in this module covers 18, 16, 14, 12, and 10 AWG, not {size}")
    column_a, column_b, column_c = _TABLE_430_72[size]
    note_1 = None
    if column_a is None:
        note_1 = (
            "Column A is Note 1: the value specified in 310.15 as applicable. "
            "240.4(D) limits overcurrent protection of 14, 12, and 10 AWG copper to "
            "15 A, 20 A, and 30 A unless 240.4(G) defers to another article."
        )
    ul_amp = _UL_CONTROL_AMPACITY.get(size)
    return {
        "awg": size,
        "gauge": f"{size} AWG",
        "column_a_separate_a": column_a,
        "column_b_within_enclosure_a": column_b,
        "column_c_beyond_enclosure_a": column_c,
        "column_a_note": note_1,
        "ul_508a_internal_ampacity_a": ul_amp,
        "citation": (
            f"Table 430.72(B) ({NEC_UNVERIFIED}); "
            f"UL 508A internal control-circuit conductors (18 AWG = 7 A, 16 AWG = 10 A). {UL_UNVERIFIED}"
        ),
    }


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "steps": [message], "report": message, **extra}


def size_motor_circuit(
    *,
    hp: float | None = None,
    kw: float | None = None,
    volts: float | None = None,
    phases: int = 3,
    fla: float | None = None,
    standard: str = "NEMA/ANSI",
    device: str = "inverse_time_breaker",
    motor_type: str | None = None,
    wire_table: str = "auto",
    temp_c: int | None = None,
    termination_c: int | None = None,
    service_factor: float | None = None,
    temp_rise_c: float | None = None,
    allow_nearest_hp: bool = False,
    overload_factor: float = 1.0,
    additional_flc: list[float] | None = None,
) -> dict[str, Any]:
    """Size one motor branch. ``fla`` is the nameplate current used for the overload.

    Conductors and the branch short-circuit device use the NEC table current
    (430.6(A)(1)). The overload setting uses the nameplate when one is given
    (430.32). IEC (``standard`` starting with IEC) returns kW plus that
    current and does not apply Tables 430.248 or 430.250.
    """

    phase_n = int(phases)
    nameplate = None if fla in (None, "") else float(fla)
    hp_v = parse_hp(hp)
    kw_v = None if kw in (None, "") else float(kw)
    volts_v = None if volts in (None, "") else float(volts)

    if is_iec_standard(standard):
        return _size_iec(hp=hp_v, kw=kw_v, volts=volts_v, phases=phase_n, nameplate=nameplate)

    if phase_n not in {1, 3}:
        return _fail("phases must be 1 or 3")
    kind = normalize_device(device)
    if kind is None:
        return _fail(f"unknown branch device '{device}'")
    motor = normalize_motor_type(motor_type, phases=phase_n)
    if motor is None:
        return _fail(f"unknown motor type '{motor_type}'")
    if phase_n == 1:
        motor = "single_phase"
    if temp_c not in (None, 60, 75) or termination_c not in (None, 60, 75):
        return _fail("wire temperature column must be 60°C or 75°C")
    if not _known_wire_request(wire_table):
        return _fail(f"unknown wire table '{wire_table}'")
    if volts_v is None or volts_v <= 0:
        return _fail("volts is required")
    if hp_v is None and kw_v is None:
        return _fail("hp or kW is required")
    if hp_v is None and kw_v is not None:
        hp_v = kw_v / HP_TO_KW
    assert hp_v is not None
    requested_hp = hp_v
    snapped = False
    looked = motor_flc(hp=hp_v, voltage=volts_v, phases=phase_n)
    if looked is None and allow_nearest_hp:
        neighbor = nearest_table_hp(hp_v, phases=phase_n)
        if neighbor is not None:
            looked = motor_flc(hp=neighbor, voltage=volts_v, phases=phase_n)
            if looked is not None:
                hp_v = neighbor
                snapped = True
    if looked is None:
        mapped = table_voltage(volts_v, phases=phase_n)
        if mapped is None:
            return _fail(
                f"{fmt_amps(volts_v)} V is not a Table {'430.248' if phase_n == 1 else '430.250'} column. "
                "IEC 400 V is not given an NEC full-load current.",
                phases=phase_n,
                voltage=volts_v,
            )
        return _fail(
            f"{fmt_amps(requested_hp)} hp has no published cell at {fmt_amps(mapped[0])} V "
            f"in NEC Table {'430.248' if phase_n == 1 else '430.250'}.",
            phases=phase_n,
            hp=requested_hp,
            voltage=volts_v,
        )
    table_hp, column_v, flc = looked
    mapped = table_voltage(volts_v, phases=phase_n)
    assert mapped is not None
    _column, voltage_why = mapped
    table_cite = "NEC Table 430.248" if phase_n == 1 else "NEC Table 430.250"
    ocpd = branch_device_max(flc, device=kind, motor_type=motor)
    if ocpd["permitted_max_a"] is None:
        return _fail(
            f"calculated {ocpd['calculated_a']} A is above the standard ratings encoded from NEC 240.6(A)",
            flc_a=flc,
        )
    conductor_min = round(flc * 1.25, 2)
    device_amps = float(ocpd["rating_a"] if ocpd["rating_a"] is not None else conductor_min)
    column_c, termination_why = termination_temperature(
        device_amps,
        termination_c=termination_c,
        temp_c=temp_c,
        wire_table=wire_table,
    )
    wire = select_branch_wire(conductor_min, column_c)
    if wire is None:
        return _fail(
            f"{fmt_amps(conductor_min)} A is above an encoded {column_c}°C row in NEC Table 310.16 or UL 508A Table 28.1. {UL_UNVERIFIED}",
            flc_a=flc,
            conductor_min_a=conductor_min,
        )
    wire["termination_rule"] = termination_why
    ol_base = nameplate if nameplate is not None else flc
    factor = float(overload_factor)
    if factor <= 0:
        return _fail("overload_factor must be positive")
    ol_current = round(ol_base * factor, 2)
    thermal = overload_limits(ol_current, service_factor=service_factor, temp_rise_c=temp_rise_c)
    control = control_circuit(16)
    feeder = None
    if additional_flc:
        feeder = feeder_ampacity([flc, *[float(item) for item in additional_flc]])

    steps: list[str] = []
    hp_text = _hp_text(table_hp)
    if snapped:
        steps.append(
            f"Requested {fmt_amps(requested_hp)} hp is not a table row. "
            f"Nearest published row is {hp_text} hp."
        )
    phase_text = "single-phase" if phase_n == 1 else "3-phase"
    motor_label = _MOTOR_LABEL[motor]
    # A 1-phase motor's table row is also named single-phase. Say that once.
    named = phase_text if motor_label == phase_text else f"{phase_text}, {motor_label}"
    steps.append(f"{hp_text} hp, {fmt_amps(volts_v)} V, {named}.")
    if voltage_why != "exact":
        steps.append(
            f"{fmt_amps(volts_v)} V: {voltage_why} ({fmt_amps(column_v)} V). Amperes are the published cell, not a ratio."
        )
    steps.append(f"Full-load current {fmt_amps(flc)} A from {table_cite}, {fmt_amps(column_v)} V column.")
    if nameplate is not None:
        steps.append(
            f"Nameplate {fmt_amps(nameplate)} A is the overload current (NEC 430.32). "
            f"Conductors and the branch device stay on the table current (NEC 430.6(A)(1))."
        )
    else:
        steps.append(
            "No nameplate current was given. The overload uses the table full-load current as a placeholder. "
            "NEC 430.6(A)(2) and 430.32 require the nameplate current before this ships."
        )
    steps.append(f"Branch conductors: {fmt_amps(flc)} A × 1.25 = {fmt_amps(conductor_min)} A minimum (NEC 430.22).")
    steps.append(termination_why)
    nec_row = wire["nec_310_16"]
    ul_row = wire["ul_508a_28_1"]
    steps.append(
        f"NEC Table 310.16 {column_c}°C: {nec_row['gauge']} copper, {fmt_amps(nec_row['ampacity_a'])} A."
    )
    steps.append(
        f"UL 508A Table 28.1 {column_c}°C: {ul_row['gauge']} copper, {fmt_amps(ul_row['ampacity_a'])} A. "
        f"{UL_UNVERIFIED}."
    )
    if wire["governed_by"] == "both":
        steps.append(f"Governing conductor: {wire['gauge']}. Both rules require it. {UL_UNVERIFIED}.")
    else:
        steps.append(f"Governing conductor: {wire['gauge']}. {wire['citation']}")
    steps.append(
        f"Branch device: {ocpd['device_label']}, {ocpd['percent']}% × {fmt_amps(flc)} A "
        f"= {fmt_amps(ocpd['calculated_a'])} A ({ocpd['citation']}, {ocpd['motor_label']})."
    )
    if kind == "instantaneous_trip":
        steps.append(f"Maximum instantaneous-trip setting {fmt_amps(ocpd['rating_a'])} A. {ocpd['rule']}.")
    elif abs(float(ocpd["rating_a"]) - float(ocpd["calculated_a"])) < 0.051:
        steps.append(f"Selected rating {fmt_amps(ocpd['rating_a'])} A, which is already a standard rating (NEC 240.6(A)).")
    else:
        steps.append(
            f"Selected rating {fmt_amps(ocpd['rating_a'])} A, the next standard rating at or above "
            f"{fmt_amps(ocpd['calculated_a'])} A. {ocpd['rule']}."
        )
    if factor != 1.0:
        steps.append(
            f"Overload is in the windings: {fmt_amps(ol_base)} A × {fmt_amps(factor)} = {fmt_amps(ol_current)} A."
        )
    steps.append(
        f"Overload setting {fmt_amps(thermal['setting_a'])} A. "
        f"Trip not over {fmt_amps(thermal['trip_max_a'])} A ({thermal['trip_percent']}%, {thermal['rule']})."
    )
    contactor_frame = 9.0 if flc <= 9.0 else 32.0
    if flc <= 32.0:
        contactor_fit = "."
    else:
        contactor_fit = (
            " does not cover this current. Design check contactor_undersized; "
            "the 32 A symbol is the largest builtin contactor."
        )
    steps.append(
        f"Contactor continuous rating at least {fmt_amps(flc)} A. Builtin library frame {fmt_amps(contactor_frame)} A"
        + contactor_fit
    )
    breaker_rating = float(ocpd["rating_a"])
    if breaker_rating > 100.0 + 0.05:
        steps.append(
            f"Breaker rating {fmt_amps(breaker_rating)} A is above the largest builtin frame (100 A). "
            "Placing that frame raises design check breaker_frame_below_rating."
        )
    elif breaker_rating > 32.0 + 0.05:
        steps.append(
            f"Breaker rating {fmt_amps(breaker_rating)} A is above the 32 A builtin frame. "
            "The next builtin frame is the 100 A MCCB. "
            "Placing the 32 A frame raises design check breaker_frame_below_rating."
        )
    steps.append(
        f"Control circuit: {control['gauge']}. Separate protection not over {control['column_a_separate_a']} A "
        f"(Table 430.72(B) Column A; {NEC_UNVERIFIED}). Inside the enclosure the branch device may be up to "
        f"{control['column_b_within_enclosure_a']} A (Column B; {NEC_UNVERIFIED}). "
        f"UL 508A internal control ampacity for {control['gauge']} is "
        f"{fmt_amps(control['ul_508a_internal_ampacity_a'])} A. {UL_UNVERIFIED}."
    )
    if feeder is not None:
        steps.append(
            f"Feeder for these motors: {fmt_amps(feeder['required_a'])} A minimum. {feeder['rule']}."
        )
    report = "\n".join(steps)
    return {
        "ok": True,
        "code": "NEC",
        "standard": "NEMA/ANSI",
        "phases": phase_n,
        "hp": table_hp,
        "hp_requested": requested_hp,
        "hp_snapped": snapped,
        "kw": None if kw_v is None else kw_v,
        "voltage": volts_v,
        "voltage_column": column_v,
        "voltage_note": voltage_why,
        "flc_a": flc,
        "flc_source": table_cite,
        "nameplate_fla_a": nameplate,
        "conductor_min_a": conductor_min,
        "conductor_rule": "NEC 430.22, 125% of table full-load current",
        "wire": wire,
        "ocpd": ocpd,
        "overload_current_a": ol_current,
        "overload_factor": factor,
        "overload": thermal,
        "contactor_min_a": flc,
        "contactor_frame_a": contactor_frame,
        "contactor_adequate": flc <= 32.0,
        "control": control,
        "feeder": feeder,
        "steps": steps,
        "report": report,
        "citations": _citations(),
    }


def _size_iec(
    *,
    hp: float | None,
    kw: float | None,
    volts: float | None,
    phases: int,
    nameplate: float | None,
) -> dict[str, Any]:
    kw_text = f"{fmt_amps(kw)} kW" if kw is not None else "kW not given"
    steps = [
        f"IEC motor: {kw_text}"
        + (f", {fmt_amps(hp)} hp" if hp is not None else "")
        + (f", {fmt_amps(volts)} V" if volts is not None else "")
        + f", {phases}-phase.",
        "IEC projects do not use NEC Table 430.248 or 430.250. No full-load table was invented.",
    ]
    if nameplate is None:
        steps.append("Pass the nameplate or other user current (fla) to record the design current.")
        report = "\n".join(steps)
        return {
            "ok": False,
            "code": "IEC",
            "error": "IEC sizing needs a nameplate or user current (fla). NEC full-load tables are not used.",
            "kw": kw,
            "hp": hp,
            "voltage": volts,
            "phases": phases,
            "nameplate_fla_a": None,
            "flc_a": None,
            "steps": steps,
            "report": report,
            "citations": ["IEC motor current is the nameplate or the current you enter. NEC Tables 430.248 and 430.250 are not applied."],
        }
    steps.append(f"Design current {fmt_amps(nameplate)} A from the nameplate or user entry.")
    steps.append("Branch device, conductor, and overload percentages from Article 430 were not applied.")
    report = "\n".join(steps)
    return {
        "ok": True,
        "code": "IEC",
        "standard": "IEC",
        "phases": phases,
        "hp": hp,
        "kw": kw,
        "voltage": volts,
        "flc_a": None,
        "flc_source": None,
        "nameplate_fla_a": nameplate,
        "steps": steps,
        "report": report,
        "citations": ["IEC motor current is the nameplate or the current you enter. NEC Tables 430.248 and 430.250 are not applied."],
    }


def _hp_text(hp: float) -> str:
    if abs(hp - (1.0 / 6.0)) <= 0.02:
        return "1/6"
    if abs(hp - (1.0 / 3.0)) <= 0.02:
        return "1/3"
    if abs(hp - 0.25) <= 0.001:
        return "1/4"
    if abs(hp - 0.5) <= 0.001:
        return "1/2"
    if abs(hp - 0.75) <= 0.001:
        return "3/4"
    if abs(hp - 1.5) <= 0.001:
        return "1-1/2"
    if abs(hp - 7.5) <= 0.001:
        return "7-1/2"
    return fmt_amps(hp)


def _citations() -> list[str]:
    return [
        "NEC Table 430.250 (3-phase) and Table 430.248 (1-phase), full-load current",
        "NEC 430.6(A)(1): table current for conductors and branch short-circuit protection; nameplate for overload",
        "NEC 430.22: single-motor conductor ampacity not less than 125% of table full-load current",
        "NEC 430.24: feeder, 125% of the largest motor plus the other motors",
        "NEC Table 430.52(C)(1) and 430.52(C)(1)(a) / 240.6(A)",
        f"NEC 430.32(A)(1): overload trip 125% or 115% of nameplate; {NEC_UNVERIFIED}",
        f"Table 430.72(B): motor control-circuit conductors; {NEC_UNVERIFIED}",
        "NEC Table 310.16 copper 60°C and 75°C columns",
        (
            "UL 508A Table 28.1 copper internal/field power conductors; "
            f"UL 508A internal control conductors 18 AWG = 7 A, 16 AWG = 10 A. {UL_UNVERIFIED}"
        ),
    ]


def citation_markdown() -> str:
    """The verification note stored with the sizing artifacts."""

    lines = [
        "# Motor and conductor sizing — table citations",
        "",
        "PanelCAD copies table cells. It does not scale a column by voltage ratio and it does not fill a blank cell.",
        "The lists below are the cells encoded in `src/panelcad/core/motor_sizing.py`.",
        "NFPA 70 table numbers are the citation for 430.248, 430.250, 430.52, 430.22, 430.24, 310.16, and 240.6(A).",
        "UL 508A numbers, Table 430.72(B), and the 430.32(A)(1) percentages are marked unverified. No unofficial site is cited.",
        "",
        "## NEC Table 430.250 — three-phase full-load current",
        "",
        "Induction-type squirrel-cage and wound-rotor motors, usual speed, normal torque.",
        "Columns encoded: 115, 200, 208, 230, 460, 575 V, from 1/2 hp through 200 hp.",
        "Citation: NFPA 70 Table 430.250.",
        "Horsepower above 200 and the 2300 V column are not encoded.",
        "",
        "1-1/2 hp at 575 V is **2.4 A**.",
        "",
        "Spot checks encoded and tested:",
        "",
        "| hp | V | A |",
        "| --- | --- | --- |",
        "| 1/2 | 115 | 4.4 |",
        "| 5 | 200 | 17.5 |",
        "| 5 | 230 | 15.2 |",
        "| 5 | 460 | 7.6 |",
        "| 5 | 575 | 6.1 |",
        "| 10 | 230 | 28 |",
        "| 10 | 460 | 14 |",
        "| 10 | 208 | 30.8 |",
        "| 1-1/2 | 575 | 2.4 |",
        "| 25 | 460 | 34 |",
        "",
        "A 480 V system uses the **460 V** column. A 110–120 V system uses 115 V. A 220–240 V system uses 230 V.",
        "The amperes are not multiplied by a voltage ratio. 400 V is not a column.",
        "",
        "## NEC Table 430.248 — single-phase full-load current",
        "",
        "1/6 hp through 10 hp at 115, 200, 208, and 230 V.",
        "Citation: NFPA 70 Table 430.248.",
        "There is no 460 V or 575 V column. A request for one returns no current.",
        "",
        "Spot checks: 1/6 hp 115 V = 4.4 A; 1 hp 115 V = 16 A; 5 hp 230 V = 28 A; 10 hp 230 V = 50 A; 1/3 hp 208 V = 4.0 A.",
        "",
        "## NEC Table 430.52(C)(1) — branch short-circuit and ground-fault device",
        "",
        "Percent of the **table** full-load current (430.6(A)(1)), not the nameplate.",
        "Citation: NFPA 70 Table 430.52(C)(1).",
        "",
        "| Motor | Nontime-delay fuse | Dual-element fuse | Instantaneous trip | Inverse-time breaker |",
        "| --- | --- | --- | --- | --- |",
        "| Single-phase | 300% | 175% | 800% | 250% |",
        "| Squirrel-cage, other than Design B energy-efficient | 300% | 175% | 800% | 250% |",
        "| Design B energy-efficient | 300% | 175% | 1100% | 250% |",
        "| Synchronous | 300% | 175% | 800% | 250% |",
        "| Wound-rotor | 150% | 150% | 800% | 150% |",
        "| DC (constant voltage) | 150% | 150% | 250% | 150% |",
        "",
        "Fuses and inverse-time breakers: if that product is not a standard rating in 240.6(A), the next higher standard rating is permitted (430.52(C)(1)(a)).",
        "Standard ratings encoded from 240.6(A): 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 110, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500, 600, 700, 800, 1000, 1200, 1600, 2000, 2500, 3000, 4000, 5000, 6000.",
        "Fuses also include 1, 3, 6, 10, and 601.",
        "An instantaneous-trip setting stops at the table percentage. It is not bumped to the next frame.",
        "",
        "Worked examples used by the tests:",
        "",
        "* 5 hp, 460 V, 7.6 A, inverse-time breaker: 250% × 7.6 = 19 A, next standard size **20 A**.",
        "* 10 hp, 230 V, 28 A, inverse-time breaker: 250% × 28 = **70 A** (already standard).",
        "* 5 hp, 460 V, dual-element fuse: 175% × 7.6 = 13.3 A, next fuse size **15 A**.",
        "",
        "The design check uses this permitted maximum. A further increase when the motor will not start (430.52(C)(1)(b)) is not the default ceiling.",
        "",
        "## NEC 430.22, 430.24, and 430.32",
        "",
        "* **430.22** — conductor ampacity not less than 125% of the table full-load current. 5 hp 460 V: 7.6 × 1.25 = **9.5 A**. 10 hp 230 V: 28 × 1.25 = **35 A**.",
        "* **430.24** — feeder for several motors: 125% of the largest table current plus the other table currents. 14 A and 7.6 A: 1.25 × 14 + 7.6 = **25.1 A**.",
        "* **430.32(A)(1)** — set the overload to the nameplate current. Maximum trip is 125% of nameplate when the service factor is 1.15 or greater or the temperature rise is 40°C or less; otherwise 115%. With neither marking given, the module uses 115%. Not verified against an official NFPA text.",
        "* **430.6(A)(1)** — nameplate current does not replace the table for the conductor or the branch device.",
        "",
        "## NEC Table 310.16 — copper, 60°C and 75°C",
        "",
        "Not more than three current-carrying conductors, 30°C ambient. Citation: NFPA 70 Table 310.16.",
        "14 AWG is 15 A at 60°C and 20 A at 75°C.",
        "Rows stop at 500 kcmil. 600 kcmil and larger are not encoded.",
        "The 90°C column is not a sizing choice here.",
        "Motor branch conductors are sized from the table ampacity. 240.4(G) points motor circuits at Article 430, so the 15/20/30 A small-conductor overcurrent caps in 240.4(D) are not what picks the motor wire.",
        "",
        "The termination column follows UL 508A 29.6 when no temperature is marked: 60°C at 100 A or less, 75°C above 100 A. That rule is not verified against the standard. A marked 60°C or 75°C termination overrides it.",
        "",
        "| Size | 60°C | 75°C |",
        "| --- | --- | --- |",
        "| 14 | 15 | 20 |",
        "| 12 | 20 | 25 |",
        "| 10 | 30 | 35 |",
        "| 8 | 40 | 50 |",
        "| 6 | 55 | 65 |",
        "| 4 | 70 | 85 |",
        "| 3 | 85 | 100 |",
        "| 2 | 95 | 115 |",
        "| 1 | 110 | 130 |",
        "| 1/0 | 125 | 150 |",
        "| 2/0 | 145 | 175 |",
        "| 3/0 | 165 | 200 |",
        "| 4/0 | 195 | 230 |",
        "| 250 | 215 | 255 |",
        "| 300 | 240 | 285 |",
        "| 350 | 260 | 310 |",
        "| 400 | 280 | 335 |",
        "| 500 | 320 | 380 |",
        "",
        "On this table alone, 5 hp at 9.5 A is 14 AWG in both columns (15 A at 60°C, 20 A at 75°C).",
        "10 hp at 35 A is **8 AWG** at 60°C (40 A; 10 AWG is 30 A) and **10 AWG** at 75°C (35 A).",
        "The installed conductor is not this column by itself. See “Installed gauge” below.",
        "",
        "## UL 508A Table 28.1 — internal panel power wiring",
        "",
        "UL 508A value, not verified against the standard. No official UL copy is cited.",
        "18 AWG and 16 AWG are **not** encoded on this table.",
        "1/0 and larger have no 60°C value encoded.",
        "The encoded rows run through 1250 kcmil, including 600 kcmil. Those ampacities stay in the module and stay unverified. They are not a new reading of NFPA 70.",
        "UL 508A 29.6, not verified against the standard: if the component does not state a wire temperature, use the 60°C column at 100 A or less and the 75°C column above 100 A.",
        "That rule is the default termination temperature. A marked 60°C or 75°C termination (project `termination_c`, the motor's `termination_c`, or `--temp-c`) overrides it.",
        "",
        "14 AWG is 15 A in both the 60°C and 75°C columns. 10 AWG is 30 A in both. 8 AWG is 40 A / 50 A.",
        "So 35 A on Table 28.1 is **8 AWG** at either column. 10 AWG is 30 A and does not carry 35 A.",
        "",
        "## Installed gauge",
        "",
        "PanelCAD evaluates NEC Table 310.16 and UL 508A Table 28.1 at the termination temperature and installs the larger conductor. The sizing text names which rule governed. Every Table 28.1 ampere in that text is marked unverified.",
        "",
        "10 hp, 230 V, table current 28 A, conductors 35 A, inverse-time device 70 A:",
        "",
        "* Unmarked terminations. 70 A is 100 A or less, so UL 508A 29.6 (not verified against the standard) selects 60°C. Both tables: 10 AWG is 30 A, so **8 AWG** (40 A). Both rules govern. The Table 28.1 half is unverified.",
        "* Terminations marked 75°C. NEC Table 310.16 allows **10 AWG** (35 A). UL 508A Table 28.1 (not verified against the standard) does not: 10 AWG is 30 A, so it requires **8 AWG** (50 A). The larger conductor is installed: **8 AWG**, governed by Table 28.1. 10 AWG is only the NEC column result.",
        "",
        "5 hp, 460 V, conductors 9.5 A, device 20 A: unmarked → 60°C, and both tables allow **14 AWG** (15 A).",
        "",
        "## Table 430.72(B) and UL 508A control conductors",
        "",
        "Not verified against an official NFPA text, and the UL figures are not verified against the standard. Copper columns encoded here:",
        "",
        "| AWG | Column A separate | Column B inside enclosure | Column C outside enclosure |",
        "| --- | --- | --- | --- |",
        "| 18 | 7 | 25 | 7 |",
        "| 16 | 10 | 40 | 10 |",
        "| 14 | Note 1 | 100 | 45 |",
        "| 12 | Note 1 | 120 | 60 |",
        "| 10 | Note 1 | 160 | 90 |",
        "",
        "Note 1 sends 14/12/10 AWG to the ampacity article. 240.4(D) caps those overcurrent devices at 15/20/30 A unless another article applies. The module does not pretend the blank cell itself says 15.",
        "Note 2 (larger than 10 AWG, inside the enclosure) is 400% of the 60°C free-air value in Table 310.17. Note 3 (outside) is 300% of the Table 310.16 60°C value. Those notes are not turned into extra ampere rows.",
        "",
        "UL 508A internal control conductors, not verified against the standard: **18 AWG = 7 A**, **16 AWG = 10 A**.",
        "Those two figures match Column A of the unverified 430.72(B) table. Circuit Builder marks internal control wiring **16 AWG** and reports a separate control device maximum of 10 A.",
        "",
        "## What Circuit Builder stores",
        "",
        "The builtin breaker symbols are 16 A, 32 A, and 100 A frames. The sized trip is written on the device as `In` (for example 20 A or 70 A). Table 430.52 still checks that trip. When the trip is above the placed symbol's frame, design check `breaker_frame_below_rating` (error) names the next larger builtin frame, or says none exists, and names a catalog breaker when one covers the trip. The symbol is not swapped.",
        "The overload stores the required setpoint as `setting`. A real adjustable range is kept when the selected part, or the library symbol, publishes one that includes the setpoint (`amps_min` below `amps_max`). A zero-width range such as `7.6-7.6A` is not written. When no published range includes the setpoint, `overload_range_misses_fla` says so. When a catalog overload covers the setpoint and none is assigned, the device note names that part and the check stays quiet.",
        "Contactors are the builtin 9 A or 32 A frames. Above 9 A the 32 A frame is selected. Above 32 A, design check `contactor_undersized` (error) names a catalog contactor that covers the full-load current when one is published.",
        "",
        "## IEC",
        "",
        "An IEC project stores kW and, when you enter it, the nameplate or user current.",
        "Tables 430.248 and 430.250 are not consulted and no substitute table is filled in.",
        "",
    ]
    return "\n".join(lines) + "\n"
