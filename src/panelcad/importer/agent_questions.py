"""Open questions for a human when an agent import is incomplete.

Nothing here guesses a tag, a contact, a terminal, or a catalog part.
An empty answer changes nothing. A catalog number is applied only when that
exact number is already in the library and has published dimensions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_QUESTION_KEYS = ("id", "question", "bbox", "image", "crop", "candidates", "changes")


def questions_for(session: Any) -> list[dict[str, Any]]:
    """Machine-readable questions still open on this project."""

    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    prep = Path(str(payload.get("prep_dir") or ""))
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or item.get("confirmed"):
            continue
        row = _question_from_item(item)
        if row is None:
            continue
        _add(found, seen, row, session, prep)
    for sheet in session.project.sheets:
        for comp in sheet.components:
            row = _question_from_component(session, sheet, comp, seen)
            if row is not None:
                _add(found, seen, row, session, prep)
        for wire in sheet.wires:
            if not getattr(wire, "inferred", False):
                continue
            row = _question_from_wire(sheet, wire)
            if row is not None:
                _add(found, seen, row, session, prep)
    return found


def apply_answer(session: Any, question_id: str, value: str) -> dict[str, Any]:
    """Apply one human answer. Refuses an empty value and an unknown catalog number."""

    question = _find(session, question_id)
    if question is None:
        return _result(session, applied=False, error=f"no open question {question_id}")
    text = str(value or "").strip()
    if not text:
        return _result(session, applied=False, error="no answer given; the item stays needs-info")
    kind = str(question.get("kind") or "")
    try:
        if kind == "contact":
            _answer_contact(session, question, text)
        elif kind == "tag":
            _answer_tag(session, question, text)
        elif kind == "device":
            _answer_device(session, question, text)
        elif kind == "part":
            _answer_part(session, question, text)
        elif kind == "wire":
            _answer_wire(session, question, text)
        elif kind == "terminal":
            _answer_terminal(session, question, text)
        elif kind == "coil":
            _answer_coil(session, question, text)
        else:
            raise ValueError(f"cannot apply a {kind or 'question'}")
    except (KeyError, ValueError) as exc:
        return _result(session, applied=False, error=str(exc).splitlines()[0], question_id=question_id)
    _confirm(session, question)
    if session.path:
        session.save(session.path)
    return _result(session, applied=True, question_id=question_id)


def apply_answers(session: Any, answers: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply each answer. A refusal leaves that item needs-info and continues."""

    results: list[dict[str, Any]] = []
    for row in answers:
        if not isinstance(row, dict):
            continue
        one = apply_answer(session, str(row.get("id") or ""), str(row.get("value") or ""))
        results.append({"id": str(row.get("id") or ""), "applied": bool(one.get("applied")), "error": one.get("error") or ""})
    report = _result(session, applied=all(item["applied"] for item in results) if results else False)
    report["results"] = results
    return report


