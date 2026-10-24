"""Patch 3b: Multi-page IEC-to-NEMA circuit consolidation and round-trip tests."""

from __future__ import annotations

from pathlib import Path

from panelcad.api.session import Session
from panelcad.convert.from_to_check import check_connections
from panelcad.convert.netlist import same_netlist
from panelcad.render.titleblock import title_block_rect, working_rect
from tests.test_convert_standard import _convert, _session


def _check_convert(session: Session, target: str, tmp_path: Path, slug: str) -> dict:
    from panelcad.convert.flags import sheet_flags

    out = _convert(session, target, tmp_path, slug)
    check = out["connection_check"]
    assert check["scorer"] == "from-to-check/1"
    assert check["ok"] is True, (check["missing"], check["extra"])
    assert check["nets_before"] == check["nets_after"] > 0
    assert check["missing"] == [], check
    assert check["extra"] == [], check
    for sh in session.project.sheets:
        flags = sheet_flags(session, sh.id)
        assert flags == [], f"Unexpected flags on {slug} sheet {sh.id}: {flags}"
    return out


def _place_offpage_out(session: Session, ref: str, x: float, y: float, link: str, label: str = "") -> object:
    c = session.place("iec.connection.offpage", ref=ref, x=x, y=y, rotation=90)
    c.properties["label"] = label or link
    c.properties["link"] = link
    c.properties["role"] = "to"
    return c


def _place_offpage_in(session: Session, ref: str, x: float, y: float, link: str, label: str = "") -> object:
    c = session.place("iec.connection.offpage_from", ref=ref, x=x, y=y, rotation=90)
    c.properties["label"] = label or link
    c.properties["link"] = link
    c.properties["role"] = "from"
    return c


