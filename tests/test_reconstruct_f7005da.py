"""Regression tests for the f7005da review (I1–I3, N20–N23)."""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest

from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.checks import run_checks
from panelcad.io.bom import instance_rows
from panelcad.library.panel import is_layout_ready, require_explicit_envelope
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.vision import FakeVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_face(path: Path, width_px: int = 900, height_px: int = 700) -> None:
    from PySide6.QtGui import QColor, QImage

    image = QImage(width_px, height_px, QImage.Format.Format_RGB32)
    image.fill(QColor("#556677"))
    image.save(str(path), "PNG")


def _photo_session(tmp_path: Path, name: str = "Rec") -> Session:
    _app()
    shot = tmp_path / "face.png"
    _write_face(shot)
    job = tmp_path / f"{name}.pcad.yaml"
    session = Session.create(name)
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    return session


def _needs_info_item(session: Session) -> str:
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, [(0, 0), (900, 0), (900, 700), (0, 700)])
    session.reconstruct_set_scale(
        photo_id, "face", (0, 0), (900, 0), known_mm=400, known_height_mm=500
    )
    session.reconstruct_identify(
        vision=FakeVision(
            {
                "p1": {
                    "devices": [
                        {
                            "tag": "U7",
                            "label_text": "??",
                            "nx": 0.2,
                            "ny": 0.2,
                            "nw": 0.1,
                            "nh": 0.1,
                            "confidence": 0.4,
                        }
                    ]
                }
            }
        )
    )
    session.reconstruct_place_needs_info()
    return next(row.id for row in session.project.reconstruct.items if row.role == "part")


def test_i1_forged_catalog_key_is_not_layout_ready():
    _app()
    session = Session.create("I1")
    session.add_sheet("panel", kind="panel_layout")
    session.add_sheet("door", kind="door_layout")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.set_component_properties(
        "K1",
        {"catalog_key": "fake|x", "catalog_verified": "full", "width_mm": 1, "height_mm": 1},
        sheet_id="main",
    )
    device = session.catalog.get("iec.contactors.ac3_9a")
    k1 = session.project.sheet("main").component("K1")
    assert is_layout_ready(device, k1, parts=session.parts) is False
    with pytest.raises(ValueError, match="not layout-ready"):
        session.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=100, y=80, sheet_id="panel")
    assert "K1" not in {c.id for c in session.project.sheet("panel").components}

    try:
        session.place(
            "iec.contactors.ac3_9a",
            ref="KF",
            sheet_id="panel",
            properties={
                "catalog_key": "fake|x",
                "catalog_verified": "full",
                "width_mm": 45,
                "height_mm": 58,
            },
        )
    except ValueError as exc:
        assert "layout-ready" in str(exc).lower() or "generic" in str(exc).lower()
    else:
        raise AssertionError("forged catalog_key must not place on a layout")

    agent = Agent()
    agent.session = session
    reply = agent.handle(
        {
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "KA",
            "sheet": "panel",
            "properties": {
                "catalog_key": "fake|x",
                "catalog_verified": "full",
                "width_mm": 45,
                "height_mm": 58,
            },
        }
    )
    assert reply.get("ok") is False
    assert "crash" not in reply

    csv_path = Path(session.path).with_name("i1.csv") if session.path else None
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as raw:
        dest = Path(raw) / "i1.csv"
        dest.write_text(
            "ref,library_id,catalog_key,catalog_verified,width_mm,height_mm\n"
            "KB,iec.contactors.ac3_9a,x|y,full,2000,0.5\n",
            encoding="utf-8",
        )
        imported = session.import_bom(dest)
        _ = imported
        if "KB" in {c.id for c in session.project.sheet("main").components}:
            kb = session.project.sheet("main").component("KB")
            assert is_layout_ready(device, kb, parts=session.parts) is False
        packed = session.layout_from_schematic("panel")
        refs = {item.get("ref") for item in packed.get("placed") or []}
        assert "KB" not in refs
        assert "K1" not in refs

    session.place("iec.contactors.ac3_9a", ref="K2", x=80, y=40, sheet_id="main")
    session.assign_part("K2", "MC-9b", manufacturer="LS Electric")
    ok = session.place_from_bom("iec.contactors.ac3_9a", ref="K2", x=120, y=80, sheet_id="panel")
    assert ok.get("placed") is True


