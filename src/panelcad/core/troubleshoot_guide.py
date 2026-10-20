"""Guided fault-finding shared by the Troubleshoot dock and by an AI.

The checks, voltages, wire numbers, and causes come from the drawing. This
module only chooses the next check and the words around it. It does not invent
a voltage, a terminal, or a PLC address.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from panelcad.core.models import Project
from panelcad.core.troubleshoot import (
    SYMPTOMS,
    _parse_readings,
    checkpoints,
    circuit_for,
    likely_causes,
    safety_block,
)
from panelcad.core.xref_index import device_index, device_index_rows
from panelcad.library.catalog import Catalog

HOW = (
    "Lock out and tag out first. The highlighted check is the next one, in circuit order. "
    "OK means that point is good. Wrong limits the fault to the path between "
    "the last OK and this check. Skipped leaves that point for later. "
    "Then save the log for the job file."
)

CSV_COLUMNS = (
    "when",
    "who",
    "ref",
    "symptom",
    "symptom_title",
    "row_kind",
    "order",
    "energized",
    "device",
    "terminal",
    "wire",
    "page_zone",
    "meter_leads",
    "expected",
    "needs_info",
    "result",
    "cause",
    "note",
    "safety",
)


def device_choices(project: Project, catalog: Catalog) -> list[dict[str, str]]:
    """One picker row per tag on the cross-reference index."""

    index = device_index(project, catalog)
    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in index.get("devices") or []:
        tag = str(group.get("device") or "").strip()
        if not tag or tag.casefold() in seen:
            continue
        seen.add(tag.casefold())
        places: list[str] = []
        for row in group.get("elements") or []:
            where = str(row.get("location") or row.get("page_zone") or "").strip()
            element = str(row.get("element") or row.get("role") or "").strip()
            bit = " ".join(part for part in (where, element) if part)
            if bit and bit not in places:
                places.append(bit)
        summary = "; ".join(places[:3])
        label = f"{tag} — {summary}" if summary else tag
        choices.append({"ref": tag, "label": label, "where": summary})
    if choices:
        choices.sort(key=lambda row: row["ref"].casefold())
        return choices
    for sheet in project.sheets:
        for comp in sheet.components:
            if comp.id.casefold() in seen:
                continue
            seen.add(comp.id.casefold())
            choices.append({"ref": comp.id, "label": comp.id, "where": sheet.title})
    choices.sort(key=lambda row: row["ref"].casefold())
    return choices


def guide(
    project: Project,
    catalog: Catalog,
    ref: str,
    symptom: str,
    readings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The walk the dock shows. The same payload is what an AI should read."""

    circuit = circuit_for(project, catalog, ref)
    symptom_id = circuit_symptom(symptom)
    check_payload = checkpoints(project, catalog, circuit["ref"], symptom_id)
    cause_payload = likely_causes(project, catalog, circuit["ref"], symptom_id, readings)
    checks = list(check_payload["checks"])
    answered = _answered(checks, readings)
    current, role = _next_check(checks, answered)
    fault = cause_payload.get("fault_between") or {}
    where = _xref_lines(project, catalog, circuit["ref"])
    path = _path_line(circuit)
    prompt = _prompt(current, role, fault, check_payload)
    marked = sum(1 for row in checks if row["id"] in answered)
    return {
        "op": "troubleshoot_guide",
        "read_only": True,
        "safety": safety_block(),
        "ref": circuit["ref"],
        "symptom": symptom_id,
        "symptom_title": SYMPTOMS[symptom_id]["title"],
        "how": HOW,
        "where": where,
        "path": path,
        "cross_refs": where,
        "checks": checks,
        "results": answered,
        "progress": f"{marked} of {len(checks)} checks marked",
        "next_check": current,
        "next_role": role,
        "instruction": prompt,
        "fault_between": fault,
        "fault_text": _fault_text(fault),
        "causes": list(cause_payload.get("causes") or []),
        "highlight": _highlight(
            circuit,
            current,
            checks=checks,
            window=list(cause_payload.get("window") or []),
        ),
        "needs_info": list(check_payload.get("needs_info") or []),
        "count": len(checks),
    }


