"""Vendor parts lists (no tag, no library id) import as needs-info parts."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from panelcad import Session
from panelcad.core.cutout import collect_features
from panelcad.io.bom import (
    _norm_header,
    format_bom_import_report,
    grouped_rows,
    instance_rows,
    read_bom_csv,
)
from panelcad.io.exchange import sniff
from panelcad.library.panel import effective_footprint, is_layout_ready
from panelcad.quote.build import build_quote
from panelcad.quote.labor import drawing_counts
from panelcad.reconstruct.models import ObservedItem, item_is_layout_ready
from panelcad.reconstruct.placeholder import is_needs_info_component, project_has_needs_info

KEYENCE = """Part Number,Description,Qty,Manufacturer
IV3-500CA,Vision sensor head,2,Keyence
IV3-G500CA,Vision amplifier,2,Keyence
LR-ZB250CP,Laser sensor,4,Keyence
"""

K2 = """Tag,Part Number,Description,Qty,Manufacturer
VS1,IV3-500CA,Vision sensor head,1,Keyence
"""

_MM_KEYS = ("width_mm", "height_mm", "depth_mm", "mount", "dim_source")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Part #", "part_number"),
        ("part", "part_number"),
        ("Item Number", "part_number"),
        ("Model", "part_number"),
        ("Model Number", "part_number"),
        ("Catalog Number", "catalog"),
        ("Cat No.", "catalog"),
        ("Manufacturer Part Number", "part_number"),
        ("Brand", "manufacturer"),
        ("Make", "manufacturer"),
        ("Count", "qty"),
        ("Quantity Required", "qty"),
        ("Qty Req.", "qty"),
        ("Part Number:", "part_number"),
        ("item no", "item"),
        ("Tag", "ref"),
        ("Mfr.", "manufacturer"),
        ("Part Number", "part_number"),
    ],
)
def test_vendor_header_aliases(raw: str, expected: str) -> None:
    assert _norm_header(raw) == expected


def test_sniff_routes_vendor_parts_and_keeps_wires_and_io(tmp_path: Path) -> None:
    vendor = _write(tmp_path, "keyence.csv", KEYENCE)
    assert sniff(vendor) == "bom"
    aliased = _write(
        tmp_path,
        "alias.csv",
        "Part #,Brand,Qty Req,Description\nIV3-500CA,Keyence,1,Vision sensor head\n",
    )
    assert sniff(aliased) == "bom"
    wires = _write(tmp_path, "wires.csv", "from,to,description\nS1.13,K1.A1,coil\n")
    assert sniff(wires) == "wires"
    io = _write(
        tmp_path,
        "io.csv",
        "module,address,description,tag\nA1,I0_0,Start,S1\n",
    )
    assert sniff(io) == "io"
    bom = _write(tmp_path, "bom.csv", "ref,library_id\nQ1,iec.breakers.mcb_1p_16a\n")
    assert sniff(bom) == "bom"


def test_unknown_csv_names_found_and_expected_columns(tmp_path: Path) -> None:
    path = _write(tmp_path, "notes.csv", "Hello,World\nalpha,beta\n")
    with pytest.raises(ValueError, match="Unrecognized CSV") as sniffed:
        sniff(path)
    message = str(sniffed.value)
    assert "Hello" in message and "World" in message
    assert "Tag/Ref" in message and "Part Number" in message
    assert "File → Import BOM…" in message
    with pytest.raises(ValueError, match="Hello") as loaded:
        read_bom_csv(path)
    assert "World" in str(loaded.value)
    assert "Expected columns" in str(loaded.value)


def test_import_any_vendor_csv_creates_needs_info_without_sizes(tmp_path: Path) -> None:
    path = _write(tmp_path, "Sample Keyence Parts.csv", KEYENCE)
    session = Session.create("Keyence")
    result = session.import_any(path)
    assert result["kind"] == "bom"
    assert result["errors"] == []
    assert result["skipped"] == []
    assert len(result["created"]) == 8
    parts = [session.sheet().component(ref) for ref in result["created"]]
    assert Counter(comp.properties.get("part_number") for comp in parts) == Counter(
        {"IV3-500CA": 2, "IV3-G500CA": 2, "LR-ZB250CP": 4}
    )
    for comp in parts:
        assert comp.library_id == "reconstruct.needs_info"
        assert "keyence" not in comp.library_id
        assert is_needs_info_component(comp)
        for key in _MM_KEYS:
            assert key not in comp.properties
        assert comp.properties.get("bom") == "no"
        assert comp.properties.get("in_panel") == "no"
        assert comp.properties.get("manufacturer") == "Keyence"
        device = session.catalog.get(comp.library_id)
        footprint = effective_footprint(device, comp)
        assert footprint.width_mm == 0
        assert footprint.height_mm == 0
        assert footprint.depth_mm == 0
        assert is_layout_ready(device, comp) is False
    assert project_has_needs_info(session.project)
    assert all(row["library_id"] != "reconstruct.needs_info" for row in instance_rows(session.project, session.catalog))
    assert all(row["library_id"] != "reconstruct.needs_info" for row in grouped_rows(session.project, session.catalog))
    bom_text = " ".join(row["part_number"] for row in instance_rows(session.project, session.catalog))
    assert "IV3-500CA" not in bom_text
    features = [item.ref for rows in collect_features(session.project, session.catalog).values() for item in rows]
    assert not set(features) & set(result["created"])
    quote = build_quote(session.project, session.catalog, session.parts)
    assert [row for row in quote["parts"] if row.get("needs_info")] == []
    shown = " ".join(
        " ".join(
            str(row.get(key) or "")
            for key in ("part_number", "catalog", "description", "manufacturer", "refs")
        )
        for row in quote["parts"]
    )
    assert "IV3-500CA" not in shown
    assert "IV3-G500CA" not in shown
    assert "LR-ZB250CP" not in shown
    assert quote["unpriced"] == 0
    assert quote["needs_info"] == 8
    assert quote["needs_info_label"] == "8 parts need info (not counted)"
    assert "need info, not included" in quote["total_label"]
    assert quote["totals"]["parts_cost"] == 0
    counts = drawing_counts(session.project, session.catalog)
    named = {ref for refs in counts["devices"].values() for ref in refs}
    named.update(counts["door_refs"], counts["terminal_refs"])
    assert not (set(result["created"]) & named)

    dest = tmp_path / "print.pdf"
    printed = session.print_package(dest)
    assert printed.get("not_for_build") is True
    from panelcad.render.print_package import extract_pdf_text

    assert "NOT FOR BUILD" in extract_pdf_text(dest)


def test_vendor_row_with_tag_and_width_stays_unsized(tmp_path: Path) -> None:
    path = _write(tmp_path, "k2.csv", K2)
    session = Session.create("Tagged")
    session.add_sheet("panel", kind="panel_layout")
    result = session.import_any(path)
    assert result["created"] == ["VS1"]
    assert result["errors"] == []
    comp = session.project.sheet("main").component("VS1")
    assert session.project.sheet("panel").components == []
    assert comp.library_id == "reconstruct.needs_info"
    assert comp.properties["part_number"] == "IV3-500CA"
    assert "width_mm" not in comp.properties

    wide = _write(
        tmp_path,
        "wide.csv",
        "Part #,Description,Count,Make,Width,Height,Depth\n"
        "LR-ZB250CP,Laser sensor,1,Keyence,45,80,30\n",
    )
    again = session.import_bom(wide)
    assert again["errors"] == []
    created = session.project.sheet("main").component(again["created"][0])
    assert created.library_id == "reconstruct.needs_info"
    blob = " ".join(f"{key}={value}" for key, value in created.properties.items())
    assert "45" not in blob and "width_mm" not in created.properties
    footprint = effective_footprint(session.catalog.get(created.library_id), created)
    assert (footprint.width_mm, footprint.height_mm, footprint.depth_mm) == (0, 0, 0)


def test_place_missing_off_names_the_row_and_does_not_invent_a_part(tmp_path: Path) -> None:
    path = _write(tmp_path, "keyence.csv", KEYENCE)
    session = Session.create("Hold")
    result = session.import_bom(path, place_missing=False)
    assert result["created"] == []
    assert result["updated"] == []
    assert len(result["skipped"]) == 3
    assert any("row 2 (IV3-500CA)" in line and "library_id" in line for line in result["skipped"])
    assert session.sheet().components == []
    report = format_bom_import_report(result)
    assert "Skipped:" in report
    assert "IV3-500CA" in report
    assert "updated 0, created 0, skipped 3" in report


def test_existing_tag_is_updated_and_explicit_needs_info_symbol_is_refused(tmp_path: Path) -> None:
    session = Session.create("Update")
    session.place("iec.breakers.mcb_1p_16a", ref="Q1", x=20, y=20)
    path = _write(
        tmp_path,
        "update.csv",
        "Tag,Part #,Brand\nQ1,iC60N,Schneider\n",
    )
    result = session.import_bom(path)
    assert result["updated"] == ["Q1"]
    assert result["created"] == []
    part = session.sheet().component("Q1")
    assert part.library_id == "iec.breakers.mcb_1p_16a"
    assert part.properties["part_number"] == "iC60N"
    assert part.properties["manufacturer"] == "Schneider"

    placeholder = _write(tmp_path, "placeholder.csv", "ref,library_id\nUN9,reconstruct.needs_info\n")
    refused = session.import_bom(placeholder)
    assert refused["errors"]
    assert "reconstruct" in refused["errors"][0].lower()
    assert "UN9" not in {comp.id for sheet in session.project.sheets for comp in sheet.components}
    with pytest.raises(ValueError, match="reconstruct"):
        session.place("reconstruct.needs_info", ref="U9", x=10, y=10)


def test_layout_only_job_still_has_no_millimetres(tmp_path: Path) -> None:
    session = Session.create("Panel only")
    session.add_sheet("panel", kind="panel_layout")
    session.project.sheets = [sheet for sheet in session.project.sheets if sheet.kind == "panel_layout"]
    session.active_sheet_id = "panel"
    path = _write(tmp_path, "one.csv", "Model,Qty,Make\nIV3-500CA,1,Keyence\n")
    result = session.import_bom(path)
    comp = session.project.sheet("panel").component(result["created"][0])
    assert comp.library_id == "reconstruct.needs_info"
    for key in _MM_KEYS:
        assert key not in comp.properties
    footprint = effective_footprint(session.catalog.get(comp.library_id), comp)
    assert footprint.width_mm == 0
    with pytest.raises(ValueError, match="NOT FOR BUILD"):
        session.quote_export(tmp_path / "quote.csv")


def test_layout_ready_rules_are_unchanged() -> None:
    item = ObservedItem(id="item-1", photo_id="photo-1", part_number="IV3-500CA", status="unconfirmed")
    assert item_is_layout_ready(item) is False
    from panelcad.reconstruct.layout import refuse_generic

    with pytest.raises(ValueError, match="not layout-ready"):
        refuse_generic(item)


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_bom_import_dialog_shows_skip_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    _app()
    from panelcad.gui.main_window import MainWindow

    warnings: list[tuple] = []

    def warning(*args, **kwargs):
        warnings.append(args)
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Ok))
    window = MainWindow(Session.create("Dialog"))
    path = _write(tmp_path, "qty-only.csv", "Qty\n1\n")
    monkeypatch.setattr(
        "panelcad.gui.main_window.QFileDialog.getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(path), "")),
    )
    window.import_bom()
    assert warnings
    text = str(warnings[-1])
    assert "Skipped:" in text
    assert "no Tag/Ref" in text
    window.close()


def test_file_import_offers_bom_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognized CSV names the columns it found and offers help."""

    from PySide6.QtWidgets import QDialog, QMessageBox

    _app()
    from panelcad.gui.guidance import ImportFixDialog
    from panelcad.gui.main_window import MainWindow

    questions: list[tuple] = []
    criticals: list[tuple] = []
    dialogs: list[ImportFixDialog] = []

    def question(*args, **kwargs):
        questions.append(args)
        return QMessageBox.StandardButton.Yes

    def critical(*args, **kwargs):
        criticals.append(args)
        return QMessageBox.StandardButton.Ok

    def show_fix(self):
        dialogs.append(self)
        self.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(critical))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(ImportFixDialog, "exec", show_fix)
    window = MainWindow(Session.create("Offer"))
    path = _write(tmp_path, "notes.csv", "Hello,World\nalpha,beta\n")
    monkeypatch.setattr(
        "panelcad.gui.main_window.QFileDialog.getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(path), "")),
    )
    window.import_any()
    assert questions == []
    assert criticals == []
    assert dialogs
    text = dialogs[0].message.text()
    assert "Hello" in text and "World" in text
    assert dialogs[0].primary.text() == "What files can I import?"
    assert dialogs[0].advice.route == "unknown"
    window.close()


