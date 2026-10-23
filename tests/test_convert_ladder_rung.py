"""Ladder relayout places the whole rung (contacts, seal-in, coil) from the drawn wires."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from panelcad.convert.layout_convert import _Part, unplaced_wired, wired_chain
from tests.test_convert_standard import _convert, _session


def _kwargs(kind: str) -> dict:
    kwargs: dict = {"kind": kind, "control": "local"}
    if kind == "vfd":
        kwargs["drive"] = "generic_1k5"
    if kind == "star_delta":
        kwargs["timer_seconds"] = 8
    if kind == "plc_motor":
        kwargs["hp"] = 5
        kwargs["voltage"] = 480
    if kind == "soft_starter":
        kwargs["bypass"] = True
    return kwargs


def _part(ref: str, role: str) -> _Part:
    return _Part(library_id="x", role=role, x=0.0, y=0.0, ref=ref, scale=1.0, rotation=0, properties={}, page="control")


def _wire(frm: str, to: str) -> SimpleNamespace:
    return SimpleNamespace(frm=frm, to=to)


def test_wired_chain_follows_the_drawn_order_not_a_fixed_role_order():
    # Overload drawn before the start button, which is not the Circuit Builder order.
    parts = [_part(r, role) for r, role in (("A", "estop"), ("B", "ol_contact"), ("C", "start"), ("K", "coil"), ("KS", "seal"))]
    sheet = SimpleNamespace(
        wires=[
            _wire("L1", "A.1"),
            _wire("A.2", "B.95"),
            _wire("B.96", "C.3"),
            _wire("C.4", "K.A1"),
            _wire("C.3", "KS.13"),
            _wire("KS.14", "C.4"),
        ]
    )
    by = {p.ref: p for p in parts}
    chain = wired_chain(sheet, parts, by["K"], "A1", skip=by["KS"])
    assert [p.ref for p in chain] == ["A", "B", "C"]


def test_unplaced_wired_names_a_part_left_off_the_rungs():
    parts = [_part("A", "stop"), _part("K", "coil"), _part("Z", "seal")]
    sheet = SimpleNamespace(wires=[_wire("A.2", "K.A1"), _wire("Z.13", "A.2")])
    rungs = [{"contacts": [parts[0]], "output": parts[1]}]
    assert unplaced_wired(sheet, parts, rungs) == ["Z"]
    rungs[0]["seal"] = parts[2]
    assert unplaced_wired(sheet, parts, rungs) == []


@pytest.mark.parametrize("start", ["NEMA/ANSI", "IEC"])
def test_dol_rung_moves_as_one_and_coil_sits_right_with_a_short_lead(start: str, tmp_path: Path):
    session = _session(f"dol {start}", start)
    session.insert_circuit(standard=start, **_kwargs("dol"))
    for step, target in enumerate(("iec" if start == "NEMA/ANSI" else "nema", "nema" if start == "NEMA/ANSI" else "iec")):
        result = _convert(session, target, tmp_path, f"dol-{step}")
        for chunk in result["chunks"]:
            assert not [i for i in chunk["issues"] if i.get("flag") == "unplaced"], chunk
        sheet = session.sheet("motor-control")
        comps = {c.properties.get("role"): c for c in sheet.components}
        coil, seal, start_pb = comps["coil"], comps["seal"], comps["start"]
        rung = [comps[r] for r in ("estop", "stop", "start", "ol_contact", "coil")]
        # Series contacts left to right, coil last (nearest the right rail).
        xs = [c.x for c in rung]
        assert xs == sorted(xs), xs
        # The seal-in sits under the start button it bridges.
        assert abs(seal.x - start_pb.x) < 1e-6 and seal.y > start_pb.y
        # Short lead from the overload to the coil.
        assert coil.x - comps["ol_contact"].x <= 60.0
