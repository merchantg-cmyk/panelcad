"""Print package / export package honour the sections and sheets the user picked."""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from panelcad.examples.dol import build_dol
from panelcad.render.print_package import plan_print_package


def _qt_app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _sheet_ids(session):
    return [sheet.id for sheet in session.project.sheets]


def test_plan_limits_drawings_to_chosen_sheets():
    s = build_dol()
    ids = _sheet_ids(s)
    assert len(ids) >= 2
    full = plan_print_package(s, sections=["drawings"])
    only = plan_print_package(s, sections=["drawings"], sheet_ids=[ids[0]])
    assert full.drawing_count == len(ids)
    assert only.drawing_count == 1
    assert [p.sheet_id for p in only.pages] == [ids[0]]
    assert plan_print_package(s, sections=["drawings"], sheet_ids=[]).pages == []


def test_plan_rejects_unknown_sheet_id():
    with pytest.raises(ValueError, match="unknown sheet"):
        plan_print_package(build_dol(), sheet_ids=["nope"])


def test_unchecked_sections_are_not_printed(tmp_path: Path, caplog):
    _qt_app()
    s = build_dol()
    with caplog.at_level(logging.INFO, logger="panelcad.print_package"):
        result = s.print_package(tmp_path / "p.pdf", sections=["cover", "bom"])
    assert set(result["order"]) <= {"cover", "bom"}
    assert "drawings" not in result["order"]
    assert result["requested_sections"] == ["cover", "bom"]
    assert any("requested sections=['cover', 'bom']" in r.getMessage() for r in caplog.records)
    assert any(f"pages={result['pages']}" in r.getMessage() for r in caplog.records)


def test_export_package_respects_sections_and_sheets(tmp_path: Path):
    _qt_app()
    s = build_dol()
    first = _sheet_ids(s)[0]
    report = s.export_package_report(
        tmp_path / "pack", pdf=True, sections=["cover", "drawings"], sheet_ids=[first]
    )
    pack = report["path"]
    assert report["warnings"] == []
    assert report["print"]["drawing_count"] == 1
    assert not (pack / "bom.csv").exists()
    assert not (pack / "wires.csv").exists()
    assert (pack / "svg" / f"{first}.svg").is_file()
    assert sorted(p.stem for p in (pack / "svg").glob("*.svg")) == [first]
    # Still re-importable: every sheet's YAML ships.
    assert all((pack / "sheets" / f"{sid}.yaml").is_file() for sid in _sheet_ids(s))
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["drawing_sheets"] == [first]


def test_export_package_default_still_writes_everything(tmp_path: Path):
    s = build_dol()
    pack = s.export_package(tmp_path / "pack", pdf=False)
    for name in ("bom.csv", "wires.csv", "checks.csv", "io.csv", "cables.csv"):
        assert (pack / name).is_file(), name


def test_print_failure_is_a_warning_not_a_silent_fallback(tmp_path: Path):
    s = build_dol()
    with (
        patch.object(type(s), "print_package", side_effect=RuntimeError("generic part K9")),
        patch.object(type(s), "export_pdf") as fallback,
    ):
        report = s.export_package_report(tmp_path / "pack", pdf=True)
    assert not fallback.called
    assert report["pdf"] is None
    assert report["warnings"] and "generic part K9" in report["warnings"][0]
    assert not (report["path"] / "print.pdf").exists()
    manifest = json.loads((report["path"] / "manifest.json").read_text(encoding="utf-8"))
    assert "generic part K9" in manifest["warnings"][0]


def test_dialog_sheet_checklist_and_initial_selection():
    _qt_app()
    from PySide6.QtCore import Qt

    from panelcad.gui.package_dialog import PrintPackageDialog

    sheets = [("power", "Power"), ("control", "Control"), ("new", "New")]
    initial = {
        "sections": ["cover", "drawings"],
        "sheet_ids": ["power"],
        "known_sheet_ids": ["power", "control"],
        "paper": "a3",
    }
    dlg = PrintPackageDialog(sheets=sheets, initial=initial)
    vals = dlg.values()
    assert vals["sections"] == ["cover", "drawings"]
    assert vals["paper"] == "a3"
    # "new" appeared after the save, so it starts ticked; "control" stays off.
    assert vals["sheet_ids"] == ["power", "new"]
    for i in range(dlg.sheet_list.count()):
        dlg.sheet_list.item(i).setCheckState(Qt.CheckState.Checked)
    assert dlg.values()["sheet_ids"] is None  # all ticked -> all sheets
    dlg.checks["drawings"].setChecked(False)
    assert not dlg.sheet_list.isEnabled()
    dlg.close()