def build_multipage_conveyor_iec(include_power_sheet: bool = True) -> Session:
    """Build a 3-page IEC conveyor control circuit (plus optional 3-phase power page)."""
    session = _session("iec_conveyor_3page", "IEC")

    if include_power_sheet:
        sp = session.project.sheets[0]
        sp.title = "1 - Main Power"
        sp.drawing_style = "iec"
        session.set_active_sheet(sp.id)
        session.place("iec.breakers.mccb_3p_100a", ref="Q1", x=100, y=55)
        session.place("iec.contactors.ac3_9a", ref="K1", x=100, y=105)
        session.place("iec.protection.overload_9a", ref="F1", x=100, y=150)
        session.place("iec.loads.motor_3ph", ref="M1", x=100, y=200)
        for p_idx, (q_in, q_out, k_in, k_out, f_in, f_out, m_in) in enumerate(
            [
                ("1", "2", "L1", "T1", "1", "2", "U1"),
                ("3", "4", "L2", "T2", "3", "4", "V1"),
                ("5", "6", "L3", "T3", "5", "6", "W1"),
            ],
            start=1,
        ):
            session.wire(f"Q1.{q_out}", f"K1.{k_in}", net=f"L{p_idx}_1")
            session.wire(f"K1.{k_out}", f"F1.{f_in}", net=f"L{p_idx}_2")
            session.wire(f"F1.{f_out}", f"M1.{m_in}", net=f"T{p_idx}")
        s1 = session.add_sheet("s1", title="2 - Safety & Supply")
        s1.drawing_style = "iec"
    else:
        s1 = session.project.sheets[0]
        s1.title = "1 - Safety & Supply"
        s1.drawing_style = "iec"

    # Page 1: Safety & Supply
    session.set_active_sheet(s1.id)
    s1.rail_hot = "L+"
    s1.rail_return = "0V"
    session.place("iec.operators.estop", ref="S1", x=70, y=60, rotation=90)
    session.place("iec.relays.cr", ref="K0", x=70, y=155, rotation=90)
    session.wire("L+", "S1.11", net="L+")
    session.wire("S1.12", "K0.A1", net="N_ESTOP")
    session.wire("K0.A2", "0V", net="0V")

    c_k0_13 = session.place("iec.contactors.aux_no", ref="K0-13", x=150, y=75, rotation=90)
    c_k0_13.properties["parent"] = "K0"
    c_k0_13.properties["terminals"] = "13-14"
    c_k0_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    _place_offpage_out(session, "OP1", 150, 155, link="L+_SAFE", label="/2.1 L+_SAFE")
    session.wire("L+", "K0-13.13", net="L+")
    session.wire("K0-13.14", "OP1.A", net="L+_SAFE")

    # Page 2: Conveyor Drive & Timer
    s2 = session.add_sheet("s2", title="3 - Conveyor Drive")
    s2.drawing_style = "iec"
    session.set_active_sheet(s2.id)
    s2.rail_hot = "L+"
    s2.rail_return = "0V"
    _place_offpage_in(session, "OP2", 70, 35, link="L+_SAFE", label="/1.2 L+_SAFE")
    session.place("iec.operators.pb_nc", ref="S2", x=70, y=65, rotation=90)
    session.place("iec.operators.pb_no", ref="S3", x=70, y=100, rotation=90)
    c_k1_13 = session.place("iec.contactors.aux_no", ref="K1-13", x=105, y=100, rotation=90)
    c_k1_13.properties["parent"] = "K1"
    c_k1_13.properties["terminals"] = "13-14"
    c_k1_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    c_ol = session.place("iec.protection.ol_contact", ref="F1-95", x=70, y=135, rotation=90)
    c_ol.properties["parent"] = "F1"
    c_ol.properties["terminals"] = "95-96"
    session.place("iec.contactors.coil", ref="K1", x=70, y=170, rotation=90)
    session.wire("OP2.A", "S2.11", net="L+_SAFE")
    session.wire("S2.12", "S3.13", net="N_STOP")
    session.wire("S2.12", "K1-13.13", net="N_STOP")
    session.wire("S3.14", "F1-95.95", net="N_START")
    session.wire("K1-13.14", "F1-95.95", net="N_START")
    session.wire("F1-95.96", "K1.A1", net="N_COIL")
    session.wire("K1.A2", "0V", net="0V")

    c_k1_23 = session.place("iec.contactors.aux_no", ref="K1-23", x=175, y=75, rotation=90)
    c_k1_23.properties["parent"] = "K1"
    c_k1_23.properties["terminals"] = "23-24"
    c_k1_23.properties["pin_labels"] = {"13": "23", "14": "24"}
    session.place("iec.timers.on_delay", ref="KT1", x=175, y=160, rotation=90)
    _place_offpage_out(session, "OP3", 230, 160, link="CONV_RUN", label="/3.1 CONV_RUN")
    session.wire("OP2.A", "K1-23.13", net="L+_SAFE")
    session.wire("K1-23.14", "KT1.A1", net="CONV_RUN")
    session.wire("K1-23.14", "OP3.A", net="CONV_RUN")
    session.wire("KT1.A2", "0V", net="0V")

    # Page 3: Status & Startup Horn
    s3 = session.add_sheet("s3", title="4 - Annunciation")
    s3.drawing_style = "iec"
    session.set_active_sheet(s3.id)
    s3.rail_hot = "L+"
    s3.rail_return = "0V"
    _place_offpage_in(session, "OP4", 70, 35, link="CONV_RUN", label="/2.2 CONV_RUN")
    c_kt_nc = session.place("iec.contactors.aux_nc", ref="KT1-15", x=70, y=80, rotation=90)
    c_kt_nc.properties["parent"] = "KT1"
    c_kt_nc.properties["terminals"] = "15-16"
    c_kt_nc.properties["pin_labels"] = {"21": "15", "22": "16"}
    c_kt_nc.properties["delayed"] = "NC"
    session.place("iec.operators.pilot_light", ref="P1", x=70, y=160, rotation=90)
    session.wire("OP4.A", "KT1-15.21", net="CONV_RUN")
    session.wire("KT1-15.22", "P1.X1", net="N_HORN")
    session.wire("P1.X2", "0V", net="0V")

    c_kt_no = session.place("iec.contactors.aux_no", ref="KT1-27", x=160, y=80, rotation=90)
    c_kt_no.properties["parent"] = "KT1"
    c_kt_no.properties["terminals"] = "27-28"
    c_kt_no.properties["pin_labels"] = {"13": "27", "14": "28"}
    c_kt_no.properties["delayed"] = "NO"
    session.place("iec.operators.pilot_light", ref="P2", x=160, y=160, rotation=90)
    session.wire("OP4.A", "KT1-27.13", net="CONV_RUN")
    session.wire("KT1-27.14", "P2.X1", net="N_RUN_LT")
    session.wire("P2.X2", "0V", net="0V")

    return session


