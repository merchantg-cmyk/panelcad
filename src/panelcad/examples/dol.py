"""Direct-on-line motor starter — multi-sheet panel set (power, control, I/O)."""

from __future__ import annotations

from pathlib import Path

from panelcad.api.session import Session

# Published W×H SKUs. Never confirm a GENERIC_BY_CATEGORY table fill.
_DOL_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("Q1", "FAZ-C6-3-NA-L", "AutomationDirect"),
    ("K1", "GH15BN-3-10A", "AutomationDirect"),
    ("F1", "MT-32/3K7.5", "LS Electric"),
    ("G1", "PSP24-120S", "AutomationDirect"),
    ("A1", "C0-00DD1-D", "AutomationDirect"),
    ("H1", "GCX1232-24L", "AutomationDirect"),
)


def _assign_dol_catalog(session: Session) -> None:
    """Attach real catalog footprints. Parts without a published body stay unplaced."""

    for ref, catalog, manufacturer in _DOL_CATALOG:
        try:
            part = session.parts.get(catalog, manufacturer)
        except KeyError:
            continue
        if not part.has_footprint():
            continue
        try:
            session.assign_part(ref, catalog, manufacturer=manufacturer)
        except KeyError:
            continue
        if part.mounting == "plug_on":
            # Published outline still occupies DIN width next to the contactor.
            session.set_panel(ref, mount="din_rail", din_rail=True)


def build_dol(name: str = "DOL Motor Starter", *, confirm_envelopes: bool = True) -> Session:
    s = Session.create(name, sheet_id="power", title="Power")
    s.set_title_block(
        company="Example Controls",
        address="12 Industrial Way",
        customer="Riverside Pump Station",
        location="MCC-1 / Pump 1",
        drawing_number="E-001",
        drawn_by="PanelCAD",
        scale="NTS",
        standard="IEC",
        rev="A",
        date="2026-08-19",
        rev_description="Initial issue",
        description="Motor power circuit",
    )

    s.place("iec.breakers.mcb_3p_32a", ref="Q1", x=40, y=22)
    s.place("iec.contactors.ac3_9a", ref="K1", x=38, y=82)
    s.place("iec.protection.overload_9a", ref="F1", x=40, y=155)
    s.place("iec.loads.motor_3ph", ref="M1", x=42, y=210)
    s.wire("Q1.2", "K1.L1", net="L1")
    s.wire("Q1.4", "K1.L2", net="L2")
    s.wire("Q1.6", "K1.L3", net="L3")
    s.wire("K1.T1", "F1.1", net="T1")
    s.wire("K1.T2", "F1.3", net="T2")
    s.wire("K1.T3", "F1.5", net="T3")
    s.wire("F1.2", "M1.U1", net="U1")
    s.wire("F1.4", "M1.V1", net="V1")
    s.wire("F1.6", "M1.W1", net="W1")

    s.add_sheet("control", title="Control", kind="schematic")
    s.sheet().description = "24 VDC control, PLC and operator devices"
    s.place("iec.power.psu_24v_5a", ref="G1", x=40, y=24)
    s.place("iec.operators.estop", ref="S0", x=120, y=30)
    s.place("iec.operators.pb_nc", ref="S2", x=160, y=30)
    s.place("iec.operators.pb_no", ref="S1", x=200, y=30)
    s.place("iec.operators.pilot_light", ref="H1", x=250, y=30)
    s.place("iec.plc.compact_16dio", ref="A1", x=120, y=100)
    s.place("iec.hmi.panel_7in", ref="P1", x=250, y=110)
    s.wire("G1.plus", "S0.11", net="24V")
    s.wire("S0.12", "S2.11", net="ES")
    s.wire("S2.12", "A1.I0_0", net="STOP")
    s.wire("S1.14", "A1.I0_1", net="START")
    s.wire("G1.minus", "A1.M", net="0V")
    s.wire("A1.Q0_1", "H1.X1", net="RUN_LT")
    s.wire("H1.X2", "G1.minus", net="0V")

    s.add_sheet("io", title="PLC I/O and VFD", kind="io")
    s.sheet().description = "CLICK stackable I/O and DURApulse GS20 — ladder rungs"
    s.place("adc.click.c2_03cpu", ref="A2", x=40, y=30)
    s.place("adc.click.c0_16nd3", ref="A3", x=150, y=24)
    s.place("adc.click.c0_08td1", ref="A4", x=230, y=28)
    s.place("adc.durapulse.gs21_20p5", ref="U1", x=330, y=40)
    s.wire("A2.Lplus", "A3.C0", net="24V")
    s.wire("A3.X0", "A2.X0", net="X0")
    s.wire("A2.Y0", "A4.Y0", net="Y0")
    s.wire("A4.Y0", "U1.DI1", net="RUN")
    s.wire("U1.DCM", "A2.M", net="0V")
    s.layout_io()

    s.add_sheet("enclosure", title="Enclosure", kind="panel_layout")
    s.sheet().description = "DIN-rail backplate, millimetres"
    s.set_enclosure(name="400×500×200", depth_mm=200)
    if confirm_envelopes:
        _assign_dol_catalog(s)
        # Layout keeps the enclosure already on the sheet. Size a catalog box
        # first so the pack is not left in the empty page outline.
        recommendation = s.recommend_enclosure(sheet_id="enclosure")
        if (recommendation.get("enclosure") or {}).get("width_mm"):
            s.apply_enclosure(
                sheet_id="enclosure",
                place_parts=False,
                recommendation=recommendation,
            )
    s.layout_from_schematic()

    s.add_sheet("ethernet", title="Ethernet", kind="network")
    s.set_network(name="Plant LAN", medium="ethernet", protocol="Modbus TCP")
    s.layout_network(medium="ethernet")
    if any(c.id == "A2" for c in s.sheet().components) and any(c.id == "P1" for c in s.sheet().components):
        s.wire("A2.ETH", "P1.ETH", net="LAN")
    if any(c.id == "A1" for c in s.sheet().components) and any(c.id == "P1" for c in s.sheet().components):
        s.wire("A1.ETH", "P1.ETH", net="HMI")

    s.add_sheet("serial", title="RS-485", kind="network")
    s.set_network(name="Modbus RTU", medium="serial", protocol="Modbus RTU")
    s.layout_network(medium="serial")
    if any(c.id == "A2" for c in s.sheet().components) and any(c.id == "U1" for c in s.sheet().components):
        s.wire("A2.COM2", "U1.COM1", net="MB-1")

    s.set_active_sheet("power")
    s.sheet().description = "Motor power circuit"
    return s


def write_dol(path: Path | str) -> Path:
    s = build_dol()
    return s.save(path)