def test_selection_is_remembered_per_project(tmp_path: Path):
    _qt_app()
    from PySide6.QtCore import QSettings

    from panelcad.gui.package_dialog import load_selection, save_selection

    store = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    save_selection("c:/jobs/a.pcad.yaml", {"sections": ["bom"], "sheet_ids": ["power"]}, store)
    save_selection("c:/jobs/b.pcad.yaml", {"sections": ["cover"], "sheet_ids": None}, store)
    again = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    assert load_selection("c:/jobs/a.pcad.yaml", again)["sections"] == ["bom"]
    assert load_selection("c:/jobs/a.pcad.yaml", again)["sheet_ids"] == ["power"]
    assert load_selection("c:/jobs/b.pcad.yaml", again)["sections"] == ["cover"]
    assert load_selection("c:/jobs/c.pcad.yaml", again) is None


def test_describe_result_lists_requested_printed_and_pages():
    from panelcad.gui.package_dialog import describe_result

    text = describe_result(
        {"pages": 4, "sections": ["cover", "drawings"], "sheets": [{}, {}]},
        ["cover", "drawings", "terminals"],
    )
    assert "Pages: 4" in text
    assert "Requested: cover, drawings, terminals" in text
    assert "Printed: cover, drawings" in text
    assert "Skipped (nothing to print): terminals" in text
    assert "Drawing sheets: 2" in text


def test_gui_export_package_uses_package_dialog(tmp_path: Path):
    _qt_app()
    from panelcad.gui import package_dialog as pkg
    from panelcad.gui.main_window import MainWindow

    opened = []

    class Probe(pkg.PrintPackageDialog):
        def exec(self):  # noqa: A003
            opened.append(self)
            return self.DialogCode.Rejected

    win = MainWindow(build_dol())
    try:
        with (
            patch("panelcad.gui.package_dialog.PrintPackageDialog", Probe),
            patch("panelcad.gui.package_dialog.load_selection", return_value=None),
        ):
            win.export_package()
        assert opened and opened[0]._folder is True
        assert opened[0].sheet_list.count() == len(win.session.project.sheets)
    finally:
        win.close()


def test_reexport_narrower_selection_removes_stale_files(tmp_path: Path):
    _qt_app()
    s = build_dol()
    ids = _sheet_ids(s)
    pack = s.export_package(tmp_path / "pack", pdf=False)
    assert (pack / "bom.csv").is_file()
    assert (pack / "svg" / f"{ids[1]}.svg").is_file()
    s.export_package(pack, pdf=False, sections=["drawings"], sheet_ids=[ids[0]])
    assert not (pack / "bom.csv").exists()
    assert not (pack / "svg" / f"{ids[1]}.svg").exists()
    assert (pack / "svg" / f"{ids[0]}.svg").is_file()
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    assert "bom.csv" not in manifest["files"]


def test_dialog_drawings_with_no_sheets_ticked_drops_drawings():
    _qt_app()
    from PySide6.QtCore import Qt

    from panelcad.gui.package_dialog import PrintPackageDialog

    dlg = PrintPackageDialog(sheets=[("a", "A"), ("b", "B")], initial={"sections": ["cover", "drawings"]})
    for i in range(dlg.sheet_list.count()):
        dlg.sheet_list.item(i).setCheckState(Qt.CheckState.Unchecked)
    vals = dlg.values()
    assert vals["sections"] == ["cover"]
    assert vals["sheet_ids"] == []
    dlg.close()