def circuit_symptom(symptom: str) -> str:
    """Symptom id, using the same names the checkpoint API accepts."""

    from panelcad.core.troubleshoot import normalize_symptom

    return normalize_symptom(symptom)


def session_rows(
    payload: dict[str, Any],
    *,
    who: str = "",
    note: str = "",
    when: str = "",
) -> list[dict[str, str]]:
    """One CSV row per check, then one row per ranked cause."""

    stamp = _exported_when(when)
    safety = str((payload.get("safety") or {}).get("loto") or "")
    results = _result_by_id(payload)
    common = {
        "when": stamp,
        "who": who.strip(),
        "ref": str(payload.get("ref") or ""),
        "symptom": str(payload.get("symptom") or ""),
        "symptom_title": str(payload.get("symptom_title") or ""),
        "note": note.strip(),
        "safety": safety,
    }
    rows: list[dict[str, str]] = []
    for check in payload.get("checks") or []:
        expected = check.get("expected") or {}
        text = expected.get("text") or expected.get("value") or ""
        if expected.get("needs_info"):
            text = "needs info"
        plc = check.get("plc") or {}
        plc_bit = ""
        if plc:
            plc_bit = " ".join(
                part
                for part in (
                    str(plc.get("address") or ""),
                    str(plc.get("tag") or ""),
                )
                if part
            )
        meter = str(check.get("meter_leads") or "")
        if plc_bit:
            meter = f"{meter}  PLC {plc_bit}".strip()
        rows.append(
            {
                **common,
                "row_kind": "check",
                "order": str(check.get("order") or ""),
                "energized": str(check.get("energized") or ""),
                "device": str(check.get("device") or ""),
                "terminal": str(check.get("terminal") or ""),
                "wire": str(check.get("wire") or ""),
                "page_zone": str(check.get("page_zone") or ""),
                "meter_leads": meter,
                "expected": str(text or ""),
                "needs_info": "yes" if expected.get("needs_info") else "",
                "result": results.get(str(check.get("id") or ""), ""),
                "cause": "",
            }
        )
    for cause in payload.get("causes") or []:
        rows.append(
            {
                **common,
                "row_kind": "cause",
                "order": str(cause.get("rank") or ""),
                "energized": "",
                "device": str(cause.get("ref") or ""),
                "terminal": " ".join(str(pin) for pin in (cause.get("terminals") or []) if pin),
                "wire": str(cause.get("wire") or ""),
                "page_zone": str(cause.get("page_zone") or ""),
                "meter_leads": "",
                "expected": "",
                "needs_info": "",
                "result": "",
                "cause": str(cause.get("text") or ""),
            }
        )
    return rows


def write_session_csv(path: Path | str, rows: list[dict[str, str]]) -> Path:
    """Job-file CSV. One CRLF per row, including the header on an empty log."""

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(CSV_COLUMNS),
            lineterminator="\r\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in CSV_COLUMNS})
    return dest


def _answered(
    checks: list[dict[str, Any]],
    readings: list[dict[str, Any]] | None,
) -> dict[str, str]:
    if not readings:
        return {}
    parsed = _parse_readings(checks, readings)
    return {str(row["id"]): str(row["result"]) for row in parsed}


def _result_by_id(payload: dict[str, Any]) -> dict[str, str]:
    """Results already applied are not stored on the check. The caller passes readings separately.

    The dock stamps results onto the guide before export. A missing stamp stays blank.
    """

    stamped = payload.get("results")
    if isinstance(stamped, dict):
        return {str(key): str(value) for key, value in stamped.items()}
    return {}


