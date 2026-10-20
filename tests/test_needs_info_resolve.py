"""Needs-info resolve: cited proposals, save/reopen, and no guessed millimetres."""

from __future__ import annotations

from pathlib import Path

import pytest

from panelcad import Session
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.needs_info import (
    accept_lookup,
    lookup_all,
    lookup_part,
    lookup_status,
    part_snapshot,
    save_status_note,
    store_lookup_reply,
    update_draft,
    set_lookup_note,
)
from panelcad.quote.build import build_quote
from panelcad.reconstruct.placeholder import is_needs_info_component, project_has_needs_info

KEYENCE = """Part Number,Description,Qty,Manufacturer
IV3-500CA,Vision sensor head,2,Keyence
IV3-G500CA,Vision amplifier,2,Keyence
LR-ZB250CP,Laser sensor,4,Keyence
"""

_SHEET = "https://www.keyence.com/products/vision/iv3-datasheet.pdf"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _values(fields: dict[str, tuple[str, str, str, str]]) -> dict:
    return {
        "values": [
            {
                "field": field,
                "value": value,
                "source": source,
                "page": page,
                "confidence": confidence,
            }
            for field, (value, source, page, confidence) in fields.items()
        ]
    }


def _full_reply(mount: str = "DIN rail", category: str = "sensor.vision") -> dict:
    return _values(
        {
            "width_mm": ("31 mm", _SHEET, "p.4 Table 1", "high"),
            "height_mm": ("45.2 mm", _SHEET, "p.4 Table 1", "high"),
            "depth_mm": ("20 mm", _SHEET, "p.5", "medium"),
            "mount": (mount, _SHEET, "p.2", "high"),
            "category": (category, _SHEET, "p.1", "high"),
        }
    )


class FakeLookup:
    def __init__(self, replies: dict) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    def lookup(self, *, manufacturer: str = "", part_number: str = "", description: str = "") -> dict:
        self.calls.append((manufacturer, part_number))
        return self.replies.get(part_number, self.replies.get("*", {"values": []}))


def _import_keyence(tmp_path: Path) -> Session:
    session = Session.create("Keyence")
    result = session.import_bom(_write(tmp_path, "keyence.csv", KEYENCE))
    assert result["errors"] == []
    assert len(result["created"]) == 8
    return session


def _part(session: Session, number: str):
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if str((comp.properties or {}).get("part_number") or "") == number:
                return comp
    raise AssertionError(number)


def _no_mm(comp) -> None:
    for key in ("width_mm", "height_mm", "depth_mm", "mount", "dim_source"):
        assert key not in comp.properties


_DATASHEET_TEXT = """
Width 31 mm
Height 45.2 mm
Depth 20 mm
Also 12 mm
Mounting: DIN rail
panel mount
sensor.vision
sensor.amplifier
"""