def test_reexport_never_deletes_unrelated_files(tmp_path: Path):
    """A folder with no earlier PanelCAD manifest is never pruned."""
    _qt_app()
    s = build_dol()
    ids = _sheet_ids(s)
    folder = tmp_path / "mine"
    (folder / "svg").mkdir(parents=True)
    (folder / "bom.csv").write_text("user data", encoding="utf-8")
    (folder / "svg" / f"{ids[1]}.svg").write_text("user svg", encoding="utf-8")
    s.export_package(folder, pdf=False, sections=["drawings"], sheet_ids=[ids[0]])
    assert (folder / "bom.csv").read_text(encoding="utf-8") == "user data"
    assert (folder / "svg" / f"{ids[1]}.svg").read_text(encoding="utf-8") == "user svg"


def test_prune_refuses_traversal_and_symlinks(tmp_path: Path):
    from panelcad.io.exchange import _sha256, _unlink_package_file

    root = tmp_path / "pack"
    (root / "svg").mkdir(parents=True)
    outside = tmp_path / "outside.svg"
    outside.write_text("x", encoding="utf-8")
    (root / "troubleshoot.json").write_text("{}", encoding="utf-8")
    (root / "svg" / "link.svg").symlink_to(outside)
    digest = _sha256(outside)  # hashes match, so only the path checks protect
    listed = dict.fromkeys(
        ["svg/../../outside.svg", "../outside.svg", "troubleshoot.json", "svg/link.svg"], digest
    )
    listed["troubleshoot.json"] = _sha256(root / "troubleshoot.json")
    for rel in listed:
        _unlink_package_file(root, rel, listed)
    assert outside.exists()
    assert (root / "troubleshoot.json").exists()
    assert (root / "svg" / "link.svg").is_symlink()
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "a.svg").write_text("x", encoding="utf-8")
    (root / "svg").rename(root / "svg_old")
    (root / "svg").symlink_to(ext, target_is_directory=True)
    _unlink_package_file(root, "svg/a.svg", {"svg/a.svg": _sha256(ext / "a.svg")})
    assert (ext / "a.svg").exists()


def test_print_failure_keeps_unlisted_print_pdf(tmp_path: Path):
    """A print.pdf no earlier PanelCAD manifest listed is never deleted."""
    s = build_dol()
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "print.pdf").write_bytes(b"user pdf")
    with patch.object(type(s), "print_package", side_effect=RuntimeError("boom")):
        report = s.export_package_report(folder, pdf=True)
    assert report["warnings"]
    assert (folder / "print.pdf").read_bytes() == b"user pdf"


def _fake_pdf(self, path, **kw):
    Path(path).write_bytes(b"%PDF-panelcad")
    return {"pages": 1, "path": str(path)}


def _failing_partial_pdf(self, path, **kw):
    Path(path).write_bytes(b"partial")
    raise RuntimeError("boom")


