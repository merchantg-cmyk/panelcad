"""Turn a human prompt line into Session edits. No mouse clicks."""

from __future__ import annotations

import re
from typing import Any

from panelcad.core.layout import is_network_sheet
from panelcad.library.ports import effective_ports

ALIASES: list[tuple[tuple[str, ...], str]] = [
    (("ethernet switch", "eth switch", "lan switch", "network switch", "switch"), "iec.network.switch_5p"),
    (("e-stop", "estop", "emergency stop"), "iec.operators.estop"),
    (("pushbutton", "push button", "start button", "pb no", "pb"), "iec.operators.pb_no"),
    (("pilot light", "lamp", "indicator"), "iec.operators.pilot_light"),
    (("relay",), "iec.relays.k_4co_24vdc"),
    (("terminal", "terminal block"), "iec.terminals.x_2p5"),
    (("hmi", "panel"), "iec.hmi.panel_7in"),
    (("vfd", "drive"), "adc.durapulse.gs21_20p5"),
    (("plc", "cpu"), "adc.click.c2_03cpu"),
]


def _sheet_from_text(session, text: str):
    low = text.lower()
    m = re.search(r"\bpage\s+(\d+)\b", low)
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(session.project.sheets):
            return session.project.sheets[idx]
    for sheet in session.project.sheets:
        title = (sheet.title or "").lower()
        if title and title in low:
            return sheet
        if sheet.id.lower() in low.split():
            return sheet
        if sheet.kind.replace("_", " ") in low:
            return sheet
    if "ethernet" in low or " lan" in f" {low}":
        for sheet in session.project.sheets:
            if sheet.kind == "network" and (not sheet.network or sheet.network.medium in ("ethernet", "mixed", "")):
                return sheet
    return session.sheet()


def _device_from_text(session, text: str):
    low = text.lower()
    for keys, lib_id in ALIASES:
        if any(k in low for k in keys):
            try:
                return session.catalog.get(lib_id)
            except KeyError:
                continue
    words = [w for w in re.findall(r"[a-z0-9]+", low) if w not in {
        "add", "an", "a", "the", "to", "on", "page", "sheet", "and", "with",
        "please", "put", "place", "insert", "new", "of", "for",
    }]
    for word in reversed(words):
        hits = session.catalog.search(word)
        if len(hits) == 1:
            return hits[0]
        if hits:
            return hits[0]
    return None


def _empty_xy(session, sheet_id: str) -> tuple[float, float]:
    sheet = session.sheet(sheet_id)
    if not sheet.components:
        return 40.0, 40.0
    right = max(c.x for c in sheet.components)
    return right + 90.0, sheet.components[0].y


