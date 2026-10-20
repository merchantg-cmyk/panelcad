"""Control vs power. A coil pin is not a motor lead."""

from __future__ import annotations

MOTOR_POWER_PINS = frozenset({"T1", "T2", "T3", "T4", "T5", "T6", "U1", "U2", "V1", "V2", "W1", "W2"})
COIL_PINS = frozenset({"A1", "A2"})
LADDER_RAILS = frozenset(
    {
        "L1",
        "L2",
        "L",
        "N",
        "0V",
        "0VDC",
        "24V",
        "24VDC",
        "24VAC",
        "120V",
        "120VAC",
        "COM",
        "NEUTRAL",
    }
)


def _lib(library_id: str) -> str:
    return str(library_id or "").lower()


def is_motor_library(library_id: str) -> bool:
    return "motor" in _lib(library_id)


def is_motor_power_pin(library_id: str, pin: str) -> bool:
    return is_motor_library(library_id) and str(pin or "").upper() in MOTOR_POWER_PINS


def is_coil_control_pin(library_id: str, pin: str) -> bool:
    if str(pin or "").upper() not in COIL_PINS:
        return False
    lib = _lib(library_id)
    return any(token in lib for token in ("contactor", "coil", "relay", "timer"))


def is_operator_library(library_id: str) -> bool:
    lib = _lib(library_id)
    return "operators" in lib or "ol_contact" in lib


def control_power_conflict(
    from_lib: str, from_pin: str, to_lib: str, to_pin: str
) -> str | None:
    """Error text if a control pin is landed on a 3-phase motor power lead."""

    a_motor = is_motor_power_pin(from_lib, from_pin)
    b_motor = is_motor_power_pin(to_lib, to_pin)
    a_ctrl = is_coil_control_pin(from_lib, from_pin) or is_operator_library(from_lib)
    b_ctrl = is_coil_control_pin(to_lib, to_pin) or is_operator_library(to_lib)
    if (a_motor and b_ctrl) or (b_motor and a_ctrl):
        return (
            "control circuit cannot land on a motor power lead (T1/T2/T3). "
            "Wire coil A1/A2 and pushbuttons on the control rung; "
            "land the motor from contactor power poles, not from the coil."
        )
    return None


def _head(raw: str) -> str:
    text = str(raw or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return "".join(ch for ch in text.upper() if ch.isalnum())


def is_ladder_rail(raw: str) -> bool:
    """A bare rail word. ``L1.X2`` is a device pin, not the L1 rail."""

    text = str(raw or "").strip()
    if not text or "." in text:
        return False
    return _head(text) in LADDER_RAILS


def ladder_motor_on_rails(
    from_raw: str,
    to_raw: str,
    from_lib: str,
    to_lib: str,
) -> str | None:
    """Ladder L1/L2 is control. Motor/power does not sit on those rails."""

    a_mot = is_motor_library(from_lib)
    b_mot = is_motor_library(to_lib)
    a_rail = is_ladder_rail(from_raw)
    b_rail = is_ladder_rail(to_raw)
    if (a_mot and b_rail) or (b_mot and a_rail):
        return (
            "Motor is power, not a ladder rung. Do not land it on L1/L2. "
            "Keep the motor off the rails."
        )
    a_prot = "fuse" in _lib(from_lib) or "breaker" in _lib(from_lib)
    b_prot = "fuse" in _lib(to_lib) or "breaker" in _lib(to_lib)
    if (a_mot and b_prot) or (b_mot and a_prot):
        return (
            "Motor is power, not a ladder rung. Do not feed it from the control fuse."
        )
    return None
