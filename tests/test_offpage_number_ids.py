"""wire_offpage carries the given wire number and makes project-unique wire ids."""

from __future__ import annotations

from panelcad import Session


def _project(tmp_path, style: str = "ladder") -> Session:
    s = Session.create("Offpage number")
    s.set_title_block(drawing_style=style)
    s.add_sheet("a", "Sheet A")
    s.add_sheet("b", "Sheet B")
    s.place("nema.operators.pb_no", ref="PB1", x=60, y=60, sheet_id="a", snap=False)
    s.place("nema.operators.pb_no", ref="PB2", x=140, y=60, sheet_id="a", snap=False)
    s.place("nema.operators.pb_no", ref="PB3", x=60, y=100, sheet_id="a", snap=False)
    s.place("nema.relays.cr", ref="CR1", x=120, y=80, sheet_id="b", snap=False)
    s.place("nema.relays.cr", ref="CR2", x=120, y=140, sheet_id="b", snap=False)
    s.wire("PB1.4", "PB2.3", sheet_id="a")
    s.wire("CR1.A2", "CR2.A2", sheet_id="b")
    path = tmp_path / "p.pcad.yaml"
    s.save(path)
    # Reopening restarts the id counter, which is where ids used to repeat.
    return Session.open(path)


def _ids(s: Session) -> list[str]:
    return [w.id for sh in s.project.sheets for w in sh.wires]


def test_wire_offpage_uses_number_and_new_ids(tmp_path):
    for style in ("ladder", "iec"):
        s = _project(tmp_path, style)
        before = set(_ids(s))
        pos = {(sh.id, c.id): (c.x, c.y) for sh in s.project.sheets for c in sh.components}
        out = s.wire_offpage("PB3.4", "CR1.A1", from_sheet="a", to_sheet="b", net="7", number="512")
        ids = _ids(s)
        assert len(ids) == len(set(ids)), ids
        assert out["from_wire"] not in before and out["to_wire"] not in before
        for sid, wid in (("a", out["from_wire"]), ("b", out["to_wire"])):
            w = next(w for w in s.project.sheet(sid).wires if w.id == wid)
            assert w.number == "512" and w.number_locked
        after = {(sh.id, c.id): (c.x, c.y) for sh in s.project.sheets for c in sh.components}
        assert {k: v for k, v in after.items() if k in pos} == pos


def test_wire_with_to_sheet_passes_number(tmp_path):
    s = _project(tmp_path)
    out = s.wire("PB3.4", "CR1.A1", sheet_id="a", to_sheet="b", number="77")
    w = next(w for w in s.project.sheet("b").wires if w.id == out["to_wire"])
    assert w.number == "77"


def test_plain_wire_id_unique_across_sheets(tmp_path):
    s = _project(tmp_path)
    s.wire("PB3.3", "PB3.4", sheet_id="a")
    ids = _ids(s)
    assert len(ids) == len(set(ids))
