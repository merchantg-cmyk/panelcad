"""One device split across sheets, and terminal tags that are not strip terminals."""

from panelcad import Session
from panelcad.core.models import Wire
from panelcad.examples.xref_demo import build_nema_seal_in


def _checks(session: Session, name: str) -> list[dict[str, str]]:
    return [row for row in session.design_checks() if row["check"] == name]


def test_coil_and_power_poles_share_one_tag():
    session = Session.create("Split", sheet_id="power", title="Power")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder")
    session.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40)
    session.add_sheet("control", kind="schematic", title="Control")
    session.place("nema.contactors.coil", ref="M1", x=80, y=40, sheet_id="control")
    session.wire("M1.A1", "M1.A2", net="120VAC", sheet_id="control")
    for index, y in enumerate((80, 120, 160, 200)):
        session.place(
            "nema.contactors.aux_no",
            ref=f"M1-{index + 1}",
            x=40,
            y=y,
            sheet_id="control",
        )

    assert _checks(session, "duplicate_device_tag") == []
    unwired = _checks(session, "unwired_coil")
    assert not any(row["ref"] == "M1" for row in unwired)
    assert _checks(session, "contact_count_exceeded") == []

    rows = {(row["sheet"], row["child"]): row for row in session.cross_reference_rows()}
    coil = rows[("control", "M1")]
    poles = rows[("power", "M1")]
    assert coil["role"] == "coil"
    assert coil["location"] != poles["location"]
    assert poles["location"] in coil["xref"]
    for index in range(1, 5):
        contact = rows[("control", f"M1-{index}")]
        assert contact["xref"] == coil["location"]
        assert contact["xref"] != poles["location"]


def test_unwired_split_coil_is_the_ladder_symbol():
    session = Session.create("Open split", sheet_id="power", title="Power")
    session.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40)
    session.add_sheet("control", kind="schematic", title="Control")
    session.place("nema.contactors.coil", ref="M1", x=80, y=40, sheet_id="control")
    unwired = _checks(session, "unwired_coil")
    assert any(row["sheet"] == "control" and row["ref"] == "M1" for row in unwired)
    assert not any(row["sheet"] == "power" for row in unwired)


def test_two_power_pole_symbols_are_still_a_duplicate_tag():
    session = Session.create("Two poles", sheet_id="one")
    session.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40)
    session.add_sheet("two", kind="schematic")
    session.place("nema.contactors.ac3_9a", ref="M1", x=40, y=40, sheet_id="two")
    assert _checks(session, "duplicate_device_tag")


def test_power_poles_without_a_coil_symbol_still_need_a1_a2():
    session = Session.create("Poles only")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    assert any(row["ref"] == "K1" for row in _checks(session, "unwired_coil"))


def test_split_iec_contactor_uses_the_power_symbol_contact_count():
    over = Session.create("IEC over", sheet_id="power")
    over.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    over.add_sheet("control", kind="schematic")
    over.place("iec.contactors.coil", ref="K1", x=40, y=40, sheet_id="control")
    over.place("iec.contactors.aux_no", ref="K1-13", x=40, y=80, sheet_id="control")
    over.place("iec.contactors.aux_nc", ref="K1-21", x=40, y=120, sheet_id="control")
    assert _checks(over, "duplicate_device_tag") == []
    exceeded = _checks(over, "contact_count_exceeded")
    assert exceeded
    assert exceeded[0]["message"].startswith("control:K1 uses 2 contacts; library allows 1")

    fit = Session.create("IEC fit", sheet_id="power")
    fit.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    fit.add_sheet("control", kind="schematic")
    fit.place("iec.contactors.coil", ref="K1", x=40, y=40, sheet_id="control")
    fit.place("iec.contactors.aux_no", ref="K1-13", x=40, y=80, sheet_id="control")
    assert _checks(fit, "contact_count_exceeded") == []


def test_device_contact_tags_are_not_missing_terminals():
    session = Session.create("Contacts", sheet_id="control")
    session.place("nema.contactors.coil", ref="M1", x=40, y=40)
    session.place("nema.contactors.aux_no", ref="M1-2", x=40, y=80)
    session.place("nema.contactors.coil", ref="CR1", x=120, y=40)
    session.place("nema.contactors.aux_no", ref="CR1-13", x=120, y=80)
    session.place("nema.timers.on_delay", ref="TR1", x=200, y=40)
    session.place("nema.contactors.aux_nc", ref="TR1-1", x=200, y=80)
    session.wire("M1-2.3", "CR1-13.3")
    session.wire("CR1-13.4", "TR1-1.1")
    session.wire("TR1-1.2", "M1.A1")
    session.sheet().wires.append(
        Wire.model_validate({"id": "wmiss", "from": "M1.A2", "to": "M1-9.4"})
    )
    session.sheet().wires.append(
        Wire.model_validate({"id": "wterm", "from": "M1.A2", "to": "X9-9.A"})
    )

    missing = _checks(session, "terminal_missing")
    targets = {row["target"] for row in missing}
    assert "M1-2" not in targets
    assert "CR1-13" not in targets
    assert "TR1-1" not in targets
    assert "M1-9" not in targets
    assert "X9-9" in targets
    strip = _checks(session, "terminal_not_on_strip")
    assert not any(row["ref"] in {"M1-2", "CR1-13", "TR1-1"} for row in strip)


def test_nema_seal_in_has_no_terminal_strip_warnings():
    rows = build_nema_seal_in().design_checks()
    assert not any(row["check"] == "terminal_missing" for row in rows)
    assert not any(row["check"] == "terminal_not_on_strip" for row in rows)
    assert not any(row["check"] == "contact_count_exceeded" for row in rows)


def test_overload_is_not_a_contactor_power_pole():
    from panelcad.core.electrical import is_power_pole_device

    session = Session.create("Overload")
    device = session.catalog.get("nema.protection.overload")
    assert not is_power_pole_device(device)
    session.place("nema.protection.overload", ref="OL1", x=40, y=40)
    session.add_sheet("control", kind="schematic")
    session.place("nema.contactors.coil", ref="OL1", x=40, y=40, sheet_id="control")
    assert _checks(session, "duplicate_device_tag")
