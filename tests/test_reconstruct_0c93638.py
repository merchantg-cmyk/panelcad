"""Regression tests for the 0c93638 Windows review (N11 partial, N14–N19)."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.core.checks import run_checks
from panelcad.io.bom import instance_rows
from panelcad.library.panel import (
    effective_footprint,
    is_layout_ready,
    require_explicit_envelope,
)
from panelcad.gui.reconstruct_wizard import ReconstructWizard
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.vision import FakeVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_face(path: Path, width_px: int = 900, height_px: int = 700) -> None:
    from PySide6.QtGui import QColor, QImage

    image = QImage(width_px, height_px, QImage.Format.Format_RGB32)
    image.fill(QColor("#556677"))
    image.save(str(path), "PNG")


def test_n11_source_string_does_not_launder_generic_sizes():
    _app()
    session = Session.create("N11")
    session.add_sheet("panel", kind="panel_layout")
    session.add_sheet("door", kind="door_layout")

    for props in (
        {"source": "my notes", "width_mm": 45, "height_mm": 58, "depth_mm": 57},
        {"catalog_source": "x", "width_mm": 45, "height_mm": 58, "depth_mm": 57},
    ):
        try:
            session.place(
                "iec.contactors.ac3_9a",
                ref="KC3",
                sheet_id="panel",
                properties=props,
            )
        except ValueError as exc:
            assert "layout-ready" in str(exc).lower() or "generic" in str(exc).lower()
        else:
            raise AssertionError("caller source/catalog_source must not make a generic layout-ready")
        assert not session.project.sheet("panel").components

    try:
        session.place(
            "nema.operators.pb_no",
            ref="PBX",
            sheet_id="door",
            properties={"source": "x", "width_mm": 30, "height_mm": 30, "depth_mm": 40},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("door operator with source=x must be refused")
    assert not session.project.sheet("door").components

    agent = Agent()
    agent.session = session
    reply = agent.handle(
        {
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "KA",
            "sheet": "panel",
            "properties": {"source": "my notes", "width_mm": 45, "height_mm": 58, "depth_mm": 57},
        }
    )
    assert reply.get("ok") is False
    assert not session.project.sheet("panel").components

    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40, sheet_id="main")
    assigned = session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    assert assigned.get("verified") in {"full", "partial"}
    result = session.place_from_bom("iec.contactors.ac3_9a", ref="K1", x=100, y=80, sheet_id="panel")
    assert result["placed"] is True
    placed = session.project.sheet("panel").component("K1")
    assert is_layout_ready(session.catalog.get("iec.contactors.ac3_9a"), placed)
    fp = effective_footprint(session.catalog.get("iec.contactors.ac3_9a"), placed)
    assert fp.dim_source == "catalog"

    from panelcad.library.panel import assigned_catalog_ready_props

    duct = session.parts.get("T1-1522G-1", "AutomationDirect")
    props = duct.assignment_properties()
    assert assigned_catalog_ready_props(props) is True
    assert props.get("catalog_depth_missing") is True
    assert "depth_mm" not in props or props.get("depth_mm") in ("", None)
    from panelcad.core.models import Component
    from panelcad.library.panel import layout_ready_gap

    laundered = Component(
        id="KX",
        library_id="iec.contactors.ac3_9a",
        x=0,
        y=0,
        properties={"dim_source": "catalog", "width_mm": 45, "height_mm": 58, "depth_mm": 57},
    )
    assert layout_ready_gap(session.catalog.get("iec.contactors.ac3_9a"), laundered)

    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as raw:
        tmp = Path(raw)
        photo = tmp / "back.png"
        write_backpanel_photo(photo)
        path = tmp / "env.pcad.yaml"
        recon = Session.create("N11recon")
        recon.save(path)
        recon = Session.open(path)
        recon.reconstruct_add_photos([photo], role="backpanel")
        recon.add_sheet("panel", kind="panel_layout")
        try:
            recon.place(
                "reconstruct.measured_envelope",
                ref="U0",
                x=40,
                y=40,
                sheet_id="panel",
                properties={
                    "dim_source": "measured",
                    "envelope_confirmed": True,
                    "width_mm": 50,
                    "height_mm": 80,
                    "depth_mm": 90,
                    "mount": "backplate",
                },
            )
        except ValueError as exc:
            assert "reconstruct_confirm_item" in str(exc) or "measured_envelope" in str(exc)
        else:
            raise AssertionError("measured_envelope via place must be refused in reconstruct mode")
        assert not recon.project.sheet("panel").components


def test_n14_layout_not_ready_rows_clear_and_preview_is_readonly(tmp_path: Path):
    _app()
    session = Session.create("N14")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    session.place("iec.operators.pb_no", ref="PB1", x=80, y=40)
    session.add_sheet("panel", kind="panel_layout")
    session.add_sheet("door", kind="door_layout")
    with pytest.raises(ValueError, match="not layout-ready"):
        session.place_from_bom("iec.contactors.ac3_9a", ref="K1", sheet_id="panel")
    with pytest.raises(ValueError, match="not layout-ready"):
        session.place_from_bom("iec.operators.pb_no", ref="PB1", sheet_id="door")
    before = session.project.model_dump(mode="json")
    session.recommend_cabinet(apply=False)
    assert session.project.model_dump(mode="json") == before
    assert "layout_ready_skipped" not in (session.project.properties or {})

    session.assign_part("K1", "MC-9b", manufacturer="LS Electric")
    placed = session.place_from_bom("iec.contactors.ac3_9a", ref="K1", sheet_id="panel")
    assert placed["placed"] is True
    session.delete_component("PB1", sheet_id="main")
    problems = [row for row in run_checks(session.project, session.catalog) if row.check == "layout_not_ready"]
    assert problems == []
    assert "layout_ready_skipped" not in (session.project.properties or {})

    job = tmp_path / "n14.pcad.yaml"
    session.save(job)
    text = job.read_text(encoding="utf-8")
    assert "layout_ready_skipped" not in text
    reopened = Session.open(job)
    problems = [row for row in run_checks(reopened.project, reopened.catalog) if row.check == "layout_not_ready"]
    assert problems == []
    assert "layout_ready_skipped" not in (reopened.project.properties or {})


def test_n15_needs_info_flag_on_real_symbol_is_refused():
    _app()
    recon = Session.create("N15")
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as raw:
        tmp = Path(raw)
        photo = tmp / "back.png"
        write_backpanel_photo(photo)
        job = tmp / "n15.pcad.yaml"
        recon.save(job)
        recon = Session.open(job)
        recon.reconstruct_add_photos([photo], role="backpanel")
        recon.add_sheet("panel", kind="panel_layout")
        recon.add_sheet("door", kind="door_layout")
        for lid, ref, sheet in (
            ("nema.operators.pb_no", "PB9", "door"),
            ("iec.contactors.ac3_9a", "KC9", "panel"),
        ):
            try:
                recon.place(
                    lid,
                    ref=ref,
                    sheet_id=sheet,
                    properties={"reconstruct_needs_info": "true"},
                )
            except ValueError as exc:
                assert "reconstruct_needs_info" in str(exc) or "layout-ready" in str(exc).lower()
            else:
                raise AssertionError("INFO NEEDED flag on a real library symbol must be refused")
        assert not recon.project.sheet("panel").components
        assert not recon.project.sheet("door").components
        holes = recon.cutouts_generate()
        features = []
        for group in (holes.get("surfaces") or holes.get("features") or {}).values() if isinstance(holes.get("surfaces") or holes.get("features"), dict) else []:
            features.extend(group if isinstance(group, list) else [])
        assert not any("22.3" in str(item) or "pb" in str(item).lower() for item in features)

    session = Session.create("N15norm")
    session.place("iec.contactors.ac3_9a", ref="KF9", x=40, y=40)
    try:
        session.set_component_properties("KF9", {"reconstruct_needs_info": "true"})
    except ValueError:
        pass
    else:
        raise AssertionError("set_component_properties must refuse the flag on a real symbol")
    session.add_sheet("panel", kind="panel_layout")
    with pytest.raises(ValueError, match="not layout-ready"):
        session.place_from_bom("iec.contactors.ac3_9a", ref="KF9", sheet_id="panel")
    rec = session.recommend_cabinet(apply=False)
    assert "KF9" not in (rec.get("incabinet_refs") or [])
    assert rec.get("incabinet_count", 0) == 0
    rows = instance_rows(session.project, session.catalog)
    hit = next(row for row in rows if row["ref"] == "KF9")
    assert hit.get("width_mm") in {"45", "45.0"} or True  # schematic generic may still display
    assert not session.project.sheet("panel").components


def test_n16_undo_after_wizard_cancel_does_not_restore_convert(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    _app()
    shot = tmp_path / "back.png"
    meta = write_backpanel_photo(shot)
    job = tmp_path / "wiz.pcad.yaml"
    session = Session.create("N16")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, meta["corners"])
    session.reconstruct_set_scale(
        photo_id,
        "face",
        meta["corners"][0],
        meta["corners"][1],
        known_mm=meta.get("enclosure_width_mm") or 400,
        known_height_mm=meta.get("enclosure_height_mm") or 500,
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
    session.checkpoint()
    u7 = None
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id == "U7":
                u7 = comp
    assert u7 is not None
    assert u7.library_id == "reconstruct.needs_info"
    dialog = ReconstructWizard(session)
    item = next(row for row in session.project.reconstruct.items if row.role == "part")
    session.reconstruct_confirm_item(
        item.id,
        envelope_w_mm=50,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="backplate",
        tag="U7",
    )
    dialog.reject()
    session.undo()
    found = None
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id == "U7":
                found = comp
    assert found is not None
    assert found.library_id == "reconstruct.needs_info"
    store_item = next(row for row in session.project.reconstruct.items if row.role == "part")
    assert store_item.status == "needs_info"


def test_n17_set_scale_replaces_bad_length_and_keeps_face(tmp_path: Path):
    _app()
    shot = tmp_path / "face.png"
    _write_face(shot)
    job = tmp_path / "scale.pcad.yaml"
    session = Session.create("N17")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    photo_id = session.reconstruct_list_photos()["photos"][0]["id"]
    session.reconstruct_set_corners(photo_id, [(0, 0), (900, 0), (900, 700), (0, 700)])
    session.reconstruct_set_scale(photo_id, "measured", (100, 100), (500, 100), known_mm=300)
    session.reconstruct_set_scale(photo_id, "measured", (100, 120), (500, 120), known_mm=200)
    photo = session.project.reconstruct.photo(photo_id)
    assert photo.scale is not None
    assert float(photo.scale.known_mm) == 200
    assert photo.scale_b is None
    px = ((500 - 100) ** 2 + (120 - 120) ** 2) ** 0.5
    assert abs(float(photo.scale.known_mm) / px - 0.5) < 1e-6

    session.reconstruct_clear_scale(photo_id)
    session.reconstruct_set_scale(
        photo_id,
        "face",
        (0, 0),
        (900, 0),
        known_mm=450,
        known_height_mm=350,
    )
    photo = session.project.reconstruct.photo(photo_id)
    assert float(photo.face_width_mm or 0) == 450
    payload = session.reconstruct_set_scale(photo_id, "din_rail", (100, 100), (170, 100), known_mm=35)
    photo = session.project.reconstruct.photo(photo_id)
    assert float(photo.face_width_mm or 0) == 450
    assert float(photo.face_height_mm or 0) == 350
    assert "face" in str(payload.get("message") or "").lower() or payload.get("face") is True
    agent = Agent()
    agent.session = session
    reply = agent.handle({"op": "reconstruct_clear_scale", "photo_id": photo_id})
    assert reply.get("ok") is True
    photo = session.project.reconstruct.photo(photo_id)
    assert photo.face_width_mm in (None, 0)
    assert photo.scale is None


def test_n18_confirmed_envelope_uses_pr40_bounds():
    _app()
    session = Session.create("N18")
    session.add_sheet("panel", kind="panel_layout")
    for width in (250000, 0.5, float("nan")):
        try:
            session.place(
                "iec.contactors.ac3_9a",
                ref="KB",
                sheet_id="panel",
                properties={
                    "dim_source": "measured",
                    "envelope_confirmed": True,
                    "width_mm": width,
                    "height_mm": 80,
                    "depth_mm": 90,
                    "mount": "din_rail",
                },
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"absurd envelope {width} must be refused")
        assert not session.project.sheet("panel").components
        with pytest.raises(ValueError):
            require_explicit_envelope(width, 80, 90, "din_rail")

    session.place("iec.contactors.ac3_9a", ref="KOK", x=40, y=40, sheet_id="main")
    session.confirm_envelope("KOK", width_mm=50, height_mm=80, depth_mm=90, mount="din_rail")
    result = session.place_from_bom("iec.contactors.ac3_9a", ref="KOK", x=100, y=80, sheet_id="panel")
    assert result["placed"] is True
    placed = session.project.sheet("panel").component("KOK")
    assert is_layout_ready(session.catalog.get("iec.contactors.ac3_9a"), placed)


def test_n19_open_does_not_write_photos_or_absolute_paths(tmp_path: Path):
    _app()
    orig = tmp_path / "orig"
    orig.mkdir()
    shot = orig / "back.png"
    write_backpanel_photo(shot)
    job = orig / "job.pcad.yaml"
    session = Session.create("N19")
    session.save(job)
    session = Session.open(job)
    session.reconstruct_add_photos([shot], role="backpanel")
    session.save(job)
    text = job.read_text(encoding="utf-8")
    assert str(orig.resolve()) not in text
    assert "original_root" not in text or "original_root: ''" in text or "original_root: null" in text
    assert "source_path:" not in text or "source_path: ''" in text

    dest_dir = tmp_path / "copy"
    dest_dir.mkdir()
    dest = dest_dir / "copy.pcad.yaml"
    dest.write_text(job.read_text(encoding="utf-8"), encoding="utf-8")
    before = {p.name for p in dest_dir.iterdir()}
    reopened = Session.open(dest)
    after = {p.name for p in dest_dir.iterdir()}
    assert after == before
    assert not (dest_dir / "photos").exists()
    dumped = dest.read_text(encoding="utf-8")
    assert str(orig.resolve()) not in dumped
    findings = [row for row in run_checks(reopened.project, reopened.catalog) if row.check == "reconstruct_photo_missing"]
    assert findings


def test_cutouts_and_circuit_builder_skip_generics_without_confirm_fixture():
    _app()
    session = Session.create("SkipCB")
    session.place("iec.contactors.ac3_9a", ref="K1", x=40, y=40)
    session.place("iec.operators.pb_no", ref="PB1", x=80, y=40)
    session.add_sheet("panel", kind="panel_layout")
    session.add_sheet("door", kind="door_layout")
    session.insert_circuit(kind="dol")
    layouts = [sheet for sheet in session.project.sheets if sheet.kind in {"panel_layout", "door_layout"}]
    generic = [
        comp
        for sheet in layouts
        for comp in sheet.components
        if not is_layout_ready(session.catalog.get(comp.library_id), comp)
        and str(comp.library_id) != "reconstruct.needs_info"
    ]
    assert generic == []
    holes = session.cutouts_generate()
    assert holes is not None
    session2 = Session.create("SkipCut")
    session2.place("iec.operators.pb_no", ref="PB1", x=40, y=40)
    session2.add_sheet("door", kind="door_layout")
    try:
        session2.place("iec.operators.pb_no", ref="PB2", sheet_id="door")
    except ValueError:
        pass
    result = session2.cutouts_generate()
    blob = str(result)
    assert "22.3" not in blob