def test_hand_placed_print_pdf_survives_failed_exports(tmp_path: Path):
    """A print.pdf PanelCAD did not write is never deleted or damaged, even
    after several exports have listed it in manifest.json's ``files``."""
    s = build_dol()
    folder = tmp_path / "pack"
    s.export_package(folder, pdf=False)
    (folder / "print.pdf").write_bytes(b"user pdf")
    s.export_package(folder, pdf=False)
    s.export_package(folder, pdf=False)
    assert (folder / "print.pdf").read_bytes() == b"user pdf"
    with patch.object(type(s), "print_package", _failing_partial_pdf):
        report = s.export_package_report(folder, pdf=True)
    assert report["warnings"]
    assert (folder / "print.pdf").read_bytes() == b"user pdf"
    assert not list(folder.glob(".print-*"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert "print.pdf" in manifest["files"]
    assert "print.pdf" not in manifest["written"]


def test_print_failure_removes_print_pdf_an_earlier_export_wrote(tmp_path: Path):
    s = build_dol()
    folder = tmp_path / "pack"
    with patch.object(type(s), "print_package", _fake_pdf):
        s.export_package_report(folder, pdf=True)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert "print.pdf" in manifest["written"]
    with patch.object(type(s), "print_package", side_effect=RuntimeError("boom")):
        s.export_package_report(folder, pdf=True)
    assert not (folder / "print.pdf").exists()


def test_print_failure_keeps_an_edited_print_pdf(tmp_path: Path):
    """A PanelCAD print.pdf the user replaced since is theirs now."""
    s = build_dol()
    folder = tmp_path / "pack"
    with patch.object(type(s), "print_package", _fake_pdf):
        s.export_package_report(folder, pdf=True)
    (folder / "print.pdf").write_bytes(b"user replaced it")
    with patch.object(type(s), "print_package", side_effect=RuntimeError("boom")):
        s.export_package_report(folder, pdf=True)
    assert (folder / "print.pdf").read_bytes() == b"user replaced it"


def test_users_own_bom_and_svg_survive_two_narrower_exports(tmp_path: Path):
    s = build_dol()
    ids = _sheet_ids(s)
    folder = tmp_path / "pack"
    s.export_package(folder, pdf=False, sections=["drawings"], sheet_ids=[ids[0]])
    (folder / "bom.csv").write_text("user bom", encoding="utf-8")
    (folder / "svg" / f"{ids[1]}.svg").write_text("user svg", encoding="utf-8")
    for _ in range(2):
        s.export_package(folder, pdf=False, sections=["drawings"], sheet_ids=[ids[0]])
    assert (folder / "bom.csv").read_text(encoding="utf-8") == "user bom"
    assert (folder / "svg" / f"{ids[1]}.svg").read_text(encoding="utf-8") == "user svg"
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert "bom.csv" in manifest["files"] and "bom.csv" not in manifest["written"]


def test_pdf_build_is_atomic_over_an_existing_print_pdf(tmp_path: Path):
    s = build_dol()
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "print.pdf").write_bytes(b"old good pdf")
    with patch.object(type(s), "print_package", _failing_partial_pdf):
        s.export_package_report(folder, pdf=True)
    assert (folder / "print.pdf").read_bytes() == b"old good pdf"
    with patch.object(type(s), "print_package", _fake_pdf):
        report = s.export_package_report(folder, pdf=True)
    assert (folder / "print.pdf").read_bytes() == b"%PDF-panelcad"
    assert report["print"]["path"] == str(folder / "print.pdf")
    assert not list(folder.glob(".print-*"))


def test_old_manifest_without_written_never_deletes(tmp_path: Path):
    s = build_dol()
    ids = _sheet_ids(s)
    folder = tmp_path / "pack"
    s.export_package(folder, pdf=False)
    (folder / "print.pdf").write_bytes(b"old export pdf")
    manifest_path = folder / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data.pop("written")
    data.pop("written_sha256")
    data["files"] = sorted(set(data["files"]) | {"print.pdf"})
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    s.export_package(folder, pdf=False, sections=["drawings"], sheet_ids=[ids[0]])
    assert (folder / "bom.csv").exists()
    assert (folder / "svg" / f"{ids[1]}.svg").exists()
    assert (folder / "print.pdf").exists()


def test_print_failure_removes_partial_pdf_from_this_attempt(tmp_path: Path):
    s = build_dol()
    folder = tmp_path / "fresh"

    def partial(self, path, **kw):
        Path(path).write_bytes(b"partial")
        raise RuntimeError("boom")

    with patch.object(type(s), "print_package", partial):
        s.export_package_report(folder, pdf=True)
    assert not (folder / "print.pdf").exists()
    assert not list(folder.glob(".print-*"))


def test_locked_files_are_skipped_not_fatal(tmp_path: Path, monkeypatch, caplog):
    """A stale file (or print.pdf) open elsewhere is skipped with a warning."""
    s = build_dol()
    ids = _sheet_ids(s)
    folder = tmp_path / "pack"
    with patch.object(type(s), "print_package", _fake_pdf):
        s.export_package_report(folder, pdf=True)
    real_unlink = Path.unlink

    def locked(self, *a, **kw):
        if self.name in ("print.pdf", f"{ids[1]}.svg"):
            raise PermissionError(13, "in use", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", locked)
    caplog.set_level(logging.WARNING, logger="panelcad.export_package")
    with patch.object(type(s), "print_package", side_effect=RuntimeError("boom")):
        report = s.export_package_report(
            folder, pdf=True, sections=["drawings"], sheet_ids=[ids[0]]
        )
    assert report["warnings"]
    assert (folder / "print.pdf").exists()
    assert (folder / "svg" / f"{ids[1]}.svg").exists()
    assert "skipped: file in use" in caplog.text
    assert (folder / "manifest.json").exists()