def build_multipage_duplex_pump_iec() -> Session:
    """Build a 4-page IEC duplex pump control circuit (6 rungs total across 4 sheets)."""
    session = _session("iec_duplex_pump_4page", "IEC")

    # Page 1
    s1 = session.project.sheets[0]
    s1.title = "1 - Level Sensing"
    s1.drawing_style = "iec"
    s1.rail_hot = "L+"
    s1.rail_return = "0V"
    session.set_active_sheet(s1.id)
    session.place("iec.operators.estop", ref="S1", x=70, y=55, rotation=90)
    session.place("iec.operators.pb_no", ref="S2", x=70, y=100, rotation=90)
    session.place("iec.relays.cr", ref="K1", x=70, y=160, rotation=90)
    _place_offpage_out(session, "OP1", 120, 100, link="L+_PERM", label="/2.1 L+_PERM")
    session.wire("L+", "S1.11", net="L+")
    session.wire("S1.12", "S2.13", net="L+_PERM")
    session.wire("S1.12", "OP1.A", net="L+_PERM")
    session.wire("S2.14", "K1.A1", net="N_LEAD")
    session.wire("K1.A2", "0V", net="0V")

    session.place("iec.operators.pb_no", ref="S3", x=180, y=100, rotation=90)
    session.place("iec.relays.cr", ref="K2", x=180, y=160, rotation=90)
    _place_offpage_out(session, "OP2", 230, 160, link="LAG_CALL", label="/3.1 LAG_CALL")
    session.wire("S1.12", "S3.13", net="L+_PERM")
    session.wire("S3.14", "K2.A1", net="LAG_CALL")
    session.wire("S3.14", "OP2.A", net="LAG_CALL")
    session.wire("K2.A2", "0V", net="0V")

    # Page 2
    s2 = session.add_sheet("s2", title="2 - Pump 1 Starter")
    s2.drawing_style = "iec"
    session.set_active_sheet(s2.id)
    s2.rail_hot = "L+"
    s2.rail_return = "0V"
    _place_offpage_in(session, "OP3", 70, 35, link="L+_PERM", label="/1.1 L+_PERM")
    c_k1_13 = session.place("iec.contactors.aux_no", ref="K1-13", x=70, y=75, rotation=90)
    c_k1_13.properties["parent"] = "K1"
    c_k1_13.properties["terminals"] = "13-14"
    c_k1_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    c_ol1 = session.place("iec.protection.ol_contact", ref="F1-95", x=70, y=120, rotation=90)
    c_ol1.properties["parent"] = "F1"
    c_ol1.properties["terminals"] = "95-96"
    session.place("iec.contactors.coil", ref="KM1", x=70, y=165, rotation=90)
    session.wire("OP3.A", "K1-13.13", net="L+_PERM")
    session.wire("K1-13.14", "F1-95.95", net="N_P1_A")
    session.wire("F1-95.96", "KM1.A1", net="N_P1_B")
    session.wire("KM1.A2", "0V", net="0V")

    # Page 3
    s3 = session.add_sheet("s3", title="3 - Pump 2 Starter")
    s3.drawing_style = "iec"
    session.set_active_sheet(s3.id)
    s3.rail_hot = "L+"
    s3.rail_return = "0V"
    _place_offpage_in(session, "OP4", 70, 35, link="LAG_CALL", label="/1.2 LAG_CALL")
    c_k2_13 = session.place("iec.contactors.aux_no", ref="K2-13", x=70, y=80, rotation=90)
    c_k2_13.properties["parent"] = "K2"
    c_k2_13.properties["terminals"] = "13-14"
    c_k2_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    c_ol2 = session.place("iec.protection.ol_contact", ref="F2-95", x=70, y=125, rotation=90)
    c_ol2.properties["parent"] = "F2"
    c_ol2.properties["terminals"] = "95-96"
    session.place("iec.contactors.coil", ref="KM2", x=70, y=165, rotation=90)
    session.wire("OP4.A", "K2-13.13", net="LAG_CALL")
    session.wire("K2-13.14", "F2-95.95", net="N_P2_A")
    session.wire("F2-95.96", "KM2.A1", net="N_P2_B")
    session.wire("KM2.A2", "0V", net="0V")

    _place_offpage_in(session, "OP5", 165, 35, link="L+_PERM", label="/1.1 L+_PERM")
    c_km1_13 = session.place("iec.contactors.aux_no", ref="KM1-13", x=165, y=80, rotation=90)
    c_km1_13.properties["parent"] = "KM1"
    c_km1_13.properties["terminals"] = "13-14"
    c_km1_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    c_km2_13 = session.place("iec.contactors.aux_no", ref="KM2-13", x=165, y=125, rotation=90)
    c_km2_13.properties["parent"] = "KM2"
    c_km2_13.properties["terminals"] = "13-14"
    c_km2_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    session.place("iec.relays.cr", ref="K3", x=165, y=165, rotation=90)
    session.wire("OP5.A", "KM1-13.13", net="L+_PERM")
    session.wire("KM1-13.14", "KM2-13.13", net="N_BOTH_A")
    session.wire("KM2-13.14", "K3.A1", net="N_BOTH_B")
    session.wire("K3.A2", "0V", net="0V")

    # Page 4
    s4 = session.add_sheet("s4", title="4 - High Level Alarm")
    s4.drawing_style = "iec"
    session.set_active_sheet(s4.id)
    s4.rail_hot = "L+"
    s4.rail_return = "0V"
    _place_offpage_in(session, "OP6", 70, 35, link="LAG_CALL", label="/1.2 LAG_CALL")
    c_k3_13 = session.place("iec.contactors.aux_no", ref="K3-13", x=70, y=90, rotation=90)
    c_k3_13.properties["parent"] = "K3"
    c_k3_13.properties["terminals"] = "13-14"
    c_k3_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    session.place("iec.operators.pilot_light", ref="P1", x=70, y=160, rotation=90)
    session.wire("OP6.A", "K3-13.13", net="LAG_CALL")
    session.wire("K3-13.14", "P1.X1", net="N_ALARM")
    session.wire("P1.X2", "0V", net="0V")

    return session


