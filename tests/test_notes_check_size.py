"""Design checks size a notes block from its text, the way it is drawn."""

from __future__ import annotations

from panelcad import Session
from panelcad.render.painter import notes_box
from panelcad.render.titleblock import title_block_rect, working_rect


def _flags(s: Session, ref: str) -> list[str]:
    return [
        r["check"]
        for r in s.design_checks()
        if r.get("ref") == ref or ref in str(r.get("message") or "")
    ]


def test_short_note_above_title_block_is_not_flagged():
    s = Session.create("Notes size")
    sheet = s.project.sheet(s.project.sheets[0].id)
    _tx, ty, _tw, _th = title_block_rect(sheet)
    # The 372 x 196 library box would reach the title block from here. The drawn box does not.
    note = s.place(
        "iec.notes.block", ref="N1", x=40, y=ty - 40, snap=False,
        properties={"text": "Field verify.", "width": 60},
    )
    _w, h = notes_box(note)
    assert note.y + h < ty
    flags = _flags(s, "N1")
    assert "symbol_on_title_block" not in flags
    assert "page_over_dense" not in flags


def test_note_on_title_block_is_still_flagged():
    s = Session.create("Notes size")
    sheet = s.project.sheets[0]
    _tx, ty, _tw, _th = title_block_rect(sheet)
    s.place("iec.notes.block", ref="N2", x=40, y=ty + 2, snap=False, properties={"text": "x", "width": 60})
    assert "symbol_on_title_block" in _flags(s, "N2")


def test_wide_note_past_border_is_still_outside():
    s = Session.create("Notes size")
    sheet = s.project.sheets[0]
    wx, wy, ww, _wh = working_rect(sheet)
    s.place(
        "iec.notes.block", ref="N3", x=wx + ww - 50, y=wy + 10, snap=False,
        properties={"text": "a note", "width": 120},
    )
    assert "page_over_dense" in _flags(s, "N3")