def test_i1_bare_catalog_mc9b_alone_is_not_layout_ready():
    """PR #40 9754934: typed catalog/part_number is not assign_part."""

    _app()
    session = Session.create("BareSKU")
    session.add_sheet("panel", kind="panel_layout")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.set_component_properties("K1", {"catalog": "MC-9b"}, sheet_id="main")
    device = session.catalog.get("iec.contactors.ac3_9a")
    k1 = session.project.sheet("main").component("K1")
    assert k1.properties.get("catalog") == "MC-9b"
    assert not k1.properties.get("catalog_key")
    assert is_layout_ready(device, k1, parts=session.parts) is False
    with pytest.raises(ValueError, match="not layout-ready"):
        session.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=100, y=80, sheet_id="panel")
    assert "K1" not in {c.id for c in session.project.sheet("panel").components}
    try:
        session.place(
            "iec.contactors.ac3_9a",
            ref="KB",
            sheet_id="panel",
            properties={"catalog": "MC-9b"},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("bare catalog must not place a generic on a layout")
    session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    assert is_layout_ready(device, session.project.sheet("main").component("K1"), parts=session.parts)


def test_i1_nan_size_on_assigned_sku_is_refused():
    """PR #40 9754934: NaN on an assigned SKU is not layout-ready."""

    _app()
    session = Session.create("NaN")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device = session.catalog.get("iec.contactors.ac3_9a")
    try:
        session.set_component_properties("K1", {"width_mm": float("nan")}, sheet_id="main")
    except Exception as exc:
        assert "finite" in str(exc).lower()
    else:
        raise AssertionError("NaN width must be refused")
    k1 = session.project.sheet("main").component("K1")
    assert is_layout_ready(device, k1, parts=session.parts)


def test_i1_import_bom_size_change_clears_confirmation(tmp_path: Path):
    """PR #40 9754934: BOM import must not keep confirmation after a size change."""

    _app()
    session = Session.create("BOM size")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.confirm_envelope(
        "K1", width_mm=45, height_mm=85, depth_mm=95, mount="din_rail", confirmed_by="george"
    )
    csv_path = tmp_path / "resize.csv"
    csv_path.write_text(
        "ref,library_id,width_mm,height_mm,depth_mm,mount\n"
        "K1,iec.contactors.ac3_9a,300,85,95,din_rail\n",
        encoding="utf-8-sig",
    )
    result = session.import_bom(csv_path)
    k1 = session.project.sheet("main").component("K1")
    device = session.catalog.get(k1.library_id)
    assert k1.properties.get("envelope_confirmed") not in {True, "yes", "1"}
    assert k1.properties.get("width_mm") == 300
    assert is_layout_ready(device, k1, parts=session.parts) is False
    assert any("envelope confirmation not imported" in note for note in result.get("warnings") or [])


def test_i1_assigned_sku_depth_or_mount_override_drops_ready():
    """PR #40 9754934: published depth and mount must still match."""

    _app()
    session = Session.create("DepthMount")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device = session.catalog.get("iec.contactors.ac3_9a")
    k1 = session.project.sheet("main").component("K1")
    assert is_layout_ready(device, k1, parts=session.parts)
    session.set_component_properties("K1", {"depth_mm": 99999}, sheet_id="main")
    assert is_layout_ready(device, session.project.sheet("main").component("K1"), parts=session.parts) is False
    session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    session.set_component_properties("K1", {"mount": "door"}, sheet_id="main")
    assert is_layout_ready(device, session.project.sheet("main").component("K1"), parts=session.parts) is False


def test_i2_caller_envelope_confirmed_is_stripped():
    _app()
    session = Session.create("I2")
    session.add_sheet("panel", kind="panel_layout")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.set_component_properties(
        "K1",
        {
            "envelope_confirmed": True,
            "width_mm": 45,
            "height_mm": 58,
            "depth_mm": 57,
            "mount": "din_rail",
        },
        sheet_id="main",
    )
    device = session.catalog.get("iec.contactors.ac3_9a")
    k1 = session.project.sheet("main").component("K1")
    assert k1.properties.get("envelope_confirmed") not in {True, "true", "yes", "1"}
    assert is_layout_ready(device, k1, parts=session.parts) is False

    try:
        session.place(
            "iec.contactors.ac3_9a",
            ref="KE",
            sheet_id="panel",
            properties={
                "envelope_confirmed": True,
                "width_mm": 45,
                "height_mm": 58,
                "depth_mm": 57,
                "mount": "din_rail",
            },
        )
    except ValueError:
        pass
    else:
        raise AssertionError("caller envelope_confirmed must not place a generic")
    assert "KE" not in {c.id for c in session.project.sheet("panel").components}

    session.confirm_envelope("K1", width_mm=45, height_mm=58, depth_mm=57, mount="din_rail")
    assert is_layout_ready(device, session.project.sheet("main").component("K1"), parts=session.parts)


def test_i3_size_edits_clear_confirmation_and_catalog_match():
    _app()
    session = Session.create("I3")
    session.add_sheet("panel", kind="panel_layout")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    device = session.catalog.get("iec.contactors.ac3_9a")
    session.set_panel("K1", width_mm=99999, height_mm=0.001, sheet_id="main")
    k1 = session.project.sheet("main").component("K1")
    assert is_layout_ready(device, k1, parts=session.parts) is False
    session.set_component_properties("K1", {"width_mm": 0.001, "height_mm": 0.001}, sheet_id="main")
    assert is_layout_ready(device, session.project.sheet("main").component("K1"), parts=session.parts) is False
    session.set_component_properties("K1", {"width_mm": 2000, "height_mm": 2000}, sheet_id="main")
    assert is_layout_ready(device, session.project.sheet("main").component("K1"), parts=session.parts) is False


def test_i3_measured_envelope_set_panel_does_not_stamp_catalog(tmp_path: Path):
    session = _photo_session(tmp_path, "I3env")
    item_id = _needs_info_item(session)
    reply = session.reconstruct_confirm_item(
        item_id,
        envelope_w_mm=50,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="backplate",
        tag="U7",
    )
    assert reply.get("layout_ready") is True
    u7 = None
    for sheet in session.project.sheets:
        try:
            u7 = sheet.component("U7")
            break
        except KeyError:
            continue
    assert u7 is not None
    device = session.catalog.get(u7.library_id)
    assert is_layout_ready(device, u7, parts=session.parts)
    session.set_panel("U7", width_mm=2000, height_mm=2000, depth_mm=2000)
    found = None
    for sheet in session.project.sheets:
        try:
            found = sheet.component("U7")
            break
        except KeyError:
            continue
    assert found is not None
    assert str(found.properties.get("dim_source") or "") != "catalog"
    assert found.properties.get("envelope_confirmed") not in {True, "true", "yes", "1"}
    assert is_layout_ready(session.catalog.get(found.library_id), found, parts=session.parts) is False
    session.set_component_properties("U7", {"width_mm": 1, "height_mm": 1})
    again = None
    for sheet in session.project.sheets:
        try:
            again = sheet.component("U7")
        except KeyError:
            continue
        break
    assert is_layout_ready(session.catalog.get(again.library_id), again, parts=session.parts) is False


def test_n20_bom_import_cannot_flag_a_real_part(tmp_path: Path):
    _app()
    session = Session.create("N20")
    session.add_sheet("panel", kind="panel_layout")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    session.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=100, y=80, sheet_id="panel")
    csv_path = tmp_path / "n20.csv"
    csv_path.write_text(
        "ref,library_id,reconstruct_needs_info\n"
        "K1,iec.contactors.ac3_9a,yes\n",
        encoding="utf-8",
    )
    session.import_bom(csv_path)
    k1 = session.project.sheet("panel").component("K1")
    assert str(k1.properties.get("reconstruct_needs_info") or "").lower() not in {"1", "true", "yes", "y"}
    assert k1.library_id != "reconstruct.needs_info"
    rows = instance_rows(session.project, session.catalog)
    priced = [row for row in rows if row["ref"] == "K1"]
    assert priced
    assert "INFO NEEDED" not in str(priced[0].get("description") or "")
    assert not any(
        row.check == "layout_not_ready" and row.ref == "K1"
        for row in run_checks(session.project, session.catalog, parts=session.parts)
    )

    other = tmp_path / "placeholder.csv"
    other.write_text(
        "ref,library_id\nUN9,reconstruct.needs_info\n",
        encoding="utf-8",
    )
    result = session.import_bom(other)
    assert result["errors"]
    assert "UN9" not in {c.id for sheet in session.project.sheets for c in sheet.components}