def run_prompt(session, text: str) -> dict[str, Any]:
    """Log the prompt, do the work through Session, return a human summary."""
    raw = " ".join(str(text).split())
    if not raw:
        return {"ok": False, "error": "empty prompt"}
    session.submit_prompt(raw)
    if session.actor.get("kind") != "ai":
        session.announce("Grok 4.6")

    low = raw.lower()
    done: list[str] = []

    label_cmd = re.search(
        r"\b(?:label|name)\s+(?:wire\s+)?(?:from\s+)?([A-Za-z0-9_.]+)"
        r"(?:\s+(?:to|->)\s+([A-Za-z0-9_.]+))?"
        r"\s+(?:as\s+|to\s+)?[\"']?([^\"']+?)[\"']?\s*$",
        raw,
        re.I,
    )
    if label_cmd:
        a, b, caption = label_cmd.group(1), label_cmd.group(2), label_cmd.group(3).strip()
        sheet = _sheet_from_text(session, raw)
        session.set_active_sheet(sheet.id)
        try:
            if b:
                wire = session.set_wire_label(frm=a, to=b, label=caption, sheet_id=sheet.id)
            else:
                wire = session.set_wire_label(wire_id=a, label=caption, sheet_id=sheet.id)
        except Exception as exc:  # noqa: BLE001
            session.emit("say", f"could not label wire: {exc}", status="error", error=str(exc))
            return {"ok": False, "error": str(exc), "did": done}
        done.append(f"labeled {wire.id} [{wire.caption()}]")
        session.emit("say", done[-1], status="working")
        return {"ok": True, "did": done}

    lock = re.search(
        r"\b(unlock|lock)\s+(?:connection\s+points|pins|terminals)?\s*(?:on\s+)?([A-Za-z]{1,4}\d+)\b",
        raw,
        re.I,
    )
    if lock:
        action, ref = lock.group(1).lower(), lock.group(2).upper()
        sheet = _sheet_from_text(session, raw)
        session.set_active_sheet(sheet.id)
        try:
            comp = session.set_pins_locked(ref, locked=(action == "lock"), sheet_id=sheet.id)
        except Exception as exc:  # noqa: BLE001
            session.emit("say", f"could not {action} pins on {ref}: {exc}", status="error", error=str(exc))
            return {"ok": False, "error": str(exc), "did": done}
        done.append(f"{'locked' if comp.pins_locked else 'unlocked'} pins on {comp.id}")
        session.emit("say", done[-1], status="working")
        return {"ok": True, "did": done}

    reset_pins = re.search(
        r"\breset\s+(?:connection\s+points|pins|terminals)\s+(?:on\s+)?([A-Za-z]{1,4}\d+)\b",
        raw,
        re.I,
    )
    if reset_pins:
        ref = reset_pins.group(1).upper()
        sheet = _sheet_from_text(session, raw)
        session.set_active_sheet(sheet.id)
        try:
            session.reset_pins(ref, sheet_id=sheet.id)
        except Exception as exc:  # noqa: BLE001
            session.emit("say", f"could not reset pins on {ref}: {exc}", status="error", error=str(exc))
            return {"ok": False, "error": str(exc), "did": done}
        done.append(f"reset pins on {ref}")
        session.emit("say", done[-1], status="working")
        return {"ok": True, "did": done}

    delete = re.search(r"\b(?:delete|remove|drop)\s+([A-Za-z]{1,4}\d+)\b", raw, re.I)
    if delete:
        ref = delete.group(1).upper()
        sheet = _sheet_from_text(session, raw)
        session.set_active_sheet(sheet.id)
        try:
            session.delete_component(ref, sheet_id=sheet.id)
        except Exception as exc:  # noqa: BLE001
            session.emit("say", f"could not delete {ref}: {exc}", status="error", error=str(exc))
            return {"ok": False, "error": str(exc), "did": done}
        done.append(f"deleted {ref}")
        session.emit("say", done[-1], status="working")
        return {"ok": True, "did": done}

    wire = re.search(
        r"\bwire\s+([A-Za-z0-9_.]+)\s+(?:to|->)\s+([A-Za-z0-9_.]+)\b",
        raw,
        re.I,
    )
    if wire:
        sheet = _sheet_from_text(session, raw)
        session.set_active_sheet(sheet.id)
        frm, to = wire.group(1), wire.group(2)
        try:
            w = session.wire(frm, to, sheet_id=sheet.id)
        except Exception as exc:  # noqa: BLE001
            session.emit("say", f"could not wire {frm} → {to}: {exc}", status="error", error=str(exc))
            return {"ok": False, "error": str(exc), "did": done}
        done.append(f"wired {w.frm} → {w.to}")
        session.emit("say", done[-1], status="working")
        return {"ok": True, "did": done}

    if re.search(r"\bresolve\s+needs[\s-]*info\b", low):
        from panelcad.needs_info import resolve_prompt

        msg = resolve_prompt(session)
        session.emit("say", msg, status="working")
        return {"ok": True, "did": [msg]}

    if re.search(r"\b(add|place|insert|put|create)\b", low) or _device_from_text(session, raw):
        sheet = _sheet_from_text(session, raw)
        device = _device_from_text(session, raw)
        if device is None:
            msg = "couldn't tell which part to add — name a library device (relay, switch, estop…)"
            session.emit("say", msg, status="error", error=msg)
            return {"ok": False, "error": msg, "did": done}
        session.set_active_sheet(sheet.id)
        x, y = _empty_xy(session, sheet.id)
        comp = session.place(device.library_id, x=x, y=y, sheet_id=sheet.id)
        done.append(f"placed {comp.id} ({device.name}) on {sheet.title or sheet.id}")
        if is_network_sheet(sheet) and any(p.kind == "ethernet" for p in effective_ports(device, comp)):
            unused = [p.id for p in effective_ports(device, comp) if p.kind == "ethernet"]
            for other in sheet.components:
                if other.id == comp.id:
                    continue
                odev = session.catalog.get(other.library_id)
                ports = [p.id for p in effective_ports(odev, other) if p.kind == "ethernet"]
                if not ports or not unused:
                    continue
                already = {(w.frm, w.to) for w in sheet.wires} | {(w.to, w.frm) for w in sheet.wires}
                dest = f"{other.id}.{ports[0]}"
                src = f"{comp.id}.{unused.pop(0)}"
                if (src, dest) in already:
                    continue
                try:
                    session.wire(src, dest, net="LAN", sheet_id=sheet.id)
                    done.append(f"wired {src} → {dest}")
                except Exception:  # noqa: BLE001
                    unused.insert(0, src.split(".", 1)[1])
        session.emit("say", "; ".join(done), status="working")
        return {"ok": True, "did": done, "ref": comp.id, "sheet": sheet.id}

    from panelcad.needs_info import unrecognized_prompt

    msg = unrecognized_prompt(session, raw)
    session.emit("say", msg, status="error", error=msg)
    return {"ok": False, "error": msg, "did": done}
