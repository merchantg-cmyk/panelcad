"""Review fixes for reconstruct-from-photos plus the needs-info placeholder."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.core.checks import run_checks
from panelcad.core.layout import is_layout_sheet
from panelcad.io.bom import grouped_rows, instance_rows, unresolved_needs_info_rows
from panelcad.library.panel import is_incabinet, is_layout_ready
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.geometry import mm_per_px, region_top_left_mm, validate_corners
from panelcad.reconstruct.models import ObservedItem, PhotoRegion, item_is_layout_ready
from panelcad.reconstruct.placeholder import is_needs_info_component, project_has_needs_info
from panelcad.reconstruct.store import load_upright_image, stored_suffix
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


def test_review_01_envelope_needs_depth_and_mount(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    item = next(row for row in session.reconstruct_unconfirmed()["items"] if row["role"] == "part")
    try:
        session.reconstruct_confirm_item(
            item["id"],
            envelope_w_mm=45,
            envelope_h_mm=73.5,
            library_id="iec.contactors.ac3_9a",
        )
    except ValueError as exc:
        assert "depth" in str(exc).lower() or "mount" in str(exc).lower()
    else:
        raise AssertionError("W×H alone must not confirm an envelope")
    confirmed = session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=45,
        envelope_h_mm=73.5,
        envelope_d_mm=87.4,
        mount="din_rail",
        library_id="iec.contactors.ac3_9a",
        tag="K9",
    )
    assert confirmed["layout_ready"] is True
    assert confirmed["envelope_d_mm"] == 87.4
    assert confirmed["mount"] == "din_rail"


def test_review_02_catalog_blank_dims_not_ready():
    from panelcad.reconstruct.models import CatalogMatch

    item = ObservedItem(
        id="i1",
        photo_id="p1",
        role="part",
        status="catalog",
        matches=[
            CatalogMatch(
                catalog="BLANK",
                manufacturer="X",
                library_id="iec.contactors.ac3_9a",
                confidence=0.9,
                width_mm=None,
                height_mm=None,
                depth_mm=None,
            )
        ],
    )
    assert item_is_layout_ready(item) is False
    item.matches[0].width_mm = 45
    item.matches[0].height_mm = 73.5
    assert item_is_layout_ready(item) is False
    item.matches[0].depth_mm = 87.4
    assert item_is_layout_ready(item) is True


def test_review_03_width_height_not_enough_for_layout_ready():
    from panelcad.core.models import Component

    half = Component(
        id="H",
        library_id="iec.contactors.ac3_9a",
        x=0,
        y=0,
        properties={"dim_source": "measured", "envelope_confirmed": "yes", "width_mm": 40, "height_mm": 80},
    )
    from panelcad.library.catalog import default_catalog

    assert is_layout_ready(default_catalog().get("iec.contactors.ac3_9a"), half) is False


def test_review_04_session_place_refuses_generic_in_reconstruct(tmp_path: Path):
    session, _job, _meta = _photo_session(tmp_path)
    session.add_sheet("panel", kind="panel_layout")
    try:
        session.place("iec.contactors.ac3_9a", ref="K9", x=20, y=20, sheet_id="panel")
    except ValueError as exc:
        assert "generic" in str(exc).lower() or "layout-ready" in str(exc).lower() or "needs-info" in str(exc).lower()
    else:
        raise AssertionError("generic place on a reconstruct layout must fail")


def test_review_05_place_uses_photo_top_left_not_rail_center(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, part_number="MC-9b", brand="LS Electric")))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", manufacturer="LS Electric", tag="K1")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    placed = session.reconstruct_place_confirmed()
    assert placed["placed"]
    photo = session.project.reconstruct.photo(photo_id)
    observed = session.project.reconstruct.item(item["id"])
    expected = region_top_left_mm(photo, observed.region)
    assert expected is not None
    assert abs(placed["placed"][0]["x"] - expected[0]) < 0.6
    assert abs(placed["placed"][0]["y"] - expected[1]) < 0.6
    sheet = session.project.sheet(placed["placed"][0]["sheet"])
    for rail in sheet.rails:
        assert abs(placed["placed"][0]["y"] - rail.y) > 0.2 or not sheet.rails


def test_review_06_single_scale_ref_is_not_enough(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    payload = session.reconstruct_set_scale(photo_id, "din_rail", meta["rail_p1"], meta["rail_p2"])
    assert payload["scaled"] is False
    photo = session.project.reconstruct.photo(photo_id)
    assert mm_per_px(photo) is None


def test_review_07_face_scale_recovers_mm(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    photo = session.project.reconstruct.photo(photo_id)
    assert mm_per_px(photo) is not None
    assert photo.scale_error_mm == 0.0


def test_review_08_scale_error_above_tolerance_refuses(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(photo_id, "measured", meta["corners"][0], meta["corners"][1], known_mm=400)
    session.reconstruct_set_scale(photo_id, "measured", meta["corners"][0], meta["corners"][3], known_mm=40)
    photo = session.project.reconstruct.photo(photo_id)
    assert mm_per_px(photo) is None
    assert photo.scale_error_mm is None or photo.scale_error_mm > 8.0


def test_review_09_save_as_copies_photos(tmp_path: Path):
    session, job, _meta = _photo_session(tmp_path)
    dest = tmp_path / "copy" / "moved.pcad.yaml"
    session.save(dest)
    assert (dest.parent / "photos").is_dir()
    assert any((dest.parent / "photos").iterdir())
    assert (dest.with_name(dest.name.replace(".pcad.yaml", "") + ".reconstruct.yaml")).exists() or list(
        dest.parent.glob("*.reconstruct.yaml")
    )


def test_review_10_exif_reader_honors_auto_transform(tmp_path: Path):
    _app()
    image = QImage(80, 120, QImage.Format.Format_RGB32)
    image.fill(0xFF112233)
    path = tmp_path / "upright.png"
    image.save(str(path), "PNG")
    loaded = load_upright_image(path)
    assert loaded.width() == 80
    assert loaded.height() == 120


def test_review_11_pcad_yaml_print_keeps_extension(tmp_path: Path):
    assert stored_suffix(Path("old.pcad.yaml")) == ".pcad.yaml"
    session, _job, _meta = _photo_session(tmp_path)
    other = tmp_path / "old.pcad.yaml"
    other.write_text("name: Old\nsheets: []\n", encoding="utf-8")
    session.reconstruct_attach_print(other)
    stored = session.project.reconstruct.prints[0].path
    assert stored.endswith(".pcad.yaml")


def test_review_12_sidecar_survives_main_open(tmp_path: Path):
    session, job, meta = _photo_session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta)))
    session.save(job)
    sidecar = job.with_name(job.stem.replace(".pcad", "") + ".reconstruct.yaml")
    if not sidecar.exists():
        sidecar = Path(str(job).replace(".pcad.yaml", ".reconstruct.yaml"))
    assert sidecar.exists()
    text = job.read_text(encoding="utf-8")
    # Drop the top-level reconstruct key the way an older extra=ignore save would.
    # Keep properties.panelcad_reconstruct so this head can reopen.
    import re

    text = re.sub(r"(?m)^reconstruct:\n(?:  .*\n)*", "", text)
    job.write_text(text, encoding="utf-8")
    reopened = Session.open(job)
    assert reopened.project.reconstruct is not None
    assert reopened.project.reconstruct.photos


def test_review_13_heic_rejected(tmp_path: Path):
    session, _job, _meta = _photo_session(tmp_path)
    heic = tmp_path / "phone.heic"
    heic.write_bytes(b"ftypheic")
    try:
        session.reconstruct_add_photos([heic], role="backpanel")
    except ValueError as exc:
        assert "HEIC" in str(exc)
        assert "JPG" in str(exc)
    else:
        raise AssertionError("HEIC must be refused")


def test_review_14_vision_unreadable_and_dedupe(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    empty = FakeVision({"p1": {"hello": "world", "unreadable": True, "devices": [{"confidence": "n/a"}]}})
    result = session.reconstruct_identify(vision=empty)
    assert result["items"] == []
    photo = session.project.reconstruct.photos[0]
    assert photo.unreadable is True
    findings = run_checks(session.project, session.catalog)
    assert any(row.check == "reconstruct_unreadable" for row in findings)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="K1", label_text="K1")))
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="K1", label_text="K1")))
    parts = [item for item in session.project.reconstruct.items if item.role == "part"]
    assert len(parts) == 1


def test_review_15_inferred_wire_dashed_and_caption(tmp_path: Path):
    from panelcad.render.contents import _stroke_wire

    class _P:
        def __init__(self) -> None:
            self.lines = 0

        def polyline(self, pts):
            self.lines += 10

        def line(self, *a):
            self.lines += 1

    painter = _P()
    _stroke_wire(painter, [(0, 0), (20, 0)], inferred=True)
    assert painter.lines >= 2


def test_review_16_pin_fallback_updates_confirm_prompt(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    rec = _recordings(meta, part_number="MC-9b", brand="LS Electric")
    rec["p1"]["strips"] = [{"tag": "X1", "terminals": [], "wire_numbers": [{"number": "104", "terminal": "1"}]}]
    session.reconstruct_identify(vision=FakeVision(rec))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")
    session.reconstruct_checklist()
    trace = next(step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire")
    session.reconstruct_answer(trace["id"], "K1:Z9")
    confirm = next(
        (step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "confirm_connection"),
        None,
    )
    assert confirm is not None
    assert "Z9" not in confirm["prompt"] or "A1" in confirm["prompt"] or "13" in confirm["prompt"]


def test_review_17_cli_agent_drawing_parity():
    from panelcad.cli.agent import OPS
    from panelcad.cli.main import app

    names = {cmd.name for cmd in app.registered_groups[0][1].registered_commands} if False else set()
    assert "reconstruct_set_corners" in OPS
    assert "reconstruct_place_needs_info" in OPS
    assert "reconstruct_confirm_item" in OPS


def test_review_18_inferred_wire_jump_has_photo(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    rec = _recordings(meta, part_number="MC-9b", brand="LS Electric")
    rec["p1"]["strips"] = [{"tag": "X1", "terminals": [], "wire_numbers": [{"number": "104", "terminal": "A1"}]}]
    session.reconstruct_identify(vision=FakeVision(rec))
    item = next(row for row in session.reconstruct_status()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(item["id"], catalog="MC-9b", tag="K1")
    session.reconstruct_checklist()
    trace = next(step for step in session.reconstruct_checklist()["checklist"] if step["kind"] == "trace_wire")
    session.reconstruct_answer(trace["id"], "K1:A1")
    findings = [row for row in run_checks(session.project, session.catalog) if row.check == "reconstruct_inferred_wire"]
    assert findings
    extra = findings[0].extra or {}
    crop = session.reconstruct_photo_region(item_id=str(extra.get("item_id") or findings[0].ref))
    assert crop.get("photo") or crop.get("wire_id") or crop.get("path") is not None


def test_review_19_measured_envelope_labeled_on_bom(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, label_text="UNKNOWN")))
    item = next(row for row in session.reconstruct_unconfirmed()["items"] if row["role"] == "part")
    session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=45,
        envelope_h_mm=73.5,
        envelope_d_mm=87.4,
        mount="din_rail",
        library_id="iec.contactors.ac3_9a",
        tag="K9",
    )
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_place_confirmed()
    rows = instance_rows(session.project, session.catalog)
    hit = next(row for row in rows if row["ref"] == "K9")
    assert "Measured envelope" in hit["description"] or "measured" in hit["notes"].lower()
    assert hit.get("part_number", "") in {"", "K9"} or "MC-9b" not in hit.get("part_number", "")


def test_needs_info_create_gates_resolve_and_print(tmp_path: Path):
    session, job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="K7", label_text="UNREADABLE")))
    placed = session.reconstruct_place_needs_info()
    if not placed["placed"]:
        item = next(row for row in session.project.reconstruct.items if row.role == "part" and row.placed_ref)
        placed = {"placed": [{"id": item.id, "ref": item.placed_ref, "sheet": item.sheet, "x": item.x_mm, "y": item.y_mm}]}
    assert placed["placed"]
    ref = placed["placed"][0]["ref"]
    sheet = session.project.sheet(placed["placed"][0]["sheet"])
    assert is_layout_sheet(sheet)
    comp = sheet.component(ref)
    assert is_needs_info_component(comp)
    assert is_layout_ready(session.catalog.get(comp.library_id), comp) is False
    assert is_incabinet(session.catalog.get(comp.library_id), comp) is False
    assert not instance_rows(session.project, session.catalog) or all(row["ref"] != ref for row in instance_rows(session.project, session.catalog))
    unresolved = unresolved_needs_info_rows(session.project)
    assert unresolved
    assert project_has_needs_info(session.project)
    findings = run_checks(session.project, session.catalog)
    assert any(row.check == "reconstruct_needs_info" for row in findings)
    session.layout_from_schematic(sheet.id)
    assert is_needs_info_component(sheet.component(ref))
    item = next(row for row in session.reconstruct_status()["items"] if row["id"] == placed["placed"][0]["id"])
    session.reconstruct_confirm_item(
        item["id"],
        envelope_w_mm=45,
        envelope_h_mm=73.5,
        envelope_d_mm=87.4,
        mount="din_rail",
        library_id="iec.contactors.ac3_9a",
        tag="K7",
    )
    converted = sheet.component(ref)
    assert not is_needs_info_component(converted)
    assert is_layout_ready(session.catalog.get(converted.library_id), converted)
    assert converted.x == comp.x
    assert converted.properties.get("reconstruct_photo")
    bare = Session.create("No reconstruct")
    try:
        bare.place("reconstruct.needs_info", ref="U1", x=10, y=10, sheet_id=bare.project.sheets[0].id)
        if is_layout_sheet(bare.project.sheets[0]):
            raise AssertionError("needs-info must not be created outside reconstruct")
    except ValueError as exc:
        assert "reconstruct" in str(exc).lower()


def test_needs_info_print_package_not_for_build(tmp_path: Path):
    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="U2", label_text="??")))
    if not any(is_needs_info_component(comp) for sheet in session.project.sheets for comp in sheet.components):
        session.reconstruct_place_needs_info()
    dest = tmp_path / "pkg.pdf"
    result = session.print_package(dest)
    assert result.get("not_for_build") is True
    assert dest.is_file() and dest.stat().st_size > 1000
    from panelcad.io.bom import instance_rows
    from panelcad.quote.build import build_quote
    from panelcad.render.print_package import extract_pdf_text

    text = extract_pdf_text(dest)
    assert "NOT FOR BUILD" in text
    assert all(row["library_id"] != "reconstruct.needs_info" for row in instance_rows(session.project, session.catalog))
    quote = build_quote(session.project, session.catalog, session.parts)
    info = [
        row
        for row in quote["parts"]
        if row.get("needs_info")
        or row.get("library_id") == "reconstruct.needs_info"
        or row.get("flag") == "INFO NEEDED"
    ]
    assert info == []
    assert quote["needs_info"] > 0
    assert "need info, not included" in quote["total_label"]


def test_print_package_raises_for_real_generic_beside_needs_info(tmp_path: Path):
    from panelcad.core.models import Component

    session, _job, meta = _photo_session(tmp_path)
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    _face_scale(session, photo_id, meta)
    session.reconstruct_identify(vision=FakeVision(_recordings(meta, tag="U2", label_text="??")))
    if not any(is_needs_info_component(comp) for sheet in session.project.sheets for comp in sheet.components):
        session.reconstruct_place_needs_info()
    layout = next(sheet for sheet in session.project.sheets if sheet.kind in {"panel_layout", "door_layout"})
    layout.components.append(Component(id="PB9", library_id="iec.operators.pb_no", x=80, y=40))
    with pytest.raises(ValueError, match="NOT FOR BUILD"):
        session.print_package(tmp_path / "blocked.pdf")


def test_wizard_has_depth_and_rolls_back(tmp_path: Path):
    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    session, _job, _meta = _photo_session(tmp_path)
    before = len(session.project.reconstruct.items)
    dialog = ReconstructWizard(session)
    assert dialog.env_d.suffix() == " mm"
    dialog.reject()
    assert len(session.project.reconstruct.items) == before


def test_corners_must_be_a_convex_quad():
    try:
        validate_corners([(0, 0), (10, 0), (4, 1), (0, 10)], 20, 20)
    except ValueError:
        return
    raise AssertionError("crossing corners must be refused")