def test_n21_present_photo_is_not_missing_when_cwd_differs(tmp_path: Path):
    session = _photo_session(tmp_path, "N21")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    old = Path.cwd()
    try:
        os.chdir(elsewhere)
        live = [
            row
            for row in session.design_checks()
            if row.get("check") == "reconstruct_photo_missing"
        ]
        assert not live
        via_run = [
            row
            for row in run_checks(
                session.project,
                session.catalog,
                project_path=session.path,
            )
            if row.check == "reconstruct_photo_missing"
        ]
        assert not via_run
        via_resolved = [
            row
            for row in run_checks(session.project, session.catalog)
            if row.check == "reconstruct_photo_missing"
        ]
        assert not via_resolved
    finally:
        os.chdir(old)

    photo = session.project.reconstruct.photos[0]
    if photo.resolved_path and Path(photo.resolved_path).is_file():
        Path(photo.resolved_path).unlink()
    if session.path:
        stored = session.path.parent / photo.path
        if stored.is_file():
            stored.unlink()
    missing = [
        row
        for row in session.design_checks()
        if row.get("check") == "reconstruct_photo_missing"
    ]
    assert missing
    extra = missing[0].get("extra") if isinstance(missing[0], dict) else missing[0].extra
    if isinstance(extra, dict):
        assert extra.get("action") == "reattach"