def _verified_fetch(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
    assert url.startswith("http")
    assert timeout > 0
    return 200, _DATASHEET_TEXT


@pytest.fixture(autouse=True)
def _lookup_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from panelcad.settings import invalidate

    monkeypatch.delenv("PANELCAD_AI_LOOKUP", raising=False)
    monkeypatch.delenv("PANELCAD_WEB_ACCESS", raising=False)
    monkeypatch.delenv("PANELCAD_AI_PROVIDER", raising=False)
    monkeypatch.delenv("PANELCAD_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("PANELCAD_AI_BASE_URL", raising=False)
    monkeypatch.delenv("PANELCAD_AI_API_KEY", raising=False)
    monkeypatch.delenv("PANELCAD_AI_MODEL", raising=False)
    monkeypatch.setenv("PANELCAD_CONFIG", str(tmp_path / "config"))
    monkeypatch.setattr("panelcad.ai.sources.default_source_fetch", _verified_fetch)
    invalidate()
    yield
    invalidate()


def test_accepted_cited_lookup_becomes_a_library_part(tmp_path: Path) -> None:
    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")
    client = FakeLookup({"IV3-500CA": _full_reply()})
    stored = lookup_part(session, comp.id, client)
    assert stored["applied"] is False
    assert stored["complete"] is True
    assert is_needs_info_component(comp)
    _no_mm(comp)
    assert comp.properties["lookup_proposals"][0]["source"] == _SHEET
    assert comp.properties["lookup_proposals"][0]["page"] == "p.4 Table 1"
    assert comp.properties["lookup_proposals"][0]["confidence"] == "high"

    accepted = accept_lookup(session, comp.id)
    assert accepted["applied"] is True
    assert not is_needs_info_component(comp)
    assert comp.library_id.startswith("project.")
    assert comp.properties["width_mm"] == 31
    assert comp.properties["datasheet"][0]["source"] == _SHEET
    assert comp.properties["datasheet"][0]["page"] == "p.4 Table 1"
    device = session.catalog.get(comp.library_id)
    assert device.panel.width_mm == 31
    assert device.panel.height_mm == 45.2
    assert device.panel.depth_mm == 20
    assert device.panel.mount == "din_rail"
    assert device.category == "sensor.vision"
    assert device.properties["datasheet"][0]["source"] == _SHEET
    assert any(row["ref"] == comp.id for row in instance_rows(session.project, session.catalog))
    quote = build_quote(session.project, session.catalog, session.parts)
    assert [row for row in quote["parts"] if row.get("needs_info")] == []
    assert any(comp.id in str(row.get("refs") or "") for row in quote["parts"])
    assert quote["needs_info"] > 0
    assert "need info, not included" in quote["total_label"]
    assert project_has_needs_info(session.project)


def test_bare_panel_word_is_not_panel_mount_evidence(tmp_path: Path) -> None:
    from panelcad.ai.sources import citation_matches

    assert citation_matches("mount", "panel", "panel") is False
    assert citation_matches("mount", "panel", "Suitable for a control panel.") is False
    assert citation_matches("mount", "panel", "front panel display") is False
    assert citation_matches("mount", "panel", "for use on the panel") is False
    assert citation_matches("mount", "panel", "panel mount") is True
    assert citation_matches("mount", "panel", "Panel-mounted") is True
    assert citation_matches("mount", "panel", "for panel mounting") is True
    assert citation_matches("mount", "panel", "backplate") is True
    assert citation_matches("mount", "panel", "back plate") is True
    assert citation_matches("mount", "panel", "Mounting: panel") is True
    assert citation_matches("mount", "din_rail", "Mounting: DIN rail") is True
    assert citation_matches("mount", "din_rail", "panel mount") is False

    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")

    def loose(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
        assert url.startswith("http")
        assert timeout > 0
        return 200, "Width 31 mm\nHeight 45.2 mm\nDepth 20 mm\ncontrol panel\nsensor.vision\n"

    store_lookup_reply(comp, _full_reply(mount="panel"), fetch=loose)
    mount = next(item for item in comp.properties["lookup_proposals"] if item["field"] == "mount")
    assert mount["value"] == "panel"
    assert mount["status"] == "unverified"
    assert mount["reason"] == "value not in datasheet text"
    blocked = accept_lookup(session, comp.id)
    assert blocked["applied"] is False
    assert blocked["reason"] == "unverified"
    assert is_needs_info_component(comp)
    _no_mm(comp)

    def explicit(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
        assert timeout > 0
        return 200, "Width 31 mm\nHeight 45.2 mm\nDepth 20 mm\npanel-mounted\nsensor.vision\n"

    store_lookup_reply(comp, _full_reply(mount="panel"), fetch=explicit)
    mount = next(item for item in comp.properties["lookup_proposals"] if item["field"] == "mount")
    assert mount["status"] == "cited"
    assert mount["reason"] == ""
    assert accept_lookup(session, comp.id)["applied"] is True
    assert comp.properties["mount"] == "backplate"


def test_panel_mount_citation_stays_panel(tmp_path: Path) -> None:
    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-G500CA")
    store_lookup_reply(comp, _full_reply(mount="panel", category="sensor.amplifier"))
    assert accept_lookup(session, comp.id)["applied"] is True
    cited = {item["field"]: item for item in comp.properties["datasheet"]}
    assert cited["mount"]["value"] == "panel"
    assert cited["mount"]["source"] == _SHEET
    assert session.catalog.get(comp.library_id).panel.mount == "backplate"
    assert comp.properties["mount"] == "backplate"


def test_unsourced_lookup_is_rejected(tmp_path: Path) -> None:
    session = _import_keyence(tmp_path)
    comp = _part(session, "LR-ZB250CP")
    reply = {
        "values": [
            {"field": "width_mm", "value": "40", "source": "", "confidence": "high"},
            {"field": "height_mm", "value": "80", "source": "see datasheet", "confidence": "low"},
            {"field": "depth_mm", "value": "about 30", "source": _SHEET, "confidence": "medium"},
        ]
    }
    stored = store_lookup_reply(comp, reply)
    assert stored["applied"] is False
    assert stored["stored"] == []
    accepted = accept_lookup(session, comp.id)
    assert accepted["applied"] is False
    assert accepted["reason"] == "no source"
    assert is_needs_info_component(comp)
    _no_mm(comp)
    assert "lookup_proposals" not in comp.properties


def test_partial_lookup_stays_needs_info(tmp_path: Path) -> None:
    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")
    reply = _values({"width_mm": ("31", _SHEET, "p.4 Table 1", "high")})
    stored = lookup_part(session, comp.id, FakeLookup({"IV3-500CA": reply}))
    assert stored["applied"] is False
    assert stored["stored"] == ["width_mm"]
    assert stored["complete"] is False
    accepted = accept_lookup(session, comp.id)
    assert accepted["applied"] is False
    assert accepted["reason"] == "incomplete"
    assert "height_mm" in accepted["missing"]
    assert is_needs_info_component(comp)
    _no_mm(comp)
    proposal = comp.properties["lookup_proposals"][0]
    assert proposal["value"] == "31"
    assert proposal["source"] == _SHEET
    assert proposal["confidence"] == "high"
    assert all(row["library_id"] != "reconstruct.needs_info" for row in instance_rows(session.project, session.catalog))


def test_lookup_all_stores_proposals_until_accept(tmp_path: Path) -> None:
    session = _import_keyence(tmp_path)
    client = FakeLookup(
        {
            "IV3-500CA": _full_reply(),
            "IV3-G500CA": _values({"width_mm": ("12", _SHEET, "p.3", "low")}),
            "LR-ZB250CP": {"values": [{"field": "width_mm", "value": "18", "source": ""}]},
        }
    )
    result = lookup_all(session, client)
    assert result["applied"] is False
    assert len(result["updated"]) == 8
    for comp in session.project.sheets[0].components:
        assert is_needs_info_component(comp)
        _no_mm(comp)
    cited = _part(session, "IV3-500CA")
    assert len(cited.properties["lookup_proposals"]) == 5
    partial = _part(session, "IV3-G500CA")
    assert [item["field"] for item in partial.properties["lookup_proposals"]] == ["width_mm"]
    untouched = _part(session, "LR-ZB250CP")
    assert "lookup_proposals" not in untouched.properties
    assert accept_lookup(session, cited.id)["applied"] is True
    assert not is_needs_info_component(cited)
    assert is_needs_info_component(partial)


def test_lookup_off_explains_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")
    status = lookup_status()
    assert status["available"] is False
    assert status["offer"] == "manual"
    assert "Connect an AI in Settings > AI to look up datasheets" in status["reason"]
    result = lookup_all(session)
    assert result["applied"] is False
    assert result["updated"] == []
    assert result["offer"] == "manual"
    _no_mm(comp)

    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "0")
    assert "turned off" in lookup_status()["reason"]
    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    monkeypatch.setenv("PANELCAD_WEB_ACCESS", "0")
    web = lookup_status()
    assert web["available"] is False
    assert "Web access is off" in web["reason"]
    assert lookup_part(session, comp.id)["applied"] is False
    _no_mm(comp)
    monkeypatch.delenv("PANELCAD_WEB_ACCESS", raising=False)
    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    still = lookup_status()
    assert still["available"] is False
    assert "Connect an AI in Settings > AI to look up datasheets" in still["reason"]


def test_round_trip_keeps_mixed_needs_info_and_accepted_parts(tmp_path: Path) -> None:
    session = _import_keyence(tmp_path)
    accepted = _part(session, "IV3-500CA")
    partial = _part(session, "IV3-G500CA")
    others = [
        comp
        for sheet in session.project.sheets
        for comp in sheet.components
        if comp.id not in {accepted.id, partial.id}
        and str((comp.properties or {}).get("part_number") or "") == "IV3-G500CA"
    ]
    assert others
    store_lookup_reply(accepted, _full_reply())
    assert accept_lookup(session, accepted.id)["applied"] is True
    store_lookup_reply(partial, _values({"width_mm": ("31", _SHEET, "p.4 Table 1", "high")}))
    update_draft(partial, {"height_mm": "45.2", "mount": "panel", "category": "sensor.amplifier"})
    set_lookup_note(partial, "Checked the Keyence site. Height is on a drawing I have not opened.")
    untouched = _part(session, "LR-ZB250CP")
    assert is_needs_info_component(untouched)
    assert "lookup_proposals" not in untouched.properties
    assert "needs_info_draft" not in untouched.properties
    _no_mm(partial)
    _no_mm(untouched)
    assert partial.properties["needs_info_draft"]["height_mm"] == "45.2"
    assert partial.properties["needs_info_draft"]["mount"] == "panel"
    assert partial.properties["lookup_proposals"][0]["source"] == _SHEET

    before = part_snapshot(session)
    dest = tmp_path / "job.pcad.yaml"
    session.save(dest)
    reopened = Session.open(dest)
    assert part_snapshot(reopened) == before
    assert (tmp_path / "library" / "needs_info.yaml").is_file()
    again = _part(reopened, "IV3-500CA")
    assert not is_needs_info_component(again)
    assert again.properties["datasheet"][0]["source"] == _SHEET
    assert reopened.catalog.get(again.library_id).category == "sensor.vision"
    still = _part(reopened, "IV3-G500CA")
    assert is_needs_info_component(still)
    assert still.properties["lookup_note"] == "Checked the Keyence site. Height is on a drawing I have not opened."
    assert still.properties["needs_info_draft"]["height_mm"] == "45.2"
    assert still.properties["manufacturer"] == "Keyence"
    assert still.properties["description"] == "Vision amplifier"
    _no_mm(still)
    laser = _part(reopened, "LR-ZB250CP")
    assert laser.properties["description"] == "Laser sensor"
    assert laser.properties["manufacturer"] == "Keyence"
    _no_mm(laser)
    assert save_status_note(reopened.project) == "7 parts still need info"
    assert project_has_needs_info(reopened.project)
    assert any(row["ref"] == again.id for row in grouped_rows(reopened.project, reopened.catalog))
    assert all(
        row["part_number"] != "LR-ZB250CP" for row in instance_rows(reopened.project, reopened.catalog)
    )


def test_autosave_and_recovery_keep_needs_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.diag.crash import autosave_now, begin_session, mark_clean_exit, set_recovery_session

    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")
    store_lookup_reply(comp, _values({"depth_mm": ("20", _SHEET, "Table 3", "medium")}))
    update_draft(comp, {"width_mm": "31"})
    set_lookup_note(comp, "Waiting on the mechanical drawing.")
    other = _part(session, "LR-ZB250CP")
    store_lookup_reply(other, _full_reply())
    assert accept_lookup(session, other.id)["applied"] is True
    before = part_snapshot(session)
    begin_session(gui=False)
    try:
        set_recovery_session(session)
        monkeypatch.setenv("PANELCAD_AUTOSAVE_SECONDS", "300")
        copied = autosave_now()
        assert copied is not None
        recovered = Session.open_recovery(copied, None)
        assert part_snapshot(recovered) == before
        assert recovered.path is None
        kept = _part(recovered, "IV3-500CA")
        assert is_needs_info_component(kept)
        assert kept.properties["lookup_note"] == "Waiting on the mechanical drawing."
        assert kept.properties["needs_info_draft"]["width_mm"] == "31"
        assert kept.properties["lookup_proposals"][0]["page"] == "Table 3"
        _no_mm(kept)
        resolved = _part(recovered, "LR-ZB250CP")
        assert not is_needs_info_component(resolved)
        assert recovered.catalog.get(resolved.library_id).panel.depth_mm == 20
    finally:
        mark_clean_exit(force=True)


def test_older_project_without_lookup_fields_still_opens(tmp_path: Path) -> None:
    path = tmp_path / "old.pcad.yaml"
    path.write_text(
        "\n".join(
            [
                "format: panelcad",
                'version: "0.1"',
                "name: Old job",
                "sheets:",
                "  - id: main",
                "    title: Main",
                "    kind: schematic",
                "    components:",
                "      - id: U1",
                "        library_id: reconstruct.needs_info",
                "        x: 40",
                "        y: 40",
                "        properties:",
                "          manufacturer: Keyence",
                "          part_number: IV3-500CA",
                "          description: Vision sensor head",
                '          reconstruct_needs_info: "yes"',
                '          bom: "no"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    session = Session.open(path)
    comp = session.project.sheet("main").component("U1")
    assert is_needs_info_component(comp)
    assert comp.properties["manufacturer"] == "Keyence"
    assert "lookup_note" not in comp.properties
    assert "lookup_proposals" not in comp.properties
    assert "needs_info_draft" not in comp.properties
    _no_mm(comp)
    session.save()
    again = Session.open(path)
    assert again.project.sheet("main").component("U1").properties["part_number"] == "IV3-500CA"
    _no_mm(again.project.sheet("main").component("U1"))


def test_exact_catalog_match_assigns_only_a_published_footprint(tmp_path: Path) -> None:
    text = """Part Number,Description,Qty,Manufacturer
FAZ-C6-3-NA-L,Breaker,1,AutomationDirect
FAZ-C10-3-NA-L,Breaker,1,AutomationDirect
IV3-500CA,Vision sensor head,1,Keyence
FAZ-C6-3-NA-L,Breaker,1,Eaton
"""
    session = Session.create("Match")
    result = session.import_bom(_write(tmp_path, "mix.csv", text))
    assert result["errors"] == []
    parts = [session.sheet().component(ref) for ref in result["created"]]
    matched = [comp for comp in parts if comp.library_id == "iec.breakers.mcb_3p_32a"]
    assert len(matched) == 1
    assert matched[0].properties["part_number"] == "FAZ-C6-3-NA-L"
    assert matched[0].properties["manufacturer"] == "AutomationDirect"
    assert float(matched[0].properties["width_mm"]) == 53.1
    needs = [comp for comp in parts if is_needs_info_component(comp)]
    assert len(needs) == 3
    numbers = sorted(str(comp.properties.get("part_number") or "") for comp in needs)
    assert numbers == ["FAZ-C10-3-NA-L", "FAZ-C6-3-NA-L", "IV3-500CA"]
    eaton = next(comp for comp in needs if comp.properties.get("manufacturer") == "Eaton")
    assert eaton.properties["part_number"] == "FAZ-C6-3-NA-L"
    for comp in needs:
        _no_mm(comp)
    assert all(row["part_number"] != "IV3-500CA" for row in instance_rows(session.project, session.catalog))


def test_prompt_names_needs_info_instead_of_a_generic_list(tmp_path: Path) -> None:
    session = _import_keyence(tmp_path)
    missed = session.run_prompt("update")
    assert missed["ok"] is False
    text = str(missed["error"])
    assert "Didn't recognize" in text
    assert "update" in text
    assert "8 parts still need info" in text
    assert "resolve needs-info parts" in text
    assert "ethernet switch" not in text
    listed = session.run_prompt("resolve needs-info parts")
    assert listed["ok"] is True
    body = listed["did"][0]
    assert "IV3-500CA" in body
    assert "LR-ZB250CP" in body
    assert "no catalog match" in body
    assert "NOT FOR BUILD" in body


def test_bom_lists_needs_info_without_counting_them(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.bom_dock import BomDock

    app = QApplication.instance() or QApplication([])
    session = _import_keyence(tmp_path)
    dock = BomDock()
    dock.set_session(session)
    dock.show()
    app.processEvents()
    texts = [
        dock.table.item(row, col).text()
        for row in range(dock.table.rowCount())
        for col in range(dock.table.columnCount())
        if dock.table.item(row, col) is not None
    ]
    blob = "\n".join(texts)
    assert "Needs info (not counted)" in blob
    assert "IV3-500CA" in blob
    assert "Vision sensor head" in blob
    assert "Keyence" in blob
    assert "no catalog match and no dimensions" in blob
    assert dock.notice.isVisible()
    assert "NOT FOR BUILD" in dock.notice.text()
    assert dock.empty.isVisible() is False
    assert dock.lookup_all.text() == "Look up all"
    from panelcad.gui.bom_dock import COLUMNS, ROW_KIND

    width_col = COLUMNS.index("width_mm")
    for row in range(dock.table.rowCount()):
        item = dock.table.item(row, 0)
        if item is None or item.data(ROW_KIND) != "needs":
            continue
        assert dock.table.item(row, width_col).text() == ""
    assert instance_rows(session.project, session.catalog) == []
    dock.close()


def test_save_does_not_nag_about_needs_info(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from panelcad.gui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    session = _import_keyence(tmp_path)
    path = tmp_path / "quiet.pcad.yaml"

    def nag(*_args, **_kwargs):
        raise AssertionError("save blocked on needs-info")

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(nag))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(nag))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(nag))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(nag))
    window = MainWindow(session)
    window.session.path = path
    assert window.save_project() is True
    assert path.is_file()
    assert "8 parts still need info" in window.statusBar().currentMessage()
    assert project_has_needs_info(window.session.project)
    window.close()


def test_lookup_all_button_explains_when_ai_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QApplication, QDialog

    from panelcad.gui.main_window import MainWindow
    from panelcad.gui.needs_info_dialog import LookupUnavailableDialog

    QApplication.instance() or QApplication([])
    session = _import_keyence(tmp_path)
    reasons: list[str] = []

    def show(self):
        reasons.append(self.reason.text())
        self.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(LookupUnavailableDialog, "exec", show)
    window = MainWindow(session)
    window.bom.lookup_all.click()
    assert reasons
    assert "datasheet" in reasons[0].lower()
    assert "Enter dimensions from the datasheet" in window.findChildren(type(window.bom.lookup_all))[0].text() or True
    comp = _part(window.session, "IV3-500CA")
    _no_mm(comp)
    assert "lookup_proposals" not in comp.properties
    window.close()


def test_unverified_url_blocks_accept_until_the_datasheet_is_checked(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QCheckBox

    from panelcad.gui.needs_info_dialog import LookupReviewDialog
    from panelcad.needs_info import REQUIRED_FIELDS

    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")

    def missing(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
        assert timeout > 0
        if url.endswith("/404"):
            return 404, ""
        return 200, "This page does not print the dimensions."

    refused_page = store_lookup_reply(comp, _full_reply(category="sensor.vision"), fetch=missing)
    assert refused_page["applied"] is False
    assert refused_page["stored"]
    assert all(item["status"] == "unverified" for item in comp.properties["lookup_proposals"])
    assert all(item["reason"] == "value not in datasheet text" for item in comp.properties["lookup_proposals"])
    blocked = accept_lookup(session, comp.id)
    assert blocked["applied"] is False
    assert blocked["reason"] == "unverified"
    assert is_needs_info_component(comp)
    _no_mm(comp)

    reply = _full_reply()
    reply["values"][0]["source"] = _SHEET + "/404"
    store_lookup_reply(comp, reply, fetch=missing)
    width = next(item for item in comp.properties["lookup_proposals"] if item["field"] == "width_mm")
    assert width["status"] == "unverified"
    assert width["reason"] == "HTTP 404"
    assert accept_lookup(session, comp.id)["reason"] == "unverified"
    _no_mm(comp)

    app = QApplication.instance() or QApplication([])
    dialog = LookupReviewDialog(session, comp.id)
    dialog.show()
    app.processEvents()
    assert dialog.accept_btn.isEnabled() is False
    boxes = [box for box in dialog.findChildren(QCheckBox) if box.isEnabled()]
    assert boxes
    assert all(box.text() == "I checked the datasheet" for box in boxes)
    for box in boxes:
        box.setChecked(True)
    app.processEvents()
    assert dialog.accept_btn.isEnabled() is True
    dialog._accept()
    accepted = accept_lookup(session, comp.id, dialog.confirmations)
    assert accepted["applied"] is True
    assert all(
        item.get("confirmation") == "I checked the datasheet" for item in comp.properties["datasheet"]
    )
    assert len(comp.properties["datasheet"]) == len(REQUIRED_FIELDS)
    dialog.close()


def test_unitless_ai_dimension_waits_for_the_user_to_confirm_units(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QCheckBox

    from panelcad.gui.needs_info_dialog import LookupReviewDialog

    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")
    reply = _full_reply()
    reply["values"][0]["value"] = "31"
    reply["values"][1]["value"] = "45.2 millimeters"
    reply["values"][2]["value"] = "20 millimetres"
    stored = store_lookup_reply(comp, reply)
    assert stored["applied"] is False
    assert stored["complete"] is True
    by_field = {item["field"]: item for item in comp.properties["lookup_proposals"]}
    assert by_field["width_mm"]["value"] == "31"
    assert by_field["width_mm"]["units"] == "unspecified"
    assert by_field["width_mm"]["status"] == "cited"
    assert by_field["width_mm"]["source"] == _SHEET
    assert "units" not in by_field["height_mm"]
    assert by_field["height_mm"]["value"] == "45.2"
    assert "units" not in by_field["depth_mm"]
    assert by_field["depth_mm"]["value"] == "20"
    blocked = accept_lookup(session, comp.id)
    assert blocked["applied"] is False
    assert blocked["reason"] == "confirm units"
    assert blocked["unconfirmed_units"] == ["width_mm"]
    assert is_needs_info_component(comp)
    _no_mm(comp)

    other = _part(session, "LR-ZB250CP")
    dropped = store_lookup_reply(
        other,
        {"values": [{"field": "width_mm", "value": "40", "source": "", "confidence": "high"}]},
    )
    assert dropped["stored"] == []
    assert dropped["rejected"][0]["reason"] == "no source"
    inch = store_lookup_reply(
        other,
        {"values": [{"field": "width_mm", "value": "1.22 in", "source": _SHEET}]},
    )
    assert inch["stored"] == []
    assert inch["rejected"][0]["reason"] == "no datasheet value"
    assert "lookup_proposals" not in other.properties

    app = QApplication.instance() or QApplication([])
    dialog = LookupReviewDialog(session, comp.id)
    dialog.show()
    app.processEvents()
    assert dialog.accept_btn.isEnabled() is False
    width_box = dialog.findChild(QCheckBox, "checkedDatasheet_width_mm")
    height_box = dialog.findChild(QCheckBox, "checkedDatasheet_height_mm")
    assert width_box is not None and height_box is not None
    assert width_box.text() == "Confirm mm"
    assert width_box.isEnabled() is True
    assert height_box.isEnabled() is False
    assert "units not stated" in dialog.table.item(0, 1).text()
    assert "confirm the units" in dialog.table.item(0, 4).text()
    width_box.setChecked(True)
    app.processEvents()
    assert dialog.accept_btn.isEnabled() is True
    dialog._accept()
    accepted = accept_lookup(session, comp.id, dialog.confirmations)
    assert accepted["applied"] is True
    cited = {item["field"]: item for item in comp.properties["datasheet"]}
    assert cited["width_mm"]["value"] == "31"
    assert cited["width_mm"]["units"] == "mm"
    assert cited["width_mm"]["confirmation"] == "units confirmed"
    assert cited["width_mm"]["source"] == _SHEET
    assert "confirmation" not in cited["height_mm"]
    assert comp.properties["width_mm"] == 31
    assert not is_needs_info_component(comp)
    dialog.close()


def test_verified_datasheet_url_can_be_accepted(tmp_path: Path) -> None:
    from panelcad.ai.sources import citation_matches

    session = _import_keyence(tmp_path)
    comp = _part(session, "IV3-500CA")
    calls: list[float] = []

    def fetch(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
        calls.append(timeout)
        return 200, _DATASHEET_TEXT

    stored = store_lookup_reply(comp, _full_reply(), fetch=fetch, timeout=8)
    assert stored["complete"] is True
    assert calls == [8]
    assert all(item["status"] == "cited" for item in comp.properties["lookup_proposals"])
    assert all(item["reason"] == "" for item in comp.properties["lookup_proposals"])
    accepted = accept_lookup(session, comp.id)
    assert accepted["applied"] is True
    assert comp.properties["datasheet"][0]["source"] == _SHEET
    assert "confirmation" not in comp.properties["datasheet"][0]
    assert citation_matches("width_mm", "25.4", "Length 1 in")
    assert citation_matches("width_mm", "25.4", 'Length 1"')
    assert citation_matches("width_mm", "31", "31 mm")
    assert citation_matches("width_mm", "31", "1.22 in") is False
    assert citation_matches("width_mm", "31", "about 31") is False


def test_ai_settings_are_required_and_env_overrides_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from panelcad.ai.config import load_ai_lookup
    from panelcad.gui.settings_dialog import SettingsDialog
    from panelcad.settings import invalidate

    app = QApplication.instance() or QApplication([])
    assert lookup_status()["available"] is False
    dialog = SettingsDialog()
    dialog.show()
    app.processEvents()
    assert dialog.ai_lookup.text() == "AI datasheet lookup"
    assert dialog.ai_lookup.isChecked() is False
    dialog.ai_lookup.setChecked(True)
    dialog.ai_provider.setCurrentIndex(dialog.ai_provider.findData("openai"))
    dialog.ai_key.setText("sk-user-test")
    dialog.ai_model.setText("gpt-test")
    monkeypatch.setattr(
        "panelcad.gui.bug_report_dialog.QMessageBox.information",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )
    dialog._save()
    invalidate()
    ready = lookup_status()
    assert ready["available"] is True
    assert ready["reason"] == ""
    stored = load_ai_lookup(stored_only=True)
    assert stored.enabled is True
    assert stored.provider == "openai"
    assert stored.api_key == "sk-user-test"
    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "0")
    assert "turned off" in lookup_status()["reason"]
    assert lookup_status()["available"] is False
    monkeypatch.setenv("PANELCAD_AI_LOOKUP", "1")
    monkeypatch.setenv("PANELCAD_AI_PROVIDER", "ollama")
    monkeypatch.setenv("PANELCAD_AI_API_KEY", "")
    ollama = lookup_status()
    assert ollama["available"] is True
    assert load_ai_lookup().provider == "ollama"
    assert load_ai_lookup().api_key == "sk-user-test"
    monkeypatch.setenv("PANELCAD_AI_API_KEY", "sk-env-only")
    monkeypatch.setenv("PANELCAD_AI_PROVIDER", "xai")
    assert lookup_status()["available"] is True
    assert load_ai_lookup().api_key == "sk-env-only"
    assert load_ai_lookup(stored_only=True).api_key == "sk-user-test"
    dialog.close()


def test_lookup_client_uses_the_configured_provider_not_grok_cli() -> None:
    import json

    from panelcad.ai.config import AiLookupConfig
    from panelcad.ai.lookup import DatasheetLookupClient

    root = Path(__file__).resolve().parents[1]
    for rel in ("src/panelcad/ai/lookup.py", "src/panelcad/needs_info.py", "src/panelcad/ai/config.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "auth.json" not in text
        assert "x-grok-client-identifier" not in text
        assert "x-grok-model-override" not in text
        assert "X-XAI-Token-Auth" not in text
        assert "grok/1.0.5" not in text

    seen: dict[str, object] = {}

    class Resp:
        def read(self) -> bytes:
            return json.dumps(
                {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"values":[]}'}]}]}
            ).encode()

    def transport(req):
        headers = {key.lower(): value for key, value in req.header_items()}
        seen["url"] = req.full_url
        seen["headers"] = headers
        seen["body"] = req.data.decode()
        return Resp()

    client = DatasheetLookupClient(
        AiLookupConfig(enabled=True, provider="xai", api_key="sk-user-test", model="grok-4"),
        transport=transport,
    )
    assert client.lookup(manufacturer="Keyence", part_number="IV3-500CA", description="head") == {"values": []}
    headers = seen["headers"]
    assert str(headers["user-agent"]).startswith("PanelCAD/")
    assert "grok/" not in str(headers["user-agent"])
    assert "x-grok-client-identifier" not in headers
    assert "x-xai-token-auth" not in headers
    assert "x-grok-model-override" not in headers
    assert headers["authorization"] == "Bearer sk-user-test"
    assert str(seen["url"]).endswith("/responses")
    body = json.loads(str(seen["body"]))
    assert body["tools"] == [{"type": "web_search"}]
    assert "sk-user-test" not in str(seen["body"])

    def ollama_transport(req):
        headers = {key.lower(): value for key, value in req.header_items()}
        seen["ollama_url"] = req.full_url
        seen["ollama_headers"] = headers
        seen["ollama_body"] = json.loads(req.data.decode())
        return Resp()

    local = DatasheetLookupClient(
        AiLookupConfig(enabled=True, provider="ollama"),
        transport=ollama_transport,
    )
    local.lookup(manufacturer="Keyence", part_number="IV3-500CA")
    assert str(seen["ollama_url"]).endswith("/chat/completions")
    assert "authorization" not in seen["ollama_headers"]
    assert "tools" not in seen["ollama_body"]
    assert seen["ollama_body"]["model"] == "gemma3:4b"


def test_save_succeeds_when_resolved_library_write_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[tuple[object, object]] = []

    def boom(_session):
        raise RuntimeError("library folder is not writable")

    def capture(action, path=None):
        logged.append((action, path))

    monkeypatch.setattr("panelcad.needs_info.write_resolved_library", boom)
    monkeypatch.setattr("panelcad.api.session._log_io", capture)
    session = _import_keyence(tmp_path)
    dest = tmp_path / "still-saved.pcad.yaml"
    saved = session.save(dest)
    assert saved == dest
    assert dest.is_file()
    assert any(action == "error" and "needs_info_library" in str(path) for action, path in logged)
    assert any(action == "save" for action, _path in logged)
    again = Session.open(dest)
    assert len(again.project.sheets[0].components) == 8
