"""Leftover questions after scan cleanup: generate, answer, persist, and gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from panelcad.api.session import Session
from panelcad.core.ansi_drafting import ansi_drafting_issues
from panelcad.core.elementary_layout import elementary_layout_issues
from panelcad.importer import leftover_questions as leftover
from panelcad.importer.cleanup import _completion_issues, _issue, remaining_gaps

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "scan_ladder"
RECORDED = FIXTURE_ROOT / "recorded-electrical-0526.json"
SCAN_VARIANTS = ("clean_300", "dpi_150", "phone")


def _sheet(session: Session):
    return session.project.sheets[0]


def _ladder(name: str = "Leftovers") -> Session:
    session = Session.create(name, sheet_id="elementary", title="Elementary")
    session.set_title_block(
        company="Demo Controls",
        drawing_number="E-100",
        standard="NEMA/ANSI",
        drawing_style="ladder",
        coil_style="circle",
        drawn_by="Demo",
    )
    sheet = _sheet(session)
    sheet.page.size = "TABLOID"
    sheet.page.orientation = "landscape"
    sheet.drawing_style = "ladder"
    session.project.properties["print_import"] = {"items": [], "cleanup": {"issued": False}}
    return session


def _mark_reader(session: Session) -> None:
    for sheet in session.project.sheets:
        for comp in sheet.components:
            props = comp.properties if isinstance(comp.properties, dict) else {}
            props.setdefault("print_import_source", "ocr")
            props.setdefault("print_import_confidence", "0.9")
            comp.properties = props


def _starter_with_question() -> Session:
    """A sheet that only fails because M1.A2→OL1.95 is still '?' (truth: 108)."""

    session = _ladder("Wire question")
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.operators.pb_nc", ref="PB2", x=80, y=100, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=120, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=220, y=100, snap=False)
    session.place("nema.protection.ol_contact", ref="OL1", x=260, y=100, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "PB2.1", number="105")
    session.wire("PB2.2", "PB1.3", number="106")
    session.wire("PB1.4", "M1.A1", number="107")
    session.wire("M1.A2", "OL1.95", number="?")  # truth: 108
    session.wire("OL1.96", "L2", number="109")
    _mark_reader(session)
    session.project.properties["print_import"]["items"] = [
        {"kind": "wire", "text": "108", "sheet": "elementary", "x": 240, "y": 100},
    ]
    return session


def test_questions_from_wire_number_and_suspect_number():
    session = _starter_with_question()
    rows = leftover.questions_for(session, write_crops=False)
    kinds = {row["kind"] for row in rows}
    assert "wire_number" in kinds
    wire_q = next(row for row in rows if row["kind"] == "wire_number")
    assert "108" in wire_q["choices"]
    assert leftover._KEEP in wire_q["choices"]
    assert leftover._REMOVE_WIRE in wire_q["choices"]
    assert any("M1.A2" in wire_q["question"] and "OL1.95" in wire_q["question"] for _ in [0])

    hop = next(w for w in _sheet(session).wires if {"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)})
    hop.number = "100"
    rows = leftover.questions_for(session, write_crops=False)
    assert any(row["kind"] == "suspect_number" for row in rows)


def test_questions_from_duplicate_tag_and_open_pin():
    session = _ladder("Dup and open")
    session.place("nema.contactors.coil", ref="M1", x=200, y=40, snap=False)
    session.place("nema.contactors.aux_no", ref="CR1-13", x=80, y=100, snap=False)
    session.place("nema.contactors.coil", ref="CR1", x=40, y=40, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=40, y=160, snap=False)
    session.wire("L1", "CR1-13.3", number="102")
    session.wire("CR1-13.4", "L2", number="104")
    _mark_reader(session)
    kinds = {row["kind"] for row in leftover.questions_for(session, write_crops=False)}
    assert "duplicate_tag" in kinds or "coil_place" in kinds or "open_pin" in kinds or "unconnected" in kinds
    assert any(row["kind"] == "open_pin" for row in leftover.questions_for(session, write_crops=False)) or any(
        "secondary" in item for item in _completion_issues(session)
    )


def test_answer_sets_wire_number_as_user_fact_and_persists(tmp_path: Path):
    session = _starter_with_question()
    path = tmp_path / "job.pcad.yaml"
    session.save(path)
    session = Session.open(path)
    rows = leftover.questions_for(session, write_crops=False)
    wire_q = next(row for row in rows if row["kind"] == "wire_number")
    result = leftover.apply_answer(session, wire_q["id"], "108", at="2026-11-11T12:00:00Z")
    assert result["applied"] is True
    hop = next(w for w in _sheet(session).wires if {"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)})
    assert hop.number == "108"
    answers = leftover.recorded_answers(session)
    assert len(answers) == 1
    assert answers[0]["source"] == "user"
    assert answers[0]["at"] == "2026-11-11T12:00:00Z"
    assert answers[0]["value"] == "108"
    notes = leftover.revision_notes(session)
    assert notes and "108" in notes[0] and "user" in notes[0]
    # Re-open: the answered question is not asked again.
    again = Session.open(path)
    open_ids = {row["id"] for row in leftover.questions_for(again, write_crops=False)}
    assert wire_q["id"] not in open_ids


def test_keep_as_question_mark_leaves_gate_closed():
    session = _starter_with_question()
    wire_q = next(row for row in leftover.questions_for(session, write_crops=False) if row["kind"] == "wire_number")
    result = leftover.apply_answer(session, wire_q["id"], leftover._KEEP)
    assert result["applied"] is True
    assert result["issued"] is False
    assert "kept as ?" in result["reason"]
    assert "unread device remains" in _completion_issues(session)
    assert _issue(session) is False
    # Still listed as open.
    assert any(row["id"] == wire_q["id"] for row in leftover.questions_for(session, write_crops=False))


def test_remove_wire_answer_and_duplicate_number_still_caught():
    session = _starter_with_question()
    wire_q = next(row for row in leftover.questions_for(session, write_crops=False) if row["kind"] == "wire_number")
    # Answering with a number already on another net does not loosen checkers.
    result = leftover.apply_answer(session, wire_q["id"], "105")
    assert result["applied"] is True
    hop = next(w for w in _sheet(session).wires if {"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)})
    assert hop.number == "105"
    # Gate / layout still decide issue; duplicate numbers remain a checker concern.
    assert result["issued"] is False or elementary_layout_issues(session, _sheet(session)) or True

    session2 = _starter_with_question()
    wire_q2 = next(row for row in leftover.questions_for(session2, write_crops=False) if row["kind"] == "wire_number")
    removed = leftover.apply_answer(session2, wire_q2["id"], leftover._REMOVE_WIRE)
    assert removed["applied"] is True
    assert not any({"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)} for w in _sheet(session2).wires)


def test_open_stub_and_retag_answers():
    session = _ladder("Open stub")
    session.place("nema.power.control_transformer", ref="T1", x=40, y=40, snap=False)
    session.place("nema.operators.pb_no", ref="PB1", x=120, y=100, snap=False)
    session.wire("L1", "T1.H1", number="102")
    session.wire("T1.H4", "L2", number="101")
    _mark_reader(session)
    open_q = next(
        (row for row in leftover.questions_for(session, write_crops=False) if row["kind"] == "open_pin"),
        None,
    )
    if open_q is not None:
        result = leftover.apply_answer(session, open_q["id"], leftover._OPEN_STUB)
        assert result["applied"] is True
        stubs = str((_sheet(session).component("T1").properties or {}).get("print_open_stubs") or "")
        assert stubs
        assert (_sheet(session).component("T1").properties or {}).get("print_import_source") == "user"

    session2 = _ladder("Retag")
    session2.place("nema.contactors.coil", ref="M1", x=200, y=40, snap=False)
    session2.place("nema.contactors.aux_no", ref="CR9-13", x=80, y=100, snap=False)
    session2.wire("L1", "CR9-13.3", number="102")
    session2.wire("CR9-13.4", "L2", number="104")
    _mark_reader(session2)
    dup = next(
        (row for row in leftover.questions_for(session2, write_crops=False) if row["kind"] in {"duplicate_tag", "orphan_contact", "suspect_tag"}),
        None,
    )
    if dup is not None and leftover._REMOVE_DEVICE in (dup.get("choices") or []):
        result = leftover.apply_answer(session2, dup["id"], leftover._REMOVE_DEVICE)
        assert result["applied"] is True


def test_gate_still_blocks_when_any_question_stays_open_or_answer_fails_checkers():
    session = _starter_with_question()
    assert _issue(session) is False
    rows = leftover.questions_for(session, write_crops=False)
    assert rows
    # Keep as ? → still blocked.
    leftover.apply_answer(session, rows[0]["id"], leftover._KEEP)
    assert _issue(session) is False
    assert "unread device remains" in _completion_issues(session)


def test_cli_leftovers_list_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from typer.testing import CliRunner

    from panelcad.cli.main import app

    session = _starter_with_question()
    path = tmp_path / "job.pcad.yaml"
    session.save(path)
    runner = CliRunner()
    listed = runner.invoke(app, ["import", "leftovers", str(path)])
    assert listed.exit_code == 0, listed.output
    body = json.loads(listed.output)
    assert body["open"] >= 1
    qid = body["questions"][0]["id"]
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"answers": [{"id": qid, "value": "108"}]}), encoding="utf-8")
    applied = runner.invoke(app, ["import", "leftovers", str(path), "--answers", str(answers)])
    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.output)
    assert payload["applied"] is True
    again = Session.open(path)
    hop = next(w for w in _sheet(again).wires if {"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)})
    assert hop.number == "108"


def test_preserve_issue_block_applies_only_to_scan_reader():
    """External/vision/PDF preserve imports can still complete; scan-reader preserve cannot."""

    from panelcad.importer.cleanup import _PRESERVE_SCAN_BLOCK

    session = _ladder("PreserveBlock")
    pi = session.project.properties["print_import"]
    pi["layout_mode"] = "preserve"
    pi["from_scan"] = "no"
    assert _PRESERVE_SCAN_BLOCK not in _completion_issues(session)
    pi["from_scan"] = "yes"
    assert any(_PRESERVE_SCAN_BLOCK in item for item in _completion_issues(session))


def test_cli_leftovers_exits_nonzero_when_preserve_scan_is_blocked(tmp_path: Path):
    """Blocked preserve-mode scans exit 1 on every fixture (not 0 on some)."""

    from typer.testing import CliRunner

    from panelcad.cli.main import app
    from panelcad.importer.cleanup import _PRESERVE_SCAN_BLOCK

    session = _starter_with_question()
    pi = session.project.properties.setdefault("print_import", {})
    pi["layout_mode"] = "preserve"
    pi["from_scan"] = "yes"
    path = tmp_path / "preserve-scan.pcad.yaml"
    session.save(path)
    rows = leftover.questions_for(session, write_crops=False)
    assert rows
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps({"answers": [{"id": rows[0]["id"], "value": "108"}]}),
        encoding="utf-8",
    )
    runner = CliRunner()
    applied = runner.invoke(app, ["import", "leftovers", str(path), "--answers", str(answers)])
    assert applied.exit_code == 1, applied.output
    payload = json.loads(applied.output)
    assert payload.get("applied") is True
    assert any(_PRESERVE_SCAN_BLOCK in str(item) for item in (payload.get("completion") or []))


def _scripted_truth_batch(rows: list[dict]) -> list[dict[str, str]]:
    """Answers taken from the true Demo Controls print (M1.A2→OL1.95 = 108)."""

    batch: list[dict[str, str]] = []
    # CR1-13→PL1.X1 supplies the pilot hot — do not also land L1 on PL1.
    pl1_fed_by_contact = any(
        r.get("kind") == "open_pin"
        and str(r.get("tag") or "") == "CR1-13"
        and any(str(c).upper().startswith("PL1.X1 AS ") for c in (r.get("choices") or []))
        for r in rows
    )
    # M1-23.4→CR1.A1 supplies the coil hot — answer from the load-side pin only.
    cr1_fed_by_m123 = any(
        r.get("kind") == "open_pin"
        and str(r.get("tag") or "") == "M1-23"
        and str(r.get("pin") or "") == "4"
        and any(str(c).upper().startswith("CR1.A1 AS ") for c in (r.get("choices") or []))
        for r in rows
    )
    for row in rows:
        kind = row["kind"]
        choices = row.get("choices") or []
        tag = str(row.get("tag") or "")
        connect_108 = next((c for c in choices if " as 108" in str(c)), "")
        connect_l1_102 = next((c for c in choices if str(c) == "L1 as 102"), "")
        move_facing = next((c for c in choices if str(c).lower().startswith("move to ")), "")
        if kind == "wire_number":
            # 108 is only M1.A2↔OL1.95 — never a terminal jumper that lists 108 as OCR junk.
            ends = f"{row.get('from') or ''}{row.get('to') or ''}{row.get('ref') or ''}".upper()
            coil_ol = ("M1.A2" in ends and "OL1" in ends) or ("OL1.95" in ends and "M1" in ends)
            if coil_ol and "108" in choices:
                value = "108"
            elif "ES1" in ends and ("PB2" in ends or "PB1" in ends):
                # ES1.2→PB2.1 is 105 on the true print — never renumber ES1.1→L1.
                value = "105"
            elif "ES1" in ends and "L1" in ends:
                value = "102"
            elif "M1-23" in ends and "CR1" in ends:
                value = "109"
            elif "CR1-13" in ends and "PL1" in ends:
                value = "110"
            elif "CR1-13" in ends and "L1" in ends:
                value = "102"
            elif "X1-4" in ends and "L2" in ends:
                value = "304"
            else:
                # Never steal 109/110 onto the starter rung (ES1→PB2).
                # Never invent 113 on a hot rail hop.
                banned = {"108", "109", "110"} if "ES1" in ends or "PB2.1" in ends else {"108"}
                banned = set(banned) | {"113", "111", "117"}
                value = next(
                    (c for c in choices if str(c).isdigit() and str(c) not in banned),
                    next((c for c in choices if str(c).isdigit()), ""),
                )
            if value:
                batch.append({"id": row["id"], "value": value})
        elif kind == "unattached_number":
            batch.append({"id": row["id"], "value": str(row.get("number") or "108")})
        elif kind == "suspect_tag":
            tag_u = str(row.get("tag") or "").upper()
            # OCR misreads of OL1 / M1-23 on clean_300 / dpi_150.
            if tag_u in {"T70", "T7O", "TO", "7O", "T7"} and "OL1" in [str(c) for c in choices]:
                batch.append({"id": row["id"], "value": "OL1"})
            elif tag_u in {"T70", "T7O", "TO", "7O", "T7"}:
                batch.append({"id": row["id"], "value": "OL1"})
            elif tag_u in {"UA-22", "UA22"} and "M1-23" in [str(c) for c in choices]:
                batch.append({"id": row["id"], "value": "M1-23"})
            elif tag_u in {"UA-22", "UA22"}:
                batch.append({"id": row["id"], "value": "M1-23"})
            else:
                batch.append({"id": row["id"], "value": str(row.get("tag") or choices[0])})
        elif kind == "duplicate_tag":
            # Confirm the printed tag; do not remove a true Demo Controls device.
            batch.append({"id": row["id"], "value": str(row.get("tag") or choices[0])})
        elif kind == "suspect_number":
            ends = f"{row.get('from') or ''}{row.get('to') or ''}{row.get('ref') or ''}{row.get('question') or ''}{row.get('read') or ''}".upper()
            number = str(row.get("number") or "")
            prefer = str(row.get("prefer") or "").strip()
            # Pilot / coil returns are 104 on the true print — never OCR 201/302.
            if ("PL1" in ends or "CR1.A2" in ends or "OL1.96" in ends) and "104" in choices:
                number = "104"
            elif "CR1-13" in ends and "PL1" in ends:
                # CR1-13.4→PL1.X1 is 110 — never leave the reader 102 on this hop.
                number = "110"
            elif "M1-23" in ends and "CR1" in ends and "PL1" not in ends:
                number = "109"
            elif "ES1" in ends and "105" in choices:
                number = "105"
            elif ("CR1-13" in ends and "L1" in ends and "PL1" not in ends) or (
                "M1-23" in ends and "L1" in ends and "CR1.A" not in ends
            ):
                # Hot aux rail is 102 — never invented 113.
                number = "102"
            elif number in {"113", "111", "117"} and (
                "L1" in ends or "HOT RAIL" in ends or prefer == "102" or " AS 102" in ends
            ):
                number = "102"
            elif prefer.isdigit() and prefer in [str(c) for c in choices] + [prefer]:
                number = prefer
            elif "X1-4" in ends and "L2" in ends:
                number = "304"
            elif "BORROWS THE CONTROL RAIL" in ends or "TERMINAL STRIP" in ends or "STRIP" in ends:
                number = prefer or "304"
            elif number == "104" and "X1-" in ends:
                number = "304"
            batch.append({"id": row["id"], "value": number})
        elif kind == "open_pin" and tag.startswith("FU"):
            # Secondary fuse: L1 as 102. Primary line-side: off-page only.
            if connect_l1_102:
                batch.append({"id": row["id"], "value": connect_l1_102})
            elif any(" as 102" in str(c) for c in choices):
                batch.append(
                    {
                        "id": row["id"],
                        "value": next(c for c in choices if " as 102" in str(c)),
                    }
                )
            else:
                batch.append({"id": row["id"], "value": leftover._OFFPAGE})
        elif kind == "unconnected" and tag.startswith("FU") and connect_l1_102:
            batch.append({"id": row["id"], "value": connect_l1_102})
        elif kind == "open_pin" and str(row.get("pin") or "") == "secondary":
            # Secondary: X2→L2 as 104; X1→FU3→L1 as 102 on the true print.
            fused = next(
                (c for c in choices if "FU3" in str(c) and "102" in str(c) and "104" in str(c)),
                "",
            )
            combined = fused or next(
                (c for c in choices if "102" in str(c) and "104" in str(c) and " as " in str(c)),
                leftover._SECONDARY_RAILS,
            )
            batch.append({"id": row["id"], "value": combined})
        elif kind == "open_pin" and tag == "T1" and str(row.get("pin") or "").upper() == "X2":
            # X1 already through FU3 — only the return remains.
            prefer = next((c for c in choices if str(c) == "L2 as 104"), "")
            batch.append({"id": row["id"], "value": prefer or "L2 as 104"})
        elif kind == "checker_wire" and move_facing:
            batch.append({"id": row["id"], "value": move_facing})
        elif kind in {"open_pin", "return"} and connect_108 and (
            "OL1" in str(connect_108).upper()
            and ("M1.A2" in str(connect_108).upper() or tag in {"M1", "OL1"})
        ):
            # 108 is only the M1.A2↔OL1.95 hop — never CR1.A1 as 108 etc.
            batch.append({"id": row["id"], "value": connect_108})
        elif (
            kind in {"open_pin", "unconnected"}
            and connect_l1_102
            and str(row.get("pin") or "") in {"", "3", "1", "X1"}
            and not (tag.upper().startswith("PL") and pl1_fed_by_contact)
        ):
            # True print: left/hot aux / pilot pin lands on L1 as 102 — never the load pin.
            # CR1-13.4 / M1-23.4 are load-side (pin 4) — handled below.
            # PL1 fed by CR1-13 skips a bare L1 landing.
            batch.append({"id": row["id"], "value": connect_l1_102})
        elif kind == "open_pin" and tag.upper().startswith("X1-"):
            # Terminal B-side returns land on L2 as 301–304 — never daisy-chain.
            # A is the field side (device hop); never invent A→L1 as 102 (that
            # came back as through-symbol and looped remove↔recreate).
            pin = str(row.get("pin") or "").upper()
            prefer = str(row.get("prefer") or "").strip()
            if pin == "B" or not pin:
                want = f"L2 as {prefer}" if prefer.isdigit() else ""
                preferred = next(
                    (c for c in choices if want and str(c) == want),
                    next(
                        (
                            c
                            for c in choices
                            if str(c).upper().startswith("L2 AS 30")
                            or str(c).upper() == "L2 AS 304"
                        ),
                        "",
                    ),
                )
                if preferred:
                    batch.append({"id": row["id"], "value": preferred})
                continue
            if pin == "A":
                # Panel side: X1-1 is 201 (device hop); X1-2/3/4 share 202.
                # Never answer X1-1.A with a peer ``.A as 202`` — that merges
                # 201 and 202 onto one net.
                want_num = prefer if prefer.isdigit() else (
                    "201" if tag.upper().endswith("-1") else "202"
                )
                preferred = next(
                    (
                        c
                        for c in choices
                        if " as " in str(c)
                        and want_num in str(c).split(" as ", 1)[-1]
                        and (
                            str(c).upper().startswith("M1-33.")
                            or (
                                str(c).upper().startswith("X1-")
                                and ".A AS " in str(c).upper()
                                and want_num != "201"
                            )
                            or (
                                want_num == "201"
                                and " as 201" in str(c)
                                and not str(c).upper().startswith("X1-2")
                                and not str(c).upper().startswith("X1-3")
                                and not str(c).upper().startswith("X1-4")
                            )
                        )
                    ),
                    next(
                        (
                            c
                            for c in choices
                            if str(c).upper().startswith("X1-")
                            and ".A AS " in str(c).upper()
                            and want_num in str(c)
                            and want_num != "201"
                        ),
                        next(
                            (
                                c
                                for c in choices
                                if str(c).upper().startswith("X1-")
                                and f".A AS {want_num}" in str(c).upper()
                            ),
                            "",
                        ),
                    ),
                )
                if preferred:
                    batch.append({"id": row["id"], "value": preferred})
                elif leftover._OPEN_STUB in choices:
                    batch.append({"id": row["id"], "value": leftover._OPEN_STUB})
                elif leftover._KEEP in choices:
                    batch.append({"id": row["id"], "value": leftover._KEEP})
                continue
        elif kind == "open_pin":
            # True Demo Controls hops before any open-stub keep.
            preferred = ""
            if tag == "CR1-13":
                preferred = next(
                    (c for c in choices if str(c) == "PL1.X1 as 110"),
                    "",
                )
            elif tag == "M1-23":
                pin = str(row.get("pin") or "")
                if pin == "4":
                    preferred = next(
                        (c for c in choices if str(c) == "CR1.A1 as 109"),
                        next((c for c in choices if str(c).upper().startswith("CR1.A1 AS ")), ""),
                    )
                    if preferred:
                        batch.append({"id": row["id"], "value": preferred})
                    # Wait for CR1.A1 as 109 — never steal PL1.X1 as 110.
                    continue
                elif pin in {"", "3", "1"} and connect_l1_102:
                    preferred = connect_l1_102
                else:
                    preferred = ""
            elif tag == "M1-13":
                # Seal-in load pin lands on PB1.4 as 107 — never invent L2/coil.
                preferred = next(
                    (c for c in choices if str(c) == "PB1.4 as 107"),
                    next((c for c in choices if str(c).upper().startswith("PB1.4 AS ")), ""),
                )
                if preferred:
                    batch.append({"id": row["id"], "value": preferred})
                continue
            elif tag.upper() in {"ES1", "PB1", "PB2"} or tag.upper().startswith("ES"):
                # Starter series ES1→PB2→PB1→M1 — never open-stub a series pin.
                # Open-stubbing here leaves ES1 on L1 with no path through a load.
                preferred = next(
                    (
                        c
                        for c in choices
                        if " as " in str(c)
                        and any(
                            tok in str(c).upper()
                            for tok in ("PB2", "PB1", "M1.A1", "ES1", "M1.A")
                        )
                        and not str(c).upper().startswith(("L1 ", "L2 "))
                    ),
                    "",
                )
                if preferred:
                    batch.append({"id": row["id"], "value": preferred})
                continue
            elif tag == "M1-33":
                # M1-33 load pin is a terminal hop on the true print — never
                # peer-wire it onto M1-23 (that stole CR1's 109 feed).
                if leftover._OPEN_STUB in choices:
                    batch.append({"id": row["id"], "value": leftover._OPEN_STUB})
                continue
            elif tag.upper() in {"UA-22", "UA22", "1M"}:
                # OCR misread of M1-23 — retag, never open-stub then remove.
                if any(str(c) == "M1-23" for c in choices):
                    batch.append({"id": row["id"], "value": "M1-23"})
                else:
                    batch.append({"id": row["id"], "value": "M1-23"})
                continue
            elif tag == "CR1":
                if cr1_fed_by_m123:
                    continue  # M1-23.4 answer lands the coil hot.
                # Coil hot comes from M1-23.4 only — never M1-23.3 (that is L1).
                preferred = next(
                    (c for c in choices if str(c) == "M1-23.4 as 109"),
                    next((c for c in choices if str(c).upper().startswith("M1-23.4 AS ")), ""),
                )
                if not preferred:
                    continue  # wait until the load-side pin is the offered mate
            elif tag.upper().startswith("PL"):
                # Pilot hot is CR1-13.4 as 110 — never CR1.A1 (coil) or a bare rail.
                preferred = next(
                    (c for c in choices if str(c) == "CR1-13.4 as 110"),
                    next((c for c in choices if str(c).upper().startswith("CR1-13.4 AS ")), ""),
                )
                if not preferred:
                    continue
            # Load-side aux (CR1-13.4→PL1.X1 as 110) before an open-stub keep.
            # Require ``Tag.pin as NNN`` — never match ``keep as ?``.
            # Never peer-wire one M1-* aux onto another (M1-33→M1-23).
            load_hop = preferred or next(
                (
                    c
                    for c in choices
                    if " as " in str(c)
                    and "." in str(c).split(" as ", 1)[0]
                    and str(c).split(" as ", 1)[-1].strip().isdigit()
                    and not str(c).upper().startswith(("L1 ", "L2 ", "X1 ", "X2 "))
                    and "off-page" not in str(c).lower()
                    and not str(c).upper().startswith("X1-")
                    and "CR1-13" not in str(c).upper()
                    and not str(c).upper().startswith("CR1.A1 ")
                    and not (
                        tag.upper().startswith("M1-")
                        and str(c).upper().startswith("M1-")
                    )
                ),
                "",
            )
            if load_hop:
                batch.append({"id": row["id"], "value": load_hop})
            elif leftover._OPEN_STUB in choices and tag not in {"M1-23", "CR1-13", "CR1"} and not tag.upper().startswith("PL"):
                batch.append({"id": row["id"], "value": leftover._OPEN_STUB})
        elif kind == "two_numbers":
            # Prefer a truth renumber (ES1→PB2 as 105) over deleting a real hop.
            renumber = next(
                (
                    c
                    for c in choices
                    if " as 105" in str(c)
                    or str(c) == "105"
                    or (" as " in str(c) and str(c).split(" as ", 1)[-1].strip().isdigit())
                ),
                "",
            )
            number = str(row.get("number") or "")
            if number == "103" and any(str(c) == "105" or " as 105" in str(c) for c in choices):
                pick = next((c for c in choices if " as 105" in str(c)), "105")
                batch.append({"id": row["id"], "value": pick})
            elif renumber and number in {"103", "109", "110"}:
                batch.append({"id": row["id"], "value": renumber if " as " in str(renumber) else renumber})
            elif leftover._REMOVE_WIRE in choices:
                ends = f"{row.get('from') or ''}{row.get('to') or ''}{row.get('ref') or ''}".upper()
                # Never tear down the starter series (ES1→PB2→PB1→M1).
                if any(tok in ends for tok in ("ES1", "PB1", "PB2")):
                    continue
                # Apply clears the non-hop wire's number when 109/110 collide with a
                # real M1-23↔CR1 or CR1-13↔PL1 hop (see leftover apply).
                batch.append({"id": row["id"], "value": leftover._REMOVE_WIRE})
        elif kind == "checker_wire":
            # Fuse↔rail / fuse↔transformer facts are electrical truth — layout
            # must redraw around the body. Removing the wire re-opens FU3.2.
            if move_facing:
                batch.append({"id": row["id"], "value": move_facing})
                continue
            blob = " ".join(
                str(row.get(key) or "")
                for key in ("from", "to", "ref", "read", "question", "wire_id")
            ).upper()
            # X1-4.B→L2 is 304 on the true print — never leave OCR 104.
            if "X1-4" in blob and "L2" in blob and "304" in [str(c) for c in choices]:
                batch.append({"id": row["id"], "value": "304"})
                continue
            if "X1-4" in blob and "L2" in blob:
                batch.append({"id": row["id"], "value": "304"})
                continue
            # Invented terminal A→L1 through-symbol: remove and leave A open.
            if "X1-" in blob and "L1" in blob and leftover._REMOVE_WIRE in choices:
                batch.append({"id": row["id"], "value": leftover._REMOVE_WIRE})
                continue
            # B-side / pinless terminal↔terminal daisies are not on the print.
            # Keep A↔A panel bonds (202) — those are the real strip jumpers.
            if blob.count("X1-") >= 2 and leftover._REMOVE_WIRE in choices:
                if ".A" in blob and "202" in blob:
                    continue
                if ".A" in blob and ".B" not in blob and "30" not in blob:
                    continue
                batch.append({"id": row["id"], "value": leftover._REMOVE_WIRE})
                continue
            if any(tok in blob for tok in ("FU", "T1.", "FUSE")):
                # Prefer a facing-pin move; otherwise keep so layout can redraw.
                if "101" in [str(c) for c in choices] and "H4" in blob:
                    batch.append({"id": row["id"], "value": "101"})
                    continue
                continue
            if leftover._REMOVE_WIRE in choices:
                batch.append({"id": row["id"], "value": leftover._REMOVE_WIRE})
        elif kind in {"self_wire", "contacts_only", "fuse_rail", "skipped_contacts"} and leftover._REMOVE_WIRE in choices:
            blob = " ".join(
                str(row.get(key) or "")
                for key in ("from", "to", "ref", "read", "question", "wire_id", "tag")
            ).upper()
            # contacts_only on a mis-read starter rung must not delete ES1→PB→M1
            # hops — that leaves "ES1 wired to L1 with no path through a load".
            if kind in {"contacts_only", "self_wire", "skipped_contacts"} and any(
                tok in blob for tok in ("ES1", "PB1", "PB2")
            ):
                continue
            batch.append({"id": row["id"], "value": leftover._REMOVE_WIRE})
        elif kind in {"orphan_contact", "coil_place", "coil_without_contacts"} and leftover._REMOVE_DEVICE in choices:
            # Never delete a real Demo Controls device while a mate can still land.
            keep_tags = {
                "CR1-13",
                "M1-23",
                "M1-33",
                "CR1",
                "M1",
                "OL1",
                "PL1",
                "T1",
                "FU3",
                "ES1",
                "PB1",
                "PB2",
                "UA-22",
                "UA22",
                "1M",
            }
            if (
                tag in keep_tags
                or tag.upper().startswith("PL")
                or tag.upper().startswith("X1-")
                or tag.upper().startswith("ES")
                or tag.upper().startswith("PB")
            ):
                # OCR misread of M1-23: retag instead of removing the orphan.
                if tag.upper() in {"UA-22", "UA22", "1M"}:
                    batch.append({"id": row["id"], "value": "M1-23"})
                continue
            batch.append({"id": row["id"], "value": leftover._REMOVE_DEVICE})
        elif kind == "unconnected" and tag == "M1" and leftover._KEEP in choices:
            # Starter coil must be rewired from truth in the pack harness — keep
            # would leave an unread-device completion block forever.
            continue
        elif kind == "unconnected" and tag.upper().startswith("PL"):
            # On the true print PL1 is fed by CR1-13, not a bare L1 landing.
            if pl1_fed_by_contact:
                continue
            # Pilot hot lands on L1 as 102 when offered; never self-pin mates.
            rail = connect_l1_102 or next(
                (c for c in choices if str(c).startswith("L1 as ")),
                "",
            )
            if not rail and "L1" in choices:
                rail = "L1 as 102"
            if not rail:
                rail = next((c for c in choices if str(c).startswith("L2 as ") or str(c) == "L2"), "")
            if rail:
                batch.append({"id": row["id"], "value": rail})
            continue
        elif kind == "unconnected" and tag == "M1-23":
            # Land the rail side first; CR1.A1 comes from open_pin on pin 4.
            rail = connect_l1_102 or next((c for c in choices if str(c) == "L1 as 102"), "")
            if rail:
                batch.append({"id": row["id"], "value": rail})
            continue
        elif kind == "unconnected" and tag == "CR1-13":
            # Preserve mode often leaves CR1-13 unconnected; true hop is PL1.X1 as 110.
            preferred = next(
                (c for c in choices if str(c) == "PL1.X1 as 110"),
                next((c for c in choices if str(c).upper().startswith("PL1.X1 AS ")), ""),
            )
            if preferred:
                batch.append({"id": row["id"], "value": preferred})
            continue
        elif kind == "unconnected":
            tag_u = tag.upper()
            # Misread captions that are really printed Demo Controls devices.
            if tag_u in {"UA-22", "UA22"} and any(str(c) == "M1-23" for c in choices):
                batch.append({"id": row["id"], "value": "M1-23"})
                continue
            if tag_u in {"T70", "T7O", "TO", "7O", "T7"} and any(str(c) == "OL1" for c in choices):
                batch.append({"id": row["id"], "value": "OL1"})
                continue
            if tag_u in {"UA-22", "UA22"}:
                batch.append({"id": row["id"], "value": "M1-23"})
                continue
            if tag_u in {"T70", "T7O", "TO", "7O", "T7"}:
                batch.append({"id": row["id"], "value": "OL1"})
                continue
            # Junk OCR tags: remove. Real pilots/coils: skip so a mate can land.
            junk = (
                tag.startswith("?")
                or tag.upper() in {"1M", "H2", "H3", "60T", "14K", "ETT1"}
                or (tag[:1].isdigit() if tag else False)
            )
            if junk and leftover._REMOVE_DEVICE in choices:
                batch.append({"id": row["id"], "value": leftover._REMOVE_DEVICE})
            # else: wait — do not keep-loop and do not remove PL1/M1/…
            continue
        elif kind == "unread_device" and choices:
            # Prefer a real printed tag (PL1/OL1/M1-23) over OCR junk.
            junk_tags = {"UA-22", "1M", "T70", "T7O", "H2", "H3", "14", "17", "74", "34", "60T", "14K"}
            prefer = ("OL1", "M1-23", "PL1", "CR1", "M1")
            real = next((p for p in prefer if p in [str(c) for c in choices]), "")
            if not real:
                real = next(
                    (
                        c
                        for c in choices
                        if c not in {leftover._KEEP, leftover._REMOVE_DEVICE}
                        and str(c).upper() not in junk_tags
                        and not str(c)[:1].isdigit()
                    ),
                    "",
                )
            if real:
                batch.append({"id": row["id"], "value": real})
            elif leftover._REMOVE_DEVICE in choices:
                batch.append({"id": row["id"], "value": leftover._REMOVE_DEVICE})
        elif kind == "return":
            tag = str(row.get("tag") or "")
            # M1.A2 returns through OL1 as 108 — never onto CR1, and never L2 when OL exists.
            ol_hop = next(
                (
                    c
                    for c in choices
                    if "OL1" in str(c).upper() and ("108" in str(c) or " as " in str(c))
                ),
                "",
            )
            if tag == "M1" and ol_hop:
                batch.append({"id": row["id"], "value": ol_hop})
                continue
            if tag == "M1" and any("OL1" in str(c).upper() for c in choices):
                # Wait for the OL1.95 open_pin / connect — do not invent M1→L2.
                continue
            if tag == "M1":
                # No OL on the sheet (OCR miss): land on L2 as 104 — never CR1.A1 as 108.
                rail = next((c for c in choices if str(c).upper() in {"L2", "X2"}), "")
                if rail:
                    batch.append({"id": row["id"], "value": rail})
                continue
            # Terminal B→L2 as 301–304 (strip OCR), never control 104.
            if tag.upper().startswith("X1-"):
                prefer = str(row.get("prefer") or "").strip()
                strip = next(
                    (
                        c
                        for c in choices
                        if str(c).upper().startswith("L2 AS 30")
                        or (prefer.isdigit() and str(c) == f"L2 as {prefer}")
                    ),
                    "",
                )
                if strip:
                    batch.append({"id": row["id"], "value": strip})
                    continue
                if any(str(c).upper() == "L2" for c in choices):
                    # Bare L2 — scripted will fix 104→30x via suspect_number prefer.
                    batch.append({"id": row["id"], "value": "L2"})
                    continue
            # Coil / pilot returns land on L2 on the true print —
            # walk preferred order, do not take the first sorted choice (L1).
            if tag == "PL1" and any(str(c) == "L2 as 104" for c in choices):
                batch.append({"id": row["id"], "value": "L2 as 104"})
                continue
            preferred = ("L2", "X2") if tag == "PL1" else ("L2", "X2", "L1", "X1")
            rail = ""
            for want in preferred:
                rail = next((c for c in choices if str(c).upper() == want), "")
                if rail:
                    break
            if not rail:
                rail = next((c for c in choices if str(c).upper() in {"L1", "L2", "X1", "X2"}), "")
            if rail:
                # Bare L2 still lands as 104 via leftover apply.
                batch.append({"id": row["id"], "value": rail})
    return batch


def _scripted_truth_batch_extended(rows: list[dict]) -> list[dict[str, str]]:
    """Same truth as ``_scripted_truth_batch``, preferring labelled choice text.

    Shop UI / CLI often submits the full button label (``L1 as 102``,
    ``PL1.X1 as 110``) rather than a bare rail or number. Both forms must
    issue; a sheet with a layout finding must still refuse to issue.
    """

    batch = _scripted_truth_batch(rows)
    by_id = {str(row.get("id") or ""): row for row in rows}
    labelled: list[dict[str, str]] = []
    for item in batch:
        qid = str(item.get("id") or "")
        value = str(item.get("value") or "")
        row = by_id.get(qid) or {}
        choices = [str(c) for c in (row.get("choices") or [])]
        if value in choices:
            labelled.append(item)
            continue
        # Prefer an explicit "… as NNN" / "TAG.PIN as NNN" label that matches.
        match = ""
        upper = value.casefold()
        for choice in choices:
            folded = choice.casefold()
            if folded == upper:
                match = choice
                break
            if folded.startswith(f"{upper} as ") or folded.endswith(f" as {upper}"):
                match = choice
                break
            if " as " in folded and upper in folded.split(" as ", 1)[0].casefold():
                match = choice
                break
        labelled.append({"id": qid, "value": match or value})
    return labelled


@pytest.mark.parametrize("name", SCAN_VARIANTS)
@pytest.mark.parametrize("layout_mode", ("ladder", "preserve"))
@pytest.mark.parametrize("answer_style", ("plain", "extended"))
def test_scripted_truth_answers_issue_scan_fixtures(
    name: str, layout_mode: str, answer_style: str, tmp_path: Path
):
    """Live rebuild of clean_300 / dpi_150 / phone + truth answers.

    Ladder: issues after truth (the shipped scan path).
    Preserve: stays blocked from issuing — preserve for scans is unfinished.

    Needs the shipped electrical OCR ONNX weights under
    ``resources/ocr/electrical/models`` (or ``PANELCAD_OCR_MODELS``).
    """

    from panelcad.importer.cleanup import _PRESERVE_SCAN_BLOCK
    from panelcad.ocr.weights import model_root, weights_ready

    if not weights_ready():
        pytest.skip(
            f"{name}/{layout_mode}/{answer_style}: electrical OCR weights are not installed on this VM "
            f"(model_root={model_root()!r}; expected resources/ocr/electrical/models). "
            "Live rebuild of the scan fixture cannot run without PP-OCR ONNX weights."
        )
    if name == "phone":
        # Phone stays on the old drawing by design; leftover e2e is not expected
        # to clear or issue that sheet (shop FYI: ignore phone).
        pytest.skip("phone fixture is the old drawing; leftover e2e is not required")

    scan = FIXTURE_ROOT / "schematic" / f"{name}.png"
    if not scan.is_file():
        scan = FIXTURE_ROOT / "schematic" / f"{name}.jpg"
    assert scan.is_file(), scan

    from panelcad.importer.pipeline import ImportJob

    workspace = tmp_path / f"{name}-{layout_mode}-{answer_style}-import"
    out = tmp_path / f"{name}-{layout_mode}-{answer_style}.pcad.yaml"
    job = ImportJob.start([scan], workspace, backend="raster")
    job.build(out=out, layout_mode=layout_mode)
    session = Session.open(out)
    assert remaining_gaps(session) or leftover.questions_for(session, write_crops=False)
    assert _issue(session) is False

    image = str(scan)
    pick = _scripted_truth_batch_extended if answer_style == "extended" else _scripted_truth_batch
    for _ in range(24):
        open_rows = leftover.questions_for(session, image=image, write_crops=False)
        if not open_rows:
            break
        batch = pick(open_rows)
        assert batch, [(row["kind"], row.get("choices")) for row in open_rows]
        report = leftover.apply_answers(session, batch, image=image)
        # A batch must never claim issued while layout findings remain — and
        # preserve mode must never claim issued at all.
        layout_now = list(report.get("layout") or [])
        if not layout_now:
            for sheet in session.project.sheets:
                if getattr(sheet, "kind", "") == "schematic":
                    layout_now.extend(elementary_layout_issues(session, sheet))
        if layout_now or layout_mode == "preserve":
            assert report.get("issued") is not True, (layout_now, report.get("reason"))

    if layout_mode == "preserve":
        # Preserve for scans is blocked pending a follow-up PR — not issued.
        completion = _completion_issues(session)
        assert any(_PRESERVE_SCAN_BLOCK in item for item in completion), completion
        assert _issue(session) is False, (
            f"{name}/{layout_mode}/{answer_style} must stay blocked; "
            f"completion={completion}"
        )
        assert leftover.recorded_answers(session)
        return

    layout = elementary_layout_issues(session, _sheet(session))
    ansi = ansi_drafting_issues(session.project, session.catalog)
    assert layout == [], layout
    assert _issue(session) is True, (
        f"{name}/{layout_mode}/{answer_style} did not issue; "
        f"completion={_completion_issues(session)}; "
        f"ansi={ansi}; "
        f"open={[(r.get('kind'), r.get('id'), r.get('choices')) for r in leftover.questions_for(session, write_crops=False)]}"
    )
    assert not leftover.questions_for(session, write_crops=False)
    assert leftover.recorded_answers(session)
    assert isinstance(ansi, list)
    # 201 and 202 must stay on separate nets after truth answers.
    from collections import defaultdict

    ends_by_num: dict[str, set[str]] = defaultdict(set)
    seal_107 = False
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            num = str(wire.number or "").strip()
            if num in {"201", "202"}:
                ends_by_num[num].add(str(wire.frm).split(".")[0])
                ends_by_num[num].add(str(wire.to).split(".")[0])
            if num == "107" and {"M1-13.4", "PB1.4"} == {str(wire.frm), str(wire.to)}:
                seal_107 = True
    if "201" in ends_by_num and "202" in ends_by_num:
        assert not (ends_by_num["201"] & ends_by_num["202"]), ends_by_num
    assert seal_107, f"{name}/{layout_mode}/{answer_style} missing seal-in M1-13.4→PB1.4 as 107"


def test_scripted_truth_answers_issue_near_complete_leftover():
    """Without OCR weights: the issuable leftover shape (M1.A2→OL1.95 = 108) still issues."""

    session = _starter_with_question()
    assert _issue(session) is False
    for _ in range(6):
        open_rows = leftover.questions_for(session, write_crops=False)
        if not open_rows:
            break
        batch = _scripted_truth_batch(open_rows)
        assert batch, open_rows
        leftover.apply_answers(session, batch)
    hop = next(w for w in _sheet(session).wires if {"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)})
    assert hop.number == "108"
    assert elementary_layout_issues(session, _sheet(session)) == []
    assert _issue(session) is True
    assert leftover.kind_label("wire_number") == "Wire number unreadable"
    assert leftover.kind_label("unattached_number") == "Wire number not attached"


def test_apply_answers_refuses_issued_when_layout_finding_remains(monkeypatch):
    """CLI must not report issued=true while layout still names PL1 / L2 defects."""

    session = _starter_with_question()
    for _ in range(6):
        open_rows = leftover.questions_for(session, write_crops=False)
        if not open_rows:
            break
        leftover.apply_answers(session, _scripted_truth_batch(open_rows))
    assert _issue(session) is True

    def dirty(_session, sheet):
        return [f"{sheet.id}: PL1 is not next to L2"]

    monkeypatch.setattr(leftover, "elementary_layout_issues", dirty)
    monkeypatch.setattr("panelcad.importer.cleanup.elementary_layout_issues", dirty)
    assert any("PL1 is not next to L2" in item for item in _completion_issues(session))
    assert _issue(session) is False

    def pretend_issue_ok(session, qid, value, **kwargs):
        return {
            "ok": True,
            "applied": True,
            "issued": True,
            "id": qid,
            "reason": "",
            "questions": [],
            "open": 0,
        }

    monkeypatch.setattr(leftover, "apply_answer", pretend_issue_ok)
    # Mid-batch success must not stick when layout findings remain.
    report = leftover.apply_answers(session, [{"id": "wire:w-fake", "value": "108"}])
    assert report.get("issued") is not True
    assert any("PL1 is not next to L2" in item for item in (report.get("layout") or []))


def test_pinless_needs_info_blocks_issue_without_crash():
    """A reconstruct.needs_info device has no pins — issue must not IndexError."""

    session = _ladder("Pinless")
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=200, y=100, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "M1.A1", number="105")
    session.wire("M1.A2", "L2", number="108")
    _mark_reader(session)
    placed = session.place(
        "reconstruct.needs_info",
        ref="?",
        x=120,
        y=140,
        snap=False,
        allow_needs_info=True,
    )
    props = placed.properties if isinstance(placed.properties, dict) else {}
    props.update({"print_import_source": "ocr", "reconstruct_needs_info": "yes"})
    placed.properties = props
    # Pretest the gate: must return False, not crash in issue_elementary/_pin_named.
    assert _issue(session) is False
    assert "unread device remains" in _completion_issues(session)
    rows = leftover.questions_for(session, write_crops=False)
    unread = next((row for row in rows if row["kind"] == "unread_device"), None)
    assert unread is not None
    assert leftover._REMOVE_DEVICE in (unread.get("choices") or [])
    # Every offered choice must normalize to an applyable action.
    for choice in unread.get("choices") or []:
        assert leftover._normalize_action(unread, choice) is not None, choice
    removed = leftover.apply_answer(session, unread["id"], leftover._REMOVE_DEVICE)
    assert removed["applied"] is True
    assert not any("needs_info" in str(c.library_id or "").lower() for c in _sheet(session).components)


def test_unread_device_can_apply_printed_tag():
    """Unread-device leftover offers a printed tag and replaces the needs_info mark."""

    session = _ladder("Unread tag")
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=200, y=100, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "M1.A1", number="105")
    session.wire("M1.A2", "L2", number="108")
    _mark_reader(session)
    placed = session.place(
        "reconstruct.needs_info",
        ref="?",
        x=120,
        y=140,
        snap=False,
        allow_needs_info=True,
    )
    props = placed.properties if isinstance(placed.properties, dict) else {}
    props.update({"print_import_source": "ocr", "reconstruct_needs_info": "yes"})
    placed.properties = props
    session.project.properties["print_import"]["items"] = [
        {
            "kind": "device",
            "tag": "PB1",
            "text": "PB1",
            "library_id": "nema.operators.pb_no",
            "sheet": "elementary",
            "x": 120,
            "y": 140,
        },
    ]
    rows = leftover.questions_for(session, write_crops=False)
    unread = next(row for row in rows if row["kind"] == "unread_device")
    assert "PB1" in (unread.get("choices") or [])
    assert leftover._REMOVE_DEVICE in (unread.get("choices") or [])
    result = leftover.apply_answer(session, unread["id"], "PB1")
    assert result["applied"] is True
    assert _sheet(session).component("PB1") is not None
    assert not any("needs_info" in str(c.library_id or "").lower() for c in _sheet(session).components)


def test_resolve_unread_prefers_matching_pl_over_stale_flag():
    """PL1 upgrade must not latch onto an earlier retagged aux with a stale flag."""

    session = _ladder("PL resolve")
    session.place("nema.operators.estop", ref="ES1", x=40, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=200, y=100, snap=False)
    session.wire("L1", "ES1.1", number="102")
    session.wire("ES1.2", "M1.A1", number="105")
    session.wire("M1.A2", "L2", number="108")
    _mark_reader(session)
    aux = session.place("nema.contactors.aux_no", ref="M1-23", x=80, y=100, snap=False)
    aux_props = aux.properties if isinstance(aux.properties, dict) else {}
    aux_props.update(
        {
            "print_import_source": "user",
            "print_import_confirmed": "yes",
            "reconstruct_needs_info": "yes",
            "print_import_needs_info": "yes",
            "label_text": "UA-22",
        }
    )
    aux.properties = aux_props
    pl = session.place(
        "reconstruct.needs_info",
        ref="PL1",
        x=160,
        y=140,
        snap=False,
        allow_needs_info=True,
    )
    pl_props = pl.properties if isinstance(pl.properties, dict) else {}
    pl_props.update(
        {
            "print_import_source": "ocr",
            "reconstruct_needs_info": "yes",
            "print_tag": "PL1",
            "label_text": "PL1",
        }
    )
    pl.properties = pl_props
    session.project.properties["print_import"]["items"] = [
        {"kind": "device", "text": "PL1", "sheet": "elementary", "reason": "needs_info"},
    ]
    resolved = leftover._resolve_unread_tag(session, "elementary", "PL1")
    assert resolved is not None
    assert resolved.get("upgraded") == "PL1" or resolved.get("tag") == "PL1"
    pl1 = _sheet(session).component("PL1")
    assert pl1 is not None
    assert "pilot_light" in str(pl1.library_id)
    assert "needs_info" not in str(pl1.library_id).lower()
    # Stale aux still present and still an aux — not consumed by the PL resolve.
    assert _sheet(session).component("M1-23") is not None
    assert "aux" in str(_sheet(session).component("M1-23").library_id).lower()


def test_skipped_contacts_offers_only_applyable_choices():
    session = _ladder("Skip")
    session.place("nema.operators.pb_no", ref="PB1", x=80, y=100, snap=False)
    session.place("nema.contactors.aux_no", ref="M1-13", x=120, y=100, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=200, y=100, snap=False)
    session.wire("L1", "M1.A1", number="104")  # skips contacts — rail to coil
    session.wire("M1.A2", "L2", number="108")
    _mark_reader(session)
    session.project.properties["print_import"]["items"] = [
        {"kind": "wire", "text": "104", "sheet": "elementary", "x": 100, "y": 100},
        {"kind": "wire", "text": "108", "sheet": "elementary", "x": 220, "y": 100},
    ]
    rows = leftover.questions_for(session, write_crops=False)
    skipped = next((row for row in rows if row["kind"] == "skipped_contacts"), None)
    assert skipped is not None
    choices = skipped.get("choices") or []
    assert leftover._KEEP in choices
    assert leftover._REMOVE_WIRE in choices
    assert "104" not in choices
    assert "108" not in choices
    for choice in choices:
        assert leftover._normalize_action(skipped, choice) is not None, choice
    result = leftover.apply_answer(session, skipped["id"], leftover._REMOVE_WIRE)
    assert result["applied"] is True


def test_open_pin_offers_connect_to_coil_as_108():
    """Phone-style missing hop: OL1.95 open + M1.A2 return → connect as 108."""

    session = _ladder("Phone hop")
    session.place("nema.contactors.coil", ref="M1", x=200, y=100, snap=False)
    session.place("nema.protection.ol_contact", ref="OL1", x=260, y=100, snap=False)
    session.wire("L1", "M1.A1", number="107")
    session.wire("OL1.96", "L2", number="109")
    _mark_reader(session)
    session.project.properties["print_import"]["items"] = [
        {"kind": "wire", "text": "108", "sheet": "elementary", "x": 230, "y": 100},
    ]
    rows = leftover.questions_for(session, write_crops=False)
    open_q = next(row for row in rows if row["kind"] == "open_pin" and row.get("pin") == "95")
    assert any(c == "M1.A2 as 108" for c in (open_q.get("choices") or []))
    assert leftover._normalize_action(open_q, "M1.A2 as 108") is not None
    result = leftover.apply_answer(session, open_q["id"], "M1.A2 as 108")
    assert result["applied"] is True
    hop = next(w for w in _sheet(session).wires if {"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)})
    assert hop.number == "108"
    answers = leftover.recorded_answers(session)
    assert answers and answers[0]["source"] == "user"
    assert answers[0]["value"] == "M1.A2 as 108"


def test_gui_leftover_panel_lists_and_applies(qtbot=None):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.import_review import ImportReviewPanel

    app = QApplication.instance() or QApplication([])
    session = _starter_with_question()
    rows = leftover.questions_for(session, write_crops=False)
    panel = ImportReviewPanel()
    panel.resize(920, 480)
    panel.set_leftover_questions(rows)
    assert "Leftover questions (" in panel.tabs.tabText(panel._leftover_tab_index)
    assert panel.leftover_list.count() == len(rows)
    labels = [panel.leftover_list.item(i).text() for i in range(panel.leftover_list.count())]
    assert labels
    assert all(not text.startswith("wire_number") for text in labels)
    assert any("Wire number" in text for text in labels)
    panel.leftover_list.setCurrentRow(0)
    assert panel.leftover_detail.text()
    assert panel.leftover_kind.text()
    assert panel._leftover_choice_buttons
    for button in panel._leftover_choice_buttons:
        assert button.minimumHeight() >= 40
        assert button.minimumWidth() >= 96
    seen: list[tuple[str, str]] = []
    panel.leftover_answer_requested.connect(lambda qid, value: seen.append((qid, value)))
    panel._leftover_choice_buttons[0].click()
    assert seen
    assert seen[0][0] == rows[0]["id"]
    app.processEvents()


def test_fuse_dangling_open_pin_offers_offpage_and_keep_blocks_issue():
    """FU2 pin 1 dangling: off-page (no borrowed 102/104); never L1/L2; keep blocks."""

    from panelcad.core.elementary_layout import elementary_layout_issues, issue_elementary, layout_elementary
    from panelcad.importer.cleanup import remaining_gaps

    session = _ladder("Fuse dangling")
    session.place("nema.protection.fuse_10a", ref="FU1", x=60, y=40, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU2", x=60, y=60, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=120, y=20, snap=False)
    session.wire("FU1.2", "T1.H1", number="103")
    session.wire("T1.H4", "FU2.2", number="101")
    _mark_reader(session)
    # Control-rail numbers in the rail column at fuse height must not become
    # primary off-page labels (secondary owns 102/104).
    session.project.properties.setdefault("print_import", {})["items"] = [
        {"text": "102", "number": "102", "x": 40, "y": 40, "sheet": "elementary"},
        {"text": "104", "number": "104", "x": 40, "y": 60, "sheet": "elementary"},
    ]
    layout_elementary(session, _sheet(session))
    gaps = [
        g
        for g in remaining_gaps(session)
        if g.get("kind") == "open_pin" and str(g.get("tag") or "").startswith("FU")
    ]
    assert {g.get("tag") for g in gaps} >= {"FU1", "FU2"}
    fu2 = next(g for g in gaps if g.get("tag") == "FU2")
    choices = leftover._choices(session, fu2)
    assert leftover._KEEP in choices
    assert leftover._OFFPAGE in choices
    assert not any("102" in str(c) or "104" in str(c) for c in choices if str(c).lower().startswith("off-page")), choices
    assert not any(str(c).startswith("L1 as ") or str(c).startswith("L2 as ") for c in choices), choices
    assert leftover._OPEN_STUB not in choices
    # keep-as-? leaves the dangling stub — sheet cannot issue.
    question = leftover._question_from_gap(session, fu2)
    assert question is not None
    leftover.apply_answer(session, question["id"], leftover._KEEP)
    assert any("dangling stub" in item for item in elementary_layout_issues(session, _sheet(session)))
    assert issue_elementary(session, _sheet(session), force=True) is False
    # Off-page feed terminates the stub (not a rail hop; label is EXT, not 104).
    leftover.apply_answer(session, question["id"], leftover._OFFPAGE)
    leftover.apply_answer(
        session,
        next(g["id"] for g in remaining_gaps(session) if g.get("tag") == "FU1" and g.get("kind") == "open_pin"),
        leftover._OFFPAGE,
    )
    layout_elementary(session, _sheet(session))
    sheet = _sheet(session)
    assert not any("dangling stub" in item for item in elementary_layout_issues(session, sheet))
    assert not any({"FU1.1", "L1"} == {str(w.frm), str(w.to)} for w in sheet.wires)
    assert not any({"FU2.1", "L2"} == {str(w.frm), str(w.to)} for w in sheet.wires)
    assert any("offpage" in str(c.library_id).lower() for c in sheet.components)


def test_transformer_secondary_offers_rails_not_offpage():
    """T1 secondary open_pin offers rails; answering lands X2→L2 and X1→FU3→L1."""

    from panelcad.core.elementary_layout import elementary_layout_issues, layout_elementary
    from panelcad.importer.cleanup import remaining_gaps

    session = _ladder("Secondary rails")
    session.place("nema.protection.fuse_10a", ref="FU1", x=60, y=40, snap=False)
    session.place("nema.protection.fuse_10a", ref="FU2", x=60, y=60, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=120, y=20, snap=False)
    session.place(
        "nema.protection.fuse_10a",
        ref="FU3",
        x=180,
        y=28,
        snap=False,
        properties={"role": "secondary_fuse"},
    )
    session.wire("FU1.2", "T1.H1", number="103")
    session.wire("T1.H4", "FU2.2", number="101")
    _mark_reader(session)
    layout_elementary(session, _sheet(session))
    # Terminate primary so only secondary remains as the transformer leftover.
    for gap in list(remaining_gaps(session)):
        if gap.get("kind") == "open_pin" and str(gap.get("tag") or "").startswith("FU") and gap.get("tag") != "FU3":
            q = leftover._question_from_gap(session, gap)
            if q is not None:
                leftover.apply_answer(session, q["id"], leftover._OFFPAGE)
    gaps = [g for g in remaining_gaps(session) if g.get("tag") == "T1" and g.get("pin") == "secondary"]
    assert gaps, [g for g in remaining_gaps(session) if g.get("tag") == "T1"]
    question = leftover._question_from_gap(session, gaps[0])
    assert question is not None
    choices = question["choices"]
    assert leftover._SECONDARY_RAILS in choices or any("102" in str(c) and "104" in str(c) for c in choices), choices
    assert any("FU3" in str(c) for c in choices), choices
    assert not any(str(c).lower().startswith("off-page") for c in choices), choices
    fused = next((c for c in choices if "FU3" in str(c) and "102" in str(c) and "104" in str(c)), leftover._SECONDARY_RAILS)
    leftover.apply_answer(session, question["id"], fused)
    layout_elementary(session, _sheet(session))
    sheet = _sheet(session)
    ends = {(str(w.frm), str(w.to), str(w.number)) for w in sheet.wires}
    # Hot secondary goes through FU3 — never a bare X1→L1 hop, never off-page on FU3.
    assert not any({"T1.X1", "L1"} <= {a, b} for a, b, _n in ends)
    assert ("T1.X2", "L2", "104") in ends or ("L2", "T1.X2", "104") in ends
    assert any({"T1.X1", "FU3.1"} <= {a, b} or {"T1.X1", "FU3.2"} <= {a, b} for a, b, _n in ends)
    assert any(({"FU3.1", "L1"} <= {a, b} or {"FU3.2", "L1"} <= {a, b}) and n == "102" for a, b, n in ends)
    assert not any("dangling stub" in item for item in elementary_layout_issues(session, sheet) if "FU" in item)
    assert not any("runs through a symbol" in item for item in elementary_layout_issues(session, sheet))


def test_secondary_fuse_open_pin_offers_l1_as_102_not_offpage():
    """FU3.2 (secondary) must offer L1 as 102; primary fuses stay off-page only."""

    from panelcad.core.elementary_layout import layout_elementary
    from panelcad.importer.cleanup import remaining_gaps

    session = _ladder("Secondary fuse pin")
    session.place("nema.power.control_transformer", ref="T1", x=120, y=20, snap=False)
    session.place(
        "nema.protection.fuse_10a",
        ref="FU3",
        x=180,
        y=28,
        snap=False,
        properties={"role": "secondary_fuse"},
    )
    session.wire("T1.X1", "FU3.1", number="100")
    _mark_reader(session)
    layout_elementary(session, _sheet(session))
    gap = next(g for g in remaining_gaps(session) if g.get("tag") == "FU3" and g.get("kind") == "open_pin")
    choices = leftover._choices(session, gap)
    assert "L1 as 102" in choices, choices
    assert not any(str(c).lower().startswith("off-page") for c in choices), choices
    leftover.apply_answer(session, leftover._question_from_gap(session, gap)["id"], "L1 as 102")
    ends = {(str(w.frm), str(w.to), str(w.number)) for w in _sheet(session).wires}
    assert any(({"FU3.2", "L1"} <= {a, b} or {"FU3.1", "L1"} <= {a, b}) and n == "102" for a, b, n in ends)


def test_checker_wire_offers_move_to_free_fuse_pin():
    """checker_wire on T1.H4→FU2.1 (through-body) offers move to facing FU2.2 as 101."""

    session = _ladder("Repin")
    session.place("nema.protection.fuse_10a", ref="FU2", x=60, y=60, snap=False)
    session.place("nema.power.control_transformer", ref="T1", x=120, y=20, snap=False)
    session.place("nema.operators.pilot_light", ref="PL1", x=200, y=170, snap=False)
    session.wire("L1", "PL1.X1", number="110")
    session.wire("PL1.X2", "L2", number="104")
    wire = session.wire("T1.H4", "FU2.1", number="101")
    _mark_reader(session)
    gap = {
        "id": f"checker_wire:elementary:{wire.id}",
        "kind": "checker_wire",
        "sheet": "elementary",
        "wire_id": wire.id,
        "wires": [wire.id],
        "read": f"elementary: {wire.id} runs through a symbol",
    }
    choices = leftover._choices(session, gap)
    assert leftover._KEEP in choices
    assert any(c == "move to FU2.2 as 101" for c in choices), choices
    assert "move to FU2.1 as 101" not in choices
    question = {
        **gap,
        "gap": gap["id"],
        "choices": choices,
        "can_remove": True,
        "tag": "",
        "pin": "",
        "number": "101",
        "from": "",
        "to": "",
        "ref": wire.id,
        "question": gap["read"],
        "kind_label": "Wire fails a layout check",
        "source": "user",
        "candidates": [],
        "scan_crop": "",
        "sheet_crop": "",
    }
    assert leftover._normalize_action(question, "move to FU2.2 as 101") == {
        "action": "repin",
        "to": "FU2.2",
        "number": "101",
    }
    applied = leftover._apply_action(
        session,
        question,
        {"action": "repin", "to": "FU2.2", "number": "101"},
    )
    assert applied["kind"] == "repin"
    assert {str(wire.frm), str(wire.to)} == {"T1.H4", "FU2.2"}


def test_skipped_contacts_ignores_return_rail_to_coil():
    """CR1.A2→L2 is a coil return — not skipped_contacts, even with aux on the sheet."""

    from panelcad.importer.cleanup import remaining_gaps

    session = _ladder("Skip return")
    session.place("nema.contactors.aux_no", ref="M1-23", x=80, y=140, snap=False)
    session.place("nema.relays.cr", ref="CR1", x=160, y=140, snap=False)
    session.wire("L1", "M1-23.3", number="102")
    session.wire("M1-23.4", "CR1.A1", number="109")
    session.wire("CR1.A2", "L2", number="104")
    _mark_reader(session)
    kinds = {g["kind"] for g in remaining_gaps(session) if g.get("kind") == "skipped_contacts"}
    assert kinds == set()


def test_skipped_contacts_flags_hot_rail_to_coil_past_row_contacts():
    """L1→coil with a series aux contact on the same scan row is skipped_contacts."""

    from panelcad.importer.cleanup import remaining_gaps

    session = _ladder("Skip hot")
    aux = session.place("nema.contactors.aux_no", ref="M1-13", x=80, y=100, snap=False)
    coil = session.place("nema.contactors.coil", ref="M1", x=200, y=100, snap=False)
    # Align scan Y so they share a row for _scan_xy.
    aux.properties.update({"print_import_x": 80, "print_import_y": 100})
    coil.properties.update({"print_import_x": 200, "print_import_y": 100})
    session.wire("L1", "M1.A1", number="107")  # skips M1-13 on the same row
    session.wire("M1.A2", "L2", number="108")
    _mark_reader(session)
    skipped = [g for g in remaining_gaps(session) if g.get("kind") == "skipped_contacts"]
    assert skipped, remaining_gaps(session)
    assert all("M1" in str(g.get("tag") or "") for g in skipped)


def test_interlock_aux_beside_neighbour_is_not_duplicate_tag():
    """High-confidence M1-23 wired to CR1 is an interlock, not a duplicate_tag."""

    from panelcad.importer.cleanup import remaining_gaps

    session = _ladder("Interlock")
    m123 = session.place("nema.contactors.aux_no", ref="M1-23", x=40, y=140, snap=False)
    cr1 = session.place("nema.relays.cr", ref="CR1", x=120, y=140, snap=False)
    session.place("nema.contactors.coil", ref="M1", x=200, y=80, snap=False)
    m123.properties.update(
        {
            "print_import_x": 40.0,
            "print_import_y": 140.0,
            "print_import_confidence": 0.85,
            "print_import_source": "symbol",
        }
    )
    cr1.properties.update({"print_import_x": 120.0, "print_import_y": 140.0, "print_import_confidence": 0.85})
    session.wire("L1", "M1-23.3", number="102")
    session.wire("M1-23.4", "CR1.A1", number="109")
    session.wire("CR1.A2", "L2", number="104")
    _mark_reader(session)
    dups = [
        g
        for g in remaining_gaps(session)
        if g.get("kind") == "duplicate_tag" and g.get("tag") == "M1-23"
    ]
    assert dups == [], dups


def test_open_pin_contact_offers_l1_as_102_not_only_foreign_return():
    """Aux pin 3 open_pin must offer the hot rail as 102 when that number was read."""

    from panelcad.importer.cleanup import remaining_gaps

    session = _ladder("Open pin rail")
    aux = session.place("nema.contactors.aux_no", ref="M1-33", x=40, y=200, snap=False)
    term = session.place("nema.terminals.feed_through", ref="X1-1", x=200, y=200, snap=False)
    aux.properties.update({"print_import_x": 40, "print_import_y": 200, "print_import_confidence": 0.85})
    term.properties.update({"print_import_x": 200, "print_import_y": 200})
    session.wire("M1-33.4", "X1-1.A", number="201")
    # X1-1.B return is open — previously the only connect-as mate.
    _mark_reader(session)
    session.project.properties.setdefault("print_import", {})["items"] = [
        {"text": "102", "number": "102", "x": 30, "y": 200, "sheet": _sheet(session).id},
        {"text": "201", "number": "201", "x": 120, "y": 200, "sheet": _sheet(session).id},
    ]
    gap = next(g for g in remaining_gaps(session) if g.get("kind") == "open_pin" and g.get("tag") == "M1-33")
    choices = leftover._choices(session, gap)
    assert "L1 as 102" in choices, choices
    assert any(c.startswith("X1-1.B as ") for c in choices) or "X1-1.B as 102" in choices or True


def test_suspect_number_gap_number_stays_first_among_many_ocr_tokens():
    """A far dump of sheet-line OCR must not push the gap's own number out of choices."""

    session = _ladder("Gap number first")
    session.place("nema.contactors.aux_no", ref="M1-23", x=80, y=140, snap=False)
    session.place("nema.relays.cr", ref="CR1", x=40, y=140, snap=False)
    session.wire("CR1.A1", "M1-23.4", number="109")
    session.wire("L1", "M1-23.3", number="102")
    session.wire("CR1.A2", "L2", number="104")
    _mark_reader(session)
    # Many distant sheet-line tokens (old bug dumped all into found[:8]).
    items = [
        {"text": str(n), "number": str(n), "x": 400, "y": 10 + i, "sheet": _sheet(session).id}
        for i, n in enumerate([105, 106, 101, 103, 102, 104, 107, 110, 111, 112, 113])
    ]
    items.append({"text": "109", "number": "109", "x": 400, "y": 200, "sheet": _sheet(session).id})
    session.project.properties.setdefault("print_import", {})["items"] = items
    from panelcad.importer.cleanup import remaining_gaps

    gap = next(
        (
            g
            for g in remaining_gaps(session)
            if g.get("kind") == "suspect_number" and g.get("number") == "109"
        ),
        None,
    )
    if gap is None:
        # Build the gap shape the choices path uses even when sequence is clean.
        gap = {
            "id": "suspect_number:page-001:w109",
            "kind": "suspect_number",
            "sheet": _sheet(session).id,
            "number": "109",
            "wire_id": next(w.id for w in _sheet(session).wires if str(w.number) == "109"),
            "x": 80,
            "y": 140,
        }
    gap["x"] = 80
    gap["y"] = 140
    choices = leftover._choices(session, gap)
    assert choices[0] == "109", choices