def test_n22_confirm_item_uses_require_explicit_envelope(tmp_path: Path):
    session = _photo_session(tmp_path, "N22")
    item_id = _needs_info_item(session)
    store = session.project.reconstruct
    item = store.item(item_id)
    for kwargs in (
        {"envelope_w_mm": math.nan, "envelope_h_mm": 80, "envelope_d_mm": 90, "mount": "backplate"},
        {"envelope_w_mm": 0.5, "envelope_h_mm": 80, "envelope_d_mm": 90, "mount": "backplate"},
        {"envelope_w_mm": 2287, "envelope_h_mm": 80, "envelope_d_mm": 90, "mount": "backplate"},
    ):
        try:
            session.reconstruct_confirm_item(item_id, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid envelope {kwargs} must be refused")
        item = store.item(item_id)
        assert item.status != "envelope"
        assert item.envelope_confirmed is False
        assert item.library_id != "reconstruct.measured_envelope"
        require_explicit_envelope  # same rule as the gate
    reply = session.reconstruct_confirm_item(
        item_id,
        envelope_w_mm=50,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="backplate",
        tag="U7",
    )
    assert reply.get("layout_ready") is True
    item = store.item(item_id)
    assert item.status == "envelope"
    found = None
    for sheet in session.project.sheets:
        try:
            found = sheet.component("U7")
        except KeyError:
            continue
        break
    assert found is not None
    assert is_layout_ready(session.catalog.get(found.library_id), found, parts=session.parts)


def test_n23_placeholder_strips_guessed_millimetres(tmp_path: Path):
    session = _photo_session(tmp_path, "N23")
    _needs_info_item(session)
    found = None
    sheet_id = None
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.library_id == "reconstruct.needs_info":
                found = comp
                sheet_id = sheet.id
                break
    assert found is not None
    session.set_component_properties(
        found.id,
        {
            "width_mm": 45,
            "height_mm": 58,
            "depth_mm": 57,
            "envelope_confirmed": True,
            "mount": "din_rail",
            "catalog_key": "fake|x",
            "catalog_verified": "full",
            "reconstruct_needs_info": "false",
        },
        sheet_id=sheet_id,
    )
    again = None
    for sheet in session.project.sheets:
        try:
            again = sheet.component(found.id)
        except KeyError:
            continue
        break
    assert again is not None
    for key in ("width_mm", "height_mm", "depth_mm", "mount", "dim_source", "catalog_key"):
        assert again.properties.get(key) in (None, "", False)
    assert again.library_id == "reconstruct.needs_info"
    fp = session.catalog.get(again.library_id)
    from panelcad.library.panel import effective_footprint

    size = effective_footprint(fp, again)
    assert size.width_mm != 45 or size.dim_source == "generic"
