"""Motor full-load current from NEC Table 430.250.

The ampere cells live in ``motor_sizing`` so Circuit Builder and the sizing
command share one copy. This module keeps the historical ``lookup_fla``
helper: a kW that is not an exact table row still snaps to the nearest
published horsepower. Voltages select a table column. They are not scaled.
"""

from __future__ import annotations

from panelcad.core.motor_sizing import HP_TO_KW, motor_flc, nearest_table_hp

# Star / delta winding current is line FLA / √3.
DELTA_OL_FACTOR = 0.58


def lookup_fla(
    *,
    hp: float | None = None,
    kw: float | None = None,
    voltage: float | None = None,
) -> float | None:
    """Table 430.250 full-load current, or None when the cell is blank.

    ``voltage`` omitted uses the 460 V column. 480 V uses that same column
    with no ratio. 400 V is not a column and returns None.
    """

    if hp is None and kw is None:
        return None
    hp_v = float(hp) if hp is not None else float(kw) / HP_TO_KW
    if hp_v <= 0:
        return None
    volts = 460.0 if voltage is None or float(voltage) <= 0 else float(voltage)
    looked = motor_flc(hp=hp_v, voltage=volts, phases=3)
    if looked is None:
        neighbor = nearest_table_hp(hp_v, phases=3)
        if neighbor is None:
            return None
        looked = motor_flc(hp=neighbor, voltage=volts, phases=3)
    if looked is None:
        return None
    return float(looked[2])


def delta_ol_setpoint(line_fla: float) -> float:
    """Overload in the delta windings is set to 0.58 × line FLA."""

    return round(float(line_fla) * DELTA_OL_FACTOR, 2)
