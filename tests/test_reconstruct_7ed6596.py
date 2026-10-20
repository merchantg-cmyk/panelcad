"""Regression tests for the 7ed6596 review (N21b, N24, N25, gate isfinite)."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QElapsedTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from panelcad import Session
from panelcad.cli.agent import Agent
from panelcad.diag.crash import crashes_dir
from panelcad.gui.main_window import MainWindow
from panelcad.io.bom import instance_rows
from panelcad.library.panel import LayoutNotReadyError, max_confirmed_envelope_mm
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


def _wait_checks(win: MainWindow, app: QApplication, tick: int, timeout_ms: int = 4000) -> None:
    timer = QElapsedTimer()
    timer.start()
    while win._checks_tick <= tick and timer.elapsed() < timeout_ms:
        app.processEvents()
    assert win._checks_tick > tick


def test_n21b_live_check_runner_keeps_present_photo(tmp_path: Path):
    """GUI Problems dock must use session.path, not cwd, to resolve photos."""

    session = _photo_session(tmp_path, "N21b")
    job = session.path
    assert job is not None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app = _app()
    old = Path.cwd()
    win = None
    try:
        os.chdir(elsewhere)
        win = MainWindow()
        win.open_path(str(job))
        app.processEvents()
        assert not [
            row
            for row in win.session.design_checks()
            if row.get("check") == "reconstruct_photo_missing"
        ]
        tick = win._checks_tick
        win._check_runner.run_now()
        _wait_checks(win, app, tick)
        missing = [
            row for row in win.problems._rows if row.get("check") == "reconstruct_photo_missing"
        ]
        assert missing == []
    finally:
        os.chdir(old)
        if win is not None:
            win.close()

    photo = session.project.reconstruct.photos[0]
    for candidate in (
        Path(photo.resolved_path) if photo.resolved_path else None,
        job.parent / photo.path,
        tmp_path / "face.png",
        Path(photo.source_path) if photo.source_path else None,
    ):
        if candidate is not None and candidate.is_file():
            candidate.unlink()
    win = MainWindow()
    try:
        os.chdir(elsewhere)
        win.open_path(str(job))
        app.processEvents()
        tick = win._checks_tick
        win._check_runner.run_now()
        _wait_checks(win, app, tick)
        missing = [
            row for row in win.problems._rows if row.get("check") == "reconstruct_photo_missing"
        ]
        assert missing
        assert missing[0].get("action") == "reattach"
        assert "re-attach" in str(missing[0].get("message") or "").lower()
    finally:
        os.chdir(old)
        win.close()


def test_n24_reconstruct_refusals_do_not_write_crash_records():
    """place / set_properties reconstruct refusals are LayoutNotReadyError, not crashes."""

    _app()
    before = {path.name for path in crashes_dir().glob("*.json")}
    agent = Agent()
    assert agent.handle({"id": 1, "op": "create_project", "name": "N24"})["ok"]
    calls = (
        {
            "id": 2,
            "op": "place",
            "library_id": "reconstruct.measured_envelope",
            "ref": "U7",
            "x": 40,
            "y": 40,
        },
        {
            "id": 3,
            "op": "place",
            "library_id": "reconstruct.needs_info",
            "ref": "UN1",
            "x": 40,
            "y": 40,
        },
        {
            "id": 4,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "K1",
            "x": 40,
            "y": 40,
            "properties": {"reconstruct_needs_info": True},
        },
    )
    for msg in calls:
        reply = agent.handle(msg)
        assert reply.get("ok") is False, msg
        assert "crash" not in reply, msg
    placed = agent.handle(
        {
            "id": 5,
            "op": "place",
            "library_id": "iec.contactors.ac3_9a",
            "ref": "K2",
            "x": 80,
            "y": 40,
        }
    )
    assert placed.get("ok") is True
    reply = agent.handle(
        {
            "id": 6,
            "op": "set_properties",
            "ref": "K2",
            "properties": {"reconstruct_needs_info": True},
        }
    )
    assert reply.get("ok") is False
    assert "crash" not in reply
    after = {path.name for path in crashes_dir().glob("*.json")}
    assert after == before


def test_n25_unconfirmed_size_edit_rewrites_measured_description(tmp_path: Path):
    session = _photo_session(tmp_path, "N25")
    item_id = _needs_info_item(session)
    session.reconstruct_confirm_item(
        item_id,
        envelope_w_mm=50,
        envelope_h_mm=80,
        envelope_d_mm=90,
        mount="backplate",
        tag="U7",
    )
    session.set_panel("U7", width_mm=60)
    found = None
    for sheet in session.project.sheets:
        try:
            found = sheet.component("U7")
            break
        except KeyError:
            continue
    assert found is not None
    assert found.properties.get("envelope_confirmed") not in {True, "true", "yes", "1"}
    desc = str(found.properties.get("description") or "")
    assert "unconfirmed" in desc
    assert "50×80×90" in desc
    assert "Measured envelope 50×80×90 mm" != desc
    row = next(r for r in instance_rows(session.project, session.catalog) if r["ref"] == "U7")
    assert "unconfirmed" in row["description"]
    assert "50×80×90" in row["description"]
    quote = build_quote(session.project, session.catalog, session.parts)
    line = next(
        (part for part in quote["parts"] if "U7" in str(part.get("refs") or "")),
        None,
    )
    assert line is not None
    assert "unconfirmed" in str(line.get("description") or "")


def test_gate_nan_width_on_catalog_symbol_is_refused():
    """#40 9754934 isfinite: NaN must not place a catalog symbol as ready."""

    _app()
    session = Session.create("NaNGate")
    session.add_sheet("panel", kind="panel_layout")
    for raw in (float("nan"), "nan"):
        try:
            session.place(
                "io.siemens.sm1221_di16",
                ref=f"A{raw}",
                sheet_id="panel",
                properties={"width_mm": raw},
            )
        except LayoutNotReadyError as exc:
            assert "finite" in str(exc).lower() or "generic" in str(exc).lower()
        else:
            raise AssertionError(f"{raw!r} width must not place on a layout")
    assert session.project.sheet("panel").components == []


def test_nit_wizard_cap_and_mount_wording(tmp_path: Path):
    session = _photo_session(tmp_path, "Nit")
    item_id = _needs_info_item(session)
    try:
        session.reconstruct_confirm_item(
            item_id,
            envelope_w_mm=50,
            envelope_h_mm=80,
            envelope_d_mm=90,
        )
    except Exception as exc:
        text = str(exc)
        assert "pick a mounting" in text
        assert "confirm=true" not in text
    else:
        raise AssertionError("confirm without mount must be refused")
    from panelcad.gui.reconstruct_wizard import ReconstructWizard

    wizard = ReconstructWizard(session)
    try:
        assert wizard.env_w.maximum() >= max_confirmed_envelope_mm()
        assert wizard.env_w.maximum() >= 2286
    finally:
        wizard.close()
