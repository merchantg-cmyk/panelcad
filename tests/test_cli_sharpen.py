"""CLI exit codes, --json, and errors a person can read."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from panelcad.cli.main import app

runner = CliRunner()


def _text(result) -> str:
    return (result.output or "") + (getattr(result, "stderr", None) or "")


def _json_line(result) -> dict:
    blob = _text(result)
    start = blob.find("{")
    assert start >= 0, blob
    payload, _end = json.JSONDecoder().raw_decode(blob[start:])
    return payload


def test_importing_the_cli_does_not_load_qt_or_ocr() -> None:
    import subprocess
    import sys

    script = (
        "import sys\n"
        "import panelcad.cli.main\n"
        "from panelcad.api.session import Session\n"
        "Session.open('examples/dol_starter.pcad.yaml')\n"
        "banned = [name for name in ('PySide6', 'cv2', 'pytesseract') if name in sys.modules]\n"
        "raise SystemExit('loaded ' + ', '.join(banned) if banned else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_help_names_exit_codes_and_examples() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, _text(result)
    text = _text(result)
    assert "Exit codes" in text
    assert "panelcad check" in text
    assert "Traceback" not in text

    printed = runner.invoke(app, ["print", "--help"])
    assert printed.exit_code == 0, _text(printed)
    assert "--json" in _text(printed)
    assert "panelcad print" in _text(printed)


def test_version_json() -> None:
    plain = runner.invoke(app, ["version"])
    assert plain.exit_code == 0
    assert plain.output.strip()
    payload = _json_line(runner.invoke(app, ["version", "--json"]))
    assert payload["ok"] is True
    assert payload["version"] == plain.output.strip()


def test_missing_file_is_a_clean_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pcad.yaml"
    result = runner.invoke(app, ["validate", str(missing)])
    text = _text(result)
    assert result.exit_code == 1
    assert f"Can't find {missing}." in text
    assert "Traceback" not in text

    as_json = runner.invoke(app, ["validate", str(missing), "--json"])
    payload = _json_line(as_json)
    assert as_json.exit_code == 1
    assert payload == {"ok": False, "error": f"Can't find {missing}."}


def test_damaged_project_is_a_clean_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.pcad.yaml"
    path.write_text("this is not a project\n", encoding="utf-8")
    result = runner.invoke(app, ["sheets", str(path)])
    text = _text(result)
    assert result.exit_code == 1
    assert "Traceback" not in text
    assert "isn't a PanelCAD project" in text or "damaged" in text


def test_bad_arguments_exit_2() -> None:
    result = runner.invoke(app, ["bom", "export"])
    assert result.exit_code == 2
    assert "Traceback" not in _text(result)


def test_validate_and_sheets_json(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    path = tmp_path / "job.pcad.yaml"
    Session.create("Job").save(path)

    checked = runner.invoke(app, ["validate", str(path), "--json"])
    assert checked.exit_code == 0, _text(checked)
    payload = json.loads(checked.output)
    assert payload["ok"] is True
    assert payload["errors"] == []

    listed = runner.invoke(app, ["sheets", str(path), "--json"])
    assert listed.exit_code == 0, _text(listed)
    sheets = json.loads(listed.output)
    assert sheets["ok"] is True
    assert sheets["sheets"][0]["id"]
    assert "components" in sheets["sheets"][0]


def test_print_json_unknown_sheet_and_missing_format(tmp_path: Path) -> None:
    from panelcad.examples.dol import build_dol

    path = tmp_path / "dol.pcad.yaml"
    build_dol().save(path)
    dest = tmp_path / "power.svg"
    result = runner.invoke(
        app,
        ["print", str(path), "--svg", str(dest), "--sheet", "power", "--json"],
    )
    assert result.exit_code == 0, _text(result)
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["sheet"] == "power"
    assert payload["svg"] == str(dest)
    assert dest.is_file()

    missing_format = runner.invoke(app, ["print", str(path)])
    assert missing_format.exit_code == 1
    assert "Pass --svg" in _text(missing_format)
    assert "Traceback" not in _text(missing_format)

    unknown = runner.invoke(
        app,
        ["print", str(path), "--pdf", str(tmp_path / "nope.pdf"), "--sheet", "not-a-sheet"],
    )
    assert unknown.exit_code == 1
    assert "unknown sheet 'not-a-sheet'" in _text(unknown)
    assert "Traceback" not in _text(unknown)
    assert not (tmp_path / "nope.pdf").exists()


def test_checks_and_strip_list_json() -> None:
    root = Path(__file__).resolve().parents[1]
    demo = root / "examples" / "dol_starter.pcad.yaml"
    strip = root / "examples" / "vendor_strip" / "strip_sample.pcad.yaml"
    checked = runner.invoke(app, ["electrical", "checks", str(demo), "--json"])
    payload = json.loads(checked.output)
    assert "ok" in payload and "problems" in payload
    assert checked.exit_code in {0, 1}
    assert "Traceback" not in _text(checked)

    listed = runner.invoke(app, ["electrical", "strip-list", str(strip), "--json"])
    assert listed.exit_code == 0, _text(listed)
    body = json.loads(listed.output)
    assert body["ok"] is True
    assert isinstance(body["strips"], list)


def test_capture_server_missing_project_does_not_start(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pcad.yaml"
    result = runner.invoke(
        app,
        ["capture-server", "--project", str(missing), "--workspace", str(tmp_path / "caps")],
    )
    assert result.exit_code == 1
    assert "Can't find" in _text(result)
    assert "listening" not in _text(result).lower()
    assert "Traceback" not in _text(result)
