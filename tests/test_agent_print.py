"""Outside-agent print prep, apply, and the read-back commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from panelcad.cli.main import app
from panelcad.importer.agent_exchange import SCHEMA_VERSION, load_schema, validate_response

runner = CliRunner()


def _write_print(path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "ACME")
    page.insert_text((130, 72), "CONTROLS")
    page.insert_text((72, 110), "E-100")
    page.insert_text((72, 140), "A")
    page.draw_line((80, 300), (300, 300))
    page.insert_text((80, 300), "Q1")
    page.insert_text((80, 318), "1489-M1C010")
    page.insert_text((250, 300), "CR1")
    page.insert_text((160, 292), "101")
    doc.save(path)
    doc.close()


def _token(page: dict, text: str) -> dict:
    want = text.upper()
    for item in page["text"]:
        if str(item.get("text") or "").upper() == want:
            return item
    found = [str(item.get("text") or "") for item in page["text"]]
    raise AssertionError(f"{text} not in extracted text: {found}")


def _device(page: dict, tag: str, **extra) -> dict:
    token = _token(page, tag)
    row = {
        "tag": tag,
        "symbol": "breaker",
        "contact": "",
        "x": float(token["x"]),
        "y": float(token["y"]),
        "sheet": "page_001",
        "rung": 1,
        "confidence": 0.95,
        "needs_info": False,
        "manufacturer": "",
        "catalog": "",
        "terminals": [],
        "bbox": {
            "x": float(token["x"]) - 6,
            "y": float(token["y"]) - 6,
            "w": 14,
            "h": 12,
        },
    }
    row.update(extra)
    return row


def _response(page: dict, prep: Path) -> dict:
    q1 = _device(
        page,
        "Q1",
        symbol="breaker",
        manufacturer="Allen-Bradley",
        catalog="1489-M1C010",
        terminals=["1", "2"],
    )
    other = _device(page, "Q1", symbol="pilot", confidence=0.4, catalog="")
    cr1 = _device(
        page,
        "CR1",
        symbol="relay",
        contact="NO",
        confidence=0.9,
        needs_info=False,
        catalog="NO-SUCH-PART",
        terminals=["A1", "A2"],
    )
    missing = _device(page, "Q1", symbol="relay", x=40, y=40, catalog="")
    missing["tag"] = "ZZ9"
    return {
        "schema_version": SCHEMA_VERSION,
        "prep_dir": str(prep),
        "project_name": "Agent round trip",
        "title_block": {
            "company": "ACME CONTROLS",
            "project": "",
            "title": "",
            "drawing_number": "E-100",
            "rev": "A",
            "sheet_number": "",
            "sheet_count": "",
            "customer": "",
            "location": "",
            "drawn_by": "",
            "date": "",
            "scale": "",
            "standard": "",
            "confidence": 0.9,
            "needs_info": False,
            "unreadable_fields": [],
        },
        "devices": [q1, other, cr1, missing],
        "terminals": [
            {
                "device": "CR1",
                "terminal": "A1",
                "sheet": "page_001",
                "confidence": 0.8,
                "needs_info": False,
                "bbox": {"x": float(cr1["x"]) - 2, "y": float(cr1["y"]) - 2, "w": 8, "h": 8},
            }
        ],
        "wires": [
            {
                "number": "101",
                "from": "Q1.2",
                "to": "CR1.A1",
                "sheet": "page_001",
                "confidence": 0.9,
                "needs_info": False,
                "bbox": {"x": float(q1["x"]), "y": float(q1["y"]) - 4, "w": 30, "h": 8},
            },
            {
                "number": "999",
                "from": "Q1.1",
                "to": "CR1.A2",
                "sheet": "page_001",
                "confidence": 0.9,
                "needs_info": False,
                "bbox": {"x": 10, "y": 10, "w": 8, "h": 4},
            },
        ],
    }


def test_schema_example_and_docs_match_v1() -> None:
    schema = load_schema()
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert validate_response(schema["examples"][0]) == []
    text = Path("docs/AGENTS.md").read_text(encoding="utf-8")
    start = text.index("```json")
    body = text[start + len("```json") : text.index("```", start + 3)]
    assert validate_response(json.loads(body)) == []
    assert "I need to re-read" not in text
    assert "Let me fix that sentence" not in text
    assert "crops of that PNG" in text
    assert "share one `number`" in text


def test_agent_print_round_trip(tmp_path: Path) -> None:
    from PIL import Image

    from panelcad.api.session import Session
    from panelcad.io.bom import grouped_rows
    from panelcad.quote.build import build_quote

    pdf = tmp_path / "sheet.pdf"
    _write_print(pdf)
    folder = tmp_path / "agent"
    prepared = runner.invoke(app, ["import", "prep", str(pdf), "--out", str(folder), "--dpi", "72"])
    assert prepared.exit_code == 0, prepared.stdout + prepared.stderr
    prep = json.loads(prepared.stdout)
    assert prep["schema_version"] == SCHEMA_VERSION
    assert prep["dpi"] == 72
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert (folder / manifest["schema"]).is_file()
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "panelcad.agent-print/v1" in readme
    assert "device.terminal" in readme
    assert "no API key" in readme
    page = json.loads((folder / "pages" / "page_001.json").read_text(encoding="utf-8"))
    assert page["lines"], "vector lines should be extracted"
    tiles = manifest["pages"][0]["tiles"]
    assert len(tiles) >= 2
    boxes = [(float(tile["x"]), float(tile["x"]) + float(tile["w"])) for tile in tiles]
    assert any(right > other_left and left < other_right for left, right in boxes for other_left, other_right in boxes if (left, right) != (other_left, other_right))
    with Image.open(folder / "pages" / "page_001.png") as image:
        assert abs(image.width - 612) <= 2

    response = _response(page, folder)
    agent_json = tmp_path / "agent.json"
    agent_json.write_text(json.dumps(response), encoding="utf-8")
    project = tmp_path / "job.pcad.yaml"
    applied = runner.invoke(app, ["import", "apply", str(agent_json), "--out", str(project)])
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    report = json.loads(applied.stdout)
    assert report["ok"] is True
    assert set(report["counts"]) == {"connected", "needs_review", "rejected"}
    tile_body = json.loads((folder / manifest["pages"][0]["tiles"][0]["text"]).read_text(encoding="utf-8"))
    assert "text" in tile_body
    accepted = {(row["kind"], row["id"]) for row in report["accepted"]}
    assert ("device", "Q1") in accepted
    assert ("device", "CR1") in accepted
    assert ("wire", "101") in accepted
    assert any(row["id"] == "101" and row.get("as") == "wire" for row in report["accepted"])
    assert report["counts"]["connected"] >= 1
    assert any(row["id"] == "ZZ9" and "extracted text" in row["reason"] for row in report["rejected"])
    assert any(row["id"] == "999" for row in report["rejected"])
    assert any(row["kind"] == "device" and row["id"] == "Q1" for row in report["conflicts"])
    assert "CR1" in report["needs_info"]
    assert "Q1" not in report["needs_info"]

    session = Session.open(project)
    sheet = session.project.sheets[0]
    q1 = sheet.component("Q1")
    cr1 = sheet.component("CR1")
    assert q1.library_id == "iec.breakers.mcb_1p_16a"
    assert q1.properties["width_mm"] == 17.5
    assert q1.properties["height_mm"] == 111
    assert q1.properties["depth_mm"] == 69
    assert q1.properties["dim_source"] == "catalog"
    assert cr1.library_id == "reconstruct.needs_info"
    assert "width_mm" not in cr1.properties
    assert str(cr1.properties.get("bom") or "").lower() == "no"
    assert session.project.title_block.company == "ACME CONTROLS"
    assert session.project.title_block.drawing_number == "E-100"
    wire = next(item for item in sheet.wires if item.number == "101")
    assert {wire.frm, wire.to} == {"Q1.2", "CR1.A1"}

    refs = [part.strip() for row in grouped_rows(session.project, session.catalog) for part in str(row["ref"]).split(",")]
    assert "Q1" in refs
    assert "CR1" not in refs
    quote = build_quote(session.project, session.catalog, session.parts)
    # Main #69: needs-info parts stay off the quote and are counted in the note.
    assert not any("CR1" in str(row.get("refs") or "").split(", ") for row in quote["parts"])
    assert quote["needs_info"] >= 1
    assert quote["incomplete"] is True

    described = runner.invoke(app, ["describe", str(project), "--json"])
    assert described.exit_code == 0, described.stdout
    snapshot = json.loads(described.stdout)
    assert snapshot["problems"]
    assert snapshot["wire_list"]
    assert any(row["number"] == "101" for row in snapshot["wire_list"])
    checked = runner.invoke(app, ["check", str(project), "--json"])
    problems = json.loads(checked.stdout)
    assert checked.exit_code == 1
    assert problems["ok"] is False
    assert any(row["ref"] == "CR1" and row["severity"] == "error" for row in problems["problems"])
    assert any(row["check"] == "print_unknown_part" and row["ref"] == "CR1" for row in problems["problems"])
    assert any(row["check"] == "print_evidence" and row["ref"] == "ZZ9" for row in problems["problems"])

    numbered = runner.invoke(
        app,
        [
            "electrical",
            "set-wire",
            str(project),
            "--from",
            wire.frm,
            "--to",
            wire.to,
            "--sheet",
            sheet.id,
            "--number",
            "202",
            "--json",
        ],
    )
    assert numbered.exit_code == 0, numbered.stdout + numbered.stderr
    assert json.loads(numbered.stdout)["number"] == "202"
    assert Session.open(project).project.sheets[0].wires[0].number == "202"


def test_apply_rejects_a_schema_miss_without_writing(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    result = runner.invoke(app, ["import", "apply", str(bad), "--out", str(tmp_path / "no.pcad.yaml")])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert report["errors"]
    assert not (tmp_path / "no.pcad.yaml").exists()

    schema = load_schema()
    sample = json.loads(json.dumps(schema["examples"][0]))
    sample["devices"][0]["width_mm"] = 12
    assert any("width_mm" in error for error in validate_response(sample))


def test_import_wizard_outside_agent_option(tmp_path: Path) -> None:
    from PySide6.QtWidgets import QApplication, QCheckBox

    from panelcad.gui.import_wizard import ImportWizard

    QApplication.instance() or QApplication([])
    pdf = tmp_path / "sheet.pdf"
    _write_print(pdf)
    wizard = ImportWizard(workspace=tmp_path / "gui-agent")
    box = wizard.findChild(QCheckBox, "outsideAgent")
    assert box is not None
    assert box.text() == "Use an outside AI agent"
    box.setChecked(True)
    assert wizard.vision.isEnabled() is False
    assert wizard.run_btn.text() == "Prepare for an agent"
    assert wizard.agent_dpi.value() == 200
    wizard.files.setText(str(pdf))
    wizard.run_import()
    assert (tmp_path / "gui-agent" / "manifest.json").is_file()
    assert "no API key" in wizard.log.toPlainText()
    assert wizard.apply_btn.isEnabled()


def _box(token: dict, width: float = 16, height: float = 12) -> dict:
    return {
        "x": float(token["x"]) - width / 2,
        "y": float(token["y"]) - height / 2,
        "w": width,
        "h": height,
    }


def test_cross_checks_flag_clipped_tags_loose_wires_and_contact_state(tmp_path: Path) -> None:
    """General checks from a strong-agent trial: not tuned to one print.

    A wire counts as connected only when its number sits on a traced stroke and
    both ends touch it. A contact needs a coil of the same tag. NO/NC that
    disagrees with a diagonal on the symbol stays needs-info.
    """

    import pymupdf

    from panelcad.api.session import Session
    from panelcad.importer.agent_exchange import confirm_review

    pdf = tmp_path / "checks.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_line((70, 220), (250, 220))
    page.insert_text((70, 220), "QA")
    page.insert_text((210, 220), "CR9")
    page.insert_text((140, 214), "12")
    page.insert_text((40, 40), "99")
    page.insert_text((400, 80), "FAN1")
    page.insert_text((400, 140), "PB1")
    page.draw_line((390, 150), (430, 190))
    page.insert_text((300, 400), "K1")
    page.insert_text((80, 500), "TR1")
    doc.save(pdf)
    doc.close()

    folder = tmp_path / "prep"
    prepared = runner.invoke(app, ["import", "prep", str(pdf), "--out", str(folder), "--dpi", "200"])
    assert prepared.exit_code == 0, prepared.stdout + prepared.stderr
    assert json.loads(prepared.stdout)["dpi"] == 200
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    page_json = json.loads((folder / "pages" / "page_001.json").read_text(encoding="utf-8"))
    holders = []
    for tile in manifest["pages"][0]["tiles"]:
        body = json.loads((folder / tile["text"]).read_text(encoding="utf-8"))
        words = {_norm_text(item.get("text")) for item in body["text"]}
        if "K1" in words:
            holders.append(tile["id"])
    assert len(holders) >= 2

    def at(text: str) -> dict:
        return _token(page_json, text)

    qa, cr9, k1, pb1, tr1 = at("QA"), at("CR9"), at("K1"), at("PB1"), at("TR1")
    response = {
        "schema_version": SCHEMA_VERSION,
        "prep_dir": str(folder),
        "project_name": "Cross check",
        "title_block": {
            "company": "",
            "project": "",
            "title": "",
            "drawing_number": "",
            "rev": "",
            "sheet_number": "",
            "sheet_count": "",
            "customer": "",
            "location": "",
            "drawn_by": "",
            "date": "",
            "scale": "",
            "standard": "",
            "confidence": 0.5,
            "needs_info": True,
            "unreadable_fields": [],
        },
        "devices": [
            {**_bare("QA", qa, "coil"), "bbox": _box(qa, 20, 16)},
            {**_bare("CR9", cr9, "relay"), "bbox": _box(cr9, 20, 16)},
            {**_bare("K1", k1, "contact", contact="NO"), "bbox": _box(k1)},
            {**_bare("TR1", tr1, "timer"), "bbox": _box(tr1), "confidence": 0.95},
            {**_bare("TR1", tr1, "NC contact", contact="NC"), "bbox": _box(tr1), "confidence": 0.8},
            {**_bare("AN1", at("FAN1"), "coil"), "tag": "AN1", "bbox": _box(at("FAN1"))},
            {
                **_bare("PB1", pb1, "pushbutton", contact="NO"),
                "bbox": {"x": float(pb1["x"]) - 8, "y": float(pb1["y"]) - 4, "w": 22, "h": 22},
            },
        ],
        "terminals": [],
        "wires": [
            {
                "number": "12",
                "from": "QA.A1",
                "to": "CR9.A1",
                "sheet": "page_001",
                "confidence": 0.9,
                "needs_info": False,
                "bbox": _box(at("12"), 12, 6),
            },
            {
                "number": "99",
                "from": "QA.A2",
                "to": "CR9.A2",
                "sheet": "page_001",
                "confidence": 0.9,
                "needs_info": False,
                "bbox": _box(at("99"), 10, 6),
            },
        ],
    }
    agent_json = tmp_path / "agent.json"
    agent_json.write_text(json.dumps(response), encoding="utf-8")
    project = tmp_path / "job.pcad.yaml"
    applied = runner.invoke(app, ["import", "apply", str(agent_json), "--out", str(project)])
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    report = json.loads(applied.stdout)
    assert report["counts"]["connected"] == 1
    assert report["counts"]["needs_review"] >= 1
    assert report["counts"]["rejected"] >= 1
    assert any(row["id"] == "12" and row["as"] == "wire" for row in report["accepted"])
    assert any(row["id"] == "99" and "not near a traced conductor" in row["reason"] for row in report["needs_review"])
    assert any(row["id"] == "K1" and "no matching coil" in row["reason"] for row in report["needs_review"])
    assert any(row["id"] == "TR1" and "timer" in row["reason"] for row in report["needs_review"])
    assert any(row["id"] == "AN1" and "FAN1" in row["reason"] for row in report["rejected"])
    assert any(row["check"] == "print_contact_state" and row["ref"] == "PB1" for row in report["problems"])
    session = Session.open(project)
    loose = next(wire for wire in session.project.sheets[0].wires if wire.number == "99")
    assert loose.inferred is True
    review_id = next(row["review_id"] for row in report["needs_review"] if row["id"] == "99")
    confirm_review(project, review_id)
    again = Session.open(project)
    assert next(wire for wire in again.project.sheets[0].wires if wire.number == "99").inferred is False
    assert not any(
        row["check"] == "print_needs_review" and row["ref"] == "99" for row in again.design_checks()
    )


def test_open_questions_wait_for_a_person(tmp_path: Path) -> None:
    """Missing tags, contact state, and catalog numbers become questions. Nothing is guessed."""

    import pymupdf

    from PySide6.QtWidgets import QApplication, QLabel, QPushButton

    from panelcad.api.session import Session
    from panelcad.gui.import_wizard import ImportWizard
    from panelcad.io.bom import grouped_rows

    pdf = tmp_path / "ask.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((400, 80), "FAN1")
    page.insert_text((400, 140), "PB1")
    page.draw_line((390, 150), (430, 190))
    page.insert_text((80, 220), "QA")
    doc.save(pdf)
    doc.close()
    folder = tmp_path / "prep"
    prepared = runner.invoke(app, ["import", "prep", str(pdf), "--out", str(folder), "--dpi", "72"])
    assert prepared.exit_code == 0, prepared.stdout
    page_json = json.loads((folder / "pages" / "page_001.json").read_text(encoding="utf-8"))
    fan, pb, qa = _token(page_json, "FAN1"), _token(page_json, "PB1"), _token(page_json, "QA")
    response = {
        "schema_version": SCHEMA_VERSION,
        "prep_dir": str(folder),
        "project_name": "Questions",
        "title_block": {
            "company": "",
            "project": "",
            "title": "",
            "drawing_number": "",
            "rev": "",
            "sheet_number": "",
            "sheet_count": "",
            "customer": "",
            "location": "",
            "drawn_by": "",
            "date": "",
            "scale": "",
            "standard": "",
            "confidence": 0.2,
            "needs_info": True,
            "unreadable_fields": [],
        },
        "devices": [
            {**_bare("AN1", fan, "coil"), "tag": "AN1", "bbox": _box(fan)},
            {
                **_bare("PB1", pb, "pushbutton", contact="NO"),
                "bbox": {"x": float(pb["x"]) - 8, "y": float(pb["y"]) - 4, "w": 22, "h": 22},
            },
            {**_bare("QA", qa, "relay"), "bbox": _box(qa)},
        ],
        "terminals": [],
        "wires": [],
    }
    project = tmp_path / "job.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "agent.json", response)), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout
    report = json.loads(applied.stdout)
    questions = report["questions"]
    assert questions
    for row in questions:
        assert row["id"] and row["question"] and row["changes"]
        assert "candidates" in row
        assert row.get("bbox") or row.get("crop") or row.get("image")
    contact = next(row for row in questions if row["kind"] == "contact" and row["ref"] == "PB1")
    assert contact["candidates"] == ["NO", "NC"]
    assert Path(contact["crop"]).is_file()
    part = next(row for row in questions if row["kind"] == "part" and row["ref"] == "QA")
    checked = runner.invoke(app, ["check", str(project), "--json"])
    body = json.loads(checked.stdout)
    assert any(row["id"] == contact["id"] for row in body["questions"])

    refused = runner.invoke(app, ["import", "answer", str(project), part["id"], "NOT-A-REAL-PART"])
    assert refused.exit_code == 1
    still = json.loads(refused.stdout)
    assert any(row["id"] == part["id"] for row in still["questions"])
    session = Session.open(project)
    assert session.project.sheets[0].component("QA").library_id == "reconstruct.needs_info"
    refs = [bit.strip() for row in grouped_rows(session.project, session.catalog) for bit in str(row["ref"]).split(",")]
    assert "QA" not in refs
    assert "PB1" not in refs

    answered = runner.invoke(app, ["import", "answer", str(project), contact["id"], "NC"])
    assert answered.exit_code == 0, answered.stdout
    done = json.loads(answered.stdout)
    assert not any(row["id"] == contact["id"] for row in done["questions"])
    session = Session.open(project)
    assert session.project.sheets[0].component("PB1").properties["contact"] == "NC"
    assert session.project.sheets[0].component("PB1").library_id == "reconstruct.needs_info"

    tagged = runner.invoke(
        app,
        [
            "import",
            "answer",
            str(project),
            next(row["id"] for row in questions if row["kind"] == "tag" and row["ref"] == "AN1"),
            "FAN1",
        ],
    )
    assert tagged.exit_code == 0, tagged.stdout
    session = Session.open(project)
    fan_comp = session.project.sheets[0].component("FAN1")
    assert fan_comp.library_id == "reconstruct.needs_info"
    assert "width_mm" not in fan_comp.properties

    replaced = runner.invoke(
        app,
        ["import", "answer", str(project), part["id"], "Allen-Bradley", "1489-M1C010"],
    )
    assert replaced.exit_code == 0, replaced.stdout
    session = Session.open(project)
    qa_comp = session.project.sheets[0].component("QA")
    assert qa_comp.library_id == "iec.breakers.mcb_1p_16a"
    assert qa_comp.properties["width_mm"] == 17.5
    refs = [bit.strip() for row in grouped_rows(session.project, session.catalog) for bit in str(row["ref"]).split(",")]
    assert "QA" in refs
    assert "PB1" not in refs

    answers = tmp_path / "answers.json"
    pb_part = next(row["id"] for row in json.loads(replaced.stdout)["questions"] if row["kind"] == "part" and row["ref"] == "PB1")
    answers.write_text(json.dumps([{"id": pb_part, "value": "NO-SUCH"}]), encoding="utf-8")
    filed = runner.invoke(app, ["import", "answer", str(project), "--answers", str(answers)])
    assert filed.exit_code == 1
    assert Session.open(project).project.sheets[0].component("PB1").library_id == "reconstruct.needs_info"

    QApplication.instance() or QApplication([])
    wizard = ImportWizard(workspace=folder)
    wizard._agent_folder = folder
    wizard.session = Session.open(project)
    wizard._show_questions(json.loads(filed.stdout)["questions"])
    assert len(wizard.findChildren(QLabel, "agentQuestion")) == 1
    assert wizard.findChild(QPushButton, "agentSkipQuestion") is not None


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _norm_text(text: object) -> str:
    return "".join(str(text or "").split()).upper()


def test_weak_extracted_text_keeps_the_reading_for_confirm(tmp_path: Path) -> None:
    """Most agent tags missing from the text means the text is weak, not the reading."""

    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "weak",
        [{"text": "X31DNG", "x": 20, "y": 20, "w": 12, "h": 4}, {"text": "K1", "x": 40, "y": 40, "w": 8, "h": 4}],
    )
    tags = ["QA", "CR1", "M1", "PB1", "K1"]
    devices = [_synthetic(tag, 30 + index * 25, 80) for index, tag in enumerate(tags)]
    response = _envelope(folder, devices)
    response["wires"] = [
        {
            "number": "12",
            "from": "QA.A1",
            "to": "CR1.A1",
            "sheet": "page_001",
            "confidence": 0.9,
            "needs_info": False,
            "bbox": {"x": 30, "y": 70, "w": 20, "h": 6},
        }
    ]
    project = tmp_path / "weak.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "weak.json", response)), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    report = json.loads(applied.stdout)
    rejected_ids = {row["id"] for row in report["rejected"] if row["kind"] == "device"}
    assert rejected_ids.isdisjoint(tags)
    assert not any(row["id"] == "12" and row["kind"] == "wire" for row in report["rejected"])
    session = Session.open(project)
    placed = {comp.id for comp in session.project.sheets[0].components}
    assert set(tags) <= placed
    confirm = [row for row in report["questions"] if row["kind"] == "tag" and "did not read" in row["question"]]
    assert {row["ref"] for row in confirm} == {"QA", "CR1", "M1", "PB1"}
    assert all(row["ref"] != "K1" for row in confirm)
    wire = next(item for item in session.project.sheets[0].wires if {item.frm, item.to} == {"QA.A1", "CR1.A1"})
    assert wire.inferred is True
    kept = runner.invoke(app, ["import", "answer", str(project), confirm[0]["id"], confirm[0]["ref"]])
    assert kept.exit_code == 0, kept.stdout
    assert not any(row["id"] == confirm[0]["id"] for row in json.loads(kept.stdout)["questions"])


def test_vector_text_rejects_a_missing_tag_and_still_asks(tmp_path: Path) -> None:
    folder = _prep_folder(tmp_path / "few", [{"text": "QA", "x": 40, "y": 40, "w": 8, "h": 4}], vector=True)
    devices = [_synthetic("QA", 40, 40), _synthetic("CR1", 80, 40), _synthetic("M1", 120, 40)]
    project = tmp_path / "few.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "few.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout
    report = json.loads(applied.stdout)
    rejected = {row["id"] for row in report["rejected"] if row["kind"] == "device"}
    assert rejected == {"CR1", "M1"}
    asked = {row["ref"] for row in report["questions"] if row["kind"] == "tag"}
    assert {"CR1", "M1"} <= asked


def test_ocr_miss_confirms_the_tag_and_offers_a_near_match(tmp_path: Path) -> None:
    folder = _prep_folder(
        tmp_path / "ocr",
        [{"text": "CRI", "x": 40, "y": 40, "w": 10, "h": 4}, {"text": "ssi", "x": 80, "y": 40, "w": 10, "h": 4}],
        vector=False,
    )
    devices = [_synthetic("CR1", 40, 40), _synthetic("SS1", 80, 40)]
    project = tmp_path / "ocr.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "ocr.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    report = json.loads(applied.stdout)
    assert not any(row["kind"] == "device" for row in report["rejected"])
    cr1 = next(row for row in report["questions"] if row["kind"] == "tag" and row["ref"] == "CR1")
    assert "did not read CR1" in cr1["question"]
    assert "CRI" in cr1["question"]
    assert cr1["candidates"] == ["CR1"]
    ss1 = next(row for row in report["questions"] if row["kind"] == "tag" and row["ref"] == "SS1")
    assert "ssi" in ss1["question"]
    assert ss1["candidates"] == ["SS1"]


def test_answering_a_clipped_tag_merges_into_the_existing_device(tmp_path: Path) -> None:
    """A short reading whose full tag is already on the sheet closes the question."""

    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "clip",
        [
            {"text": "TR10", "x": 40, "y": 50, "w": 12, "h": 4},
            {"text": "QA", "x": 90, "y": 50, "w": 8, "h": 4},
            {"text": "CR9", "x": 140, "y": 50, "w": 10, "h": 4},
        ],
        vector=True,
    )
    devices = [
        _synthetic("TR10", 40, 50),
        _synthetic("QA", 90, 50),
        _synthetic("CR9", 140, 50),
        _synthetic("R10", 40, 90),
    ]
    project = tmp_path / "clip.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "clip.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    report = json.loads(applied.stdout)
    assert any(row["id"] == "R10" and "TR10" in row["reason"] for row in report["rejected"])
    question = next(row for row in report["questions"] if row["kind"] == "tag" and row["ref"] == "R10")
    assert "TR10" in question["candidates"]
    answered = runner.invoke(app, ["import", "answer", str(project), question["id"], "TR10"])
    assert answered.exit_code == 0, answered.stdout
    body = json.loads(answered.stdout)
    assert body["applied"] is True
    assert not any(row["id"] == question["id"] for row in body["questions"])
    session = Session.open(project)
    ids = [comp.id for comp in session.project.sheets[0].components]
    assert ids.count("TR10") == 1
    assert "R10" not in ids
    assert "reading R10 was confirmed as this tag" in session.project.sheets[0].component("TR10").properties["notes"]


def test_merging_a_placed_tag_keeps_its_wires(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    session = Session.create("Merge", sheet_id="page-001", title="page")
    props = {
        "reconstruct_needs_info": "yes",
        "description": "INFO NEEDED",
        "bom": "no",
        "print_import_pins": "A1",
    }
    for ref, x in (("R10", 30), ("TR10", 70), ("QA", 110)):
        session.place(NEEDS_INFO_LIBRARY_ID, ref=ref, x=x, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.wire("R10.A1", "QA.A1", number="12")
    session.project.properties["print_import"] = {
        "items": [
            {
                "kind": "device",
                "reason": "not_in_evidence",
                "text": "R10",
                "sheet": "page-001",
                "detail": "tag is not in the extracted text; extracted text has TR10",
                "candidates": ["TR10"],
                "bbox": {"x": 20, "y": 30, "w": 16, "h": 12},
                "x": 30,
                "y": 40,
            }
        ]
    }
    path = tmp_path / "placed.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    question = next(row for row in questions_for(session) if row["kind"] == "tag" and row["ref"] == "R10")
    result = apply_answer(session, question["id"], "TR10")
    assert result["applied"] is True
    assert not any(row["id"] == question["id"] for row in result["questions"])
    sheet = Session.open(path).project.sheets[0]
    assert [comp.id for comp in sheet.components].count("TR10") == 1
    assert not any(comp.id == "R10" for comp in sheet.components)
    wire = sheet.wires[0]
    assert {wire.frm, wire.to} == {"TR10.A1", "QA.A1"}


def test_rejected_wire_answer_adds_a_row_and_keeps_the_other(tmp_path: Path) -> None:
    """A missing wire row is added. A different row with the same number stays."""

    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "wires",
        [
            {"text": "QA", "x": 40, "y": 40, "w": 8, "h": 4},
            {"text": "CR1", "x": 90, "y": 40, "w": 10, "h": 4},
            {"text": "12", "x": 60, "y": 70, "w": 8, "h": 4},
        ],
        vector=True,
    )
    devices = [_synthetic("QA", 40, 40), _synthetic("CR1", 90, 40), _synthetic("SS1", 140, 40)]
    response = _envelope(folder, devices)
    response["wires"] = [
        {
            "number": "12",
            "from": "QA.A1",
            "to": "CR1.A1",
            "sheet": "page_001",
            "confidence": 0.9,
            "needs_info": False,
            "bbox": {"x": 40, "y": 60, "w": 40, "h": 8},
        },
        {
            "number": "12",
            "from": "QA.A1",
            "to": "SS1.1",
            "sheet": "page_001",
            "confidence": 0.9,
            "needs_info": False,
            "bbox": {"x": 90, "y": 60, "w": 40, "h": 8},
        },
        {
            "number": "99",
            "from": "QA.A2",
            "to": "CR1.A2",
            "sheet": "page_001",
            "confidence": 0.9,
            "needs_info": False,
            "bbox": {"x": 40, "y": 100, "w": 30, "h": 8},
        },
    ]
    project = tmp_path / "wires.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "wires.json", response)), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    report = json.loads(applied.stdout)
    assert any(row["kind"] == "wire" and row["ref"] == "99" for row in report["questions"])
    assert any(row["kind"] == "wire" and "SS1.1" in row.get("candidates", []) for row in report["questions"])
    kept = Session.open(project).project.sheets[0]
    assert any({wire.frm, wire.to} == {"QA.A1", "CR1.A1"} and wire.number == "12" for wire in kept.wires)
    missing = next(row for row in report["questions"] if row["kind"] == "wire" and row["ref"] == "99")
    created = runner.invoke(app, ["import", "answer", str(project), missing["id"], "QA.A2 to CR1.A2"])
    assert created.exit_code == 0, created.stdout + created.stderr
    sheet = Session.open(project).project.sheets[0]
    assert any(wire.number == "99" and {wire.frm, wire.to} == {"QA.A2", "CR1.A2"} for wire in sheet.wires)
    assert any(wire.number == "12" and {wire.frm, wire.to} == {"QA.A1", "CR1.A1"} for wire in sheet.wires)
    tag = next(row for row in json.loads(created.stdout)["questions"] if row["kind"] == "tag" and row["ref"] == "SS1")
    restored = runner.invoke(app, ["import", "answer", str(project), tag["id"], "SS1"])
    assert restored.exit_code == 0, restored.stdout + restored.stderr
    sheet = Session.open(project).project.sheets[0]
    assert any({wire.frm, wire.to} == {"QA.A1", "SS1.1"} for wire in sheet.wires)
    assert any(wire.number == "12" and {wire.frm, wire.to} == {"QA.A1", "CR1.A1"} for wire in sheet.wires)


def test_merge_closes_questions_for_the_old_tag(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    folder = _prep_folder(tmp_path / "merge", [{"text": "K1", "x": 20, "y": 20, "w": 8, "h": 4}], vector=False)
    devices = [
        _synthetic("M", 40, 80),
        _synthetic("M1", 90, 80),
    ]
    devices[0]["symbol"] = "contact"
    devices[1]["symbol"] = "coil"
    project = tmp_path / "merge.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "merge.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    report = json.loads(applied.stdout)
    tag = next(row for row in report["questions"] if row["kind"] == "tag" and row["ref"] == "M")
    answered = runner.invoke(app, ["import", "answer", str(project), tag["id"], "M1"])
    assert answered.exit_code == 0, answered.stdout + answered.stderr
    body = json.loads(answered.stdout)
    assert not any(row.get("ref") == "M" for row in body["questions"])
    assert not any(row.get("ref") == "M" for row in body["problems"])
    ids = [comp.id for comp in Session.open(project).project.sheets[0].components]
    assert "M" not in ids
    assert ids.count("M1") == 1


def test_crops_are_per_project_and_follow_the_page(tmp_path: Path) -> None:
    folder = _prep_folder(tmp_path / "crops", [{"text": "QA", "x": 40, "y": 40, "w": 8, "h": 4}], vector=True)
    devices = [_synthetic("ZZ9", 50, 60)]
    one = tmp_path / "one.pcad.yaml"
    two = tmp_path / "two.pcad.yaml"
    payload = _envelope(folder, devices)
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "crops.json", payload)), "--out", str(one)],
    )
    assert applied.exit_code == 0, applied.stdout
    report = json.loads(applied.stdout)
    question = next(row for row in report["questions"] if row["ref"] == "ZZ9")
    assert question["crop"]
    assert question["crop"].endswith(".png")
    assert Path(question["crop"]).parent.name == "one.pcad.agent-questions"
    again = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "crops.json", payload)), "--out", str(two)],
    )
    other = next(row for row in json.loads(again.stdout)["questions"] if row["ref"] == "ZZ9")
    assert Path(other["crop"]).parent.name == "two.pcad.agent-questions"
    assert Path(question["crop"]) != Path(other["crop"])


def test_scan_dpi_sets_millimetres(tmp_path: Path) -> None:
    from PIL import Image

    scan = tmp_path / "scan.png"
    image = Image.new("RGB", (600, 400), (255, 255, 255))
    ImageDraw = __import__("PIL.ImageDraw", fromlist=["ImageDraw"]).Draw
    ImageDraw(image).rectangle((8, 340, 590, 392), fill=(0, 0, 0))
    image.save(scan, dpi=(300, 300))
    folder = tmp_path / "dpi"
    prepared = runner.invoke(app, ["import", "prep", str(scan), "--out", str(folder), "--dpi", "200"])
    assert prepared.exit_code == 0, prepared.stdout + prepared.stderr
    page = json.loads((folder / "pages" / "page_001.json").read_text(encoding="utf-8"))
    assert abs(float(page["width_mm"]) - 50.8) < 0.2
    assert abs(float(page["height_mm"]) - 33.867) < 0.2


def test_wire_answer_adds_a_row_and_does_not_rewrite_another(tmp_path: Path) -> None:
    """Both ends add the named row. Another row of that number stays."""

    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    session = Session.create("Rows", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1,A1"}
    for ref, x in (("M10", 30.0), ("OL10", 70.0), ("PB11", 110.0)):
        session.place(NEEDS_INFO_LIBRARY_ID, ref=ref, x=x, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.wire("M10.1", "OL10.1", number="1012")
    session.wire("M10.1", "PB11.1", number="X1")
    session.wire("OL10.1", "PB11.1", number="X1")
    session.project.properties["print_import"] = {
        "items": [
            {
                "kind": "wire",
                "reason": "conflict",
                "text": "1012",
                "sheet": "page-001",
                "detail": "same endpoints already use another reading",
                "wire_from": "",
                "wire_to": "",
                "bbox": {"x": 20, "y": 30, "w": 12, "h": 8},
                "image": "",
            },
            {
                "kind": "wire",
                "reason": "conflict",
                "text": "X1",
                "sheet": "page-001",
                "detail": "more than one row",
                "bbox": {"x": 40, "y": 30, "w": 12, "h": 8},
            },
        ]
    }
    path = tmp_path / "rows.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    single = next(row for row in questions_for(session) if row["ref"] == "1012")
    many = next(row for row in questions_for(session) if row["ref"] == "X1")
    one = apply_answer(session, single["id"], "PB11.1 to M10.1")
    assert one["applied"] is True, one.get("error")
    sheet = Session.open(path).project.sheets[0]
    assert any({wire.frm, wire.to} == {"M10.1", "OL10.1"} and wire.number == "1012" for wire in sheet.wires)
    assert any({wire.frm, wire.to} == {"PB11.1", "M10.1"} and wire.number == "1012" for wire in sheet.wires)
    session = Session.open(path)
    two = apply_answer(session, many["id"], "M10.1 to OL10.1")
    assert two["applied"] is True, two.get("error")
    sheet = Session.open(path).project.sheets[0]
    assert any({wire.frm, wire.to} == {"M10.1", "PB11.1"} and wire.number == "X1" for wire in sheet.wires)
    assert any({wire.frm, wire.to} == {"M10.1", "OL10.1"} and wire.number == "X1" for wire in sheet.wires)


def test_wire_answer_places_a_tag_from_another_sheet(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    session = Session.create("Sheets", sheet_id="page-001", title="one")
    session.add_sheet("page-002", title="two", kind="schematic")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "A1,1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="M20", x=30, y=40, sheet_id="page-001", snap=False, allow_needs_info=True, properties=dict(props))
    session.place(NEEDS_INFO_LIBRARY_ID, ref="QA", x=30, y=40, sheet_id="page-002", snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "2011",
                "sheet": "page-002",
                "detail": "endpoint M20.1 is not an accepted device",
                "wire_from": "QA.A1",
                "wire_to": "M20.1",
                "bbox": {"x": 20, "y": 30, "w": 16, "h": 10},
            }
        ]
    }
    path = tmp_path / "sheets.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    question = next(row for row in questions_for(session) if row["ref"] == "2011")
    result = apply_answer(session, question["id"], "QA.A1 to M20.1")
    assert result["applied"] is True, result.get("error")
    second = Session.open(path).project.sheet("page-002")
    assert second.component("M20").library_id == NEEDS_INFO_LIBRARY_ID
    assert str(second.component("M20").properties.get("print_xref") or "").lower() == "yes"
    assert any({wire.frm, wire.to} == {"QA.A1", "M20.1"} and wire.number == "2011" for wire in second.wires)
    assert not any(row["kind"] == "part" and row["ref"] == "M20" and row["sheet"] == "page-002" for row in result["questions"])
    assert not any(row.get("ref") == "M20" and row.get("sheet") == "page-002" and "INFO NEEDED" in row.get("message", "") for row in result["problems"])


def test_same_pins_keep_both_wire_numbers(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import _replay_held_wires, apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    session = Session.create("Pins", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    for ref, x in (("M10", 30.0), ("TR10", 80.0)):
        session.place(NEEDS_INFO_LIBRARY_ID, ref=ref, x=x, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.wire("M10.1", "TR10.1", number="L1")
    session.project.properties["print_import"] = {
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "1031",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "M10.1",
                "wire_to": "TR10.1",
                "bbox": {"x": 20, "y": 20, "w": 10, "h": 8},
            }
        ],
        "held_wires": [
            {"number": "1031", "from": "M10.1", "to": "TR10.1", "sheet": "page-001", "bbox": {"x": 20, "y": 20, "w": 10, "h": 8}}
        ],
    }
    path = tmp_path / "pins.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    _replay_held_wires(session)
    session.save(path)
    session = Session.open(path)
    assert any(row["ref"] == "1031" for row in questions_for(session))
    assert [wire.number for wire in session.project.sheets[0].wires] == ["L1"]
    question = next(row for row in questions_for(session) if row["ref"] == "1031")
    result = apply_answer(session, question["id"], "M10.1 to TR10.1")
    assert result["applied"] is True, result.get("error")
    numbers = {wire.number for wire in Session.open(path).project.sheets[0].wires}
    assert numbers == {"L1", "1031"}


def test_unprinted_catalog_and_symbol_mismatch_stay_needs_info(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "parts",
        [
            {"text": "S1", "x": 40, "y": 40, "w": 8, "h": 4},
            {"text": "3RT2015-1BB41", "x": 40, "y": 48, "w": 28, "h": 4},
            {"text": "Q2", "x": 120, "y": 40, "w": 8, "h": 4},
        ],
        vector=True,
    )
    guessed = _synthetic("S1", 40, 40)
    guessed["symbol"] = "pushbutton"
    guessed["catalog"] = "3RT2015-1BB41"
    guessed["needs_info"] = False
    guessed["bbox"] = {"x": 30, "y": 30, "w": 40, "h": 24}
    absent = _synthetic("Q2", 120, 40)
    absent["symbol"] = "breaker"
    absent["catalog"] = "1489-M1C010"
    absent["needs_info"] = False
    absent["bbox"] = {"x": 110, "y": 30, "w": 20, "h": 16}
    project = tmp_path / "parts.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "parts.json", _envelope(folder, [guessed, absent]))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    sheet = Session.open(project).project.sheets[0]
    assert sheet.component("S1").library_id == "reconstruct.needs_info"
    assert sheet.component("Q2").library_id == "reconstruct.needs_info"
    questions = json.loads(applied.stdout)["questions"]
    assert any(row["ref"] == "S1" and "pushbutton" in row["question"] for row in questions)
    assert any(row["ref"] == "Q2" and "not printed on this device" in row["question"] for row in questions)


def test_guessed_catalog_families_stay_needs_info(tmp_path: Path) -> None:
    """A coil, a contact, and an unknown symbol do not receive a guessed part."""

    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "families",
        [
            {"text": "K1", "x": 40, "y": 40, "w": 8, "h": 4},
            {"text": "PSP24-060S", "x": 48, "y": 48, "w": 24, "h": 4},
            {"text": "C1", "x": 140, "y": 40, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 148, "y": 48, "w": 24, "h": 4},
            {"text": "U1", "x": 90, "y": 200, "w": 8, "h": 4},
            {"text": "3RT2015-1BB41", "x": 98, "y": 208, "w": 28, "h": 4},
            {"text": "Y1", "x": 40, "y": 100, "w": 8, "h": 4},
            {"text": "3RT2015-1BB41", "x": 48, "y": 108, "w": 28, "h": 4},
            {"text": "K2", "x": 40, "y": 150, "w": 8, "h": 4},
            {"text": "M1", "x": 200, "y": 150, "w": 8, "h": 4},
            {"text": "3RT2015-1BB41", "x": 210, "y": 158, "w": 28, "h": 4},
        ],
        vector=True,
    )

    def marked(tag: str, x: float, y: float, symbol: str, catalog: str) -> dict:
        row = _synthetic(tag, x, y)
        row["symbol"] = symbol
        row["catalog"] = catalog
        row["needs_info"] = False
        row["bbox"] = {"x": x - 8, "y": y - 6, "w": 36, "h": 16}
        return row

    devices = [
        marked("K1", 40, 40, "coil", "PSP24-060S"),
        marked("C1", 140, 40, "contact", "1489-M1C010"),
        marked("U1", 90, 200, "unknown", "3RT2015-1BB41"),
        marked("Y1", 40, 100, "coil", ""),
        marked("K2", 40, 150, "coil", "3RT2015-1BB41"),
        marked("M1", 200, 150, "coil", "3RT2015-1BB41"),
    ]
    project = tmp_path / "families.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "families.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    sheet = Session.open(project).project.sheets[0]
    assert sheet.component("K1").library_id == "reconstruct.needs_info"
    assert sheet.component("C1").library_id == "reconstruct.needs_info"
    assert sheet.component("U1").library_id == "reconstruct.needs_info"
    assert sheet.component("K2").library_id == "reconstruct.needs_info"
    assert sheet.component("M1").library_id == "iec.contactors.ac3_9a"
    questions = json.loads(applied.stdout)["questions"]
    assert any(row["ref"] == "K1" and "power" in row["question"] for row in questions)
    assert any(row["ref"] == "K2" and "not printed on this device" in row["question"] for row in questions)
    assert questions and all("_" not in str(row["sheet"]) for row in questions)


def test_wire_answer_rejects_an_unknown_tag(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    session = Session.create("Typo", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="PB2", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "prep_dir": str(tmp_path / "missing-prep"),
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "22",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "PB2.1",
                "wire_to": "CR4.1",
                "bbox": {"x": 20, "y": 30, "w": 12, "h": 8},
            }
        ],
    }
    path = tmp_path / "typo.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    question = next(row for row in questions_for(session) if row["ref"] == "22")
    result = apply_answer(session, question["id"], "PB2.1 to CRR4.1")
    assert result["applied"] is False
    assert "no device CRR4" in str(result.get("error") or "")
    ids = [comp.id for comp in Session.open(path).project.sheets[0].components]
    assert "CRR4" not in ids
    assert any(row["id"] == question["id"] for row in result["questions"])


def test_wire_answer_rejects_title_text_wire_numbers_and_bare_tokens(tmp_path: Path) -> None:
    """A word on the print is not a device just because it was typed as an end."""

    _assert_title_words_refused(tmp_path / "words", boxed=False)


def test_boxed_right_strip_title_words_are_refused(tmp_path: Path) -> None:
    _assert_title_words_refused(tmp_path / "boxed", boxed=True)


def _assert_title_words_refused(folder: Path, boxed: bool) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    folder = _prep_folder(
        folder,
        [
            {"text": "SHEET", "x": 160, "y": 250, "w": 16, "h": 4},
            {"text": "21", "x": 40, "y": 80, "w": 8, "h": 4},
            {"text": "G", "x": 40, "y": 100, "w": 6, "h": 4},
            {"text": "LS-2041-E1", "x": 40, "y": 120, "w": 28, "h": 4},
            {"text": "SOL1", "x": 80, "y": 40, "w": 12, "h": 4},
            {"text": "6,9", "x": 40, "y": 140, "w": 12, "h": 4},
            {"text": "102,205", "x": 40, "y": 155, "w": 18, "h": 4},
            {"text": "-K1:3,5", "x": 40, "y": 170, "w": 20, "h": 4},
            {"text": "SHEET", "x": 170, "y": 40, "w": 20, "h": 4},
            {"text": "REV", "x": 170, "y": 56, "w": 12, "h": 4},
            {"text": "DWG", "x": 170, "y": 72, "w": 14, "h": 4},
            {"text": "CUSTOMER", "x": 176, "y": 48, "w": 24, "h": 4},
            {"text": "JOBNO", "x": 176, "y": 100, "w": 18, "h": 4},
            {"text": "LS-2041-E1", "x": 90, "y": 258, "w": 28, "h": 4},
            {"text": "LIFT", "x": 122, "y": 250, "w": 14, "h": 4},
            {"text": "DUPLEX", "x": 140, "y": 250, "w": 18, "h": 4},
            {"text": "REV:", "x": 170, "y": 248, "w": 12, "h": 4},
        ],
    )
    if not boxed:
        page_path = folder / "pages" / "page_001.json"
        page = json.loads(page_path.read_text(encoding="utf-8"))
        page["lines"] = []
        page_path.write_text(json.dumps(page), encoding="utf-8")
    if boxed:
        page_path = folder / "pages" / "page_001.json"
        page = json.loads(page_path.read_text(encoding="utf-8"))
        page["lines"] = [
            {"x0": 165, "y0": 32, "x1": 210, "y1": 32},
            {"x0": 165, "y0": 112, "x1": 210, "y1": 112},
            {"x0": 165, "y0": 32, "x1": 165, "y1": 112},
            {"x0": 210, "y0": 32, "x1": 210, "y1": 112},
        ]
        page_path.write_text(json.dumps(page), encoding="utf-8")
    session = Session.create("Words", sheet_id="page-001", title="page")
    session.set_title_block(drawing_number="LS-2041-E1")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="PB2", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "prep_dir": str(folder),
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "21",
                "sheet": "page_001",
                "confirmed": True,
                "wire_from": "PB2.1",
                "wire_to": "PB2.1",
            },
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "22",
                "sheet": "page_001",
                "detail": "held",
                "wire_from": "PB2.1",
                "wire_to": "CR4.1",
                "bbox": {"x": 20, "y": 30, "w": 12, "h": 8},
            },
        ],
    }
    path = folder.parent / f"{folder.name}.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    question = next(row for row in questions_for(session) if row["ref"] == "22")
    assert question["sheet"] == "page-001"
    for answer in (
        "PB2.1 to SHEET.1",
        "PB2.1 to 21.1",
        "PB2.1 to G.1",
        "PB2.1 to LS-2041-E1.1",
        "PB2.1 to 6,9.1",
        "PB2.1 to 102,205.1",
        "PB2.1 to -K1:3,5.1",
        "PB2.1 to -K1/3.1",
        "PB2.1 to CUSTOMER.1",
        "PB2.1 to DUPLEX.1",
        "PB2.1 to LIFT.1",
        "PB2.1 to LS-2041-E1.1",
        "PB2.1 to JOBNO.1",
    ):
        result = apply_answer(session, question["id"], answer)
        assert result["applied"] is False, answer
        assert "no device" in str(result.get("error") or "")
        session = Session.open(path)
    ids = [comp.id for comp in session.project.sheets[0].components]
    assert ids == ["PB2"]


def test_wire_answer_adds_a_text_only_device_and_reuses_case(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    folder = _prep_folder(tmp_path / "solenoid", [{"text": "SOL1", "x": 80, "y": 40, "w": 12, "h": 4}])
    session = Session.create("Solenoid", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="PB2", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "prep_dir": str(folder),
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "1062",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "PB2.1",
                "wire_to": "SOL1.1",
                "bbox": {"x": 70, "y": 30, "w": 16, "h": 10},
            }
        ],
    }
    path = tmp_path / "solenoid.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    question = next(row for row in questions_for(session) if row["ref"] == "1062")
    result = apply_answer(session, question["id"], "PB2.1 to SOL1.1")
    assert result["applied"] is True, result.get("error")
    saved = Session.open(path)
    sol = saved.project.sheets[0].component("SOL1")
    assert sol.library_id == NEEDS_INFO_LIBRARY_ID
    assert str(sol.properties.get("print_xref") or "").lower() not in {"1", "true", "yes"}
    added = next(row for row in result["questions"] if row["kind"] == "device" and row["ref"] == "SOL1")
    assert added["question"] == "SOL1 is printed here but was not read as a device. Add it?"
    assert added["crop"]
    assert not any(row["kind"] == "part" and row["ref"] == "SOL1" for row in result["questions"])
    assert any(row.get("ref") == "SOL1" and "INFO NEEDED" in row.get("message", "") for row in result["problems"])
    session = Session.open(path)
    added = next(row for row in questions_for(session) if row["kind"] == "device" and row["ref"] == "SOL1")
    confirmed = apply_answer(session, added["id"], "yes")
    assert confirmed["applied"] is True, confirmed.get("error")
    part = next(row for row in confirmed["questions"] if row["kind"] == "part" and row["ref"] == "SOL1")
    assert part["question"].startswith("What catalog number is printed for SOL1?")

    session = Session.create("Case", sheet_id="page-001", title="page")
    session.place(NEEDS_INFO_LIBRARY_ID, ref="PB2", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.place(NEEDS_INFO_LIBRARY_ID, ref="CR4", x=80, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "22",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "PB2.1",
                "wire_to": "CR4.1",
                "bbox": {"x": 20, "y": 30, "w": 12, "h": 8},
            }
        ]
    }
    case_path = tmp_path / "case.pcad.yaml"
    session.save(case_path)
    session = Session.open(case_path)
    question = next(row for row in questions_for(session) if row["ref"] == "22")
    result = apply_answer(session, question["id"], "pb2.1 to cr4.1")
    assert result["applied"] is True, result.get("error")
    sheet = Session.open(case_path).project.sheets[0]
    assert [comp.id for comp in sheet.components] == ["PB2", "CR4"]
    assert any({wire.frm, wire.to} == {"PB2.1", "CR4.1"} for wire in sheet.wires)


def test_question_text_is_one_sentence(tmp_path: Path) -> None:
    from panelcad.importer.agent_questions import _question_from_item

    same_pin = _question_from_item(
        {
            "kind": "wire",
            "reason": "unresolved",
            "text": "22",
            "sheet": "page_001",
            "detail": "wire from and to are the same pin (CR4.1)",
            "wire_from": "CR4.1",
            "wire_to": "CR4.1",
        }
    )
    assert same_pin is not None
    assert "pin (CR4.1). Answer" in same_pin["question"]
    number = _question_from_item(
        {
            "kind": "wire",
            "reason": "conflict",
            "text": "22",
            "sheet": "page_001",
            "detail": "same endpoints already use wire number 21",
        }
    )
    assert number is not None
    assert "number 21. Answer" in number["question"]
    tag = _question_from_item(
        {
            "kind": "device",
            "reason": "not_in_evidence",
            "text": "-X2",
            "sheet": "page_001",
            "detail": "tag is not in the extracted text",
        }
    )
    assert tag is not None
    assert tag["question"] == "What tag is printed here? -X2 is not in the extracted text."


def test_catalog_kind_and_distance_stay_needs_info(tmp_path: Path) -> None:
    """A guessed part has to be the same kind, and printed close to that device."""

    from panelcad.api.session import Session
    from panelcad.importer.agent_exchange import _family_allows

    assert _family_allows("overload", "iec.protection.fuse") is False
    assert _family_allows("contactor aux contact", "iec.contactors.ac3_9a") is False
    assert _family_allows("pushbutton", "iec.operators.pilot_light") is False
    assert _family_allows("pilot", "iec.operators.pb_no") is False
    assert _family_allows("selector", "iec.operators.estop") is False
    assert _family_allows("estop", "iec.operators.pb_nc") is False
    assert _family_allows("horn", "iec.operators.pilot_light") is False
    assert _family_allows("fuse", "iec.protection.overload_9a") is False
    assert _family_allows("overload relay", "iec.relays.k_4co_24vdc") is False
    assert _family_allows("power supply", "iec.power.control_transformer") is False
    assert _family_allows("transformer", "iec.power.psu_24v_5a") is False
    assert _family_allows("coil", "iec.contactors.aux_no") is False
    assert _family_allows("disconnect", "iec.protection.disconnect_3p") is False
    assert _family_allows("limit switch", "iec.operators.pb_no") is False
    assert _family_allows("float switch", "iec.protection.overload_9a") is False
    assert _family_allows("unknown", "iec.contactors.ac3_9a") is False
    assert _family_allows("overload", "iec.protection.overload_9a") is True
    assert _family_allows("power supply", "iec.power.psu_24v_5a") is True
    assert _family_allows("transformer", "iec.power.control_transformer") is True
    assert _family_allows("coil", "iec.contactors.ac3_9a") is True
    assert _family_allows("coil", "iec.relays.k_4co_24vdc") is True
    assert _family_allows("pushbutton", "iec.operators.pb_no", "NO") is True
    assert _family_allows("pushbutton", "iec.operators.pb_nc", "NO") is False
    assert _family_allows("breaker", "iec.breakers.mcb_1p_16a") is True
    assert _family_allows("solenoid coil", "iec.contactors.ac3_9a") is False
    assert _family_allows("soft starter", "iec.contactors.ac3_9a") is False
    assert _family_allows("fused disconnect", "iec.protection.fuse") is False
    assert _family_allows("contactor", "iec.contactors.ac3_9a", "NO") is False
    assert _family_allows("relay", "iec.contactors.ac3_9a", "NC") is False
    assert _family_allows("pilot relay", "iec.operators.pilot_light") is False
    assert _family_allows("mcb", "iec.breakers.mpcb_3p") is False
    assert _family_allows("overload contact", "iec.protection.overload_9a", "NC") is False
    assert _family_allows("overload", "iec.protection.overload_9a", "NC") is False
    assert _family_allows("drive", "iec.drives.vfd_1k5") is True
    assert _family_allows("vfd", "iec.drives.vfd_1k5") is True
    assert _family_allows("plc", "iec.plc.compact_16dio") is True
    assert _family_allows("mcb", "iec.breakers.mcb_1p_16a") is True
    assert _family_allows("overload relay", "iec.protection.overload_9a") is True
    assert _family_allows("contactor coil", "iec.contactors.ac3_9a") is True
    assert _family_allows("relay coil", "iec.relays.k_4co_24vdc") is True
    assert _family_allows("relay coil", "iec.contactors.ac3_9a") is False
    assert _family_allows("relay", "iec.contactors.ac3_9a") is False
    assert _family_allows("selector switch", "iec.operators.selector_2pos") is True
    assert _family_allows("emergency stop", "iec.operators.estop") is True
    assert _family_allows("motor protector", "iec.breakers.mpcb_3p") is True
    assert _family_allows("overload relay", "iec.relays.k_4co_24vdc") is False

    folder = _prep_folder(
        tmp_path / "kinds",
        [
            {"text": "OL1", "x": 30, "y": 30, "w": 8, "h": 4},
            {"text": "EHCC3DIU", "x": 42, "y": 36, "w": 22, "h": 4},
            {"text": "AUX1", "x": 30, "y": 70, "w": 10, "h": 4},
            {"text": "3RT2015-1BB41", "x": 44, "y": 76, "w": 28, "h": 4},
            {"text": "S1", "x": 30, "y": 110, "w": 8, "h": 4},
            {"text": "GCX1232-24L", "x": 42, "y": 116, "w": 24, "h": 4},
            {"text": "PS1", "x": 30, "y": 150, "w": 8, "h": 4},
            {"text": "9070T100D23", "x": 42, "y": 156, "w": 24, "h": 4},
            {"text": "K9", "x": 30, "y": 190, "w": 8, "h": 4},
            {"text": "N9", "x": 150, "y": 190, "w": 8, "h": 4},
            {"text": "3RT2015-1BB41", "x": 30, "y": 260, "w": 28, "h": 4},
            {"text": "M9", "x": 120, "y": 30, "w": 8, "h": 4},
            {"text": "3RT2015-1BB41", "x": 134, "y": 30, "w": 28, "h": 4},
            {"text": "Q9", "x": 120, "y": 80, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 128, "y": 86, "w": 24, "h": 4},
        ],
        vector=True,
    )

    def marked(tag: str, x: float, y: float, symbol: str, catalog: str, bbox: dict) -> dict:
        row = _synthetic(tag, x, y)
        row["symbol"] = symbol
        row["catalog"] = catalog
        row["needs_info"] = False
        row["bbox"] = bbox
        return row

    devices = [
        marked("OL1", 30, 30, "overload", "EHCC3DIU", {"x": 22, "y": 24, "w": 16, "h": 12}),
        marked("AUX1", 30, 70, "contactor aux contact", "3RT2015-1BB41", {"x": 22, "y": 64, "w": 50, "h": 16}),
        marked("S1", 30, 110, "pushbutton", "GCX1232-24L", {"x": 22, "y": 104, "w": 50, "h": 16}),
        marked("PS1", 30, 150, "power supply", "9070T100D23", {"x": 22, "y": 144, "w": 50, "h": 16}),
        marked("K9", 30, 190, "coil", "3RT2015-1BB41", {"x": 22, "y": 184, "w": 16, "h": 12}),
        marked("N9", 150, 190, "coil", "", {"x": 142, "y": 184, "w": 16, "h": 12}),
        marked("M9", 120, 30, "coil", "3RT2015-1BB41", {"x": 112, "y": 24, "w": 16, "h": 12}),
        marked("Q9", 120, 80, "breaker", "1489-M1C010", {"x": 112, "y": 74, "w": 40, "h": 16}),
    ]
    project = tmp_path / "kinds.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "kinds.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    sheet = Session.open(project).project.sheets[0]
    assert sheet.component("OL1").library_id == "reconstruct.needs_info"
    assert sheet.component("AUX1").library_id == "reconstruct.needs_info"
    assert sheet.component("S1").library_id == "reconstruct.needs_info"
    assert sheet.component("PS1").library_id == "reconstruct.needs_info"
    assert sheet.component("K9").library_id == "reconstruct.needs_info"
    assert sheet.component("M9").library_id == "iec.contactors.ac3_9a"
    assert sheet.component("Q9").library_id == "iec.breakers.mcb_1p_16a"
    questions = json.loads(applied.stdout)["questions"]
    assert any(row["ref"] == "K9" and "not printed on this device" in row["question"] for row in questions)


def test_exact_symbol_kinds_assign_drive_and_plc(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "kinds2",
        [
            {"text": "D1", "x": 40, "y": 40, "w": 8, "h": 4},
            {"text": "GS21-20P5", "x": 50, "y": 46, "w": 24, "h": 4},
            {"text": "U1", "x": 40, "y": 120, "w": 8, "h": 4},
            {"text": "C0-00DD1-D", "x": 50, "y": 126, "w": 24, "h": 4},
            {"text": "B1", "x": 140, "y": 40, "w": 8, "h": 4},
            {"text": "3RV2011-1BA10", "x": 150, "y": 46, "w": 28, "h": 4},
        ],
        vector=True,
    )

    def marked(tag: str, x: float, y: float, symbol: str, catalog: str) -> dict:
        row = _synthetic(tag, x, y)
        row["symbol"] = symbol
        row["catalog"] = catalog
        row["needs_info"] = False
        row["bbox"] = {"x": x - 8, "y": y - 6, "w": 50, "h": 18}
        return row

    devices = [
        marked("D1", 40, 40, "drive", "GS21-20P5"),
        marked("U1", 40, 120, "plc", "C0-00DD1-D"),
        marked("B1", 140, 40, "mcb", "3RV2011-1BA10"),
    ]
    project = tmp_path / "kinds2.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "kinds2.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    sheet = Session.open(project).project.sheets[0]
    assert sheet.component("D1").library_id == "iec.drives.vfd_1k5"
    assert sheet.component("U1").library_id == "iec.plc.compact_16dio"
    assert sheet.component("B1").library_id == "reconstruct.needs_info"
    questions = json.loads(applied.stdout)["questions"]
    assert not any(row["ref"] == "D1" and "iec.drives" in row["question"] for row in questions)
    assert any(row["ref"] == "B1" and "mpcb" in row["question"] for row in questions)


def test_catalog_distance_uses_the_tighter_limit(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_exchange import _excluded_boxes, _point_in_any_box

    frame = {
        "width_mm": 400,
        "height_mm": 280,
        "token_rows": [],
        "lines": [
            {"x0": 10, "y0": 10, "x1": 390, "y1": 10},
            {"x0": 10, "y0": 250, "x1": 390, "y1": 250},
            {"x0": 10, "y0": 10, "x1": 10, "y1": 250},
            {"x0": 390, "y0": 10, "x1": 390, "y1": 250},
        ],
    }
    boxes = _excluded_boxes(frame)
    assert _point_in_any_box(114, 272, boxes)
    assert not _point_in_any_box(114, 200, boxes)

    folder = _prep_folder(
        tmp_path / "reach",
        [
            {"text": "B4", "x": 40, "y": 40, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 48, "y": 46, "w": 24, "h": 4},
            {"text": "B5", "x": 40, "y": 180, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 40, "y": 230, "w": 24, "h": 4},
        ],
        vector=True,
    )
    page_path = folder / "pages" / "page_001.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["title_block"] = {"x": 20, "y": 20, "w": 80, "h": 40}
    page_path.write_text(json.dumps(page), encoding="utf-8")

    def marked(tag: str, x: float, y: float, bbox: dict) -> dict:
        row = _synthetic(tag, x, y)
        row["symbol"] = "breaker"
        row["catalog"] = "1489-M1C010"
        row["needs_info"] = False
        row["bbox"] = bbox
        return row

    devices = [
        marked("B4", 40, 40, {"x": 32, "y": 32, "w": 40, "h": 20}),
        marked("B5", 40, 180, {"x": 32, "y": 174, "w": 16, "h": 12}),
    ]
    project = tmp_path / "reach.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "reach.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    sheet = Session.open(project).project.sheets[0]
    assert sheet.component("B4").library_id == "reconstruct.needs_info"
    assert sheet.component("B5").library_id == "reconstruct.needs_info"


def test_catalog_beside_its_device_is_not_swallowed_by_the_title_row(tmp_path: Path) -> None:
    """A number 4 mm past the device box is that device's catalog, not the title row."""

    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "hug",
        [
            {"text": "SHEET", "x": 40, "y": 92, "w": 16, "h": 4},
            {"text": "B8", "x": 36, "y": 74, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 54, "y": 74, "w": 20, "h": 4},
        ],
        vector=True,
    )
    page_path = folder / "pages" / "page_001.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["lines"] = []
    page_path.write_text(json.dumps(page), encoding="utf-8")
    device = _synthetic("B8", 40, 78)
    device["symbol"] = "breaker"
    device["catalog"] = "1489-M1C010"
    device["needs_info"] = False
    device["bbox"] = {"x": 30, "y": 70, "w": 20, "h": 16}
    project = tmp_path / "hug.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "hug.json", _envelope(folder, [device]))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    assert Session.open(project).project.sheets[0].component("B8").library_id == "iec.breakers.mcb_1p_16a"