def build_multipage_reversing_hoist_iec() -> Session:
    """Build a 3-page IEC reversing hoist control circuit (6 rungs across 3 sheets)."""
    session = _session("iec_reversing_hoist_3page", "IEC")

    # Page 1
    s1 = session.project.sheets[0]
    s1.title = "1 - Hoist Permissive"
    s1.drawing_style = "iec"
    s1.rail_hot = "L+"
    s1.rail_return = "0V"
    session.set_active_sheet(s1.id)
    session.place("iec.operators.estop", ref="S1", x=70, y=60, rotation=90)
    c_ol = session.place("iec.protection.ol_contact", ref="F1-95", x=70, y=105, rotation=90)
    c_ol.properties["parent"] = "F1"
    c_ol.properties["terminals"] = "95-96"
    session.place("iec.relays.cr", ref="K0", x=70, y=160, rotation=90)
    session.wire("L+", "S1.11", net="L+")
    session.wire("S1.12", "F1-95.95", net="N_ES")
    session.wire("F1-95.96", "K0.A1", net="N_PERM_COIL")
    session.wire("K0.A2", "0V", net="0V")

    c_k0_13 = session.place("iec.contactors.aux_no", ref="K0-13", x=150, y=80, rotation=90)
    c_k0_13.properties["parent"] = "K0"
    c_k0_13.properties["terminals"] = "13-14"
    c_k0_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    _place_offpage_out(session, "OP1", 150, 160, link="HOIST_PERM", label="/2.1 HOIST_PERM")
    session.wire("L+", "K0-13.13", net="L+")
    session.wire("K0-13.14", "OP1.A", net="HOIST_PERM")

    # Page 2
    s2 = session.add_sheet("s2", title="2 - Up/Down Contactors")
    s2.drawing_style = "iec"
    session.set_active_sheet(s2.id)
    s2.rail_hot = "L+"
    s2.rail_return = "0V"
    _place_offpage_in(session, "OP2", 70, 35, link="HOIST_PERM", label="/1.2 HOIST_PERM")
    session.place("iec.operators.pb_no", ref="S2", x=70, y=65, rotation=90)
    session.place("iec.operators.pb_nc", ref="S4", x=70, y=100, rotation=90)
    c_k2_21 = session.place("iec.contactors.aux_nc", ref="K2-21", x=70, y=135, rotation=90)
    c_k2_21.properties["parent"] = "K2"
    c_k2_21.properties["terminals"] = "21-22"
    c_k2_21.properties["pin_labels"] = {"21": "21", "22": "22"}
    session.place("iec.contactors.coil", ref="K1", x=70, y=170, rotation=90)
    session.wire("OP2.A", "S2.13", net="HOIST_PERM")
    session.wire("S2.14", "S4.11", net="N_UP_1")
    session.wire("S4.12", "K2-21.21", net="N_UP_2")
    session.wire("K2-21.22", "K1.A1", net="N_UP_3")
    session.wire("K1.A2", "0V", net="0V")

    session.place("iec.operators.pb_no", ref="S3", x=170, y=65, rotation=90)
    session.place("iec.operators.pb_nc", ref="S5", x=170, y=100, rotation=90)
    c_k1_21 = session.place("iec.contactors.aux_nc", ref="K1-21", x=170, y=135, rotation=90)
    c_k1_21.properties["parent"] = "K1"
    c_k1_21.properties["terminals"] = "21-22"
    c_k1_21.properties["pin_labels"] = {"21": "21", "22": "22"}
    session.place("iec.contactors.coil", ref="K2", x=170, y=170, rotation=90)
    session.wire("OP2.A", "S3.13", net="HOIST_PERM")
    session.wire("S3.14", "S5.11", net="N_DN_1")
    session.wire("S5.12", "K1-21.21", net="N_DN_2")
    session.wire("K1-21.22", "K2.A1", net="N_DN_3")
    session.wire("K2.A2", "0V", net="0V")

    # Page 3
    s3 = session.add_sheet("s3", title="3 - Brake & Status")
    s3.drawing_style = "iec"
    session.set_active_sheet(s3.id)
    s3.rail_hot = "L+"
    s3.rail_return = "0V"
    _place_offpage_in(session, "OP3", 70, 35, link="HOIST_PERM", label="/1.2 HOIST_PERM")
    c_k1_13 = session.place("iec.contactors.aux_no", ref="K1-13", x=70, y=85, rotation=90)
    c_k1_13.properties["parent"] = "K1"
    c_k1_13.properties["terminals"] = "13-14"
    c_k1_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    c_k2_13 = session.place("iec.contactors.aux_no", ref="K2-13", x=110, y=85, rotation=90)
    c_k2_13.properties["parent"] = "K2"
    c_k2_13.properties["terminals"] = "13-14"
    c_k2_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    session.place("iec.relays.cr", ref="K3", x=70, y=160, rotation=90)
    session.wire("OP3.A", "K1-13.13", net="HOIST_PERM")
    session.wire("OP3.A", "K2-13.13", net="HOIST_PERM")
    session.wire("K1-13.14", "K3.A1", net="N_BRK")
    session.wire("K2-13.14", "K3.A1", net="N_BRK")
    session.wire("K3.A2", "0V", net="0V")

    c_k3_13 = session.place("iec.contactors.aux_no", ref="K3-13", x=175, y=85, rotation=90)
    c_k3_13.properties["parent"] = "K3"
    c_k3_13.properties["terminals"] = "13-14"
    c_k3_13.properties["pin_labels"] = {"13": "13", "14": "14"}
    session.place("iec.operators.pilot_light", ref="P1", x=175, y=160, rotation=90)
    session.wire("OP3.A", "K3-13.13", net="HOIST_PERM")
    session.wire("K3-13.14", "P1.X1", net="N_BRK_LT")
    session.wire("P1.X2", "0V", net="0V")

    return session


