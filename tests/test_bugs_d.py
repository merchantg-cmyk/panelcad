"""Regression tests for tester D (main 2e9a73f). D12 is out of scope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panelcad import Session
from panelcad.io.bom import grouped_rows, instance_rows
from panelcad.quote.build import build_quote

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_catalog.pcad.yaml"


def _unknown_csv(tmp_path: Path) -> Path:
    path = tmp_path / "unknown.csv"
    path.write_text(
        "Part Number,Description,Qty,Manufacturer\n"
        "ZZ-UNKNOWN-9,Mystery relay,2,Acme\n",
        encoding="utf-8",
    )
    return path


def test_d01_d03_needs_info_stays_off_the_quote(tmp_path: Path) -> None:
    session = Session.open(SAMPLE)
    before = build_quote(session.project, session.catalog, session.parts)
    session.import_bom(_unknown_csv(tmp_path))
    quote = build_quote(session.project, session.catalog, session.parts)
    assert [row for row in quote["parts"] if row.get("needs_info")] == []
    shown = " ".join(
        " ".join(
            str(row.get(key) or "")
            for key in (
                "part_number",
                "catalog",
                "description",
                "manufacturer",
                "vendor",
                "refs",
                "note",
                "flag",
            )
        )
        for row in quote["parts"]
    )
    assert "ZZ-UNKNOWN-9" not in shown
    assert "Mystery relay" not in shown
    assert "Acme" not in shown
    assert quote["needs_info"] == 2
    assert quote["needs_info_label"] == "2 parts need info (not counted)"
    assert "unpriced" not in quote["needs_info_label"]
    assert quote["unpriced"] == before["unpriced"]
    assert quote["incomplete"] is True
    assert "INCOMPLETE" in quote["total_label"]
    assert "2 parts need info, not included" in quote["total_label"]
    assert "unpriced" not in quote["needs_info_label"]
    assert quote["lines"] == before["lines"]
    assert quote["totals"]["parts_sell"] == before["totals"]["parts_sell"]
    assert quote["totals"]["total"] == before["totals"]["total"]
    assert "stay off the quote" in " ".join(quote["assumptions"])


def test_d02_package_and_csv_list_needs_info_without_counting_it(tmp_path: Path) -> None:
    session = Session.create("Needs")
    session.import_bom(_unknown_csv(tmp_path))
    flat = tmp_path / "flat.csv"
    grouped = tmp_path / "grouped.csv"
    session.export_bom(flat, grouped=False)
    session.export_bom(grouped, grouped=True)
    for path in (flat, grouped):
        text = path.read_text(encoding="utf-8-sig")
        assert "Needs info (not counted)" in text
        assert "ZZ-UNKNOWN-9" in text
        assert "Mystery relay" in text
        assert "Acme" in text
        assert "needs-info" in text
    counted = instance_rows(session.project, session.catalog)
    assert all(row["part_number"] != "ZZ-UNKNOWN-9" for row in counted)
    assert all(row["library_id"] != "reconstruct.needs_info" for row in grouped_rows(session.project, session.catalog))

    dest = tmp_path / "package.pdf"
    session.print_package(dest, sections=["bom"])
    from panelcad.render.print_package import extract_pdf_text

    pdf = extract_pdf_text(dest)
    assert "Needs info (not counted)" in pdf
    assert "ZZ-UNKNOWN-9" in pdf
    assert "Mystery relay" in pdf
    assert "Acme" in pdf


def test_d04_recovered_save_keeps_the_original_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.diag.crash import begin_session, current_lock_path, mark_clean_exit
    from panelcad.io.yaml_format import save_project

    folder_a = tmp_path / "A"
    folder_b = tmp_path / "B"
    (folder_a / "out" / "s4").mkdir(parents=True)
    folder_b.mkdir()
    original = folder_a / "out" / "s4" / "job.pcad.yaml"
    original.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    begin_session(gui=True)
    monkeypatch.chdir(folder_a)
    session = Session.open(Path("out") / "s4" / "job.pcad.yaml")
    assert session.path == original.resolve()
    lock = json.loads(current_lock_path().read_text(encoding="utf-8"))
    assert Path(lock["project_path"]) == original.resolve()
    session.project.name = "Edited after recovery"
    recovery = tmp_path / "recovery-copy.pcad.yaml"
    save_project(session.project, recovery)
    before = original.read_text(encoding="utf-8")
    monkeypatch.chdir(folder_b)
    opened = Session.open_recovery(recovery, lock["project_path"])
    opened.save()
    assert "Edited after recovery" in original.read_text(encoding="utf-8")
    assert not (folder_b / "out" / "s4" / "job.pcad.yaml").exists()
    assert "Edited after recovery" not in before
    mark_clean_exit(force=True)


def test_d05_redacts_configured_endpoint_and_common_key_shapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from panelcad.diag.redact import redact_text
    from panelcad.diag.settings import BugSettings, save_settings

    hook = "http://127.0.0.1:9/hook/T000FAKE/B000FAKE/supersecretvalue"
    relay = "https://relay.example/ingest?key=relaysecretvalue"
    save_settings(BugSettings(webhook_url=hook, relay_url=relay))
    monkeypatch.setenv("PANELCAD_BUG_RELAY", relay)
    text = "\n".join(
        [
            f"endpoint {hook}",
            f"relay {relay}",
            "ANTHROPIC_API_KEY=anthropicsecretvalue",
            "token ghp_1234567890abcdefghij",
            "https://discord.com/api/webhooks/1234567890/abcdefghijklmnop",
            "see https://ada:s3cretpass@files.example/drawings",
            "Authorization: Bearer supersecrettokenvalue",
        ]
    )
    out = redact_text(text)
    for secret in (
        "supersecretvalue",
        "relaysecretvalue",
        "anthropicsecretvalue",
        "ghp_1234567890abcdefghij",
        "abcdefghijklmnop",
        "s3cretpass",
        "supersecrettokenvalue",
    ):
        assert secret not in out
    assert "ANTHROPIC_API_KEY=[redacted]" in out
    assert "Bearer [redacted]" in out
    session = Session.create("Redact")
    result = session.report_bug("Leak", text, steps=f"open {hook}", send=False)
    blob = Path(result["path"]).read_bytes()
    assert b"supersecretvalue" not in blob
    assert b"relaysecretvalue" not in blob


def test_d06_bot_destination_names_the_channel() -> None:
    from panelcad.diag.settings import BugSettings

    cfg = BugSettings(slack_bot_token="xoxb-fake-bot-token-123456", slack_channel="C1234567890").with_env()
    assert cfg.destination_name() == "Slack channel C1234567890 (bot)"
    hooked = BugSettings(webhook_url="https://hooks.slack.com/services/T/B/secret").with_env()
    assert hooked.destination_name() == "your webhook"


def test_d07_flat_bom_counts_a_ref_once(tmp_path: Path) -> None:
    session = Session.open(SAMPLE)
    flat = instance_rows(session.project, session.catalog)
    grouped = grouped_rows(session.project, session.catalog)
    refs = [row["ref"] for row in flat]
    assert len(refs) == len(set(refs))
    assert len(flat) == len(grouped) == 6
    assert sum(int(row["qty"]) for row in flat) == 6
    q1 = next(row for row in flat if row["ref"] == "Q1")
    assert q1["qty"] == "1"
    exported = tmp_path / "flat.csv"
    session.export_bom(exported, grouped=False)
    body = exported.read_text(encoding="utf-8-sig")
    assert body.count("\nQ1,") + body.count(",Q1,") >= 1
    # The tag is one bill line, not one line per sheet.
    assert sum(1 for line in body.splitlines() if ",Q1," in f",{line},") == 1


def test_d08_quote_dock_follows_the_dark_theme(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.quote_dock import QuoteDock
    from panelcad.theme import set_theme, ui_hex

    app = QApplication.instance() or QApplication([])
    set_theme("dark")
    session = Session.open(SAMPLE)
    dock = QuoteDock()
    dock.set_session(session)
    app.processEvents()
    field = ui_hex("field").lower()
    sheet = dock.parts.styleSheet().lower()
    assert field in sheet
    assert "#ffffff" not in sheet
    assert "255,255,255" not in sheet
    banner = dock.banner.styleSheet().lower()
    footer = dock.totals.styleSheet().lower()
    assert "#fff6d8" not in banner
    assert "#ffffff" not in footer
    assert "#f4c7c7" not in footer
    set_theme("light")


def test_d09_ai_without_settings_does_not_call_ollama(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    def refused(*_args, **_kwargs):
        raise AssertionError("network")

    monkeypatch.setattr("socket.create_connection", refused)
    runner = CliRunner()
    result = runner.invoke(app, ["ai", "add a start pushbutton", "-p", str(tmp_path / "job.pcad.yaml")])
    assert result.exit_code == 1
    assert "connect your own AI in Settings > AI (no key is built in)" in result.output
    assert "Traceback" not in result.output
    assert "ModelAPIError" not in result.output


def test_d10_digit_above_of_is_needs_info() -> None:
    from panelcad.importer.read import SheetGlyph, sheet_from_glyphs

    of_word = SheetGlyph("OF", 0.96, 200, 40, 30, 20)
    count = SheetGlyph("12", 0.96, 236, 40, 28, 20)
    above = SheetGlyph("4", 0.95, 210, 8, 14, 18)
    title = [
        SheetGlyph("PUMP", 0.95, 20, 8, 70, 18),
        SheetGlyph("STATION", 0.95, 100, 8, 90, 18),
        above,
        of_word,
        count,
    ]
    found = sheet_from_glyphs(title, in_title_block=True)
    assert found["sheet_number"] == ""
    assert found["sheet_count"] == "12"
    assert found["sheet_number_needs_info"] is True

    same_line = sheet_from_glyphs(
        [SheetGlyph("4", 0.95, 160, 40, 16, 20), of_word, count],
        in_title_block=True,
    )
    assert same_line["sheet_number"] == "4"
    assert same_line["sheet_count"] == "12"
    assert same_line["sheet_number_needs_info"] is False


def test_d11_bare_sheet_fraction_is_kept_and_a_scale_is_not() -> None:
    from panelcad.importer.read import sheet_fields_from_text

    bare = sheet_fields_from_text("12/12", in_title_block=True)
    assert bare == {"sheet_number": "12", "sheet_count": "12", "sheet_number_needs_info": False}
    titled = sheet_fields_from_text("PUMP STATION 12/12", in_title_block=True)
    assert titled["sheet_number"] == "" and titled["sheet_number_needs_info"] is True
    scale = sheet_fields_from_text("SCALE 3/7", in_title_block=True)
    assert scale["sheet_number"] == "" and scale["sheet_number_needs_info"] is False
    half = sheet_fields_from_text("1/2", in_title_block=True)
    assert half["sheet_number"] == "" and half["sheet_number_needs_info"] is False
    mixed = sheet_fields_from_text("SCALE 1/2 SHEET 6 OF 9", in_title_block=True)
    assert mixed["sheet_number"] == "6" and mixed["sheet_count"] == "9"
    outside = sheet_fields_from_text("12/12", in_title_block=False)
    assert outside["sheet_number"] == "" and outside["sheet_number_needs_info"] is False
    page = sheet_fields_from_text("PAGE 12/12", in_title_block=True)
    assert page["sheet_number"] == "12" and page["sheet_count"] == "12"
    pg = sheet_fields_from_text("PG. 4/6", in_title_block=True)
    assert pg["sheet_number"] == "4" and pg["sheet_count"] == "6"
    for text in ("REV A 3/12", "DWG 4471 REV A 3/12", "MOTOR 1/3 HP", "JOB 24/36"):
        found = sheet_fields_from_text(text, in_title_block=True)
        assert found["sheet_number"] == "" and found["sheet_number_needs_info"] is False, text
    dated = sheet_fields_from_text("DATE 10/17  SHEET 2 OF 5", in_title_block=True)
    assert dated["sheet_number"] == "2" and dated["sheet_count"] == "5"
    assert dated["sheet_number_needs_info"] is False
    for text in ("PUMP STATION\n12/12", "PUMP STATION 4\n12/12", "12/12\nREV A 3/12"):
        found = sheet_fields_from_text(text, in_title_block=True)
        assert found["sheet_number"] == "12" and found["sheet_count"] == "12", text
        assert found["sheet_number_needs_info"] is False, text
    conveyor = sheet_fields_from_text("LINE 7/9 CONVEYOR", in_title_block=True)
    assert conveyor["sheet_number"] == "" and conveyor["sheet_number_needs_info"] is True
    own_cell = sheet_fields_from_text("DATE\n10/17", in_title_block=True)
    assert own_cell["sheet_number"] == "" and own_cell["sheet_number_needs_info"] is True
    farther = sheet_fields_from_text("DATE\n10/17\n12/12", in_title_block=True)
    assert farther["sheet_number"] == "12" and farther["sheet_count"] == "12"
    assert farther["sheet_number_needs_info"] is False
    for text in (
        "PUMP STATION\nDATE\n10/17/2026\n12/12",
        "PUMP STATION\nDATE\n2026-10-17\n12/12",
        "PUMP STATION\nDATE\nOCT 17 2026\n12/12",
        "PUMP STATION\nDATE: 10/17/2026\n12/12",
        "DRAWN BY GM  DATE: 10/17/2026\n12/12",
        "DATE: 10/17\n12/12",
        "DATED 2026-10-17\n12/12",
        "DATE OCT 17 2026\n12/12",
        "12/12\nDATE\n10/17",
        "12/12\r\nDATE\r\n10/17",
        "12/12\nDATE",
        "DATE\n10/17/2026 12/12",
        "DATE\n10/17/2026      12/12",
        "PUMP STATION\nDATE\n10/17/26 12/12",
        "DATE\n12/12 10/17/2026",
        "DATE\n10 / 17 / 2026 12/12",
        "PLOT DATE\n10/17\n12/12",
        "ISSUE DATE\n10/17\n12/12",
        "DRAWN BY GM DATE\n12/12",
    ):
        found = sheet_fields_from_text(text, in_title_block=True)
        assert found["sheet_number"] == "12" and found["sheet_count"] == "12", text
        assert found["sheet_number_needs_info"] is False, text
        assert found["sheet_number"] != "10", text
    month_row = sheet_fields_from_text("DATE\nOCT 17 2026   3/5", in_title_block=True)
    assert month_row["sheet_number"] == "3" and month_row["sheet_count"] == "5"
    assert month_row["sheet_number_needs_info"] is False
    same_line_date = sheet_fields_from_text("DATE: 10/17/2026", in_title_block=True)
    assert same_line_date["sheet_number"] == "" and same_line_date["sheet_number_needs_info"] is False
    month_day = sheet_fields_from_text("DATE: 10/17", in_title_block=True)
    assert month_day["sheet_number"] == "" and month_day["sheet_number_needs_info"] is False
    for text in (
        "ISSUE DATE\n10/17",
        "REV DATE\n10/17",
        "DATE ISSUED\n10/17",
        "DRAWN DATE\n10/17",
        "DATE\n\n10/17",
        "DATE\n \n10/17",
        "PLOT DATE\n\n10/17",
    ):
        found = sheet_fields_from_text(text, in_title_block=True)
        assert found["sheet_number"] == "", text
        assert found["sheet_number"] != "10", text
        assert found["sheet_number_needs_info"] is True, text


def test_d13_print_review_stays_closed_until_an_import() -> None:
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow
    from panelcad.theme import set_theme, ui_hex

    app = QApplication.instance() or QApplication([])
    set_theme("dark")
    window = MainWindow(Session.open(SAMPLE))
    window.resize(1366, 768)
    window.show()
    app.processEvents()
    assert window.import_review_wrap.isVisible() is False
    crop = window.import_review.crop.styleSheet().lower()
    assert ui_hex("panel").lower() in crop
    assert "#f4f1e8" not in crop
    assert "#ffffff" not in window.import_review.rebuilt.styleSheet().lower()
    window.close()
    set_theme("light")


def test_d14_recovery_path_is_elided_and_kept_in_full(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication

    from panelcad.diag.crash import UncleanExit
    from panelcad.gui.bug_report_dialog import RecoveryDialog

    app = QApplication.instance() or QApplication([])
    recovery = tmp_path / ("autosave-" + "9" * 80 + "-job.pcad.yaml")
    recovery.write_text("format: panelcad\nname: Job\nsheets: []\n", encoding="utf-8")
    info = UncleanExit(
        project_name="Job",
        project_path=str(tmp_path / "job.pcad.yaml"),
        recovery_path=str(recovery),
        when="2026-10-17T00:00:00Z",
    )
    dialog = RecoveryDialog(None, Session.create("Job"), info)
    dialog.resize(640, 360)
    dialog.show()
    app.processEvents()
    from PySide6.QtWidgets import QLabel, QPushButton

    shown = dialog.findChild(QLabel, "recoveryCopyPath")
    assert shown is not None
    assert shown.toolTip() == str(recovery)
    assert shown.text() != str(recovery)
    assert "…" in shown.text() or shown.fontMetrics().horizontalAdvance(shown.text()) <= shown.width()
    opener = dialog.findChild(QPushButton, "recoveryOpenFolder")
    assert opener is not None and opener.text() == "Open folder"
    dialog.details.setChecked(True)
    app.processEvents()
    assert str(recovery) in dialog.details_view.toPlainText()
    dialog.close()


def test_needs_info_quote_warns_and_asks_before_the_customer_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    from panelcad.gui.quote_dock import QuoteDock

    session = Session.create("Priced")
    session.place(
        "iec.relays.k_4co_24vdc",
        ref="K1",
        x=20,
        y=20,
        properties={"unit_cost": "10.00", "part_number": "RH4B-UL", "manufacturer": "IDEC"},
    )
    session.import_bom(_unknown_csv(tmp_path))
    quote = build_quote(session.project, session.catalog, session.parts)
    assert quote["unpriced"] == 0
    assert quote["needs_info"] == 2
    assert quote["incomplete"] is True
    assert quote["total_label"] == "INCOMPLETE – 2 parts need info, not included"
    assert quote["totals"]["parts_sell"] != 0
    path = tmp_path / "job.pcad.yaml"
    session.save(path)
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    result = CliRunner().invoke(app, ["quote", "build", str(path)])
    assert result.exit_code == 0, result.output
    assert "warning: INCOMPLETE – 2 parts need info, not included" in result.output

    app_qt = QApplication.instance() or QApplication([])
    asked: dict[str, str] = {}

    def question(*args, **_kwargs):
        asked["text"] = str(args[2] if len(args) > 2 else "")
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *_a, **_k: None))
    dock = QuoteDock()
    dock.set_session(session)
    dock._export("pdf", "customer")
    app_qt.processEvents()
    assert "Write the customer file anyway?" in asked["text"]
    assert "2 parts need info, not included" in asked["text"]


def test_third_party_counted_column_is_not_a_needs_info_flag(tmp_path: Path) -> None:
    path = tmp_path / "vendor.csv"
    path.write_text(
        "Tag,Part Number,Description,Qty,Manufacturer,Counted\n"
        "K9,RH4B-UL,Relay,1,IDEC,0\n",
        encoding="utf-8",
    )
    session = Session.create("Vendor count")
    result = session.import_bom(path)
    assert result["created"]
    assert result["skipped"] == []
    comp = session.sheet().component(result["created"][0])
    assert comp.properties.get("part_number") == "RH4B-UL"
    assert comp.properties.get("manufacturer") == "IDEC"


def test_two_line_fraction_uses_its_own_line() -> None:
    from panelcad.importer.read import SheetGlyph, sheet_from_glyphs

    def glyph(text: str, left: float, top: float) -> SheetGlyph:
        return SheetGlyph(text, 0.95, left, top, 18.0 * max(len(text), 1), 20.0)

    stacked = sheet_from_glyphs(
        [
            glyph("PUMP", 20, 10),
            glyph("STATION", 120, 10),
            glyph("12/12", 20, 50),
        ],
        in_title_block=True,
    )
    assert stacked["sheet_number"] == "12" and stacked["sheet_count"] == "12"
    assert stacked["sheet_number_needs_info"] is False
    above_digit = sheet_from_glyphs(
        [
            glyph("PUMP", 20, 10),
            glyph("STATION", 120, 10),
            glyph("4", 280, 10),
            glyph("12/12", 20, 50),
        ],
        in_title_block=True,
    )
    assert above_digit["sheet_number"] == "12" and above_digit["sheet_count"] == "12"
    rev_below = sheet_from_glyphs(
        [
            glyph("12/12", 20, 10),
            glyph("REV", 20, 50),
            glyph("A", 80, 50),
            glyph("3/12", 120, 50),
        ],
        in_title_block=True,
    )
    assert rev_below["sheet_number"] == "12" and rev_below["sheet_count"] == "12"
    assert rev_below["sheet_number_needs_info"] is False
    shared = sheet_from_glyphs(
        [
            glyph("DATE", 20, 10),
            glyph("10/17/2026", 20, 50),
            glyph("12/12", 240, 50),
        ],
        in_title_block=True,
    )
    assert shared["sheet_number"] == "12" and shared["sheet_count"] == "12"
    assert shared["sheet_number_needs_info"] is False
    issue = sheet_from_glyphs(
        [
            glyph("ISSUE", 20, 10),
            glyph("DATE", 140, 10),
            glyph("10/17", 20, 50),
        ],
        in_title_block=True,
    )
    assert issue["sheet_number"] == "" and issue["sheet_number_needs_info"] is True
    rev_date = sheet_from_glyphs(
        [
            glyph("REV", 20, 10),
            glyph("DATE", 100, 10),
            glyph("10/17", 20, 50),
        ],
        in_title_block=True,
    )
    assert rev_date["sheet_number"] == "" and rev_date["sheet_number_needs_info"] is True
    plot = sheet_from_glyphs(
        [
            glyph("PLOT", 20, 10),
            glyph("DATE", 120, 10),
            glyph("10/17", 20, 50),
            glyph("12/12", 20, 90),
        ],
        in_title_block=True,
    )
    assert plot["sheet_number"] == "12" and plot["sheet_count"] == "12"
    assert plot["sheet_number_needs_info"] is False


def test_bom_export_round_trip_does_not_invent_a_needs_info_part(tmp_path: Path) -> None:
    import csv

    session = Session.open(SAMPLE)
    session.import_bom(_unknown_csv(tmp_path))
    before_ids = [
        (sheet.id, comp.id, comp.library_id)
        for sheet in session.project.sheets
        for comp in sheet.components
    ]
    before_quote = build_quote(session.project, session.catalog, session.parts)
    flat = tmp_path / "flat.csv"
    grouped = tmp_path / "grouped.csv"
    session.export_bom(flat, grouped=False)
    session.export_bom(grouped, grouped=True)

    def counted_qty(path: Path) -> int:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert rows
        assert "counted" in rows[0] and "flag" in rows[0]
        total = 0
        for row in rows:
            if str(row.get("counted") or "").strip().lower() != "yes":
                continue
            total += int(float(row.get("qty") or 0))
        return total

    bill = sum(int(row["qty"]) for row in instance_rows(session.project, session.catalog))
    assert counted_qty(flat) == counted_qty(grouped) == bill
    flat_text = flat.read_text(encoding="utf-8-sig")
    assert "Needs info (not counted)" in flat_text
    assert "needs-info" in flat_text

    again = session.import_bom(flat)
    grouped_again = session.import_bom(grouped)
    assert again["created"] == []
    assert grouped_again["created"] == []
    after_ids = [
        (sheet.id, comp.id, comp.library_id)
        for sheet in session.project.sheets
        for comp in sheet.components
    ]
    assert after_ids == before_ids
    after_quote = build_quote(session.project, session.catalog, session.parts)
    assert after_quote["needs_info"] == before_quote["needs_info"]
    assert after_quote["totals"]["total"] == before_quote["totals"]["total"]


def test_save_empty_string_keeps_the_open_file(tmp_path: Path) -> None:
    path = tmp_path / "job.pcad.yaml"
    session = Session.create("Open")
    session.save(path)
    session.project.name = "Still open"
    saved = session.save("")
    assert saved == path
    assert "Still open" in path.read_text(encoding="utf-8")
    assert not (tmp_path / ".pcad.yaml").exists()


def test_passed_in_webhook_is_redacted_at_any_path(tmp_path: Path) -> None:
    from panelcad.diag.redact import redact_text
    from panelcad.diag.settings import BugSettings

    url = "http://127.0.0.1:9/ingest/v1/ingestsecretvalue"
    assert "ingestsecretvalue" in redact_text(f"posted {url}")
    session = Session.create("Direct")
    result = session.report_bug(
        "Endpoint",
        f"The saved endpoint was {url}",
        steps="Report with the webhook still only in memory",
        settings=BugSettings(webhook_url=url, relay_url="", slack_bot_token=""),
        send=False,
    )
    blob = Path(result["path"]).read_bytes()
    assert b"ingestsecretvalue" not in blob
    assert url.encode() not in blob


def test_context_fractions_are_not_sheet_numbers_in_three_fonts() -> None:
    """REV, HP, JOB, and a month/day date must not become the sheet. 12/12 still does."""

    import shutil

    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    faces = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    missing = [path for path in faces if not Path(path).is_file()]
    if missing:
        pytest.skip("font missing: " + ", ".join(missing))
    from panelcad.importer.raster import ocr_title_sheet, prepare_opencv, read_pixels
    from panelcad.library.catalog import default_catalog

    dpi = 150

    def read(text: str | list[str], font_path: str) -> dict:
        lines = [text] if isinstance(text, str) else list(text)
        image = Image.new("RGB", (int(11 * dpi), int(8.5 * dpi)), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 12, image.width - 12, image.height - 12), outline=(0, 0, 0), width=3)
        font = ImageFont.truetype(font_path, 36)
        y = image.height - 90 - 48 * (len(lines) - 1)
        for line in lines:
            cells = [line] if isinstance(line, str) else list(line)
            x = 48
            for cell in cells:
                draw.text((x, y), cell, fill=(0, 0, 0), font=font)
                x += 320
            y += 48
        ppm = dpi / 25.4
        crop = ocr_title_sheet(np.asarray(image), ppm, image.width / ppm, image.height / ppm)
        prepared, _angles = prepare_opencv(image)
        facts = read_pixels(prepared, prepared.width / ppm, prepared.height / ppm, default_catalog())
        return {"crop": crop, "title": facts["title"]}

    for font_path in faces:
        for text in ("REV A 3/12", "MOTOR 1/3 HP", "JOB 24/36", "DWG 4471"):
            found = read(text, font_path)
            assert found["crop"]["sheet_number"] == "", (font_path, text, found["crop"])
            assert found["crop"]["sheet_number_needs_info"] is False, (font_path, text, found["crop"])
            assert found["title"]["sheet_number"] == "", (font_path, text, found["title"])
            assert found["title"]["sheet_number_needs_info"] is False, (font_path, text, found["title"])
        dated = read("DATE 10/17  SHEET 2 OF 5", font_path)
        assert dated["crop"]["sheet_number"] == "2" and dated["crop"]["sheet_count"] == "5", dated["crop"]
        assert dated["title"]["sheet_number"] == "2" and dated["title"]["sheet_count"] == "5", dated["title"]
        assert dated["title"]["sheet_number_needs_info"] is False
        bare = read("12/12", font_path)
        assert bare["title"]["sheet_number"] == "12" and bare["title"]["sheet_count"] == "12", bare["title"]
        page = read("PAGE 12/12", font_path)
        assert page["title"]["sheet_number"] == "12" and page["title"]["sheet_count"] == "12", page["title"]
        pg = read("PG. 4/6", font_path)
        assert pg["title"]["sheet_number"] == "4" and pg["title"]["sheet_count"] == "6", pg["title"]
        for lines in (
            ["PUMP STATION", "12/12"],
            ["PUMP STATION 4", "12/12"],
            ["12/12", "REV A 3/12"],
        ):
            stacked = read(lines, font_path)
            assert stacked["crop"]["sheet_number"] == "12" and stacked["crop"]["sheet_count"] == "12", (
                font_path,
                lines,
                stacked["crop"],
            )
            assert stacked["title"]["sheet_number"] == "12" and stacked["title"]["sheet_count"] == "12", (
                font_path,
                lines,
                stacked["title"],
            )
            assert stacked["title"]["sheet_number_needs_info"] is False
        for lines in (
            ["PUMP STATION", "DATE", "10/17/2026", "12/12"],
            ["PUMP STATION", "DATE: 10/17/2026", "12/12"],
            ["12/12", "DATE", "10/17"],
            ["DATE", "10/17", "12/12"],
        ):
            stacked = read(lines, font_path)
            assert stacked["crop"]["sheet_number"] == "12" and stacked["crop"]["sheet_count"] == "12", (
                font_path,
                lines,
                stacked["crop"],
            )
            assert stacked["crop"]["sheet_number"] != "10"
            assert stacked["title"]["sheet_number"] == "12" and stacked["title"]["sheet_count"] == "12", (
                font_path,
                lines,
                stacked["title"],
            )
            assert stacked["title"]["sheet_number_needs_info"] is False
            assert stacked["title"]["sheet_number"] != "10"
        under_date = read(["DATE", "10/17"], font_path)
        assert under_date["crop"]["sheet_number"] == "", (font_path, under_date["crop"])
        assert under_date["crop"]["sheet_number_needs_info"] is True
        assert under_date["title"]["sheet_number"] == "", (font_path, under_date["title"])
        assert under_date["title"]["sheet_number_needs_info"] is True
        shared_row = read(["DATE", ["10/17/2026", "12/12"]], font_path)
        assert shared_row["crop"]["sheet_number"] == "12" and shared_row["crop"]["sheet_count"] == "12", (
            font_path,
            shared_row["crop"],
        )
        assert shared_row["title"]["sheet_number"] == "12" and shared_row["title"]["sheet_count"] == "12", (
            font_path,
            shared_row["title"],
        )
        assert shared_row["title"]["sheet_number_needs_info"] is False
        for lines in (["ISSUE DATE", "10/17"], ["REV DATE", "10/17"]):
            claimed = read(lines, font_path)
            assert claimed["crop"]["sheet_number"] == "", (font_path, lines, claimed["crop"])
            assert claimed["crop"]["sheet_number_needs_info"] is True, (font_path, lines, claimed["crop"])
            assert claimed["title"]["sheet_number"] == "", (font_path, lines, claimed["title"])
            assert claimed["title"]["sheet_number_needs_info"] is True, (font_path, lines, claimed["title"])
        plotted = read(["PLOT DATE", "10/17", "12/12"], font_path)
        assert plotted["crop"]["sheet_number"] == "12" and plotted["crop"]["sheet_count"] == "12", (
            font_path,
            plotted["crop"],
        )
        assert plotted["title"]["sheet_number"] == "12" and plotted["title"]["sheet_count"] == "12", (
            font_path,
            plotted["title"],
        )
        assert plotted["title"]["sheet_number_needs_info"] is False
