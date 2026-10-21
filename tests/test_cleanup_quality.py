"""Cleanup never makes a built circuit harder to read."""

from __future__ import annotations

import pytest

from panelcad.api.session import Session
from panelcad.core.sheet_quality import CROSSING_ALLOWANCE, PRIMARY, sheet_quality

DOL = dict(kind="dol", hp=5, voltage=480, control="local", pilot_lights=True)
CASES = {
    "dol": [DOL],
    "reversing": [dict(kind="reversing", hp=5, voltage=480, control="local", pilot_lights=True)],
    "star_delta": [dict(kind="star_delta", hp=25, voltage=480, control="local")],
    "soft_starter": [dict(kind="soft_starter", hp=20, voltage=480, control="local")],
    "vfd": [dict(kind="vfd", drive="generic_1k5", hp=2, voltage=480, control="local")],
    "dol_x6": [DOL] * 6,
}


def _built(calls):
    s = Session.create("quality")
    for call in calls:
        s.insert_circuit(**call)
    return s


def _totals(s):
    out = dict.fromkeys(PRIMARY, 0)
    for sheet in s.project.sheets:
        if sheet.kind == "schematic":
            q = sheet_quality(sheet, s.catalog, s.project)
            for key in PRIMARY:
                out[key] += q[key]
    return out


def _no_worse(name, before, after):
    assert after["overlaps"] <= before["overlaps"], (name, before, after)
    assert after["through"] <= before["through"], (name, before, after)
    allowance = CROSSING_ALLOWANCE if after["overlaps"] < before["overlaps"] else 0
    # Totals over several pages: the allowance is per page that cleared one.
    assert after["crossings"] <= before["crossings"] + allowance * max(1, before["overlaps"]), (name, before, after)


@pytest.mark.parametrize("name", sorted(CASES))
def test_cleanup_never_worsens(name):
    s = _built(CASES[name])
    before = _totals(s)
    result = s.cleanup()
    after = _totals(s)
    _no_worse(name, before, after)
    for row in result["sheets"]:
        if "before" in row:
            _no_worse(name, row["before"], row["after"])
    # A second Cleanup has nothing left to make worse either.
    s.cleanup()
    again = _totals(s)
    _no_worse(name, after, again)


def test_rejected_tidy_keeps_the_rungs():
    s = _built(CASES["reversing"])
    sheet = next(sh for sh in s.project.sheets if sh.id == "motor-control")
    placed = {c.id: (c.x, c.y) for c in sheet.components}
    s.cleanup()
    sheet = s.sheet("motor-control")
    assert {c.id: (c.x, c.y) for c in sheet.components} == placed


def test_over_dense_page_is_split_then_tidied():
    s = _built(CASES["dol_x6"])
    assert s.page_density().get("over_dense")
    result = s.cleanup()
    assert result.get("split") and len(result["split"][0]["pages"]) >= 2
    assert not s.page_density().get("over_dense")
    totals = _totals(s)
    # PB6 no longer sits on M2-13 (nor PB10 on M4-13) on the continuation pages.
    assert totals["overlaps"] == 0 and totals["through"] == 0


def test_cleanup_survives_a_page_the_reflow_or_split_cannot_read(monkeypatch):
    """As-built pages broke reflow and split. Cleanup must not raise."""

    import panelcad.core.power_layout as power_layout

    session = Session.create("Unreadable")
    for _ in range(6):
        session.insert_circuit(kind="dol", hp=5, voltage=480, control="local", pilot_lights=True)
    sheets_before = [sheet.id for sheet in session.project.sheets]

    def broken_reflow(*_args, **_kwargs):
        raise IndexError("list index out of range")

    def broken_split(*_args, **_kwargs):
        session.add_sheet("half-made", title="half made")
        raise ValueError("pin ref must be Component.Pin, got ''")

    monkeypatch.setattr(power_layout, "reflow_existing_rungs", broken_reflow)
    monkeypatch.setattr(session, "split_sheet", broken_split)
    session.cleanup()
    assert [sheet.id for sheet in session.project.sheets] == sheets_before


def test_cleanup_keeps_a_page_whole_when_the_split_adds_crossings(monkeypatch):
    from panelcad.core import sheet_quality as quality

    session = Session.create("Whole")
    for _ in range(6):
        session.insert_circuit(kind="dol", hp=5, voltage=480, control="local", pilot_lights=True)
    real = quality.sheet_quality

    def worse_on_continuations(sheet, catalog, project=None):
        q = dict(real(sheet, catalog, project))
        if sheet.id.endswith("-2"):
            q["crossings"] += 100
        return q

    monkeypatch.setattr(quality, "sheet_quality", worse_on_continuations)
    result = session.cleanup()
    assert not result.get("split")
    assert all(not sheet.id.endswith("-2") for sheet in session.project.sheets)
