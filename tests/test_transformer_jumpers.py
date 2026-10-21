"""Control transformer primary jumpers follow the supply voltage.

Two primary windings, H1-H2 and H3-H4. Fed at the higher rating they go in
series (H2-H3); at the lower rating in parallel (H1-H3, H2-H4). When the
rating or the supply is unknown nothing is drawn and Problems says why.
"""

from __future__ import annotations

from panelcad import Session
from panelcad.core.checks import run_checks
from panelcad.core.transformer_jumpers import jumper_plan, jumper_primitives


def _cpt(volts: float | None, *, primary: str | None = None):
    session = Session.create("cpt")
    device = session.catalog.get("nema.power.control_transformer")
    props = {} if volts is None else {"primary_voltage": f"{volts:g} V"}
    if primary is not None:
        props["primary"] = primary
    comp = session.place("nema.power.control_transformer", x=60, y=60, properties=props)
    return session, device, comp


def _links(device, comp):
    plan = jumper_plan(device, comp)
    return plan.mode, {frozenset(pair) for pair in plan.links}


def test_480v_supply_puts_the_windings_in_series():
    _session, device, comp = _cpt(480)
    assert _links(device, comp) == ("series", {frozenset({"H2", "H3"})})


def test_240v_supply_puts_the_windings_in_parallel():
    _session, device, comp = _cpt(240)
    assert _links(device, comp) == ("parallel", {frozenset({"H1", "H3"}), frozenset({"H2", "H4"})})


def test_jumpers_are_drawn_between_the_right_terminals():
    pins = {}
    for volts, want in ((480, {(28.0, 34.0)}), (240, {(12.0, 34.0), (28.0, 50.0)})):
        _session, device, comp = _cpt(volts)
        pins = {p.id: p for p in device.pins}
        lines = [p for p in jumper_primitives(device, comp) if p.type == "line"]
        assert {(min(p.y, p.y2), max(p.y, p.y2)) for p in lines} == want
        # Each link sits left of the H labels so H1..H4 stay readable.
        labels = [p for p in device.primitives if p.type == "text" and p.text in {"H1", "H2", "H3", "H4"}]
        assert len(labels) == 4
        assert all(line.x < min(label.x for label in labels) - 1.0 for line in lines)
    assert pins["H3"].y > pins["H2"].y


def test_unknown_supply_is_a_warning_not_a_guess():
    session, device, comp = _cpt(None)
    plan = jumper_plan(device, comp)
    assert plan.mode == "unknown" and not plan.links
    assert jumper_primitives(device, comp) == []
    found = [f for f in run_checks(session.project, session.catalog) if f.check == "transformer_jumpers_unknown"]
    assert len(found) == 1 and found[0].severity == "warning" and found[0].ref == comp.id
    # 208 V matches neither winding arrangement of a 480/240 V part.
    _s, device, comp = _cpt(208)
    assert jumper_plan(device, comp).mode == "unknown"
    # A single-rated primary cannot say which arrangement 480 V means.
    _s, device, comp = _cpt(480, primary="480V")
    assert jumper_plan(device, comp).mode == "unknown"


def test_built_circuits_pick_the_set_from_line_voltage():
    for volts, mode in ((480, "series"), (240, "parallel")):
        session = Session.create(f"dol {volts}")
        session.insert_circuit(kind="dol", control="local", control_voltage=120, voltage=volts)
        cat = session.catalog
        cpts = [
            (cat.get(c.library_id), c)
            for sheet in session.project.sheets
            for c in sheet.components
            if c.library_id.endswith("power.control_transformer")
        ]
        assert cpts
        assert {jumper_plan(d, c).mode for d, c in cpts} == {mode}
        assert not [f for f in run_checks(session.project, cat) if f.check == "transformer_jumpers_unknown"]


def test_dual_primary_counts_at_the_supply_voltage_on_the_nameplate():
    """A 480/240 V primary on a 480 V input adds its 480 V current, not 240 V."""

    from panelcad.api.session import Session
    from panelcad.electrical.nameplate import build_nameplate

    for applied in (True, False):
        session = Session.create("dual primary")
        session.insert_circuit(kind="dol", hp=5, voltage=480, control="local", control_voltage=120)
        if not applied:
            for sheet in session.project.sheets:
                for comp in sheet.components:
                    comp.properties.pop("primary_voltage", None)
        lines = build_nameplate(session.project, session.catalog).lines
        xfmr = next(line for line in lines if line.kind == "transformer")
        assert xfmr.included and abs(xfmr.amperes - 150 / (3**0.5 * 480)) < 0.01, (applied, xfmr)
