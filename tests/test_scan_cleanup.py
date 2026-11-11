"""The scan wrap-up may fill a gap. It may not invent, redraw, or issue a bad sheet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panelcad.api.session import Session
from panelcad.core.elementary_layout import elementary_layout_issues
from panelcad.core.ladder import rail_xs
from panelcad.importer.cleanup import (
    cleanup_harness,
    load_cleanup_reply,
    remaining_gaps,
    replay_cleanup,
    review_cleanup,
    work_json,
    wrap_up_scan,
)
from panelcad.importer.vision import FakeVision
from panelcad.render.sheet import pin_world


class SpyVision(FakeVision):
    def __init__(self, recordings: dict | None = None) -> None:
        super().__init__(recordings)
        self.calls: list[dict] = []

    def extract(self, work: dict) -> dict:
        self.calls.append(work)
        return super().extract(work)


def _sheet(session: Session):
    return session.project.sheet("elementary")


def _ladder(name: str = "Cleanup") -> Session:
    session = Session.create(name, sheet_id="elementary", title="Elementary")
    session.set_title_block(
        company="Demo Controls",
        drawing_number="E-100",
        standard="NEMA/ANSI",
        drawing_style="ladder",
        coil_style="circle",
        drawn_by="Demo",
    )
    sheet = _sheet(session)
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    return session


def _starter() -> Session:
    """A real starter with two blanks: wire ? between PB1 and M1, and PL1's return."""

    session = _ladder()
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=100, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=120, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=220, y=100, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=160, y=140, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "PB2.1", number="105")
    session.wire("PB2.2", "PB1.3", number="106")
    session.wire("PB1.4", "M1.A1", number="?")
    session.wire("M1.A2", "L2", number="108")
    return session


def _scan(tmp_path: Path) -> str:
    path = tmp_path / "scan.png"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\x81\x8e\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return str(path)


def _right(session: Session, ref: str) -> float:
    sheet = _sheet(session)
    comp = sheet.component(ref)
    device = session.catalog.get(comp.library_id)
    pin = next((item for item in device.pins if str(item.side) == "right"), device.pins[-1])
    return pin_world(comp, pin)[0]


def _numbers(session: Session) -> set[str]:
    return {str(wire.number or "") for wire in _sheet(session).wires}


def _has_pair(session: Session, left: str, right: str) -> bool:
    want = {left.upper(), right.upper()}
    for wire in _sheet(session).wires:
        ends = {str(wire.frm).split(".")[0].upper(), str(wire.to).split(".")[0].upper()}
        if ends == want:
            return True
    return False


def test_harness_text_is_what_the_vision_call_reads(tmp_path: Path):
    text = cleanup_harness()
    for phrase in (
        "Extract facts only",
        "Do not draw, move lines, restyle, or place symbols",
        "Do not overwrite a good tag",
        "pb_nc",
        "pb_no",
        'Unreadable stays "?"',
        "Never invent",
        "layout engine redraws",
        "catalog library symbol",
        "Wires through symbols are illegal",
        "do not invent a landing",
        "runs from L1 to L2 with no load",
        "gold Demo Controls",
    ):
        assert phrase in text
    session = _starter()
    vision = SpyVision({"cleanup": {"devices": [], "wires": [], "connections": []}})
    wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert vision.calls
    work = vision.calls[0]
    assert work["instructions"] == text
    assert work["harness"] == text
    assert work["gaps"]
    assert "scan_ladder_demo" not in work_json(work)
    assert "truth" not in work