def test_reach_past_the_symbol_edge_is_only_half_the_symbol(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "half",
        [
            {"text": "B9", "x": 40, "y": 40, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 65, "y": 40, "w": 24, "h": 4},
        ],
        vector=True,
    )
    device = _synthetic("B9", 40, 40)
    device["symbol"] = "breaker"
    device["catalog"] = "1489-M1C010"
    device["needs_info"] = False
    device["bbox"] = {"x": 10, "y": 20, "w": 60, "h": 40}
    project = tmp_path / "half.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "half.json", _envelope(folder, [device]))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    assert Session.open(project).project.sheets[0].component("B9").library_id == "reconstruct.needs_info"


def test_wide_agent_box_cannot_claim_a_distant_catalog(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    folder = _prep_folder(
        tmp_path / "wide",
        [
            {"text": "B6", "x": 40, "y": 40, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 80, "y": 40, "w": 24, "h": 4},
            {"text": "B7", "x": 40, "y": 140, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 54, "y": 140, "w": 24, "h": 4},
        ],
        vector=True,
    )

    def marked(tag: str, x: float, y: float, bbox: dict) -> dict:
        row = _synthetic(tag, x, y)
        row["symbol"] = "breaker"
        row["catalog"] = "1489-M1C010"
        row["needs_info"] = False
        row["bbox"] = bbox
        return row

    devices = [
        marked("B6", 40, 40, {"x": 10, "y": 20, "w": 60, "h": 40}),
        marked("B7", 40, 140, {"x": 32, "y": 132, "w": 36, "h": 16}),
    ]
    project = tmp_path / "wide.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "wide.json", _envelope(folder, devices))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    sheet = Session.open(project).project.sheets[0]
    assert sheet.component("B6").library_id == "reconstruct.needs_info"
    assert sheet.component("B7").library_id == "iec.breakers.mcb_1p_16a"


def test_timer_coil_question_names_the_kind_and_has_a_crop(tmp_path: Path) -> None:
    folder = _prep_folder(
        tmp_path / "timer",
        [
            {"text": "-K3", "x": 40, "y": 40, "w": 10, "h": 4},
            {"text": "3RT2015-1BB41", "x": 52, "y": 46, "w": 28, "h": 4},
        ],
        vector=True,
    )
    device = _synthetic("-K3", 40, 40)
    device["symbol"] = "timer coil"
    device["catalog"] = "3RT2015-1BB41"
    device["needs_info"] = False
    device["bbox"] = {"x": 30, "y": 30, "w": 40, "h": 20}
    project = tmp_path / "timer.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "timer.json", _envelope(folder, [device]))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    question = next(row for row in json.loads(applied.stdout)["questions"] if row["ref"] == "-K3" and row["kind"] == "part")
    assert "not in the library" not in question["question"]
    assert "timer coil" in question["question"]
    assert question["crop"]


def test_contact_refusal_does_not_sound_like_a_kind_mismatch() -> None:
    from panelcad.importer.agent_exchange import _catalog_block

    part = type("Part", (), {"catalog": "GS21-20P5", "library_id": "iec.drives.vfd_1k5"})()
    text = _catalog_block({"symbol": "drive", "contact": "NO", "catalog": "GS21-20P5"}, None, part)
    assert "read as a contact" in text
    assert "iec.drives" not in text


def test_declining_one_device_reopens_every_wire(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    folder = _prep_folder(tmp_path / "decline", [{"text": "SOL1", "x": 80, "y": 40, "w": 12, "h": 4}])
    session = Session.create("Decline", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="PB2", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "prep_dir": str(folder),
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "22",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "PB2.1",
                "wire_to": "CR4.1",
                "bbox": {"x": 70, "y": 30, "w": 16, "h": 10},
            }
        ],
    }
    path = tmp_path / "decline.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    wire = next(row for row in questions_for(session) if row["ref"] == "22")
    added = apply_answer(session, wire["id"], "PB2.1 to SOL1.1")
    assert added["applied"] is True, added.get("error")
    device = next(row for row in added["questions"] if row["kind"] == "device" and row["ref"] == "SOL1")
    session = Session.open(path)
    declined = apply_answer(session, device["id"], "no, not a device")
    assert declined["applied"] is True, declined.get("error")
    sheet = Session.open(path).project.sheets[0]
    assert [comp.id for comp in sheet.components] == ["PB2"]
    assert not any(wire.frm.startswith("SOL1.") or wire.to.startswith("SOL1.") for wire in sheet.wires)
    assert any(row["ref"] == "22" and row["kind"] == "wire" for row in declined["questions"])


