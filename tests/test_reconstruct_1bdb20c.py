"""Regression tests for the 1bdb20c Windows review (items 1–15)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.cli.agent import OP_HELP, Agent
from panelcad.core.checks import run_checks
from panelcad.io.bom import instance_rows
from panelcad.quote.build import build_quote
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.geometry import mm_per_px, photo_to_mm
from panelcad.reconstruct.store import EMBED_KEY
from panelcad.reconstruct.vision import FakeVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _photo_session(tmp_path: Path) -> tuple[Session, Path, dict]:
    _app()
    shot = tmp_path / "backpanel.png"
    meta = write_backpanel_photo(shot)
    job = tmp_path / "asbuilt.pcad.yaml"
    session = Session.create("Review")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    return session, job, meta


def _face_scale(session: Session, photo_id: str, meta: dict) -> None:
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "face",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta.get("enclosure_width_mm") or 400,
        known_height_mm=meta.get("enclosure_height_mm") or 500,
    )


def _recordings(meta: dict, **extra) -> dict:
    region = meta["part_region"]
    w = float(meta["width_px"])
    h = float(meta["height_px"])
    device = {
        "tag": extra.get("tag", "K1"),
        "brand": extra.get("brand", ""),
        "part_number": extra.get("part_number", ""),
        "label_text": extra.get("label_text", "DIRTY LABEL"),
        "nx": region["x"] / w,
        "ny": region["y"] / h,
        "nw": region["w"] / w,
        "nh": region["h"] / h,
        "confidence": extra.get("confidence", 0.4),
    }
    return {"p1": {"devices": [device], "strips": [], "unreadable": False, **extra.get("extract", {})}}


def test_item_01_generic_place_refused_on_empty_reconstruct_layout():
    session = Session.create("Plain")
    session.add_sheet("panel", kind="panel_layout")
    assert session.project.reconstruct is None
    try:
        session.place("iec.contactors.ac3_9a", ref="KG", x=80, y=80, sheet_id="panel")
    except ValueError as exc:
        assert "generic" in str(exc).lower() or "layout-ready" in str(exc).lower()
    else:
        raise AssertionError("generic place on a panel layout must fail")
    assert not session.project.sheet("panel").components
    agent = Agent()
    agent.session = session
    reply = agent.handle({"op": "place", "library_id": "iec.contactors.ac3_9a", "ref": "KG2", "x": 80, "y": 80, "sheet": "panel"})
    assert reply.get("ok") is False
    assert not session.project.sheet("panel").components


def test_item_02_embed_survives_main_style_save_as(tmp_path: Path):
    session, job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    rec = _recordings(meta, tag="U7", label_text="??")
    rec["p1"]["strips"] = [{"tag": "X1", "terminals": [], "wire_numbers": [{"number": "104", "terminal": "1"}]}]
    session.reconstruct_identify(vision=FakeVision(rec))
    session.reconstruct_place_needs_info()
    trace = next((step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire"), None)
    if trace:
        session.reconstruct_answer(trace["id"], "K1:A1")
    session.save(job)
    dest = tmp_path / "copy" / "moved.pcad.yaml"
    dest.parent.mkdir()
    session.save(dest)
    text = dest.read_text(encoding="utf-8")
    if "\nreconstruct:" in text:
        head, rest = text.split("\nreconstruct:", 1)
        # Drop the top-level reconstruct key the way main extra=ignore would.
        lines = rest.splitlines()
        skip = 1
        while skip < len(lines) and (lines[skip].startswith(" ") or lines[skip].startswith("\t") or not lines[skip].strip()):
            skip += 1
        dest.write_text(head + "\n" + "\n".join(lines[skip:]), encoding="utf-8")
    sidecar = dest.with_name("moved.reconstruct.yaml")
    if sidecar.exists():
        sidecar.unlink()
    raw = dest.read_text(encoding="utf-8")
    assert EMBED_KEY in raw or "panelcad_reconstruct" in raw
    reopened = Session.open(dest)
    assert reopened.project.reconstruct is not None
    assert reopened.project.reconstruct.photos
    assert reopened.project.reconstruct.items
    inferred = [wire for sheet in reopened.project.sheets for wire in sheet.wires if getattr(wire, "inferred", False)]
    assert inferred or any(item.wire_number for item in reopened.project.reconstruct.items)


def test_item_03_wizard_defaults_do_not_write_scale_and_cancel_rolls_back(tmp_path: Path, monkeypatch):
    from panelcad.gui.reconstruct_wizard import ReconstructWizard
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    session, job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    before = session.project.model_dump(mode="json")
    dialog = ReconstructWizard(session)
    assert dialog.cancel.text() == "Cancel"
    assert dialog.p1.text() == ""
    assert dialog.p2.text() == ""
    assert dialog.known.value() == 0
    dialog._apply_scale()
    photo = session.project.reconstruct.photo(photo_id)
    assert photo.scale is None
    assert session.reconstruct_status()["scaled"] == []
    session.add_sheet("asbuilt", kind="schematic")
    session.place("iec.terminals.x_2p5", ref="X9", x=20, y=20, sheet_id="asbuilt")
    session.place("iec.contactors.coil", ref="K9", x=80, y=20, sheet_id="asbuilt")
    session.wire("X9.A", "K9.A1", sheet_id="asbuilt", number="9")
    session.project.sheet("asbuilt").wires[-1].id = "wnew"
    dialog.reject()
    after = session.project.model_dump(mode="json")
    assert after["reconstruct"] == before["reconstruct"]
    wires = [wire for sheet in session.project.sheets for wire in sheet.wires]
    assert not any(wire.id == "wnew" or str(wire.number or "") == "9" for wire in wires)


def test_item_04_malformed_nx_and_confidence_create_no_items(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    bad_nx = FakeVision(
        {
            "p1": {
                "devices": [
                    {
                        "tag": "MC-9b",
                        "part_number": "MC-9b",
                        "nx": "not-a-number",
                        "ny": 0.2,
                        "nw": 0.1,
                        "nh": 0.1,
                        "confidence": 0.9,
                    }
                ]
            }
        }
    )
    result = session.reconstruct_identify(vision=bad_nx)
    assert [item for item in result["items"] if item["role"] == "part"] == []
    assert all(item.get("part_number") in {"", None} for item in result["items"])
    bad_conf = FakeVision(
        {
            "p1": {
                "devices": [
                    {
                        "tag": "MC-9b",
                        "part_number": "MC-9b",
                        "nx": 0.2,
                        "ny": 0.2,
                        "nw": 0.1,
                        "nh": 0.1,
                        "confidence": "high",
                    }
                ]
            }
        }
    )
    result = session.reconstruct_identify(vision=bad_conf)
    assert [item for item in result["items"] if item["role"] == "part"] == []
    findings = run_checks(session.project, session.catalog)
    assert any(row.check == "reconstruct_unreadable" for row in findings)


def test_item_05_set_scale_docs_match_return_and_drawing_tools():
    from panelcad.ai import drawing as drawing_mod

    help_text = OP_HELP["reconstruct_set_scale"]
    assert "known_height_mm" in help_text
    assert "scaled=false" in help_text or "8 mm" in help_text
    assert "two known lengths" in help_text or "face" in help_text
    names = {name for name in dir(drawing_mod) if name.startswith("reconstruct_")}
    for op in (
        "reconstruct_tag_photo",
        "reconstruct_list_photos",
        "reconstruct_checklist",
        "reconstruct_confirm_connection",
        "reconstruct_attach_print",
        "reconstruct_status",
        "reconstruct_set_scale",
    ):
        assert op in names
    doc = Path("docs/AI_INTERFACE.md").read_text(encoding="utf-8")
    assert "scaled: false" in doc
    assert "Refuses when the residual is above 8 mm" not in doc


def test_item_05b_set_scale_30deg_returns_scaled_false(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(photo_id, "measured", meta["corners"][0], meta["corners"][1], known_mm=400)
    payload = session.reconstruct_set_scale(photo_id, "measured", meta["corners"][0], meta["corners"][3], known_mm=40)
    assert payload["scaled"] is False
    assert payload.get("error_mm") is None or payload["error_mm"] > 8
    assert "Refuses when the residual" not in Path("docs/AI_INTERFACE.md").read_text(encoding="utf-8")


def test_item_06_envelope_convert_bom_has_no_contactor_fields(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="U7", label_text="UNKNOWN")))
    item = next(row for row in session.reconstruct_unconfirmed()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=50,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="backplate",
        library_id="iec.contactors.ac3_9a",
        tag="U7",
    )
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_place_confirmed()
    rows = instance_rows(session.project, session.catalog)
    hit = next(row for row in rows if row["ref"] == "U7")
    assert hit["library_id"] == "reconstruct.measured_envelope"
    assert "Measured envelope" in hit["description"]
    blob = " ".join(f"{k}={v}" for k, v in hit.items())
    for forbidden in ("poles=3", "Ie=9A", "AC3=4kW", "coil=24VDC", "IEC 60947-4-1"):
        assert forbidden not in blob
    assert hit.get("part_number", "") in {"", "U7"}


def test_item_07_prose_scale_answer_stays_open(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(photo_id, "din_rail", meta["rail_p1"], meta["rail_p2"])
    step = next(row for row in session.reconstruct_checklist()["checklist"] if row["kind"] == "confirm_scale")
    answered = session.reconstruct_answer(step["id"], "I measured the enclosure inside width as 600 mm")
    assert answered.get("status") == "open"
    assert "not stored" in str(answered.get("message") or "").lower()
    nxt = session.reconstruct_next_question()
    assert nxt["done"] is False
    photo = session.project.reconstruct.photo(photo_id)
    assert mm_per_px(photo) is None
    assert session.reconstruct_status()["scaled"] == []


def test_item_08_eight_distinct_needs_info_finding_keys(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="U7", label_text="??")))
    session.reconstruct_place_needs_info()
    findings = [row for row in run_checks(session.project, session.catalog) if row.check == "reconstruct_needs_info"]
    keys = [row.key() for row in findings]
    assert len(findings) == 1
    assert len(set(keys)) == len(keys)
    assert "U7" in findings[0].message
    assert "size unknown" in findings[0].message
    assert "width_mm" not in findings[0].message


def test_item_09_confirmed_envelope_clears_unreadable_and_compare(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    rec = _recordings(meta, tag="U4", label_text="??")
    rec["p1"]["unreadable"] = True
    session.reconstruct_identify(vision=FakeVision(rec))
    item = next(row for row in session.project.reconstruct.items if row.role == "part")
    item.unreadable = True
    session.reconstruct_confirm_item(
        item.id,
        envelope_w_mm=50,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="backplate",
        tag="U4",
    )
    assert session.project.reconstruct.item(item.id).unreadable is False
    problems = run_checks(session.project, session.catalog)
    assert not any(row.check == "reconstruct_unconfirmed" for row in problems)
    assert not any("unreadable" in row.message.lower() and "U4" in row.message for row in problems)
    index = tmp_path / "old.yaml"
    index.write_text("tags:\n  - {tag: U4, catalog: ''}\nwires: []\n", encoding="utf-8")
    compared = session.reconstruct_compare_print(index)
    messages = [row["message"] for row in compared.get("mismatches") or []]
    assert not any("no panel photo has confirmed that tag" in msg for msg in messages)


def test_item_10_catalog_convert_drops_placeholder_wording(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, part_number="MC-9b", brand="LS Electric", tag="K1")))
    session.reconstruct_place_needs_info()
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    ref = item["placed_ref"]
    sheet = session.project.sheet(item["sheet"])
    before = dict(sheet.component(ref).properties)
    assert before.get("description") == "INFO NEEDED"
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", manufacturer="LS Electric", tag="K1")
    after = sheet.component(ref).properties
    assert after.get("description") != "INFO NEEDED"
    assert "approx_error_mm" not in after
    assert "reconstruct placeholder" not in str(after.get("notes") or "")


def test_item_11_placeholder_has_no_photo_derived_mm(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="U7", label_text="??")))
    placed = session.reconstruct_place_needs_info()
    if placed["placed"]:
        ref = placed["placed"][0]["ref"]
        sheet = session.project.sheet(placed["placed"][0]["sheet"])
    else:
        item = next(row for row in session.project.reconstruct.items if row.role == "part" and row.placed_ref)
        ref = item.placed_ref
        sheet = session.project.sheet(item.sheet)
    props = sheet.component(ref).properties
    forbidden = {
        "width_mm",
        "height_mm",
        "depth_mm",
        "mount",
        "display_width_mm",
        "display_height_mm",
        "approx_width_mm",
        "approx_height_mm",
        "approx_error_mm",
    }
    assert forbidden.isdisjoint(props)
    copy = session.duplicate_component(ref, sheet_id=sheet.id)
    assert forbidden.isdisjoint(copy.properties)


def test_item_12_hand_edited_placeholder_is_flagged_and_hidden_from_search(tmp_path: Path):
    dest = tmp_path / "hand.pcad.yaml"
    dest.write_text(
        "format: panelcad\nname: Hand\nsheets:\n"
        "  - id: main\n    kind: schematic\n    components:\n"
        "      - id: UHAND\n        library_id: reconstruct.needs_info\n        x: 10\n        y: 10\n"
        "        properties: {reconstruct_needs_info: yes}\n",
        encoding="utf-8",
    )
    session = Session.open(dest)
    findings = run_checks(session.project, session.catalog)
    assert any(row.check == "reconstruct_needs_info" and row.ref == "UHAND" for row in findings)
    fresh = Session.create("Search")
    hits = fresh.catalog.search("INFO NEEDED")
    assert not any(d.library_id == "reconstruct.needs_info" for d in hits)
    assert not any(d.library_id == "reconstruct.needs_info" for d in fresh.catalog.search("needs_info"))


def test_item_13_quote_lists_placeholder_as_unpriced_without_changing_priced(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    session.place("iec.contactors.ac3_9a", ref="KREAL", x=20, y=20, properties={"unit_cost": "12.50"})
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="U7", label_text="??")))
    session.reconstruct_place_needs_info()
    with_ph = build_quote(session.project, session.catalog)
    assert [row for row in with_ph["parts"] if row.get("needs_info")] == []
    assert with_ph["needs_info"] > 0
    assert with_ph["unpriced"] == 0
    assert with_ph["incomplete"] is True
    assert with_ph["total_label"].startswith("INCOMPLETE")
    assert "need info" in with_ph["total_label"]
    assert "unpriced" not in with_ph["total_label"]
    priced_with = with_ph["totals"]["parts_sell"]
    labor_with = with_ph["totals"]["labor_sell"]
    for sheet in session.project.sheets:
        sheet.components = [comp for comp in sheet.components if str((comp.properties or {}).get("reconstruct_needs_info") or "") not in {"1", "true", "yes"} and comp.library_id != "reconstruct.needs_info"]
    without = build_quote(session.project, session.catalog)
    assert without["unpriced"] == 0
    assert without["incomplete"] is False
    assert without["total_label"] == "Total"
    assert without["totals"]["parts_sell"] == priced_with
    assert without["totals"]["labor_sell"] == labor_with


def test_item_14_face_mm_per_px_matches_homography(tmp_path: Path):
    _app()
    image = QImage(200, 200, QImage.Format.Format_RGB32)
    image.fill(QColor("#667788"))
    shot = tmp_path / "face.png"
    image.save(str(shot), "PNG")
    job = tmp_path / "face.pcad.yaml"
    session = Session.create("Face")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, [(0, 0), (200, 0), (200, 200), (0, 200)])
    payload = session.reconstruct_set_scale(photo_id, "face", (0, 0), (200, 0), known_mm=100, known_height_mm=100)
    photo = session.project.reconstruct.photo(photo_id)
    mapped = photo_to_mm(photo, 180, 160)
    assert mapped is not None
    assert abs(mapped[0] - 90) < 0.6
    assert abs(mapped[1] - 80) < 0.6
    ratio = mm_per_px(photo)
    assert ratio is not None
    assert abs(ratio - 0.5) < 0.05
    assert payload["mm_per_px"] == ratio
    assert abs(payload["mm_per_px"] - 1.0) > 0.2


def test_item_15_one_trace_answer_one_wire_and_confirm_names_pin(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    rec = _recordings(meta, part_number="MC-9b", brand="LS Electric", tag="K1")
    rec["p1"]["strips"] = [{"tag": "X1", "terminals": [], "wire_numbers": [{"number": "104", "terminal": "1"}]}]
    session.reconstruct_identify(vision=FakeVision(rec))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")
    session.reconstruct_checklist()
    trace = next(step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire")
    session.reconstruct_answer(trace["id"], "K1:A1")
    wires = [wire for sheet in session.project.sheets for wire in sheet.wires]
    inferred = [wire for wire in wires if getattr(wire, "inferred", False)]
    assert len(inferred) == 1
    nxt = session.reconstruct_next_question()
    assert nxt["done"] is False
    step = nxt["step"]
    assert step["kind"] == "confirm_connection"
    assert "A" in (step.get("from_ref") or "") or "A" in (step.get("prompt") or "")
    session.reconstruct_checklist()
    traces = [row for row in session.reconstruct_checklist()["checklist"] if row["kind"] == "trace_wire" and row["status"] == "open"]
    assert traces == []