def test_file_import_of_vendor_csv_reports_needs_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QDialog, QMessageBox

    _app()
    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.needs_info_dialog import NeedsInfoImportDialog

    warnings: list[tuple] = []
    questions: list[tuple] = []
    guides: list[NeedsInfoImportDialog] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: warnings.append(args) or QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: questions.append(args) or QMessageBox.StandardButton.No),
    )

    def show_guide(self):
        guides.append(self)
        self.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(NeedsInfoImportDialog, "exec", show_guide)
    window = MainWindow(Session.create("Vendor GUI"))
    path = _write(tmp_path, "keyence.csv", KEYENCE)
    monkeypatch.setattr(
        "panelcad.gui.main_window.QFileDialog.getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(path), "")),
    )
    window.import_any()
    assert questions == []
    assert warnings == []
    assert guides
    text = guides[0].message.text()
    assert "created 8" in text
    assert "NOT FOR BUILD" in text
    assert "IV3-500CA" in text
    assert "no catalog match" in text
    assert guides[0].show_bom.text() == "Show in BOM"
    assert guides[0].open_problems.text() == "Open Problems"
    assert guides[0].match.text() == "Match to catalog part…"
    assert guides[0].lookup_all.text() == "Look up all"
    assert project_has_needs_info(window.session.project)
    window.close()
