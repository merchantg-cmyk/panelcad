"""Regression tests for the 51c9dae review (N25b, N26, N25c)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.library.panel import is_layout_ready
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


def _confirm_u7(session: Session, item_id: str, width: float = 50, height: float = 80, depth: float = 90):
    return session.reconstruct_confirm_item(
        item_id,
        envelope_w_mm=width,
        envelope_h_mm=height,
        envelope_d_mm=depth,
        mount="backplate",
        tag="U7",
    )


def _assert_unconfirmed_was_50x80x90(session: Session) -> None:
    found = _components_named(session, "U7")
    assert found
    for _sheet_id, comp in found:
        assert comp.properties.get("envelope_confirmed") not in {True, "true", "yes", "1"}
        desc = str(comp.properties.get("description") or "")
        assert "unconfirmed" in desc
        assert "50×80×90" in desc
        assert desc != "Measured envelope 50×80×90 mm"
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    assert "unconfirmed" in row["description"]
    assert "50×80×90" in row["description"]
    grouped = next(r for r in grouped_rows(session.project, session.catalog) if "U7" in r.get("ref", ""))
    assert "unconfirmed" in grouped["description"]
    assert "50×80×90" in grouped["description"]
    quote = build_quote(session.project, session.catalog, session.parts)
    line = next((part for part in quote["parts"] if "U7" in str(part.get("refs") or "")), None)
    assert line is not None
    assert "unconfirmed" in str(line.get("description") or "")


def _import_u7_row(tmp_path: Path, session: Session, name: str, width: str, height: str, depth: str, mount: str) -> None:
    sheet_id = _components_named(session, "U7")[0][0]
    csv_path = tmp_path / name
    csv_path.write_text(
        "ref,library_id,sheet,width_mm,height_mm,depth_mm,mount\n"
        f"U7,reconstruct.measured_envelope,{sheet_id},{width},{height},{depth},{mount}\n",
        encoding="utf-8-sig",
    )
    session.import_bom(csv_path)


def test_n25b_bom_import_size_change_rewrites_measured_description(tmp_path: Path):
    session = _photo_session(tmp_path, "N25b")
    item_id = _needs_info_item(session)
    _confirm_u7(session, item_id)
    _import_u7_row(tmp_path, session, "resize.csv", "60", "80", "90", "backplate")
    _assert_unconfirmed_was_50x80x90(session)

    session = _photo_session(tmp_path, "N25bMount")
    _needs_info_item(session)
    item_id = next(row.id for row in session.project.reconstruct.items if row.role == "part")
    _confirm_u7(session, item_id)
    _import_u7_row(tmp_path, session, "remount.csv", "50", "80", "90", "din_rail")
    _assert_unconfirmed_was_50x80x90(session)


def test_n26_reconfirm_applies_envelope_on_every_sheet(tmp_path: Path):
    session = _photo_session(tmp_path, "N26")
    item_id = _needs_info_item(session)
    first = _confirm_u7(session, item_id)
    assert first.get("layout_ready") is True
    placed = _components_named(session, "U7")
    assert placed
    src_sheet_id, src = placed[0]
    extra = session.add_sheet("panel2", kind="panel_layout")
    extra.components.append(src.model_copy(deep=True))
    session.set_panel("U7", width_mm=60, sheet_id=src_sheet_id)
    before = _components_named(session, "U7")
    assert len(before) >= 2
    assert any(
        not is_layout_ready(session.catalog.get(comp.library_id), comp, parts=session.parts)
        for _sid, comp in before
    )
    reply = _confirm_u7(session, item_id, width=60, height=80, depth=90)
    assert reply.get("layout_ready") is True
    after = _components_named(session, "U7")
    assert len(after) >= 2
    for _sid, comp in after:
        assert is_layout_ready(session.catalog.get(comp.library_id), comp, parts=session.parts)
        assert float(comp.properties.get("width_mm")) == 60
        assert float(comp.properties.get("height_mm")) == 80
        assert float(comp.properties.get("depth_mm")) == 90
        assert str(comp.properties.get("mount")) == "backplate"
        assert comp.properties.get("envelope_confirmed") in {True, "true", "yes", "1"}
        assert str(comp.properties.get("description") or "") == "Measured envelope 60×80×90 mm"
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    assert row["width_mm"] == "60"
    assert row["height_mm"] == "80"
    assert row["depth_mm"] == "90"
    assert row["description"] == "Measured envelope 60×80×90 mm"


def test_n25c_set_panel_confirm_restores_measured_description(tmp_path: Path):
    session = _photo_session(tmp_path, "N25c")
    item_id = _needs_info_item(session)
    _confirm_u7(session, item_id)
    session.set_panel("U7", width_mm=60)
    mid = _components_named(session, "U7")
    assert mid
    assert "unconfirmed" in str(mid[0][1].properties.get("description") or "")
    session.set_panel(
        "U7",
        width_mm=60,
        height_mm=80,
        depth_mm=90,
        mount="backplate",
        confirm=True,
    )
    found = _components_named(session, "U7")
    assert found
    for _sid, comp in found:
        desc = str(comp.properties.get("description") or "")
        assert desc == "Measured envelope 60×80×90 mm"
        assert "unconfirmed" not in desc
        assert is_layout_ready(session.catalog.get(comp.library_id), comp, parts=session.parts)
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    assert row["description"] == "Measured envelope 60×80×90 mm"
    grouped = next(r for r in grouped_rows(session.project, session.catalog) if "U7" in r.get("ref", ""))
    assert grouped["description"] == "Measured envelope 60×80×90 mm"
    quote = build_quote(session.project, session.catalog, session.parts)
    line = next((part for part in quote["parts"] if "U7" in str(part.get("refs") or "")), None)
    assert line is not None
    assert str(line.get("description") or "") == "Measured envelope 60×80×90 mm"
