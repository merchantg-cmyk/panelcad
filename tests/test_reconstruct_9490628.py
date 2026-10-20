"""Regression tests for the 9490628 review (N27, N28, multi-sheet save/reopen)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.core.checks import run_checks
from panelcad.io.bom import instance_rows
from panelcad.library.panel import assigned_catalog_ready, envelope_is_confirmed, is_layout_ready
from panelcad.quote.build import build_quote
from panelcad.reconstruct.vision import FakeVision


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _write_face(path: Path, width_px: int = 900, height_px: int = 700) -> None:
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
    session.save(job)
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


def _components_named(session: Session, ref: str) -> list:
    found = []
    for sheet in session.project.sheets:
        try:
            found.append((sheet.id, sheet.component(ref)))
        except KeyError:
            continue
    return found


def _confirm_envelope(
    session: Session,
    item_id: str,
    width: float = 50,
    height: float = 80,
    depth: float = 90,
    mount: str = "backplate",
):
    return session.reconstruct_confirm_item(
        item_id,
        envelope_w_mm=width,
        envelope_h_mm=height,
        envelope_d_mm=depth,
        mount=mount,
        tag="U7",
    )


def _confirm_catalog(session: Session, item_id: str):
    return session.reconstruct_confirm_item(
        item_id, catalog="MC-9b", manufacturer="LS Electric", tag="U7"
    )


def _assert_catalog_device(session: Session, comp) -> None:
    device = session.catalog.get(comp.library_id)
    assert comp.library_id != "reconstruct.measured_envelope"
    assert str(comp.properties.get("catalog") or "") == "MC-9b"
    assert envelope_is_confirmed(comp) is False
    assert not str(comp.properties.get("envelope_confirmed_by") or "").strip()
    assert not str(comp.properties.get("envelope_confirmed_at") or "").strip()
    assert str(comp.properties.get("mount") or "") != "backplate"
    assert assigned_catalog_ready(comp, parts=session.parts, device=device)
    assert is_layout_ready(device, comp, parts=session.parts)
    assert "Measured envelope" not in str(comp.properties.get("description") or "")


def _assert_envelope_not_orderable(session: Session, comp) -> None:
    assert comp.library_id == "reconstruct.measured_envelope"
    for key in ("catalog", "part_number", "catalog_key", "manufacturer"):
        assert not str(comp.properties.get(key) or "").strip(), key
    assert str(comp.properties.get("description") or "") == "Measured envelope 55×80×90 mm"
    assert is_layout_ready(session.catalog.get(comp.library_id), comp, parts=session.parts)
    assert str(comp.properties.get("mount") or "") == "din_rail"
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    assert not str(row.get("catalog") or "").strip()
    assert "MC-9b" not in row.get("part_number", "")
    assert "MC-9b" not in row.get("description", "")
    quote = build_quote(session.project, session.catalog, session.parts)
    line = next((part for part in quote["parts"] if "U7" in str(part.get("refs") or "")), None)
    assert line is not None
    blob = " ".join(str(line.get(k) or "") for k in ("catalog", "part_number", "description", "manufacturer"))
    assert "MC-9b" not in blob
    assert not any(
        row.check == "catalog_missing_dimensions" and row.ref == "U7"
        for row in run_checks(session.project, session.catalog)
    )


def test_n27_envelope_to_catalog_assigns_sku_and_drops_confirmation(tmp_path: Path):
    session = _photo_session(tmp_path, "N27")
    item_id = _needs_info_item(session)
    _confirm_envelope(session, item_id)
    before = _components_named(session, "U7")
    assert before
    assert before[0][1].library_id == "reconstruct.measured_envelope"
    reply = _confirm_catalog(session, item_id)
    assert reply.get("layout_ready") is True
    found = _components_named(session, "U7")
    assert found
    for _sid, comp in found:
        _assert_catalog_device(session, comp)
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    assert row.get("catalog") == "MC-9b"
    assert "Measured envelope" not in row.get("description", "")


def test_n28_catalog_to_envelope_strips_order_keys(tmp_path: Path):
    session = _photo_session(tmp_path, "N28")
    item_id = _needs_info_item(session)
    _confirm_catalog(session, item_id)
    mid = _components_named(session, "U7")
    assert mid
    assert str(mid[0][1].properties.get("catalog") or "") == "MC-9b"
    reply = _confirm_envelope(session, item_id, width=55, height=80, depth=90, mount="din_rail")
    assert reply.get("layout_ready") is True
    found = _components_named(session, "U7")
    assert found
    for _sid, comp in found:
        _assert_envelope_not_orderable(session, comp)


def test_n27_n28_multi_sheet_save_reopen(tmp_path: Path):
    session = _photo_session(tmp_path, "N27N28")
    item_id = _needs_info_item(session)
    _confirm_envelope(session, item_id)
    src_sheet_id, src = _components_named(session, "U7")[0]
    extra = session.add_sheet("panel2", kind="panel_layout")
    extra.components.append(src.model_copy(deep=True))
    _confirm_catalog(session, item_id)
    assert len(_components_named(session, "U7")) >= 2
    for _sid, comp in _components_named(session, "U7"):
        _assert_catalog_device(session, comp)
    job = session.path
    assert job is not None
    session.save(job)
    session = Session.open(job)
    item_id = next(row.id for row in session.project.reconstruct.items if row.role == "part")
    for _sid, comp in _components_named(session, "U7"):
        _assert_catalog_device(session, comp)
    _confirm_envelope(session, item_id, width=55, height=80, depth=90, mount="din_rail")
    for _sid, comp in _components_named(session, "U7"):
        _assert_envelope_not_orderable(session, comp)
    session.save(job)
    session = Session.open(job)
    for _sid, comp in _components_named(session, "U7"):
        _assert_envelope_not_orderable(session, comp)


def _assert_no_catalog_ratings(session: Session, comp) -> None:
    from panelcad.reconstruct.layout import assignment_keys_to_strip

    assert comp.library_id == "reconstruct.measured_envelope"
    assert float(comp.properties.get("width_mm")) == 60
    assert float(comp.properties.get("height_mm")) == 80
    assert float(comp.properties.get("depth_mm")) == 90
    assert str(comp.properties.get("mount") or "") == "backplate"
    assert str(comp.properties.get("description") or "") == "Measured envelope 60×80×90 mm"
    for key in assignment_keys_to_strip():
        assert not str(comp.properties.get(key) or "").strip(), key
    assert is_layout_ready(session.catalog.get(comp.library_id), comp, parts=session.parts)


def test_n29_envelope_after_catalog_strips_assigned_ratings(tmp_path: Path):
    """Catalog → envelope must drop every assign_part rating, not only identity keys."""

    session = _photo_session(tmp_path, "N29")
    item_id = _needs_info_item(session)
    _confirm_envelope(session, item_id)
    _confirm_catalog(session, item_id)
    catalog_comp = _components_named(session, "U7")[0][1]
    assert str(catalog_comp.properties.get("amps") or "").strip()
    extra = session.add_sheet("panel2", kind="panel_layout")
    extra.components.append(catalog_comp.model_copy(deep=True))
    reply = _confirm_envelope(session, item_id, width=60, height=80, depth=90, mount="backplate")
    assert reply.get("layout_ready") is True
    found = _components_named(session, "U7")
    assert len(found) >= 2
    for _sid, comp in found:
        _assert_no_catalog_ratings(session, comp)
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    for key in ("amps", "coil_voltage", "contact_config", "mounting"):
        assert not str(row.get(key) or "").strip(), key
    assert "Measured envelope" in row["description"]
    job = session.path
    assert job is not None
    session.save(job)
    session = Session.open(job)
    reopened = _components_named(session, "U7")
    assert len(reopened) >= 2
    for _sid, comp in reopened:
        _assert_no_catalog_ratings(session, comp)
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    for key in ("amps", "coil_voltage", "contact_config", "mounting"):
        assert not str(row.get(key) or "").strip(), key