def _next_check(
    checks: list[dict[str, Any]],
    answered: dict[str, str],
) -> tuple[dict[str, Any] | None, str]:
    """Next point in trace order. A Wrong reading focuses the fault window."""

    ordered = sorted(checks, key=lambda row: int(row.get("order") or 0))
    first_wrong = next((row for row in ordered if answered.get(row["id"]) == "wrong"), None)
    if first_wrong is not None:
        phase = [
            row
            for row in ordered
            if row.get("energized") == first_wrong.get("energized")
            and row["order"] <= first_wrong["order"]
        ]
        pending = [row for row in phase if row["id"] not in answered]
        if pending:
            return pending[0], "gap"
        return first_wrong, "fault"
    for energized in ("DE-ENERGIZED", "LIVE"):
        pending = [
            row
            for row in ordered
            if row.get("energized") == energized and row["id"] not in answered
        ]
        if pending:
            return pending[0], "next"
    return None, "done"


def _prompt(
    current: dict[str, Any] | None,
    role: str,
    fault: dict[str, Any],
    check_payload: dict[str, Any],
) -> str:
    if role == "done" or current is None:
        if not check_payload.get("checks"):
            return "This drawing has no checks for that device and symptom."
        return "Every check has a result. Read the likely causes, then save the log."
    place = _check_place(current)
    expected = current.get("expected") or {}
    if expected.get("needs_info"):
        expect = "The drawing does not state the expected value. Record what the meter shows."
    else:
        shown = expected.get("text") or expected.get("value") or ""
        expect = f"Expected: {shown}." if shown else "Expected value is on the drawing."
    lead = str(current.get("meter_leads") or "").strip()
    meter = f"Meter: {lead}." if lead else ""
    where = str(current.get("page_zone") or current.get("zone") or "").strip()
    page = f"Page {where}." if where else ""
    kind = (
        "Lock out and tag out, and prove zero energy."
        if current.get("energized") == "DE-ENERGIZED"
        else "This check is energized. Qualified persons only."
    )
    body = " ".join(part for part in (kind, page, f"At {place}.", meter, expect) if part)
    if role == "gap":
        return (
            "This point is still open, and it sits before the Wrong reading. "
            "Mark it so the fault is the path between the last OK and the first Wrong. "
            + body
        )
    if role == "fault":
        fault_line = _fault_text(fault)
        return " ".join(part for part in (fault_line, "Review this point.", body) if part)
    return body


def _fault_text(fault: dict[str, Any]) -> str:
    wrong = fault.get("first_wrong") if isinstance(fault, dict) else None
    if not isinstance(wrong, dict) or not wrong:
        return ""
    last = fault.get("last_ok") if isinstance(fault.get("last_ok"), dict) else None
    here = _mark_place(wrong)
    if last:
        return f"The fault is between {_mark_place(last)} and {here}."
    return f"The fault is at or before {here}."


def _mark_place(mark: dict[str, Any]) -> str:
    device = str(mark.get("device") or "").strip()
    terminal = str(mark.get("terminal") or "").strip()
    wire = str(mark.get("wire") or "").strip()
    bits = [device]
    if terminal:
        bits.append(f"terminal {terminal}")
    if wire:
        bits.append(f"wire {wire}")
    return " ".join(bit for bit in bits if bit) or "the previous point"


def _check_place(check: dict[str, Any]) -> str:
    return _mark_place(
        {
            "device": check.get("device") or "",
            "terminal": check.get("terminal") or "",
            "wire": check.get("wire") or "",
        }
    )


def _xref_lines(project: Project, catalog: Catalog, ref: str) -> list[str]:
    lines: list[str] = []
    rows = device_index_rows(project, catalog, ref)
    for row in rows:
        element = str(row.get("element") or row.get("role") or "device").strip()
        tag = str(row.get("ref") or ref).strip()
        where = str(row.get("location") or row.get("page_zone") or "").strip()
        terminals = str(row.get("terminals") or "").strip()
        wires = str(row.get("wires") or "").strip()
        bits = [f"{tag} {element}".strip()]
        if where:
            bits.append(where)
        if terminals:
            bits.append(f"terminals {terminals}")
        if wires:
            bits.append(f"wires {wires}")
        line = " — ".join(bits)
        if line not in lines:
            lines.append(line)
    return lines