def test_title_block_does_not_swallow_the_drawing(tmp_path: Path) -> None:
    """Devices just above a drawn title block stay devices. The block's own cell does not."""

    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    folder = _prep_folder(
        tmp_path / "above",
        [
            {"text": "SHEET", "x": 300, "y": 250, "w": 16, "h": 4},
            {"text": "COMPANY", "x": 40, "y": 255, "w": 22, "h": 4},
            {"text": "SCALE", "x": 80, "y": 255, "w": 16, "h": 4},
            {"text": "PB9B", "x": 100, "y": 210, "w": 14, "h": 4},
            {"text": "M9", "x": 200, "y": 220, "w": 10, "h": 4},
            {"text": "HORN9", "x": 80, "y": 223, "w": 16, "h": 4},
            {"text": "RIGHT", "x": 380, "y": 250, "w": 16, "h": 4},
            {"text": "B4", "x": 100, "y": 223, "w": 8, "h": 4},
            {"text": "1489-M1C010", "x": 108, "y": 223, "w": 24, "h": 4},
        ],
        vector=True,
    )
    page_path = folder / "pages" / "page_001.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["lines"] = [
        {"x0": 20, "y0": 240, "x1": 420, "y1": 240},
        {"x0": 20, "y0": 275, "x1": 420, "y1": 275},
        {"x0": 20, "y0": 240, "x1": 20, "y1": 275},
        {"x0": 280, "y0": 240, "x1": 280, "y1": 275},
        {"x0": 360, "y0": 240, "x1": 360, "y1": 275},
        {"x0": 420, "y0": 240, "x1": 420, "y1": 275},
    ]
    page["width_mm"] = 430
    page["height_mm"] = 280
    page_path.write_text(json.dumps(page), encoding="utf-8")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["pages"][0]["width_mm"] = 430
    manifest["pages"][0]["height_mm"] = 280
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    device = _synthetic("B4", 100, 223)
    device["symbol"] = "circuit breaker"
    device["catalog"] = "1489-M1C010"
    device["needs_info"] = False
    device["bbox"] = {"x": 92, "y": 214, "w": 20, "h": 16}
    project = tmp_path / "above.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "above.json", _envelope(folder, [device]))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    sheet = Session.open(project).project.sheets[0]
    assert sheet.component("B4").library_id == "iec.breakers.mcb_1p_16a"

    session = Session.open(project)
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="X", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"]["items"] = [
        {
            "kind": "wire",
            "reason": "unresolved",
            "text": number,
            "sheet": "page-001",
            "detail": "held",
            "wire_from": "X.1",
            "wire_to": "X.1",
            "bbox": {"x": 20, "y": 30, "w": 12, "h": 8},
        }
        for number in ("22", "23", "24", "25")
    ]
    session.save(project)
    session = Session.open(project)
    for number, answer in (("22", "X.1 to PB9B.1"), ("23", "X.1 to M9.1"), ("24", "X.1 to HORN9.1")):
        question = next(row for row in questions_for(session) if row["ref"] == number)
        result = apply_answer(session, question["id"], answer)
        assert result["applied"] is True, (answer, result.get("error"))
        session = Session.open(project)
    question = next(row for row in questions_for(session) if row["ref"] == "25")
    refused = apply_answer(session, question["id"], "X.1 to RIGHT.1")
    assert refused["applied"] is False
    assert "no device RIGHT" in str(refused.get("error") or "")
    company = apply_answer(Session.open(project), question["id"], "X.1 to COMPANY.1")
    assert company["applied"] is False
    assert "no device COMPANY" in str(company.get("error") or "")


