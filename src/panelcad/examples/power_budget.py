"""PLC HOA sample and a deliberately overloaded 24 V supply.

Published loads cite the part library. The rest are estimates.
"""

from __future__ import annotations

from panelcad.api.session import Session


def build_plc_hoa() -> Session:
    """NEMA PLC motor circuit plus HMI, sensor, fan, and a DI module on 24 V."""

    session = Session.create("PLC HOA power budget")
    result = session.insert_circuit(
        kind="plc_motor",
        control="local",
        hoa=True,
        pilot_lights=True,
    )
    roles = {row["role"]: row for row in result["components"]}
    control = str(result["control_sheet"])
    session.assign_part(roles["psu"]["id"], "1606-XLE120E")
    session.assign_part(roles["plc"]["id"], "C2-03CPU")
    session.assign_part(roles["pilot_run"]["id"], "800FD-P3N3R")
    # Below the ladder, clear of the supply and the title block.
    session.place("iec.plc.di16", ref="DI1", x=18.0, y=128.0, sheet_id=control)
    session.place("iec.hmi.panel_7in", ref="HMI1", x=88.0, y=196.0, sheet_id=control)
    session.place("iec.sensors.prox_24v", ref="PX1", x=168.0, y=208.0, sheet_id=control)
    session.place("iec.loads.fan_24v", ref="FAN1", x=214.0, y=202.0, sheet_id=control)
    session.assign_part("DI1", "C0-16ND3")
    for ref in ("DI1", "HMI1", "PX1", "FAN1"):
        session.wire(f"{ref}.Lplus", "24V", net="24V", sheet_id=control)
        session.wire(f"{ref}.M", "0V", net="0V", sheet_id=control)
    return session


def build_overloaded() -> Session:
    """5 A / 120 W supply, 1 A output breaker, two 50 W estimated loads."""

    session = Session.create("Overloaded PSU")
    session.place("nema.power.psu_24v_5a", ref="PS1", x=40.0, y=40.0)
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=110.0, y=30.0)
    session.place("iec.loads.fan_24v", ref="FAN1", x=180.0, y=24.0)
    session.place("iec.hmi.panel_7in", ref="HMI1", x=180.0, y=80.0)
    session.wire("PS1.plus", "Q1.1", net="24V")
    session.wire("Q1.2", "FAN1.Lplus", net="24V")
    session.wire("Q1.2", "HMI1.Lplus", net="24V")
    session.wire("PS1.minus", "FAN1.M", net="0V")
    session.wire("PS1.minus", "HMI1.M", net="0V")
    session.assign_part("PS1", "1606-XLE120E")
    session.assign_part("Q1", "1489-M1C010")
    session.set_power_estimate("FAN1", power_w=50.0)
    session.set_power_estimate("HMI1", power_w=50.0)
    return session