def test_multipage_conveyor_consolidates_control_sheets_and_roundtrips(tmp_path: Path):
    """3 IEC control pages + 1 power page -> 1 power page + 1 consolidated NEMA ladder page -> roundtrip back to 4 IEC pages."""
    session = build_multipage_conveyor_iec(include_power_sheet=True)
    assert len(session.project.sheets) == 4
    before_snap = session.project.model_copy(deep=True)

    out_nema = _check_convert(session, "nema", tmp_path, "conveyor-nema")

    # 1 power sheet + 1 consolidated control ladder sheet = 2 sheets total
    assert len(session.project.sheets) == 2
    pwr_sheet, ctrl_sheet = session.project.sheets[0], session.project.sheets[1]
    assert pwr_sheet.title == "1 - Main Power"
    assert ctrl_sheet.drawing_style == "ladder"
    assert (ctrl_sheet.rail_hot, ctrl_sheet.rail_return) == ("24V", "0V")

    # All intermediate off-page connectors on the consolidated control sheet were eliminated
    offpages = [c for c in ctrl_sheet.components if "offpage" in c.library_id.lower()]
    assert offpages == []

    # All 5 control outputs are placed horizontally on the single NEMA ladder sheet above the title block
    wx, wy, ww, wh = working_rect(ctrl_sheet)
    _tx, ty, _tw, _th = title_block_rect(ctrl_sheet)
    assert all(c.rotation == 0 for c in ctrl_sheet.components)
    for c in ctrl_sheet.components:
        assert wy <= c.y < ty - 5.0, f"{c.id} at y={c.y} overlaps title block y={ty}"

    # Verify From-To netlist equivalence across the multi-page consolidation
    assert out_nema["connection_check"]["ok"] is True
    assert out_nema["connection_check"]["nets_before"] == out_nema["connection_check"]["nets_after"]

    # Round-trip back to IEC restores all 4 original sheets and off-page connectors
    _check_convert(session, "iec", tmp_path, "conveyor-iec-rt")
    assert len(session.project.sheets) == 4
    for s_before, s_after in zip(before_snap.sheets, session.project.sheets):
        assert s_after.id == s_before.id
        assert s_after.title == s_before.title
        assert {c.id: c.library_id for c in s_after.components} == {
            c.id: c.library_id for c in s_before.components
        }