def test_scan_title_row_above_the_labels_is_refused() -> None:
    """An undrawn row joins when it is about two of its own text heights away."""

    from panelcad.importer.agent_exchange import _point_in_box, _title_label_boxes

    tokens = [
        {"text": "SHEET", "x": 60, "y": 250.0, "w": 16, "h": 2.2},
        {"text": "ACME", "x": 80, "y": 250.0, "w": 16, "h": 2.2},
        {"text": "DUPLEX", "x": 80, "y": 242.2, "w": 18, "h": 2.2},
    ]
    # Box gap is 250.0 - (242.2 + 2.2) = 5.6 mm, just over two label heights.
    page = {"width_mm": 279.4, "height_mm": 215.9, "lines": [], "token_rows": tokens}
    boxes = _title_label_boxes(tokens, page)
    assert any(_point_in_box(89, 243.3, box) for box in boxes)
    assert not any(_point_in_box(89, 220, box) for box in boxes)


def test_f1_verticals_keep_the_title_row_and_not_the_rung(tmp_path: Path) -> None:
    """Outer sides set the top. The shorter middle divider only splits cells."""

    from panelcad.api.session import Session
    from panelcad.importer.agent_exchange import _point_in_box, _title_label_boxes

    lines = [
        {"x0": 282.2, "y0": 246.9, "x1": 282.2, "y1": 275.2},
        {"x0": 352.8, "y0": 257.5, "x1": 352.8, "y1": 275.2},
        {"x0": 423.3, "y0": 246.9, "x1": 423.3, "y1": 275.2},
    ]
    tokens = [
        {"text": "SHEET", "x": 360, "y": 260, "w": 16, "h": 3},
        {"text": "DUPLEX", "x": 300, "y": 253, "w": 20, "h": 3},
        {"text": "24VDC", "x": 390, "y": 253, "w": 16, "h": 3},
    ]
    page = {"width_mm": 431.8, "height_mm": 279.4, "lines": lines, "token_rows": tokens}
    boxes = _title_label_boxes(tokens, page)
    assert any(_point_in_box(310, 254.5, box) for box in boxes)
    assert any(_point_in_box(398, 254.5, box) for box in boxes)
    assert not any(_point_in_box(310, 230, box) for box in boxes)

    folder = _prep_folder(
        tmp_path / "f3",
        [
            {"text": "SHEET", "x": 360, "y": 260, "w": 16, "h": 3},
            {"text": "DUPLEX", "x": 300, "y": 253, "w": 20, "h": 3},
            {"text": "B4", "x": 300, "y": 224, "w": 8, "h": 3},
            {"text": "1489-M1C010", "x": 312, "y": 224, "w": 24, "h": 3},
            {"text": "1489-M1C010", "x": 335, "y": 253, "w": 24, "h": 3},
        ],
        vector=True,
    )
    body_path = folder / "pages" / "page_001.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    body["lines"] = lines
    body["width_mm"] = 431.8
    body["height_mm"] = 279.4
    body_path.write_text(json.dumps(body), encoding="utf-8")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["pages"][0]["width_mm"] = 431.8
    manifest["pages"][0]["height_mm"] = 279.4
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    device = _synthetic("B4", 300, 226)
    device["symbol"] = "circuit breaker"
    device["catalog"] = "1489-M1C010"
    device["needs_info"] = False
    device["bbox"] = {"x": 292, "y": 216, "w": 20, "h": 16}
    project = tmp_path / "f3.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "f3.json", _envelope(folder, [device]))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout + applied.stderr
    assert Session.open(project).project.sheets[0].component("B4").library_id == "iec.breakers.mcb_1p_16a"


