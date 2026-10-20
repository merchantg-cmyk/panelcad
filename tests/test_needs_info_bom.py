"""INFO NEEDED marks stay off the counted bill and the quote.

The rule is the mark itself: the built-in symbol, any ``*.needs_info`` id,
a device named INFO NEEDED, or a description that is exactly that. It is not
a rule for one job, and it does not fill in a generic body size.
"""

from __future__ import annotations

from pathlib import Path

from panelcad import Session
from panelcad.core.models import Device
from panelcad.io.bom import grouped_rows, instance_rows, read_bom_csv
from panelcad.library.panel import effective_footprint
from panelcad.quote.build import build_quote
from panelcad.quote.labor import drawing_counts

_GENERIC_MM = {"18", "80", "70", "45", "22.5", "12"}


def _mark(
    session: Session,
    library_id: str,
    ref: str,
    *,
    sheet_id: str | None = None,
    **properties: object,
):
    return session.place(
        library_id,
        ref=ref,
        x=30,
        y=30,
        sheet_id=sheet_id,
        properties=properties or None,
        allow_needs_info=True,
    )


def test_needs_info_marks_stay_off_the_counted_bom_and_the_quote(tmp_path: Path) -> None:
    session = Session.create("Marks")
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    before = build_quote(session.project, session.catalog, session.parts)

    _mark(
        session,
        "reconstruct.needs_info",
        "U1",
        description="INFO NEEDED",
        reconstruct_needs_info=True,
        part_number="MARK-1",
        manufacturer="Acme",
    )
    session.add_sheet("other", kind="schematic")
    _mark(
        session,
        "reconstruct.needs_info",
        "U1",
        description="INFO NEEDED",
        part_number="MARK-1",
        manufacturer="Acme",
        sheet_id="other",
    )

    # A different library id, with the model's default 18×80×70 body.
    local = Device(
        library_id="local.needs_info",
        id="needs_info",
        name="Unknown",
        category="switching.relay",
    )
    session.catalog.devices[local.library_id] = local
    _mark(session, "local.needs_info", "U2", description="Vision head", part_number="MARK-2")

    named = Device(
        library_id="local.marker",
        id="marker",
        name="INFO NEEDED",
        category="switching.relay",
    )
    session.catalog.devices[named.library_id] = named
    _mark(session, "local.marker", "U3", part_number="MARK-3")

    # bom: no stored the way YAML loads an unquoted no. Not a needs-info mark.
    hidden = Device(
        library_id="local.note",
        id="note",
        name="Job note",
        category="connection.notes",
        properties={"bom": False},
    )
    session.catalog.devices[hidden.library_id] = hidden
    _mark(session, "local.note", "N1")

    counted_ids = {row["library_id"] for row in instance_rows(session.project, session.catalog)}
    grouped_ids = {row["library_id"] for row in grouped_rows(session.project, session.catalog)}
    assert counted_ids == {"iec.breakers.mcb_1p_16a"}
    assert grouped_ids == {"iec.breakers.mcb_1p_16a"}
    for ref in ("U1", "U2", "U3", "N1"):
        assert all(row["ref"] != ref for row in instance_rows(session.project, session.catalog))

    for ref in ("U1", "U2", "U3"):
        comp = next(
            item
            for sheet in session.project.sheets
            for item in sheet.components
            if item.id == ref
        )
        footprint = effective_footprint(session.catalog.get(comp.library_id), comp)
        assert (footprint.width_mm, footprint.height_mm, footprint.depth_mm) == (0.0, 0.0, 0.0)

    dest = tmp_path / "bom.csv"
    session.export_bom(dest, grouped=False)
    text = dest.read_text(encoding="utf-8-sig")
    assert "Needs info (not counted)" in text
    rows = read_bom_csv(dest)
    counted = [row for row in rows if str(row.get("counted") or "").lower() == "yes"]
    assert [row["ref"] for row in counted] == ["Q1"]
    assert sum(int(float(row["qty"] or 0)) for row in counted) == 1
    flagged = [row for row in rows if str(row.get("flag") or "") == "needs-info" and row.get("ref")]
    assert {row["ref"] for row in flagged} == {"U1", "U2", "U3"}
    assert all(row.get("counted") == "no" for row in flagged)
    assert all(row.get("qty") == "1" for row in flagged)
    for row in flagged:
        for key in ("width_mm", "height_mm", "depth_mm"):
            assert str(row.get(key) or "") == ""
            assert str(row.get(key) or "") not in _GENERIC_MM
    assert "N1" not in {row.get("ref") for row in rows}
    assert any(
        row.get("description") == "Needs info (not counted)" and not str(row.get("ref") or "").strip()
        for row in rows
    )

    quote = build_quote(session.project, session.catalog, session.parts)
    blob = " ".join(
        " ".join(str(row.get(key) or "") for key in ("part_number", "catalog", "description", "refs", "library_id"))
        for row in quote["parts"]
    )
    for token in ("MARK-1", "MARK-2", "MARK-3", "U1", "U2", "U3", "INFO NEEDED", "local.needs_info"):
        assert token not in blob
    assert quote["needs_info"] == 3
    assert quote["needs_info_label"] == "3 parts need info (not counted)"
    assert quote["totals"]["parts_cost"] == before["totals"]["parts_cost"]
    assert quote["totals"]["parts_sell"] == before["totals"]["parts_sell"]
    assert quote["totals"]["total"] == before["totals"]["total"]
    assert quote["lines"] == before["lines"]
    counts = drawing_counts(session.project, session.catalog)
    named = {ref for refs in counts["devices"].values() for ref in refs}
    named.update(counts["door_refs"], counts["terminal_refs"])
    assert {"U1", "U2", "U3", "N1"}.isdisjoint(named)
    assert "Q1" in named

    pdf = tmp_path / "bom.pdf"
    session.print_package(pdf, sections=["bom"])
    from panelcad.render.print_package import extract_pdf_text

    printed = extract_pdf_text(pdf)
    assert "Needs info (not counted)" in printed
    assert "needs-info" in printed
    assert "MARK-1" in printed
    assert "Q1" in printed