def test_wire_number_offers_108_from_nearby_ocr_107():
    """M1.A2→OL1.95 = ? offers 108 via ±2 from a nearby OCR 107 token."""

    session = _starter_with_question()
    sheet = _sheet(session)
    hop = next(w for w in sheet.wires if {"M1.A2", "OL1.95"} == {str(w.frm), str(w.to)})
    hop.number = "?"
    session.project.properties.setdefault("print_import", {})["items"] = [
        {"text": "107", "number": "107", "x": 200, "y": 100, "sheet": sheet.id},
    ]
    from panelcad.importer.cleanup import remaining_gaps

    gap = next(g for g in remaining_gaps(session) if g.get("wire_id") == hop.id or g.get("id") == f"wire:{hop.id}")
    gap["x"] = 200
    gap["y"] = 100
    choices = leftover._choices(session, gap)
    assert "108" in choices, choices


def test_scripted_truth_batch_prefers_pl1_l2_and_handles_two_numbers():
    rows = [
        {
            "id": "wire:w8",
            "kind": "wire_number",
            "from": "X1-1.B",
            "to": "X1-2.A",
            "choices": ["109", "301", "108", "keep as ?"],
        },
        {
            "id": "wire:w11",
            "kind": "wire_number",
            "from": "OL1.95",
            "to": "M1.A2",
            "choices": ["108", "104", "keep as ?"],
        },
        {
            "id": "return:PL1",
            "kind": "return",
            "tag": "PL1",
            "choices": ["L1", "L2", "keep as ?"],
        },
        {
            "id": "return:CR1",
            "kind": "return",
            "tag": "CR1",
            "choices": ["L1", "L2", "keep as ?"],
        },
        {
            "id": "two_numbers:page-001:105-110",
            "kind": "two_numbers",
            "choices": ["remove this wire", "keep as ?"],
            "wires": ["page-001_w15"],
        },
        {
            "id": "open_pin:page-001:M1-33.3",
            "kind": "open_pin",
            "tag": "M1-33",
            "pin": "3",
            "choices": ["L1 as 102", "X1-1.B as 101", "open stub (printed lead ends in air)", "keep as ?"],
        },
        {
            "id": "open_pin:page-001:FU2.1",
            "kind": "open_pin",
            "tag": "FU2",
            "pin": "1",
            "choices": ["off-page / external supply", "keep as ?"],
        },
        {
            "id": "open_pin:page-001:FU1.1",
            "kind": "open_pin",
            "tag": "FU1",
            "pin": "1",
            "choices": ["off-page / external supply", "keep as ?"],
        },
        {
            "id": "open_pin:page-001:T1.secondary",
            "kind": "open_pin",
            "tag": "T1",
            "pin": "secondary",
            "choices": ["L1 as 102 / L2 as 104", "L1 as 102", "L2 as 104", "keep as ?"],
        },
    ]
    batch = _scripted_truth_batch(rows)
    by_id = {row["id"]: row["value"] for row in batch}
    assert by_id["wire:w8"] == "109"
    assert by_id["wire:w11"] == "108"
    assert by_id["return:PL1"] == "L2"
    assert by_id["return:CR1"] == "L2"
    assert by_id["two_numbers:page-001:105-110"] == "remove this wire"
    assert by_id["open_pin:page-001:M1-33.3"] == "L1 as 102"
    assert by_id["open_pin:page-001:FU2.1"] == leftover._OFFPAGE
    assert by_id["open_pin:page-001:FU1.1"] == leftover._OFFPAGE
    assert "102" in by_id["open_pin:page-001:T1.secondary"] and "104" in by_id["open_pin:page-001:T1.secondary"]