def load_answers(path: Path | str) -> list[dict[str, str]]:
    """A list of {id, value}, or a map of id to value."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("answers"), list):
        data = data["answers"]
    if isinstance(data, dict):
        return [{"id": str(key), "value": str(value)} for key, value in data.items() if str(key) != "project"]
    rows: list[dict[str, str]] = []
    for item in data or []:
        if isinstance(item, dict) and item.get("id"):
            rows.append({"id": str(item["id"]), "value": str(item.get("value") or "")})
    return rows


def _result(session: Any, *, applied: bool, error: str = "", question_id: str = "") -> dict[str, Any]:
    rows = questions_for(session)
    body: dict[str, Any] = {
        "ok": applied,
        "applied": applied,
        "id": question_id,
        "questions": rows,
        "open": len(rows),
        "problems": session.design_checks(),
    }
    if error:
        body["error"] = error
    return body


def _find(session: Any, question_id: str) -> dict[str, Any] | None:
    for row in questions_for(session):
        if row["id"] == question_id:
            return row
    return None


def _add(found: list[dict[str, Any]], seen: set[str], row: dict[str, Any], session: Any, prep: Path) -> None:
    if row["id"] in seen:
        return
    seen.add(row["id"])
    row["sheet"] = _project_sheet_name(session, str(row.get("sheet") or ""))
    if not str(row.get("image") or "").strip():
        row["image"] = _image_for_sheet(prep, str(row.get("sheet") or ""))
    row["crop"] = _write_crop(session, prep, row)
    for key in _QUESTION_KEYS:
        row.setdefault(key, "" if key != "candidates" else [])
    found.append(row)


def _question_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    detail = str(item.get("detail") or item.get("reason") or "")
    reason = str(item.get("reason") or "")
    kind = str(item.get("kind") or "")
    ref = str(item.get("tag") or item.get("text") or "")
    sheet = str(item.get("sheet") or "")
    bbox = item.get("bbox") if isinstance(item.get("bbox"), dict) else None
    image = str(item.get("image") or "")
    candidates = [str(value) for value in item.get("candidates") or [] if str(value).strip()]
    if "confirm this tag" in detail or "did not read this tag" in detail:
        ocr = ""
        found = re.search(r"OCR read [^.]+\.?", detail)
        if found:
            ocr = found.group(0).strip()
            if not ocr.endswith("."):
                ocr += "."
        return _row(
            "tag",
            sheet,
            ref,
            question=f"PanelCAD's text did not read {ref}. {ocr} Confirm the tag printed here.".replace("  ", " ").strip(),
            candidates=candidates or ([ref] if ref else []),
            changes=(
                "Keeps this tag if you repeat it. "
                "A different tag renames the device. "
                "If that tag is already on the sheet, this reading is merged into that device. "
                "It does not invent a catalog part."
            ),
            bbox=bbox,
            image=image,
            x=item.get("x"),
            y=item.get("y"),
        )
    if reason == "contact_state" or "geometry reads" in detail or "neither is used" in detail:
        return _row(
            "contact",
            sheet,
            ref,
            question=f"Is {ref or 'this contact'} NO or NC? {detail}".strip(),
            candidates=candidates or ["NO", "NC"],
            changes="Records NO or NC on this device. It does not choose a catalog part.",
            bbox=bbox,
            image=image,
        )
    if "clipped" in detail or (reason == "not_in_evidence" and kind == "device"):
        longer = _mentioned(detail)
        if reason == "not_in_evidence" and "clipped" not in detail:
            note = detail.strip()
            if note.lower().startswith("tag "):
                rest = note[4:].strip()
                body = f"{ref} {rest}".strip() if ref else rest
            elif ref and note:
                body = f"{ref} {note}"
            else:
                body = ref or note or "The reading"
            question = f"What tag is printed here? {_sentence(body)}"
        else:
            question = f"What tag is printed here? {ref or 'The reading'} is incomplete. {_sentence(detail)}".strip()
        return _row(
            "tag",
            sheet,
            ref,
            question=question,
            candidates=candidates or ([longer] if longer else []),
            changes=(
                "Renames this device if it is on the sheet. "
                "A tag that was not placed becomes a needs-info mark with no millimetres. "
                "A tag that is already on the sheet merges this reading into that device. "
                "It stays off the BOM and the quote until a catalog number is answered."
            ),
            bbox=bbox,
            image=image,
            x=item.get("x"),
            y=item.get("y"),
        )
    if "no matching coil" in detail or "does not match its timer coil" in detail:
        return _row(
            "coil",
            sheet,
            ref,
            question=f"Which coil does {ref} belong to? {detail}".strip(),
            candidates=candidates,
            changes=(
                "Points this contact at a coil that is already on the drawing, "
                "or keeps the tag if you repeat it. It does not create a coil."
            ),
            bbox=bbox,
            image=image,
        )
    if kind == "wire":
        return _wire_row(sheet, ref, detail, item, bbox, image, candidates)
    if reason in {"needs_info", "unknown"} and kind == "device":
        read = str(item.get("text") or "").strip()
        note = str(item.get("detail") or "").strip()
        if note.lower() in {"needs_info", "unknown"}:
            note = ""
        bits = []
        if read and read != ref:
            bits.append(f"The print was read as {read}, which is not in the library.")
        if note:
            bits.append(note if note.endswith(".") else note + ".")
        extra = (" " + " ".join(bits)) if bits else ""
        return _row(
            "part",
            sheet,
            ref,
            question=f"What catalog number is printed for {ref}?{extra} Leave the question unanswered if you don't know.",
            candidates=candidates,
            changes=(
                "Replaces the needs-info mark only when this exact catalog number is in the library "
                "and has published width and height. Otherwise the part stays needs-info, off the BOM and the quote."
            ),
            bbox=bbox,
            image=image,
        )
    if kind == "device" and ref:
        return _row(
            "tag",
            sheet,
            ref,
            question=f"Confirm {ref}. {detail}".strip(),
            candidates=candidates or [ref],
            changes=(
                "Keeps this tag if you repeat it. "
                "A different tag renames the device or merges into one that is already on the sheet."
            ),
            bbox=bbox,
            image=image,
            x=item.get("x"),
            y=item.get("y"),
        )
    return None


def _question_from_component(session: Any, sheet: Any, comp: Any, seen: set[str]) -> dict[str, Any] | None:
    from panelcad.reconstruct.placeholder import is_needs_info_component

    props = comp.properties or {}
    if str(props.get("contact_needs_info") or "").lower() in {"1", "true", "yes"}:
        row = _row(
            "contact",
            sheet.id,
            comp.id,
            question=f"Is {comp.id} NO or NC? {props.get('contact_review') or ''}".strip(),
            candidates=["NO", "NC"],
            changes="Records NO or NC on this device. It does not choose a catalog part.",
            bbox=_box_from_props(props),
        )
        if row["id"] not in seen:
            return row
    if is_needs_info_component(comp) and str(props.get("print_xref") or "").lower() not in {"1", "true", "yes"}:
        part_id = _qid("part", sheet.id, comp.id)
        if part_id not in seen:
            added = str(props.get("print_added_from_text") or "").lower() in {"1", "true", "yes"}
            confirmed = str(props.get("print_device_confirmed") or "").lower() in {"1", "true", "yes"}
            if added and not confirmed:
                return _row(
                    "device",
                    sheet.id,
                    comp.id,
                    question=f"{comp.id} is printed here but was not read as a device. Add it?",
                    candidates=["yes", "no"],
                    changes=(
                        "Yes keeps this mark and then asks for its catalog number. "
                        "No removes the mark and reopens the wire question."
                    ),
                    bbox=_box_from_props(props),
                )
            read = str(props.get("part_number") or "").strip()
            review = str(props.get("print_catalog_review") or "").strip()
            if review:
                extra = " " + (review if review.endswith(".") else review + ".")
            elif read:
                extra = f" The print was read as {read}, which is not in the library."
            else:
                extra = ""
            return _row(
                "part",
                sheet.id,
                comp.id,
                question=f"What catalog number is printed for {comp.id}?{extra}",
                candidates=[],
                changes=(
                    "Replaces the needs-info mark only when this exact catalog number is in the library "
                    "and has published width and height. Otherwise the part stays needs-info, off the BOM and the quote."
                ),
                bbox=_box_from_props(props),
            )
        pins = str(props.get("print_import_pins") or "").strip()
        if not pins and _wired(sheet, comp.id):
            return _row(
                "terminal",
                sheet.id,
                comp.id,
                question=f"Which terminal numbers are printed on {comp.id}?",
                candidates=[],
                changes="Records those terminal names. It does not invent a pin you do not name.",
            )
    return None


def _question_from_wire(sheet: Any, wire: Any) -> dict[str, Any]:
    number = str(wire.number or wire.id)
    return _row(
        "wire",
        sheet.id,
        number,
        question=(
            f"Wire {number} from {wire.frm} to {wire.to} is not confirmed. "
            "Answer both ends as 'device.terminal to device.terminal'."
        ),
        candidates=[wire.frm, wire.to],
        changes="Reconnects this wire to devices that are already on the sheet. It does not create a device.",
        wire_from=wire.frm,
        wire_to=wire.to,
        id=_qid("wire", sheet.id, number, _wire_extra(wire.frm, wire.to)),
    )


def _wire_row(
    sheet: str,
    ref: str,
    detail: str,
    item: dict[str, Any],
    bbox: dict[str, Any] | None,
    image: str,
    candidates: list[str],
) -> dict[str, Any]:
    frm = str(item.get("wire_from") or "")
    to = str(item.get("wire_to") or "")
    note = _sentence(detail)
    gap = f"{note} " if note else ""
    return _row(
        "wire",
        sheet,
        ref,
        question=(
            f"Wire {ref or 'without a number'} is incomplete. {gap}"
            "Answer both ends as 'device.terminal to device.terminal', "
            "or one device.terminal for the end that is wrong."
        ).strip(),
        candidates=candidates or [value for value in (frm, to) if value],
        changes=(
            "Adds this row when it is not on the sheet. "
            "A different row with the same number is left as it is. "
            "It does not invent a device that you do not name."
        ),
        bbox=bbox,
        image=image,
        wire_from=frm,
        wire_to=to,
        id=_qid("wire", sheet, ref, _wire_extra(frm, to)),
    )


def _row(kind: str, sheet: str, ref: str, **fields: Any) -> dict[str, Any]:
    body = {
        "id": _qid(kind, sheet, ref),
        "kind": kind,
        "sheet": sheet,
        "ref": ref,
        "question": "",
        "bbox": None,
        "image": "",
        "crop": "",
        "candidates": [],
        "changes": "",
    }
    body.update(fields)
    if fields.get("id"):
        body["id"] = str(fields["id"])
    return body


def _sentence(detail: str) -> str:
    """One capitalized sentence, so a reason does not run into the next clause."""

    text = str(detail or "").strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "."
    return text


def _project_sheet_name(session: Any, sheet_id: str) -> str:
    """The project sheet id. Prep pages use page_001; the project uses page-001."""

    found = _sheet_by_id(session, sheet_id)
    if found is not None:
        return str(found.id)
    return sheet_id.replace("_", "-")


def _qid(kind: str, sheet: str, ref: str, extra: str = "") -> str:
    raw = f"{kind}-{str(sheet).replace('_', '-')}-{ref}"
    if extra:
        raw = f"{raw}-{extra}"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return f"q-{slug}"[:96]


def _wire_extra(frm: str, to: str) -> str:
    """A short id for one wire row, so two rows of one number stay distinct."""

    if not frm and not to:
        return ""
    import hashlib

    ends = "|".join(sorted((str(frm or ""), str(to or ""))))
    return hashlib.sha1(ends.encode("utf-8")).hexdigest()[:6]


def _mentioned(detail: str) -> str:
    match = re.search(r"extracted text has (\S+)", detail)
    return match.group(1).rstrip(".") if match else ""


def _wired(sheet: Any, ref: str) -> bool:
    prefix = ref + "."
    return any(wire.frm.startswith(prefix) or wire.to.startswith(prefix) for wire in sheet.wires)


def _box_from_props(props: dict[str, Any]) -> dict[str, Any] | None:
    raw = props.get("agent_bbox")
    return raw if isinstance(raw, dict) else None


def _image_for_sheet(prep: Path, sheet: str) -> str:
    manifest_path = prep / "manifest.json"
    if not sheet or not manifest_path.is_file():
        return ""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    want = sheet.replace("-", "_")
    for page in manifest.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id") or "")
        if page_id == sheet or page_id.replace("-", "_") == want:
            return str(page.get("image") or "")
    return ""


def _crop_root(session: Any, prep: Path) -> Path:
    if session.path:
        project = Path(session.path)
        return project.parent / f"{project.stem}.agent-questions"
    return prep / "agent-questions"


def _write_crop(session: Any, prep: Path, row: dict[str, Any]) -> str:
    bbox = row.get("bbox")
    image_rel = str(row.get("image") or "")
    if not isinstance(bbox, dict) or not image_rel or not prep.is_dir():
        return ""
    image = prep / image_rel
    meta_path = image.with_suffix(".json")
    if not image.is_file() or not meta_path.is_file():
        return ""
    try:
        stat = image.stat()
        stamp = f"{image_rel}|{stat.st_mtime_ns}|{stat.st_size}|{bbox['x']}|{bbox['y']}|{bbox['w']}|{bbox['h']}"
    except (OSError, KeyError, TypeError):
        return ""
    import hashlib

    digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:12]
    dest_root = _crop_root(session, prep)
    dest = dest_root / f"{row['id']}-{digest}.png"
    if dest.is_file():
        return str(dest)
    try:
        from PIL import Image

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        width_mm = float(meta.get("width_mm") or 0)
        height_mm = float(meta.get("height_mm") or 0)
        if width_mm <= 0 or height_mm <= 0:
            return ""
        picture = Image.open(image)
        x = float(bbox["x"])
        y = float(bbox["y"])
        width = float(bbox["w"])
        height = float(bbox["h"])
        if width <= 0 or height <= 0:
            return ""
        pad = 2.0
        scale_x = picture.width / width_mm
        scale_y = picture.height / height_mm
        x0 = max(0, int((x - pad) * scale_x))
        y0 = max(0, int((y - pad) * scale_y))
        x1 = min(picture.width, int((x + width + pad) * scale_x + 0.999))
        y1 = min(picture.height, int((y + height + pad) * scale_y + 0.999))
        if x1 <= x0 or y1 <= y0:
            return ""
        dest.parent.mkdir(parents=True, exist_ok=True)
        picture.crop((x0, y0, x1, y1)).save(dest)
        own = re.compile(rf"^{re.escape(str(row['id']))}-[0-9a-f]{{12}}\.png$")
        for old in dest_root.iterdir():
            if old.name != dest.name and own.match(old.name):
                old.unlink(missing_ok=True)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    return str(dest)


def _confirm(session: Any, question: dict[str, Any]) -> None:
    payload = dict(session.project.properties.get("print_import") or {})
    items = list(payload.get("items") or [])
    want = question["id"]
    for item in items:
        if item.get("confirmed"):
            continue
        built = _question_from_item(item)
        if built is not None and built["id"] == want:
            item["confirmed"] = True
    payload["items"] = items
    session.project.properties["print_import"] = payload


def _answer_contact(session: Any, question: dict[str, Any], value: str) -> None:
    state = value.strip().upper()
    if state not in {"NO", "NC"}:
        raise ValueError("answer NO or NC; the contact stays needs-info")
    _sheet, comp = _component(session, str(question.get("ref") or ""))
    if comp is None:
        raise ValueError(f"no device {question.get('ref')}")
    comp.properties["contact"] = state
    comp.properties.pop("contact_needs_info", None)
    comp.properties.pop("contact_review", None)


def _answer_tag(session: Any, question: dict[str, Any], value: str) -> None:
    tag = value.strip()
    if not tag or any(ch.isspace() for ch in tag):
        raise ValueError("a tag has no spaces; nothing was renamed")
    ref = str(question.get("ref") or "")
    sheet = _sheet_by_id(session, str(question.get("sheet") or ""))
    comp = _component_on(sheet, ref) if sheet is not None else None
    if comp is None:
        sheet, comp = _component(session, ref)
    if comp is None:
        _place_answered_tag(session, question, tag)
        return
    if tag == comp.id:
        _replay_held_wires(session)
        return
    target = _component_on(sheet, tag) if sheet is not None else None
    if target is not None:
        _merge_tag(session, sheet, comp, target)
        return
    session.rename_component(comp.id, tag, sheet_id=sheet.id if sheet is not None else None)
    _retire_replaced_tag(session, ref, tag)
    _replay_held_wires(session)


def _place_answered_tag(session: Any, question: dict[str, Any], tag: str) -> None:
    """A human named a tag that was never placed. It stays a needs-info mark.

    The same tag already on the sheet absorbs this reading. The question closes
    and a second device is not created.
    """

    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    sheet_id = _sheet_id(session, str(question.get("sheet") or ""))
    sheet = _sheet_by_id(session, sheet_id)
    target = _component_on(sheet, tag) if sheet is not None else None
    if target is not None:
        reading = str(question.get("ref") or "")
        _remember_merge(target, reading)
        _retire_replaced_tag(session, reading, tag)
        _replay_held_wires(session)
        return
    try:
        x = float(question.get("x"))
        y = float(question.get("y"))
    except (TypeError, ValueError):
        raise ValueError("no position was stored for that reading; the tag was not placed") from None
    session.place(
        NEEDS_INFO_LIBRARY_ID,
        ref=tag,
        x=x,
        y=y,
        sheet_id=sheet_id,
        snap=False,
        allow_needs_info=True,
        properties={
            "reconstruct_needs_info": "yes",
            "description": "INFO NEEDED",
            "label_text": tag,
            "needs_missing": "catalog number",
            "bom": "no",
            "in_panel": "no",
            "incabinet": "no",
            "print_import_anchor": "yes",
            "notes": "answered tag — needs a catalog part with published dimensions",
        },
    )
    _replay_held_wires(session)


def _component_on(sheet: Any, ref: str) -> Any:
    if sheet is None or not ref:
        return None
    folded = _fold_tag(ref)
    match = None
    for comp in sheet.components:
        if comp.id == ref:
            return comp
        if match is None and _fold_tag(comp.id) == folded:
            match = comp
    return match


def _merge_tag(session: Any, sheet: Any, source: Any, target: Any) -> None:
    """Fold a clipped reading into the device that already has the answered tag."""

    from panelcad.importer.build import _remember_needs_info_pin
    from panelcad.reconstruct.placeholder import is_needs_info_component

    old = source.id
    new = target.id
    prefix = old + "."
    new_prefix = new + "."
    for wire in sheet.wires:
        if wire.frm.startswith(prefix):
            wire.frm = new_prefix + wire.frm[len(prefix) :]
        if wire.to.startswith(prefix):
            wire.to = new_prefix + wire.to[len(prefix) :]
    if is_needs_info_component(target):
        pins = str(source.properties.get("print_import_pins") or "")
        for name in [part.strip() for part in pins.split(",") if part.strip()]:
            _remember_needs_info_pin(target, name)
    _remember_merge(target, old)
    _retire_replaced_tag(session, old, new)
    session.delete_component(old, sheet_id=sheet.id)
    _replay_held_wires(session)


def _swap_end(end: str, old: str, new: str) -> str:
    text = str(end or "")
    if not old or not new or old == new or "." not in text:
        return text
    device, pin = text.split(".", 1)
    if device == old:
        return f"{new}.{pin}"
    return text


def _names_tag(item: dict[str, Any], tag: str) -> bool:
    if not tag:
        return False
    for key in ("tag", "text", "id", "ref"):
        if str(item.get(key) or "") == tag and str(item.get("kind") or "") != "wire":
            return True
    return str(item.get("tag") or "") == tag or (
        str(item.get("kind") or "") != "wire" and str(item.get("text") or "") == tag
    )


def _retire_replaced_tag(session: Any, old: str, new: str) -> None:
    """Close questions for a tag that was merged away, and point held wires at the survivor."""

    if not old or old == new:
        return
    payload = dict(session.project.properties.get("print_import") or {})
    items: list[dict[str, Any]] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if str(row.get("kind") or "") == "wire":
            row["wire_from"] = _swap_end(str(row.get("wire_from") or ""), old, new)
            row["wire_to"] = _swap_end(str(row.get("wire_to") or ""), old, new)
        elif _names_tag(row, old):
            row["confirmed"] = True
        items.append(row)
    held: list[dict[str, Any]] = []
    for item in payload.get("held_wires") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["from"] = _swap_end(str(row.get("from") or ""), old, new)
        row["to"] = _swap_end(str(row.get("to") or ""), old, new)
        held.append(row)
    payload["items"] = items
    payload["held_wires"] = held
    session.project.properties["print_import"] = payload


def _replay_held_wires(session: Any) -> None:
    """Place held wires once both endpoint devices exist. Do not rewrite another row."""

    payload = dict(session.project.properties.get("print_import") or {})
    kept: list[dict[str, Any]] = []
    for item in payload.get("held_wires") or []:
        if not isinstance(item, dict):
            continue
        sheet = _sheet_by_id(session, str(item.get("sheet") or ""))
        frm = str(item.get("from") or "")
        to = str(item.get("to") or "")
        number = str(item.get("number") or "")
        if sheet is None:
            kept.append(dict(item))
            continue
        _copy_known_end(session, sheet, frm, item.get("bbox"))
        _copy_known_end(session, sheet, to, item.get("bbox"))
        if not _ends_ready(sheet, frm, to):
            kept.append(dict(item))
            continue
        if _other_number_on_ends(sheet, number, frm, to) is not None and _wire_number_and_ends(sheet, number, frm, to) is None:
            kept.append(dict(item))
            _mark_pin_conflict(payload, number, frm, to)
            continue
        if _wire_number_and_ends(sheet, number, frm, to) is None:
            _require_end(session, sheet, frm)
            _require_end(session, sheet, to)
            created = session.wire(frm, to, sheet_id=sheet.id, number=number or None)
            created.inferred = False
        _confirm_held_question(payload, number, frm, to)
    payload["held_wires"] = kept
    session.project.properties["print_import"] = payload


def _ends_ready(sheet: Any, frm: str, to: str) -> bool:
    if "." not in frm or "." not in to:
        return False
    left, right = frm.split(".", 1)[0], to.split(".", 1)[0]
    ids = {comp.id for comp in sheet.components}
    return left in ids and right in ids


def _wire_with_ends(sheet: Any, frm: str, to: str) -> Any:
    for wire in sheet.wires:
        if {wire.frm, wire.to} == {frm, to}:
            return wire
    return None


def _copy_known_end(session: Any, sheet: Any, endpoint: str, bbox: Any) -> None:
    """A tag that already exists on another sheet is marked needs-info on this sheet."""

    if "." not in str(endpoint or ""):
        return
    ref = endpoint.split(".", 1)[0]
    found = _canonical_component(session, ref)
    if found is None or _component_on(sheet, found.id) is not None:
        return
    _place_needs_info_tag(session, sheet, found.id, bbox, xref=True)


def _place_needs_info_tag(session: Any, sheet: Any, tag: str, bbox: Any, *, xref: bool) -> None:
    from panelcad.reconstruct.placeholder import NEEDS_INFO_LIBRARY_ID

    if _component_on(sheet, tag) is not None:
        return
    x, y = 40.0, 40.0
    if isinstance(bbox, dict):
        try:
            x = float(bbox.get("x") or 40) + float(bbox.get("w") or 0) / 2.0
            y = float(bbox.get("y") or 40) + float(bbox.get("h") or 0) / 2.0
        except (TypeError, ValueError):
            x, y = 40.0, 40.0
    props: dict[str, Any] = {
        "reconstruct_needs_info": "yes",
        "description": "INFO NEEDED",
        "label_text": tag,
        "needs_missing": "catalog number",
        "bom": "no",
        "in_panel": "no",
        "incabinet": "no",
        "print_import_anchor": "yes",
    }
    if xref:
        props["print_xref"] = "yes"
        props["notes"] = "print import — endpoint copied onto this sheet"
    else:
        props["print_added_from_text"] = "yes"
        props["notes"] = "print import — needs a catalog part with published dimensions"
        if isinstance(bbox, dict):
            props["agent_bbox"] = bbox
    session.place(
        NEEDS_INFO_LIBRARY_ID,
        ref=tag,
        x=x,
        y=y,
        sheet_id=sheet.id,
        snap=False,
        allow_needs_info=True,
        properties=props,
    )


def _ensure_end(session: Any, sheet: Any, endpoint: str, bbox: Any) -> str:
    """Return device.terminal using the tag already stored on the project.

    A copy is only a cross-reference when that device is already on another
    sheet. A tag that is only in the extracted text is a new needs-info device,
    with its own part question. A wire number, title-block token, single letter,
    or bare number is not a device.
    """

    if "." not in endpoint:
        raise ValueError(f"{endpoint} is not device.terminal")
    ref, pin = endpoint.split(".", 1)
    ref = ref.strip()
    pin = pin.strip()
    found = _canonical_component(session, ref)
    if found is not None:
        tag = found.id
        if _component_on(sheet, tag) is None:
            _place_needs_info_tag(session, sheet, tag, bbox, xref=True)
    else:
        if not _tag_in_extracted_text(session, ref) or _tag_unusable(session, ref):
            raise ValueError(f"no device {ref} on this print; check the tag")
        tag = _printed_tag(session, ref)
        _place_needs_info_tag(session, sheet, tag, bbox, xref=False)
    resolved = f"{tag}.{pin}"
    _require_end(session, sheet, resolved)
    return resolved


def _tag_in_extracted_text(session: Any, ref: str) -> bool:
    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    prep = Path(str(payload.get("prep_dir") or ""))
    manifest_path = prep / "manifest.json"
    if not ref or not manifest_path.is_file():
        return False
    want = re.sub(r"\s+", "", ref).upper()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for page in manifest.get("pages") or []:
        if not isinstance(page, dict):
            continue
        path = prep / str(page.get("text") or "")
        if not path.is_file():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for token in body.get("text") or []:
            if isinstance(token, dict) and re.sub(r"\s+", "", str(token.get("text") or "")).upper() == want:
                return True
    return False


def _mark_pin_conflict(payload: dict[str, Any], number: str, frm: str, to: str) -> None:
    note = f"these ends already use another wire number; confirm wire {number}"
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "wire":
            continue
        if number and str(item.get("text") or "") != number:
            continue
        ends = {str(item.get("wire_from") or ""), str(item.get("wire_to") or "")}
        if ends != {frm, to}:
            continue
        item["confirmed"] = False
        detail = str(item.get("detail") or "")
        if note not in detail:
            item["detail"] = f"{detail} {note}".strip()


def _confirm_held_question(payload: dict[str, Any], number: str, frm: str, to: str) -> None:
    for item in payload.get("items") or []:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "wire":
            continue
        if number and str(item.get("text") or "") != number:
            continue
        ends = {str(item.get("wire_from") or ""), str(item.get("wire_to") or "")}
        if ends == {frm, to}:
            item["confirmed"] = True


def _drop_held_wire(session: Any, number: str, frm: str, to: str) -> None:
    payload = dict(session.project.properties.get("print_import") or {})
    kept = []
    for item in payload.get("held_wires") or []:
        if not isinstance(item, dict):
            continue
        same_number = not number or str(item.get("number") or "") == number
        same_ends = {str(item.get("from") or ""), str(item.get("to") or "")} == {frm, to} if frm and to else False
        if same_number and (same_ends or not frm):
            continue
        kept.append(item)
    payload["held_wires"] = kept
    session.project.properties["print_import"] = payload


def _remember_merge(comp: Any, reading: str) -> None:
    text = str(reading or "").strip()
    if text and text != comp.id:
        note = f"reading {text} was confirmed as this tag"
    else:
        note = "another print reading was confirmed as this tag"
    existing = str(comp.properties.get("notes") or "")
    if note not in existing:
        comp.properties["notes"] = f"{existing}; {note}".strip("; ").strip()


def _answer_part(session: Any, question: dict[str, Any], value: str) -> None:
    from panelcad.needs_info import resolve_with_catalog

    manufacturer, catalog = _split_catalog(value)
    try:
        part = session.parts.get(catalog, manufacturer or None)
    except KeyError as exc:
        raise ValueError(str(exc).splitlines()[0] + "; the part stays needs-info") from None
    if not part.has_footprint():
        raise ValueError("catalog part has no published dimensions; the part stays needs-info")
    result = resolve_with_catalog(session, str(question.get("ref") or ""), part)
    if not result.get("applied"):
        raise ValueError(str(result.get("reason") or "part was not replaced") + "; the part stays needs-info")


def _answer_wire(session: Any, question: dict[str, Any], value: str) -> None:
    """Add the answered row, or confirm it. Never rewrite a different row."""

    sheet = _sheet_of(session, str(question.get("sheet") or ""))
    both = _both_ends(value)
    if both:
        frm, to = both
    else:
        wire = _find_wire(sheet, question)
        frm, to = _parse_ends(value, question, wire)
    frm = _ensure_end(session, sheet, frm, question.get("bbox"))
    to = _ensure_end(session, sheet, to, question.get("bbox"))
    _remember_added_wire(sheet, frm, question)
    _remember_added_wire(sheet, to, question)
    number = str(question.get("ref") or "").strip()
    existing = _wire_number_and_ends(sheet, number, frm, to)
    if existing is None:
        created = session.wire(frm, to, sheet_id=sheet.id, number=number or None)
        created.inferred = False
    else:
        existing.inferred = False
        if number:
            existing.number = number
    _drop_held_wire(session, number, str(question.get("wire_from") or frm), str(question.get("wire_to") or to))


def _answer_terminal(session: Any, question: dict[str, Any], value: str) -> None:
    from panelcad.importer.build import _remember_needs_info_pin

    names = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
    if not names:
        raise ValueError("name at least one terminal; nothing was recorded")
    _sheet, comp = _component(session, str(question.get("ref") or ""))
    if comp is None:
        raise ValueError(f"no device {question.get('ref')}")
    for name in names:
        _remember_needs_info_pin(comp, name)


def _answer_coil(session: Any, question: dict[str, Any], value: str) -> None:
    ref = str(question.get("ref") or "")
    tag = value.strip()
    if tag == ref:
        return
    sheet, comp = _component(session, ref)
    if comp is None:
        raise ValueError(f"no device {ref}")
    target_sheet, target = _component(session, tag)
    if target is None:
        raise ValueError(f"no coil {tag} on the drawing; nothing was created")
    if sheet is None or target_sheet is None:
        raise ValueError(f"no coil {tag} on the drawing; nothing was created")
    prefix = comp.id + "."
    new_prefix = target.id + "."
    for page in session.project.sheets:
        for wire in page.wires:
            if wire.frm.startswith(prefix):
                wire.frm = new_prefix + wire.frm[len(prefix) :]
            if wire.to.startswith(prefix):
                wire.to = new_prefix + wire.to[len(prefix) :]
    session.delete_component(comp.id, sheet_id=sheet.id)


def _split_catalog(value: str) -> tuple[str, str]:
    parts = value.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _parse_ends(value: str, question: dict[str, Any], wire: Any) -> tuple[str, str]:
    match = re.split(r"\s+to\s+", value.strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(match) == 2 and "." in match[0] and "." in match[1]:
        return match[0].strip(), match[1].strip()
    if wire is not None and value.count(".") == 1 and " " not in value.strip():
        side = str(question.get("open_end") or "")
        if side == "from":
            return value.strip(), wire.to
        if side == "to":
            return wire.frm, value.strip()
    raise ValueError("answer both ends as 'device.terminal to device.terminal'")


def _require_end(session: Any, sheet: Any, endpoint: str) -> None:
    from panelcad.importer.build import _remember_needs_info_pin

    if "." not in endpoint:
        raise ValueError(f"{endpoint} is not device.terminal")
    ref, pin = endpoint.split(".", 1)
    try:
        comp = sheet.component(ref)
    except KeyError as exc:
        raise ValueError(f"no device {ref} on the sheet; it was not created") from exc
    device = session.catalog.get(comp.library_id)
    try:
        device.pin(pin)
    except KeyError:
        _remember_needs_info_pin(comp, pin)


def _component(session: Any, ref: str) -> tuple[Any, Any]:
    found = _canonical_component(session, ref)
    if found is None:
        return None, None
    for sheet in session.project.sheets:
        if any(comp.id == found.id for comp in sheet.components):
            return sheet, found
    return None, found


def _canonical_component(session: Any, ref: str) -> Any:
    """The device with this tag, ignoring case. The stored id is kept."""

    want = _fold_tag(ref)
    folded = None
    for sheet in session.project.sheets:
        for comp in sheet.components:
            if comp.id == ref:
                return comp
            if folded is None and _fold_tag(comp.id) == want:
                folded = comp
    return folded


def _fold_tag(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).upper()


def _tag_body(folded: str) -> str:
    if folded.startswith("-") and len(folded) > 1:
        return folded[1:]
    return folded


def _tag_unusable(session: Any, ref: str) -> bool:
    """A wire number, title-block token, list, single letter, or bare number is not a device."""

    raw = str(ref or "").strip()
    if any(mark in raw for mark in (",", ":", "/")):
        return True
    folded = _fold_tag(raw)
    body = _tag_body(folded)
    if not any(char.isalpha() for char in body):
        return True
    if len(body) == 1 and body.isalpha():
        return True
    if folded in _wire_numbers(session) or folded in _title_values(session):
        return True
    matches = [(token, page) for token, page in _matching_tokens(session, folded) if _token_has_point(token)]
    if matches and all(_token_excluded(token, page) for token, page in matches):
        return True
    return False


def _wire_numbers(session: Any) -> set[str]:
    found: set[str] = set()
    for sheet in session.project.sheets:
        for wire in sheet.wires:
            number = _fold_tag(getattr(wire, "number", "") or "")
            if number:
                found.add(number)
    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    for item in payload.get("items") or []:
        if isinstance(item, dict) and str(item.get("kind") or "") == "wire":
            number = _fold_tag(item.get("text") or "")
            if number:
                found.add(number)
    for item in payload.get("held_wires") or []:
        if isinstance(item, dict):
            number = _fold_tag(item.get("number") or "")
            if number:
                found.add(number)
    return found


def _title_values(session: Any) -> set[str]:
    block = getattr(session.project, "title_block", None)
    found: set[str] = set()
    if block is not None:
        for name in ("drawing_number", "rev", "company", "customer", "location", "drawn_by", "address"):
            value = _fold_tag(getattr(block, name, "") or "")
            if value:
                found.add(value)
    seen: set[int] = set()
    for token, page in _page_tokens(session):
        for key in ("drawing_number", "sheet_number"):
            value = _fold_tag(token.get(key) or "")
            if value:
                found.add(value)
        mark = id(page)
        if mark in seen or not isinstance(page, dict):
            continue
        seen.add(mark)
        from panelcad.importer.agent_exchange import _title_block_identifiers

        for value in _title_block_identifiers(page):
            folded = _fold_tag(value)
            if folded:
                found.add(folded)
    return found


def _printed_tag(session: Any, ref: str) -> str:
    want = _fold_tag(ref)
    for token, _page in _page_tokens(session):
        text = str(token.get("text") or "").strip()
        if text and _fold_tag(text) == want:
            return text
    return ref.strip()


def _page_tokens(session: Any):
    payload = dict(getattr(session.project, "properties", {}).get("print_import") or {})
    prep = Path(str(payload.get("prep_dir") or ""))
    manifest_path = prep / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for page in manifest.get("pages") or []:
        if not isinstance(page, dict):
            continue
        path = prep / str(page.get("text") or "")
        if not path.is_file():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        record = dict(body)
        record["_source_path"] = str(path)
        try:
            record["_source_mtime"] = path.stat().st_mtime_ns
        except OSError:
            record["_source_mtime"] = 0
        try:
            record["width_mm"] = float(body.get("width_mm") or page.get("width_mm") or 0)
            record["height_mm"] = float(body.get("height_mm") or page.get("height_mm") or 0)
        except (TypeError, ValueError):
            record["width_mm"] = 0.0
            record["height_mm"] = 0.0
        record["token_rows"] = [item for item in body.get("text") or [] if isinstance(item, dict)]
        for key in ("drawing_number", "sheet_number"):
            value = str(body.get(key) or "").strip()
            if value:
                yield {"text": value, key: value}, record
        for token in record["token_rows"]:
            yield token, record


def _matching_tokens(session: Any, folded: str):
    for token, page in _page_tokens(session):
        if _fold_tag(token.get("text") or "") == folded:
            yield token, page


def _token_has_point(token: dict[str, Any]) -> bool:
    try:
        float(token["x"])
        float(token["y"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _token_excluded(token: dict[str, Any], page: dict[str, Any]) -> bool:
    """True when this token sits in the print's own title block or border."""

    from panelcad.importer.agent_exchange import _excluded_boxes, _point_in_any_box

    try:
        x = float(token["x"]) + float(token.get("w") or 0) / 2.0
        y = float(token["y"]) + float(token.get("h") or 0) / 2.0
    except (KeyError, TypeError, ValueError):
        return False
    return _point_in_any_box(x, y, _excluded_boxes(page))


