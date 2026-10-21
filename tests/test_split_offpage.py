"""split_sheet keeps off-page references pointing at the right page."""

from __future__ import annotations

from panelcad.api.session import Session
from panelcad.core.xref import offpage_callout, offpage_peer, sheet_number, xref_problems


def _six_dol() -> Session:
    session = Session.create("Split arrows")
    for _ in range(6):
        session.insert_circuit(kind="dol", hp=5, voltage=480, control="local", pilot_lights=True)
    return session


def _arrows(session: Session, tag: str) -> list[tuple[str, object]]:
    out = []
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if "offpage" in comp.library_id and comp.properties.get("tag") == tag:
                out.append((sheet.id, comp))
    return out


def _broken(session: Session) -> list[str]:
    return [row["message"] for row in xref_problems(session.project, session.catalog) if row["check"] == "broken_offpage"]


def test_split_feed_pairs_are_not_broken() -> None:
    session = _six_dol()
    result = session.split_sheet("motor-control", apply=True)
    assert result["applied"] and len(result["pages"]) >= 2
    assert _broken(session) == []


def test_far_arrow_follows_a_moved_device() -> None:
    session = _six_dol()
    session.add_sheet("rio-io", title="Remote IO", kind="schematic")
    session.place("nema.operators.pb_no", ref="PB20", x=80, y=60, sheet_id="rio-io")
    session.wire_offpage("PB12.4", "PB20.3", from_sheet="motor-control", to_sheet="rio-io", net="X")
    session.split_sheet("motor-control", apply=True)
    home = next(sheet for sheet in session.project.sheets if any(c.id == "PB12" for c in sheet.components))
    assert home.id != "motor-control"
    arrows = _arrows(session, "X")
    # The arrow wired only to PB12 went with it, so there is no relay pair.
    assert sorted(sid for sid, _ in arrows) == sorted([home.id, "rio-io"])
    rio = session.sheet("rio-io")
    far = next(comp for sid, comp in arrows if sid == "rio-io")
    peer = offpage_peer(session.project, session.catalog, rio, far)
    assert peer is not None and peer[0].id == home.id
    text = offpage_callout(session.project, session.catalog, rio, far)
    assert text.startswith(f"FROM {sheet_number(session.project.sheets, home.id)} /")
    assert not text.startswith(f"FROM {sheet_number(session.project.sheets, 'rio-io')} ")
    assert _broken(session) == []
    session.undo()
    assert any(c.id == "PB12" for c in session.sheet("motor-control").components)


def test_link_resolves_the_far_end_not_this_page() -> None:
    session = Session.create("Link")
    session.add_sheet("b", title="B", kind="schematic")
    session.place("nema.operators.pb_no", ref="PB1", x=60, y=60, sheet_id="main")
    session.place("nema.operators.pb_no", ref="PB2", x=60, y=60, sheet_id="b")
    session.wire_offpage("PB1.4", "PB2.3", from_sheet="main", to_sheet="b", net="Y")
    page = session.sheet("b")
    arrow = next(comp for sid, comp in _arrows(session, "Y") if sid == "b")
    # A stale target leaves only the link. Its far end is main:PB1.
    arrow.properties["dest_ref"] = "GONE"
    arrow.properties["dest_pin"] = ""
    arrow.properties["link"] = "main:PB1.4|b:PB2.3"
    peer = offpage_peer(session.project, session.catalog, page, arrow)
    assert peer is not None and (peer[0].id, peer[1].id) == ("main", "PB1")
    arrow.properties["link"] = "b:PB2.3|main:PB1.4"
    peer = offpage_peer(session.project, session.catalog, page, arrow)
    assert peer is not None and (peer[0].id, peer[1].id) == ("main", "PB1")
    # Only this page in the link: no peer rather than a self reference.
    arrow.properties["link"] = "b:PB9.1|b:PB2.3"
    assert offpage_peer(session.project, session.catalog, page, arrow) is None


def test_deleting_the_far_device_is_still_a_broken_offpage():
    session = Session.create("Broken")
    for _ in range(2):
        session.insert_circuit(kind="dol", hp=5, voltage=480, control="local")
    session.add_sheet("io", title="IO", kind="schematic")
    session.place("nema.operators.pb_no", ref="PB20", x=80, y=60, sheet_id="io")
    session.wire_offpage("PB4.4", "PB20.3", from_sheet="motor-control", to_sheet="io", net="X")
    assert not [row for row in session.design_checks() if row["check"] == "broken_offpage"]
    session.delete_component("PB20", sheet_id="io")
    assert [row for row in session.design_checks() if row["check"] == "broken_offpage"]