def test_template_bottom_row_is_inside_the_drawn_block() -> None:
    """Rungs above the block must not crowd its own lines out of the box search."""

    from panelcad.importer.agent_exchange import _point_in_box, _title_label_boxes

    lines = [{"x0": 10, "y0": y, "x1": 400, "y1": y} for y in range(20, 240, 8)]
    lines.extend(
        [
            {"x0": 10, "y0": 249, "x1": 400, "y1": 249},
            {"x0": 10, "y0": 273, "x1": 400, "y1": 273},
            {"x0": 10, "y0": 289, "x1": 400, "y1": 289},
            {"x0": 10, "y0": 249, "x1": 10, "y1": 289},
            {"x0": 400, "y0": 249, "x1": 400, "y1": 289},
        ]
    )
    tokens = [
        {"text": "SHEET", "x": 300, "y": 260, "w": 16, "h": 3},
        {"text": "QA", "x": 40, "y": 287, "w": 8, "h": 3},
        {"text": "TB", "x": 80, "y": 287, "w": 8, "h": 3},
    ]
    page = {"width_mm": 420, "height_mm": 300, "lines": lines, "token_rows": tokens}
    boxes = _title_label_boxes(tokens, page)
    assert any(_point_in_box(44, 288.5, box) for box in boxes)
    assert any(_point_in_box(84, 288.5, box) for box in boxes)