def test_invented_connection_is_dropped(tmp_path: Path):
    session = _starter()
    before = len(_sheet(session).wires)
    vision = SpyVision(
        {
            "cleanup": {
                "connections": [
                    {"from": "ES1.2", "to": "M1.A2", "confidence": 0.99, "unreadable": False},
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["issued"] is False
    assert any(row["reason"] == "not a named gap" for row in result["dropped"])
    assert len(_sheet(session).wires) == before
    assert not _has_pair(session, "ES1", "M1")


def test_overwritten_tag_is_dropped(tmp_path: Path):
    session = _starter()
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {
                        "tag": "ES1",
                        "library_id": "nema.operators.pb_no",
                        "confidence": 0.99,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "overwritten tag" for row in result["dropped"])
    assert _sheet(session).component("ES1").library_id == "nema.operators.estop"
    assert result["issued"] is False


def test_reader_set_nc_is_not_flipped(tmp_path: Path):
    session = _starter()
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {
                        "tag": "PB2",
                        "library_id": "nema.operators.pb_no",
                        "confidence": 0.99,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "flipped contact sense" for row in result["dropped"])
    assert _sheet(session).component("PB2").library_id.endswith("pb_nc")
    assert _sheet(session).component("PB1").library_id.endswith("pb_no")


def test_guessed_wire_number_is_dropped(tmp_path: Path):
    session = _starter()
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    vision = SpyVision(
        {
            "cleanup": {
                "wires": [
                    {"gap": f"wire:{wire.id}", "number": "7", "confidence": 0.99, "unreadable": False},
                    {"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.2, "unreadable": False},
                    {"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.99, "unreadable": True},
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    reasons = {row["reason"] for row in result["dropped"]}
    assert "guessed wire number" in reasons
    assert "unsure" in reasons
    assert wire.number == "?"
    assert result["issued"] is False


def test_redraw_request_does_not_move_the_coil(tmp_path: Path):
    session = _starter()
    before = _sheet(session).component("M1").x
    vision = SpyVision(
        {
            "cleanup": {
                "action": "redraw",
                "move": [{"tag": "M1", "x": 10, "y": 10}],
                "devices": [
                    {
                        "tag": "CR9",
                        "library_id": "nema.relays.cr",
                        "x": 12,
                        "y": 12,
                        "confidence": 0.99,
                        "unreadable": False,
                    }
                ],
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "redraw" for row in result["dropped"])
    assert _sheet(session).component("M1").x == before
    assert all(comp.id != "CR9" for comp in _sheet(session).components)
    assert result["issued"] is False


def test_honest_gap_fills_are_redrawn(tmp_path: Path):
    session = _starter()
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    kept = _sheet(session).component("PB2").library_id
    vision = SpyVision(
        {
            "cleanup": {
                "wires": [{"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.95, "unreadable": False}],
                "connections": [
                    {"gap": "return:PL1", "to": "L2", "number": "104", "confidence": 0.95, "unreadable": False},
                    {
                        "gap": "unread_device:elementary",
                        "from": "CR9.A2",
                        "to": "L2",
                        "number": "109",
                        "confidence": 0.95,
                        "unreadable": False,
                    },
                ],
                "devices": [
                    {
                        "tag": "CR9",
                        "library_id": "nema.relays.cr",
                        "gap": "unread_device:elementary",
                        "confidence": 0.95,
                        "unreadable": False,
                    }
                ],
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["issued"] is True, result
    assert "107" in _numbers(session)
    assert _has_pair(session, "PL1", "L2")
    assert _sheet(session).component("CR9").library_id == "nema.relays.cr"
    assert session.catalog.get("nema.relays.cr").primitives
    assert _sheet(session).component("PB2").library_id == kept
    assert "105" in _numbers(session)
    assert elementary_layout_issues(session, _sheet(session)) == []
    _left, right = rail_xs(_sheet(session))
    assert right - _right(session, "M1") <= 36.0
    assert right - _right(session, "PL1") <= 36.0


def test_uncertain_model_abstains(tmp_path: Path):
    session = _starter()
    before = len(_sheet(session).wires)
    vision = SpyVision(
        {
            "cleanup": {
                "wires": [{"gap": "wire:w4", "number": "?", "confidence": 0.99, "unreadable": False}],
                "connections": [
                    {"gap": "return:PL1", "to": "L2", "confidence": 0.4, "unreadable": False},
                    {"gap": "return:PL1", "to": "L2", "confidence": 0.99, "unreadable": True},
                ],
                "devices": [
                    {"tag": "FU9", "library_id": "nema.protection.fuse_10a", "confidence": 0.1, "unreadable": False}
                ],
            }
        }
    )
    # The ? wire's id is whatever the session assigned. Point the abstaining number at it.
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    vision.recordings["cleanup"]["wires"][0]["gap"] = f"wire:{wire.id}"
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["issued"] is False
    assert result["added"] == []
    assert wire.number == "?"
    assert len(_sheet(session).wires) == before
    assert not _has_pair(session, "PL1", "L2")
    assert all(comp.id != "FU9" for comp in _sheet(session).components)


def test_no_gaps_does_not_call_vision(tmp_path: Path):
    session = _starter()
    for wire in _sheet(session).wires:
        if wire.number == "?":
            wire.number = "107"
    session.wire("PL1.X2", "L2", number="104")
    assert remaining_gaps(session) == []
    vision = SpyVision({"cleanup": {"wires": [{"number": "999", "confidence": 0.99}]}})
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["called"] is False
    assert result["reason"] == "no gaps"
    assert vision.calls == []
    assert "999" not in _numbers(session)


def test_contacts_only_path_is_not_issued(tmp_path: Path):
    session = _ladder("Dead")
    session.place("nema.operators.estop", ref="ES1", x=40, y=80, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=80, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=120, y=80, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "PB2.1", number="105")
    session.wire("PB2.2", "PB1.3", number="?")
    session.wire("PB1.4", "L2", number="106")
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    vision = SpyVision(
        {
            "cleanup": {
                "wires": [{"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.95, "unreadable": False}],
                "connections": [
                    {"from": "PB1.4", "to": "L2", "confidence": 0.99, "unreadable": False},
                ],
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["issued"] is False
    assert result["reason"] == "checkers failed"
    # The blank number is a named gap and does not create the illegal rung.
    # The rung was already contacts-only, so the sheet still does not issue.
    assert "107" in {str(item.number or "") for item in _sheet(session).wires}
    assert any("no load" in item for item in elementary_layout_issues(session, _sheet(session)))
    assert any(row["reason"] == "not a named gap" for row in result["dropped"])


def test_through_symbol_reply_is_dropped(tmp_path: Path):
    session = _starter()
    before = [(wire.frm, wire.to) for wire in _sheet(session).wires]
    vision = SpyVision(
        {
            "cleanup": {
                "connections": [
                    {
                        "gap": "return:M1",
                        "from": "M1.A1",
                        "to": "M1.A2",
                        "confidence": 0.99,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "through symbol" for row in result["dropped"])
    assert [(wire.frm, wire.to) for wire in _sheet(session).wires] == before


def test_unknown_library_symbol_is_dropped(tmp_path: Path):
    session = _starter()
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {"tag": "ZZ9", "library_id": "made.up.coil", "confidence": 0.99, "unreadable": False}
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "not a catalog symbol" for row in result["dropped"])
    assert all(comp.id != "ZZ9" for comp in _sheet(session).components)


def test_confirmed_wire_number_is_not_replaced(tmp_path: Path):
    session = _starter()
    numbered = next(item for item in _sheet(session).wires if item.number == "105")
    vision = SpyVision(
        {
            "cleanup": {
                "wires": [
                    {
                        "from": numbered.frm,
                        "to": numbered.to,
                        "number": "999",
                        "confidence": 0.99,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert numbered.number == "105"
    assert any(row["reason"] in {"invented connection", "overwritten wire number"} for row in result["dropped"])


def test_no_vision_leaves_the_gaps(tmp_path: Path):
    session = _starter()
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=None)
    assert result["called"] is False
    assert result["reason"] == "no vision"
    assert any(gap["kind"] == "wire_number" for gap in result["gaps"])
    assert any(gap["kind"] == "return" and gap["tag"] == "PL1" for gap in result["gaps"])
    assert "?" in _numbers(session)


def test_unread_device_joins_the_rung_and_a_new_contact_does_not_tie_l2(tmp_path: Path):
    session = _ladder("Missing start")
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=220, y=100, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "PB2.1", number="?")
    session.wire("M1.A2", "L2", number="108")
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    vision = SpyVision(
        {
            "cleanup": {
                "wires": [{"gap": f"wire:{wire.id}", "number": "105", "confidence": 0.95, "unreadable": False}],
                "devices": [
                    {"tag": "PB1", "library_id": "nema.operators.pb_no", "gap": "unread_device:elementary", "confidence": 0.95, "unreadable": False},
                    {"tag": "S9", "library_id": "nema.operators.pb_no", "gap": "unread_device:elementary", "confidence": 0.95, "unreadable": False},
                ],
                "connections": [
                    {"gap": "unread_device:elementary", "from": "PB2.2", "to": "PB1.3", "number": "106", "confidence": 0.95, "unreadable": False},
                    {"gap": "unread_device:elementary", "from": "PB1.4", "to": "M1.A1", "number": "107", "confidence": 0.95, "unreadable": False},
                    {"gap": "unread_device:elementary", "from": "S9.4", "to": "L2", "confidence": 0.99, "unreadable": False},
                ],
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["issued"] is True, result
    assert _sheet(session).component("PB1").library_id.endswith("pb_no")
    assert _sheet(session).component("PB2").library_id.endswith("pb_nc")
    assert _has_pair(session, "PB2", "PB1")
    assert _has_pair(session, "PB1", "M1")
    assert not _has_pair(session, "S9", "L2")
    assert any(row["reason"] == "contacts-only" for row in result["dropped"])
    assert "105" in _numbers(session)


def test_a_floating_fuse_is_a_named_gap(tmp_path: Path):
    session = _starter()
    for wire in _sheet(session).wires:
        if not str(wire.number or "").strip() or wire.number == "?":
            wire.number = "107"
    session.wire("PL1.X2", "L2", number="104")
    session.place("nema.protection.fuse_10a", ref="FU1", x=70, y=40, snap=False)
    gaps = remaining_gaps(session)
    floating = [gap for gap in gaps if gap["kind"] == "unconnected"]
    assert [gap["tag"] for gap in floating] == ["FU1"]
    assert floating[0]["library_id"] == "nema.protection.fuse_10a"
    assert any(gap["kind"] == "unread_device" for gap in gaps)
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {"tag": "T1", "library_id": "nema.power.control_transformer", "gap": "unread_device:elementary", "confidence": 0.95, "unreadable": False}
                ],
                "connections": [
                    {"gap": "unconnected:FU1", "from": "L1", "to": "FU1.1", "number": "102", "confidence": 0.95, "unreadable": False},
                    {"from": "ES1.2", "to": "M1.A2", "confidence": 0.99, "unreadable": False},
                ],
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    invented = [
        row for row in result["dropped"] if row["reason"] in {"invented connection", "not a named gap"}
    ]
    assert invented
    assert all("FU1" not in str(row.get("item")) for row in invented)
    assert not any(row["reason"] == "invented device" for row in result["dropped"])
    assert any(row["reason"] == "no wiring" for row in result["dropped"])
    assert all(comp.id != "T1" for comp in _sheet(session).components)
    if result["issued"]:
        assert _has_pair(session, "L1", "FU1")


def test_orphan_contact_and_bad_tag_are_named_and_unconfirmed_becomes_unknown(tmp_path: Path):
    session = _ladder("Suspect")
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=100, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=300, y=100, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=120, y=140, snap=False)
    session.place("nema.operators.pb_no", ref="NVAN0", x=160, y=100, snap=False)
    session.place("nema.relays.cr", ref="KA1", x=340, y=100, snap=False)
    session.place("nema.operators.pb_no", ref="PB", x=200, y=140, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "PB2.1", number="105")
    session.wire("PB2.2", "CR1.A1", number="106")
    session.wire("CR1.A2", "L2", number="108")
    low = _sheet(session).component("PB2")
    low.properties["print_import_confidence"] = "0.20"
    gaps = remaining_gaps(session)
    kinds = {gap["kind"]: gap["tag"] for gap in gaps if gap.get("tag")}
    assert any(gap["kind"] == "orphan_contact" and gap["tag"] == "M1-13" for gap in gaps)
    assert any(gap["kind"] == "suspect_tag" and gap["tag"] == "NVAN0" for gap in gaps)
    assert any(gap["kind"] == "suspect_tag" and gap["tag"] == "PB2" and gap["reason"] == "low confidence" for gap in gaps)
    assert any(gap["kind"] == "untagged" and gap["tag"] == "PB" for gap in gaps)
    assert any(gap["kind"] == "coil_without_contacts" and gap["tag"] == "KA1" for gap in gaps)
    assert "ES1" not in {gap.get("tag") for gap in gaps if gap["kind"] in {"suspect_tag", "orphan_contact"}}
    vision = SpyVision({"cleanup": {"devices": [], "wires": [], "connections": []}})
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["calls"] == 1
    assert result["gaps_sent"] == len(gaps)
    # An empty reply does not blank a printed tag. Low confidence is only a question.
    tags = {comp.id for comp in _sheet(session).components}
    assert {"ES1", "PB2", "NVAN0", "M1-13", "KA1", "PB"} <= tags
    assert result["resolved"] == 0
    assert not any(item.get("kind") == "blank" for item in result["added"])


def test_a_dropped_page_is_a_named_gap():
    session = _ladder("Pages")
    session.place("nema.contactors.coil", ref="M1", x=80, y=80, snap=False)
    session.wire("M1.A2", "L2", number="108")
    gaps = remaining_gaps(
        session,
        [{"id": "power", "devices": 2, "wires": 3, "tags": ["Q1", "MTR"], "image": "power.png"}],
    )
    assert any(gap["kind"] == "dropped_page" and gap["sheet"] == "power" for gap in gaps)


def test_dropped_and_empty_pages_are_not_sent_and_still_block_issue(tmp_path: Path):
    """No fact answers these. They stay on the report and the model is not asked."""

    session = _starter()
    for wire in _sheet(session).wires:
        if wire.number == "?":
            wire.number = "107"
    session.wire("PL1.X2", "L2", number="104")
    pages = [{"id": "power", "devices": 2, "wires": 3, "tags": ["Q1", "MTR"], "image": "power.png"}]
    report = remaining_gaps(session, pages)
    assert any(gap["kind"] == "dropped_page" for gap in report)
    assert not any(gap["kind"] not in {"dropped_page", "empty_page"} for gap in report)
    vision = SpyVision({"cleanup": {}})
    alone = wrap_up_scan(session, image=_scan(tmp_path), backend=vision, pages=pages)
    assert alone["called"] is False
    assert alone["issued"] is False
    assert alone["reason"] == "report only"
    assert alone["gaps_sent"] == 0
    assert vision.calls == []
    assert any(gap["kind"] == "dropped_page" for gap in alone["gaps"])

    session.place("nema.contactors.coil", ref="M9", x=40, y=40, sheet_id="elementary", snap=False)
    bare = session.add_sheet("bare", title="Bare")
    bare.page.size = "TABLOID"
    bare.page.orientation = "landscape"
    bare.drawing_style = "ladder"
    session.place("nema.contactors.coil", ref="M8", x=40, y=40, sheet_id="bare", snap=False)
    vision.calls.clear()
    mixed = wrap_up_scan(session, image=_scan(tmp_path), backend=vision, pages=pages)
    assert mixed["called"] is True
    sent = {gap["kind"] for gap in vision.calls[0]["gaps"]}
    assert "dropped_page" not in sent and "empty_page" not in sent
    assert "unconnected" in sent
    reported = {gap["kind"] for gap in mixed["gaps"]}
    assert "dropped_page" in reported and "empty_page" in reported
    assert mixed["gaps_sent"] == len(vision.calls[0]["gaps"])
    assert mixed["issued"] is False


def test_replacing_a_good_tag_is_still_dropped(tmp_path: Path):
    session = _starter()
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {
                        "tag": "PB9",
                        "replaces": "ES1",
                        "library_id": "nema.operators.pb_no",
                        "confidence": 0.99,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "overwritten tag" for row in result["dropped"])
    assert _sheet(session).component("ES1").library_id.endswith("estop")


def test_terminal_return_can_land_and_a_contact_cannot(tmp_path: Path):
    session = _starter()
    for wire in _sheet(session).wires:
        if not str(wire.number or "").strip() or wire.number == "?":
            wire.number = "107"
    session.wire("PL1.X2", "L2", number="104")
    session.place("nema.terminals.feed_through", ref="X1-4", x=200, y=80, snap=False)
    gaps = remaining_gaps(session)
    assert any(gap.get("tag") == "X1-4" and gap["kind"] == "return" for gap in gaps)
    assert not any(gap.get("tag") == "PB1" for gap in gaps)
    vision = SpyVision(
        {
            "cleanup": {
                "connections": [
                    {"gap": "return:X1-4", "to": "L2", "number": "304", "confidence": 0.95, "unreadable": False},
                    {"gap": "return:PB1", "from": "PB1.4", "to": "L2", "confidence": 0.99, "unreadable": False},
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert result["issued"] is True, result
    assert _has_pair(session, "X1-4", "L2")
    assert any(row["reason"] in {"invented connection", "not a named gap"} for row in result["dropped"])
    assert _sheet(session).component("X1-4").library_id == "nema.terminals.feed_through"


@pytest.mark.parametrize(
    "phrase",
    [
        "Extract facts only",
        "pb_nc",
        "Unreadable stays",
        "layout engine redraws",
        "catalog library symbol",
        "no load",
        "gold Demo Controls",
    ],
)
def test_each_harness_rule_is_in_the_call_text(phrase: str):
    assert phrase in cleanup_harness()
    # The rule string is not only a test comment. The cleanup work copies it.
    session = _ladder()
    session.place("nema.contactors.coil", ref="M1", x=80, y=80, snap=False)
    session.wire("M1.A2", "L2", number="?")
    work_gaps = remaining_gaps(session)
    from panelcad.importer.cleanup import cleanup_work

    payload = cleanup_work(session, "scan.png", work_gaps)
    assert phrase in payload["instructions"]
    assert json.loads(work_json(payload))["instructions"] == cleanup_harness()


def test_a_new_device_without_a_named_gap_is_dropped(tmp_path: Path):
    session = _starter()
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {"tag": "M9", "library_id": "nema.contactors.coil", "confidence": 0.95, "unreadable": False}
                ],
                "connections": [
                    {"from": "M9.A2", "to": "L2", "number": "104", "confidence": 0.95, "unreadable": False}
                ],
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "not a named gap" for row in result["dropped"])
    assert all(comp.id != "M9" for comp in _sheet(session).components)


def test_cross_sheet_reference_is_rejected_and_a_failed_apply_keeps_the_sheets(tmp_path: Path, monkeypatch):
    session = _starter()
    session.add_sheet("page-001-p2", title="Page 2")
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40, sheet_id="page-001-p2", snap=False)
    before_ids = [sheet.id for sheet in session.project.sheets]
    before_wires = {sheet.id: len(sheet.wires) for sheet in session.project.sheets}
    vision = SpyVision(
        {
            "cleanup": {
                "connections": [
                    {
                        "gap": "return:PL1",
                        "from": "PB1.4",
                        "to": "FU1.1",
                        "number": "109",
                        "confidence": 0.95,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "cross sheet" for row in result["dropped"])
    assert [sheet.id for sheet in session.project.sheets] == before_ids
    assert session.project.sheet("page-001-p2").component("FU1").id == "FU1"

    def _boom(*_args, **_kwargs):
        raise KeyError("no component 'FU1' on sheet 'page-001'")

    monkeypatch.setattr(session, "wire", _boom)
    vision.recordings["cleanup"]["connections"] = [
        {
            "gap": "return:PL1",
            "from": "PL1.X2",
            "to": "L2",
            "number": "104",
            "confidence": 0.91,
            "unreadable": False,
        }
    ]
    failed = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "not applied" for row in failed["dropped"])
    assert [sheet.id for sheet in session.project.sheets] == before_ids
    assert {sheet.id: len(sheet.wires) for sheet in session.project.sheets} == before_wires


def test_one_bad_gap_does_not_drop_a_good_one_and_every_item_is_accounted(tmp_path: Path, monkeypatch):
    session = _starter()
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    real = session.wire

    def _wire(frm, to, *args, **kwargs):
        if "FU1" in {str(frm), str(to)}:
            raise KeyError("no component 'FU1' on sheet 'page-001'")
        return real(frm, to, *args, **kwargs)

    monkeypatch.setattr(session, "wire", _wire)
    reply = {
        "wires": [{"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.95, "unreadable": False}],
        "connections": [
            {"gap": "return:PL1", "from": "PL1.X2", "to": "L2", "number": "104", "confidence": 0.9, "unreadable": False},
            {"from": "CR1.A2", "to": "L2", "number": "104", "confidence": 0.84, "unreadable": False},
        ],
    }
    vision = SpyVision({"cleanup": reply})
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert wire.number == "107"
    assert result["calls"] == 1
    assert result["crops"] >= 1
    assert result["prompt_chars"] > 0
    accounted = list(result["added"]) + list(result["dropped"])
    assert len(accounted) >= len(reply["wires"]) + len(reply["connections"])
    assert any(row["reason"] == "not a named gap" for row in result["dropped"])


_PHONE_REPLY = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "phone-live-reply.json").read_text(encoding="utf-8")
)
_LEGIBLE = ("PB1", "PB2", "X1-1", "X1-2", "X1-3", "X1-4", "OL1", "CR1-13")


def test_phone_unsure_reply_does_not_blank_legible_tags(tmp_path: Path):
    """The home-PC phone reply said unreadable at 0.2. Those tags stay printed."""

    assert "Low confidence is a question" in cleanup_harness()
    session = _ladder("Phone")
    placed = {
        "ES1": "nema.operators.estop",
        "PB2": "nema.operators.pb_nc",
        "PB1": "nema.operators.pb_no",
        "CR1-13": "nema.contactors.aux_no",
        "X1-1": "nema.terminals.feed_through",
        "X1-2": "nema.terminals.feed_through",
        "X1-3": "nema.terminals.feed_through",
        "X1-4": "nema.terminals.feed_through",
        "OL1": "nema.protection.ol_contact",
        "M1-13": "nema.contactors.aux_no",
        "M1-23": "nema.contactors.aux_no",
    }
    for tag, library_id in placed.items():
        session.place(library_id, ref=tag, x=40, y=40, snap=False)
        comp = _sheet(session).component(tag)
        comp.properties["print_import_confidence"] = "0.20" if tag != "OL1" else "0.95"
    accepted, dropped = review_cleanup(session, _PHONE_REPLY["reply"], _PHONE_REPLY["gaps"])
    blanked = {item.get("tag") for item in accepted if item.get("kind") == "blank"}
    assert not blanked & set(_LEGIBLE)
    assert not blanked & {"M1-13", "M1-23"}
    assert any(row["reason"] == "unsure" and row["item"].get("tag") == "PB1" for row in dropped)
    vision = SpyVision({"cleanup": _PHONE_REPLY["reply"]})
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    tags = {comp.id for comp in _sheet(session).components}
    assert set(_LEGIBLE) <= tags
    assert not any(item.get("kind") == "blank" and item.get("tag") in _LEGIBLE for item in result["added"])
    assert result["resolved"] == sum(1 for item in result["added"] if item.get("kind") not in {"blank", "confirm"})


def test_a_confident_replacement_may_blank_a_nonstandard_tag_and_an_unsure_reply_may_not(tmp_path: Path):
    session = _ladder("Replace")
    session.place("nema.operators.pb_no", ref="NVAN0", x=40, y=40, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=40, snap=False)
    _sheet(session).component("PB2").properties["print_import_confidence"] = "0.20"
    session.wire("L1", "PB2.1", number="105")
    gaps = remaining_gaps(session)
    assert any(gap.get("tag") == "NVAN0" and gap.get("reason") == "nonstandard tag" for gap in gaps)
    assert any(gap.get("tag") == "PB2" and gap.get("reason") == "low confidence" for gap in gaps)
    unsure = {
        "devices": [
            {"tag": "PB2", "replaces": "PB2", "library_id": "nema.operators.pb_nc", "confidence": 0.2, "unreadable": True},
            {"tag": "?", "replaces": "NVAN0", "library_id": "nema.operators.pb_no", "confidence": 0.2, "unreadable": True},
        ]
    }
    accepted, dropped = review_cleanup(session, unsure, gaps)
    assert not any(item.get("kind") == "blank" for item in accepted)
    assert any(row["reason"] == "unsure" for row in dropped)
    sure = {
        "devices": [
            {
                "tag": "PB9",
                "replaces": "NVAN0",
                "library_id": "nema.operators.pb_no",
                "confidence": 0.92,
                "unreadable": False,
            }
        ]
    }
    accepted, _dropped = review_cleanup(session, sure, gaps)
    assert any(item.get("kind") == "retag" and item.get("old") == "NVAN0" and item.get("tag") == "PB9" for item in accepted)
    gone = {
        "devices": [
            {
                "tag": "?",
                "replaces": "NVAN0",
                "library_id": "nema.operators.pb_no",
                "confidence": 0.91,
                "unreadable": True,
            }
        ]
    }
    accepted, _dropped = review_cleanup(session, gone, gaps)
    assert any(
        item.get("kind") == "blank" and item.get("tag") == "NVAN0" and item.get("replaces_bad_read") for item in accepted
    )


def _challenge(gap_id: str, target: str, **extra) -> dict:
    item = {
        "gap": gap_id,
        "target": target,
        "reason": "The scan shows no such conductor.",
        "evidence": "The two pins do not meet on the scan.",
        "confidence": 0.92,
    }
    item.update(extra)
    return {"challenges": [item]}


def _self_short():
    session = _ladder("Short")
    session.place("nema.operators.estop", ref="ES1", x=40, y=80, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=220, y=80, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU2", x=80, y=40, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "M1.A1", number="105")
    session.wire("M1.A2", "L2", number="108")
    short = session.wire("FU2.1", "FU2.2", number="?")
    return session, short


def test_a_self_wire_challenge_clears_that_finding(tmp_path: Path):
    session, short = _self_short()
    sheet = _sheet(session)
    before = {(wire.id, wire.frm, wire.to) for wire in sheet.wires}
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "self_wire" and item.get("wire_id") == short.id)
    reply_path = Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "challenge-reply.json"
    recorded = load_cleanup_reply(reply_path)
    recorded["challenges"][0]["gap"] = gap["id"]
    recorded["challenges"][0]["target"] = short.id
    result = replay_cleanup(session, recorded, image=_scan(tmp_path))
    assert any(item.get("kind") == "challenge" and item.get("target") == short.id for item in result["added"]), result
    assert all(wire.id != short.id for wire in sheet.wires)
    assert not any("connects FU2's own pins" in item for item in elementary_layout_issues(session, sheet))
    assert {(wire.id, wire.frm, wire.to) for wire in sheet.wires} == {row for row in before if row[0] != short.id}
    assert result["issued"] is False or elementary_layout_issues(session, sheet) == []


def test_a_challenge_of_a_fact_no_gap_names_is_dropped(tmp_path: Path):
    session, short = _self_short()
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "self_wire")
    before = len(_sheet(session).wires)
    result = replay_cleanup(
        session,
        _challenge(gap["id"], "M1"),
        image=_scan(tmp_path),
    )
    assert any(row["reason"] == "not a named gap" for row in result["dropped"])
    assert all(wire.id != "M1" for wire in _sheet(session).wires)
    assert _sheet(session).component("M1").id == "M1"
    assert len(_sheet(session).wires) == before
    assert any(wire.id == short.id for wire in _sheet(session).wires)


def test_a_challenge_that_makes_the_checkers_worse_is_rolled_back(tmp_path: Path):
    session, short = _self_short()
    coil = _sheet(session).component("M1")
    coil.properties["print_import_confidence"] = "0.20"
    gap = next(item for item in remaining_gaps(session) if item.get("tag") == "M1" and item["kind"] == "coil_without_contacts")
    before = elementary_layout_issues(session, _sheet(session))
    result = replay_cleanup(session, _challenge(gap["id"], "M1", evidence="The circle is blank."), image=_scan(tmp_path))
    assert any(row["reason"] == "checkers worse" for row in result["dropped"]), result
    assert _sheet(session).component("M1").id == "M1"
    assert any(wire.id == short.id for wire in _sheet(session).wires)
    assert elementary_layout_issues(session, _sheet(session)) == before


def test_a_low_confidence_or_unsure_challenge_is_dropped(tmp_path: Path):
    session, short = _self_short()
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "self_wire")
    for extra in ({"confidence": 0.2}, {"confidence": 0.95, "unreadable": True}):
        fresh, wire = _self_short()
        named = next(item for item in remaining_gaps(fresh) if item["kind"] == "self_wire")
        result = replay_cleanup(fresh, _challenge(named["id"], wire.id, **extra), image=_scan(tmp_path))
        assert any(row["reason"] == "unsure" for row in result["dropped"])
        assert any(item.id == wire.id for item in _sheet(fresh).wires)
        assert result["added"] == [] or all(item.get("kind") != "challenge" for item in result["added"])
    assert gap["id"]


def test_a_challenge_cannot_add_or_move_a_wire(tmp_path: Path):
    session, short = _self_short()
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "self_wire")
    ends = {(wire.id, wire.frm, wire.to) for wire in _sheet(session).wires}
    result = replay_cleanup(
        session,
        _challenge(gap["id"], short.id, **{"from": "FU2.2", "to": "L2", "number": "109"}),
        image=_scan(tmp_path),
    )
    assert any(row["reason"] == "replacement" for row in result["dropped"])
    assert {(wire.id, wire.frm, wire.to) for wire in _sheet(session).wires} == ends
    assert not any(wire.frm == "FU2.2" and wire.to == "L2" for wire in _sheet(session).wires)


def test_a_fill_that_does_not_change_the_checker_count_is_kept(tmp_path: Path):
    session = _starter()
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    vision = SpyVision(
        {"cleanup": {"wires": [{"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.95, "unreadable": False}]}}
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert wire.number == "107"
    assert not any(row["reason"] == "not issued" for row in result["dropped"])
    assert any(item.get("kind") == "wire_number" and item.get("number") == "107" for item in result["added"])


def test_a_fill_that_adds_a_finding_is_rolled_back(tmp_path: Path):
    session = _starter()
    for wire in _sheet(session).wires:
        if wire.number == "?":
            wire.number = "107"
    session.place("nema.protection.fuse_10a", ref="FU2", x=70, y=40, snap=False)
    vision = SpyVision(
        {
            "cleanup": {
                "connections": [
                    {"gap": "unconnected:FU2", "from": "L1", "to": "FU2.1", "number": "101", "confidence": 0.95, "unreadable": False},
                    {"gap": "unconnected:FU2", "from": "FU2.2", "to": "L2", "number": "103", "confidence": 0.95, "unreadable": False},
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "not issued" and str(row["item"].get("from") or "") == "FU2.2" for row in result["dropped"]), result
    assert not any(wire.to == "L2" and str(wire.frm).startswith("FU2") for wire in _sheet(session).wires)
    assert not any("is a fuse from" in item for item in elementary_layout_issues(session, _sheet(session)))


def test_the_sheets_right_rail_is_accepted_and_an_unknown_rail_is_not(tmp_path: Path):
    session = _ladder("Phone rail")
    sheet = _sheet(session)
    sheet.rail_hot = "L1"
    sheet.rail_return = "X2"
    session.place("nema.operators.pilot_light", ref="PL1", x=180, y=80, snap=False)
    session.wire("L1", "PL1.X1", number="103")
    gaps = remaining_gaps(session)
    assert any(gap["kind"] == "return" and gap["tag"] == "PL1" for gap in gaps)
    accepted, dropped = review_cleanup(
        session,
        {
            "connections": [
                {"gap": "return:PL1", "from": "PL1.X2", "to": "X2", "number": "104", "confidence": 0.91, "unreadable": False},
                {"gap": "return:PL1", "from": "PL1.X2", "to": "L3", "number": "105", "confidence": 0.91, "unreadable": False},
            ]
        },
        gaps,
    )
    assert any(item.get("to") == "X2" and item.get("number") == "104" for item in accepted)
    assert any(row["reason"] == "invented connection" and row["item"].get("to") == "L3" for row in dropped)


def test_an_unwired_device_add_is_dropped(tmp_path: Path):
    session = _starter()
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {
                        "tag": "CR1",
                        "library_id": "nema.relays.cr",
                        "gap": "unread_device:elementary",
                        "confidence": 0.93,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    assert any(row["reason"] == "no wiring" for row in result["dropped"])
    assert all(comp.id != "CR1" for comp in _sheet(session).components)


def test_gap_crops_contain_their_geometry(tmp_path: Path):
    from PIL import Image

    from panelcad.importer.cleanup import _crop_box

    session = _ladder("Crops")
    sheet = _sheet(session)
    session.place("nema.operators.estop", ref="ES1", x=180, y=90, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=280, y=90, snap=False)
    session.wire("ES1.2", "M1.A1", number="?")
    image = Image.new("RGB", (900, 600), "white")
    image.save(tmp_path / "page.png")
    gaps = remaining_gaps(session)
    wire_gap = next(gap for gap in gaps if gap["kind"] == "wire_number")
    symbol = {"kind": "suspect_tag", "tag": "ES1", "sheet": sheet.id}
    wire_box = _crop_box(session, wire_gap, image)
    symbol_box = _crop_box(session, symbol, image)
    width_mm, height_mm = sheet.page.dimensions()
    ppm_x = image.size[0] / float(width_mm)
    ppm_y = image.size[1] / float(height_mm)
    device = session.catalog.get("nema.operators.estop")
    center = ((180 + device.schematic_width / 2) * ppm_x, (90 + device.schematic_height / 2) * ppm_y)
    assert wire_box[0] > 20, wire_box
    assert symbol_box[0] <= center[0] <= symbol_box[2]
    assert symbol_box[1] <= center[1] <= symbol_box[3]
    assert wire_box[0] <= center[0] <= wire_box[2]


def test_phone_return_does_not_rewrite_other_wire_numbers(tmp_path: Path):
    session = _ladder("Phone numbers")
    sheet = _sheet(session)
    sheet.rail_hot = "L1"
    sheet.rail_return = "X2"
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=180, y=80, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=240, y=120, snap=False)
    kept = {
        session.wire("L1", "FU1.1", number="102").id: "102",
        session.wire("FU1.2", "X2", number="103").id: "103",
        session.wire("L1", "M1.A1", number="108").id: "108",
        session.wire("M1.A2", "X2", number="109").id: "109",
        session.wire("L1", "PL1.X1", number="201").id: "201",
    }
    vision = SpyVision(
        {
            "cleanup": {
                "connections": [
                    {
                        "gap": "return:PL1",
                        "from": "PL1.X2",
                        "to": "X2",
                        "number": "104",
                        "confidence": 0.91,
                        "unreadable": False,
                    }
                ]
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    numbers = {wire.id: str(wire.number or "") for wire in _sheet(session).wires}
    for wire_id, number in kept.items():
        assert numbers[wire_id] == number
    assert any(str(wire.number or "") == "104" and "PL1" in f"{wire.frm}{wire.to}" for wire in _sheet(session).wires)
    assert result.get("violations", 0) == 0
    recorded = load_cleanup_reply(
        Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "phone-live-reply.json"
    )
    blank = session.wire("FU1.2", "M1.A1", number="?")
    blank.id = "page-001_w13"
    before = {wire.id: str(wire.number or "") for wire in _sheet(session).wires}
    replay_cleanup(session, recorded, image=_scan(tmp_path))
    after = {wire.id: str(wire.number or "") for wire in _sheet(session).wires}
    for wire_id, number in before.items():
        if wire_id == "page-001_w13":
            continue
        assert after.get(wire_id) == number


def test_two_new_devices_that_share_a_wire_are_one_unit(tmp_path: Path):
    session = _starter()
    vision = SpyVision(
        {
            "cleanup": {
                "devices": [
                    {
                        "tag": "T1",
                        "library_id": "nema.power.control_transformer",
                        "gap": "unread_device:elementary",
                        "confidence": 0.95,
                        "unreadable": False,
                    },
                    {
                        "tag": "CR1-13",
                        "library_id": "nema.contactors.aux_no",
                        "gap": "unread_device:elementary",
                        "confidence": 0.95,
                        "unreadable": False,
                    },
                ],
                "connections": [
                    {
                        "gap": "unread_device:elementary",
                        "from": "T1.X1",
                        "to": "CR1-13.3",
                        "number": "110",
                        "confidence": 0.95,
                        "unreadable": False,
                    }
                ],
            }
        }
    )
    result = wrap_up_scan(session, image=_scan(tmp_path), backend=vision)
    tags = {comp.id for comp in _sheet(session).components}
    assert ("T1" in tags) == ("CR1-13" in tags)
    assert not any("no component" in str(row.get("error") or "") for row in result["dropped"])
    if "T1" in tags:
        assert _has_pair(session, "T1", "CR1-13")
    else:
        assert any(row["reason"] in {"not issued", "no wiring"} for row in result["dropped"])


def test_a_self_wire_crop_follows_that_wires_ends(tmp_path: Path):
    from PIL import Image

    from panelcad.core.models import Point
    from panelcad.importer.cleanup import _crop_box

    session = _ladder("Crop short")
    sheet = _sheet(session)
    session.place("nema.operators.estop", ref="ES1", x=40, y=40, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=40, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU2", x=260, y=140, snap=False)
    short = session.wire("FU2.1", "FU2.2", number="?")
    short.points = [Point(x=40, y=40), Point(x=80, y=40), Point(x=260, y=140)]
    image = Image.new("RGB", (900, 600), "white")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "self_wire")
    box = _crop_box(session, gap, image)
    width_mm, height_mm = sheet.page.dimensions()
    ppm_x = image.size[0] / float(width_mm)
    ppm_y = image.size[1] / float(height_mm)
    device = session.catalog.get("nema.protection.fuse_10a")
    x0, y0 = 260 * ppm_x, 140 * ppm_y
    x1 = (260 + device.schematic_width) * ppm_x
    y1 = (140 + device.schematic_height) * ppm_y
    assert box[0] <= x0 and box[2] >= x1
    assert box[1] <= y0 and box[3] >= y1
    es1 = (50 * ppm_x, 50 * ppm_y)
    assert not (box[0] <= es1[0] <= box[2] and box[1] <= es1[1] <= box[3])


def test_a_challenge_removes_a_phantom_device_and_its_dangling_wire(tmp_path: Path):
    session = _ladder("Phantom")
    session.place("nema.operators.estop", ref="ES1", x=40, y=80, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=220, y=80, snap=False)
    session.place("nema.contactors.coil", ref="KA1", x=120, y=140, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "M1.A1", number="105")
    session.wire("M1.A2", "L2", number="108")
    session.wire("L1", "KA1.A1", number="440")
    dangling = session.wire("KA1.A2", "L2", number="441")
    from panelcad.importer.cleanup import _rename_tag

    assert _rename_tag(session, "KA1", "?", "")
    phantom = next(comp.id for comp in _sheet(session).components if comp.id.startswith("?"))
    gap = next(item for item in remaining_gaps(session) if item.get("tag") == phantom)
    kept = {wire.id: str(wire.number or "") for wire in _sheet(session).wires if wire.id != dangling.id}
    result = replay_cleanup(
        session,
        _challenge(
            gap["id"],
            phantom,
            reason="KA1 is not on the scan",
            evidence="The circle is blank.",
            confidence=0.91,
        ),
        image=_scan(tmp_path),
    )
    tags = {comp.id for comp in _sheet(session).components}
    assert phantom not in tags
    assert all(comp.id != "KA1" for comp in _sheet(session).components)
    assert all(wire.id != dangling.id for wire in _sheet(session).wires)
    numbers = {wire.id: str(wire.number or "") for wire in _sheet(session).wires}
    for wire_id, number in kept.items():
        if wire_id in numbers:
            assert numbers[wire_id] == number
    assert any(item.get("kind") == "challenge" for item in result["added"]), result


def test_self_wire_crop_uses_the_scan_position_not_the_layout(tmp_path: Path):
    from PIL import Image

    from panelcad.importer.cleanup import _crop_box

    source = Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "schematic" / "clean_300.png"
    image = Image.open(source)
    image.load()
    session = _ladder("Scan frame")
    sheet = _sheet(session)
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    # Laid-out position maps to the old crop [4110, 2192, 4596, 2481] on this scan.
    session.place("nema.protection.fuse_10a", ref="FU2", x=354.0, y=191.6, snap=False)
    fuse = sheet.component("FU2")
    fuse.properties["print_import_x"] = 70.4
    fuse.properties["print_import_y"] = 57.2
    session.wire("FU2.1", "FU2.2", number="?")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "self_wire")
    box = _crop_box(session, gap, image)
    ppm = 300.0 / 25.4
    device = session.catalog.get("nema.protection.fuse_10a")
    left = 70.4 * ppm
    top = 57.2 * ppm
    right = (70.4 + device.schematic_width) * ppm
    bottom = (57.2 + device.schematic_height) * ppm
    assert box[0] <= left and box[2] >= right, box
    assert box[1] <= top and box[3] >= bottom, box
    assert box != (4110, 2192, 4596, 2481)


def test_a_return_does_not_tee_the_primary_onto_the_rail(tmp_path: Path):
    session = _ladder("Primary short")
    sheet = _sheet(session)
    sheet.rail_hot = "L1"
    sheet.rail_return = "X2"
    session.place("nema.protection.fuse_10a", ref="FU2", x=40, y=40, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=100, y=40, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=220, y=120, snap=False)
    session.wire("L1", "FU2.1", number="100")
    from panelcad.importer.cleanup import _apply

    _apply(
        session,
        [
            {"kind": "return", "from": "FU2.2", "to": "T1.H4", "number": "101"},
            {"kind": "return", "from": "T1.H4", "to": "X2", "number": "104"},
            {"kind": "return", "from": "PL1.X2", "to": "X2", "number": "104"},
        ],
    )
    numbers = {wire.id: (wire.frm, wire.to, str(wire.number or "")) for wire in sheet.wires}
    assert any(pair[2] == "101" and "T1.H4" in {pair[0], pair[1]} and "X2" not in {pair[0], pair[1]} for pair in numbers.values())
    assert not any(
        "T1.H4" in {frm, to} and "X2" in {frm, to} for frm, to, _number in numbers.values()
    )
    assert any("PL1" in f"{frm}{to}" and "X2" in {frm, to} for frm, to, _number in numbers.values())
    text = " ".join(elementary_layout_issues(session, sheet))
    assert "ties the primary" not in text
    assert "two wire numbers" not in text


def test_reader_mistakes_are_asked_and_not_rewritten(tmp_path: Path):
    session = _ladder("Asked")
    sheet = _sheet(session)
    session.place("nema.power.control_transformer", ref="T1", x=40, y=10, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=50, y=36, snap=False)
    session.place("nema.contactors.coil", ref="M1_2", x=180, y=230, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=150, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-23", x=200, y=150, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=180, y=160, snap=False)
    # 16 mm is still one row (a coil anchor sits below the contact). 12 mm split it.
    # The next row is 30 mm down, which is where the family member stays.
    sheet.component("PL1").properties.update({"print_import_x": 21.0, "print_import_y": 104.0})
    sheet.component("M1-23").properties.update({"print_import_x": 25.0, "print_import_y": 88.0})
    sheet.component("M1-13").properties.update({"print_import_x": 40.0, "print_import_y": 160.0})
    session.place("nema.contactors.coil", ref="CR1", x=300, y=120, snap=False)
    session.place("nema.contactors.aux_no", ref="CR1-13", x=62, y=36, snap=False)
    sheet.component("CR1-13").properties.update({"print_import_x": 62.0, "print_import_y": 52.0})
    session.place("nema.contactors.aux_no", ref="M1-33", x=290, y=136, snap=False)
    sheet.component("M1-33").properties.update({"print_import_x": 290.0, "print_import_y": 136.0})
    sheet.component("CR1").properties.update({"print_import_x": 300.0, "print_import_y": 120.0})
    # 104 shares no second number with ES1 or PB2. 100 is the other number on that row.
    session.place("nema.operators.estop", ref="ES1", x=80, y=200, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=130, y=210, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=40, y=200, snap=False)
    session.place("nema.terminals.feed_through", ref="X1", x=20, y=230, snap=False)
    session.wire("L1", "PB1.3", number="100")
    wrong_104 = session.wire("ES1.2", "PB2.1", number="104")
    session.wire("X1.A", "M1_2.A1", number="110")
    session.wire("M1_2.A2", "L2", number="115")
    session.place("nema.operators.pb_no", ref="PB9", x=170, y=150, snap=False)
    session.wire("PB9.4", "PL1.X1", number="103")
    wrong_107 = session.wire("PL1.X2", "L2", number="107")
    session.wire("M1.A2", "L2", number="106")
    before = {(wire.id, wire.frm, wire.to, str(wire.number or "")) for wire in sheet.wires}
    tags = {comp.id for comp in sheet.components}
    kinds = {gap["kind"] for gap in remaining_gaps(session)}
    assert {"duplicate_tag", "coil_place", "skipped_contacts", "suspect_number"} <= kinds
    asked = {gap.get("wire_id") for gap in remaining_gaps(session) if gap["kind"] == "suspect_number"}
    assert wrong_104.id in asked
    assert wrong_107.id in asked
    dup = [gap for gap in remaining_gaps(session) if gap["kind"] == "duplicate_tag" and gap.get("tag") == "M1-23"]
    assert dup
    beside = [gap for gap in remaining_gaps(session) if gap.get("read") == "CR1-13 is beside M1"]
    assert beside and beside[0]["id"] == "duplicate_tag:elementary:CR1-13:M1"
    m133 = [gap for gap in remaining_gaps(session) if gap.get("read") == "M1-33 is beside CR1"]
    assert m133 and m133[0]["id"] == "duplicate_tag:elementary:M1-33:CR1"
    assert {(wire.id, wire.frm, wire.to, str(wire.number or "")) for wire in sheet.wires} == before
    assert {comp.id for comp in sheet.components} == tags


def test_a_printed_return_stays_and_a_rail_name_is_not_a_suspect_number():
    """107 printed on PL1's return is not replaced by L2, and L1/L2 are not questions."""

    session = _ladder("Printed return")
    session.place("nema.operators.pilot_light", ref="PL1", x=80, y=40, snap=False)
    session.place("nema.protection.ol_contact", ref="OL1", x=140, y=40, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=180, y=40, snap=False)
    kept = session.wire("PL1.X2", "L2", number="107")
    session.wire("PL1.X1", "M1.A2", number="103")
    session.wire("M1.A2", "OL1.95", number="108")
    wrong_104 = session.wire("OL1.96", "L2", number="104")
    session.wire("L1", "M1.A1", number="101")
    session.wire("L1", "PL1.X1")
    session.number_wires(overwrite=True)
    assert kept.number == "107"
    assert kept.number_locked is True
    assert all(str(wire.number or "") != "L2" for wire in _sheet(session).wires)
    gaps = remaining_gaps(session)
    asked = {gap.get("wire_id") for gap in gaps if gap["kind"] == "suspect_number"}
    assert kept.id in asked
    assert wrong_104.id in asked
    assert not any(str(gap.get("number") or "") in {"L1", "L2", "101"} for gap in gaps if gap["kind"] == "suspect_number")


def test_an_unattached_low_confidence_number_is_asked_and_not_written():
    """OCR saw 107 with no from/to. The wire stays L2, and the token is a question."""

    from PIL import Image

    from panelcad.importer.cleanup import _crop_box, review_cleanup

    session = _ladder("Unattached 107")
    sheet = _sheet(session)
    session.place("nema.operators.pilot_light", ref="PL1", x=110, y=130, snap=False)
    sheet.component("PL1").properties.update({"print_import_x": 110.0, "print_import_y": 130.0})
    wire = session.wire("PL1.X2", "L2")
    wire.number = "L2"
    wire.number_locked = False
    session.project.properties["print_import"] = {
        "items": [
            {
                "kind": "wire",
                "text": "107",
                "sheet": sheet.id,
                "reason": "ambiguous",
                "confidence": 0.4,
                "x": 131.05,
                "y": 139.82,
            },
            {
                "kind": "wire",
                "text": "108",
                "sheet": sheet.id,
                "reason": "ambiguous",
                "confidence": 0.4,
                "x": 20.0,
                "y": 20.0,
            },
        ]
    }
    gaps = [gap for gap in remaining_gaps(session) if gap["kind"] == "unattached_number"]
    assert [gap["number"] for gap in gaps] == ["107"]
    assert gaps[0]["wire_id"] == wire.id
    assert gaps[0]["id"] == "unattached_number:elementary:107:131.1:139.8"
    assert "not attached" in gaps[0]["read"]
    assert wire.number == "L2"
    image = Image.new("RGB", (800, 520), (255, 255, 255))
    box = _crop_box(session, gaps[0], image)
    assert box[2] > box[0] and box[3] > box[1]
    accepted, dropped = review_cleanup(
        session,
        {"wires": [{"gap": gaps[0]["id"], "number": "107", "confidence": 0.95, "unreadable": False}]},
        remaining_gaps(session),
    )
    assert wire.number == "L2"
    assert any(row["reason"] == "invented connection" for row in dropped)
    assert not any(item.get("number") == "107" for item in accepted)


def test_an_unattached_number_attaches_only_the_printed_digits(tmp_path: Path):
    """The crop answer may put 107 on the named wire. A different number, or a numbered wire, is dropped."""

    session = _ladder("Attach 107")
    sheet = _sheet(session)
    session.place("nema.operators.pilot_light", ref="PL1", x=110, y=130, snap=False)
    sheet.component("PL1").properties.update({"print_import_x": 110.0, "print_import_y": 130.0})
    wire = session.wire("PL1.X2", "L2")
    wire.number = "L2"
    wire.number_locked = False
    other = session.wire("PL1.X1", "L1", number="105")
    session.project.properties["print_import"] = {
        "items": [
            {"kind": "wire", "text": "107", "sheet": sheet.id, "reason": "ambiguous", "confidence": 0.4, "x": 131.05, "y": 139.82}
        ]
    }
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "unattached_number")
    rejected, dropped = review_cleanup(
        session,
        {"attaches": [
            {"gap": gap["id"], "attach": wire.id, "number": "108", "confidence": 0.95},
            {"gap": gap["id"], "attach": other.id, "number": "107", "confidence": 0.95},
            {"gap": gap["id"], "attach": wire.id, "number": "107", "confidence": 0.2},
        ]},
        remaining_gaps(session),
    )
    assert rejected == []
    assert {row["reason"] for row in dropped} >= {"not the printed number", "not a named gap", "unsure"}
    assert wire.number == "L2"
    result = replay_cleanup(
        session,
        {"attaches": [{"gap": gap["id"], "attach": wire.id, "number": "107", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "attach" and item.get("number") == "107" for item in result["added"]), result
    assert wire.number == "107"
    assert wire.number_locked is True
    assert other.number == "105"
    wire.number = "105"
    taken, denied = review_cleanup(
        session,
        {"attaches": [{"gap": gap["id"], "attach": wire.id, "number": "107", "confidence": 0.95}]},
        [gap],
    )
    assert taken == []
    assert any(row["reason"] == "overwritten wire number" for row in denied)
    assert wire.number == "105"


def test_a_duplicate_tag_retag_must_name_a_device_already_on_the_sheet(tmp_path: Path):
    session = _ladder("Retag")
    sheet = _sheet(session)
    session.place("nema.contactors.aux_no", ref="M1-23", x=10, y=160, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=50, y=164, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=10, y=210, snap=False)
    sheet.component("M1-23").properties.update({"print_import_x": 10.0, "print_import_y": 160.0})
    sheet.component("CR1").properties.update({"print_import_x": 50.14, "print_import_y": 163.7})
    sheet.component("M1").properties.update({"print_import_x": 10.0, "print_import_y": 210.0})
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "duplicate_tag" and item.get("tag") == "M1-23")
    rejected, dropped = review_cleanup(
        session,
        {"answers": [
            {"gap": gap["id"], "retag": "ZZ9-23", "confidence": 0.95},
            {"gap": gap["id"], "retag": "CR1-23", "confidence": 0.2},
        ]},
        remaining_gaps(session),
    )
    assert rejected == []
    assert {row["reason"] for row in dropped} >= {"invented device", "unsure"}
    result = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "retag": "CR1-23", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "retag" and item.get("tag") == "CR1-23" for item in result["added"]), result
    assert _sheet(session).component("CR1-23").library_id.endswith("aux_no")
    assert all(comp.id != "M1-23" for comp in _sheet(session).components)


def test_a_suspect_number_may_be_cleared_and_not_replaced(tmp_path: Path):
    session = _ladder("Clear 104")
    session.place("nema.operators.estop", ref="ES1", x=40, y=80, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=90, y=80, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=40, y=80, snap=False)
    session.wire("L1", "PB1.3", number="100")
    wrong = session.wire("ES1.2", "PB2.1", number="104")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "suspect_number" and item.get("wire_id") == wrong.id)
    rejected, dropped = review_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "ES1", "confidence": 0.95}]},
        remaining_gaps(session),
    )
    assert rejected == []
    assert any(row["reason"] == "guessed wire number" for row in dropped)
    assert wrong.number == "104"
    result = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "?", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "clear_number" and item.get("wire_id") == wrong.id for item in result["added"]), result
    assert wrong.number == "?"
    again = _ladder("Replace 104")
    again.place("nema.operators.estop", ref="ES1", x=40, y=80, snap=False)
    again.place("nema.operators.pb_nc", ref="PB2", x=90, y=80, snap=False)
    again.place("nema.operators.pb_no", ref="PB1", x=40, y=80, snap=False)
    again.wire("L1", "PB1.3", number="100")
    printed = again.wire("ES1.2", "PB2.1", number="104")
    again.project.properties["print_import"] = {
        "items": [{"kind": "wire", "text": "105", "sheet": "elementary", "x": 65.0, "y": 80.0, "confidence": 0.4, "reason": "ambiguous"}]
    }
    named = next(item for item in remaining_gaps(again) if item["kind"] == "suspect_number" and item.get("wire_id") == printed.id)
    replaced = replay_cleanup(
        again,
        {"answers": [{"gap": named["id"], "number": "105", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "replace_number" and item.get("number") == "105" for item in replaced["added"]), replaced
    assert printed.number == "105"


def test_a_read_of_100_may_be_corrected_to_105_and_106_on_104_may_not(tmp_path: Path):
    """dpi_150 w12 is the reader's 100 where the print shows 105. No loose 105 exists.

    0 and 5 are confusable, so 105 is the same token. 4 and 6 are not, so 106
    cannot replace the 104 on w7. 100 to 102 is not confusable either.
    """

    session = _ladder("dpi_150 numbers")
    sheet = _sheet(session)
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=100, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=120, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=260, y=100, snap=False)
    session.place("nema.protection.ol_contact", ref="OL1", x=300, y=100, snap=False)
    session.wire("PB2.2", "PB1.3", number="106")
    session.wire("M1.A2", "OL1.95", number="108")
    w7 = session.wire("OL1.96", "L2", number="104", wire_id="page-001_w7")
    w12 = session.wire("ES1.2", "PB2.1", number="100", wire_id="page-001_w12")
    gaps = remaining_gaps(session)
    gap7 = next(item for item in gaps if item.get("wire_id") == w7.id)
    gap12 = next(item for item in gaps if item.get("wire_id") == w12.id)
    _accepted, dropped = review_cleanup(
        session,
        {"answers": [
            {"gap": gap7["id"], "number": "106", "confidence": 0.95},
            {"gap": gap12["id"], "number": "102", "confidence": 0.95},
        ]},
        gaps,
    )
    assert _accepted == []
    assert {row["reason"] for row in dropped} == {"guessed wire number"}
    assert w7.number == "104"
    assert w12.number == "100"
    # 100 and 108 differ by a confusable digit, but 108 already names M1–OL1.
    crossed = replay_cleanup(
        session,
        {"answers": [{"gap": gap12["id"], "number": "108", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert next(wire.number for wire in _sheet(session).wires if wire.id == w12.id) == "100"
    assert any(row.get("reason") == "not issued" for row in crossed["dropped"]), crossed
    kept = replay_cleanup(
        session,
        {"answers": [{"gap": gap12["id"], "number": "105", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "replace_number" and item.get("number") == "105" for item in kept["added"]), kept
    assert next(wire.number for wire in _sheet(session).wires if wire.id == w12.id) == "105"


def test_a_renamed_landing_finding_does_not_roll_back_the_retag(tmp_path: Path):
    """M1-23 already fails to land on L1. Renaming it to CR1-13 keeps that finding."""

    session = _ladder("Retag landing")
    sheet = _sheet(session)
    session.place("nema.contactors.aux_no", ref="M1-23", x=30, y=80, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=80, y=84, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=300, y=160, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=300, y=40, snap=False)
    sheet.component("M1-23").properties.update({"print_import_x": 30.0, "print_import_y": 80.0})
    sheet.component("PL1").properties.update({"print_import_x": 80.0, "print_import_y": 84.0})
    session.wire("L1", "M1-23.4", number="102")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "duplicate_tag" and item.get("tag") == "M1-23")
    result = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "retag": "CR1-13", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "retag" and item.get("tag") == "CR1-13" for item in result["added"]), result
    assert _sheet(session).component("CR1-13").library_id.endswith("aux_no")


def test_a_wire_into_a_phantom_may_be_removed_when_findings_do_not_change(tmp_path: Path):
    session = _ladder("Phantom wire")
    session.place("nema.contactors.coil", ref="M1", x=200, y=80, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=40, y=120, snap=False)
    session.place("nema.contactors.coil", ref="M1_2", x=80, y=200, snap=False)
    session.wire("L1", "M1.A1", number="102")
    session.wire("M1.A2", "L2", number="108")
    session.wire("L1", "M1-13.3", number="106")
    phantom = session.wire("L1", "M1_2.A1", number="110", wire_id="page-001_w20")
    gaps = remaining_gaps(session)
    assert any(gap.get("tag") == "M1_2" for gap in gaps)
    gap = next(item for item in gaps if item.get("wire_id") == phantom.id or phantom.id in (item.get("wires") or []))
    result = replay_cleanup(session, _challenge(gap["id"], phantom.id), image=_scan(tmp_path))
    assert any(item.get("kind") == "challenge" and item.get("target") == phantom.id for item in result["added"]), result
    assert all(wire.id != phantom.id for wire in _sheet(session).wires)
    assert _sheet(session).component("M1_2").id == "M1_2"


def test_an_open_secondary_or_contact_pin_blocks_issue(tmp_path: Path):
    """The phone sheet added T1 and left X1/X2 and the seal-in open. It must not issue."""

    from panelcad.importer.cleanup import _completion_issues, _issue

    session = _ladder("Phone incomplete")
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=20, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=100, y=20, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=120, y=100, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=120, y=140, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=300, y=100, snap=False)
    session.wire("L1", "FU1.1", number="102")
    session.wire("FU1.2", "T1.H1", number="103")
    session.wire("L1", "PB1.3", number="106")
    session.wire("PB1.4", "M1.A1", number="107")
    session.wire("M1.A2", "L2", number="108")
    session.wire("PB1.4", "M1-13.3", number="106")
    text = "\n".join(_completion_issues(session))
    assert "T1 secondary does not feed a wire" in text
    assert "T1 pin H4 does not land" in text
    assert "M1-13 pin 4 does not land" in text
    assert _issue(session, set()) is False
    session.wire("T1.H4", "L2", number="101")
    session.wire("T1.X1", "L1", number="102")
    session.wire("T1.X2", "L2", number="104")
    session.wire("M1-13.4", "PB1.4", number="107")
    assert not any("secondary" in item or "M1-13 pin" in item or "pin H4" in item for item in _completion_issues(session))


def test_a_new_finding_is_not_an_improvement_when_the_count_falls():
    from panelcad.importer.cleanup import _checkers_better

    assert _checkers_better({"old-a", "old-b"}, {"fresh"}) is False
    assert _checkers_better({"old-a", "old-b"}, {"old-a"}) is True


def test_the_ansi_baseline_is_taken_before_cleanup_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A finding the facts add is compared with the sheet from before those facts."""

    from panelcad.core.ansi_drafting import ansi_drafting_issues as real_ansi

    calls = {"n": 0}

    def wrapped(project, catalog):
        calls["n"] += 1
        found = list(real_ansi(project, catalog))
        if calls["n"] > 1:
            found.append("ansi: added after the facts")
        return found

    monkeypatch.setattr("panelcad.core.ansi_drafting.ansi_drafting_issues", wrapped)
    session = _starter()
    wire = next(item for item in _sheet(session).wires if item.number == "?")
    result = wrap_up_scan(
        session,
        image=_scan(tmp_path),
        backend=SpyVision(
            {"cleanup": {"wires": [{"gap": f"wire:{wire.id}", "number": "107", "confidence": 0.95, "unreadable": False}]}}
        ),
    )
    assert wire.number == "107"
    assert result["issued"] is False
    assert calls["n"] >= 2


def test_a_coil_place_challenge_is_kept_only_when_no_new_finding_appears(tmp_path: Path):
    session = _ladder("Coil place")
    session.place("nema.power.control_transformer", ref="T1", x=40, y=40, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=48, y=48, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=40, y=160, snap=False)
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "coil_place" and item.get("tag") == "M1")
    kept = replay_cleanup(
        session,
        _challenge(gap["id"], "M1", evidence="This coil is not on the scan."),
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "challenge" and item.get("target") == "M1" for item in kept["added"]), kept
    assert all(comp.id != "M1" for comp in _sheet(session).components)
    fresh = _ladder("Coil place rejected")
    fresh.place("nema.power.control_transformer", ref="T1", x=40, y=40, snap=False)
    fresh.place("nema.contactors.coil", ref="M1", x=48, y=48, snap=False)
    fresh.place("nema.contactors.aux_no", ref="M1-13", x=40, y=160, snap=False)
    named = next(item for item in remaining_gaps(fresh) if item["kind"] == "coil_place")
    rejected = replay_cleanup(fresh, _challenge(named["id"], "M1"), image=_scan(tmp_path))
    assert any(row["reason"] == "checkers worse" for row in rejected["dropped"]), rejected
    assert fresh.project.sheet("elementary").component("M1").id == "M1"


def test_a_skipped_contact_wire_is_removed_only_when_the_checkers_improve(tmp_path: Path):
    session = _ladder("Skipped")
    session.place("nema.contactors.aux_no", ref="CR1-13", x=40, y=120, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=200, y=40, snap=False)
    skipped = session.wire("L1", "CR1.A1", number="104")
    session.wire("CR1.A1", "CR1-13.3", number="112")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "skipped_contacts" and item.get("wire_id") == skipped.id)
    kept = replay_cleanup(session, _challenge(gap["id"], skipped.id), image=_scan(tmp_path))
    assert any(item.get("kind") == "challenge" and item.get("target") == skipped.id for item in kept["added"]), kept
    assert all(wire.id != skipped.id for wire in _sheet(session).wires)
    assert not any("two wire numbers" in item for item in elementary_layout_issues(session, _sheet(session)))
    fresh = _ladder("Skipped rejected")
    fresh.place("nema.contactors.aux_no", ref="CR1-13", x=40, y=120, snap=False)
    fresh.place("nema.contactors.coil", ref="CR1", x=200, y=40, snap=False)
    left = fresh.wire("L1", "CR1.A1", number="110")
    fresh.wire("CR1.A2", "L2", number="111")
    named = next(item for item in remaining_gaps(fresh) if item["kind"] == "skipped_contacts" and item.get("wire_id") == left.id)
    rejected = replay_cleanup(fresh, _challenge(named["id"], left.id), image=_scan(tmp_path))
    assert any(row["reason"] == "checkers worse" for row in rejected["dropped"]), rejected
    assert any(wire.id == left.id for wire in fresh.project.sheet("elementary").wires)


def test_the_next_device_on_the_row_is_beside_past_forty_millimetres():
    """M1-23 and CR1 are the adjacent symbols. 40.14 mm is still beside."""

    session = _ladder("Beside")
    sheet = _sheet(session)
    session.place("nema.contactors.aux_no", ref="M1-23", x=10, y=160, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=50, y=164, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=10, y=200, snap=False)
    sheet.component("M1-23").properties.update({"print_import_x": 10.0, "print_import_y": 160.0})
    sheet.component("CR1").properties.update({"print_import_x": 50.14, "print_import_y": 163.7})
    sheet.component("M1").properties.update({"print_import_x": 10.0, "print_import_y": 200.0})
    gaps = [gap for gap in remaining_gaps(session) if gap["kind"] == "duplicate_tag" and gap.get("tag") == "M1-23"]
    assert gaps and gaps[0]["read"] == "M1-23 is beside CR1"
    assert gaps[0]["id"] == "duplicate_tag:elementary:M1-23:CR1"


def test_a_terminal_on_the_row_is_not_the_device_beside_a_contact():
    """M1-33 sits on the bottom row, far left of the X1 strip. That is not a rung mistake."""

    session = _ladder("Terminal row")
    sheet = _sheet(session)
    session.place("nema.contactors.aux_no", ref="M1-33", x=40, y=200, snap=False)
    session.place("nema.terminals.feed_through", ref="X1-1", x=220, y=200, snap=False)
    session.place("nema.terminals.feed_through", ref="X1-2", x=240, y=202, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=40, y=120, snap=False)
    for comp, x, y in (("M1-33", 40.0, 200.0), ("X1-1", 220.0, 200.0), ("X1-2", 240.0, 202.0), ("M1", 40.0, 120.0)):
        sheet.component(comp).properties.update({"print_import_x": x, "print_import_y": y})
    reads = [str(gap.get("read") or "") for gap in remaining_gaps(session) if gap["kind"] == "duplicate_tag"]
    assert not any("beside X1" in read for read in reads)
    assert not any("beside L1" in read or "beside L2" in read for read in reads)


def test_every_gap_id_is_unique_and_the_crop_file_uses_it(tmp_path: Path):
    """Two M1 contacts are two questions. The crop name is the id, not the list index."""

    from panelcad.importer.cleanup import _write_crops

    session = _ladder("Unique gaps")
    sheet = _sheet(session)
    session.place("nema.contactors.aux_no", ref="M1-23", x=20, y=80, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=70, y=84, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=20, y=140, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=20, y=200, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=80, y=204, snap=False)
    session.place("nema.terminals.feed_through", ref="X1-1", x=220, y=200, snap=False)
    for comp, x, y in (
        ("M1-23", 20.0, 80.0),
        ("CR1", 70.0, 84.0),
        ("M1", 20.0, 140.0),
        ("M1-13", 20.0, 200.0),
        ("PB1", 80.0, 204.0),
        ("X1-1", 220.0, 200.0),
    ):
        sheet.component(comp).properties.update({"print_import_x": x, "print_import_y": y})
    session.wire("L1", "M1.A1", number="?")
    gaps = remaining_gaps(session)
    ids = [str(gap["id"]) for gap in gaps]
    assert len(ids) == len(set(ids))
    assert len([gap for gap in gaps if gap["kind"] == "duplicate_tag" and str(gap["id"]).startswith("duplicate_tag:elementary:M1")]) >= 2
    kinds = {gap["kind"] for gap in gaps}
    assert "duplicate_tag" in kinds and "wire_number" in kinds
    crops = _write_crops(session, gaps, {}, _scan(tmp_path))
    names = [Path(row["path"]).name for row in crops]
    assert len(names) == len(set(names))
    for row in crops:
        stem = Path(row["path"]).stem
        assert stem == "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in row["id"]).strip("._")


def test_dpi_150_does_not_ask_to_put_107_on_the_106_net():
    """ImportJob keeps the loose 107. It cannot go on M1-13.3–L2, which already carries 106.

    The printed 107 is the node after the start button (PB1 to M1, and the
    holding contact's other side). The reader also tied M1-13 pin 3, the 106
    side of that contact, straight to L2 and left that wire blank. The token
    sits beside the contact, so the blank wire is the nearest open one, and
    writing 107 there is two numbers on one net. The checker stays as it is.
    There is no other open wire for this token, so it is not asked.
    """

    from panelcad.core.elementary_layout import _shorted_numbers
    from panelcad.importer.build import _stamp_properties

    session = _ladder("dpi_150 107")
    sheet = _sheet(session)
    session.project.properties["print_import"] = {"from_to_schedule": {}}
    placed = (
        ("nema.operators.pb_nc", "PB2", 81.0, 106.0),
        ("nema.operators.pb_no", "PB1", 117.0, 106.0),
        ("nema.contactors.aux_no", "M1-13", 117.0, 142.0),
        ("nema.contactors.coil", "M1", 328.8, 116.0),
    )
    for library_id, tag, x, y in placed:
        session.place(library_id, ref=tag, x=x, y=y, snap=False)
        sheet.component(tag).properties.update({"print_import_x": x, "print_import_y": y})
    session.wire("PB2.2", "M1-13.3", number="106", wire_id="page-001_w7")
    rail = session.wire("M1-13.3", "L2", wire_id="page-001_w11")
    rail.number = "?"
    session.wire("PB1.4", "M1.A1", number="107", wire_id="page-001_w8")
    token = {
        "number": "107",
        "x": 131.05,
        "y": 139.82,
        "from_tag": "",
        "to_tag": "",
        "from_pin": "",
        "to_pin": "",
        "confidence": 0.4,
        "ambiguous": True,
        "source": "ocr",
    }
    _stamp_properties(
        session,
        {"sheets": [{"id": sheet.id, "wires": [token]}], "title_block": {}, "from_to_schedule": {}},
        {"skipped": [], "unresolved_wires": [], "wires": []},
        layout_mode="ladder",
    )
    stored = [
        item
        for item in session.project.properties["print_import"]["items"]
        if item.get("kind") == "wire" and item.get("text") == "107"
    ]
    assert stored and abs(float(stored[0]["x"]) - 131.05) < 0.01
    assert not any(gap["kind"] == "unattached_number" for gap in remaining_gaps(session))
    rail.number = "107"
    findings = _shorted_numbers(session, sheet)
    assert any("106, 107 are two wire numbers on one net" in item for item in findings)
    assert rail.number == "107"


def test_a_pinned_number_is_stored_without_reading_x_too_early():
    """A pinned sheet-line number still has a scan position.

    The review list is built from synthetic facts. No electrical OCR weights.
    """

    from panelcad.importer.build import _add_loose_number_items

    items: list[dict] = []
    _add_loose_number_items(
        items,
        {
            "sheets": [
                {
                    "id": "page-001",
                    "wires": [
                        {
                            "number": "104",
                            "x": 180.0,
                            "y": 72.0,
                            "from_tag": "PL1",
                            "from_pin": "X2",
                            "to_tag": "L2",
                            "to_pin": "L2",
                            "ambiguous": False,
                            "confidence": 0.8,
                        },
                        {
                            "number": "105",
                            "from_tag": "ES1",
                            "from_pin": "2",
                            "to_tag": "PB2",
                            "to_pin": "1",
                            "ambiguous": False,
                        },
                    ],
                }
            ]
        },
    )
    printed = [item for item in items if item.get("text") == "104"]
    assert printed == [
        {
            "kind": "wire",
            "text": "104",
            "sheet": "page-001",
            "reason": "printed",
            "confidence": 0.8,
            "x": 180.0,
            "y": 72.0,
        }
    ]
    assert all(item.get("text") != "105" for item in items)


def test_a_loose_number_with_no_open_wire_is_stored_and_not_asked():
    """clean_300 keeps loose tokens and sends none: none of them sits on an open wire."""

    from panelcad.importer.build import _stamp_properties

    session = _ladder("Loose only")
    sheet = _sheet(session)
    session.project.properties["print_import"] = {"from_to_schedule": {}}
    session.place("nema.contactors.coil", ref="M1", x=40, y=40, snap=False)
    session.wire("L1", "M1.A1", number="106")
    _stamp_properties(
        session,
        {
            "sheets": [
                {
                    "id": sheet.id,
                    "wires": [
                        {
                            "number": "201",
                            "x": 400.0,
                            "y": 20.0,
                            "ambiguous": True,
                            "confidence": 0.4,
                        }
                    ],
                }
            ],
            "title_block": {},
            "from_to_schedule": {},
        },
        {"skipped": [], "unresolved_wires": [], "wires": []},
        layout_mode="ladder",
    )
    items = session.project.properties["print_import"]["items"]
    assert any(item.get("kind") == "wire" and item.get("text") == "201" for item in items)
    assert not any(gap["kind"] == "unattached_number" for gap in remaining_gaps(session))


def test_gap_ids_match_across_hash_seeds():
    import os
    import subprocess
    import sys

    script = """
import json
from panelcad.api.session import Session
from panelcad.importer.cleanup import remaining_gaps

session = Session.create("Gaps", sheet_id="elementary", title="Elementary")
session.set_title_block(standard="NEMA/ANSI", drawing_style="ladder", coil_style="circle", drawn_by="Demo")
sheet = session.project.sheet("elementary")
sheet.page.size = "TABLOID"
sheet.page.orientation = "landscape"
sheet.drawing_style = "ladder"
session.place("nema.operators.estop", ref="ES1", x=40, y=80, snap=False)
session.place("nema.operators.pb_nc", ref="PB2", x=90, y=80, snap=False)
session.place("nema.operators.pb_no", ref="PB1", x=140, y=80, snap=False)
session.place("nema.protection.fuse_10a", ref="FU2", x=40, y=40, snap=False)
session.wire("L1", "ES1.1", number="102")
session.wire("ES1.2", "PB2.1", number="105")
session.wire("PB2.2", "PB1.3", number="106")
session.wire("PB1.4", "L2", number="108")
session.wire("FU2.1", "FU2.2", number="?")
print(json.dumps([gap["id"] for gap in remaining_gaps(session)]))
"""
    ids = []
    for seed in ("0", "1", "42"):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["QT_QPA_PLATFORM"] = "offscreen"
        raw = subprocess.check_output([sys.executable, "-c", script], env=env, cwd=str(Path(__file__).resolve().parents[1]))
        ids.append(json.loads(raw.decode().strip().splitlines()[-1]))
    assert ids[0] == ids[1] == ids[2]
    assert any(item.startswith("contacts_only:") for item in ids[0])
    assert any(item.startswith("self_wire:") for item in ids[0])


def test_phone_challenges_run_before_a_new_device_and_keep_103(tmp_path: Path):
    from tests.test_recorded_electrical_build import _load, _replay

    session = _replay("phone", _load())
    sheet = session.project.sheet("page-001")
    kept = next(wire for wire in sheet.wires if str(wire.number) == "103" and "FU1" in f"{wire.frm}{wire.to}")
    other = next(wire for wire in session.project.sheet("page-001-p2").wires if "M1-13" in f"{wire.frm}{wire.to}")
    gaps = remaining_gaps(session)
    challenged = next(gap for gap in gaps if gap.get("wire_id") == other.id or other.id in (gap.get("wires") or []))
    reply = {
        "challenges": [
            {
                "gap": challenged["id"],
                "target": other.id,
                "reason": "This landing is not on the scan.",
                "evidence": "No conductor is drawn on that pin.",
                "confidence": 0.93,
            }
        ],
        "devices": [
            {
                "tag": "T1",
                "library_id": "nema.power.control_transformer",
                "gap": "unread_device:page-001",
                "confidence": 0.95,
                "unreadable": False,
            }
        ],
        "connections": [
            {
                "gap": "unread_device:page-001",
                "from": "L1",
                "to": "T1.H1",
                "number": "111",
                "confidence": 0.95,
                "unreadable": False,
            }
        ],
    }
    result = replay_cleanup(session, reply, image=_scan(tmp_path))
    numbers = {wire.id: str(wire.number or "") for wire in sheet.wires}
    assert numbers.get(kept.id) == "103"
    assert {kept.frm, kept.to} == {"FU1.2", "X2"}
    # Removing that landing clears "no load" and adds "does not land".
    # A new finding is not an improvement, so the wire stays.
    assert any(row["reason"] == "checkers worse" for row in result["dropped"]), result
    assert any(wire.id == other.id for wire in session.project.sheet("page-001-p2").wires)
    assert any(item.get("kind") == "device" and item.get("tag") == "T1" for item in result["added"])
    assert not any("runs through a symbol" in str(item.get("reason") or "") for item in result["dropped"])


def test_a_routing_finding_does_not_throw_away_the_fact_and_a_real_one_does():
    from panelcad.importer.cleanup import _fill_no_worse, _without_routing

    before = {"elementary: something else"}
    routed = before | {"elementary: w4 runs through a symbol", "elementary: w7 jogs through the power block"}
    assert _fill_no_worse(_without_routing(before), _without_routing(routed))
    shorted = before | {"elementary: 108, 109 are two wire numbers on one net"}
    assert not _fill_no_worse(_without_routing(before), _without_routing(shorted))


def test_one_bad_landing_does_not_sink_the_new_device(tmp_path: Path):
    """FU2's pin-to-itself guess is dropped. T1 and its primary stay."""

    session = _ladder("Split")
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=120, snap=False)
    session.wire("L1", "FU1.1", number="102")
    reply = {
        "devices": [
            {
                "tag": "T1",
                "library_id": "nema.power.control_transformer",
                "gap": "unread_device:elementary",
                "confidence": 0.95,
                "unreadable": False,
            },
            {
                "tag": "FU2",
                "library_id": "nema.protection.fuse_10a",
                "gap": "unread_device:elementary",
                "confidence": 0.95,
                "unreadable": False,
            },
        ],
        "connections": [
            {
                "gap": "unread_device:elementary",
                "from": "FU1.2",
                "to": "T1.H1",
                "number": "103",
                "confidence": 0.95,
                "unreadable": False,
            },
            {
                "gap": "unread_device:elementary",
                "from": "FU2.1",
                "to": "FU2.2",
                "number": "104",
                "confidence": 0.95,
                "unreadable": False,
            },
        ],
    }
    result = replay_cleanup(session, reply, image=_scan(tmp_path))
    assert _sheet(session).component("T1").id == "T1"
    assert any({wire.frm, wire.to} == {"FU1.2", "T1.H1"} for wire in _sheet(session).wires)
    assert all(comp.id != "FU2" for comp in _sheet(session).components)
    assert any(row["reason"] in {"no wiring", "through symbol"} for row in result["dropped"]), result


def test_an_open_stub_satisfies_that_terminal_and_a_bare_pin_does_not(tmp_path: Path):
    from panelcad.importer.cleanup import _completion_issues
    from panelcad.importer.raster import open_stub_pins

    pins = [("H2", 0.0, 0.0), ("H1", 0.0, 16.0), ("X1", 30.0, 0.0)]
    box = (-4.0, -6.0, 12.0, 20.0)
    stubs = open_stub_pins(pins, [((0.0, 0.0), (14.0, 0.0)), ((30.0, 0.0), (44.0, 0.0))], box)
    assert stubs == ["H2", "X1"]
    reached = open_stub_pins(pins, [((0.0, 0.0), (0.0, 16.0))], box)
    assert reached == []

    session = _ladder("Stubs")
    session.place("nema.power.control_transformer", ref="T1", x=80, y=40, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=40, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=120, snap=False)
    session.wire("FU1.2", "T1.H1", number="103")
    session.wire("T1.H4", "FU1.1", number="101")
    session.wire("PL1.X2", "L2", number="104")
    bare = "\n".join(_completion_issues(session))
    assert "T1 secondary does not feed a wire" in bare
    _sheet(session).component("T1").properties["print_open_stubs"] = "X1,X2,H2,H3"
    landed = "\n".join(_completion_issues(session))
    assert "secondary does not feed" not in landed
    _sheet(session).component("T1").properties["print_open_stubs"] = "H2,H3"
    still = "\n".join(_completion_issues(session))
    assert "T1 secondary does not feed a wire" in still


def test_an_open_contact_pin_takes_a_rail_stub_and_nothing_else(tmp_path: Path):
    session = _ladder("Open pin")
    session.place("nema.contactors.aux_no", ref="CR1-13", x=80, y=80, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=160, y=80, snap=False)
    session.wire("CR1-13.4", "PL1.X1", number="110")
    session.wire("PL1.X2", "L2", number="104")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "open_pin" and item.get("pin") == "3")
    assert gap["tag"] == "CR1-13"
    kept = replay_cleanup(
        session,
        {
            "connections": [
                {
                    "gap": gap["id"],
                    "from": "L1",
                    "to": "CR1-13.3",
                    "number": "102",
                    "confidence": 0.95,
                    "unreadable": False,
                }
            ]
        },
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "return" and item.get("number") == "102" for item in kept["added"]), kept
    assert any({wire.frm, wire.to} == {"L1", "CR1-13.3"} and wire.number == "102" for wire in _sheet(session).wires)
    fresh = _ladder("Open pin rejected")
    fresh.place("nema.contactors.aux_no", ref="CR1-13", x=80, y=80, snap=False)
    fresh.place("nema.operators.pilot_light", ref="PL1", x=160, y=80, snap=False)
    fresh.wire("CR1-13.4", "PL1.X1", number="110")
    fresh.wire("PL1.X2", "L2", number="104")
    named = next(item for item in remaining_gaps(fresh) if item["kind"] == "open_pin")
    rejected, dropped = review_cleanup(
        fresh,
        {
            "connections": [
                {
                    "gap": named["id"],
                    "from": "PL1.X2",
                    "to": "CR1-13.3",
                    "number": "102",
                    "confidence": 0.95,
                    "unreadable": False,
                }
            ]
        },
        remaining_gaps(fresh),
    )
    assert rejected == []
    assert any(row["reason"] == "invented connection" for row in dropped)


def test_a_return_numbered_with_the_rail_name_can_take_the_printed_number(tmp_path: Path):
    session = _ladder("Rail return")
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=80, snap=False)
    session.place("nema.contactors.aux_no", ref="CR1-13", x=80, y=80, snap=False)
    session.wire("L1", "CR1-13.3", number="102")
    session.wire("CR1-13.4", "PL1.X1", number="110")
    landed = session.wire("PL1.X2", "L2", number="L2")
    session.project.properties["print_import"] = {
        "items": [{"kind": "wire", "text": "104", "sheet": "elementary", "x": 200.0, "y": 80.0, "confidence": 0.4}]
    }
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "suspect_number" and item.get("wire_id") == landed.id)
    assert gap["number"] == "L2"
    guessed, dropped = review_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "106", "confidence": 0.95}]},
        remaining_gaps(session),
    )
    assert guessed == []
    assert any(row["reason"] == "guessed wire number" for row in dropped)
    kept = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "104", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "replace_number" and item.get("number") == "104" for item in kept["added"]), kept
    assert landed.number == "104"


def test_two_numbers_on_one_net_names_both_wires_and_one_reply_can_fix_both(tmp_path: Path):
    session = _ladder("Two numbers")
    session.place("nema.contactors.coil", ref="M1", x=200, y=80, snap=False)
    session.place("nema.protection.ol_contact", ref="OL1", x=260, y=80, snap=False)
    session.place("nema.relays.cr", ref="CR1", x=80, y=140, snap=False)
    session.wire("M1.A2", "OL1.95", number="108")
    bogus = session.wire("OL1.95", "M1.A2", number="109", wire_id="w12")
    real = session.wire("CR1.A1", "M1.A1", number="100", wire_id="w14")
    session.wire("CR1.A2", "L2", number="104")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "two_numbers")
    assert set(gap["wires"]) >= {bogus.id, "w12"} or bogus.id in gap["wires"]
    assert any(wire_id in gap["wires"] for wire_id in (bogus.id,))
    number_gap = next(item for item in remaining_gaps(session) if item.get("wire_id") == real.id and item["kind"] == "suspect_number")
    result = replay_cleanup(
        session,
        {
            "challenges": [
                {
                    "gap": gap["id"],
                    "target": bogus.id,
                    "reason": "The scan has one conductor here.",
                    "evidence": "109 is the cross-reference under the coil, not a second wire.",
                    "confidence": 0.95,
                }
            ],
            "answers": [{"gap": number_gap["id"], "number": "109", "confidence": 0.95}],
        },
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "challenge" and item.get("target") == bogus.id for item in result["added"]), result
    assert all(wire.id != bogus.id for wire in _sheet(session).wires)
    assert real.number == "109"


def test_an_open_stub_is_ink_that_stops_and_a_wired_pin_is_not_one():
    import numpy as np

    from panelcad.importer.raster import _ink_open_stub, record_open_stubs

    ppm = 4.0
    mask = np.zeros((200, 160), dtype=np.uint8)
    # Catalog stubs end at the pin with ink inward. H2 at (20, 28) has inward
    # ink to the right; H1 at (20, 12) has none.
    for step in range(0, 6):
        px = int(round((20.0 + step * 0.8) * ppm))
        py = int(round(28.0 * ppm))
        mask[py - 1 : py + 2, px - 1 : px + 2] = 255
    assert _ink_open_stub(mask, ppm, 20.0, 28.0, "left", [])
    assert not _ink_open_stub(mask, ppm, 20.0, 12.0, "left", [])
    # A tiny break right outside the pin followed by a wire run is not an open stub.
    broken = np.zeros((200, 160), dtype=np.uint8)
    for step in range(0, 6):
        px = int(round((20.0 + step * 0.8) * ppm))
        py = int(round(28.0 * ppm))
        broken[py - 1 : py + 2, px - 1 : px + 2] = 255
    for step in range(3, 22):
        px = int(round((20.0 - step * 0.8) * ppm))
        py = int(round(28.0 * ppm))
        broken[py - 1 : py + 2, px - 1 : px + 2] = 255
    assert not _ink_open_stub(broken, ppm, 20.0, 28.0, "left", [])
    # A vertical rail within 3 mm outside the pin is not an open stub.
    near_rail = mask.copy()
    rx = int(round((20.0 - 2.4) * ppm))
    near_rail[int(round(10.0 * ppm)) : int(round(46.0 * ppm)), rx - 1 : rx + 2] = 255
    assert not _ink_open_stub(near_rail, ppm, 20.0, 28.0, "left", [])
    session_catalog = _ladder().catalog
    devices = [
        {"tag": "T1", "library_id": "nema.power.control_transformer", "x": 20.0, "y": 0.0},
    ]
    wires = [{"from_tag": "T1", "from_pin": "H1", "to_tag": "FU1", "to_pin": "2", "ambiguous": False}]
    record_open_stubs(devices, session_catalog, mask, ppm, wires)
    stubs = set(devices[0].get("open_stubs") or [])
    assert "H1" not in stubs
    assert "H2" in stubs
    side_wired = [{"from_tag": "T1", "from_pin": "left", "to_tag": "FU1", "to_pin": "2", "ambiguous": False}]
    record_open_stubs(devices, session_catalog, mask, ppm, side_wired)
    assert "H2" not in set(devices[0].get("open_stubs") or [])


def test_fixture_transformer_open_stubs_are_recorded():
    """Printed open terminals on the fixture scans are recorded; a wired rail stub is not."""

    import cv2
    import numpy as np
    from PIL import Image

    from panelcad.importer.raster import _ink, _match_symbol, record_open_stubs

    catalog = _ladder().catalog
    width_mm = 17 * 25.4
    height_mm = 11 * 25.4
    root = Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "schematic"
    for name in ("clean_300.png", "dpi_150.png"):
        image = Image.open(root / name)
        rgb = np.asarray(image.convert("RGB"))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        ppm = rgb.shape[1] / width_mm
        binary = _ink(gray)
        match_ink = cv2.dilate(binary, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        token = {"text": "T1", "x": 121.0, "y": 17.0, "w": 8.0, "confidence": 0.95}
        device = _match_symbol(token, match_ink, ppm, rgb.shape, catalog, "NEMA/ANSI", height_mm=height_mm)
        assert device.get("library_id") and not device.get("guess"), device
        wires = [
            {"from_tag": "T1", "from_pin": "H1", "to_tag": "FU1", "to_pin": "2", "ambiguous": False},
            {"from_tag": "T1", "from_pin": "H4", "to_tag": "FU2", "to_pin": "2", "ambiguous": False},
        ]
        record_open_stubs([device], catalog, binary, ppm, wires)
        assert set(device.get("open_stubs") or []) == {"H2", "H3", "X1", "X2"}, (name, device.get("open_stubs"))

    contact = {
        "tag": "CR1-13",
        "library_id": "nema.contactors.aux_no",
        "x": 192.87,
        "y": 95.67,
        "confidence": 0.9,
    }
    dpi = Image.open(root / "dpi_150.png")
    rgb = np.asarray(dpi.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    ppm = rgb.shape[1] / width_mm
    binary = _ink(gray)
    record_open_stubs([contact], catalog, binary, ppm, wires=[])
    assert "3" not in set(contact.get("open_stubs") or []), contact.get("open_stubs")

    blank = {"tag": "T2", "library_id": "nema.power.control_transformer", "x": 200.0, "y": 200.0, "confidence": 0.9}
    record_open_stubs([blank], catalog, binary, ppm, wires=[])
    assert (blank.get("open_stubs") or []) == []


def test_ladder_build_stamps_transformer_open_stubs(tmp_path: Path):
    """Ladder synth keeps printed open stubs on the component and clears the secondary blocker."""

    from panelcad.importer.build import build_session
    from panelcad.importer.cleanup import _completion_issues
    from panelcad.library.catalog import default_catalog

    stitched = {
        "title_block": {"standard": "NEMA/ANSI", "drawing_title": "Open stubs"},
        "sheets": [
            {
                "id": "page-001",
                "kind": "schematic",
                "devices": [
                    {
                        "tag": "T1",
                        "library_id": "nema.power.control_transformer",
                        "x": 100.0,
                        "y": 20.0,
                        "confidence": 0.95,
                        "open_stubs": ["H1", "H2", "H3", "X1", "X2"],
                    },
                    {"tag": "FU1", "library_id": "nema.protection.fuse_10a", "x": 40.0, "y": 20.0, "confidence": 0.95},
                    {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "x": 40.0, "y": 50.0, "confidence": 0.95},
                    {"tag": "PL1", "library_id": "nema.operators.pilot_light", "x": 200.0, "y": 120.0, "confidence": 0.95},
                    {"tag": "PB1", "library_id": "nema.operators.pb_no", "x": 80.0, "y": 120.0, "confidence": 0.95},
                    {"tag": "M1", "library_id": "nema.contactors.coil", "x": 220.0, "y": 120.0, "confidence": 0.95},
                ],
                "wires": [
                    {"number": "103", "from_tag": "FU1", "from_pin": "2", "to_tag": "T1", "to_pin": "H1"},
                    {"number": "101", "from_tag": "T1", "from_pin": "H4", "to_tag": "FU2", "to_pin": "2"},
                    {"number": "102", "from_tag": "L1", "from_pin": "L1", "to_tag": "PB1", "to_pin": "3"},
                    {"number": "107", "from_tag": "PB1", "from_pin": "4", "to_tag": "M1", "to_pin": "A1"},
                    {"number": "108", "from_tag": "M1", "from_pin": "A2", "to_tag": "L2", "to_pin": "L2"},
                    {"number": "110", "from_tag": "L1", "from_pin": "L1", "to_tag": "PL1", "to_pin": "X1"},
                    {"number": "104", "from_tag": "PL1", "from_pin": "X2", "to_tag": "L2", "to_pin": "L2"},
                ],
            }
        ],
    }
    session, _report = build_session(stitched, catalog=default_catalog(), layout_mode="ladder")
    host = None
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id == "T1":
                host = comp
                break
    assert host is not None
    stubs = str((host.properties or {}).get("print_open_stubs") or "")
    assert stubs == "H2,H3,X1,X2", stubs
    assert not any("T1 secondary" in item for item in _completion_issues(session))


def test_unread_device_open_stub_answer_clears_secondary(tmp_path: Path):
    """A phone-like unread transformer can confirm open secondary stubs."""

    from panelcad.importer.cleanup import _completion_issues, _issue

    session = _ladder("Phone T1 stubs")
    session.place("nema.power.control_transformer", ref="T1", x=100, y=20, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=20, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU2", x=40, y=50, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=120, snap=False)
    session.wire("FU1.2", "T1.H1", number="103")
    session.wire("T1.H4", "FU2.2", number="101")
    session.wire("PL1.X2", "L2", number="104")
    gap = next(
        item
        for item in remaining_gaps(session)
        if item["kind"] == "open_pin" and item.get("pin") == "secondary" and item.get("tag") == "T1"
    )
    assert "secondary does not feed" in "\n".join(_completion_issues(session))
    assert _issue(session) is False
    low, dropped = review_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "open_stub": True, "confidence": 0.5}]},
        remaining_gaps(session),
    )
    assert low == []
    assert any(row["reason"] == "unsure" for row in dropped)
    kept = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "open_stub": True, "confidence": 0.92}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "open_stub" for item in kept["added"]), kept
    stubs = set(str((_sheet(session).component("T1").properties or {}).get("print_open_stubs") or "").split(","))
    stubs.discard("")
    assert {"X1", "X2"} <= stubs
    assert "secondary does not feed" not in "\n".join(_completion_issues(session))


def test_a_printed_number_far_from_its_other_wire_stays_a_token(tmp_path: Path):
    from panelcad.importer.cleanup import _read_token_near
    from panelcad.importer.raster import collapse_wire_numbers

    pinned = {"number": "104", "x": 20.0, "y": 20.0, "from_tag": "CR1", "from_pin": "A2", "to_tag": "L2", "to_pin": "L2"}
    near = {"number": "104", "x": 22.0, "y": 20.0, "ambiguous": True}
    far = {"number": "104", "x": 180.0, "y": 72.0, "ambiguous": True}
    kept = collapse_wire_numbers([near, pinned, far])
    texts = [(item.get("number"), round(float(item.get("y") or 0), 1)) for item in kept]
    assert ("104", 72.0) in texts
    assert ("104", 20.0) in texts
    session = _ladder("Token")
    session.place("nema.operators.pilot_light", ref="PL1", x=160, y=60, snap=False)
    _sheet(session).component("PL1").properties.update({"print_import_x": 160.0, "print_import_y": 60.0})
    wire = session.wire("PL1.X2", "L2", number="L2")
    session.project.properties["print_import"] = {
        "items": [{"kind": "wire", "text": "104", "sheet": "elementary", "x": 180.0, "y": 72.0, "reason": "printed"}]
    }
    assert _read_token_near(session, _sheet(session), wire, "104")


def test_a_phantom_wire_stays_removed_when_the_phantom_does_not_land(tmp_path: Path):
    session = _ladder("Phantom landing")
    sheet = _sheet(session)
    session.place("nema.contactors.coil", ref="M1", x=370, y=80, snap=False)
    session.place("nema.contactors.coil", ref="M1_2", x=370, y=120, snap=False)
    session.wire("L1", "M1.A1", number="102")
    session.wire("M1.A2", "L2", number="108")
    phantom = session.wire("M1_2.A2", "L2", number="115", wire_id="page-001_w21")
    gap = next(item for item in remaining_gaps(session) if item.get("tag") == "M1_2")
    assert phantom.id in gap.get("wires", [])
    result = replay_cleanup(session, _challenge(gap["id"], phantom.id), image=_scan(tmp_path))
    assert any(item.get("kind") == "challenge" and item.get("target") == phantom.id for item in result["added"]), result
    assert all(wire.id != phantom.id for wire in sheet.wires)


def test_the_same_number_on_two_device_nets_names_both_wires():
    session = _ladder("Two nets")
    session.place("nema.contactors.coil", ref="M1", x=200, y=80, snap=False)
    session.place("nema.protection.ol_contact", ref="OL1", x=260, y=80, snap=False)
    session.place("nema.relays.cr", ref="CR1", x=80, y=140, snap=False)
    bogus = session.wire("OL1.95", "M1.A2", number="109", wire_id="w12")
    real = session.wire("CR1.A1", "M1.A1", number="109", wire_id="w14")
    session.wire("CR1.A2", "L2", number="104")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "two_numbers" and item.get("number") == "109")
    assert set(gap["wires"]) == {bogus.id, real.id}


def test_a_coil_cross_reference_list_is_not_a_wire_number():
    from panelcad.importer.raster import _wire_numbers_on_the_page

    numbers = [
        {"text": "3-4", "x": 10.0, "y": 10.0},
        {"text": "109", "x": 10.0, "y": 16.0},
        {"text": "3-4", "x": 10.0, "y": 22.0},
        {"text": "111", "x": 10.0, "y": 28.0},
        {"text": "3-4", "x": 10.0, "y": 34.0},
        {"text": "115", "x": 10.0, "y": 40.0},
        {"text": "108", "x": 40.0, "y": 16.0},
    ]
    kept = {str(item["text"]) for item in _wire_numbers_on_the_page(numbers, [], None)}
    assert kept == {"108"}
    spread = [
        {"text": "3-4", "x": 10.0, "y": 16.0},
        {"text": "109", "x": 24.0, "y": 16.5},
        {"text": "108", "x": 55.0, "y": 4.0},
    ]
    kept = {str(item["text"]) for item in _wire_numbers_on_the_page(spread, [], None)}
    assert kept == {"108"}


def test_phone_transformer_and_return_fuse_land_in_load_first_order(tmp_path: Path):
    """Adding T1 and FU2 together lands FU2 on T1 before L2 regardless of device list order."""

    for order in (
        [
            {"tag": "T1", "library_id": "nema.power.control_transformer", "gap": "unread_device:elementary", "confidence": 0.94, "unreadable": False},
            {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "gap": "unread_device:elementary", "confidence": 0.94, "unreadable": False},
        ],
        [
            {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "gap": "unread_device:elementary", "confidence": 0.94, "unreadable": False},
            {"tag": "T1", "library_id": "nema.power.control_transformer", "gap": "unread_device:elementary", "confidence": 0.94, "unreadable": False},
        ],
    ):
        session = _ladder("Phone primary")
        session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=20, snap=False)
        session.place("nema.operators.pilot_light", ref="PL1", x=200, y=120, snap=False)
        session.wire("L1", "FU1.1", number="100")
        session.wire("L1", "PL1.X1", number="?")
        session.wire("PL1.X2", "L2", number="104")
        assert any(gap["kind"] == "unread_device" for gap in remaining_gaps(session))
        result = replay_cleanup(
            session,
            {
                "devices": order,
                "connections": [
                    {"gap": "unread_device:elementary", "from": "FU1.2", "to": "T1.H1", "number": "103", "confidence": 0.93, "unreadable": False},
                    {"gap": "unread_device:elementary", "from": "FU2.2", "to": "T1.H4", "number": "101", "confidence": 0.93, "unreadable": False},
                    {"gap": "unread_device:elementary", "from": "FU2.1", "to": "L2", "number": "104", "confidence": 0.93, "unreadable": False},
                ],
            },
            image=_scan(tmp_path),
        )
        sheet = _sheet(session)
        tags = {comp.id for comp in sheet.components}
        assert {"T1", "FU2"} <= tags, result
        assert _has_pair(session, "FU1", "T1")
        assert _has_pair(session, "FU2", "T1")
        assert _has_pair(session, "FU2", "L2")
        assert {"103", "101", "104"} <= _numbers(session)
        issues = "\n".join(elementary_layout_issues(session, sheet))
        assert "no path through a load" not in issues, issues
        assert "ties the primary" not in issues, issues


def test_rejected_return_fuse_leaves_open_transformer_primary_gap(tmp_path: Path):
    """When FU2 has only a rail wire and rolls back, T1 stays and open_pin names T1.H4."""

    from panelcad.importer.cleanup import _issue

    session = _ladder("Phone missing 101")
    session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=20, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=120, snap=False)
    session.wire("L1", "FU1.1", number="100")
    session.wire("L1", "PL1.X1", number="?")
    session.wire("PL1.X2", "L2", number="104")
    result = replay_cleanup(
        session,
        {
            "devices": [
                {"tag": "T1", "library_id": "nema.power.control_transformer", "gap": "unread_device:elementary", "confidence": 0.94, "unreadable": False},
                {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "gap": "unread_device:elementary", "confidence": 0.94, "unreadable": False},
            ],
            "connections": [
                {"gap": "unread_device:elementary", "from": "FU1.2", "to": "T1.H1", "number": "103", "confidence": 0.93, "unreadable": False},
                {"gap": "unread_device:elementary", "from": "FU2.1", "to": "L2", "number": "104", "confidence": 0.93, "unreadable": False},
            ],
        },
        image=_scan(tmp_path),
    )
    sheet = _sheet(session)
    tags = {comp.id for comp in sheet.components}
    assert "T1" in tags, result
    assert "FU2" not in tags, result
    assert _issue(session) is False
    gap_ids = {gap["id"] for gap in remaining_gaps(session)}
    assert "open_pin:elementary:T1.H4" in gap_ids, gap_ids
    follow = replay_cleanup(
        session,
        {
            "devices": [
                {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "gap": "unread_device:elementary", "confidence": 0.95, "unreadable": False},
            ],
            "connections": [
                {"gap": "open_pin:elementary:T1.H4", "from": "FU2.2", "to": "T1.H4", "number": "101", "confidence": 0.94, "unreadable": False},
                {"gap": "unread_device:elementary", "from": "FU2.1", "to": "L2", "number": "104", "confidence": 0.94, "unreadable": False},
            ],
        },
        image=_scan(tmp_path),
    )
    assert "FU2" in {comp.id for comp in sheet.components}, follow
    assert "open_pin:elementary:T1.H4" not in {gap["id"] for gap in remaining_gaps(session)}


def test_fix_token_and_coil_xref_filter_drop_contact_table_and_keep_hyphen_wire():
    """_fix_token preserves 3-4 so _wire_numbers_on_the_page drops the xref stack, while 2-15 stays."""

    from panelcad.importer.raster import _fix_token, _wire_numbers_on_the_page
    from panelcad.importer.read import label_tokens

    catalog = _ladder().catalog
    coil = [{"tag": "M1", "library_id": "nema.contactors.coil", "x": 340.0, "y": 100.0, "confidence": 0.95}]
    raw_rows = [
        ("3-4", 342.0, 136.0),
        ("109", 356.0, 136.0),
        ("3-4", 342.0, 142.0),
        ("111", 356.0, 142.0),
        ("3-4", 342.0, 148.0),
        ("115", 356.0, 148.0),
        ("108", 385.0, 114.0),
        ("2-15", 140.0, 80.0),
    ]
    tokens = [
        {"text": _fix_token(raw), "x": x, "y": y, "w": 6.0, "h": 3.0, "confidence": 0.9}
        for raw, x, y in raw_rows
    ]
    assert tokens[0]["text"] == "3-4"
    labeled = label_tokens(tokens, 431.8, 279.4, catalog=catalog)
    kept = {str(item["text"]) for item in _wire_numbers_on_the_page(labeled["wires"], coil, catalog)}
    assert kept == {"108", "2-15"}, kept

    # Even if a degraded scan lost the dash and read "34", the aligned >=2-row stack under M1 is dropped.
    degraded = [
        {"text": _fix_token(raw.replace("3-4", "34")), "x": x, "y": y, "w": 6.0, "h": 3.0, "confidence": 0.9}
        for raw, x, y in raw_rows
    ]
    labeled_deg = label_tokens(degraded, 431.8, 279.4, catalog=catalog)
    kept_deg = {str(item["text"]) for item in _wire_numbers_on_the_page(labeled_deg["wires"], coil, catalog)}
    assert kept_deg == {"108", "2-15"}, kept_deg


def test_trace_wires_does_not_duplicate_a_direct_pin_pair():
    """A single stroke between M1.A2 and OL1.95 does not spawn a second chain wire grabbing 109."""

    import cv2
    import numpy as np

    from panelcad.importer.raster import _trace_wires

    catalog = _ladder().catalog
    width_mm, height_mm, ppm = 300.0, 220.0, 6.0
    w_px, h_px = int(round(width_mm * ppm)), int(round(height_mm * ppm))
    binary = np.zeros((h_px, w_px), dtype=np.uint8)
    rgb = np.full((h_px, w_px, 3), 255, dtype=np.uint8)
    x0, x1, y0 = int(round(128.0 * ppm)), int(round(168.0 * ppm)), int(round(100.0 * ppm))
    cv2.line(binary, (x0, y0), (x1, y0), 1, 2)
    cv2.line(rgb, (x0, y0), (x1, y0), (0, 0, 0), 2)
    devices = [
        {"tag": "M1", "library_id": "nema.contactors.coil", "x": 100.0, "y": 86.0, "confidence": 0.95},
        {"tag": "OL1", "library_id": "nema.protection.ol_contact", "x": 168.0, "y": 84.0, "confidence": 0.95},
    ]
    numbers = [
        {"text": "108", "x": 148.0, "y": 96.0, "confidence": 0.92},
        {"text": "109", "x": 148.0, "y": 115.0, "confidence": 0.90},
    ]
    traced = _trace_wires(numbers, binary, rgb, devices, catalog, ppm, width_mm, height_mm)
    pinned = [
        wire
        for wire in traced
        if {
            (str(wire.get("from_tag") or ""), str(wire.get("from_pin") or "")),
            (str(wire.get("to_tag") or ""), str(wire.get("to_pin") or "")),
        }
        == {("M1", "A2"), ("OL1", "95")}
    ]
    assert len(pinned) == 1, traced
    assert pinned[0]["number"] == "108", pinned


def test_ambiguous_m1_ol1_108_hop_stays_connected_when_ol_is_off_rung():
    """An ambiguous M1.A2→OL1.95 hop keeps 108 even when OL1's scan y is off the coil rung."""

    from panelcad.importer.build import build_session
    from panelcad.library.catalog import default_catalog

    catalog = default_catalog()
    devices = [
        {"tag": "ES1", "library_id": "nema.operators.estop", "x": 40, "y": 90, "confidence": 0.9},
        {"tag": "PB2", "library_id": "nema.operators.pb_nc", "x": 80, "y": 90, "confidence": 0.9},
        {"tag": "PB1", "library_id": "nema.operators.pb_no", "x": 120, "y": 90, "confidence": 0.9},
        {"tag": "M1", "library_id": "nema.contactors.coil", "x": 170, "y": 90, "confidence": 0.9},
        {"tag": "OL1", "library_id": "nema.protection.ol_contact", "x": 220, "y": 160, "confidence": 0.72},
    ]
    wires = [
        {"number": "102", "x": 20, "y": 90, "from_tag": "L1", "from_pin": "L1", "to_tag": "ES1", "to_pin": "1", "confidence": 0.9},
        {"number": "105", "x": 60, "y": 90, "from_tag": "ES1", "from_pin": "2", "to_tag": "PB2", "to_pin": "1", "confidence": 0.9},
        {"number": "106", "x": 100, "y": 90, "from_tag": "PB2", "from_pin": "2", "to_tag": "PB1", "to_pin": "3", "confidence": 0.9},
        {"number": "107", "x": 145, "y": 90, "from_tag": "PB1", "from_pin": "4", "to_tag": "M1", "to_pin": "A1", "confidence": 0.9},
        {
            "number": "108",
            "x": 195,
            "y": 95,
            "from_tag": "M1",
            "from_pin": "A2",
            "to_tag": "OL1",
            "to_pin": "95",
            "confidence": 0.4,
            "ambiguous": True,
        },
        {"number": "104", "x": 280, "y": 160, "from_tag": "OL1", "from_pin": "96", "to_tag": "L2", "to_pin": "L2", "confidence": 0.9},
        {"number": "107", "x": 100, "y": 165, "ambiguous": True, "confidence": 0.4},
    ]
    session, _rep = build_session(
        {
            "title_block": {"standard": "NEMA/ANSI"},
            "sheets": [
                {
                    "id": "page-001",
                    "kind": "schematic",
                    "width_mm": 431.8,
                    "height_mm": 279.4,
                    "devices": devices,
                    "wires": wires,
                }
            ],
        },
        catalog=catalog,
        layout_mode="ladder",
    )
    sheet = session.project.sheet("page-001")
    assert any({"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)} and str(w.number) == "108" for w in sheet.wires)
    assert not any({"L1", "OL1.95"} == {str(w.frm), str(w.to)} for w in sheet.wires)
    assert not any({"M1.A2", "L2"} == {str(w.frm), str(w.to)} for w in sheet.wires)


def test_mid_span_return_token_reaches_long_rung_in_synth_and_cleanup(tmp_path: Path):
    """A return token printed >150 mm right of PL1 attaches in ladder synth and _read_token_near."""

    from panelcad.importer.build import build_session
    from panelcad.importer.cleanup import _read_token_near
    from panelcad.library.catalog import default_catalog

    session = _ladder("Mid-span return")
    session.place("nema.contactors.aux_no", ref="CR1-13", x=50, y=174, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=88, y=174, snap=False)
    _sheet(session).component("PL1").properties.update({"print_import_x": 88.0, "print_import_y": 174.0})
    session.wire("L1", "CR1-13.3", number="102")
    session.wire("CR1-13.4", "PL1.X1", number="110")
    ret_wire = session.wire("PL1.X2", "L2", number="L2")
    session.project.properties["print_import"] = {
        "items": [{"kind": "wire", "text": "104", "sheet": "elementary", "x": 256.0, "y": 177.5, "reason": "printed"}]
    }
    assert _read_token_near(session, _sheet(session), ret_wire, "104")
    gap = next(item for item in remaining_gaps(session) if item["kind"] == "suspect_number" and item.get("wire_id") == ret_wire.id)
    kept = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "104", "confidence": 0.94}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "replace_number" and item.get("number") == "104" for item in kept["added"]), kept
    assert ret_wire.number == "104"

    stitched = {
        "title_block": {"standard": "NEMA/ANSI", "drawing_title": "Mid-span synth"},
        "sheets": [
            {
                "id": "page-001",
                "kind": "schematic",
                "width_mm": 431.8,
                "devices": [
                    {"tag": "CR1-13", "library_id": "nema.contactors.aux_no", "x": 50.0, "y": 174.0, "confidence": 0.95},
                    {"tag": "PL1", "library_id": "nema.operators.pilot_light", "x": 88.0, "y": 174.0, "confidence": 0.95},
                ],
                "wires": [
                    {"number": "102", "from_tag": "L1", "from_pin": "L1", "to_tag": "CR1-13", "to_pin": "3"},
                    {"number": "110", "from_tag": "CR1-13", "from_pin": "4", "to_tag": "PL1", "to_pin": "X1"},
                    {"number": "104", "x": 256.0, "y": 188.0, "ambiguous": True, "confidence": 0.88},
                ],
            }
        ],
    }
    synth, _rep = build_session(stitched, catalog=default_catalog(), layout_mode="ladder")
    pl_returns = [
        wire.number
        for sh in synth.project.sheets
        for wire in sh.wires
        if {"PL1.X2", "L2"} == {str(wire.frm), str(wire.to)}
    ]
    assert pl_returns == ["104"], pl_returns


def test_rail_named_return_accepts_sheet_stored_rail_number_and_rejects_other_nets(tmp_path: Path):
    """When dpi_150 stored 104 only at y=72, PL1's L2 return accepts 104 and rejects 106 and 999."""

    session = _ladder("dpi_150 PL1 return")
    session.place("nema.operators.pb_nc", ref="PB2", x=60, y=100, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=110, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=220, y=100, snap=False)
    session.place("nema.protection.ol_contact", ref="OL1", x=270, y=100, snap=False)
    session.place("nema.contactors.aux_no", ref="CR1-13", x=50, y=182, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=88, y=182, snap=False)
    _sheet(session).component("PL1").properties.update({"print_import_x": 88.0, "print_import_y": 182.0})
    session.wire("L1", "PB2.1", number="105")
    session.wire("PB2.2", "PB1.3", number="106")
    session.wire("PB1.4", "M1.A1", number="107")
    session.wire("M1.A2", "OL1.95", number="108")
    session.wire("OL1.96", "L2", number="104")
    session.wire("L1", "CR1-13.3", number="102")
    session.wire("CR1-13.4", "PL1.X1", number="110")
    pl_ret = session.wire("PL1.X2", "L2", number="L2")
    session.project.properties["print_import"] = {
        "items": [{"kind": "wire", "text": "104", "sheet": "elementary", "x": 248.0, "y": 72.0, "reason": "printed"}]
    }
    gap = next(
        item
        for item in remaining_gaps(session)
        if item["kind"] == "suspect_number" and item.get("wire_id") == pl_ret.id
    )
    unseen_acc, unseen_drop = review_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "999", "confidence": 0.95}]},
        remaining_gaps(session),
    )
    assert unseen_acc == []
    assert any(row["reason"] == "guessed wire number" for row in unseen_drop)

    other_net = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "106", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert pl_ret.number == "L2", other_net

    kept = replay_cleanup(
        session,
        {"answers": [{"gap": gap["id"], "number": "104", "confidence": 0.95}]},
        image=_scan(tmp_path),
    )
    assert any(item.get("kind") == "replace_number" and item.get("number") == "104" for item in kept["added"]), kept
    assert pl_ret.number == "104"


def test_cleanup_placed_transformer_detects_open_stubs_from_phone_scan_ink(tmp_path: Path):
    """A cleanup-placed transformer gets ink-proven open stubs from the prepared phone scan, never on blank paper."""

    from PIL import Image

    from panelcad.importer.cleanup import _completion_issues
    from panelcad.importer.ingest import _image_dpi, _save_upright, prepare_scan

    phone_src = Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "schematic" / "phone.jpg"
    raw = Image.open(phone_src)
    corrected, _angles = prepare_scan(raw)
    prepared_path = tmp_path / "phone_page.png"
    _save_upright(corrected, prepared_path, raw, dpi=_image_dpi(corrected))

    reply = {
        "devices": [
            {"tag": "T1", "library_id": "nema.power.control_transformer", "gap": "unread_device:elementary", "confidence": 0.92, "unreadable": False},
            {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "gap": "unread_device:elementary", "confidence": 0.92, "unreadable": False},
        ],
        "connections": [
            {"gap": "unread_device:elementary", "from": "FU1.2", "to": "T1.H1", "number": "103", "confidence": 0.90, "unreadable": False},
            {"gap": "unread_device:elementary", "from": "FU2.2", "to": "T1.H4", "number": "101", "confidence": 0.90, "unreadable": False},
            {"gap": "unread_device:elementary", "from": "FU2.1", "to": "L2", "number": "104", "confidence": 0.90, "unreadable": False},
        ],
    }

    session = _ladder("Phone ink stubs")
    fu1 = session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=20, snap=False)
    fu1.properties.update({"print_import_x": 58.9, "print_import_y": 23.7})
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=170, snap=False)
    session.wire("L1", "FU1.1", number="102")
    session.wire("L1", "PL1.X1", number="?")
    session.wire("PL1.X2", "L2", number="104")

    replay_cleanup(session, reply, image=str(prepared_path))
    t1 = _sheet(session).component("T1")
    stubs = {s.strip() for s in str((t1.properties or {}).get("print_open_stubs") or "").split(",") if s.strip()}
    assert stubs == {"H2", "H3", "X1", "X2"}, stubs
    assert not any("T1 secondary" in item for item in _completion_issues(session))
    assert "open_pin:elementary:T1.secondary" not in {gap["id"] for gap in remaining_gaps(session)}

    # Blank white scan: never blanket-mark catalog pins open without ink evidence.
    blank_path = tmp_path / "blank_page.png"
    Image.new("RGB", (800, 520), (255, 255, 255)).save(blank_path)
    blank_session = _ladder("Blank scan stubs")
    blank_session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=20, snap=False)
    blank_session.place("nema.operators.pilot_light", ref="PL1", x=200, y=170, snap=False)
    blank_session.wire("L1", "FU1.1", number="102")
    blank_session.wire("L1", "PL1.X1", number="?")
    blank_session.wire("PL1.X2", "L2", number="104")

    replay_cleanup(blank_session, reply, image=str(blank_path))
    blank_t1 = _sheet(blank_session).component("T1")
    assert not (blank_t1.properties or {}).get("print_open_stubs")
    assert any("T1 secondary does not feed a wire" in item for item in _completion_issues(blank_session))
    assert "open_pin:elementary:T1.secondary" in {gap["id"] for gap in remaining_gaps(blank_session)}


def test_cleanup_open_stubs_use_scan_aspect_when_sheet_stays_a3(tmp_path: Path):
    """A cleanup-placed T1 on an A3 default sheet still records open stubs from a no-DPI phone page."""

    from PIL import Image

    from panelcad.importer.cleanup import _completion_issues, _scan_frame_mm
    from panelcad.importer.ingest import prepare_scan

    phone_src = Path(__file__).resolve().parent / "fixtures" / "scan_ladder" / "schematic" / "phone.jpg"
    corrected, _angles = prepare_scan(Image.open(phone_src))
    # Real ImportJob pages can land without a DPI tag while the ladder sheet stays A3.
    page_path = tmp_path / "phone_page_nodpi.png"
    corrected.convert("RGB").save(page_path)
    picture = Image.open(page_path)
    assert picture.info.get("dpi") in (None, ())
    assert _scan_frame_mm(picture, 420.0, 297.0) == (431.8, 279.4)

    session = _ladder("Phone A3 stubs")
    sheet = _sheet(session)
    sheet.page.size = "A3"
    sheet.page.orientation = "landscape"
    assert sheet.page.dimensions() == (420.0, 297.0)
    fu1 = session.place("nema.protection.fuse_10a", ref="FU1", x=40, y=20, snap=False)
    fu1.properties.update({"print_import_x": 58.9, "print_import_y": 23.7})
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=170, snap=False)
    session.wire("L1", "FU1.1", number="102")
    session.wire("L1", "PL1.X1", number="?")
    session.wire("PL1.X2", "L2", number="104")

    reply = {
        "devices": [
            {"tag": "T1", "library_id": "nema.power.control_transformer", "gap": "unread_device:elementary", "confidence": 0.92, "unreadable": False},
            {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "gap": "unread_device:elementary", "confidence": 0.92, "unreadable": False},
        ],
        "connections": [
            {"gap": "unread_device:elementary", "from": "FU1.2", "to": "T1.H1", "number": "103", "confidence": 0.90, "unreadable": False},
            {"gap": "unread_device:elementary", "from": "FU2.2", "to": "T1.H4", "number": "101", "confidence": 0.90, "unreadable": False},
            {"gap": "unread_device:elementary", "from": "FU2.1", "to": "L2", "number": "104", "confidence": 0.90, "unreadable": False},
        ],
    }
    replay_cleanup(session, reply, image=str(page_path))
    t1 = sheet.component("T1")
    stubs = {s.strip() for s in str((t1.properties or {}).get("print_open_stubs") or "").split(",") if s.strip()}
    assert stubs == {"H2", "H3", "X1", "X2"}, stubs
    assert not any("T1 secondary" in item for item in _completion_issues(session))


def test_leftover_wire_question_or_suspect_number_blocks_issue(tmp_path: Path):
    """Confirming tags persists, but leftover wire '?' / suspect_number / unread devices still block issue."""

    from panelcad.importer.cleanup import _completion_issues, _issue

    session = _starter()
    session.wire("L1", "PL1.X1", number="110")
    _sheet(session).component("ES1").properties["print_import_confidence"] = "0.70"
    _sheet(session).component("PB2").properties["print_import_confidence"] = "0.72"
    result = replay_cleanup(
        session,
        {
            "devices": [
                {"tag": "ES1", "library_id": "nema.operators.estop", "gap": "suspect_tag:ES1", "confidence": 0.90, "unreadable": False},
                {"tag": "PB2", "library_id": "nema.operators.pb_nc", "gap": "suspect_tag:PB2", "confidence": 0.90, "unreadable": False},
            ],
            "connections": [
                {"gap": "return:PL1", "from": "PL1.X2", "to": "L2", "number": "104", "confidence": 0.92, "unreadable": False},
            ],
        },
        image=_scan(tmp_path),
    )
    # Confirmed tags stay confirmed and leave the suspect_tag list.
    assert str((_sheet(session).component("ES1").properties or {}).get("print_import_confirmed") or "") == "yes"
    assert not any(gap["kind"] == "suspect_tag" for gap in remaining_gaps(session))
    # PB1.4 -> M1.A1 is still '?', so the unread_device slot remains and the sheet does not issue.
    assert any(gap["kind"] == "wire_number" and gap.get("number") == "?" for gap in remaining_gaps(session))
    assert any(gap["kind"] == "unread_device" for gap in remaining_gaps(session))
    assert "unread device remains" in _completion_issues(session)
    assert result["issued"] is False, result
    assert _issue(session) is False

    # A sheet that only carries a suspect_number also does not issue.
    numbered = _starter()
    numbered.wire("L1", "PL1.X1", number="110")
    numbered.wire("PL1.X2", "L2", number="104")
    hop = next(w for w in _sheet(numbered).wires if {"PB1.4", "M1.A1"} == {str(w.frm), str(w.to)})
    hop.number = "100"  # ends in 00 → suspect_number
    assert any(gap["kind"] == "suspect_number" and gap.get("wire_id") == hop.id for gap in remaining_gaps(numbered))
    assert "unread device remains" in _completion_issues(numbered)
    assert _issue(numbered) is False

    # Genuinely unreadable '?' device or orphan contact still blocks completion.
    with_unknown = _starter()
    with_unknown.wire("L1", "PL1.X1", number="110")
    with_unknown.wire("PL1.X2", "L2", number="104")
    with_unknown.place("nema.operators.pb_no", ref="?", x=160, y=100, snap=False)
    with_unknown.wire("L1", "?.3", number="102")
    with_unknown.wire("?.4", "M1.A1", number="107")
    assert "unread device remains" in _completion_issues(with_unknown)
    assert _issue(with_unknown) is False

    with_orphan = _starter()
    with_orphan.wire("L1", "PL1.X1", number="110")
    with_orphan.wire("PL1.X2", "L2", number="104")
    with_orphan.place("nema.contactors.aux_no", ref="CR9-13", x=80, y=180, snap=False)
    with_orphan.wire("L1", "CR9-13.3", number="102")
    with_orphan.wire("CR9-13.4", "PL1.X1", number="110")
    assert "unread device remains" in _completion_issues(with_orphan)
    assert _issue(with_orphan) is False


def test_dpi_150_primary_route_contact_pins_and_104_return_stay_clean(tmp_path: Path):
    """dpi_150 keeps FU1.1->L1 off FU2, preserves contact pin 3 for L1, and attaches 104 to FU2.1->L2."""

    from panelcad.importer.build import build_session
    from panelcad.importer.cleanup import _completion_issues
    from panelcad.importer.raster import collapse_wire_numbers
    from panelcad.importer.tile import merge_extractions
    from panelcad.library.catalog import default_catalog

    # Two loose 104 tokens on separate rungs in the same low-DPI tile both survive collapse_wire_numbers.
    collapsed = collapse_wire_numbers(
        [
            {"number": "104", "x": 247.8, "y": 72.2, "ambiguous": True, "confidence": 0.4},
            {"number": "104", "x": 256.9, "y": 178.3, "ambiguous": True, "confidence": 0.4},
        ]
    )
    assert len(collapsed) == 2

    catalog = default_catalog()
    tile_0 = {
        "devices": [
            {"tag": "FU1", "library_id": "nema.protection.fuse_10a", "x": 58.93, "y": 23.71, "confidence": 0.9, "tile_x": 0, "tile_y": 0, "tile_w": 272, "tile_h": 280},
            {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "x": 59.10, "y": 61.98, "confidence": 0.72, "tile_x": 0, "tile_y": 0, "tile_w": 272, "tile_h": 280},
            {"tag": "T1", "library_id": "nema.power.control_transformer", "x": 100.92, "y": 18.97, "confidence": 0.9, "open_stubs": ["H2", "H3", "X1", "X2"], "tile_x": 0, "tile_y": 0, "tile_w": 272, "tile_h": 280},
            {"tag": "ES1", "library_id": "nema.operators.estop", "x": 40.81, "y": 79.76, "confidence": 0.72},
            {"tag": "PB2", "library_id": "nema.operators.pb_nc", "x": 81.11, "y": 81.79, "confidence": 0.9},
            {"tag": "PB1", "library_id": "nema.operators.pb_no", "x": 116.84, "y": 81.79, "confidence": 0.9},
            {"tag": "M1-13", "library_id": "nema.contactors.aux_no", "x": 117.01, "y": 117.69, "confidence": 0.9},
            {"tag": "M1-23", "library_id": "nema.contactors.aux_no", "x": 40.81, "y": 171.70, "confidence": 0.9},
            {"tag": "PL1", "library_id": "nema.operators.pilot_light", "x": 80.77, "y": 169.84, "confidence": 0.9},
            {"tag": "M1-33", "library_id": "nema.contactors.aux_no", "x": 40.81, "y": 201.85, "confidence": 0.9},
        ],
        "wires": [
            {"number": "101", "x": 94, "y": 66, "from_tag": "T1", "from_pin": "H4", "to_tag": "FU2", "to_pin": "2", "confidence": 0.9},
            {"number": "103", "x": 94, "y": 28, "from_tag": "T1", "from_pin": "H1", "to_tag": "FU1", "to_pin": "2", "confidence": 0.9},
            {"number": "102", "x": 40, "y": 28, "from_tag": "FU1", "from_pin": "1", "to_tag": "L1", "to_pin": "L1", "confidence": 0.9},
            {"number": "100", "x": 68, "y": 88, "from_tag": "ES1", "from_pin": "2", "to_tag": "PB2", "to_pin": "1", "confidence": 0.9},
            {"number": "106", "x": 100, "y": 88, "from_tag": "PB2", "from_pin": "2", "to_tag": "PB1", "to_pin": "3", "confidence": 0.9},
            {"number": "106", "x": 100, "y": 110, "from_tag": "PB2", "from_pin": "2", "to_tag": "M1-13", "to_pin": "3", "confidence": 0.9},
            {"number": "?", "x": 145, "y": 110, "from_tag": "PB1", "from_pin": "4", "to_tag": "M1-13", "to_pin": "4", "confidence": 0.4},
            {"number": "110", "x": 75, "y": 178, "from_tag": "M1-23", "from_pin": "4", "to_tag": "PL1", "to_pin": "X1", "confidence": 0.9},
            {"number": "104", "x": 247.8, "y": 72.2, "ambiguous": True, "confidence": 0.4, "tile_x": 0, "tile_y": 0, "tile_w": 272, "tile_h": 280},
            {"number": "104", "x": 256.9, "y": 178.3, "ambiguous": True, "confidence": 0.4, "tile_x": 0, "tile_y": 0, "tile_w": 272, "tile_h": 280},
        ],
    }
    tile_1 = {
        "devices": [
            {"tag": "M1", "library_id": "nema.contactors.coil", "x": 152.91, "y": 91.78, "confidence": 0.9},
            {"tag": "OL1", "library_id": "nema.protection.ol_contact", "x": 192.87, "y": 89.75, "confidence": 0.72},
            {"tag": "X1-1", "library_id": "nema.terminals.feed_through", "x": 299.4, "y": 199.5, "confidence": 0.9},
        ],
        "wires": [
            {"number": "?", "x": 148, "y": 90, "from_tag": "PB1", "from_pin": "4", "to_tag": "M1", "to_pin": "A1", "confidence": 0.4},
            {"number": "?", "x": 250, "y": 125, "from_tag": "M1-13", "from_pin": "3", "to_tag": "L2", "to_pin": "L2", "confidence": 0.4},
            {"number": "108", "x": 185, "y": 95, "from_tag": "M1", "from_pin": "A2", "to_tag": "OL1", "to_pin": "95", "confidence": 0.9},
            {"number": "104", "x": 315.0, "y": 102.2, "from_tag": "OL1", "from_pin": "96", "to_tag": "L2", "to_pin": "L2", "confidence": 0.9, "ambiguous": False, "tile_x": 160, "tile_y": 0, "tile_w": 272, "tile_h": 280},
            {"number": "201", "x": 182, "y": 208, "from_tag": "M1-33", "from_pin": "4", "to_tag": "X1-1", "to_pin": "A", "confidence": 0.9},
            {"number": "202", "x": 342, "y": 215, "from_tag": "X1-1", "from_pin": "B", "to_tag": "L2", "to_pin": "L2", "confidence": 0.9},
            {"number": "104", "x": 247.8, "y": 72.2, "ambiguous": True, "confidence": 0.4, "tile_x": 160, "tile_y": 0, "tile_w": 272, "tile_h": 280},
            {"number": "104", "x": 256.9, "y": 178.3, "ambiguous": True, "confidence": 0.4, "tile_x": 160, "tile_y": 0, "tile_w": 272, "tile_h": 280},
        ],
    }
    merged = merge_extractions([tile_0, tile_1])
    session, _rep = build_session(
        {
            "title_block": {"standard": "NEMA/ANSI"},
            "sheets": [{"id": "page-001", "kind": "schematic", "width_mm": 431.8, "devices": merged["devices"], "wires": merged["wires"]}],
        },
        catalog=catalog,
        layout_mode="ladder",
    )
    sheet = session.project.sheet("page-001")
    p2 = session.add_sheet("page-001-p2", title="Elementary 2")
    p2.page.size = "TABLOID"
    p2.page.orientation = "landscape"
    p2.drawing_style = "ladder"
    p2.rail_hot = "L1"
    p2.rail_return = "L2"
    session.place("nema.relays.cr", ref="CR1", x=347.0, y=35.0, sheet_id="page-001-p2", snap=False)
    session.wire("L1", "CR1.A1", number="L1", sheet_id="page-001-p2")
    session.wire("CR1.A2", "L2", number="113", sheet_id="page-001-p2")

    fu2_ret = next(w for w in sheet.wires if {"FU2.1", "L2"} == {str(w.frm), str(w.to)})
    assert fu2_ret.number == "104"
    assert not any(gap["kind"] == "unattached_number" and gap.get("number") == "104" for gap in remaining_gaps(session))

    w_m13_l2 = next(w.id for w in sheet.wires if {"M1-13.3", "L2"} == {w.frm, w.to})
    w_pb1_m1 = next(w.id for w in sheet.wires if {"PB1.4", "M1.A1"} == {w.frm, w.to})
    w_pb1_m13 = next(w.id for w in sheet.wires if {"PB1.4", "M1-13.4"} == {w.frm, w.to})
    w_es1_pb2 = next(w.id for w in sheet.wires if {"ES1.2", "PB2.1"} == {w.frm, w.to})

    result = replay_cleanup(
        session,
        {
            "devices": [
                {"tag": "FU2", "library_id": "nema.protection.fuse_10a", "gap": "suspect_tag:FU2", "confidence": 0.9, "unreadable": False},
                {"tag": "ES1", "library_id": "nema.operators.estop", "gap": "suspect_tag:ES1", "confidence": 0.9, "unreadable": False},
                {"tag": "OL1", "library_id": "nema.protection.ol_contact", "gap": "suspect_tag:OL1", "confidence": 0.9, "unreadable": False},
            ],
            "wires": [
                {"gap": f"wire:{w_pb1_m1}", "number": "107", "confidence": 0.85, "unreadable": False},
                {"gap": f"wire:{w_pb1_m13}", "number": "107", "confidence": 0.85, "unreadable": False},
            ],
            "answers": [
                {"gap": f"suspect_number:page-001:{w_es1_pb2}", "number": "105", "confidence": 0.9},
                {"gap": "duplicate_tag:page-001:M1-23:PL1", "retag": "CR1-13", "confidence": 0.9},
            ],
            "challenges": [
                {"gap": "contacts_only:page-001:ES1-M1-13-PB1-PB2", "target": w_m13_l2, "reason": "M1-13 is not wired to L2", "evidence": "Seal-in branch", "confidence": 0.85},
            ],
        },
        image=_scan(tmp_path),
    )
    assert result["called"] is True, result
    assert elementary_layout_issues(session, sheet) == []
    assert any({"L1", "CR1-13.3"} == {str(w.frm), str(w.to)} for w in sheet.wires)
    assert any({"L1", "M1-33.3"} == {str(w.frm), str(w.to)} for w in sheet.wires)
    assert any({"FU2.1", "L2"} == {str(w.frm), str(w.to)} and str(w.number) == "104" for w in sheet.wires)
    # Open wire/number questions must keep the sheet from issuing even when layout is clean.
    if any(gap["kind"] in {"wire_number", "suspect_number", "duplicate_tag", "unread_device"} for gap in remaining_gaps(session)):
        assert "unread device remains" in _completion_issues(session)
        assert result["issued"] is False