def _remember_added_wire(sheet: Any, endpoint: str, question: dict[str, Any]) -> None:
    ref = str(endpoint or "").split(".", 1)[0]
    comp = _component_on(sheet, ref)
    if comp is None:
        return
    props = comp.properties
    if str(props.get("print_added_from_text") or "").lower() not in {"1", "true", "yes"}:
        return
    qid = str(question.get("id") or "")
    if not qid:
        return
    stored = [part for part in str(props.get("print_added_questions") or "").split(",") if part]
    if qid not in stored:
        stored.append(qid)
    props["print_added_questions"] = ",".join(stored)


def _answer_device(session: Any, question: dict[str, Any], value: str) -> None:
    """Yes keeps a text-only mark. No removes it and reopens the wire."""

    ref = str(question.get("ref") or "")
    sheet, comp = _component(session, ref)
    if comp is None or sheet is None:
        raise ValueError(f"no device {ref}")
    text = value.strip().casefold().rstrip(".")
    if text in {"no", "not a device", "no, not a device", "no not a device"}:
        stored = [part for part in str(comp.properties.get("print_added_questions") or "").split(",") if part]
        old = str(comp.properties.get("print_added_question") or "")
        if old and old not in stored:
            stored.append(old)
        session.delete_component(comp.id, sheet_id=sheet.id)
        for question_id in stored:
            _reopen_question(session, question_id)
        return
    if text in {"yes", "y", "add", ref.casefold()}:
        comp.properties["print_device_confirmed"] = "yes"
        return
    raise ValueError("answer yes to add the device, or no if it is not a device")


