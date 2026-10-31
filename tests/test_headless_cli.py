"""Headless commands for the main GUI actions.

Existing commands are reused. These tests cover the folder reconstruct intake,
the photo-feedback report, print-package sheet selection, and --json on the
report commands that did not have it.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from panelcad.api.session import Session
from panelcad.cli.main import app

runner = CliRunner()


def _qt() -> None:
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        QApplication([])


def _png(path: Path, color: str = "#888888") -> None:
    from PySide6.QtGui import QColor, QImage

    _qt()
    image = QImage(64, 48, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))


def _project(tmp_path: Path) -> Path:
    path = tmp_path / "job.pcad.yaml"
    session = Session.create("Headless")
    session.place("iec.terminals.x_2p5", ref="X1-1", x=30, y=30)
    session.save(path)
    return path


def _combined(result) -> str:
    return (result.output or "") + (getattr(result, "stderr", None) or "")


def test_reconstruct_add_reads_a_photo_folder(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    _png(folder / "b-door.png", "#334455")
    _png(folder / "a-back.png", "#223344")
    (folder / "notes.pdf").write_bytes(b"%PDF-1.1")
    (folder / "nested").mkdir()
    _png(folder / "nested" / "skip.png")
    project = _project(tmp_path)

    added = runner.invoke(app, ["reconstruct", "add", str(project), str(folder), "--json"])
    assert added.exit_code == 0, _combined(added)
    payload = json.loads(added.output)
    assert payload["ok"] is True
    assert payload["count"] == 2
    names = {row["original_name"] for row in payload["added"]}
    assert names == {"a-back.png", "b-door.png"}

    empty = tmp_path / "empty"
    empty.mkdir()
    missed = runner.invoke(app, ["reconstruct", "add", str(project), str(empty), "--json"])
    assert missed.exit_code != 0
    assert "no panel photos" in _combined(missed)


def test_reconstruct_feedback_writes_pngs_and_missing_json(tmp_path: Path) -> None:
    folder = tmp_path / "photos"
    folder.mkdir()
    _png(folder / "cabinet.png")
    project = _project(tmp_path)
    added = runner.invoke(app, ["reconstruct", "add", str(project), str(folder)])
    assert added.exit_code == 0, _combined(added)

    out = tmp_path / "feedback"
    result = runner.invoke(
        app,
        ["reconstruct", "feedback", str(project), "--out", str(out), "--json"],
    )
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["photos"] == 1
    assert payload["missing"] == 0
    assert Path(payload["pngs"][0]).is_file()
    missing = json.loads(Path(payload["missing_json"]).read_text(encoding="utf-8"))
    assert missing == []

    plain = runner.invoke(app, ["reconstruct", "feedback", str(project), "--out", str(tmp_path / "again")])
    assert plain.exit_code == 0, _combined(plain)
    assert "feedback 1 png  missing 0" in plain.output

    bare = tmp_path / "bare.pcad.yaml"
    Session.create("Bare").save(bare)
    failed = runner.invoke(app, ["reconstruct", "feedback", str(bare), "--out", str(tmp_path / "none")])
    assert failed.exit_code != 0
    assert "no panel photos" in _combined(failed)

    bad_filter = runner.invoke(
        app,
        ["reconstruct", "feedback", str(project), "--out", str(tmp_path / "bad"), "--filter", "nope", "--json"],
    )
    assert bad_filter.exit_code != 0
    assert "filter must be" in _combined(bad_filter)


def test_package_sheet_selection_and_unknown_sheet(tmp_path: Path) -> None:
    from panelcad.examples.dol import build_dol

    _qt()
    src = tmp_path / "job.pcad.yaml"
    session = build_dol()
    session.save(src)
    one_pages = session.plan_print_package(sheet_ids=["power"])["pages"]
    assert one_pages < session.plan_print_package()["pages"]

    dest = tmp_path / "power.pdf"
    result = runner.invoke(
        app,
        ["package", str(src), "--pdf", str(dest), "--sheets", "power", "--json"],
    )
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["pages"] == one_pages
    assert payload["sheets"] == ["power"]
    assert dest.is_file()

    bad = runner.invoke(
        app,
        ["package", str(src), "--pdf", str(tmp_path / "nope.pdf"), "--sheets", "not-a-sheet"],
    )
    assert bad.exit_code != 0
    assert "unknown sheet" in _combined(bad)
    assert not (tmp_path / "nope.pdf").exists()


def test_bom_wires_terminals_and_check_json(tmp_path: Path) -> None:
    project = _project(tmp_path)

    bom = runner.invoke(app, ["bom", "export", str(project), "--out", str(tmp_path / "bom.csv"), "--json"])
    assert bom.exit_code == 0, _combined(bom)
    bom_payload = json.loads(bom.output)
    assert bom_payload["ok"] is True
    assert Path(bom_payload["path"]).is_file()
    assert "X1-1" in Path(bom_payload["path"]).read_text(encoding="utf-8")

    wires = runner.invoke(
        app,
        ["electrical", "wire-list", str(project), "--out", str(tmp_path / "wires.csv"), "--json"],
    )
    assert wires.exit_code == 0, _combined(wires)
    wire_payload = json.loads(wires.output)
    assert wire_payload["rows"] == 0
    assert Path(wire_payload["path"]).is_file()

    strips = runner.invoke(
        app,
        ["electrical", "terminals", str(project), "--out", str(tmp_path / "strips.csv"), "--json"],
    )
    assert strips.exit_code == 0, _combined(strips)
    strip_payload = json.loads(strips.output)
    assert strip_payload["rows"] >= 1
    assert "X1" in Path(strip_payload["path"]).read_text(encoding="utf-8")

    checked = runner.invoke(app, ["check", str(project), "--json"])
    assert "problems" in json.loads(checked.output)
    assert checked.exit_code in {0, 1}

    missing = runner.invoke(
        app,
        ["bom", "export", str(tmp_path / "missing.pcad.yaml"), "--out", str(tmp_path / "no.csv"), "--json"],
    )
    assert missing.exit_code != 0
    assert "ok" in _combined(missing) or "No such file" in _combined(missing) or "not found" in _combined(missing).lower()