def test_multipage_duplex_pump_4pages_consolidates_to_1_nema_sheet(tmp_path: Path):
    """4-page IEC duplex pump control circuit consolidates onto 1 NEMA ladder sheet and round-trips."""
    session = build_multipage_duplex_pump_iec()
    assert len(session.project.sheets) == 4
    before_snap = session.project.model_copy(deep=True)

    _check_convert(session, "nema", tmp_path, "duplex-pump-nema")

    assert len(session.project.sheets) == 1
    ctrl = session.project.sheets[0]
    assert ctrl.drawing_style == "ladder"
    assert [c for c in ctrl.components if "offpage" in c.library_id.lower()] == []

    # All 6 outputs (CR1, CR2, M1, M2, CR3, LT1) are on the single sheet in increasing Y order
    _tx, ty, _tw, _th = title_block_rect(ctrl)
    assert all(c.rotation == 0 for c in ctrl.components)
    assert all(c.y < ty - 5.0 for c in ctrl.components)

    # Round-trip back to IEC restores all 4 sheets
    _check_convert(session, "iec", tmp_path, "duplex-pump-iec-rt")
    assert len(session.project.sheets) == 4
    for s_before, s_after in zip(before_snap.sheets, session.project.sheets):
        assert s_after.id == s_before.id
        assert {c.id: c.library_id for c in s_after.components} == {
            c.id: c.library_id for c in s_before.components
        }


def test_multipage_reversing_hoist_3pages_consolidates_to_1_nema_sheet(tmp_path: Path):
    """3-page IEC reversing hoist circuit with limit switches and parallel brake contacts consolidates onto 1 NEMA sheet."""
    session = build_multipage_reversing_hoist_iec()
    assert len(session.project.sheets) == 3
    before_snap = session.project.model_copy(deep=True)

    _check_convert(session, "nema", tmp_path, "hoist-nema")

    assert len(session.project.sheets) == 1
    ctrl = session.project.sheets[0]
    assert ctrl.drawing_style == "ladder"
    assert [c for c in ctrl.components if "offpage" in c.library_id.lower()] == []
    _tx, ty, _tw, _th = title_block_rect(ctrl)
    assert all(c.rotation == 0 for c in ctrl.components)
    assert all(c.y < ty - 5.0 for c in ctrl.components)

    # Round-trip back to IEC restores all 3 sheets
    _check_convert(session, "iec", tmp_path, "hoist-iec-rt")
    assert len(session.project.sheets) == 3
    for s_before, s_after in zip(before_snap.sheets, session.project.sheets):
        assert s_after.id == s_before.id
        assert {c.id: c.library_id for c in s_after.components} == {
            c.id: c.library_id for c in s_before.components
        }


