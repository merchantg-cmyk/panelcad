"""A sheet's own drawing style survives load and save, whatever the project style."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from panelcad import Session
from panelcad.cli.main import app
from panelcad.core.ladder import stamp_drawing_style
from panelcad.io.yaml_format import load_project

_PART = "iec.operators.pb_no"
_STYLES = ("ladder", "iec", "freeform", "schematic")


def _saved_styles(path: Path) -> dict[str, str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {str(s["id"]): str(s.get("drawing_style") or "") for s in raw["sheets"]}


def _build(tmp_path: Path, project_style: str, sheet_style: str) -> Path:
    s = Session.create("style-rt", sheet_id="main")
    s.project.title_block.drawing_style = project_style  # type: ignore[assignment]
    s.add_sheet("styled", title="Styled", kind="schematic")
    sheet = next(p for p in s.project.sheets if p.id == "styled")
    sheet.drawing_style = sheet_style  # type: ignore[assignment]
    s.place(_PART, ref="K1", x=40, y=40, sheet_id="styled")
    path = tmp_path / "proj.panelcad.yaml"
    s.save(path)
    assert _saved_styles(path)["styled"] == sheet_style
    return path


@pytest.mark.parametrize("sheet_style", _STYLES)
def test_session_open_save_keeps_sheet_style_blank_project(tmp_path: Path, sheet_style: str) -> None:
    path = _build(tmp_path, "", sheet_style)
    for _ in range(2):
        s = Session.open(path)
        assert next(p for p in s.project.sheets if p.id == "styled").drawing_style == sheet_style
        s.save(path)
        assert _saved_styles(path)["styled"] == sheet_style


@pytest.mark.parametrize("project_style", ("ladder", "schematic"))
@pytest.mark.parametrize("sheet_style", ("ladder", "iec", "freeform"))
def test_session_open_keeps_own_style_under_other_project_style(
    tmp_path: Path, project_style: str, sheet_style: str
) -> None:
    path = _build(tmp_path, project_style, sheet_style)
    s = Session.open(path)
    s.save(path)
    assert _saved_styles(path)["styled"] == sheet_style


@pytest.mark.parametrize("sheet_style", _STYLES)
def test_cli_open_save_keeps_sheet_style(tmp_path: Path, sheet_style: str) -> None:
    path = _build(tmp_path, "", sheet_style)
    runner = CliRunner()
    res = runner.invoke(app, ["inspect", str(path)])
    assert res.exit_code in (0, 1), res.output
    res = runner.invoke(app, ["rename", str(path), "K1", "K2"])
    assert res.exit_code == 0, res.output
    assert _saved_styles(path)["styled"] == sheet_style


def test_blank_sheet_still_takes_project_style(tmp_path: Path) -> None:
    path = _build(tmp_path, "ladder", "ladder")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    for sheet in raw["sheets"]:
        sheet.pop("drawing_style", None)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    project = load_project(path)
    stamp_drawing_style(project)
    assert all(p.drawing_style == "ladder" for p in project.sheets if p.kind == "schematic")


def test_title_block_change_still_restyles_followers() -> None:
    s = Session.create("style-tb", sheet_id="main")
    s.set_title_block(drawing_style="schematic")
    assert s.project.sheets[0].drawing_style == "schematic"
    s.set_title_block(drawing_style="freeform")
    assert s.project.sheets[0].drawing_style == "freeform"


def test_project_freeform_followers_follow_next_change() -> None:
    s = Session.create("style-ff", sheet_id="main")
    s.set_title_block(drawing_style="freeform")
    s.set_title_block(drawing_style="schematic")
    assert s.project.sheets[0].drawing_style == "schematic"