def test_template_strip_inner_dividers_keep_the_outer_edges(tmp_path: Path) -> None:
    """Cell walls inside a wide title strip must not crowd out the strip's sides."""

    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    inners = [27.0 + index * (379.0 - 27.0) / 24.0 for index in range(25)]
    lines = [
        {"x0": 8, "y0": y, "x1": 412, "y1": y}
        for y in (249, 273, 289)
    ]
    lines.extend({"x0": 10, "y0": y, "x1": 400, "y1": y} for y in range(20, 240, 8))
    for x in (8.0, 9.8, 390.0, 412.0, *inners):
        lines.append({"x0": x, "y0": 249, "x1": x, "y1": 289})
    folder = _prep_folder(
        tmp_path / "strip",
        [
            {"text": "COMPANY", "x": 26, "y": 255, "w": 24, "h": 4},
            {"text": "SHEET", "x": 160, "y": 260, "w": 16, "h": 4},
            {"text": "TITLE", "x": 250, "y": 255, "w": 16, "h": 4},
            {"text": "SCALE", "x": 348, "y": 270, "w": 22, "h": 4},
            {"text": "QA", "x": 12, "y": 276, "w": 8, "h": 4},
            {"text": "TB", "x": 80, "y": 280, "w": 8, "h": 4},
            {"text": "CHK", "x": 200, "y": 280, "w": 10, "h": 4},
            {"text": "APP", "x": 394, "y": 276, "w": 10, "h": 4},
            {"text": "CR1", "x": 100, "y": 200, "w": 10, "h": 4},
        ],
    )
    page_path = folder / "pages" / "page_001.json"
    page = json.loads(page_path.read_text(encoding="utf-8"))
    page["lines"] = lines
    page["width_mm"] = 431.8
    page["height_mm"] = 300.0
    page_path.write_text(json.dumps(page), encoding="utf-8")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["pages"][0]["width_mm"] = 431.8
    manifest["pages"][0]["height_mm"] = 300.0
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    session = Session.create("Strip", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="X", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "prep_dir": str(folder),
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "22",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "X.1",
                "wire_to": "X.1",
                "bbox": {"x": 20, "y": 30, "w": 12, "h": 8},
            }
        ],
    }
    path = tmp_path / "strip.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    question = next(row for row in questions_for(session) if row["ref"] == "22")
    for tag in ("QA", "TB", "CHK", "APP"):
        refused = apply_answer(session, question["id"], f"X.1 to {tag}.1")
        assert refused["applied"] is False, tag
        assert f"no device {tag}" in str(refused.get("error") or "")
        session = Session.open(path)
        question = next(row for row in questions_for(session) if row["ref"] == "22")
    accepted = apply_answer(session, question["id"], "X.1 to CR1.1")
    assert accepted["applied"] is True, accepted.get("error")
    assert "CR1" in [comp.id for comp in Session.open(path).project.sheets[0].components]