def _reopen_question(session: Any, question_id: str) -> None:
    if not question_id:
        return
    payload = dict(session.project.properties.get("print_import") or {})
    items = list(payload.get("items") or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        built = _question_from_item(item)
        if built is not None and built["id"] == question_id:
            item["confirmed"] = False
    payload["items"] = items
    session.project.properties["print_import"] = payload


def _sheet_of(session: Any, sheet_id: str) -> Any:
    sheet = _sheet_by_id(session, sheet_id)
    if sheet is None:
        raise ValueError(f"no sheet {sheet_id}")
    return sheet


def _sheet_id(session: Any, sheet_id: str) -> str:
    sheet = _sheet_by_id(session, sheet_id)
    if sheet is None:
        raise ValueError(f"no sheet {sheet_id}")
    return sheet.id


def _sheet_by_id(session: Any, sheet_id: str) -> Any:
    want = sheet_id.replace("_", "-")
    for sheet in session.project.sheets:
        if sheet.id == sheet_id or sheet.id.replace("_", "-") == want:
            return sheet
    return None


def _both_ends(value: str) -> tuple[str, str] | None:
    match = re.split(r"\s+to\s+", str(value or "").strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(match) == 2 and "." in match[0] and "." in match[1]:
        return match[0].strip(), match[1].strip()
    return None


def _find_wire(sheet: Any, question: dict[str, Any]) -> Any:
    """The row this question names, or None when that row was never placed.

    A different row that only shares the wire number is left alone.
    """

    number = str(question.get("ref") or "")
    frm = str(question.get("wire_from") or "")
    to = str(question.get("wire_to") or "")
    if frm and to:
        return _wire_number_and_ends(sheet, number, frm, to)
    matches = [wire for wire in sheet.wires if number and str(wire.number or wire.id) == number]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"wire {number} has more than one row; answer both ends")
    return None


def _wire_number_and_ends(sheet: Any, number: str, frm: str, to: str) -> Any:
    want = {frm, to}
    for wire in sheet.wires:
        if {wire.frm, wire.to} != want:
            continue
        if number and str(wire.number or "") != number and wire.id != number:
            continue
        return wire
    return None


def _other_number_on_ends(sheet: Any, number: str, frm: str, to: str) -> Any:
    want = {frm, to}
    for wire in sheet.wires:
        if {wire.frm, wire.to} != want:
            continue
        if number and str(wire.number or "") == number:
            continue
        return wire
    return None