def test_multipage_overflow_spills_to_continuation_sheet_above_6_rungs(tmp_path: Path):
    """When 4 IEC control pages have 8 rungs total (> 6 max per sheet), NEMA consolidates to 2 ladder sheets."""
    session = build_multipage_duplex_pump_iec()
    # Add 2 more columns on Page 4 so total outputs = 8 (6 on sheet 1, 2 on continuation sheet 2)
    s4 = session.project.sheets[3]
    session.set_active_sheet(s4.id)
    c_km1_23 = session.place("iec.contactors.aux_no", ref="KM1-23", x=145, y=90, rotation=90)
    c_km1_23.properties["parent"] = "KM1"
    c_km1_23.properties["terminals"] = "23-24"
    c_km1_23.properties["pin_labels"] = {"13": "23", "14": "24"}
    session.place("iec.operators.pilot_light", ref="P2", x=145, y=160, rotation=90)
    session.wire("OP6.A", "KM1-23.13", net="LAG_CALL")
    session.wire("KM1-23.14", "P2.X1", net="N_P1_RUN")
    session.wire("P2.X2", "0V", net="0V")

    c_km2_23 = session.place("iec.contactors.aux_no", ref="KM2-23", x=220, y=90, rotation=90)
    c_km2_23.properties["parent"] = "KM2"
    c_km2_23.properties["terminals"] = "23-24"
    c_km2_23.properties["pin_labels"] = {"13": "23", "14": "24"}
    session.place("iec.operators.pilot_light", ref="P3", x=220, y=160, rotation=90)
    session.wire("OP6.A", "KM2-23.13", net="LAG_CALL")
    session.wire("KM2-23.14", "P3.X1", net="N_P2_RUN")
    session.wire("P3.X2", "0V", net="0V")

    before_snap = session.project.model_copy(deep=True)
    _check_convert(session, "nema", tmp_path, "overflow-nema")

    # 8 rungs across 4 IEC sheets -> 2 NEMA ladder sheets (6 rungs + 2 rungs)
    assert len(session.project.sheets) == 2
    assert all(s.drawing_style == "ladder" for s in session.project.sheets)

    # Round-trip back to IEC restores all 4 sheets
    _check_convert(session, "iec", tmp_path, "overflow-iec-rt")
    assert len(session.project.sheets) == 4
    for s_before, s_after in zip(before_snap.sheets, session.project.sheets):
        assert s_after.id == s_before.id
        assert {c.id: c.library_id for c in s_after.components} == {
            c.id: c.library_id for c in s_before.components
        }


def test_power_sheet_not_relayouted_onto_right_rail_and_flags_catch_mess(tmp_path: Path):
    """Regression test: 3-phase power sheet must not be treated as a control ladder or pushed to the right rail, and sheet_flags must catch component/rail/wire overlaps."""
    from panelcad.convert.flags import sheet_flags
    from panelcad.core.ladder import is_ladder_sheet

    session = build_multipage_conveyor_iec(include_power_sheet=True)
    _check_convert(session, "nema", tmp_path, "conveyor-power-check")

    pwr_sheet = session.sheet("main")
    assert not is_ladder_sheet(pwr_sheet, session.project)
    pwr_xs = {c.id: float(c.x) for c in pwr_sheet.components}
    assert set(pwr_xs) == {"CB1", "M1", "OL1", "MTR1"}
    # All 4 3-phase power devices remain aligned on the feeder column (~100 mm), never pushed to right rail (~365 mm)
    for cid, cx in pwr_xs.items():
        assert 85.0 <= cx <= 125.0, f"{cid} was pushed off the feeder column to x={cx}"
    assert sheet_flags(session, "main") == []

    # Now simulate the pre-fix right-rail mess on a copy and verify sheet_flags catches all 3 failure modes
    broken = Session(session.project.model_copy(deep=True))
    b_main = broken.sheet("main")
    b_main.drawing_style = "ladder"
    b_main.rail_hot = "L1"
    b_main.rail_return = "L2"
    for comp in b_main.components:
        if comp.id in {"M1", "MTR1"}:
            comp.x = 359.0
    b_main.components[1].y = 35.0  # M1
    b_main.components[3].y = 85.0  # MTR1 overlapping M1 and right rail
    broken_flags = {f["flag"] for f in sheet_flags(broken, "main")}
    assert "component_overlap" in broken_flags, broken_flags
    assert "rail_collision" in broken_flags, broken_flags
    assert "wire_overlap" in broken_flags, broken_flags