def test_title_boxes_are_computed_once_and_the_search_stays_fast(tmp_path: Path, monkeypatch) -> None:
    """One wire answer searches each prep page once, and a dense page stays cheap."""

    import time

    from panelcad.api.session import Session
    from panelcad.importer import agent_exchange
    from panelcad.importer.agent_exchange import _point_in_box, _title_label_boxes
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    folder = tmp_path / "cached"
    pages = folder / "pages"
    pages.mkdir(parents=True)
    manifest_pages = []
    for index in range(1, 4):
        name = f"page_{index:03d}"
        body = {
            "id": name,
            "width_mm": 215.9,
            "height_mm": 279.4,
            "text": [
                {"text": "SHEET", "x": 160, "y": 250, "w": 16, "h": 4},
                {"text": "SOL9", "x": 80, "y": 40, "w": 12, "h": 4},
            ],
            "lines": [
                {"x0": 20, "y0": 80, "x1": 180, "y1": 80},
                {"x0": 150, "y0": 240, "x1": 210, "y1": 240},
                {"x0": 150, "y0": 270, "x1": 210, "y1": 270},
                {"x0": 150, "y0": 240, "x1": 150, "y1": 270},
                {"x0": 210, "y0": 240, "x1": 210, "y1": 270},
            ],
        }
        (pages / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")
        manifest_pages.append(
            {
                "id": name,
                "image": f"pages/{name}.png",
                "width_mm": 215.9,
                "height_mm": 279.4,
                "text": f"pages/{name}.json",
                "tiles": [],
            }
        )
    (folder / "manifest.json").write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "pages": manifest_pages}),
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    real = agent_exchange._compute_title_label_boxes

    def _count(tokens, page):
        path = str(page.get("_source_path") or "")
        if path:
            counts[path] = counts.get(path, 0) + 1
        return real(tokens, page)

    monkeypatch.setattr(agent_exchange, "_compute_title_label_boxes", _count)
    session = Session.create("Cached", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1"}
    session.place(NEEDS_INFO_LIBRARY_ID, ref="PB2", x=30, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "prep_dir": str(folder),
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "1062",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "PB2.1",
                "wire_to": "SOL9.1",
                "bbox": {"x": 70, "y": 30, "w": 16, "h": 10},
            }
        ],
    }
    path = tmp_path / "cached.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    question = next(row for row in questions_for(session) if row["ref"] == "1062")
    result = apply_answer(session, question["id"], "PB2.1 to SOL9.1")
    assert result["applied"] is True, result.get("error")
    assert len(counts) == 3
    assert set(counts.values()) == {1}

    lines = [{"x0": 0.0, "y0": float(i), "x1": 400.0, "y1": float(i)} for i in range(200)]
    lines.extend({"x0": float(i), "y0": 0.0, "x1": float(i), "y1": 280.0} for i in range(200))
    dense_tokens = [{"text": "SHEET", "x": 300, "y": 250, "w": 16, "h": 3}]
    dense = {"width_mm": 420, "height_mm": 300, "lines": lines, "token_rows": dense_tokens}
    started = time.perf_counter()
    _title_label_boxes(dense_tokens, dense)
    assert time.perf_counter() - started < 1.0

    f1_lines = [
        {"x0": 282.2, "y0": 246.9, "x1": 282.2, "y1": 275.2},
        {"x0": 352.8, "y0": 257.5, "x1": 352.8, "y1": 275.2},
        {"x0": 423.3, "y0": 246.9, "x1": 423.3, "y1": 275.2},
    ]
    f1_tokens = [
        {"text": "SHEET", "x": 360, "y": 260, "w": 16, "h": 3},
        {"text": "DUPLEX", "x": 300, "y": 253, "w": 20, "h": 3},
        {"text": "24VDC", "x": 390, "y": 253, "w": 16, "h": 3},
    ]
    f1 = _title_label_boxes(
        f1_tokens, {"width_mm": 431.8, "height_mm": 279.4, "lines": f1_lines, "token_rows": f1_tokens}
    )
    assert len(f1) == 1
    assert abs(f1[0]["x"] - 282.2) < 0.05
    assert abs(f1[0]["y"] - 246.9) < 0.05
    assert abs(f1[0]["w"] - 141.1) < 0.05
    assert abs(f1[0]["h"] - 32.5) < 0.05

    strip = [{"x0": 10, "y0": y, "x1": 400, "y1": y} for y in range(20, 240, 8)]
    strip.extend(
        [
            {"x0": 10, "y0": 249, "x1": 400, "y1": 249},
            {"x0": 10, "y0": 273, "x1": 400, "y1": 273},
            {"x0": 10, "y0": 289, "x1": 400, "y1": 289},
            {"x0": 10, "y0": 249, "x1": 10, "y1": 289},
            {"x0": 400, "y0": 249, "x1": 400, "y1": 289},
        ]
    )
    strip_tokens = [
        {"text": "SHEET", "x": 300, "y": 260, "w": 16, "h": 3},
        {"text": "QA", "x": 40, "y": 287, "w": 8, "h": 3},
        {"text": "TB", "x": 80, "y": 287, "w": 8, "h": 3},
    ]
    strip_boxes = _title_label_boxes(
        strip_tokens, {"width_mm": 420, "height_mm": 300, "lines": strip, "token_rows": strip_tokens}
    )
    assert len(strip_boxes) == 1
    assert abs(strip_boxes[0]["y"] - 249.0) < 0.05
    assert abs(strip_boxes[0]["y"] + strip_boxes[0]["h"] - 289.0) < 0.05
    assert not any(_point_in_box(50, 232, box) for box in strip_boxes)