def _path_line(circuit: dict[str, Any]) -> str:
    parts: list[str] = []
    for segment in _segments(circuit):
        ref = str(segment.get("ref") or "").strip()
        if not ref:
            continue
        pins = [str(pin) for pin in (segment.get("terminals") or []) if pin]
        wire = str(segment.get("wire") or "").strip()
        label = ref
        if pins:
            label += " " + "/".join(pins)
        if wire:
            label += f" (wire {wire})"
        if not parts or parts[-1] != label:
            parts.append(label)
    return " → ".join(parts)


def _segments(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    control = circuit.get("control") or {}
    rows.extend(control.get("segments") or [])
    power = circuit.get("power") or {}
    for phase in power.get("phases") or []:
        rows.extend(phase.get("segments") or [])
    return rows


def _local_now() -> datetime:
    """Clock time in the local zone, with that zone's UTC offset."""

    return datetime.now().astimezone()


def _exported_when(when: str) -> str:
    """Job-file timestamp. A blank value is local time with a numeric offset."""

    text = when.strip()
    if text:
        return text
    return _local_now().replace(microsecond=0).isoformat()


def _highlight(
    circuit: dict[str, Any],
    current: dict[str, Any] | None,
    *,
    checks: list[dict[str, Any]] | None = None,
    window: list[str] | None = None,
) -> dict[str, Any]:
    window_ids = {str(item) for item in (window or []) if item}
    if window_ids:
        return _window_highlight(list(checks or []), window_ids, current)
    devices: list[str] = []
    wires: list[str] = []
    for segment in _segments(circuit):
        ref = str(segment.get("ref") or "").strip()
        wire = str(segment.get("wire") or "").strip()
        if ref and ref not in devices:
            devices.append(ref)
        if wire and wire not in wires:
            wires.append(wire)
    focus = ""
    focus_wire = ""
    sheet = ""
    if current:
        focus = str(current.get("device") or "")
        focus_wire = str(current.get("wire") or "")
        sheet = str(current.get("sheet") or "")
        if focus and focus not in devices:
            devices.append(focus)
        if focus_wire and focus_wire not in wires:
            wires.append(focus_wire)
    if not sheet:
        for segment in _segments(circuit):
            if segment.get("sheet"):
                sheet = str(segment["sheet"])
                break
    return {
        "sheet": sheet,
        "devices": devices,
        "wires": wires,
        "focus": focus,
        "focus_wire": focus_wire,
    }


def _window_highlight(
    checks: list[dict[str, Any]],
    window_ids: set[str],
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    """Devices and wires on the fault window, and nothing else on the circuit."""

    rows = [row for row in checks if str(row.get("segment_id") or "") in window_ids]
    devices: list[str] = []
    wires: list[str] = []
    sheet = ""
    for row in rows:
        ref = str(row.get("device") or "").strip()
        wire = str(row.get("wire") or "").strip()
        if ref and ref not in devices:
            devices.append(ref)
        if wire and wire not in wires:
            wires.append(wire)
        if not sheet and row.get("sheet"):
            sheet = str(row["sheet"])
    focus = ""
    focus_wire = ""
    if current and str(current.get("segment_id") or "") in window_ids:
        focus = str(current.get("device") or "")
        focus_wire = str(current.get("wire") or "")
        if current.get("sheet"):
            sheet = str(current["sheet"])
    elif rows:
        last = rows[-1]
        focus = str(last.get("device") or "")
        focus_wire = str(last.get("wire") or "")
        if last.get("sheet"):
            sheet = str(last["sheet"])
    if focus and focus not in devices:
        focus = devices[0] if devices else ""
    if focus_wire and focus_wire not in wires:
        focus_wire = ""
    return {
        "sheet": sheet,
        "devices": devices,
        "wires": wires,
        "focus": focus,
        "focus_wire": focus_wire,
    }
