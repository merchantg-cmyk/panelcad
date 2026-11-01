"""Accept is for a low-confidence read. A missing-info flag stays put.

The photo is the synthetic backpanel fixture. The label is the partial catalog
number MC-9. The matcher scores LS MC-9b at about 0.59, so the row is
low-confidence. A blank photo is missing info.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication
from typer.testing import CliRunner

from panelcad.cli.main import app
from panelcad.reconstruct.fixtures import write_backpanel_photo
from panelcad.reconstruct.vision import FakeVision
from tests.test_photos_to_drawings import _device, _face_scale


runner = CliRunner()


def _qt() -> QApplication:
    return QApplication.instance() or QApplication([])


def _text(result) -> str:
    return (result.output or "") + (getattr(result, "stderr", None) or "")


def _recording(meta: dict) -> dict:
    return {
        "devices": [_device(meta, tag="K1", part_number="MC-9", brand="LS Electric", confidence=0.91)],
        "strips": [],
        "rails": [],
        "ducts": [],
        "texts": [],
        "unreadable": False,
    }


def _blank(path: Path) -> None:
    _qt()
    image = QImage(64, 48, QImage.Format.Format_RGB32)
    image.fill(QColor("#6a6a6a"))
    assert image.save(str(path))


def test_cli_accepts_a_low_confidence_read_and_refuses_missing_info(
    tmp_path: Path, monkeypatch
) -> None:
    from panelcad.api.session import Session
    from panelcad.reconstruct import service as recon_service

    _qt()
    meta = write_backpanel_photo(tmp_path / "nameplate.png", seed=7)
    proj = tmp_path / "job.pcad.yaml"
    shutil.copy(Path("examples/dol_starter.pcad.yaml"), proj)
    session = Session.open(proj)
    session.reconstruct_add_photos([tmp_path / "nameplate.png"], role="backpanel")
    photo_id = session.project.reconstruct.photos[0].id
    _face_scale(session, photo_id, meta)
    session.save(proj)

    monkeypatch.setattr(
        recon_service,
        "get_backend",
        lambda *_args, **_kwargs: FakeVision({photo_id: _recording(meta)}),
    )
    listed = runner.invoke(app, ["reconstruct", "photos-to-drawings", str(proj)])
    assert listed.exit_code == 0, _text(listed)
    payload = json.loads(listed.output)
    assert payload["stopped"] is True
    low = next(row for row in payload["queue"] if row["reason"] == "low_confidence")
    assert low["kind"] == "part"
    opened = Session.open(proj)
    item = opened.project.reconstruct.item(low["source_id"])
    assert item.part_number == "MC-9"
    assert item.matches[0].catalog == "MC-9b"
    assert item.matches[0].confidence == pytest.approx(0.59, abs=0.01)

    accepted = runner.invoke(
        app,
        ["reconstruct", "photos-to-drawings", str(proj), "--accept", low["source_id"]],
    )
    assert accepted.exit_code == 0, _text(accepted)
    done = json.loads(accepted.output)
    assert done["drawings_generated"] is True
    assert done["stopped"] is False
    assert "Drawings generated" in done["summary"]
    assert all(row.get("id") != low["id"] for row in done["queue"])

    blank_proj = tmp_path / "blank.pcad.yaml"
    shutil.copy(Path("examples/dol_starter.pcad.yaml"), blank_proj)
    _blank(tmp_path / "blank.png")
    blank = Session.open(blank_proj)
    blank.reconstruct_add_photos([tmp_path / "blank.png"], role="other")
    blank.save(blank_proj)
    monkeypatch.setattr(
        recon_service,
        "get_backend",
        lambda *_args, **_kwargs: FakeVision({}),
    )
    stopped = runner.invoke(app, ["reconstruct", "photos-to-drawings", str(blank_proj)])
    assert stopped.exit_code == 0, _text(stopped)
    missing = next(row for row in json.loads(stopped.output)["queue"] if row["reason"] == "missing_info")
    refused = runner.invoke(
        app,
        ["reconstruct", "photos-to-drawings", str(blank_proj), "--accept", missing["id"]],
    )
    refused_text = _text(refused)
    assert refused.exit_code == 1
    assert "Accept applies only to a low-confidence read" in refused_text
    assert "Traceback" not in refused_text