def test_declining_a_shared_device_reopens_both_wires(tmp_path: Path) -> None:
    from panelcad.api.session import Session
    from panelcad.importer.agent_questions import apply_answer, questions_for
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    folder = _prep_folder(tmp_path / "shared", [{"text": "SOL1", "x": 90, "y": 40, "w": 12, "h": 4}])
    session = Session.create("Shared", sheet_id="page-001", title="page")
    props = {"reconstruct_needs_info": "yes", "bom": "no", "print_import_pins": "1,2"}
    for ref, x in (("CR21", 30.0), ("M10", 60.0)):
        session.place(NEEDS_INFO_LIBRARY_ID, ref=ref, x=x, y=40, snap=False, allow_needs_info=True, properties=dict(props))
    session.project.properties["print_import"] = {
        "prep_dir": str(folder),
        "items": [
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "1062",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "CR21.1",
                "wire_to": "M10.1",
                "bbox": {"x": 70, "y": 30, "w": 16, "h": 10},
            },
            {
                "kind": "wire",
                "reason": "unresolved",
                "text": "1031",
                "sheet": "page-001",
                "detail": "held",
                "wire_from": "M10.1",
                "wire_to": "CR21.2",
                "bbox": {"x": 70, "y": 48, "w": 16, "h": 10},
            },
        ],
    }
    path = tmp_path / "shared.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    first = next(row for row in questions_for(session) if row["ref"] == "1062")
    added = apply_answer(session, first["id"], "CR21.1 to SOL1.1")
    assert added["applied"] is True, added.get("error")
    session = Session.open(path)
    second = next(row for row in questions_for(session) if row["ref"] == "1031")
    added = apply_answer(session, second["id"], "M10.1 to SOL1.2")
    assert added["applied"] is True, added.get("error")
    device = next(row for row in added["questions"] if row["kind"] == "device" and row["ref"] == "SOL1")
    session = Session.open(path)
    declined = apply_answer(session, device["id"], "no")
    assert declined["applied"] is True, declined.get("error")
    sheet = Session.open(path).project.sheets[0]
    assert "SOL1" not in [comp.id for comp in sheet.components]
    assert not any("SOL1." in wire.frm or "SOL1." in wire.to for wire in sheet.wires)
    open_refs = {row["ref"] for row in declined["questions"] if row["kind"] == "wire"}
    assert {"1062", "1031"} <= open_refs


def test_answer_accepts_a_leading_dash(tmp_path: Path) -> None:
    from panelcad.api.session import Session

    folder = _prep_folder(tmp_path / "dash", [{"text": "QA", "x": 40, "y": 40, "w": 8, "h": 4}], vector=True)
    project = tmp_path / "dash.pcad.yaml"
    applied = runner.invoke(
        app,
        ["import", "apply", str(_write_json(tmp_path / "dash.json", _envelope(folder, [_synthetic("X2", 40, 80)]))), "--out", str(project)],
    )
    assert applied.exit_code == 0, applied.stdout
    question = next(row for row in json.loads(applied.stdout)["questions"] if row["ref"] == "X2")
    answered = runner.invoke(app, ["import", "answer", str(project), question["id"], "-X2"])
    assert answered.exit_code == 0, answered.stdout + answered.stderr
    assert "No such option" not in answered.output
    ids = [comp.id for comp in Session.open(project).project.sheets[0].components]
    assert "-X2" in ids


def _prep_folder(folder: Path, tokens: list[dict], *, vector: bool = False) -> Path:
    pages = folder / "pages"
    pages.mkdir(parents=True)
    page = {
        "id": "page_001",
        "width_mm": 215.9,
        "height_mm": 279.4,
        "vector": vector,
        "ocr": "tesseract",
        "page_text": "",
        "text": tokens,
        "lines": [{"x0": 20, "y0": 80, "x1": 180, "y1": 80}],
    }
    (pages / "page_001.json").write_text(json.dumps(page), encoding="utf-8")
    from PIL import Image

    Image.new("RGB", (400, 520), (255, 255, 255)).save(pages / "page_001.png")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "pages": [
            {
                "id": "page_001",
                "image": "pages/page_001.png",
                "width_mm": 215.9,
                "height_mm": 279.4,
                "text": "pages/page_001.json",
                "tiles": [],
            }
        ],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _synthetic(tag: str, x: float, y: float) -> dict:
    return {
        "tag": tag,
        "symbol": "relay",
        "contact": "",
        "x": x,
        "y": y,
        "sheet": "page_001",
        "rung": 0,
        "confidence": 0.9,
        "needs_info": True,
        "manufacturer": "",
        "catalog": "",
        "terminals": ["A1"],
        "bbox": {"x": x - 6, "y": y - 6, "w": 14, "h": 12},
    }


def _envelope(folder: Path, devices: list[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "prep_dir": str(folder),
        "project_name": "Synthetic",
        "title_block": {
            "company": "",
            "project": "",
            "title": "",
            "drawing_number": "",
            "rev": "",
            "sheet_number": "",
            "sheet_count": "",
            "customer": "",
            "location": "",
            "drawn_by": "",
            "date": "",
            "scale": "",
            "standard": "",
            "confidence": 0.2,
            "needs_info": True,
            "unreadable_fields": [],
        },
        "devices": devices,
        "terminals": [],
        "wires": [],
    }


def _bare(tag: str, token: dict, symbol: str, contact: str = "") -> dict:
    return {
        "tag": tag,
        "symbol": symbol,
        "contact": contact,
        "x": float(token["x"]),
        "y": float(token["y"]),
        "sheet": "page_001",
        "rung": 0,
        "confidence": 0.9,
        "needs_info": False,
        "manufacturer": "",
        "catalog": "",
        "terminals": ["A1", "A2"],
    }
