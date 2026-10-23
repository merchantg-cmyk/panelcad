"""A TO arrow placed by hand gets its FROM arrow on the target page."""

from __future__ import annotations

from panelcad import Session
from panelcad.cli.agent import OPS, Agent


def _to_props(src: str, src_pin: str, dest: str, ref: str, pin: str, tag: str) -> dict[str, str]:
    return {
        "tag": tag,
        "callout": f"TO / {ref}",
        "dest_sheet": dest,
        "dest_ref": ref,
        "dest_pin": pin,
        "link": f"{src}:{src_pin}|{dest}:{ref}.{pin}",
        "role": "to",
    }


def _setup() -> Session:
    s = Session.create("Pairing")
    s.add_sheet("ctl", "Control")
    s.add_sheet("pwr", "Power")
    s.place("nema.operators.pb_no", ref="PB1", x=60, y=60, sheet_id="ctl", snap=False)
    s.place("nema.relays.cr", ref="CR1", x=160, y=80, sheet_id="pwr", snap=False)
    return s


def test_hand_placed_to_arrow_is_flagged_then_paired():
    s = _setup()
    pin = s.catalog.get("nema.operators.pb_no").pins[-1].id
    s.place(
        "iec.connection.offpage", ref="OP1", x=300, y=56, sheet_id="ctl", snap=False,
        properties=_to_props("ctl", f"PB1.{pin}", "pwr", "CR1", "A1", "9"),
    )
    s.wire(f"PB1.{pin}", "OP1.A", sheet_id="ctl", net="9")
    checks = [r["check"] for r in s.design_checks()]
    assert "offpage_missing_from" in checks
    before = {(sh.id, c.id): (c.x, c.y) for sh in s.project.sheets for c in sh.components}
    out = s.pair_offpage()
    assert len(out["added"]) == 1
    added = out["added"][0]
    frm = s.project.sheet("pwr").component(added["ref"])
    assert frm.properties["role"] == "from"
    assert frm.properties["link"] == f"ctl:PB1.{pin}|pwr:CR1.A1"
    wires = [w for w in s.project.sheet("pwr").wires if w.id == added["wire"]]
    assert wires and {wires[0].frm, wires[0].to} == {f"{added['ref']}.A", "CR1.A1"}
    after = {(sh.id, c.id): (c.x, c.y) for sh in s.project.sheets for c in sh.components}
    assert {k: v for k, v in after.items() if k in before} == before
    assert "offpage_missing_from" not in [r["check"] for r in s.design_checks()]
    # Running it again adds nothing; one undo removes the pair.
    assert s.pair_offpage()["added"] == []
    s.undo()
    assert "offpage_missing_from" in [r["check"] for r in s.design_checks()]


def test_wire_offpage_pairs_are_not_flagged():
    s = _setup()
    pin = s.catalog.get("nema.operators.pb_no").pins[-1].id
    s.wire_offpage(f"PB1.{pin}", "CR1.A1", from_sheet="ctl", to_sheet="pwr", net="9")
    assert "offpage_missing_from" not in [r["check"] for r in s.design_checks()]
    assert s.pair_offpage()["added"] == []


def test_agent_place_of_to_arrow_adds_from_arrow():
    assert "pair_offpage" in OPS
    agent = Agent()
    assert agent.handle({"id": "1", "op": "create_project", "name": "Pair op"})["ok"]
    for sid, title in (("ctl", "Control"), ("pwr", "Power")):
        assert agent.handle({"id": sid, "op": "add_sheet", "sheet_id": sid, "title": title})["ok"]
    assert agent.handle({"id": "2", "op": "place", "library_id": "nema.operators.pb_no", "ref": "PB1", "x": 60, "y": 60, "sheet": "ctl"})["ok"]
    assert agent.handle({"id": "3", "op": "place", "library_id": "nema.relays.cr", "ref": "CR1", "x": 160, "y": 80, "sheet": "pwr"})["ok"]
    placed = agent.handle(
        {
            "id": "4", "op": "place", "library_id": "iec.connection.offpage", "ref": "OP1",
            "x": 300, "y": 56, "sheet": "ctl",
            "properties": _to_props("ctl", "PB1.4", "pwr", "CR1", "A1", "9"),
        }
    )
    assert placed["ok"], placed
    added = placed["result"]["pair_offpage"]["added"]
    assert len(added) == 1 and added[0]["sheet"] == "pwr"


def test_hand_drawn_from_arrow_with_own_link_counts_as_partner():
    s = _setup()
    pin = s.catalog.get("nema.operators.pb_no").pins[-1].id
    s.place(
        "iec.connection.offpage", ref="OP1", x=300, y=56, sheet_id="ctl", snap=False,
        properties=_to_props("ctl", f"PB1.{pin}", "pwr", "CR1", "A1", "9"),
    )
    s.place(
        "iec.connection.offpage_from", ref="OP2", x=20, y=76, sheet_id="pwr", snap=False,
        properties={"tag": "9", "dest_sheet": "ctl", "dest_ref": "PB1", "role": "from", "link": "hand-drawn"},
    )
    assert "offpage_missing_from" not in [r["check"] for r in s.design_checks()]
    assert s.pair_offpage()["added"] == []
