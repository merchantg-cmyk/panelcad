"""Offline replay of the PR159 clean_300/dpi_150 root-cause pack (no OCR)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panelcad.importer.ladder_synth import extract_import_from_to, synthesize_imported_ladder
from panelcad.importer.tile import _separate_colliding_tags, merge_extractions

_PACK_CANDIDATES = [
    Path("/tmp/pr159-rootcause"),
    Path("/tmp/pr159-rootcause-uploads"),
]


def _pack_root() -> Path:
    for path in _PACK_CANDIDATES:
        if (path / "clean_300" / "raw_merged.json").is_file():
            return path
    # Unpack from the uploaded tarball once.
    upload = Path("/tmp/pr159-rootcause.tar.gz")
    if upload.is_file():
        import tarfile

        dest = Path("/tmp/pr159-rootcause")
        if not (dest / "clean_300" / "raw_merged.json").is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(upload, "r:gz") as archive:
                archive.extractall("/tmp")
        if (dest / "clean_300" / "raw_merged.json").is_file():
            return dest
    pytest.skip("PR159 root-cause pack not available on this VM")


@pytest.mark.parametrize("fixture", ["clean_300", "dpi_150"])
def test_pack_synth_does_not_invent_m113_return_or_m1_2(fixture: str) -> None:
    root = _pack_root() / fixture
    merged = json.loads((root / "raw_merged.json").read_text())
    facts = {
        "standard": "NEMA",
        "devices": list(merged.get("devices") or []),
        "wires": list(merged.get("wires") or []),
    }
    sched = extract_import_from_to(facts)
    tags = {d.tag for d in sched.devices}
    assert "M1_2" not in tags
    invented = [
        row
        for row in sched.from_to_rows
        if row.source == "rung_inferred"
        and (
            ({row.from_tag, row.to_tag} >= {"M1-13", "L2"})
            or "M1_2" in {row.from_tag, row.to_tag}
        )
    ]
    assert invented == [], [r.to_dict() for r in invented]


def test_pack_dpi_150_merge_separates_m123_cr113() -> None:
    root = _pack_root() / "dpi_150"
    merged = json.loads((root / "raw_merged.json").read_text())
    # Re-merge the two colliding aux devices as a fresh extraction part.
    collide = [
        d
        for d in (merged.get("devices") or [])
        if str(d.get("tag")) in {"M1-23", "CR1-13"}
    ]
    assert len(collide) == 2
    # Reset to identical box (as in the pack) and re-run merge separation.
    for device in collide:
        device["x"] = 40.98
        device["y"] = 171.69
    out = merge_extractions([{"devices": collide, "wires": []}])
    tags = {str(d.get("tag")) for d in out["devices"]}
    assert tags == {"M1-23", "CR1-13"}
    by_tag = {str(d["tag"]): d for d in out["devices"]}
    assert (by_tag["M1-23"]["x"], by_tag["M1-23"]["y"]) != (
        by_tag["CR1-13"]["x"],
        by_tag["CR1-13"]["y"],
    )


@pytest.mark.parametrize("fixture", ["clean_300", "dpi_150"])
def test_pack_before_leftover_kinds_recorded(fixture: str) -> None:
    """Document pack before leftovers; origin fixes target these classes."""

    root = _pack_root() / fixture
    before = json.loads((root / "leftover-list-before.json").read_text())
    rows = before.get("questions") if isinstance(before, dict) else before
    kinds = {str(row.get("kind") or "") for row in rows}
    assert kinds
    # Phone is ignored; these two fixtures must still list the cascade kinds
    # the origin fixes address (at least one of contacts_only / checker_wire).
    assert {"contacts_only", "checker_wire"} & kinds


@pytest.mark.parametrize("fixture", ["clean_300", "dpi_150"])
def test_pack_rebuild_clears_invented_cascade_leftovers(fixture: str) -> None:
    """Re-extract + synthesize from pack merged facts: no contacts_only / M1_2 cascade."""

    from collections import Counter

    from panelcad.importer.cleanup import remaining_gaps
    from panelcad.importer.ladder_synth import synthesize_imported_ladder
    from panelcad.importer.tile import _separate_colliding_tags

    root = _pack_root() / fixture
    merged = json.loads((root / "raw_merged.json").read_text())
    devices = list(merged.get("devices") or [])
    _separate_colliding_tags(devices)
    facts = {"standard": "NEMA", "devices": devices, "wires": list(merged.get("wires") or [])}
    sched = extract_import_from_to(facts)
    session = synthesize_imported_ladder(facts, schedule=sched)
    kinds = Counter(str(g.get("kind") or "") for g in remaining_gaps(session))
    assert kinds.get("contacts_only", 0) == 0, kinds
    assert kinds.get("untagged", 0) == 0 or "M1_2" not in {
        c.id for sheet in session.project.sheets for c in sheet.components
    }
    assert not any(c.id == "M1_2" for sheet in session.project.sheets for c in sheet.components)
    # Interlock aux both present after collide separation + same-tag-only drop.
    ids = {c.id for sheet in session.project.sheets for c in sheet.components}
    assert "M1-13" in ids and "CR1-13" in ids


def test_pack_dpi_150_does_not_invent_collide_short_across_pl1() -> None:
    """Keeping M1-23+CR1-13 must not invent M1-23→CR1-13→PL1 over the OCR hop."""

    from panelcad.importer.tile import _separate_colliding_tags

    root = _pack_root() / "dpi_150"
    merged = json.loads((root / "raw_merged.json").read_text())
    devices = list(merged.get("devices") or [])
    wires = list(merged.get("wires") or [])
    _separate_colliding_tags(devices)
    sched = extract_import_from_to({"standard": "NEMA", "devices": devices, "wires": wires})
    invented = [
        row
        for row in sched.from_to_rows
        if row.source == "rung_inferred"
        and (
            {row.from_tag, row.to_tag} == {"M1-23", "CR1-13"}
            or {row.from_tag, row.to_tag} == {"CR1-13", "PL1"}
        )
    ]
    assert invented == [], [r.to_dict() for r in invented]
    # OCR hop stays the only M1-23→PL1 fact.
    ocr_pl1 = [
        row
        for row in sched.from_to_rows
        if {row.from_tag, row.to_tag} == {"M1-23", "PL1"} and row.source == "ocr"
    ]
    assert ocr_pl1, "OCR M1-23.4→PL1.X1 must survive"


def test_pack_dpi_150_rebuild_has_no_two_numbers_from_collide() -> None:
    from collections import Counter

    from panelcad.importer.cleanup import remaining_gaps
    from panelcad.importer.ladder_synth import synthesize_imported_ladder
    from panelcad.importer.tile import _separate_colliding_tags

    root = _pack_root() / "dpi_150"
    merged = json.loads((root / "raw_merged.json").read_text())
    devices = list(merged.get("devices") or [])
    _separate_colliding_tags(devices)
    facts = {"standard": "NEMA", "devices": devices, "wires": list(merged.get("wires") or [])}
    session = synthesize_imported_ladder(facts, schedule=extract_import_from_to(facts))
    kinds = Counter(str(g.get("kind") or "") for g in remaining_gaps(session))
    assert kinds.get("two_numbers", 0) == 0, kinds
    # M1-23 keeps its OCR wire — not stripped to unconnected by an invented short.
    assert not any(g.get("kind") == "unconnected" and g.get("tag") == "M1-23" for g in remaining_gaps(session))


@pytest.mark.parametrize("fixture", ["clean_300", "dpi_150"])
def test_pack_fuse_transformer_facing_pin_and_dangling_stub(fixture: str) -> None:
    """Pack geometry: T1.H4 lands on facing FU2.2; a one-pin fuse cannot issue."""

    from panelcad.api.session import Session
    from panelcad.core.elementary_layout import (
        elementary_layout_issues,
        issue_elementary,
        layout_elementary,
    )
    from panelcad.core.elementary_layout import _crosses_own_body
    from panelcad.render.wiring import route_sheet_wires

    # Pack before-project coordinates (fuses left of T1).
    _ = fixture  # both fixtures share the same fuse/T1 geometry
    session = Session.create("Pack fuse", sheet_id="elementary", title="Elementary")
    session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
    sheet = session.project.sheet("elementary")
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    session.place("nema.protection.fuse_10a", ref="FU2", x=61.0, y=62.0, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=347.0, y=37.0, snap=False)
    bad = session.wire("T1.H4", "FU2.1", number="101")
    routes = route_sheet_wires(sheet, session.catalog, project=session.project)
    assert _crosses_own_body(sheet, session.catalog, bad, routes.get(bad.id) or [])
    assert any("runs through a symbol" in item for item in elementary_layout_issues(session, sheet))

    layout_elementary(session, sheet)
    ends = {(str(w.frm), str(w.to)) for w in sheet.wires} | {(str(w.to), str(w.frm)) for w in sheet.wires}
    assert ("T1.H4", "FU2.2") in ends
    assert any("dangling stub" in item for item in elementary_layout_issues(session, sheet))
    assert issue_elementary(session, sheet, force=True) is False


def _pack_rebuild_session(fixture: str):
    """Synthesize the pack fixture and attach OCR schedule/items for leftovers."""

    root = _pack_root() / fixture
    merged = json.loads((root / "raw_merged.json").read_text())
    devices = list(merged.get("devices") or [])
    _separate_colliding_tags(devices)
    facts = {"standard": "NEMA", "devices": devices, "wires": list(merged.get("wires") or [])}
    sched = extract_import_from_to(facts)
    session = synthesize_imported_ladder(facts, schedule=sched)
    sheet = next(s for s in session.project.sheets if getattr(s, "kind", "") == "schematic")
    payload = session.project.properties.setdefault("print_import", {})
    payload["from_to_schedule"] = {"from_to_rows": [row.to_dict() for row in sched.from_to_rows]}
    items: list[dict] = []
    for wire in merged.get("wires") or []:
        number = str(wire.get("number") or "")
        if number.isdigit():
            items.append(
                {
                    "text": number,
                    "number": number,
                    "x": wire.get("x"),
                    "y": wire.get("y"),
                    "sheet": sheet.id,
                }
            )
    payload["items"] = items
    return session, sheet, root


def _ensure_truth_wires(session, sheet) -> None:
    """Land missing fixture-truth hops with facing fuse pins (no through-body).

    Primary fuse line-side pins never invent a hop to this sheet's control
    rails — those feeds are answered as off-page/external supply leftovers.
    Secondary hot is X1→FU3→L1; never a bare X1→L1.
    """

    truth = json.loads((_pack_root() / "truth.json").read_text())
    # Pack OCR may predate FU3 — place the printed secondary fuse from truth.
    ids = {comp.id for comp in sheet.components}
    for tag in truth["sheets"]["elementary"]["components"]:
        if tag in ids:
            continue
        if not str(tag).upper().startswith("FU"):
            continue
        try:
            session.place(
                "nema.protection.fuse_10a",
                ref=tag,
                x=180.0,
                y=28.0,
                sheet_id=sheet.id,
                snap=False,
                properties={"role": "secondary_fuse", "print_import_source": "user"},
            )
            ids.add(tag)
        except (ValueError, KeyError, AssertionError):
            continue
    wires = truth["sheets"]["elementary"]["wires"]
    existing = {(str(w.frm), str(w.to)) for w in sheet.wires} | {(str(w.to), str(w.frm)) for w in sheet.wires}
    for frm, to, number in wires:
        left = str(frm).split(".")[0]
        right = str(to).split(".")[0]
        if left not in ids and left not in {"L1", "L2"}:
            continue
        if right not in ids and right not in {"L1", "L2"}:
            continue
        # Never invent primary FU↔rail feeds — leftover answers land off-page.
        if {left, right} & {"FU1", "FU2"} and {left, right} & {"L1", "L2"}:
            continue
        if (frm, to) in existing or (to, frm) in existing:
            continue
        try:
            session.wire(frm, to, number=str(number), sheet_id=sheet.id)
            existing.add((frm, to))
        except (ValueError, KeyError, AssertionError):
            continue


def _fuse_offpage_labels(sheet) -> dict[str, str]:
    """Map fuse pin → off-page callout label for primary feeds."""

    from panelcad.render.sheet import parse_pin_ref

    found: dict[str, str] = {}
    by_id = {comp.id: comp for comp in sheet.components}
    for wire in sheet.wires:
        ends = []
        for text in (wire.frm, wire.to):
            try:
                ref, pin = parse_pin_ref(str(text))
            except ValueError:
                continue
            if ref and pin:
                ends.append((ref, pin))
        if len(ends) != 2:
            continue
        for (a_ref, a_pin), (b_ref, _b_pin) in (ends, list(reversed(ends))):
            a = by_id.get(a_ref)
            b = by_id.get(b_ref)
            if a is None or b is None:
                continue
            if "fuse" not in str(a.library_id or "").lower():
                continue
            if "offpage" not in str(b.library_id or "").lower():
                continue
            props = b.properties if isinstance(b.properties, dict) else {}
            label = str(props.get("callout") or props.get("tag") or b.id)
            found[f"{a_ref}.{a_pin}"] = label
    return found


@pytest.mark.parametrize("fixture", ["clean_300", "dpi_150"])
def test_pack_after_true_issues_with_pin_to_pin_fuses(fixture: str) -> None:
    """True leftover answers issue the sheet with facing fuses + off-page primary feeds."""

    from panelcad.core.elementary_layout import (
        elementary_layout_issues,
        issue_elementary,
        layout_elementary,
    )
    from panelcad.core.panel_wiring import export_sheet_png
    from panelcad.importer.cleanup import _completion_issues, remaining_gaps
    from panelcad.importer import leftover_questions as leftover
    from tests.test_leftover_questions import _scripted_truth_batch

    session, sheet, _root = _pack_rebuild_session(fixture)
    layout_elementary(session, sheet)
    assert any("dangling stub" in item for item in elementary_layout_issues(session, sheet))
    # OCR near the pin may auto-record an off-page feed (facts only). Any
    # remaining fuse open_pin leftover offers off-page / external — never L1/L2.
    fuse_open = [
        gap
        for gap in remaining_gaps(session)
        if gap.get("kind") == "open_pin" and str(gap.get("tag") or "").startswith("FU")
    ]
    for gap in fuse_open:
        choices = leftover._choices(session, gap)
        assert leftover._KEEP in choices
        assert leftover._OFFPAGE in choices
        assert any(str(c).lower().startswith("off-page") for c in choices), choices
        assert not any(str(c).startswith("L1 as ") or str(c).startswith("L2 as ") for c in choices), choices
        assert leftover._OPEN_STUB not in choices
    if not fuse_open:
        # Auto-landed from OCR — primary pins must already be off-page, not rails.
        labels = _fuse_offpage_labels(sheet)
        assert labels, "expected OCR off-page primary feeds or open_pin leftovers"

    for _ in range(24):
        _ensure_truth_wires(session, sheet)
        rows = leftover.questions_for(session, write_crops=False)
        if not rows:
            break
        batch = _scripted_truth_batch(rows)
        if not batch:
            _ensure_truth_wires(session, sheet)
            break
        leftover.apply_answers(session, batch)
    _ensure_truth_wires(session, sheet)
    layout_elementary(session, sheet)
    assert issue_elementary(session, sheet, force=True) is True
    assert elementary_layout_issues(session, sheet) == []
    assert _completion_issues(session) == []

    ends = {(str(w.frm), str(w.to), str(w.number)) for w in sheet.wires}
    assert ("T1.H4", "FU2.2", "101") in ends or ("FU2.2", "T1.H4", "101") in ends
    assert ("FU1.2", "T1.H1", "103") in ends or ("T1.H1", "FU1.2", "103") in ends
    # Primary never on control rails; secondary is X1→FU3→L1 and X2→L2.
    assert not any({"FU1.1", "L1"} <= {str(w.frm), str(w.to)} for w in sheet.wires)
    assert not any({"FU2.1", "L2"} <= {str(w.frm), str(w.to)} for w in sheet.wires)
    assert not any({"T1.X1", "L1"} <= {str(w.frm), str(w.to)} for w in sheet.wires)
    assert ("T1.X2", "L2", "104") in ends or ("L2", "T1.X2", "104") in ends
    assert any({"T1.X1", "FU3.1"} <= {a, b} or {"T1.X1", "FU3.2"} <= {a, b} for a, b, _n in ends)
    assert any(({"FU3.1", "L1"} <= {a, b} or {"FU3.2", "L1"} <= {a, b}) and n == "102" for a, b, n in ends)
    labels = _fuse_offpage_labels(sheet)
    assert "FU1.1" in labels and "FU2.1" in labels, labels
    assert labels.get("FU1.1") not in {"102", "104"}, labels
    assert labels.get("FU2.1") not in {"102", "104"}, labels
    assert not any("through a symbol" in item for item in elementary_layout_issues(session, sheet))

    out = Path("/opt/cursor/artifacts") / f"pr159-{fixture}-after-true-fuse.png"
    export_sheet_png(sheet, out, catalog=session.catalog, dpi=150.0, project=session.project)
    assert out.is_file() and out.stat().st_size > 1000