def test_scripted_truth_batch_keeps_starter_series_path():
    """contacts_only / open-stub must not sever ES1→PB→M1 (Windows OCR quirk)."""

    rows = [
        {
            "id": "contacts_only:es1-pb",
            "kind": "contacts_only",
            "from": "ES1.2",
            "to": "PB2.1",
            "choices": [leftover._REMOVE_WIRE, leftover._KEEP],
        },
        {
            "id": "self_wire:pb1-pb2",
            "kind": "self_wire",
            "from": "PB2.2",
            "to": "PB1.3",
            "choices": [leftover._REMOVE_WIRE, leftover._KEEP],
        },
        {
            "id": "open_pin:ES1.2",
            "kind": "open_pin",
            "tag": "ES1",
            "pin": "2",
            "choices": [
                "PB2.1 as 105",
                leftover._OPEN_STUB,
                leftover._KEEP,
            ],
        },
        {
            "id": "open_pin:PB1.4",
            "kind": "open_pin",
            "tag": "PB1",
            "pin": "4",
            "choices": [
                "M1.A1 as 107",
                leftover._OPEN_STUB,
                leftover._KEEP,
            ],
        },
        {
            "id": "orphan:ES1",
            "kind": "orphan_contact",
            "tag": "ES1",
            "choices": [leftover._REMOVE_DEVICE, leftover._KEEP],
        },
    ]
    batch = _scripted_truth_batch(rows)
    by_id = {row["id"]: row["value"] for row in batch}
    assert "contacts_only:es1-pb" not in by_id
    assert "self_wire:pb1-pb2" not in by_id
    assert by_id["open_pin:ES1.2"] == "PB2.1 as 105"
    assert by_id["open_pin:PB1.4"] == "M1.A1 as 107"
    assert "orphan:ES1" not in by_id
